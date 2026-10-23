"""decision/adapters.py — Signal Engine: dai dati REALI al contratto `Signal`.

Legge il ledger (`predictions` + `match_analysis` + `matches`) — la stessa fonte
di `auto_bet._today_value_picks` — e costruisce i `Signal` della catena.
**Read-only**: nessuna scrittura, nessun ordine, nessuna rete. Il modulo
raccoglie, non giudica: la decisione (quota fuori fascia, lega esclusa, EV
insufficiente...) spetta al Risk Engine, che la MOTIVA con un `ReasonCode`.
Questo e' il motivo per cui l'adapter NON rifiltra le quote come faceva la
difesa in profondita' di `auto_bet`: filtrare qui perderebbe il motivo.

Cosa viene da dove:

| campo del Signal | fonte |
|---|---|
| `price`, `blended_prob`, `ev`, `market_prob`, `market_edge`, `tier` | `predictions` (status = tier) |
| `model_prob` | `match_analysis.prob_1/prob_X/prob_2` (Poisson+DC, pre-blend) |
| `league`, `kickoff` | `matches` |
| `data_quality.model_coverage` | `team_ratings` (`n_home` + `n_away` per squadra) |
| `confidence` | `compute_confidence()` — pesi documentati qui sotto |

⚠️ La `confidence` e' una **euristica dichiarata**, non una verita': serve a
separare i segnali da giocare in automatico da quelli da far vedere a un umano
(soglia `review_confidence_min`). Va ricalibrata sul ledger quando ci saranno
abbastanza chiusure, e per questo i pesi sono costanti esplicite.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping, Optional  # noqa: F401

from .limits import RiskLimits
from .models import DataQuality, Signal

logger = logging.getLogger("decision.adapters")

#: Partite osservate per considerare "piena" la copertura di una squadra
#: (~ il MIN_MATCHES del rating engine: sotto quella soglia il rating non c'e').
FULL_SAMPLE = 8

MODEL_PROB_BY_OUTCOME = {"1": "prob_1", "X": "prob_X", "2": "prob_2"}

#: Esiti canonici del ledger e alias del pareggio.
CANONICAL_OUTCOMES = ("1", "X", "2")
DRAW_ALIASES = ("x", "draw", "pareggio", "tie")

# Pesi di `compute_confidence` (somma massima 1.0; clamp finale 0..1).
W_GATE_PASSED = 0.35      # il segnale ha gia' superato i gate di is_sane
W_COVERAGE = 0.30         # ratings reali per entrambe le squadre
W_CALIBRATED = 0.15       # calibrazione isotonica attiva
W_EDGE_STRONG = 0.10
W_DEPTH = 0.10            # libro profondo al floor
W_CLV = 0.05

_MISSING = object()


def _row_dict(row: Any) -> dict:
    """Accetta `sqlite3.Row`, dict o mappa (mai tuple posizionali)."""
    if isinstance(row, Mapping):
        return dict(row)
    try:
        return dict(row)                       # sqlite3.Row
    except Exception:
        return {}


def _num(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out


def model_coverage(n_home: Optional[float], n_away: Optional[float],
                   *, full: int = FULL_SAMPLE) -> float:
    """0.0 = nessuna squadra con rating, 1.0 = entrambe con campione pieno.

    Lineare sul campione (una squadra con meta' delle partite vale 0.5), cosi'
    il peso del modello nel blend non viene trattato come on/off.
    """
    def one(value: Optional[float]) -> float:
        return max(0.0, min(float(value or 0) / max(full, 1), 1.0))

    return round((one(n_home) + one(n_away)) / 2.0, 4)


def compute_confidence(*, model_coverage: float, calibrated: bool, edge: float,
                       ev: float, depth_usdc: Optional[float] = None,
                       has_clv_positive: Optional[bool] = None,
                       limits: Optional[RiskLimits] = None) -> float:
    """Confidenza 0..1 del segnale (euristica DOCUMENTATA, vedi pesi in testa)."""
    limits = limits or RiskLimits.from_env()
    score = W_GATE_PASSED
    score += W_COVERAGE * max(0.0, min(float(model_coverage), 1.0))
    if calibrated:
        score += W_CALIBRATED
    strong_edge = max(limits.edge_strong, 2 * limits.edge_min)
    if edge >= strong_edge:
        score += W_EDGE_STRONG
    if depth_usdc is not None and depth_usdc >= limits.min_exec_depth_usdc * 2:
        score += W_DEPTH
    if has_clv_positive is True:
        score += W_CLV
    elif has_clv_positive is False:
        score -= W_CLV
    return round(max(0.0, min(score, 1.0)), 4)


def calibration_active() -> bool:
    """True se la calibrazione isotonica e' attiva (fail-safe: False).

    Legge il singleton di `ml_ensemble` (`get_ensemble()` carica da disco, poi
    e' in cache): il calibratore esiste ma conta solo se e' `fitted_`.
    """
    try:
        import ml_ensemble
        model = ml_ensemble.get_ensemble()
        calibrator = getattr(model, "calibrator", None)
        return bool(calibrator is not None and getattr(calibrator, "fitted_", False))
    except Exception:
        return False


def canonical_outcome(esito: str, home: str, away: str,
                      resolve: Optional[Callable[[str], str]] = None
                      ) -> Optional[str]:
    """Esito canonico 1/X/2 da un esito di ledger (None se non decidibile).

    Il ledger `predictions` NON e' omogeneo: le righe scritte da `sx_signals`
    portano gia' "1"/"X"/"2", quelle scritte da `fixture_engine` portano il
    NOME DELLA SQUADRA giocata (stessa convenzione di
    `auto_bet._canonical_esito`, che la normalizza a valle). Senza questa
    normalizzazione l'adapter scartava in silenzio i segnali con il nome
    squadra — misurato in PRODUZIONE il 15/09: 'Atlético Madrid' rifiutato
    come "esito non valido" e il confronto shadow misurava un insieme diverso
    da quello su cui la produzione scommette (3 segnali su 4 righe).

    La decisione sul lato (nome grezzo e RISOLTO confrontati con casa e
    trasferta tramite `team_names.same_team`) vive in **`team_names.side_of`**:
    una sola regola condivisa con il settlement del `tracker`, che la usa per
    lo stesso scopo (prima erano due copie, e quella del tracker conosceva
    solo il nome normalizzato). Il resolver e' iniettabile (`resolve`, default
    `default_resolver()` -> `team_names.resolve_team`).

    Fail-closed: se l'esito coincide con ENTRAMBE le squadre (dato incoerente)
    o con nessuna, ritorna None — la riga viene scartata come prima, mai un
    esito indovinato per esclusione.
    """
    raw = str(esito or "").strip()
    if raw.lower() in DRAW_ALIASES:
        return "X"
    if raw in ("1", "2"):
        return raw
    home, away = str(home or "").strip(), str(away or "").strip()
    if not raw or not (home or away):
        return None
    resolver = resolve or default_resolver()
    try:
        from team_names import side_of
        return side_of(raw, home, away, resolve=resolver)
    except Exception:
        # Ripiego deterministico solo se l'helper condiviso non e' importabile:
        # confronto esatto sul nome grezzo. Mai un contenimento di token qui.
        if raw == home:
            return "1"
        if raw == away:
            return "2"
        return None


def signal_from_row(row: Any, *, coverage: float = 0.0, calibrated: bool = False,
                    limits: Optional[RiskLimits] = None,
                    depth_usdc: Optional[float] = None,
                    has_clv_positive: Optional[bool] = None,
                    price_source: str = "ledger",
                    resolve: Optional[Callable[[str], str]] = None
                    ) -> Optional[Signal]:
    """Un `Signal` da una riga del ledger (None se inutilizzabile)."""
    data = _row_dict(row)
    match_id = str(data.get("id") or data.get("match_id") or "").strip()
    home = str(data.get("home_team") or "").strip()
    away = str(data.get("away_team") or "").strip()
    # Esito canonico: '1'/'X'/'2' oppure il nome della squadra giocata.
    outcome = canonical_outcome(str(data.get("esito") or ""), home, away,
                                resolve=resolve)
    price = _num(data.get("quota"))
    blended = _num(data.get("prob"))
    market_prob = _num(data.get("market_prob"))

    if not match_id or outcome is None:
        logger.warning("adapter: riga senza match_id/esito valido: %s", data)
        return None
    if price is None or price <= 1.0 or blended is None or market_prob is None:
        logger.warning("adapter: riga %s %s senza quota/probabilita' utilizzabili", match_id, outcome)
        return None
    if not 0.0 < blended < 1.0 or not 0.0 < market_prob < 1.0:
        logger.warning("adapter: riga %s %s con probabilita' fuori range (blend %s, mercato %s)",
                       match_id, outcome, blended, market_prob)
        return None

    limits = limits or RiskLimits.from_env()

    model_prob = _num(data.get(MODEL_PROB_BY_OUTCOME.get(outcome, "")))
    warnings: list[str] = []
    if model_prob is None or not 0.0 < model_prob < 1.0:
        model_prob = blended
        warnings.append("model_prob_assente")

    edge = _num(data.get("market_edge"))
    if edge is None:
        edge = blended - market_prob
        warnings.append("edge_calcolato")

    tier = str(data.get("status") or "").strip()
    if tier not in ("value", "strong_value", "moderate"):
        import value_filter as vf
        tier = vf.get_signal_tier(_num(data.get("ev"), 0.0) or 0.0, edge)

    if coverage < 0.5:
        warnings.append("coverage_bassa")

    quality = DataQuality(
        ratings_home_n=int(_num(data.get("n_home"), 0) or 0),
        ratings_away_n=int(_num(data.get("n_away"), 0) or 0),
        model_coverage=coverage,
        calibrated=calibrated,
        market_coherent=bool(data.get("market_coherent", True)),
        inv_sum=_num(data.get("inv_sum")),
        depth_usdc=depth_usdc,
    )
    confidence = compute_confidence(
        model_coverage=coverage, calibrated=calibrated, edge=edge,
        ev=_num(data.get("ev"), 0.0) or 0.0, depth_usdc=depth_usdc,
        has_clv_positive=has_clv_positive, limits=limits)

    kickoff = _parse_kickoff(data.get("commence_time") or data.get("kickoff"))
    if kickoff is None:
        warnings.append("kickoff_assente")
        kickoff = datetime.now(timezone.utc) + timedelta(hours=1)

    return Signal(
        match_id=match_id,
        league=str(data.get("league") or ""),
        market="1X2",
        outcome=outcome,                        # type: ignore[arg-type]
        selection_label=_selection_label(data, outcome),
        kickoff=kickoff,
        price=price,
        price_source=price_source,
        market_prob=market_prob,
        model_prob=model_prob,
        blended_prob=blended,
        edge=edge,
        ev=_num(data.get("ev")),
        tier=tier,                              # type: ignore[arg-type]
        confidence=confidence,
        data_quality=quality,
        warnings=warnings,
        reasons=[f"ledger status={data.get('status')}"],
    )


def _selection_label(data: Mapping, outcome: str) -> str:
    home = str(data.get("home_team") or "").strip()
    away = str(data.get("away_team") or "").strip()
    if outcome == "1" and home:
        return f"{home} (1)"
    if outcome == "2" and away:
        return f"{away} (2)"
    if outcome == "X":
        return f"Pareggio ({home} - {away})".strip()
    return outcome


def _parse_kickoff(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Percorso database (read-only)
# ---------------------------------------------------------------------------

#: Stessa selezione di `auto_bet._today_value_picks`, con in piu' il JOIN su
#: `match_analysis` per la probabilita' del MODELLO (pre-blend).
QUERY_OPEN_SIGNALS = """
    SELECT m.id, m.home_team, m.away_team, m.commence_time, m.league,
           p.esito, p.quota, p.prob, p.ev, p.market_prob, p.market_edge, p.status,
           a.prob_1, a.prob_X, a.prob_2
    FROM matches m
    JOIN predictions p ON m.id = p.match_id
    LEFT JOIN match_analysis a ON a.match_id = m.id
    WHERE m.commence_time >= ? AND m.commence_time < ?
      AND p.status IN ('value', 'strong_value', 'moderate')
      AND p.mercato = '1X2'
      AND p.esito_finale IS NULL
    ORDER BY p.ev DESC
"""


def default_resolver() -> Callable[[str], str]:
    """Risoluzione nomi squadra del progetto (fail-safe sul nome originale).

    ⚠️ 23/10/2026: qui si importava `resolve_team_name`, un nome che in
    `team_names` NON ESISTE (la funzione e' `resolve_team`). L'`ImportError`
    finiva nell'`except` e il resolver restituiva sempre il nome originale,
    quindi la risoluzione non avveniva MAI e `canonical_outcome` lavorava solo
    con `same_team`: misurato in produzione su Wolves vs Watford, dove
    l'esito "Wolverhampton Wanderers" non agganciava "Wolves" e un segnale
    `value` con edge +21,9pp veniva scartato in silenzio dalla catena.
    """
    def resolve(name: str) -> str:
        try:
            from team_names import resolve_team
            return resolve_team(name) or name
        except Exception:
            return name

    return resolve


def _ratings_samples(conn, names: Iterable[str]) -> dict[str, int]:
    """{nome risolto: partite osservate} da `team_ratings` (read-only)."""
    cleaned = [n for n in {str(x).strip() for x in names} if n]
    if not cleaned:
        return {}
    placeholders = ",".join("?" for _ in cleaned)
    try:
        rows = conn.execute(
            f"SELECT team, COALESCE(n_home,0) + COALESCE(n_away,0) FROM team_ratings "
            f"WHERE team IN ({placeholders})", cleaned).fetchall()
    except Exception as exc:
        logger.warning("adapter: team_ratings non leggibile (%s): copertura 0", exc)
        return {}
    out: dict[str, int] = {}
    for team, total in rows:
        out[str(team)] = int(total or 0)
    return out


def iter_signals(*, conn=None, now: Optional[datetime] = None, hours: float = 24.0,
                 limits: Optional[RiskLimits] = None, calibrated: Optional[bool] = None,
                 resolve: Optional[Callable[[str], str]] = None,
                 depth_lookup: Optional[Callable[[str, str], Optional[float]]] = None,
                 price_source: str = "ledger") -> list[Signal]:
    """`Signal` aperti nelle prossime `hours` ore (finestra mobile, come auto_bet).

    `conn` iniettabile (test offline con DB temporaneo); se assente usa
    `tracker._get_conn()`. La connessione aperta qui viene chiusa qui.
    """
    limits = limits or RiskLimits.from_env()
    resolver = resolve or default_resolver()
    if calibrated is None:
        calibrated = calibration_active()

    moment = now or datetime.now(timezone.utc)
    start = moment.isoformat().replace("+00:00", "Z")
    end = (moment + timedelta(hours=hours)).isoformat().replace("+00:00", "Z")

    own_conn = False
    if conn is None:
        from tracker import _get_conn
        conn = _get_conn()
        own_conn = True
    try:
        # Le connessioni del progetto non impostano `row_factory`: mappo per
        # NOME usando la description del cursore (vale per tuple e per Row).
        cursor = conn.execute(QUERY_OPEN_SIGNALS, (start, end))
        columns = [str(d[0]) for d in (cursor.description or [])]
        rows = [dict(zip(columns, record)) for record in cursor.fetchall()]
        samples: dict[str, int] = {}
        names: list[str] = []
        for row in rows:
            names.extend([resolver(str(row.get("home_team") or "")),
                          resolver(str(row.get("away_team") or ""))])
        samples = _ratings_samples(conn, names)

        signals: list[Signal] = []
        for row in rows:
            home = resolver(str(row.get("home_team") or ""))
            away = resolver(str(row.get("away_team") or ""))
            n_home = samples.get(home, 0)
            n_away = samples.get(away, 0)
            row["n_home"], row["n_away"] = n_home, n_away
            depth = None
            if depth_lookup is not None:
                try:
                    depth = depth_lookup(str(row.get("id") or ""), str(row.get("esito") or ""))
                except Exception:
                    depth = None
            signal = signal_from_row(
                row,
                coverage=model_coverage(n_home, n_away),
                calibrated=bool(calibrated),
                limits=limits,
                depth_usdc=depth,
                price_source=price_source,
                resolve=resolver,
            )
            if signal is not None:
                signals.append(signal)
        # INFO solo se ha trovato qualcosa: "0 segnali aperti" ripetuto a ogni
        # giro del job (60s) e' rumore puro (21/09/2026).
        logger.log(logging.INFO if signals else logging.DEBUG,
                   "adapter: %d segnali aperti su %d righe di ledger",
                   len(signals), len(rows))
        return signals
    finally:
        if own_conn:
            try:
                conn.close()
            except Exception:
                pass


__all__ = [
    "CANONICAL_OUTCOMES", "DRAW_ALIASES", "FULL_SAMPLE",
    "MODEL_PROB_BY_OUTCOME", "QUERY_OPEN_SIGNALS", "calibration_active",
    "canonical_outcome", "compute_confidence", "default_resolver",
    "iter_signals", "model_coverage", "signal_from_row",
]
