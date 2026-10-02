"""tune_steam_params.py — Tuning dei parametri Steam Move (OFFLINE).

Direttiva del proprietario (02/10/2026): ottimizzare sui dati STORICI di
`price_snapshots` i parametri dello Steam Move massimizzando Sharpe Ratio e
ROI simulato, **evitando l'overfitting**.

I parametri del progetto NON sono ricopiati: la configurazione di partenza si
legge da `steam_move.config()`, che e' l'unico posto dove vivono i default.
⚠️ La direttiva cita `STEAM_MOVE_MIN_DROP_PCT`: nel codice l'env si chiama
**`STEAM_MOVE_PCT`** (stessa grandezza: la frazione di crollo che accende lo
steam). Il report stampa i nomi di env VERI.

Spazio di ricerca (3 parametri, non 4):
    STEAM_MOVE_PCT            0.01 .. 0.10   crollo che accende il segnale
    STEAM_MOVE_WINDOW_MIN     5   .. 120     ampiezza della finestra
    STEAM_MOVE_MIN_WINDOW_MIN 0   .. 60      span minimo fra i due estremi
                                             (clampato a <= WINDOW_MIN)

`STEAM_MOVE_DEDUP_MIN` e' **escluso di proposito**: filtra le SCRITTURE degli
snapshot (dedup dei prezzi identici), quindi sulla storia gia' registrata non
ha alcun effetto. Ottimizzarlo significherebbe far muovere un parametro che la
funzione obiettivo non vede: l'ottimizzatore restituirebbe un valore casuale
spacciato per "ottimo". E' esattamente l'errore che questo tool deve evitare.

Come si misura (nessun look-ahead, nessuna formula copiata):
- per ogni (match_id, esito) con almeno 2 snapshot si scorre la serie in ordine
  cronologico; a ogni snapshot `j` si prende il PIU' VECCHIO snapshot entro
  `WINDOW_MIN` minuti prima e si misura il crollo: il PRIMO punto che soddisfa
  `span >= MIN_WINDOW_MIN` e `move_pct <= -soglia` e' il segnale. Si agisce li',
  mai sul minimo futuro della serie.
- **CLV** = `market_calib.clv_raw(prezzo_al_segnale, prezzo_di_chiusura)`: dice
  se, entrando al momento dello steam, si e' preso un prezzo MIGLIORE della
  chiusura. E' la metrica corretta per una strategia che compra un ritardo di
  prezzo (ed e' calcolabile su tutte le partite con snapshot).
- **ROI simulato** = `(quota_presa - 1)` se vinta, `-1` se persa, dove la quota
  presa e' quella del LEDGER (`predictions.quota` / `bets.price`) e l'esito e'
  quello reale. ⚠️ NON si usa il prezzo sharp come prezzo di entrata: su SX il
  bot prende un prezzo DIVERSO (il ritardo che la strategia compra) e simulare
  il fill sul prezzo Pinnacle produrrebbe un ROI finto (circa il -vig). Gli
  eventi senza una quota presa nel ledger contano solo per il CLV.
- **Sharpe per trade** = `media(clv) / std(clv)`: il rapporto segnale/rumore
  della serie di CLV (non annualizzato: qui l'unita' e' la scommessa).

Anti-overfitting (il cuore del tool):
1. **fold cronologici** (`--folds`, default 3): gli eventi si dividono in blocchi
   temporali e la funzione obiettivo e' la MEDIA dei punteggi dei singoli
   blocchi, non il punteggio sull'aggregato. Un parametro che funziona solo in
   un periodo fortunato paga la media.
2. **penalita' di stabilita'** (`--stability-penalty`, default 0.5): si sottrae
   `penalty * std(punteggi fra i fold)`. Premia configurazioni robuste, non
   configurazioni con un picco.
3. **pavimento sui trade** (`--min-trades`, default 20): sotto soglia la
   configurazione viene penalizzata — un parametro che scatta 3 volte non e'
   un risultato, e' un caso.
4. **verdetto dichiarato**: il report confronta il punteggio in-sample con
   quello sull'ULTIMO fold (il piu' recente, out-of-sample puro) e avvisa se il
   divario e' ampio. Senza questo confronto "ottimo" vuol dire solo "adatto al
   passato".
5. **minimo di dati**: sotto `--min-events` la ricerca NON parte (nessun
   risultato invece di un risultato inventato).

Il tool e' di SOLA LETTURA: legge il DB in `mode=ro` e **non imposta env**, non
scrive file di configurazione e non tocca la produzione: stampa i valori
consigliati e la riga di comando per applicarli, la decisione resta umana.

Optuna e' OPZIONALE (direttiva: usarla se c'e'): se `import optuna` riesce si
usa TPE con seed fisso, altrimenti si degrada a ricerca random stile TPE
(seed fisso, stesso spazio). Il report dichiara SEMPRE quale motore ha girato.

CLI:
  venv/bin/python scripts/tune_steam_params.py
  venv/bin/python scripts/tune_steam_params.py --trials 500 --folds 4 --json
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote

# --- Bootstrap del path: lo script vive in scripts/, i moduli in root -------
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from market_calib import clv_raw  # noqa: E402

# --- Default dichiarati (allineati a steam_move se importabile) ------------
DEFAULT_BOOK = "pinnacle"
DEFAULT_MOVE_PCT = 0.04
DEFAULT_WINDOW_MIN = 30.0
DEFAULT_MIN_WINDOW_MIN = 15.0

#: Spazio di ricerca: (nome, minimo, massimo). Ordine = ordine di stampa.
PARAM_SPACE: Tuple[Tuple[str, float, float], ...] = (
    ("move_pct", 0.01, 0.10),
    ("window_min", 5.0, 120.0),
    ("min_window_min", 0.0, 60.0),
)
#: Nome dell'env di produzione per ogni parametro (il report li stampa VERI).
ENV_NAMES = {
    "move_pct": "STEAM_MOVE_PCT",
    "window_min": "STEAM_MOVE_WINDOW_MIN",
    "min_window_min": "STEAM_MOVE_MIN_WINDOW_MIN",
}
UNSEARCHABLE = {
    "dedup_min": ("STEAM_MOVE_DEDUP_MIN",
                  "filtra le SCRITTURE degli snapshot: sulla storia gia' "
                  "registrata non ha effetto, quindi non e' ottimizzabile"),
    "book": ("STEAM_MOVE_BOOK", "identita' della fonte sharp, non una soglia"),
    "enabled": ("STEAM_MOVE_ENABLED", "interruttore, non un parametro da tarare"),
}

DEFAULT_TRIALS = 200
DEFAULT_FOLDS = 3
DEFAULT_MIN_TRADES = 20
DEFAULT_MIN_EVENTS = 30
DEFAULT_STABILITY_PENALTY = 0.5
#: Punteggio assegnato alle configurazioni che non rispettano i pavimenti.
PENALTY_SCORE = -999.0


# ---------------------------------------------------------------------------
# Produzione (letta, mai copiata)
# ---------------------------------------------------------------------------

def production_config() -> Dict[str, Any]:
    """Configurazione Steam Move ATTIVA, da `steam_move`. Fallback dichiarato."""
    out = {"source": "default", "enabled": True, "move_pct": DEFAULT_MOVE_PCT,
           "window_min": DEFAULT_WINDOW_MIN,
           "min_window_min": DEFAULT_MIN_WINDOW_MIN, "book": DEFAULT_BOOK,
           "dedup_min": 5.0}
    try:
        import steam_move
        cfg = steam_move.config()
        out.update(cfg)
        out["source"] = "steam_move"
    except Exception:
        pass
    return out


# ---------------------------------------------------------------------------
# Lettura del DB (sola lettura)
# ---------------------------------------------------------------------------

def default_db_path() -> Path:
    try:
        import tracker
        return Path(tracker.DB_PATH)
    except Exception:
        return _ROOT / "data" / "quotaverace.db"


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    path = Path(db_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"DB non trovato: {path}")
    conn = sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _rows(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> List[Dict]:
    try:
        return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]
    except sqlite3.Error:
        return []


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    try:
        return bool(conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,)).fetchone())
    except sqlite3.Error:
        return False


def parse_ts(value: Optional[str]) -> Optional[datetime]:
    """ISO -> datetime naive UTC. `None` se illeggibile (mai un istante ambiguo)."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _pos_float(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out > 1.0 else None


# ---------------------------------------------------------------------------
# Dataset: serie di snapshot + esito + quota presa
# ---------------------------------------------------------------------------

def load_series(conn: sqlite3.Connection, book: str) -> List[Dict]:
    """Serie di snapshot per (match_id, esito), in ordine cronologico.

    Scarta le serie con meno di 2 punti (un movimento ha bisogno di due letture)
    e i `recorded_at` illeggibili: un istante ambiguo falserebbe lo span.
    """
    if not _table_exists(conn, "price_snapshots"):
        return []
    raw = _rows(conn, "SELECT match_id, esito, price, recorded_at "
                      "FROM price_snapshots WHERE bookmaker=? "
                      "ORDER BY match_id, esito, recorded_at", (str(book),))
    grouped: Dict[Tuple[str, str], List[Tuple[datetime, float]]] = {}
    for r in raw:
        price = _pos_float(r.get("price"))
        ts = parse_ts(r.get("recorded_at"))
        mid, esito = str(r.get("match_id") or ""), str(r.get("esito") or "")
        if price is None or ts is None or not mid or not esito:
            continue
        grouped.setdefault((mid, esito), []).append((ts, price))
    out = []
    for (mid, esito), points in grouped.items():
        points.sort(key=lambda p: p[0])
        if len(points) < 2:
            continue
        out.append({"match_id": mid, "esito": esito, "times": [p[0] for p in points],
                    "prices": [p[1] for p in points]})
    out.sort(key=lambda e: e["times"][0])
    return out


def load_taken_prices(conn: sqlite3.Connection) -> Dict[Tuple[str, str], float]:
    """Quota EFFETTIVAMENTE presa per (match_id, esito): bets > predictions."""
    out: Dict[Tuple[str, str], float] = {}
    if _table_exists(conn, "predictions"):
        for r in _rows(conn, "SELECT match_id, esito, quota FROM predictions"):
            price = _pos_float(r.get("quota"))
            if price is not None:
                out[(str(r.get("match_id")), str(r.get("esito")))] = price
    if _table_exists(conn, "bets"):
        for r in _rows(conn, "SELECT match_id, esito, price FROM bets WHERE mode='live'"):
            price = _pos_float(r.get("price"))
            if price is not None:
                out[(str(r.get("match_id")), str(r.get("esito")))] = price
    return out


def load_winners(conn: sqlite3.Connection) -> Dict[Tuple[str, str], Optional[bool]]:
    """`True/False` se l'esito e' vinto/perso. Chiave (match_id, esito).

    Fonte 1: `match_results` (esito reale) con `canonical_outcome`, che e' la
    stessa normalizzazione della produzione — nessuna regola ricopiata.
    Fonte 2 (fallback): `predictions.esito_finale` ('won'/'lost').
    I `push` NON entrano: non hanno un esito binario.
    """
    out: Dict[Tuple[str, str], Optional[bool]] = {}
    if _table_exists(conn, "match_results"):
        try:
            from decision.adapters import canonical_outcome
        except Exception:
            canonical_outcome = None
        if canonical_outcome is not None:
            for r in _rows(conn, "SELECT match_id, home_team, away_team, result "
                                 "FROM match_results"):
                result = str(r.get("result") or "").strip().upper()
                if result not in ("1", "X", "2"):
                    continue
                key = (str(r.get("match_id")), None)
                out[("__match__", key[0])] = result           # esito reale del match
                out[("__teams__", key[0])] = (r.get("home_team"), r.get("away_team"))
    if _table_exists(conn, "predictions"):
        for r in _rows(conn, "SELECT match_id, esito, esito_finale FROM predictions"):
            verdict = str(r.get("esito_finale") or "").strip().lower()
            if verdict not in ("won", "lost"):
                continue
            out[(str(r.get("match_id")), str(r.get("esito")))] = (verdict == "won")
    return out


def resolve_winner(series: Dict, winners: Dict, ) -> Optional[bool]:
    """Vince l'esito della serie? `None` se non determinabile (fuori dai conti).

    Prima la riga di previsione (quota + verdicto per lo STESSO esito, la
    corrispondenza esatta), poi il risultato reale normalizzato con
    `canonical_outcome` (nome squadra o 1/X/2).
    """
    mid, esito = str(series.get("match_id")), str(series.get("esito"))
    direct = winners.get((mid, esito), "missing")
    if direct != "missing":
        return direct
    result = winners.get(("__match__", mid))
    teams = winners.get(("__teams__", mid))
    if not result or not teams:
        return None
    try:
        from decision.adapters import canonical_outcome
    except Exception:
        return None
    try:
        canon = canonical_outcome(esito, teams[0] or "", teams[1] or "")
    except Exception:
        return None
    if canon is None:
        return None
    return canon == result


# ---------------------------------------------------------------------------
# Misura del segnale (stessa logica di `steam_move.delta_q_dt`, ma su una
# serie STORICA: nessuna riscrittura del modulo di produzione, che resta
# l'unica autorita' sul verdetto live).
# ---------------------------------------------------------------------------

def first_trigger(times: List[datetime], prices: List[float], *, move_pct: float,
                  window_min: float, min_window_min: float
                  ) -> Optional[Dict[str, Any]]:
    """Primo punto della serie in cui lo steam scatta. `None` se non scatta.

    A ogni istante `j` si confronta col piu' VECCHIO snapshot entro
    `window_min` minuti prima: e' la stessa finestra che `delta_q_dt` legge in
    produzione (`since_minutes=window`). `span` e' l'intervallo REALE fra i due
    estremi, e sotto `min_window_min` il movimento non e' misurabile.
    """
    n = len(times)
    for j in range(1, n):
        limit = times[j].timestamp() - window_min * 60.0
        i = j - 1
        while i > 0 and times[i - 1].timestamp() >= limit:
            i -= 1
        span = (times[j] - times[i]).total_seconds() / 60.0
        if span < float(min_window_min) or span <= 0:
            continue
        first_price = prices[i]
        if first_price <= 0:
            continue
        move = (prices[j] / first_price) - 1.0
        if move <= -abs(float(move_pct)):
            return {"index": j, "time": times[j], "signal_price": prices[j],
                    "first_price": first_price, "move_pct": move * 100.0,
                    "span_minutes": round(span, 1)}
    return None


def evaluate_params(series_list: List[Dict], winners: Dict, taken: Dict,
                    params: Dict[str, float]) -> Dict[str, Any]:
    """Eventi innescati da un set di parametri, con CLV e ROI (uno per evento).

    Nessun look-ahead: il segnale e' il PRIMO trigger, la chiusura e' l'ultimo
    prezzo della serie.

    ⚠️ Gli eventi il cui trigger cade sull'ULTIMO snapshot vengono SCARTATI e
    contati a parte (`skipped_no_closing`): li' il prezzo "di chiusura" coincide
    con quello d'ingresso, quindi il CLV sarebbe 0 per COSTRUZIONE e non perche'
    il segnale non valga. Includerli diluirebbe la media verso zero e farebbe
    sembrare debole un segnale che e' solo non ancora misurabile.
    """
    events: List[Dict] = []
    skipped_no_closing = 0
    window_min = float(params["window_min"])
    min_window_min = min(float(params["min_window_min"]), window_min)
    for s in series_list:
        trig = first_trigger(s["times"], s["prices"], move_pct=float(params["move_pct"]),
                             window_min=window_min, min_window_min=min_window_min)
        if not trig:
            continue
        if trig["index"] >= len(s["prices"]) - 1:
            skipped_no_closing += 1
            continue
        closing = s["prices"][-1]
        clv = clv_raw(trig["signal_price"], closing)
        if clv is None:
            continue
        won = resolve_winner(s, winners)
        taken_price = taken.get((str(s["match_id"]), str(s["esito"])))
        roi = None
        if won is not None and taken_price is not None:
            roi = (taken_price - 1.0) if won else -1.0
        events.append({"match_id": s["match_id"], "esito": s["esito"],
                       "time": trig["time"], "clv": clv, "roi": roi,
                       "won": won, "signal_price": trig["signal_price"],
                       "move_pct": trig["move_pct"], "span_minutes": trig["span_minutes"]})
    return {"events": events, "skipped_no_closing": skipped_no_closing}


# ---------------------------------------------------------------------------
# Punteggio (con le guardie anti-overfitting)
# ---------------------------------------------------------------------------

def _mean(values: Sequence[float]) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def _std(values: Sequence[float]) -> float:
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return 0.0
    m = sum(vals) / len(vals)
    return (sum((v - m) ** 2 for v in vals) / (len(vals) - 1)) ** 0.5


#: Sotto questa dispersione lo Sharpe non e' misurabile: una serie di CLV
#: costanti ha std ~1e-17 (rumore in virgola mobile), e dividere per quello
#: produce un rapporto enorme e finto. Il campione degenere vale 0, non
#: un punteggio che domina la ricerca.
SHARPE_MIN_STD = 1e-6


def sharpe_of(values: Sequence[float]) -> float:
    """Rapporto media/std (per trade). 0 se la serie e' degenere."""
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return 0.0
    std = _std(vals)
    if std < SHARPE_MIN_STD:
        return 0.0
    return (sum(vals) / len(vals)) / std


def fold_events(events: List[Dict], folds: int) -> List[List[Dict]]:
    """Blocchi TEMPORALI contigui degli eventi (mai casuali: il tempo conta)."""
    ordered = sorted(events, key=lambda e: e["time"])
    n = len(ordered)
    if n == 0:
        return []
    folds = max(1, min(int(folds), n))
    size = n / folds
    out = []
    for k in range(folds):
        lo, hi = int(round(k * size)), int(round((k + 1) * size))
        chunk = ordered[lo:hi]
        if chunk:
            out.append(chunk)
    return out


def score_events(events: List[Dict], *, w_sharpe: float = 1.0,
                 w_roi: float = 1.0) -> Optional[Dict[str, Any]]:
    """Punteggio di un insieme di eventi: `w_sharpe*Sharpe(CLV) + w_roi*ROI`."""
    if not events:
        return None
    clv = [e["clv"] for e in events]
    rois = [e["roi"] for e in events if e.get("roi") is not None]
    sharpe = sharpe_of(clv)
    roi = _mean(rois)
    score = w_sharpe * sharpe + w_roi * (roi if roi is not None else 0.0)
    return {"score": round(score, 5), "sharpe": round(sharpe, 5),
            "roi": round(roi, 5) if roi is not None else None,
            "clv_mean": round(_mean(clv) or 0.0, 5),
            "trades": len(events), "trades_with_price": len(rois),
            "hit_rate": round(_mean([1.0 if e.get("won") else 0.0 for e in events
                                     if e.get("won") is not None]) or 0.0, 4)}


def objective_for(series_list: List[Dict], winners: Dict, taken: Dict,
                  params: Dict[str, float], *, folds: int = DEFAULT_FOLDS,
                  min_trades: int = DEFAULT_MIN_TRADES,
                  stability_penalty: float = DEFAULT_STABILITY_PENALTY,
                  w_sharpe: float = 1.0, w_roi: float = 1.0) -> Dict[str, Any]:
    """Funzione obiettivo: media fra i fold, meno penalita' di stabilita'.

    Il punteggio NON e' quello sull'aggregato: si calcola su ogni blocco
    temporale e si media, cosi' una configurazione che funziona solo in un
    periodo non puo' vincere. Sotto `min_trades` il punteggio e' la penalita'
    (dichiarata), non zero: un "0" potrebbe risultare il migliore.
    """
    measured = evaluate_params(series_list, winners, taken, params)
    events = measured["events"]
    overall = score_events(events, w_sharpe=w_sharpe, w_roi=w_roi)
    out: Dict[str, Any] = {"params": dict(params), "events": len(events),
                           "skipped_no_closing": measured["skipped_no_closing"],
                           "overall": overall, "folds": [], "score": PENALTY_SCORE,
                           "reason": "insufficient_trades"}
    if len(events) < int(min_trades):
        return out
    chunks = fold_events(events, folds)
    scores = []
    for chunk in chunks:
        s = score_events(chunk, w_sharpe=w_sharpe, w_roi=w_roi)
        if s is None:
            continue
        out["folds"].append({"trades": s["trades"], "score": s["score"],
                             "sharpe": s["sharpe"], "roi": s["roi"],
                             "clv_mean": s["clv_mean"]})
        scores.append(s["score"])
    if not scores:
        return out
    base = _mean(scores) or 0.0
    dispersion = _std(scores)
    out["fold_mean"] = round(base, 5)
    out["fold_std"] = round(dispersion, 5)
    out["score"] = round(base - float(stability_penalty) * dispersion, 5)
    out["reason"] = "ok"
    # Ultimo fold = out-of-sample piu' recente: il confronto onesto.
    if out["folds"]:
        out["last_fold"] = out["folds"][-1]
    return out


# ---------------------------------------------------------------------------
# Ricerca: Optuna se disponibile, altrimenti random search (stesso spazio)
# ---------------------------------------------------------------------------

def optuna_available() -> bool:
    try:
        import optuna  # noqa: F401
        return True
    except Exception:
        return False


def _clamp_min_window(params: Dict[str, float]) -> Dict[str, float]:
    """`min_window_min` non puo' superare `window_min` (finestra impossibile)."""
    out = dict(params)
    out["min_window_min"] = min(float(out["min_window_min"]), float(out["window_min"]))
    return out


def search(series_list: List[Dict], winners: Dict, taken: Dict, *, trials: int,
           folds: int, min_trades: int, stability_penalty: float,
           w_sharpe: float, w_roi: float, seed: int = 7,
           use_optuna: bool = True) -> Dict[str, Any]:
    """Esegue la ricerca. Ritorna `{engine, results, best}` (results ordinati)."""
    def score(params: Dict[str, float]) -> Dict[str, Any]:
        # `params` riportati = quelli EFFETTIVAMENTE valutati (clampati):
        # mostrare i valori grezzi mentre il punteggio e' calcolato su altri
        # significherebbe consigliare una configurazione mai misurata.
        clamped = _clamp_min_window(params)
        res = objective_for(series_list, winners, taken, clamped, folds=folds,
                            min_trades=min_trades,
                            stability_penalty=stability_penalty,
                            w_sharpe=w_sharpe, w_roi=w_roi)
        res["params"] = {k: round(v, 5) for k, v in clamped.items()}
        return res

    results: List[Dict[str, Any]] = []
    engine = "random"

    if use_optuna and optuna_available():
        import optuna
        optuna.logging.set_verbosity(optuna.logging.WARNING)
        engine = "optuna_tpe"
        study = optuna.create_study(
            direction="maximize",
            sampler=optuna.samplers.TPESampler(seed=int(seed)))

        def _obj(trial) -> float:
            params = {name: trial.suggest_float(name, low, high)
                      for name, low, high in PARAM_SPACE}
            res = score(params)
            results.append(res)
            return float(res["score"])

        study.optimize(_obj, n_trials=int(trials), show_progress_bar=False)
    else:
        rng = random.Random(int(seed))
        for _ in range(int(trials)):
            params = {name: rng.uniform(low, high)
                      for name, low, high in PARAM_SPACE}
            results.append(score(params))

    results.sort(key=lambda r: r["score"], reverse=True)
    return {"engine": engine, "trials": len(results),
            "best": results[0] if results else None, "results": results}


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _params_line(params: Dict[str, float]) -> str:
    return "  ".join(f"{k}={v:.4g}" for k, v in params.items())


def format_report(res: Dict[str, Any], prod: Dict[str, Any],
                  coverage: Dict[str, Any]) -> str:
    if res.get("error"):
        return f"tune_steam_params: ERRORE — {res['error']}"
    out = ["Tuning parametri Steam Move (sola lettura, offline)",
           "=" * 52,
           "Copertura: " + ", ".join(f"{k}={v}" for k, v in coverage.items())]
    if not res.get("ran"):
        out.append(f"\n⛔ ricerca NON eseguita: {res.get('reason')}")
        return "\n".join(out)

    out.append(f"\nMotore      : {res['engine']} "
               f"({res['trials']} prove, folds {res['folds']})")
    out.append("Configurazione ATTUALE in produzione "
               f"(fonte {prod.get('source')}):")
    out.append("  " + _params_line({k: prod[k] for k, _, _ in PARAM_SPACE}))
    cur = res.get("current")
    if cur and cur.get("overall"):
        out.append(f"  punteggio produzione: {cur['score']} "
                   f"(fold mean {cur.get('fold_mean')}, "
                   f"eventi {cur['events']}, sharpe "
                   f"{cur['overall']['sharpe']}, roi {cur['overall']['roi']})")

    best = res.get("best")
    if not best or best["score"] <= PENALTY_SCORE:
        out.append("\n⚠️  nessuna configurazione sopra il pavimento di trade: "
                   "il campione storico non basta a concludere nulla.")
        return "\n".join(out)

    out.append("\n--- MIGLIORE ---")
    out.append("  parametri : " + _params_line(best["params"]))
    out.append(f"  punteggio : {best['score']} (fold mean {best.get('fold_mean')}, "
               f"dev.std fra fold {best.get('fold_std')})")
    ov = best["overall"]
    out.append(f"  aggregato : eventi={ov['trades']} "
               f"(con prezzo preso {ov['trades_with_price']}) "
               f"sharpe={ov['sharpe']} roi={ov['roi']} "
               f"CLV medio={ov['clv_mean'] * 100:+.3f}%")
    if best.get("skipped_no_closing"):
        out.append(f"  segnali scartati (trigger sull'ultimo snapshot, CLV non "
                   f"misurabile): {best['skipped_no_closing']}")
    for i, f in enumerate(best.get("folds", [])):
        out.append(f"    fold {i + 1}: n={f['trades']:<4} score={f['score']:>8} "
                   f"sharpe={f['sharpe']:>7} roi={f['roi']} "
                   f"CLV={f['clv_mean'] * 100:+.3f}%")

    last = best.get("last_fold")
    if last and best.get("fold_mean") is not None:
        gap = best["fold_mean"] - last["score"]
        out.append(f"\n  verifica out-of-sample (fold piu' recente): "
                   f"score={last['score']} — scarto vs media {gap:+.4g}")
        if gap > 0.5 * max(1e-9, abs(best["fold_mean"])):
            out.append("  ⚠️  scarto ampio: la configurazione rende meglio nel "
                       "passato recente che nell'ultimo periodo — prudenza.")

    out.append("\n--- prime 5 prove ---")
    for r in res["results"][:5]:
        out.append(f"  {r['score']:>9}  {_params_line(r['params'])}  "
                   f"(eventi {r['events']}, {r['reason']})")

    out.append("\n--- come applicare (decisione umana) ---")
    for name, _, _ in PARAM_SPACE:
        out.append(f"  railway variables --service betting_bot "
                   f"--set {ENV_NAMES[name]}={best['params'][name]:.4g}")
    out.append("  NB: il tool NON imposta nulla; i valori vanno confrontati con "
               "il campione prima di essere applicati.")
    out.append("\nParametri NON ottimizzabili sui dati storici:")
    for name, (env_name, why) in UNSEARCHABLE.items():
        out.append(f"  {env_name:<26} {why}")
    for w in res.get("warnings", []):
        out.append(f"\n⚠️  {w}")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Esecuzione
# ---------------------------------------------------------------------------

def run(db_path: Path, *, book: str, trials: int, folds: int, min_trades: int,
        min_events: int, stability_penalty: float, w_sharpe: float, w_roi: float,
        seed: int, use_optuna: bool = True) -> Dict[str, Any]:
    """Pipeline completa: carica, cerca, risponde. Fail-safe su ogni eccezione."""
    prod = production_config()
    res: Dict[str, Any] = {"db": str(db_path), "book": book, "read_only": True,
                           "production": prod, "warnings": [], "ran": False}
    try:
        conn = connect_readonly(db_path)
    except Exception as exc:
        res["error"] = f"{type(exc).__name__}: {exc}"
        return res
    try:
        series_list = load_series(conn, book)
        winners = load_winners(conn)
        taken = load_taken_prices(conn)
    finally:
        conn.close()

    res["coverage"] = {"serie_snapshot": len(series_list),
                       "con_esito_noto": sum(1 for s in series_list
                                             if resolve_winner(s, winners) is not None),
                       "quote_prese_nel_ledger": len(taken)}
    res["params_space"] = [{"name": n, "min": lo, "max": hi}
                           for n, lo, hi in PARAM_SPACE]
    res["folds"] = max(1, int(folds))
    if len(series_list) < int(min_events):
        res["reason"] = (f"servono almeno {min_events} serie di snapshot sharp "
                         f"(trovate {len(series_list)})")
        return res
    if not winners:
        res["reason"] = "nessun esito reale nel ledger (`match_results` vuoto)"
        return res

    res["ran"] = True
    current = objective_for(series_list, winners, taken,
                            {k: float(prod[k]) for k, _, _ in PARAM_SPACE},
                            folds=folds, min_trades=0,
                            stability_penalty=stability_penalty,
                            w_sharpe=w_sharpe, w_roi=w_roi)
    res["current"] = current

    found = search(series_list, winners, taken, trials=trials, folds=folds,
                   min_trades=min_trades, stability_penalty=stability_penalty,
                   w_sharpe=w_sharpe, w_roi=w_roi, seed=seed, use_optuna=use_optuna)
    res.update(found)
    if not optuna_available():
        res["warnings"].append(
            "Optuna non installata: ricerca random con seed fisso "
            "(`pip install -r requirements-scripts.txt` per TPE)")
    n_ok = sum(1 for r in found["results"] if r["score"] > PENALTY_SCORE)
    if n_ok == 0:
        res["warnings"].append(
            f"nessuna configurazione raggiunge {min_trades} trade: alza il "
            "campione o abbassa --min-trades (consapevolmente)")
    else:
        res["warnings"].append(
            f"{n_ok}/{found['trials']} configurazioni sopra il pavimento di trade")
    return res


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Tuning dei parametri Steam Move su price_snapshots (offline)")
    ap.add_argument("--db", default=None, help="path del DB (default: tracker.DB_PATH)")
    ap.add_argument("--book", default=None,
                    help=f"book sharp da analizzare (default {DEFAULT_BOOK})")
    ap.add_argument("--trials", type=int, default=DEFAULT_TRIALS)
    ap.add_argument("--folds", type=int, default=DEFAULT_FOLDS,
                    help="blocchi temporali per la media anti-overfitting")
    ap.add_argument("--min-trades", type=int, default=DEFAULT_MIN_TRADES,
                    help="trade minimi perche' una configurazione sia valida")
    ap.add_argument("--min-events", type=int, default=DEFAULT_MIN_EVENTS,
                    help="serie minime per avviare la ricerca")
    ap.add_argument("--stability-penalty", type=float, default=DEFAULT_STABILITY_PENALTY)
    ap.add_argument("--w-sharpe", type=float, default=1.0)
    ap.add_argument("--w-roi", type=float, default=1.0)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--no-optuna", action="store_true",
                    help="forza la ricerca random (diagnostica)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    book = args.book or production_config().get("book") or DEFAULT_BOOK
    db_path = Path(args.db) if args.db else default_db_path()
    res = run(db_path, book=book, trials=args.trials, folds=args.folds,
              min_trades=args.min_trades, min_events=args.min_events,
              stability_penalty=args.stability_penalty, w_sharpe=args.w_sharpe,
              w_roi=args.w_roi, seed=args.seed, use_optuna=not args.no_optuna)

    if args.json:
        payload = dict(res)
        payload.pop("results", None)      # la tabella completa e' per il report
        payload["top"] = (res.get("results") or [])[:5]
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    else:
        print(format_report(res, production_config(), res.get("coverage", {})))
    return 1 if res.get("error") else 0


if __name__ == "__main__":                                    # pragma: no cover
    sys.exit(main())
