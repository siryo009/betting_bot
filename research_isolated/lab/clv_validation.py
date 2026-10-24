"""CLV VALIDATION — verifica indipendente del CLV osservato (20/10/2026).

Perche' esiste
==============
Il progetto riporta un CLV **positivo** sugli ordini eseguiti (`clv_tracker`:
+4,54% il 16/10 con n=45; +3,67% con n=58 sullo snapshot del 20/10). Un CLV
positivo e' l'unico indicatore di edge *indipendente dalla varianza dei
risultati*, quindi vale la pena verificarlo — ma proprio perche' e' l'unico,
va verificato con la stessa diffidenza con cui il progetto tratta ogni altro
numero (22/09, 25/09: due campioni storici letti come edge e poi smontati).

Questo modulo **non** si fida di `clv_tracker`: ricostruisce ogni osservazione
dalle colonne GREZZE dello snapshot, con la formula scritta sotto, e solo alla
fine confronta il risultato con la produzione (`crosscheck()`). Se i due numeri
divergono, la divergenza e' un risultato, non un fastidio.

La definizione, in un posto solo
================================
    CLV_gross = quota_acquisto / quota_chiusura - 1

positivo = abbiamo comprato a un prezzo **migliore** di quello di chiusura.
E' esattamente `market_calib.clv_raw(signal_odds, closing_odds)` (verificato a
runtime da `crosscheck()` su ogni osservazione, non "per costruzione").

⚠️ **CORREZIONE DI UN ERRORE TROVATO IN QUESTO LABORATORIO.** Il modulo
`clv_execution.py` (20/10 18:32) riportava «il segno si inverte: -3,33%» e ne
deduceva che l'edge non esiste. La sua proprieta' `Sample.clv` calcolava
`closing/buy - 1`, cioe' i due operandi **scambiati**: sulla stessa coppia di
prezzi la formula invertita da' esattamente `-3,33%` dove quella del progetto
da' `+3,67%` (dimostrato numericamente da `TestCorrezioneDelSegno`). La
narrativa di quel report ("echo", "de-vig", "segnale vs acquisto") resta utile
come elenco di trappole, ma **la sua conclusione sul segno non e' valida**.
Il modulo e' stato corretto; il suo vecchio JSON resta come reperto storico.

Cosa NON fa
===========
* non scrive sul ledger di produzione (connessione `mode=ro`, e il test prova
  che una UPDATE viene RIFIUTATA);
* non chiama la rete, non piazza ordini, non tocca i file di stato del rischio;
* non inventa un benchmark quando la chiusura non c'e': la conta a parte.

Le trappole che il modulo tratta esplicitamente
==============================================
1. **ECHO** — `clv_history.closing_quota` nasce come copia di `signal_quota`:
   confrontarla produce un CLV 0 "per costruzione" che non misura nulla.
2. **FONTE NON DICHIARATA** — `clv_history.closing_odds` scritto prima del
   13/10/2026 (quando la colonna `closing_source` non esisteva) non dice da
   dove viene. `clv_tracker` lo etichetta `pinnacle_t0` per DEFAULT del
   lettore: qui resta `sharp_legacy` e viene misurato anche da solo.
3. **SELEZIONE** — le puntate senza chiusura catturata (22/80) non sono un
   campione casuale: qui si riporta il loro ROI accanto a quello del campione
   misurato, perche' un campione selezionato sul risultato falsa il CLV.
4. **MARGINE DEL BOOK** — il CLV grezzo si confronta con una quota che
   *contiene* il vig: il vig-free si calcola solo col mercato COMPLETO
   (altrimenti si dichiara non disponibile invece di stimarlo).
5. **DOPPIO CONTEGGIO** — una puntata per (match, esito); nessuna somma di
   righe duplicate di `clv_history` (che ha piu' righe per partita).

Uso
===
    venv/bin/python -m research_isolated.lab.clv_validation [--json] [--db PATH]
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import stats

logger = logging.getLogger(__name__)

OUT_DIR = Path(__file__).resolve().parents[1] / "out"
PREREG_FILE = Path(__file__).resolve().parent / "clv_prereg.json"

#: Soglia di affidabilita' del progetto (significance.py, multi_market,
#: clv_tracker): sotto, un numero non si legge come verita'.
MIN_SAMPLES = 30

#: Il numero CITATO dal progetto (AGENTS.md, 16/10/2026): CLV medio **+4,54%**
#: su **45** osservazioni. Vive qui perche' la verifica indipendente deve
#: rispondere a QUESTA domanda ("quel numero e' vero?"), non a una domanda
#: riscritta a posteriori. La tolleranza e' dichiarata: un decimo di punto.
CITED_CLV = 0.0454
CITED_SAMPLE_N = 45
CITED_TOLERANCE = 0.001

#: La colonna `closing_source` esiste dal 13/10/2026: una chiusura scritta
#: prima non PUO' dichiarare la propria fonte. Non e' un difetto del dato, e'
#: il motivo per cui la provenienza va dichiarata invece che assunta.
SOURCE_COLUMN_DATE = datetime(2026, 10, 13, tzinfo=timezone.utc)

#: Classi di provenienza della chiusura. `sharp_legacy` NON e' "Pinnacle":
#: e' "non dichiarata, scritta dallo stesso job che oggi dichiara Pinnacle".
CLASS_SHARP_DECLARED = "sharp_declared"
CLASS_SHARP_LEGACY = "sharp_legacy"
CLASS_VENUE = "venue_internal"
CLASS_UNKNOWN = "unknown"

SHARP_DECLARED_SOURCES = ("pinnacle_t0", "pinnacle_quota")
VENUE_SOURCES = ("sx", "sxbet")

#: ⚠️ SOLO `closing_odds` e' una CHIUSURA. Le altre due colonne NON lo sono e
#: qui vengono rifiutate come tali, anche se `clv_tracker` le accetterebbe:
#:   * `closing_quota` nasce come COPIA di `signal_quota` (seme della quota di
#:     segnale) → confrontarla da' un CLV 0 per costruzione;
#:   * `pinnacle_quota` e' la quota sharp al momento dell'ANALISI (la scrive
#:     `fixture_engine`), non quella di chiusura: usarla come closing
#:     produrrebbe un CLV che misura il movimento fino al segnale, non fino al
#:     fischio.
#: Se un giorno `closing_odds` mancasse, la riga va esclusa e CONTATA
#: (`only_reference_price`): meglio un campione piu' piccolo che un numero
#: costruito su una quota che non e' quella che dichiara di essere.
CLOSING_COLUMNS = (("closing_odds", None),)
REFERENCE_COLUMNS = (("pinnacle_quota", "pinnacle_quota"),
                     ("closing_quota", "closing_quota"))

#: Motivi di esclusione (machine-readable, contati uno per uno).
EXCL_NOT_LIVE = "not_live"
EXCL_NO_ROW = "no_closing_row"
EXCL_NO_CLOSING = "no_closing_price"
EXCL_ECHO = "echo_signal"
EXCL_BAD_PRICE = "bad_buy_price"
EXCL_BAD_CLOSING = "bad_closing_price"
EXCL_DUPLICATE = "duplicate_observation"
EXCL_REFERENCE_ONLY = "only_reference_price"

STATUS_VERIFIED = "verified"
STATUS_NOT_VERIFIED = "not_verified"
STATUS_INSUFFICIENT = "insufficient"
STATUS_UNAVAILABLE = "unavailable"

#: Campi ammessi in un predicato di coorte. Volutamente NON include `clv`,
#: `profit` ne' **`result`**: una coorte scelta sull'ESITO non e' una coorte,
#: e' una selezione a posteriori. `result` era ammesso fino al 20/10/2026 —
#: la docstring diceva il contrario e il codice lo permetteva: nessun protocollo
#: lo usava, ma bastava una coorte aggiunta dopo aver visto i numeri per
#: trasformare "tutte le puntate" in "solo quelle vinte". Ora e' rifiutata
#: alla validazione (il test la blinda).
PREDICATE_FIELDS = (
    "sport", "market", "odds", "odds_band", "lead_h", "lead_band",
    "closing_source_class", "source_declared", "has_liquidity",
)


# ---------------------------------------------------------------------------
# 0. utilita' difensive (mai un'eccezione verso il chiamante)
# ---------------------------------------------------------------------------

def _num(value: Any) -> Optional[float]:
    """Numero finito o None. `None` e `NaN` non sono prezzi."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def _price(value: Any) -> Optional[float]:
    """Prezzo decimale valido: > 1.0 (sotto, non e' una quota)."""
    f = _num(value)
    return f if f is not None and f > 1.0 else None


def parse_ts(value: Any) -> Optional[datetime]:
    """ISO -> datetime con fuso. Assume UTC, ma lo FA SAPERE nel return.

    Il ledger scrive naive (`datetime.now().isoformat()`) e a volte con la
    `Z`: senza questa normalizzazione le date non si confrontano (lezione del
    17/09 e del 18/10, dove un parser rigoroso rese il ledger invisibile).
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00").replace(" ", "T", 1) if " " in text[:11] else text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _table_columns(conn: sqlite3.Connection, table: str) -> set:
    try:
        return {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _safe_rows(conn: sqlite3.Connection, sql: str, params: Sequence = (),
               errors: Optional[List[str]] = None) -> List[sqlite3.Row]:
    try:
        return list(conn.execute(sql, tuple(params)))
    except sqlite3.Error as exc:            # pragma: no cover - difensivo
        if errors is not None:
            errors.append(f"{type(exc).__name__}: {exc}")
        logger.warning("clv_validation: query fallita (%s)", exc)
        return []


def canonical_outcome(esito: Any, home: Any = "", away: Any = "") -> Optional[str]:
    """Esito canonico (`1`/`X`/`2`) con la regola di PRODUZIONE.

    Il ledger e' misto: `sx_signals` scrive `1`/`X`/`2`, `fixture_engine` il
    nome della squadra. Import pigro: la lettura dell'esito non deve trascinare
    il pacchetto `decision` nell'import del modulo.
    """
    try:
        from decision.adapters import canonical_outcome as _canon   # pigro
        return _canon(esito, home or "", away or "")
    except Exception:                       # pragma: no cover - difensivo
        text = str(esito or "").strip()
        if text in ("1", "X", "2"):
            return text
        low = text.lower()
        if low in ("draw", "pareggio", "x"):
            return "X"
        if home and text == str(home).strip():
            return "1"
        if away and text == str(away).strip():
            return "2"
        return None


def clv_gross(buy: float, closing: float) -> Optional[float]:
    """LA formula: `acquisto / chiusura - 1`.

    Delega a `market_calib.clv_raw` quando disponibile (fonte unica del
    progetto) e ricade sull'aritmetica esplicita solo se l'import fallisce —
    il contrario di ricopiare la formula e chiamarla "verifica".
    """
    if buy is None or closing is None or buy <= 1.0 or closing <= 1.0:
        return None
    try:
        from market_calib import clv_raw          # pigro: fonte unica
        return clv_raw(float(buy), float(closing))
    except Exception:                       # pragma: no cover - difensivo
        return (float(buy) / float(closing)) - 1.0


def fair_closing_odds(all_closings: Sequence[float], idx: int,
                      method: str = "power") -> Optional[float]:
    """Quota di chiusura DEVIGATA dell'esito `idx`, dal mercato COMPLETO.

    Il de-vig e' quello del progetto (`market_calib.devig`): qui si sceglie
    solo l'INDICE, che e' l'unica cosa che `clv_vig_free` fa per euristica
    (probabilita' implicita piu' vicina). Con meno di due esiti non si stima
    nulla: il margine verrebbe attribuito in silenzio all'esito mancante.
    """
    vals = [_price(o) for o in (all_closings or [])]
    if len(vals) < 2 or any(v is None for v in vals):
        return None
    if not (0 <= idx < len(vals)):
        return None
    try:
        from market_calib import devig
        probs = devig(list(vals), method=method)
    except Exception:                       # pragma: no cover - difensivo
        return None
    if not probs or idx >= len(probs):
        return None
    p = _num(probs[idx])
    if p is None or p <= 0:
        return None
    return 1.0 / p


# ---------------------------------------------------------------------------
# 1. Osservazioni: una riga per (puntata eseguita, chiusura disponibile)
# ---------------------------------------------------------------------------

@dataclass
class Observation:
    """Un'osservazione di CLV. La fonte e i due timestamp viaggiano SEMPRE."""

    obs_id: str
    bet_id: Any
    event_id: str
    match_id: str
    sport: str
    market: str
    selection: str
    odds_buy: float
    buy_ts: str
    odds_closing: Optional[float]
    closing_ts: Optional[str]
    closing_column: str
    closing_source: str
    closing_source_class: str
    clv_gross: Optional[float]
    clv_vig_free: Optional[float]
    vig_method: Optional[str]
    signal_quota: Optional[float]
    reference_odds: Optional[float]
    #: QUALE colonna ha fornito il prezzo di riferimento (`pinnacle_quota` |
    #: `closing_quota`). Senza, un prezzo "di riferimento" non e' verificabile:
    #: `closing_quota` e' una COPIA della quota d'acquisto e non un benchmark.
    reference_source: Optional[str]
    liquidity_usdc: Optional[float]
    liquidity_source: str
    result: Optional[str]
    profit: Optional[float]
    stake: Optional[float]
    kickoff: Optional[str]
    lead_h: Optional[float]
    capture_lag_min: Optional[float]
    flags: List[str] = field(default_factory=list)

    # -- derivate, dichiarate come proprieta' perche' si leggono nei report --
    @property
    def odds_band(self) -> str:
        return odds_band(self.odds_buy)

    @property
    def lead_band(self) -> str:
        return lead_band(self.lead_h)

    @property
    def source_declared(self) -> bool:
        return self.closing_source_class != CLASS_SHARP_LEGACY

    @property
    def has_liquidity(self) -> bool:
        return self.liquidity_usdc is not None

    @property
    def shopping(self) -> Optional[float]:
        """Quanto il prezzo pagato e' meglio della quota del segnale.

        E' il `+/-` del line shopping: se e' negativo abbiamo pagato peggio di
        quanto il segnale diceva, e quel costo va letto accanto al CLV.
        """
        q = _price(self.signal_quota)
        if q is None:
            return None
        return self.odds_buy / q - 1.0

    def as_row(self) -> Dict[str, Any]:
        return {
            "obs_id": self.obs_id,
            "bet_id": self.bet_id,
            "event_id": self.event_id,
            "match_id": self.match_id,
            "sport": self.sport,
            "market": self.market,
            "selection": self.selection,
            "odds_buy": round(self.odds_buy, 6),
            "buy_ts": self.buy_ts,
            "odds_closing": None if self.odds_closing is None else round(self.odds_closing, 6),
            "closing_ts": self.closing_ts,
            "closing_column": self.closing_column,
            "closing_source": self.closing_source,
            "closing_source_class": self.closing_source_class,
            "clv_gross": None if self.clv_gross is None else round(self.clv_gross, 6),
            "clv_vig_free": None if self.clv_vig_free is None else round(self.clv_vig_free, 6),
            "signal_quota": None if self.signal_quota is None else round(self.signal_quota, 6),
            "reference_odds": (None if self.reference_odds is None
                               else round(self.reference_odds, 6)),
            "reference_source": self.reference_source,
            "shopping": None if self.shopping is None else round(self.shopping, 6),
            "liquidity_usdc": self.liquidity_usdc,
            "liquidity_source": self.liquidity_source,
            "result": self.result,
            "profit": self.profit,
            "stake": self.stake,
            "kickoff": self.kickoff,
            "lead_h": None if self.lead_h is None else round(self.lead_h, 3),
            "capture_lag_min": (None if self.capture_lag_min is None
                                else round(self.capture_lag_min, 1)),
            "odds_band": self.odds_band,
            "lead_band": self.lead_band,
            "flags": ",".join(self.flags),
        }


#: Colonne del CSV = tutti i campi dell'osservazione + i derivati. Dedup
#: OBBLIGATORIO e in ordine stabile: `flags` e' gia' fra le annotazioni, e un
#: nome ripetuto produce un header con due colonne `flags` (i lettori rigorosi
#: la rinominano, chi la legge a mano crede di aver perso un campo).
CSV_FIELDS = list(dict.fromkeys(
    list(Observation.__annotations__) + ["shopping", "odds_band", "lead_band"]))


def odds_band(odds: Optional[float]) -> str:
    """Fascia quota a confini FISSI (quelle del progetto), non a quantili."""
    o = _num(odds)
    if o is None:
        return "n/d"
    if o < 1.30:
        return "<1.30"
    if o < 1.80:
        return "1.30-1.80"
    if o < 2.20:
        return "1.80-2.20"
    return ">=2.20"


def lead_band(hours: Optional[float]) -> str:
    h = _num(hours)
    if h is None:
        return "n/d"
    if h < 6:
        return "<6h"
    return ">=6h"


def sport_of(market: Any) -> str:
    m = str(market or "").strip().upper()
    if m in ("TENNIS", "ML", "ML_OT"):
        return "tennis" if m == "TENNIS" else "usa_ml"
    if m in ("1X2", "OU", "AH", "OU_OT", "AH_OT"):
        return "football"
    return m.lower() or "unknown"


def closing_of(row: Any) -> Tuple[Optional[float], str]:
    """(valore, etichetta-fonte) della CHIUSURA, con la fonte dichiarata.

    L'etichetta non inventa: se `closing_source` e' vuoto la fonte diventa
    `closing_odds?` (col punto interrogativo) e la classe del chiamante dira'
    `sharp_legacy`. Senza fonte il numero resta misurabile ma non verificabile.
    """
    value = _closing_value(row)
    if value is None:
        return None, ""
    declared = (str(row["closing_source"]).strip()
                if "closing_source" in row.keys() and row["closing_source"] else "")
    return value, (declared or "closing_odds?")


def _closing_value(row: Any) -> Optional[float]:
    """Il prezzo di chiusura, e SOLO dalla colonna che lo rappresenta."""
    if row is None:
        return None
    for column, _fallback in CLOSING_COLUMNS:
        if column in row.keys():
            value = _price(row[column])
            if value is not None:
                return value
    return None


def reference_of(row: Any) -> Tuple[Optional[float], str]:
    """La quota sharp al momento dell'ANALISI (non una chiusura).

    Serve a spiegare perche' una riga non e' misurabile ("c'e' solo il
    riferimento, non la chiusura") e a un controllo di coerenza: se il
    riferimento e' gia' uguale alla chiusura, il mercato non si e' mosso.
    """
    if row is None:
        return None, ""
    for column, label in REFERENCE_COLUMNS:
        if column not in row.keys():
            continue
        value = _price(row[column])
        if value is not None:
            return value, label
    return None, ""


def classify_source(label: str, closing_ts: Optional[datetime]) -> str:
    """Classe di provenienza dichiarata (vedi il docstring del modulo)."""
    text = (label or "").strip().lower()
    if text in SHARP_DECLARED_SOURCES:
        return CLASS_SHARP_DECLARED
    if text in VENUE_SOURCES:
        return CLASS_VENUE
    if text in ("closing_odds?", "closing_odds", ""):
        # Scritta prima che la colonna esistesse: provenance NON dichiarata.
        if closing_ts is None or closing_ts < SOURCE_COLUMN_DATE:
            return CLASS_SHARP_LEGACY
        return CLASS_UNKNOWN
    return CLASS_UNKNOWN


def _liquidity_map(conn: sqlite3.Connection) -> Dict[tuple, Tuple[float, str]]:
    """(fixture_id, market_type, selection) -> (liquidita', origine).

    Solo OU/AH hanno un prezzo di libro persistito: per 1X2 e tennis la
    liquidita' NON e' a ledger e il campo resta `n/d` (con il motivo), invece
    di essere stimata dalla quota.
    """
    out: Dict[tuple, Tuple[float, str]] = {}
    if not _table_columns(conn, "market_quotes"):
        return out
    for row in _safe_rows(conn, "SELECT fixture_id, market_type, selection, "
                                "liquidity FROM market_quotes"):
        liq = _num(row["liquidity"])
        if liq is None:
            continue
        key = (str(row["fixture_id"]), str(row["market_type"]).upper(),
               str(row["selection"]))
        out[key] = (liq, "market_quotes.liquidity")
    return out


def build_observations(*, db: Optional[str | Path] = None,
                       conn: Optional[sqlite3.Connection] = None,
                       include_echo: bool = False) -> Dict[str, Any]:
    """Costruisce il dataset delle osservazioni. Mai solleva al chiamante.

    Ritorna `{observations, excluded, pending, schema, degraded, error}`:
    ogni riga non misurabile finisce in `excluded` con il suo motivo, cosi'
    il denominatore e' esplicito (un'esclusione silenziosa e' la prima fonte
    di campioni gonfiati).
    """
    bundle: Dict[str, Any] = {
        "observations": [], "excluded": {}, "pending": [], "schema": {},
        "degraded": [], "error": None, "totals": {},
    }
    own = False
    errors: List[str] = []
    try:
        if conn is None:
            from . import loader
            conn = loader.connect(db)
            own = True
        now = datetime.now(timezone.utc)
        bet_cols = _table_columns(conn, "bets")
        clv_cols = _table_columns(conn, "clv_history")
        match_cols = _table_columns(conn, "matches")
        if not bet_cols or not clv_cols:
            bundle["error"] = "tabelle `bets`/`clv_history` assenti nello snapshot"
            return bundle
        bundle["schema"] = {"bets": sorted(bet_cols), "clv": sorted(clv_cols),
                            "has_source_column": "closing_source" in clv_cols}
        for missing in (c for c in ("home", "away", "commence") if c not in bet_cols):
            bundle["degraded"].append(
                f"`bets.{missing}` assente: kickoff e nomi si leggono da `matches` "
                "(che viene potata, quindi la copertura cala sulle righe vecchie)")
        if "closing_source" not in clv_cols:
            bundle["degraded"].append(
                "nessuna colonna `closing_source`: ogni chiusura e' "
                "`sharp_legacy` per costruzione")
        if "betfair_odds" not in clv_cols:
            bundle["degraded"].append(
                "secondo benchmark (`betfair_odds`) assente: nessuna conferma "
                "indipendente disponibile")

        matches = {}
        if match_cols:
            for row in _safe_rows(conn, "SELECT id, home_team, away_team, "
                                        "commence_time, league FROM matches",
                                  errors=errors):
                matches[str(row["id"])] = dict(row)

        # Righe di clv indicizzate per partita: `clv_history` ha PIU' righe per
        # evento (un esito per riga, piu' riscritture). La chiusura si cerca
        # per ESITO CANONICO, mai per "prima riga disponibile".
        by_match: Dict[str, List[dict]] = {}
        for row in _safe_rows(conn, "SELECT * FROM clv_history", errors=errors):
            by_match.setdefault(str(row["match_id"]), []).append(dict(row))

        liquidity = _liquidity_map(conn)
        sql_bets = ("SELECT * FROM bets" if "mode" not in bet_cols
                    else "SELECT * FROM bets WHERE lower(coalesce(mode,''))='live'")
        seen: Dict[tuple, str] = {}
        scanned = 0
        for row in _safe_rows(conn, sql_bets, errors=errors):
            bet = dict(row)
            mode = str(bet.get("mode") or "").lower()
            if "mode" in bet_cols and mode != "live":
                _bump(bundle["excluded"], EXCL_NOT_LIVE)
                continue
            scanned += 1
            match_id = str(bet.get("match_id") or "")
            info = matches.get(match_id, {})
            home = str(bet.get("home") or info.get("home_team") or "")
            away = str(bet.get("away") or info.get("away_team") or "")
            target = canonical_outcome(bet.get("esito"), home, away)
            if target is None:
                target = str(bet.get("esito") or "").strip()

            chosen = None
            for candidate in by_match.get(match_id, []):
                canon = canonical_outcome(candidate.get("esito"), home, away)
                if (canon or str(candidate.get("esito") or "").strip()) == target:
                    chosen = candidate
                    break
            if chosen is None:
                _bump(bundle["excluded"], EXCL_NO_ROW)
                bundle["pending"].append({"bet_id": bet.get("id"),
                                          "match_id": match_id,
                                          "reason": EXCL_NO_ROW})
                continue

            odds_buy = _price(bet.get("price"))
            if odds_buy is None:
                _bump(bundle["excluded"], EXCL_BAD_PRICE)
                continue
            odds_close, label = closing_of(chosen)
            ref_odds, ref_label = reference_of(chosen)
            if odds_close is None:
                # C'e' il riferimento sharp dell'analisi ma NON la chiusura:
                # e' un motivo DIVERSO e va contato a parte (una quota che non
                # e' quella di chiusura non si usa come closing).
                reference = ref_odds
                reason = EXCL_REFERENCE_ONLY if reference is not None else EXCL_NO_CLOSING
                _bump(bundle["excluded"], reason)
                bundle["pending"].append({"bet_id": bet.get("id"),
                                          "match_id": match_id,
                                          "reason": reason})
                continue
            if odds_close <= 1.0:
                _bump(bundle["excluded"], EXCL_BAD_CLOSING)
                continue
            signal_quota = _price(chosen.get("signal_quota"))
            if signal_quota is not None and abs(signal_quota - odds_close) < 1e-9:
                # Chiusura = eco del segnale: CLV 0 per costruzione.
                if not include_echo:
                    _bump(bundle["excluded"], EXCL_ECHO)
                    bundle["pending"].append({"bet_id": bet.get("id"),
                                              "match_id": match_id,
                                              "reason": EXCL_ECHO})
                    continue

            key = (match_id, target)
            if key in seen:
                _bump(bundle["excluded"], EXCL_DUPLICATE)
                continue
            seen[key] = str(bet.get("id"))

            closing_ts = parse_ts(chosen.get("updated_at"))
            buy_ts = parse_ts(bet.get("created_at"))
            kickoff = parse_ts(bet.get("commence") or info.get("commence_time"))
            lead_h = None
            if kickoff and buy_ts:
                lead_h = (kickoff - buy_ts).total_seconds() / 3600.0
            lag_min = None
            if kickoff and closing_ts:
                lag_min = (closing_ts - kickoff).total_seconds() / 60.0

            # Vig-free SOLO col mercato completo: gli esiti fratelli stanno
            # nelle altre righe di clv dello stesso match.
            others, idx = _market_closings(by_match.get(match_id, []), chosen,
                                           home, away, target, odds_close)
            fair = fair_closing_odds(others, idx) if others and idx is not None else None
            clv_vf = clv_gross(odds_buy, fair) if fair else None

            obs = Observation(
                obs_id=f"{bet.get('id')}:{match_id[:14]}:{target}",
                bet_id=bet.get("id"), event_id=match_id, match_id=match_id,
                sport=sport_of(bet.get("mercato")),
                market=str(bet.get("mercato") or ""),
                selection=str(bet.get("esito") or ""),
                odds_buy=odds_buy,
                buy_ts=buy_ts.isoformat() if buy_ts else "",
                odds_closing=odds_close,
                closing_ts=closing_ts.isoformat() if closing_ts else None,
                closing_column="closing_odds",
                closing_source=label,
                closing_source_class=classify_source(label, closing_ts),
                clv_gross=clv_gross(odds_buy, odds_close),
                clv_vig_free=clv_vf,
                vig_method="power" if clv_vf is not None else None,
                signal_quota=signal_quota,
                reference_odds=ref_odds,
                reference_source=ref_label,
                liquidity_usdc=_liquidity_for(liquidity, match_id, bet),
                liquidity_source=("market_quotes.liquidity"
                                  if _liquidity_for(liquidity, match_id, bet) is not None
                                  else "not_persisted"),
                result=(str(bet.get("esito_finale")) if bet.get("esito_finale") else None),
                profit=_num(bet.get("profit")),
                stake=_num(bet.get("stake")),
                kickoff=kickoff.isoformat() if kickoff else None,
                lead_h=lead_h, capture_lag_min=lag_min,
            )
            if obs.closing_source_class == CLASS_SHARP_LEGACY:
                obs.flags.append("source_not_declared")
            if kickoff is None:
                obs.flags.append("kickoff_unknown")
            if obs.clv_vig_free is None:
                obs.flags.append("vig_free_unavailable")
            bundle["observations"].append(obs)

        obs_list = bundle["observations"]
        obs_list.sort(key=lambda o: o.buy_ts or "")
        bundle["totals"] = {
            "live_bets": scanned,
            "measured": len(obs_list),
            "pending": len(bundle["pending"]),
            "excluded": dict(bundle["excluded"]),
        }
        if errors:
            bundle["degraded"].append("errori di lettura: " + "; ".join(errors))
        del now
        return bundle
    except Exception as exc:                # pragma: no cover - difensivo
        bundle["error"] = f"{type(exc).__name__}: {exc}"
        logger.warning("clv_validation: costruzione osservazioni fallita (%s)", exc)
        return bundle
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _market_closings(rows: Sequence[dict], chosen: dict, home: str, away: str,
                     target: str, closing: float) -> Tuple[List[float], Optional[int]]:
    """Chiusure del mercato COMPLETO (per il de-vig) e indice dell'esito.

    Per un 1X2 servono tre righe, per un testa-a-testa due. Se ne manca una il
    mercato non e' de-vigabile e il vig-free non si calcola (mai una stima del
    margine spacciata per misura).
    """
    ordered: List[Tuple[str, float]] = []
    for row in rows:
        canon = canonical_outcome(row.get("esito"), home, away)
        value, _ = closing_of(row)
        if canon is None or value is None:
            continue
        if canon in [c for c, _ in ordered]:
            continue
        ordered.append((canon, value))
    if len(ordered) < 2:
        return [], None
    full_order = ["1", "X", "2"] if len(ordered) >= 3 else ["1", "2"]
    table = dict(ordered)
    if not all(k in table for k in full_order):
        return [], None
    values = [table[k] for k in full_order]
    idx = full_order.index(target) if target in full_order else None
    if idx is None:
        return [], None
    # Il valore dell'esito deve essere QUELLO della riga scelta (non una
    # riscrittura diversa della stessa partita).
    if abs(values[idx] - closing) > 1e-9:
        return [], None
    return values, idx


def _liquidity_for(liquidity: Dict[tuple, Tuple[float, str]], match_id: str,
                   bet: dict) -> Optional[float]:
    market = str(bet.get("mercato") or "").upper()
    if market not in ("OU", "AH"):
        return None
    selection = str(bet.get("esito") or "").strip()
    for key, (liq, _src) in liquidity.items():
        if key[0] == match_id and key[2] == selection:
            return liq
    return None


def _bump(counter: Dict[str, int], key: str) -> None:
    counter[key] = counter.get(key, 0) + 1


# ---------------------------------------------------------------------------
# 2. Statistica per coorte (intervalli sempre, mai un numero puntuale)
# ---------------------------------------------------------------------------

def summarise(values: Sequence[float], clusters: Optional[Sequence] = None,
              key: str = "gross", min_n: int = MIN_SAMPLES) -> Dict[str, Any]:
    """Statistica di un insieme di CLV: media, CI bootstrap a cluster, p.

    Il bootstrap e' a CLUSTER per partita (due selezioni sullo stesso evento
    non sono indipendenti) e il p-value e' il t-test sulla media, la stessa
    scelta di `significance.roi_test` — non un test inventato qui.
    """
    vals = [float(v) for v in values if v is not None]
    out: Dict[str, Any] = {"key": key, "n": len(vals), "mean": None,
                           "median": None, "ci95": None, "p_value": None,
                           "share_positive": None, "share_ci95": None,
                           "min_detectable": None, "status": STATUS_UNAVAILABLE,
                           "note": ""}
    if not vals:
        out["note"] = "nessuna osservazione"
        return out
    ci = stats.bootstrap_mean_ci(vals, clusters)
    out["mean"] = ci.get("mean")
    out["ci95"] = [ci.get("lo"), ci.get("hi")]
    out["clusters"] = ci.get("clusters")
    out["median"] = sorted(vals)[len(vals) // 2]
    out["n_positive"] = sum(1 for v in vals if v > 0)
    out["share_positive"] = out["n_positive"] / len(vals)
    lo, hi = stats.wilson_ci(out["n_positive"], len(vals))
    out["share_ci95"] = [lo, hi]
    out["sd"] = stats.std(vals)
    out["min_detectable"] = stats.min_detectable_effect(len(vals), out["sd"] or 0.0)
    out["p_value"] = _p_value(vals)
    if len(vals) < min_n:
        out["status"] = STATUS_INSUFFICIENT
        out["note"] = (f"campione {len(vals)} < {min_n}: nessuna conclusione, "
                       "l'intervallo e' troppo largo")
        return out
    low, high = out["ci95"][0], out["ci95"][1]
    if low is not None and low > 0:
        out["status"] = STATUS_VERIFIED
    elif high is not None and high < 0:
        out["status"] = STATUS_NOT_VERIFIED
    else:
        out["status"] = STATUS_INSUFFICIENT
        out["note"] = "intervallo che include lo zero: direzione non dimostrata"
    return out


def _p_value(values: Sequence[float]) -> Optional[float]:
    """t-test a due code sulla media (scipy se c'e', altrimenti None)."""
    vals = [float(v) for v in values if v is not None]
    if len(vals) < 3:
        return None
    try:
        from scipy import stats as sps
    except Exception:                       # pragma: no cover - opzionale
        return None
    sd = stats.std(vals)
    if not sd:
        return None
    try:
        res = sps.ttest_1samp(vals, 0.0)
        p = _num(getattr(res, "pvalue", None))
        return p
    except Exception:                       # pragma: no cover - difensivo
        return None


def roi_block(observations: Sequence[Observation]) -> Dict[str, Any]:
    """ROI REALIZZATO del campione (e non: del CLV)."""
    rows = [o for o in observations if o.profit is not None and o.stake]
    if not rows:
        return {"n": 0, "pnl": None, "staked": None, "roi": None,
                "ci95": None, "hit_rate": None}
    pnl = sum(float(o.profit) for o in rows)
    staked = sum(float(o.stake) for o in rows)
    units = [float(o.profit) / float(o.stake) for o in rows]
    ci = stats.bootstrap_mean_ci(units, [o.match_id for o in rows])
    wins = sum(1 for r in rows if (r.result or "").lower() in ("won", "vinta", "1"))
    lo, hi = stats.wilson_ci(wins, len(rows))
    return {
        "n": len(rows), "pnl": round(pnl, 4), "staked": round(staked, 4),
        "roi": (pnl / staked) if staked else None,
        "ci95": [ci.get("lo"), ci.get("hi")],
        "hit_rate": wins / len(rows), "hit_ci95": [lo, hi],
    }


# ---------------------------------------------------------------------------
# 3. Coorti PREREGISTRATE (lette da un file congelato, non scritte qui)
# ---------------------------------------------------------------------------

def load_prereg(path: Optional[Path | str] = None) -> Dict[str, Any]:
    """Legge il protocollo congelato e ne calcola l'impronta.

    L'impronta (sha256 del contenuto canonico) viaggia nei risultati: se il
    file viene modificato dopo la raccolta, l'impronta non combacia piu' e il
    report lo dichiara (`prereg_modified`). E' l'unico modo per dimostrare che
    le coorti non sono state scelte guardando i numeri.
    """
    p = Path(path or PREREG_FILE)
    out: Dict[str, Any] = {"path": str(p), "cohorts": [], "hypothesis": None,
                           "sha256": None, "error": None, "frozen_at": None}
    try:
        raw = p.read_bytes()
    except OSError as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out
    import hashlib
    out["sha256"] = hashlib.sha256(raw).hexdigest()
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        out["error"] = f"JSON non leggibile: {exc}"
        return out
    out["cohorts"] = list(data.get("cohorts") or [])
    out["hypothesis"] = data.get("hypothesis")
    out["frozen_at"] = data.get("frozen_at")
    out["declared_primary"] = data.get("primary")
    out["errors"] = validate_cohorts(out["cohorts"])
    return out


def validate_cohorts(cohorts: Sequence[dict]) -> List[str]:
    """Un predicato che tocca il RISULTATO e' una selezione a posteriori."""
    problems: List[str] = []
    for c in cohorts or []:
        cid = c.get("id") or "?"
        if not c.get("where"):
            # Una coorte senza predicato e' ammessa SOLO se dichiara `all`:
            # e' la coorte primaria ("tutte le osservazioni misurate"), e
            # scriverla come predicato vuoto e' piu' onesto che inventarne
            # uno che sembri un filtro.
            if c.get("all") is True:
                continue
            problems.append(f"{cid}: predicato assente")
            continue
        for clause in c["where"]:
            field_name = clause.get("field")
            if field_name not in PREDICATE_FIELDS:
                problems.append(
                    f"{cid}: campo '{field_name}' non ammesso (una coorte si "
                    "definisce su variabili ex-ante)")
    return problems


def cohort_rows(observations: Sequence[Observation], cohort: dict
                ) -> List[Observation]:
    """Applica il predicato dichiarativo della coorte."""
    out = []
    for obs in observations:
        if all(_clause_ok(obs, c) for c in cohort.get("where") or []):
            out.append(obs)
    return out


def _clause_ok(obs: Observation, clause: dict) -> bool:
    field_name = clause.get("field")
    value = getattr(obs, field_name, None)
    if "in" in clause:
        allowed = [str(v) for v in clause["in"]]
        if isinstance(value, bool):
            return str(value).lower() in [a.lower() for a in allowed]
        return str(value) in allowed
    if "gte" in clause:
        num = _num(value)
        return num is not None and num >= float(clause["gte"])
    if "lt" in clause:
        num = _num(value)
        return num is not None and num < float(clause["lt"])
    if "eq" in clause:
        return value == clause["eq"]
    return False


def evaluate_cohorts(observations: Sequence[Observation],
                     cohorts: Sequence[dict]) -> List[Dict[str, Any]]:
    """Statistica per ogni coorte preregistrata (in ordine, senza filtri)."""
    out = []
    for c in cohorts:
        rows = cohort_rows(observations, c)
        vals = [o.clv_gross for o in rows]
        block = summarise(vals, [o.match_id for o in rows],
                          key=f"{c.get('id')}:gross")
        block["id"] = c.get("id")
        block["label"] = c.get("label")
        block["primary"] = bool(c.get("primary"))
        block["rationale"] = c.get("rationale")
        block["roi"] = roi_block(rows)
        vf = [o.clv_vig_free for o in rows if o.clv_vig_free is not None]
        # ⚠️ Il vig-free si calcola solo dove il mercato e' COMPLETO (3 esiti
        # per un 1X2, 2 per un testa-a-testa), quindi vive su un sottoinsieme:
        # confrontarlo con il grezzo del campione INTERO sarebbe un confronto
        # fra popolazioni diverse (e il 20/10 dava +5.49% contro +4.54% con
        # 6 contro 57 righe). Qui si isola il grezzo sugli STESSI campioni.
        gross_on_vf = [o.clv_gross for o in rows
                       if o.clv_vig_free is not None and o.clv_gross is not None]
        block["vig_free"] = {
            "n": len(vf),
            "mean": (sum(vf) / len(vf)) if vf else None,
            "gross_on_same_n": len(gross_on_vf),
            "gross_on_same": ((sum(gross_on_vf) / len(gross_on_vf))
                              if gross_on_vf else None),
            "note": ("mercato completo disponibile: confronto APPAIATO col "
                     "grezzo sugli stessi campioni" if vf else
                     "mercato incompleto: vig-free non calcolabile"),
        }
        out.append(block)
    return out


# ---------------------------------------------------------------------------
# 4. Confronto con la produzione (il numero che il progetto riporta)
# ---------------------------------------------------------------------------

def crosscheck(observations: Sequence[Observation],
               db: Optional[str | Path] = None) -> Dict[str, Any]:
    """Ricalcola il CLV con `clv_tracker` e confronta OSSERVAZIONE PER
    OSSERVAZIONE (non solo la media: due errori opposti si compensano).

    Se il modulo di produzione non e' importabile, il confronto si dichiara
    non disponibile — mai "coincide" per assenza di dati.
    """
    out: Dict[str, Any] = {"available": False, "error": None,
                           "our_n": len(observations), "their_n": None,
                           "our_mean": None, "their_mean": None,
                           "max_abs_diff": None, "mismatches": []}
    try:
        import clv_tracker as ct
    except Exception as exc:                # pragma: no cover - difensivo
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out
    try:
        bundle = ct.collect(db=str(db)) if db else ct.collect()
        block = ct.summarize(bundle.get("samples") or [])
    except Exception as exc:                # pragma: no cover - difensivo
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out
    out["available"] = True
    out["their_n"] = block.get("n")
    out["their_mean"] = block.get("avg_clv")
    ours = [o for o in observations if o.clv_gross is not None]
    out["our_mean"] = (sum(o.clv_gross for o in ours) / len(ours)) if ours else None
    # Confronto per chiave (match + esito canonico).
    theirs: Dict[tuple, float] = {}
    for sample in bundle.get("samples") or []:
        key = (str(sample.get("match_id")),
               str(sample.get("outcome") or sample.get("esito") or ""))
        value = _num(sample.get("clv"))
        if value is not None:
            theirs.setdefault(key, value)
    diffs = []
    for obs in ours:
        value = theirs.get((obs.match_id, obs.selection))
        if value is None:
            value = theirs.get((obs.match_id, str(obs.selection)))
        if value is None:
            continue
        diffs.append(abs(value - obs.clv_gross))
    out["compared"] = len(diffs)
    out["max_abs_diff"] = max(diffs) if diffs else None
    out["mismatches"] = [d for d in diffs if d > 1e-6]
    out["match"] = bool(diffs) and not out["mismatches"]
    out["delta_mean"] = (None if out["their_mean"] is None or out["our_mean"] is None
                         else out["our_mean"] - out["their_mean"])
    return out


def definition_variants(observations: Sequence[Observation],
                        conn: sqlite3.Connection) -> Dict[str, Any]:
    """Le definizioni ALTERNATIVE, per escluderle con i numeri e non a parole.

    Un numero puo' venire da tre definizioni diverse di "chiusura":
      * `closing_odds` — la chiusura vera (quella usata dal report);
      * il RIFERIMENTO sharp dell'analisi (`pinnacle_quota`/`closing_quota`):
        misura il movimento fino al SEGNALE, non fino al fischio;
      * `closing_quota` come se fosse una chiusura: e' una COPIA della quota
        di segnale, quindi il CLV tende a zero per costruzione.

    Se le tre dessero lo stesso numero, la scelta della colonna non avrebbe
    conseguenze. Non la danno: ecco perche' la definizione va dichiarata.
    """
    rows: Dict[tuple, dict] = {}
    for row in _safe_rows(conn, "SELECT * FROM clv_history"):
        rows.setdefault((str(row["match_id"]), str(row["esito"] or "").strip()),
                        dict(row))
    ref_vals: List[float] = []
    seme_vals: List[float] = []
    for obs in observations:
        row = rows.get((obs.match_id, obs.selection))
        if not row:
            continue
        ref, _label = reference_of(row)
        if ref is not None:
            value = clv_gross(obs.odds_buy, ref)
            if value is not None:
                ref_vals.append(value)
        seme = _price(row.get("closing_quota"))
        if seme is not None:
            value = clv_gross(obs.odds_buy, seme)
            if value is not None:
                seme_vals.append(value)
    def _mean(vals: Sequence[float]) -> Optional[float]:
        return (sum(vals) / len(vals)) if vals else None
    return {
        "closing_odds": {"n": len(observations),
                         "mean": _mean([o.clv_gross for o in observations
                                        if o.clv_gross is not None])},
        "reference_as_closing": {"n": len(ref_vals), "mean": _mean(ref_vals)},
        "seme_as_closing": {"n": len(seme_vals), "mean": _mean(seme_vals)},
    }


def crosscheck_formula(observations: Sequence[Observation]) -> Dict[str, Any]:
    """La formula scritta qui coincide con `market_calib.clv_raw` su OGNI riga.

    Senza questo controllo "la definizione e' quella del progetto" sarebbe una
    dichiarazione; con questo e' un fatto verificato riga per riga.
    """
    bad = []
    checked = 0
    for obs in observations:
        if obs.clv_gross is None or obs.odds_closing is None:
            continue
        try:
            from market_calib import clv_raw
            expected = clv_raw(obs.odds_buy, obs.odds_closing)
        except Exception:                   # pragma: no cover - difensivo
            expected = obs.odds_buy / obs.odds_closing - 1.0
        checked += 1
        if expected is None or abs(expected - obs.clv_gross) > 1e-9:
            bad.append(obs.obs_id)
    return {"checked": checked, "mismatches": bad, "ok": checked > 0 and not bad}


def sign_sanity(buy: float, closing: float) -> Dict[str, float]:
    """Le due orientazioni, per rendere impossibile confonderle.

    `forward` e' la definizione del progetto (positivo = prezzo migliore del
    nostro); `inverted` e' l'errore trovato in `clv_execution` (scambio degli
    operandi) e serve solo come reperto: `abs(forward - inverted) > 0` sempre.
    """
    return {"forward": buy / closing - 1.0, "inverted": closing / buy - 1.0}


# ---------------------------------------------------------------------------
# 5. Ripartizioni (fonte, sport, fascia, lead time) + trappole
# ---------------------------------------------------------------------------

def reference_quality(observations: Sequence[Observation]) -> Dict[str, Any]:
    """DA DOVE viene il prezzo di riferimento, e con che timestamp.

    Il prezzo di riferimento serve a distinguere due domande diverse: "il
    mercato si e' mosso verso di noi fino al fischio?" (chiusura) e "il
    mercato si era gia' mosso quando abbiamo comprato?" (riferimento
    dell'analisi). Le due colonne che lo contengono NON sono equivalenti:
    `closing_quota` e' una copia della quota d'acquisto, quindi un CLV
    calcolato su di essa e' ~0 per costruzione. Per questo la fonte viaggia
    con l'osservazione (`reference_source`) e qui si conta.

    ⚠️ Il ledger (`clv_history`) NON registra un timestamp separato per
    `pinnacle_quota`: la riga ha un solo `updated_at`, che e' l'istante di
    cattura della CHIUSURA. Il limite e' dichiarato, non colmato con una
    stima.
    """
    counts: Dict[str, int] = {}
    for o in observations:
        key = o.reference_source or "assente"
        counts[key] = counts.get(key, 0) + 1
    return {
        "n": sum(counts.values()),
        "by_source": counts,
        "seme_copy": counts.get("closing_quota", 0),
        "timestamp_available": False,
        "note": ("`clv_history` non ha un timestamp per il prezzo di "
                 "riferimento: `updated_at` e' l'istante della CHIUSURA. Il "
                 "riferimento si usa come confronto di prezzo, mai come "
                 "chiusura."),
    }


def closing_quality(observations: Sequence[Observation]) -> Dict[str, Any]:
    """QUALITA' del timestamp di chiusura: quanto e' vicino al fischio?

    Una "chiusura" catturata due ore prima del kickoff non e' la closing line:
    misura il movimento fino a quel momento. Il dato esiste per ogni riga
    (`capture_lag_min`), ma senza aggregarlo nessuno se ne accorge.
    """
    lags = [float(o.capture_lag_min) for o in observations
            if o.capture_lag_min is not None]
    out: Dict[str, Any] = {"n": len(lags), "missing": len(observations) - len(lags),
                           "median_min_before": None, "p90_min_before": None,
                           "share_after_kickoff": None, "share_before_30": None,
                           "note": ""}
    if not lags:
        out["note"] = "nessuna riga con kickoff a ledger: ritardo non misurabile"
        return out
    # `capture_lag_min` = closing_ts - kickoff: NEGATIVO = catturata prima del
    # fischio. Il segno si riporta come "minuti PRIMA", che e' la grandezza che
    # dice se la chiusura e' davvero la closing line.
    lags.sort()
    median = lags[len(lags) // 2]
    p90 = lags[min(len(lags) - 1, int(round(0.9 * (len(lags) - 1))))]
    out["median_min_before"] = round(-median, 1)
    out["p90_min_before"] = round(-p90, 1)
    out["share_after_kickoff"] = sum(1 for v in lags if v > 0) / len(lags)
    out["share_before_30"] = sum(1 for v in lags if v < -30) / len(lags)
    return out


def cited_reproduction(observations: Sequence[Observation]) -> Dict[str, Any]:
    """Il numero CITATO dal progetto e' riproducibile dalla serie?

    Il +4,54% riportato il 16/10/2026 (AGENTS.md, 45 osservazioni) e' una
    FOTOGRAFIA di un campione che poi e' cresciuto: la domanda corretta non e'
    "il numero di oggi e' 4,54?" ma "la serie contiene quel valore al punto in
    cui il campione aveva quella dimensione?".

    Si ordinano le osservazioni per istante d'ACQUISTO e si prende la media
    sulle prime `CITED_SAMPLE_N` — poi si confronta col valore citato con una
    tolleranza DICHIARATA. La media sull'intera serie e' riportata accanto:
    le due insieme dicono se il numero e' un errore di calcolo (le prime 45 non
    danno 4,54) o una fotografia (le danno, e la serie oggi vale meno).
    """
    rows = sorted([o for o in observations
                   if o.clv_gross is not None and o.buy_ts],
                  key=lambda o: o.buy_ts)
    out: Dict[str, Any] = {
        "cited": CITED_CLV, "cited_n": CITED_SAMPLE_N,
        "tolerance_pp": CITED_TOLERANCE * 100,
        "available": len(rows) >= CITED_SAMPLE_N,
        "n": min(len(rows), CITED_SAMPLE_N),
        "mean": None, "delta_pp": None, "reproduced": None,
        "current": None, "current_n": len(rows), "note": "",
    }
    if not rows:
        out["note"] = "nessuna osservazione con istante d'acquisto"
        return out
    window = rows[:CITED_SAMPLE_N]
    out["mean"] = sum(o.clv_gross for o in window) / len(window)
    out["current"] = sum(o.clv_gross for o in rows) / len(rows)
    out["delta_pp"] = (out["mean"] - CITED_CLV) * 100
    if not out["available"]:
        out["note"] = (f"la serie ha solo {len(rows)} osservazioni: le prime "
                       f"{CITED_SAMPLE_N} esistono, il confronto e' parziale")
    out["reproduced"] = bool(abs(out["mean"] - CITED_CLV) <= CITED_TOLERANCE)
    return out


def by_field(observations: Sequence[Observation], attr: str) -> Dict[str, dict]:
    """Statistica per valore di un campo dichiarato, con CI per gruppo."""
    groups: Dict[str, List[Observation]] = {}
    for obs in observations:
        groups.setdefault(str(getattr(obs, attr, "n/d")), []).append(obs)
    out = {}
    for key, rows in sorted(groups.items()):
        vals = [o.clv_gross for o in rows if o.clv_gross is not None]
        block = summarise(vals, [o.match_id for o in rows], key=key)
        block["roi"] = roi_block(rows)
        out[key] = block
    return out


def selection_bias(observations: Sequence[Observation],
                   all_bets: Sequence[dict]) -> Dict[str, Any]:
    """Il campione MISURATO e' rappresentativo delle puntate eseguite?

    E' la trappola piu' costosa: se le puntate con chiusura disponibile sono
    sistematicamente migliori (o peggiori) di quelle senza, il CLV del
    campione non descrive la strategia.
    """
    measured_ids = {str(o.bet_id) for o in observations}
    measured = [b for b in all_bets if str(b.get("id")) in measured_ids]
    missing = [b for b in all_bets if str(b.get("id")) not in measured_ids]

    def _roi(rows: Sequence[dict]) -> Dict[str, Any]:
        pl = [_num(r.get("profit")) for r in rows if _num(r.get("profit")) is not None]
        st = [_num(r.get("stake")) for r in rows if _num(r.get("stake")) is not None]
        total = sum(st) if st else 0.0
        return {"n": len(pl), "pnl": round(sum(pl), 4),
                "staked": round(total, 4),
                "roi": (sum(pl) / total) if total else None}

    return {"measured": _roi(measured), "missing": _roi(missing),
            "coverage": (len(measured) / len(all_bets)) if all_bets else None}


def cumulative_series(observations: Sequence[Observation]) -> Dict[str, Any]:
    """Media CUMULATA per data d'acquisto: il campione migliora o degrada?

    Perche' e' una misura e non una selezione: la regola e' fissata PRIMA (si
    prende tutto cio' che esiste fino a ogni giorno, in ordine di tempo) e non
    sceglie nessuna fascia. Serve a rispondere alla domanda che un numero
    singolo non puo' risolvere: **il CLV di oggi e' quello di dieci giorni fa?**

    Un edge stabile produce una cumulata piatta; un numero che vive di un
    periodo fortunato produce una cumulata che decade man mano che si aggiunge
    campione. Se degrada, il valore puntuale non e' la stima da usare.
    """
    rows = [o for o in observations
            if o.clv_gross is not None and parse_ts(o.buy_ts) is not None]
    if not rows:
        return {"available": False, "points": [], "note": "nessuna data d'acquisto"}
    rows.sort(key=lambda o: parse_ts(o.buy_ts))
    days = sorted({parse_ts(o.buy_ts).date().isoformat() for o in rows})
    points = []
    running: List[float] = []
    for day in days:
        running.extend(o.clv_gross for o in rows
                       if parse_ts(o.buy_ts).date().isoformat() == day)
        vals = [v for v in running if v is not None]
        if not vals:
            continue
        points.append({"day": day, "n": len(vals),
                       "mean": sum(vals) / len(vals),
                       "n_today": sum(1 for o in rows
                                      if parse_ts(o.buy_ts).date().isoformat() == day)})
    # Stabilita' misurata su DUE meta' temporali (non sul primo punto: con n=2
    # una singola osservazione decide l'intero confronto).
    vals = [o.clv_gross for o in rows if o.clv_gross is not None]
    half = len(vals) // 2
    stability: Dict[str, Any] = {}
    if half >= 10:
        a, b = vals[:half], vals[half:]
        delta = (sum(b) / len(b)) - (sum(a) / len(a))
        stability = {"first_half": {"n": len(a), "mean": sum(a) / len(a)},
                     "second_half": {"n": len(b), "mean": sum(b) / len(b)},
                     "delta": delta}
    # Il valore al momento in cui il campione ha raggiunto la soglia di
    # affidabilita': e' li' che il numero e' stato letto la prima volta.
    crossing = next((p for p in points if p["n"] >= MIN_SAMPLES), None)
    note = ""
    last = points[-1] if points else None
    if stability and last:
        delta = stability["delta"]
        if delta < -0.005:
            note = (f"la seconda meta' del campione vale {_delta(delta)} della "
                    "prima: la cumulata scende man mano che si aggiunge campione "
                    "(il numero migliore e' quello iniziale)")
        elif delta > 0.005:
            note = (f"la seconda meta' del campione vale {_delta(delta)} della "
                    "prima: la cumulata sale")
        else:
            note = ("le due meta' del campione danno lo stesso numero: la media "
                    "non dipende da un periodo fortunato")
    elif last and crossing and abs(last["mean"] - crossing["mean"]) > 0.005:
        note = (f"campione ancora piccolo per due meta': la cumulata e' passata "
                f"da {_pct(crossing['mean'])} (n={crossing['n']}) a "
                f"{_pct(last['mean'])} (n={last['n']})")
    return {"available": True, "points": points, "stability": stability,
            "crossing": crossing, "note": note}


def _delta(value: float) -> str:
    return f"{value * 100:+.2f}pp"


def vig_bounds(gross: Optional[float]) -> Dict[str, Optional[float]]:
    """Quanto il CLV GREZZO sovrastima quello vig-free, e di quanto.

    Il confronto non e' simmetrico: la chiusura di un book contiene il margine,
    quindi il prezzo fair e' piu' alto della chiusura e il CLV vig-free e'
    **piu' basso** del grezzo. Il margine per esito dipende dalla quota; qui si
    dichiara la forchetta tipica di un mercato a 3 esiti con overround 3-5%
    (circa 1,0-1,4pp a quote 1.8-2.0) invece di inventare un numero preciso.
    """
    if gross is None:
        return {"gross": None, "vig_free_low": None, "vig_free_high": None}
    return {"gross": gross,
            "vig_free_low": gross - 0.014,
            "vig_free_high": gross - 0.010}


def echo_impact(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Due trappole diverse, contate SEPARATAMENTE (non sono la stessa cosa).

    * `seme_copy` — `closing_quota` e' una COPIA di `signal_quota`: e' il seme
      che `tracker.save_clv` scrive alla prima chiamata a una quota sola. Non
      e' una chiusura e non va usata come tale (in produzione 411/475 righe:
      e' il motivo per cui `CLOSING_COLUMNS` accetta solo `closing_odds`).
    * `echo` — la chiusura VERA (`closing_odds`) coincide con la quota
      d'acquisto: CLV 0 per costruzione, esclusa dal campione (`EXCL_ECHO`).
      Contarla sposterebbe la media verso zero senza misurare nulla.

    """
    out = {"available": False, "rows": 0, "echo": 0, "seme_copy": 0,
           "share": None}
    try:
        rows = _safe_rows(conn, "SELECT signal_quota, closing_quota, closing_odds, "
                                "pinnacle_quota FROM clv_history")
    except Exception:                       # pragma: no cover - difensivo
        return out
    out["available"] = True
    out["rows"] = len(rows)
    echo = 0
    seme = 0
    for row in rows:
        sig = _price(row["signal_quota"])
        clo = _price(row["closing_quota"])
        if sig is not None and clo is not None and abs(sig - clo) < 1e-9:
            seme += 1
        close = _price(row["closing_odds"])
        if sig is not None and close is not None and abs(sig - close) < 1e-9:
            echo += 1
    out["echo"] = echo
    out["seme_copy"] = seme
    out["share"] = (echo / len(rows)) if rows else None
    return out


def provenance(conn: sqlite3.Connection) -> Dict[str, int]:
    """Ruolo delle quote a ledger: CHIUSURA (per classe) vs RIFERIMENTO.

    La distinzione conta: `clv_history` ha 448 righe ma le chiusure T-0 sono
    solo quelle con `closing_odds`. Le altre (`pinnacle_quota`, seminate
    dall'analisi) sono RIFERIMENTI: contarle insieme farebbe sembrare la
    copertura della chiusura quattro volte piu' alta di quella che e'.
    """
    counts: Dict[str, int] = {}
    try:
        rows = _safe_rows(conn, "SELECT * FROM clv_history")
    except Exception:                       # pragma: no cover - difensivo
        return counts
    for row in rows:
        if _closing_value(row) is not None:
            _value, lab = closing_of(row)
            ts = parse_ts(row["updated_at"] if "updated_at" in row.keys() else None)
            key = f"closing:{classify_source(lab, ts)}"
            counts[key] = counts.get(key, 0) + 1
            continue
        ref, ref_label = reference_of(row)
        if ref is not None:
            key = f"reference:{ref_label}"
            counts[key] = counts.get(key, 0) + 1
    return counts


def odds_gradient(observations: Sequence[Observation]) -> Dict[str, Any]:
    """Il CLV cresce con la quota? E' l'alternativa da escludere prima di dire
    "edge".

    Se il CLV sale monotonicamente con la quota **mentre** il ROI realizzato
    scende, la spiegazione piu' parsimoniosa non e' "catturiamo valore": e'
    che il nostro prezzo (exchange) e la chiusura sharp quotano i longshot in
    modo diverso. E' la stessa forma del favourite-longshot bias che il
    progetto usa gia' nel de-vig: qui si misura, non si assume.
    """
    pairs = [(o.odds_buy, o.clv_gross) for o in observations
             if o.clv_gross is not None]
    out: Dict[str, Any] = {"n": len(pairs), "pearson": None,
                           "by_band": {}, "note": ""}
    if len(pairs) < 5:
        out["note"] = "campione troppo piccolo per una correlazione"
        return out
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    try:
        import statistics as _st
        out["pearson"] = _st.correlation(xs, ys)
    except Exception:                       # pragma: no cover - difensivo
        out["pearson"] = None
    order = ["<1.30", "1.30-1.80", "1.80-2.20", ">=2.20"]
    bands = by_field(observations, "odds_band")
    out["by_band"] = {b: bands[b] for b in order if b in bands}
    means = [(b, (bands[b] or {}).get("mean")) for b in order if b in bands]
    means = [(b, m) for b, m in means if m is not None]
    if len(means) >= 2:
        monotone = all(means[i][1] <= means[i + 1][1] for i in range(len(means) - 1))
        out["monotone"] = monotone
        out["note"] = (
            "il CLV cresce con la quota in modo monotono: la differenza di "
            "prezzo exchange/sharp sui longshot spiega il numero tanto quanto "
            "un vantaggio, e il ROI per fascia dice quale delle due regge"
            if monotone else
            "nessun gradiente monotono con la quota: l'ipotesi 'differenza di "
            "prezzo sui longshot' non spiega da sola il CLV")
    return out


# ---------------------------------------------------------------------------
# 6. Verdetto, output (MD/JSON/CSV)
# ---------------------------------------------------------------------------

def verdict(dataset: Dict[str, Any], cohorts: Sequence[dict],
            checks: Dict[str, Any], bias: Dict[str, Any]) -> Dict[str, Any]:
    """Il verdetto obbligatorio: verificato | non verificato | insufficiente.

    La domanda non e' "il numero torna?" ma "il CLV positivo identifica un
    vantaggio REPLICABILE?". Sono due domande diverse e il verdetto le tiene
    separate: il calcolo puo' essere verificato e la replicabilita' no.
    """
    primary = next((c for c in cohorts if c.get("primary")), None)
    calc_ok = bool(checks.get("formula", {}).get("ok") and
                   checks.get("crosscheck", {}).get("match"))
    reasons: List[str] = []
    if not calc_ok:
        reasons.append("il calcolo non e' riproducibile dalle colonne grezze")
    if primary is None:
        reasons.append("nessuna coorte primaria preregistrata")
        primary_status = STATUS_UNAVAILABLE
    else:
        primary_status = primary.get("status")
    if primary_status == STATUS_VERIFIED:
        reasons.append("la coorte primaria ha CI95 interamente sopra zero")
    elif primary_status == STATUS_NOT_VERIFIED:
        reasons.append("la coorte primaria ha CI95 interamente sotto zero")
    else:
        reasons.append("la coorte primaria non conclude (campione o intervallo)")

    # La replicabilita' richiede PIU' di una coorte significante: serve che la
    # direzione regga fuori dal campione che l'ha generata.
    checked_cohorts = [c for c in cohorts if c.get("status") == STATUS_VERIFIED]
    contradictory = [c.get("id") for c in cohorts
                     if c.get("status") == STATUS_NOT_VERIFIED]
    if contradictory:
        reasons.append(f"coorti con segno opposto e intervallo sopra zero: {contradictory}")
    coverage = bias.get("coverage")
    if coverage is not None and coverage < 0.9:
        reasons.append(f"copertura della chiusura {coverage:.0%}: il campione "
                       "non descrive tutte le puntate eseguite")
    if not checks.get("prospective", {}).get("ready"):
        reasons.append("nessun campione PROSPETTICO: la finestra di 7 giorni "
                       "non e' ancora stata percorsa")
    if not checks.get("second_benchmark", {}).get("available"):
        reasons.append("nessuna conferma da un secondo benchmark sharp")
    gradient = checks.get("gradient") or {}
    if gradient.get("monotone"):
        reasons.append(
            "il CLV cresce in modo MONOTONO con la quota: la differenza di "
            "prezzo fra exchange e sharp sui longshot spiega il numero almeno "
            "quanto un vantaggio (il ROI per fascia dice che e' cosi')")
    venue = next((c for c in cohorts
                  if c.get("id") == "C11"), None)
    if venue and (venue.get("mean") is not None) and venue.get("mean") <= 0:
        reasons.append(
            "contro la chiusura dello STESSO exchange in cui compriamo il CLV "
            f"e' {_pct(venue.get('mean'))}: il vantaggio, se c'e', e' nel "
            "prezzo relativo al benchmark, non nell'esecuzione")

    replicable = bool(calc_ok and primary_status == STATUS_VERIFIED
                      and not contradictory and coverage is not None
                      and coverage >= 0.9
                      and checks.get("prospective", {}).get("ready")
                      and checks.get("second_benchmark", {}).get("available"))

    if not calc_ok:
        overall = "non_verificato"
    elif replicable:
        overall = "verificato"
    elif len(checked_cohorts) >= 2 and primary_status == STATUS_VERIFIED:
        overall = "non_verificato"
    else:
        overall = "dati_insufficienti"
    return {
        "calculation_verified": calc_ok,
        "primary_status": primary_status,
        "cohorts_verified": [c.get("id") for c in checked_cohorts],
        "cohorts_contradictory": contradictory,
        "replicable": replicable,
        "overall": overall,
        "reasons": reasons,
        "promotion_allowed": False,
        "promotion_rule": ("nessuna promozione: il CLV da solo non basta "
                           "(direttiva 20/10/2026: non promuovere una strategia "
                           "sulla sola base del CLV)"),
    }


def prospective_status(path: Optional[Path | str] = None) -> Dict[str, Any]:
    """Stato della raccolta PROSPETTICA (shadow, nessun ordine).

    Finche' i giorni coperti sono < 7 il verdetto sulla replicabilita' non e'
    esprimibile: e' il motivo per cui `ready` resta False.
    """
    try:
        from . import clv_shadow
        return clv_shadow.status(path)
    except Exception as exc:                # pragma: no cover - difensivo
        return {"ready": False, "days": 0, "rows": 0,
                "error": f"{type(exc).__name__}: {exc}"}


def run(*, db: Optional[str | Path] = None,
        out_dir: Optional[Path | str] = None,
        write: bool = True) -> Dict[str, Any]:
    """Misura completa: osservazioni, coorti, controlli, verdetto, file."""
    from . import loader
    out_path = Path(out_dir or OUT_DIR)
    db_path = loader.require_snapshot(db)
    conn = loader.connect(db_path)
    try:
        ds = build_observations(conn=conn, include_echo=False)
        all_bets = [dict(r) for r in _safe_rows(
            conn, "SELECT * FROM bets WHERE lower(coalesce(mode,''))='live'")]
        traps = {
            "echo": echo_impact(conn),
            "provenance": provenance(conn),
        }
        variants = (definition_variants(ds["observations"], conn)
                    if ds["observations"] else {})
    finally:
        conn.close()

    prereg = load_prereg()
    observations = ds["observations"]
    cohorts = evaluate_cohorts(observations, prereg["cohorts"])
    bias = selection_bias(observations, all_bets)
    gradient = odds_gradient(observations)
    checks = {
        "variants": variants,
        "formula": crosscheck_formula(observations),
        "crosscheck": crosscheck(observations, db_path),
        "prospective": prospective_status(),
        "gradient": gradient,
        "shopping": shopping_block(observations),
        "second_benchmark": {
            "available": any(o.clv_vig_free is not None for o in observations),
            "n": sum(1 for o in observations if o.clv_vig_free is not None),
        },
    }
    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "snapshot": str(db_path),
        "snapshot_sha256": _file_sha256(db_path),
        "definition": {
            "formula": "clv_gross = quota_acquisto / quota_chiusura - 1",
            "source": "market_calib.clv_raw (verificata riga per riga)",
            "positive_means": "abbiamo comprato a un prezzo migliore della chiusura",
            "vig_free": "solo con il mercato COMPLETO (market_calib.devig, power)",
        },
        "dataset": {
            "measured": len(observations),
            "totals": ds["totals"],
            "pending": ds["pending"],
            "excluded": ds["excluded"],
            "degraded": ds["degraded"],
            "schema": ds["schema"],
            "error": ds["error"],
        },
        "cohorts": cohorts,
        "overall": summarise([o.clv_gross for o in observations if o.clv_gross is not None],
                             [o.match_id for o in observations], key="all"),
        "by_source": by_field(observations, "closing_source_class"),
        "by_sport": by_field(observations, "sport"),
        "by_odds_band": by_field(observations, "odds_band"),
        "by_lead_band": by_field(observations, "lead_band"),
        "realized": roi_block(observations),
        "closing_quality": closing_quality(observations),
        "reference_quality": reference_quality(observations),
        "cited": cited_reproduction(observations),
        "gradient": gradient,
        "cumulative": cumulative_series(observations),
        "selection_bias": bias,
        "traps": traps,
        "checks": checks,
        "prereg": {k: v for k, v in prereg.items() if k != "cohorts"},
        "observations": [o.as_row() for o in observations],
    }
    buys = sorted(o.buy_ts for o in observations if o.buy_ts)
    span: Dict[str, Any] = {}
    if buys:
        first = parse_ts(buys[0])
        last = parse_ts(buys[-1])
        span = {"first": buys[0][:10], "last": buys[-1][:10],
                "days": ((last - first).days + 1) if first and last else None}
    result["span"] = span
    result["verdict"] = verdict(result, cohorts, checks, bias)
    if write:
        _write_outputs(result, out_path)
    return result


def shopping_block(observations: Sequence[Observation]) -> Dict[str, Any]:
    """Slippage del line shopping: prezzo PAGATO vs quota del segnale.

    E' l'unico costo misurabile con questi dati (non ci sono fee a ledger).
    Un valore negativo significa aver pagato peggio di quanto il segnale
    diceva, e va letto accanto al CLV: un CLV positivo pagato caro e' un edge
    piu' piccolo di quello che sembra.
    """
    vals = [o.shopping for o in observations if o.shopping is not None]
    if not vals:
        return {"n": 0, "mean": None, "median": None, "worse_share": None}
    return {"n": len(vals), "mean": sum(vals) / len(vals),
            "median": sorted(vals)[len(vals) // 2],
            "worse_share": sum(1 for v in vals if v < 0) / len(vals),
            "beyond_1pct": sum(1 for v in vals if abs(v) > 0.01) / len(vals)}


def _write_outputs(result: Dict[str, Any], out_dir: Path) -> None:
    """Scrive CLV_VALIDATION.md, CLV_VALIDATION.json e CLV_OBSERVATIONS.csv.

    Le osservazioni per riga vivono SOLO nel CSV (il JSON resta leggero: e' il
    riepilogo, non il registro). Il JSON dichiara dove stanno (`artifacts`):
    senza, chi apre il riepilogo e non trova le righe pensa che siano sparite,
    oppure le cerca in un file che non le contiene.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = dict(result)
    obs = payload.pop("observations", [])
    payload["artifacts"] = {
        "report": "CLV_VALIDATION.md",
        "observations_csv": "CLV_OBSERVATIONS.csv",
        "observations_n": len(obs),
        "csv_fields": list(CSV_FIELDS),
    }
    (out_dir / "CLV_VALIDATION.json").write_text(
        json.dumps(payload, indent=1, default=str), encoding="utf-8")
    with (out_dir / "CLV_OBSERVATIONS.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in obs:
            writer.writerow(row)
    (out_dir / "CLV_VALIDATION.md").write_text(format_report(result), encoding="utf-8")


def _pct(value: Optional[float], sign: bool = True) -> str:
    if value is None:
        return "n/d"
    return f"{value * 100:+.2f}%" if sign else f"{value * 100:.2f}%"


def _file_sha256(path: Path) -> Optional[str]:
    """Impronta del file su cui si e' misurato (riproducibilita')."""
    try:
        import hashlib
        h = hashlib.sha256()
        with Path(path).open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:                       # pragma: no cover - difensivo
        return None


def _snapshot_age(path: Path) -> str:
    """Eta' dello snapshot nel report: un dato stantio non deve passare per attuale.

    E' la lezione delle date fisse nei test (15/09, 17/09) applicata al DATO: se
    il file e' vecchio, tutte le misure sono vecchie, e il lettore deve vederlo
    nel titolo invece di doverlo dedurre dai numeri.
    """
    try:
        age = (datetime.now(timezone.utc)
               - datetime.fromtimestamp(path.stat().st_mtime, timezone.utc))
    except OSError:                             # pragma: no cover - difensivo
        return ""
    hours = age.total_seconds() / 3600
    label = f" · estratto {hours:.1f}h fa"
    if hours > 48:
        label += " ⚠️ DATO STANTIO"
    return label


def _ci(block: Dict[str, Any]) -> str:
    ci = block.get("ci95") or [None, None]
    return f"[{_pct(ci[0])}, {_pct(ci[1])}]"


def format_report(result: Dict[str, Any]) -> str:
    """Il report leggibile — stessa sostanza del JSON, senza arrotondamenti."""
    v = result.get("verdict") or {}
    ds = result.get("dataset") or {}
    lines: List[str] = []
    add = lines.append
    add("# CLV VALIDATION — verifica indipendente del CLV osservato")
    add("")
    snap_path = Path(str(result.get("snapshot")))
    add(f"Generato: `{result.get('generated_at')}` · snapshot "
        f"`{snap_path.name}`{_snapshot_age(snap_path)}")
    if result.get("snapshot_sha256"):
        add(f"sha256 dello snapshot: `{str(result['snapshot_sha256'])[:32]}…` "
            "(la copia e' la stessa su cui e' stata fatta la misura: senza "
            "questo un numero non e' riproducibile).")
    add("")
    add("## Conclusione obbligatoria")
    add("")
    add(f"**{str(v.get('overall', 'n/d')).upper().replace('_', ' ')}**")
    add("")
    add(f"- calcolo riproducibile dalle colonne grezze: **{'sì' if v.get('calculation_verified') else 'no'}**")
    add(f"- coorte primaria: **{v.get('primary_status')}**")
    add(f"- replicabile: **{'sì' if v.get('replicable') else 'no'}**")
    add(f"- promozione consentita: **{'sì' if v.get('promotion_allowed') else 'no'}** — {v.get('promotion_rule')}")
    for reason in v.get("reasons") or []:
        add(f"- {reason}")
    add("")
    add("## 1. Definizione matematica")
    add("")
    d = result.get("definition") or {}
    add(f"`{d.get('formula')}` — positivo = {d.get('positive_means')}.")
    add("")
    add(f"Formula del progetto: `{d.get('source')}`. Il controllo riga per riga "
        f"riporta **{len(((result.get('checks') or {}).get('formula') or {}).get('mismatches') or [])} "
        "discrepanze** su "
        f"{((result.get('checks') or {}).get('formula') or {}).get('checked', 0)} osservazioni.")
    add("")
    add(f"⚠️ Vig-free: {d.get('vig_free')}.")
    add("")
    add("## 2. Dataset e trappole")
    add("")
    add(f"- puntate LIVE: **{ds.get('totals', {}).get('live_bets')}** · misurate: "
        f"**{ds.get('measured')}** · senza chiusura: **{ds.get('totals', {}).get('pending')}**")
    excl = ds.get("totals", {}).get("excluded") or {}
    if excl:
        add(f"- escluse e contate: " + ", ".join(f"`{k}` {n}" for k, n in sorted(excl.items())))
    prov = (result.get("traps") or {}).get("provenance") or {}
    if prov:
        add("- provenienza delle chiusure a ledger: " +
            ", ".join(f"`{k}` {n}" for k, n in sorted(prov.items())))
    add("")
    add("Convenzione delle fonti: `closing_odds?` (col punto interrogativo) = "
        "chiusura scritta PRIMA che la colonna `closing_source` esistesse "
        "(13/10): il numero e' misurabile, la fonte no. Nel CSV la classe sta "
        "in `closing_source_class`, cosi' un `?` non passa per una fonte.")
    add("")
    cq = result.get("closing_quality") or {}
    if cq.get("n"):
        add(f"Distanza della chiusura dal fischio (`closing_ts` vs kickoff): mediana "
            f"**{cq.get('median_min_before')} min PRIMA**, p90 "
            f"**{cq.get('p90_min_before')} min prima**; catturata DOPO il kickoff "
            f"nel {_pct(cq.get('share_after_kickoff'), False)} (lì il prezzo è "
            "in-play, non una chiusura) e più di 30' prima nel "
            f"{_pct(cq.get('share_before_30'), False)}" +
            (f" · {cq.get('missing')} righe senza kickoff (non misurabili)"
             if cq.get("missing") else "") + ".")
        add("")
    echo = (result.get("traps") or {}).get("echo") or {}
    rq = result.get("reference_quality") or {}
    if rq.get("n"):
        add(f"Prezzo di riferimento (quale colonna lo fornisce): " +
            ", ".join(f"`{k}` {n}" for k, n in sorted(rq["by_source"].items())) +
            f" — {rq['note']}")
        add("")
    if echo.get("available"):
        add(f"- ECHO (chiusura VERA = quota d'acquisto, CLV 0 per costruzione): "
            f"**{echo.get('echo')}/{echo.get('rows')}** righe "
            f"({_pct(echo.get('share'), False)}) — escluse dal campione")
        add(f"- SEME (`closing_quota` copiata da `signal_quota`, NON e' una "
            f"chiusura): **{echo.get('seme_copy')}/{echo.get('rows')}** righe — "
            "rifiutate come closing a monte")
    for note in ds.get("degraded") or []:
        add(f"- ⚠️ {note}")
    add("")
    add("## 3. Il confronto con la produzione")
    add("")
    cc = (result.get("checks") or {}).get("crosscheck") or {}
    ov = result.get("overall") or {}
    if cc.get("available"):
        add(f"| | valore | n |")
        add(f"|---|---|---|")
        add(f"| questo modulo (grezzo) | **{_pct(cc.get('our_mean'))}** | {cc.get('our_n')} |")
        add(f"| `clv_tracker` (produzione) | {_pct(cc.get('their_mean'))} | {cc.get('their_n')} |")
        add("")
        add(f"Osservazioni confrontate una per una: **{cc.get('compared')}** · "
            f"discrepanze oltre 1e-6: **{len(cc.get('mismatches') or [])}** · "
            f"differenza massima: {cc.get('max_abs_diff')}")
        add("")
    else:
        add(f"Confronto non disponibile: {cc.get('error')}")
        add("")
    variants = (result.get("checks") or {}).get("variants") or {}
    if variants:
        add("### La stessa formula con le ALTRE definizioni di \"chiusura\"")
        add("")
        add("| colonna usata come chiusura | CLV | n | perché è sbagliata |")
        add("|---|---|---|---|")
        add(f"| `closing_odds` (usata) | **{_pct((variants.get('closing_odds') or {}).get('mean'))}** | "
            f"{(variants.get('closing_odds') or {}).get('n')} | è la chiusura vera, T-0 |")
        add(f"| riferimento sharp (`pinnacle_quota`) | "
            f"{_pct((variants.get('reference_as_closing') or {}).get('mean'))} | "
            f"{(variants.get('reference_as_closing') or {}).get('n')} | misura il movimento "
            "fino al SEGNALE, non fino al fischio |")
        add(f"| `closing_quota` (seme del segnale) | "
            f"{_pct((variants.get('seme_as_closing') or {}).get('mean'))} | "
            f"{(variants.get('seme_as_closing') or {}).get('n')} | è una COPIA della quota "
            "d'acquisto: CLV ≈ 0 per costruzione |")
        add("")
        add("Se le tre dessero lo stesso numero la scelta della colonna sarebbe "
            "irrilevante. Non la danno: il numero dipende dalla definizione, quindi "
            "la definizione va dichiarata (e lo è, nel protocollo congelato).")
        add("")
    add("## 4. CLV per coorti PREREGISTRATE")
    add("")
    add(f"Protocollo: `{Path(str((result.get('prereg') or {}).get('path'))).name}` · "
        f"sha256 `{str((result.get('prereg') or {}).get('sha256'))[:16]}…` · "
        f"congelato `{(result.get('prereg') or {}).get('frozen_at')}`")
    add("")
    add("| coorte | n | CLV grezzo | IC95 | positivi | ROI realizzato | stato |")
    add("|---|---|---|---|---|---|---|")
    for c in result.get("cohorts") or []:
        roi = c.get("roi") or {}
        add(f"| {'**' if c.get('primary') else ''}{c.get('id')}{'**' if c.get('primary') else ''} "
            f"{c.get('label')} | {c.get('n')} | {_pct(c.get('mean'))} | {_ci(c)} | "
            f"{_pct(c.get('share_positive'), False)} | {_pct(roi.get('roi'))} | {c.get('status')} |")
    add("")
    add("Le coorti sono definite in un file congelato (predicati su variabili "
        "ex-ante; il campo `clv` e' rifiutato per costruzione). La primaria e' "
        "l'insieme misurato, non una fascia scelta guardando i risultati.")
    add("")
    add("## 5. Separazione delle quattro grandezze")
    add("")
    real = result.get("realized") or {}
    add("| grandezza | valore | n |")
    add("|---|---|---|")
    add(f"| CLV grezzo | {_pct(ov.get('mean'))} {_ci(ov)} | {ov.get('n')} |")
    vf_blocks = [(c.get('id'), c.get('vig_free')) for c in (result.get("cohorts") or [])]
    vf_any = next(((i, b) for i, b in vf_blocks if b and b.get("mean") is not None), None)
    add(f"| CLV vig-free | {_pct(vf_any[1]['mean']) if vf_any else 'non calcolabile'} | "
        f"{(vf_any[1]['n']) if vf_any else 0} |")
    if vf_any:
        add(f"| ↳ CLV grezzo sugli STESSI campioni | "
            f"{_pct(vf_any[1].get('gross_on_same'))} | "
            f"{vf_any[1].get('gross_on_same_n')} |")
    add(f"| ROI realizzato | {_pct(real.get('roi'))} (IC95 {_ci(real)}) | {real.get('n')} |")
    shop = (result.get('checks') or {}).get('shopping') or {}
    add(f"| slippage / line shopping | {_pct(shop.get('mean'))} (mediana "
        f"{_pct(shop.get('median'))}) | {shop.get('n')} |")
    add("")
    if shop.get("n"):
        add(f"Line shopping: pagato **peggio** del segnale nel "
            f"{_pct(shop.get('worse_share'), False)} dei casi; differenze oltre "
            f"l'1% nel {_pct(shop.get('beyond_1pct'), False)}. Non ci sono fee a "
            "ledger: questo e' l'unico costo misurabile.")
        add("")
    bounds = vig_bounds(ov.get("mean"))
    if vf_any and vf_any[1].get("n") and ov.get("n"):
        add(f"⚠️ Il vig-free copre **{vf_any[1].get('n')}/{ov.get('n')}** campioni "
            "(dove il mercato e' completo): la sua media NON si confronta con "
            "quella del campione intero, solo con il grezzo appaiato qui sopra. "
            "La direzione (grezzo vs vig-free) dipende da dove stanno le puntate "
            "rispetto al favorito: sui favoriti il de-vig alza la probabilita' "
            "fair e il vig-free scende, sui longshot succede l'opposto.")
        add("")
    if bounds.get("gross") is not None:
        add(f"Il vig-free non e' misurabile sul campione intero (mancano i mercati "
            f"completi). Forchetta dichiarata, non misurata: con un overround del "
            f"3-5% il CLV vig-free sta in **[{_pct(bounds['vig_free_low'])}, "
            f"{_pct(bounds['vig_free_high'])}]** — il grezzo sovrastima, non sottostima.")
        add("")
    add(f"Il ROI realizzato copre {real.get('n')} puntate: con un CLV vero del "
        f"{_pct(ov.get('mean'))} e una sd del P/L unitario di ~1, la deviazione "
        "standard della media supera il 10% — il ROI non e' una prova contro il "
        "CLV a questa dimensione, e il CLV non e' una prova per il ROI.")
    add("")
    add("## 6. Ripartizioni")
    add("")
    for key, title in (("by_source", "Per provenienza della chiusura"),
                       ("by_sport", "Per sport"),
                       ("by_odds_band", "Per fascia quota (confini fissi)"),
                       ("by_lead_band", "Per anticipo dell'ordine")):
        block = result.get(key) or {}
        if not block:
            continue
        add(f"### {title}")
        add("")
        add("| gruppo | n | CLV | IC95 | ROI | stato |")
        add("|---|---|---|---|---|---|")
        for label, row in block.items():
            shown = "n/d (kickoff non a ledger)" if label == "n/d" else label
            add(f"| {shown} | {row.get('n')} | {_pct(row.get('mean'))} | {_ci(row)} | "
                f"{_pct((row.get('roi') or {}).get('roi'))} | {row.get('status')} |")
        add("")
        if key == "by_lead_band":
            nd = (block.get("n/d") or {}).get("n")
            tot = ov.get("n") or 0
            if nd:
                add(f"⚠️ `n/d` = **{nd}/{tot}** campioni senza kickoff a ledger: la "
                    "tabella `matches` viene potata (`clear_old_matches`), quindi "
                    "l'anticipo dell'ordine NON e' ricostruibile per la maggior "
                    "parte delle puntate. Le coorti C6/C7 sono leggibili solo sul "
                    "residuo e non bastano a dire che il meccanismo sia il tempo.")
                add("")
    grad = result.get("gradient") or {}
    if grad.get("by_band"):
        add("### Gradiente per quota (l'alternativa da escludere)")
        add("")
        add("| fascia | n | CLV | IC95 | ROI realizzato |")
        add("|---|---|---|---|---|")
        for band, row in grad["by_band"].items():
            add(f"| {band} | {row.get('n')} | {_pct(row.get('mean'))} | {_ci(row)} | "
                f"{_pct((row.get('roi') or {}).get('roi'))} |")
        add("")
        add(f"Correlazione quota/CLV: **{grad.get('pearson')}** (n={grad.get('n')}). "
            f"{grad.get('note')}")
        add("")
    cum = result.get("cumulative") or {}
    if cum.get("available") and cum.get("points"):
        add("### Media CUMULATA per data d'acquisto (il numero e' stabile?)")
        add("")
        add("| fino al | n | CLV cumulato |")
        add("|---|---|---|")
        pts = cum["points"]
        step = max(1, len(pts) // 8)
        for point in pts[::step] + ([pts[-1]] if (len(pts) - 1) % step else []):
            add(f"| {point['day']} | {point['n']} | {_pct(point['mean'])} |")
        add("")
        if cum.get("note"):
            add(f"{cum['note']}")
            add("")
    cited = result.get("cited") or {}
    if cited.get("mean") is not None:
        add(f"**Riproduzione del numero citato** (`+4,54%` su "
            f"{cited.get('cited_n')} osservazioni, AGENTS.md 16/10): sulle prime "
            f"**{cited.get('n')}** osservazioni per istante d'acquisto il CLV e' "
            f"**{_pct(cited.get('mean'))}** (delta "
            f"{cited.get('delta_pp'):+.2f}pp contro il citato, tolleranza "
            f"{cited.get('tolerance_pp'):.1f}pp) → "
            + ("**riprodotto**" if cited.get("reproduced") else "**NON riprodotto**") +
            f". Sull'intera serie di oggi ({cited.get('current_n')} osservazioni) "
            f"il valore e' **{_pct(cited.get('current'))}**: il +4,54% e' una "
            "fotografia di un campione piu' piccolo, non un errore di calcolo — "
            "ed e' il motivo per cui la verifica si fa sull'intera serie.")
        if cited.get("note"):
            add("")
            add(f"⚠️ {cited['note']}.")
        add("")
    add("## 7. Selezione del campione (la trappola che falsa il CLV)")
    add("")
    sb = result.get("selection_bias") or {}
    m, miss = sb.get("measured") or {}, sb.get("missing") or {}
    add(f"- puntate con chiusura: **{m.get('n')}** · P/L {m.get('pnl')} su {m.get('staked')} "
        f"stake → ROI **{_pct(m.get('roi'))}**")
    add(f"- puntate senza chiusura: **{miss.get('n')}** · P/L {miss.get('pnl')} su "
        f"{miss.get('staked')} stake → ROI **{_pct(miss.get('roi'))}**")
    add(f"- copertura: **{_pct(sb.get('coverage'), False)}**")
    add("")
    add("Se le due popolazioni hanno ROI diversi, il campione con chiusura NON e' "
        "casuale e il CLV descrive quel sottoinsieme, non la strategia.")
    add("")
    add("## 8. Raccolta prospettica (shadow)")
    add("")
    ps = (result.get("checks") or {}).get("prospective") or {}
    add(f"- giorni coperti: **{ps.get('days', 0)}/7** · osservazioni PROSPETTICHE: "
        f"{ps.get('rows', 0)}" + (f" · pre-congelamento (storico, non contate): "
                                  f"{ps.get('pre_freeze')}"
                                  if ps.get("pre_freeze") else "")
        + f" · ipotesi congelata: `{ps.get('hypothesis_id')}`")
    hyp = (result.get("prereg") or {}).get("hypothesis") or {}
    if hyp.get("statement"):
        add(f"- **ipotesi (una sola)**: {hyp.get('statement')}")
        add(f"- falsificazione: {hyp.get('falsification')}")
    add(f"- pronta per l'analisi: **{'sì' if ps.get('ready') else 'no'}** — "
        f"{ps.get('note') or ps.get('error') or ''}")
    add("")
    add("Come si raccoglie (operativo, nessun ordine in nessun passo):")
    add("")
    add("```bash")
    add("# 1) snapshot fresco del volume (sola copia, sqlite backup) — una volta al giorno")
    add("#    railway ssh --service betting_bot -- python3 - <<'PY' > snapshot.b64")
    add("# 2) registra i segnali APERTI del momento (idempotente per signal_id)")
    add("QV_RESEARCH_DB=research_isolated/snapshot/quotaverace_prod.db \\")
    add("  venv/bin/python -m research_isolated.lab.clv_shadow record")
    add("# 3) stato della finestra / analisi quando i 7 giorni sono percorsi")
    add("  venv/bin/python -m research_isolated.lab.clv_shadow status")
    add("  venv/bin/python -m research_isolated.lab.clv_shadow analyse")
    add("```")
    add("")
    add("Il registro e' `research_isolated/out/clv_shadow/signals.jsonl` (append-only, "
        "fuori dal ledger): registra OGNI segnale 1X2 aperto, compresi quelli che "
        "la corsia non avrebbe giocato. Nessuna credenziale, nessuna scrittura, "
        "nessun ordine: la chiusura la scrive gia' la produzione (`closing_line`).")
    add("")
    add("## 9. Limiti (dichiarati, non aggirati)")
    add("")
    span = result.get("span") or {}
    add(f"1. **Una sola era.** Le {ov.get('n')} osservazioni vengono da "
        f"{span.get('first')} a {span.get('last')} ({span.get('days')} giorni) "
        "di un'unica configurazione (fascia 1.20-2.00, gate EV 1,5%, Kelly "
        "12%). Non e' un holdout: e' il campione che ha generato l'ipotesi.")
    add("2. **Chiusura sharp non certificata.** Il valore a T-0 viene dalla cache "
        "del provider sharp: `clv_history` non registra l'eta' di quella cache, "
        "quindi la chiusura e' \"l'ultima lettura disponibile prima del fischio\", "
        "non necessariamente il prezzo di chiusura ufficiale. Una cache stantia "
        "comprime il CLV verso zero (non lo gonfia), quindi la direzione resta.")
    add("3. **Provenienza parziale.** Le chiusure scritte prima del 13/10 non "
        "dichiarano la fonte: sono tenute in una classe separata (`sharp_legacy`) "
        "e misurate anche da sole.")
    add("4. **Nessun benchmark indipendente.** Il secondo sharp (Betfair) copre "
        f"{(result.get('checks') or {}).get('second_benchmark', {}).get('n', 0)} "
        "osservazioni: insufficiente per una conferma.")
    add("5. **Liquidita' non disponibile** per 1X2 e tennis (`market_quotes` "
        "persiste solo OU/AH): il campo e' `n/d` con il motivo, non stimato.")
    add("6. **Risultato finale** e' quello del ledger (`bets.esito_finale`), non "
        "riverificato contro la fonte dell'exchange in questo modulo.")
    add("")
    add("## 10. Cosa serve per cambiare il verdetto")
    add("")
    add("- percorrere la finestra prospettica di **7 giorni** (>= 30 segnali con "
        "chiusura) sull'ipotesi congelata, senza ordini;")
    add("- una chiusura con **fonte dichiarata** per ogni osservazione (il job la "
        "scrive dal 13/10: le nuove lo fanno);")
    add("- il **secondo benchmark** popolato (Betfair) per la conferma indipendente;")
    add("- **un'era diversa** per il test di replicabilita': stesso segno su un "
        "periodo che non ha contribuito a generare l'ipotesi.")
    add("")
    return "\n".join(lines) + "\n"


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Verifica indipendente del CLV osservato")
    ap.add_argument("--db", default=None, help="snapshot SQLite (sola lettura)")
    ap.add_argument("--out", default=None, help="cartella di output")
    ap.add_argument("--json", action="store_true", help="stampa il JSON invece del report")
    ap.add_argument("--no-write", action="store_true", help="non scrive i file")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    res = run(db=args.db, out_dir=args.out, write=not args.no_write)
    if args.json:
        payload = dict(res)
        payload.pop("observations", None)
        print(json.dumps(payload, indent=1, default=str))
    else:
        print(format_report(res))
    return 0 if not (res.get("dataset") or {}).get("error") else 1


if __name__ == "__main__":                  # pragma: no cover
    raise SystemExit(main())
