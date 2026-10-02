"""clv_brier_analytics.py — CLV e Brier Score dal ledger (OFFLINE, sola lettura).

Direttiva del proprietario (02/10/2026): misurare due cose che dicono se il bot
sta battendo il mercato e se il prezzo sharp che usa e' calibrato.

  1) **CLV %** — Closing Line Value: la quota PRESA dal bot divisa per la quota
     di CHIUSURA dello sharp (Pinnacle). CLV positivo in modo sistematico e' il
     segnale piu' affidabile che la selezione batte il mercato, molto prima che
     il ROI su poche centinaia di bet diventi significativo.
  2) **Brier Score** — `mean((p - y)^2)` della probabilita' dichiarata contro
     l'esito reale. Serve a due domande distinte, tenute separate:
       - `brier_model`: la probabilita' con cui il bot ha SCOMMESSO
         (`predictions.prob`) e' calibrata?
       - `brier_sharp`: la probabilita' de-vigata della CLOSING SHARP e'
         calibrata? E' la misura dell'accuratezza del de-vigging (Shin vs
         power vs multiplicative) — la direttiva chiede Shin, e il modulo lo
         confronta con gli altri due sugli STESSI dati.

Il collegamento e' in **sola lettura** (`file:...?mode=ro`, il test tenta una
scrittura e pretende che SQLite la rifiuti): il file di produzione e' il DB di
`tracker` (`data/quotaverace.db` sul volume `/app/data`; la direttiva lo chiama
"ledger.db"). NESSUN import di rete, nessuna chiamata API, zero crediti.

Fonti (tutte nel DB):
  `clv_history`      signal_quota / closing_quota / pinnacle_quota
  `predictions`      quota presa, `prob` (p del modello), `esito_finale`
                     ('won' | 'lost' | 'push'), mercato, lega, created_at
  `price_snapshots`  storico prezzi per (match_id, esito) con `bookmaker`:
                     l'ultimo prezzo sharp = closing line
  `match_results`    esito reale ('1' | 'X' | '2')

Le formule NON sono ricopiate: CLV e de-vig arrivano da `market_calib`
(`clv_raw`, `clv_vig_free`, `devig_with_z`), che e' l'unico posto dove vivono.

CLI:
  venv/bin/python scripts/clv_brier_analytics.py
  venv/bin/python scripts/clv_brier_analytics.py --db data/quotaverace.db --json
  venv/bin/python scripts/clv_brier_analytics.py --since 2026-09-19 --book pinnacle
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import quote

# --- Bootstrap del path: lo script vive in scripts/, i moduli in root -------
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from market_calib import clv_raw, clv_vig_free, devig_with_z  # noqa: E402

DEFAULT_BOOK = "pinnacle"
#: Metodi di de-vig confrontati sullo STESSO set di closing sharp.
DEVIG_METHODS = ("shin", "power", "multiplicative")
#: Esiti di un mercato 1X2 (l'unica forma de-vigabile a 3 vie dal ledger).
OUTCOMES_1X2 = ("1", "X", "2")
RESULT_VALUES = ("1", "X", "2")


# ---------------------------------------------------------------------------
# Connessione in sola lettura
# ---------------------------------------------------------------------------

def default_db_path() -> Path:
    """DB di produzione (`tracker.DB_PATH`), senza importarlo a livello modulo."""
    try:
        import tracker
        return Path(tracker.DB_PATH)
    except Exception:
        return _ROOT / "data" / "quotaverace.db"


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    """Connessione SQLite in SOLA LETTURA. Solleva se il file non esiste."""
    path = Path(db_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"DB non trovato: {path}")
    conn = sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    try:
        return bool(conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,)).fetchone())
    except sqlite3.Error:
        return False


def _rows(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> List[Dict]:
    """Query fail-safe: una tabella assente vale come 'nessuna riga'."""
    try:
        return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]
    except sqlite3.Error:
        return []


def _table_columns(conn: sqlite3.Connection, name: str) -> set:
    """Nomi delle colonne presenti; insieme vuoto se la tabella non c'e'.

    Serve perche' lo schema del ledger EVOLVE (la colonna `predictions.league`
    nasce il 22/09, `settled_at` prima ancora): una diagnostica deve poter
    leggere anche il DB di un deploy precedente, invece di rispondere
    "nessuna riga" perche' il SELECT nomina una colonna che non esiste.
    """
    try:
        return {str(r[1]) for r in conn.execute(f"PRAGMA table_info({name})")}
    except sqlite3.Error:
        return set()


def _select_cols(conn: sqlite3.Connection, table: str,
                 wanted: Sequence[str]) -> List[str]:
    """Intersezione fra le colonne volute e quelle esistenti (ordine voluto)."""
    existing = _table_columns(conn, table)
    return [c for c in wanted if c in existing]

# ---------------------------------------------------------------------------
# Classificazione sport / mercato
# ---------------------------------------------------------------------------

def classify_sport(mercato: Optional[str], match_id: Optional[str] = "",
                   league: Optional[str] = "") -> str:
    """Sport di una riga di ledger: `calcio` | `tennis` | `esports` | `altro`.

    Deterministica e dichiarata (nessun fuzzy): il mercato del ledger e' la
    fonte primaria, il prefisso del match_id la conferma per le corsie SX.
    """
    m = str(mercato or "").strip().upper()
    mid = str(match_id or "").strip().lower()
    lg = str(league or "").strip().lower()
    if m == "TENNIS" or mid.startswith("sx-tennis-") or "atp" in lg or "wta" in lg:
        return "tennis"
    if m == "ML" or mid.startswith("sx-esports-"):
        return "esports"
    if m in ("1X2", "OU", "AH", "12", "OU_OT", "AH_OT", "ML_OT"):
        return "calcio"
    if not m:
        return "altro"
    return "altro"


def classify_market(mercato: Optional[str]) -> str:
    """Mercato normalizzato: la chiave di raggruppamento del report."""
    m = str(mercato or "").strip().upper()
    return m or "?"


# ---------------------------------------------------------------------------
# Statistiche di base (nessuna dipendenza nuova: numpy non serve qui)
# ---------------------------------------------------------------------------

def summarize(values: Iterable[float]) -> Optional[Dict[str, Any]]:
    """Media/mediana/dev.std/positivi di una serie. None se vuota."""
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return None
    n = len(vals)
    mean = sum(vals) / n
    ordered = sorted(vals)
    mid = n // 2
    median = (ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0)
    if n > 1:
        var = sum((v - mean) ** 2 for v in vals) / (n - 1)
        std = var ** 0.5
    else:
        std = 0.0
    # I valori restano a piena precisione: l'arrotondamento e' una scelta di
    # STAMPA (il report usa `:.3f`), non del dato. Un `--json` arrotondato
    # non e' confrontabile con la formula di `market_calib` che lo genera.
    return {
        "n": n,
        "mean_pct": mean * 100.0,
        "median_pct": median * 100.0,
        "std_pct": std * 100.0,
        "positive_pct": sum(1 for v in vals if v > 0) / n * 100.0,
        "min_pct": min(vals) * 100.0,
        "max_pct": max(vals) * 100.0,
    }


def brier_score(pairs: Iterable[Tuple[float, float]]) -> Optional[Dict[str, Any]]:
    """Brier Score `mean((p - y)^2)` + skill score sul forecast costante.

    `skill = 1 - brier / brier_ref` dove `brier_ref = base*(1-base)` e' il
    Brier di un forecast costante pari alla frequenza base: > 0 significa
    "meglio di non sapere nulla", <= 0 significa "peggio del banale".
    """
    vals = [(float(p), float(y)) for p, y in pairs if p is not None and y is not None]
    if not vals:
        return None
    n = len(vals)
    brier = sum((p - y) ** 2 for p, y in vals) / n
    base = sum(y for _, y in vals) / n
    ref = base * (1.0 - base)
    # Piena precisione (come `summarize`): il `:.5f` e' del report, non del dato.
    return {
        "n": n,
        "brier": brier,
        "base_rate_pct": base * 100.0,
        "brier_ref": ref,
        "skill": 1.0 - brier / ref if ref > 0 else None,
    }


def _group(rows: List[Dict], key: str) -> Dict[str, List[Dict]]:
    out: Dict[str, List[Dict]] = {}
    for r in rows:
        out.setdefault(str(r.get(key) or "?"), []).append(r)
    return out


# ---------------------------------------------------------------------------
# Lettura del ledger
# ---------------------------------------------------------------------------

def parse_ts(value: Optional[str]) -> Optional[datetime]:
    """ISO -> datetime naive UTC (`Z`/offset gestiti). None se illeggibile.

    I confronti sulle date si fanno in PYTHON, mai come stringhe in SQL: il
    ledger salva `2026-09-12T07:11:39` (con la 'T') mentre `datetime('now')`
    produce lo spazio, e a parita' di giorno il confronto stringa slitta
    (bug del 17/09 sulla scadenza delle righe stale).
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _since_dt(since: Optional[str]) -> Optional[datetime]:
    if not since:
        return None
    dt = parse_ts(since)
    if dt is None:
        raise ValueError(f"--since non parsabile: {since!r} (atteso ISO, es. 2026-09-19)")
    return dt


def load_predictions(conn: sqlite3.Connection, since: Optional[datetime] = None,
                     ) -> List[Dict]:
    """Previsioni del ledger con la lega (colonna dal 22/09, opzionale).

    Il SELECT nomina SOLO le colonne presenti: su un DB di un deploy
    precedente (`status`/`profit`/`settled_at` non ancora migrati) la
    diagnostica legge comunque le righe invece di riportare zero.
    """
    cols = _select_cols(conn, "predictions",
                        ("id", "match_id", "mercato", "esito", "quota", "prob",
                         "status", "esito_finale", "profit", "created_at",
                         "settled_at", "league"))
    if "match_id" not in cols:
        return []
    raw = _rows(conn, f"SELECT {', '.join(cols)} FROM predictions")
    out = []
    for r in raw:
        ts = parse_ts(r.get("created_at")) or parse_ts(r.get("settled_at"))
        if since is not None and (ts is None or ts < since):
            continue
        r["_ts"] = ts
        r.setdefault("league", None)
        r["sport"] = classify_sport(r.get("mercato"), r.get("match_id"), r.get("league"))
        r["market"] = classify_market(r.get("mercato"))
        out.append(r)
    return out


def load_clv_history(conn: sqlite3.Connection, since: Optional[datetime] = None,
                     ) -> List[Dict]:
    """Campioni CLV registrati dalla produzione (tabella `clv_history`)."""
    cols = _select_cols(conn, "clv_history",
                        ("match_id", "esito", "signal_quota", "closing_quota",
                         "pinnacle_quota", "updated_at"))
    if not {"match_id", "esito"} <= set(cols):
        return []
    out = []
    for r in _rows(conn, f"SELECT {', '.join(cols)} FROM clv_history"):
        ts = parse_ts(r.get("updated_at"))
        if since is not None and (ts is None or ts < since):
            continue
        out.append({
            "match_id": r.get("match_id"), "esito": r.get("esito"),
            "taken": _pos_float(r.get("signal_quota")),
            "closing": _pos_float(r.get("pinnacle_quota")) or _pos_float(r.get("closing_quota")),
            "closing_source": "pinnacle" if _pos_float(r.get("pinnacle_quota")) else "bookmaker",
            "all_closing": None, "_ts": ts,
        })
    return out


def load_bets(conn: sqlite3.Connection, since: Optional[datetime] = None) -> List[Dict]:
    """Puntate reali/simulate col prezzo effettivamente preso."""
    cols = _select_cols(conn, "bets",
                        ("match_id", "mercato", "esito", "price", "stake",
                         "mode", "esito_finale", "created_at"))
    if "match_id" not in cols:
        return []
    out = []
    for r in _rows(conn, f"SELECT {', '.join(cols)} FROM bets"):
        ts = parse_ts(r.get("created_at"))
        if since is not None and (ts is None or ts < since):
            continue
        out.append({"match_id": r.get("match_id"), "mercato": r.get("mercato"),
                    "esito": r.get("esito"), "taken": _pos_float(r.get("price")),
                    "mode": r.get("mode"), "_ts": ts})
    return out


def load_snapshots(conn: sqlite3.Connection, book: str) -> List[Dict]:
    """Snapshot dei prezzi sharp, in ordine cronologico per (match, esito).

    Ordenati in SQL su `recorded_at` (tutte le righe della STESSA colonna, in
    formato ISO uniforme: il confronto e' fra stringhe omogenee) e poi
    consolidati in Python.
    """
    cols = _select_cols(conn, "price_snapshots",
                        ("match_id", "esito", "price", "bookmaker", "recorded_at"))
    if not {"match_id", "esito", "price", "bookmaker", "recorded_at"} <= set(cols):
        return []
    return _rows(conn, f"SELECT {', '.join(cols)} FROM price_snapshots "
                       "WHERE bookmaker=? ORDER BY recorded_at", (str(book),))


def sharp_closings(snapshots: List[Dict]) -> Tuple[Dict[Tuple[str, str], Dict],
                                                   Dict[str, Dict[str, float]]]:
    """Ultimo prezzo sharp per (match, esito) + mercati 1X2 completi.

    Ritorna `(closing, markets)` dove `markets[match_id] = {esito: price}` solo
    quando sono presenti TUTTI e tre gli esiti 1X2: senza il mercato completo
    il de-vig avrebbe un margine incompleto (fail-closed, mai un de-vig a 2 su 3).
    """
    closing: Dict[Tuple[str, str], Dict] = {}
    per_match: Dict[str, Dict[str, float]] = {}
    for r in snapshots:
        mid, esito = str(r.get("match_id") or ""), str(r.get("esito") or "")
        price = _pos_float(r.get("price"))
        if not mid or not esito or price is None:
            continue
        closing[(mid, esito)] = {"price": price, "ts": r.get("recorded_at")}
        per_match.setdefault(mid, {})[esito] = price
    markets = {mid: odds for mid, odds in per_match.items()
               if all(_pos_float(odds.get(e)) for e in OUTCOMES_1X2)}
    return closing, markets


def load_results(conn: sqlite3.Connection) -> Dict[str, str]:
    """Esito reale per match: `{'1'|'X'|'2': ...}` (solo valori riconosciuti)."""
    cols = _select_cols(conn, "match_results", ("match_id", "result"))
    if not {"match_id", "result"} <= set(cols):
        return {}
    out = {}
    for r in _rows(conn, f"SELECT {', '.join(cols)} FROM match_results"):
        res = str(r.get("result") or "").strip().upper()
        if res in RESULT_VALUES:
            out[str(r.get("match_id"))] = res
    return out


def _pos_float(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out > 1.0 else None


# ---------------------------------------------------------------------------
# CLV
# ---------------------------------------------------------------------------

def build_clv_samples(predictions: List[Dict], clv_rows: List[Dict],
                      closing: Dict[Tuple[str, str], Dict],
                      markets: Dict[str, Dict[str, float]],
                      league_by_match: Optional[Dict[str, str]] = None,
                      ) -> List[Dict]:
    """Unifica le fonti CLV in una lista di campioni confrontabili.

    Priorita': `clv_history` (e' la misura che la produzione ha registrato, con
    la closing sharp quando disponibile) e poi `predictions` con la closing
    ricavata dall'ultimo snapshot sharp. Ogni campione dichiara la PROVENIENZA
    della closing: un numero senza la sua fonte non e' verificabile.
    """
    league_by_match = league_by_match or {}
    samples: List[Dict] = []
    seen: set = set()

    for r in clv_rows:
        mid, esito = str(r.get("match_id")), str(r.get("esito"))
        taken, close = r.get("taken"), r.get("closing")
        if not mid or not esito or taken is None or close is None:
            continue
        market_odds = markets.get(mid)
        all_closing = [market_odds[e] for e in OUTCOMES_1X2] if market_odds else None
        samples.append({
            "match_id": mid, "esito": esito, "taken": taken, "closing": close,
            "all_closing": all_closing, "closing_source": r.get("closing_source"),
            "source": "clv_history",
            "sport": classify_sport(None, mid, league_by_match.get(mid)),
            "market": "1X2" if esito in OUTCOMES_1X2 else "?",
        })
        seen.add((mid, esito))

    for r in predictions:
        mid, esito = str(r.get("match_id")), str(r.get("esito"))
        taken = _pos_float(r.get("quota"))
        if not mid or not esito or taken is None or (mid, esito) in seen:
            continue
        snap = closing.get((mid, esito))
        if not snap:
            continue
        market_odds = markets.get(mid)
        all_closing = [market_odds[e] for e in OUTCOMES_1X2] if market_odds else None
        samples.append({
            "match_id": mid, "esito": esito, "taken": taken,
            "closing": snap["price"], "all_closing": all_closing,
            "closing_source": "snapshot_sharp", "source": "predictions",
            "sport": r.get("sport"), "market": r.get("market"),
        })
        seen.add((mid, esito))
    return samples


def clv_stats(samples: List[Dict], method: str = "shin") -> Dict[str, Any]:
    """CLV grezzo e vig-free sui campioni. Le formule arrivano da market_calib."""
    raw, vig_free, by_source = [], [], {}
    for s in samples:
        r = clv_raw(s["taken"], s["closing"])
        if r is not None:
            raw.append(r)
        v = clv_vig_free(s["taken"], s["closing"], s.get("all_closing"),
                         method=method)
        if v is not None:
            vig_free.append(v)
        by_source[s.get("closing_source") or "?"] = \
            by_source.get(s.get("closing_source") or "?", 0) + 1
    return {"samples": len(samples), "by_closing_source": by_source,
            "raw": summarize(raw), "vig_free": summarize(vig_free),
            "vig_free_n": len(vig_free)}


# ---------------------------------------------------------------------------
# Brier
# ---------------------------------------------------------------------------

def brier_model_rows(predictions: List[Dict]) -> List[Dict]:
    """(p del modello, esito realizzato) dalle previsioni CHIUSE.

    `esito_finale` e' `won`/`lost`/`push` (vocabolario di `settle_predictions`):
    i push sono ESCLUSI dal Brier — non hanno un esito binario, e contarli
    come sconfitte falserebbe la calibrazione.
    """
    out = []
    for r in predictions:
        outcome = str(r.get("esito_finale") or "").strip().lower()
        prob = r.get("prob")
        if outcome not in ("won", "lost") or prob is None:
            continue
        try:
            p = float(prob)
        except (TypeError, ValueError):
            continue
        if not 0.0 <= p <= 1.0:
            continue
        out.append({"p": p, "y": 1.0 if outcome == "won" else 0.0,
                    "sport": r.get("sport"), "market": r.get("market"),
                    "status": r.get("status")})
    return out


def brier_sharp_rows(markets: Dict[str, Dict[str, float]],
                     results: Dict[str, str], *,
                     methods: Sequence[str] = DEVIG_METHODS,
                     league_by_match: Optional[Dict[str, str]] = None,
                     ) -> Dict[str, List[Dict]]:
    """Brier delle probabilita' de-vigate della closing sharp, per metodo.

    Per ogni partita con mercato 1X2 sharp COMPLETO e risultato noto si
    de-vigano le tre quote e si confrontano con l'esito reale (y = 1 per
    l'esito vincente, 0 per gli altri): tre osservazioni per partita, cosi'
    il Brier misura la calibrazione sull'intero mercato e non solo sul lato
    giocato. Il `z` di Shin e' registrato in media (denaro informato stimato).
    """
    league_by_match = league_by_match or {}
    out: Dict[str, List[Dict]] = {m: [] for m in methods}
    z_values: List[float] = []
    for mid, odds_map in markets.items():
        result = results.get(mid)
        if result not in RESULT_VALUES:
            continue
        odds = [odds_map[e] for e in OUTCOMES_1X2]
        sport = classify_sport(None, mid, league_by_match.get(mid))
        for method in methods:
            try:
                fair, z = devig_with_z(odds, method=method)
            except Exception:
                continue
            if not fair or len(fair) != len(OUTCOMES_1X2):
                continue
            if method == "shin" and z is not None:
                z_values.append(float(z))
            for i, esito in enumerate(OUTCOMES_1X2):
                out.setdefault(method, []).append({
                    "p": float(fair[i]), "y": 1.0 if esito == result else 0.0,
                    "sport": sport, "market": "1X2"})
    out["_shin_z_mean"] = sum(z_values) / len(z_values) if z_values else None
    out["_shin_z_n"] = len(z_values)
    return out


def _brier_block(rows: List[Dict], key: Optional[str] = None,
                 ) -> Dict[str, Any]:
    """Brier complessivo + per gruppo (`sport` o `market`)."""
    overall = brier_score((r["p"], r["y"]) for r in rows)
    block: Dict[str, Any] = {"overall": overall}
    if key:
        block["by_" + key] = {
            g: brier_score((r["p"], r["y"]) for r in grp)
            for g, grp in sorted(_group(rows, key).items())}
    return block


# ---------------------------------------------------------------------------
# Analisi completa
# ---------------------------------------------------------------------------

def analyze(db_path: Path, *, since: Optional[str] = None, book: str = DEFAULT_BOOK,
            method: str = "shin") -> Dict[str, Any]:
    """Esegue tutte le misure. Fail-safe: un errore diventa `error`, mai un crash."""
    result: Dict[str, Any] = {
        "db": str(db_path), "read_only": True, "book": book,
        "devig_method": method, "since": since, "warnings": []}
    since_dt = _since_dt(since)
    try:
        conn = connect_readonly(db_path)
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result
    try:
        predictions = load_predictions(conn, since_dt)
        clv_rows = load_clv_history(conn, since_dt)
        bets = load_bets(conn, since_dt)
        snapshots = load_snapshots(conn, book)
        results = load_results(conn)
        league_by_match = {str(r.get("match_id")): r.get("league")
                           for r in predictions if r.get("league")}
    finally:
        conn.close()

    # Il prezzo PRESO puo' venire anche dalle puntate reali (piu' autorevole
    # della quota del segnale): si aggiorna la quota presa dei campioni.
    taken_by_match: Dict[Tuple[str, str], float] = {}
    for b in bets:
        if b.get("taken") is not None and b.get("match_id"):
            taken_by_match[(str(b["match_id"]), str(b.get("esito")))] = b["taken"]

    closing, markets = sharp_closings(snapshots)
    samples = build_clv_samples(predictions, clv_rows, closing, markets,
                                league_by_match)
    for s in samples:
        better = taken_by_match.get((str(s["match_id"]), str(s["esito"])))
        if better is not None:
            s["taken"] = better
            s["source"] = "bets"
    unmatched = set(taken_by_match) - {(str(s["match_id"]), str(s["esito"]))
                                       for s in samples}

    # --- CLV ---------------------------------------------------------------
    clv_block: Dict[str, Any] = {"overall": clv_stats(samples, method)}
    clv_block["by_sport"] = {g: clv_stats(grp, method)
                             for g, grp in sorted(_group(samples, "sport").items())}
    clv_block["by_market"] = {g: clv_stats(grp, method)
                              for g, grp in sorted(_group(samples, "market").items())}

    # --- Brier modello -----------------------------------------------------
    model_rows = brier_model_rows(predictions)
    brier_model = _brier_block(model_rows, "sport")
    brier_model["by_market"] = _brier_block(model_rows, "market")["by_market"]

    # --- Brier sharp (de-vigging) -----------------------------------------
    sharp_rows = brier_sharp_rows(markets, results, league_by_match=league_by_match)
    brier_sharp: Dict[str, Any] = {"by_method": {}}
    for m in DEVIG_METHODS:
        rows = sharp_rows.get(m) or []
        brier_sharp["by_method"][m] = _brier_block(rows, "sport")
    brier_sharp["shin_z_mean"] = (round(sharp_rows.get("_shin_z_mean"), 5)
                                 if sharp_rows.get("_shin_z_mean") is not None else None)
    brier_sharp["shin_z_n"] = sharp_rows.get("_shin_z_n", 0)

    result.update({
        "coverage": {
            "predictions": len(predictions),
            "predictions_settled": sum(
                1 for p in predictions if str(p.get("esito_finale") or "").lower()
                in ("won", "lost")),
            "clv_history": len(clv_rows),
            "clv_samples": len(samples),
            "bets_with_price": len(taken_by_match),
            "sharp_snapshots": len(snapshots),
            "matches_with_sharp_1x2": len(markets),
            "match_results": len(results),
            "matches_de_vigable": sum(1 for m in markets
                                      if results.get(m) in RESULT_VALUES),
        },
        "clv": clv_block,
        "brier_model": brier_model,
        "brier_sharp": brier_sharp,
    })
    if not samples:
        result["warnings"].append(
            "nessun campione CLV: servono `clv_history` o snapshot sharp "
            f"(bookmaker='{book}') per gli stessi (match_id, esito) delle previsioni")
    if not model_rows:
        result["warnings"].append(
            "nessuna previsione CHIUSA con `prob`: il Brier del modello si popola "
            "quando il settlement salda le righe")
    if not markets:
        result["warnings"].append(
            f"nessun mercato 1X2 sharp completo su bookmaker='{book}' in "
            "price_snapshots: il Brier del de-vigging resta vuoto")
    if unmatched:
        result["warnings"].append(
            f"{len(unmatched)} puntate col prezzo preso non hanno una closing "
            "sharp/CLV corrispondente e restano fuori dalle medie")
    return result


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _fmt_stats(stats: Optional[Dict]) -> str:
    if not stats:
        return "n=0"
    return (f"n={stats['n']:<4} media={stats['mean_pct']:+7.3f}%  "
            f"mediana={stats['median_pct']:+7.3f}%  "
            f"positivi={stats['positive_pct']:5.1f}%")


def format_report(res: Dict[str, Any]) -> str:
    if res.get("error"):
        return f"clv_brier_analytics: ERRORE — {res['error']}"
    out = ["Analisi CLV & Brier (sola lettura)",
           "=" * 34,
           f"DB                : {res['db']} (mode=ro)",
           f"book sharp        : {res['book']}   de-vig: {res['devig_method']}",
           f"filtro era        : {res.get('since') or 'NESSUNO (tutte le ere)'}"]
    cov = res.get("coverage", {})
    out.append("")
    out.append("Copertura: " + ", ".join(f"{k}={v}" for k, v in cov.items()))

    out.append("\n--- CLV (quota presa vs closing sharp) ---")
    overall = res["clv"]["overall"]
    for label, key in (("totale", "raw"), ("vig-free", "vig_free")):
        if key == "vig_free" and not overall.get("vig_free_n"):
            out.append(f"  {label:<9} : n=0 (serve il mercato completo per devigare)")
            continue
        out.append(f"  {label:<9} : {_fmt_stats(overall.get(key))}")
    out.append(f"  campioni   : {overall['samples']} "
               f"(fonti closing: {overall.get('by_closing_source')})")
    for group in ("by_sport", "by_market"):
        block = res["clv"].get(group) or {}
        if not block:
            continue
        out.append(f"  per {group[3:]}:")
        for name, st in block.items():
            out.append(f"    {name:<12} {_fmt_stats(st.get('raw'))}")

    out.append("\n--- Brier del MODELLO (prob. scommessa vs esito) ---")
    bm = res["brier_model"]
    if not bm.get("overall"):
        out.append("  n=0 — nessuna previsione chiusa con `prob`")
    else:
        ov = bm["overall"]
        out.append(f"  totale     : n={ov['n']} Brier={ov['brier']:.5f} "
                   f"(base {ov['base_rate_pct']}%) skill={ov['skill']}")
        for name, st in (bm.get("by_sport") or {}).items():
            out.append(f"    {name:<12} n={st['n']:<4} Brier={st['brier']:.5f} "
                       f"skill={st['skill']}")
        for name, st in (bm.get("by_market") or {}).items():
            out.append(f"    [{name}] n={st['n']:<4} Brier={st['brier']:.5f} "
                       f"skill={st['skill']}")

    out.append("\n--- Brier del DE-VIGGING sulla closing sharp ---")
    bs = res["brier_sharp"]
    if not bs.get("by_method", {}).get("shin", {}).get("overall"):
        out.append("  n=0 — nessun mercato 1X2 sharp completo con risultato noto")
    else:
        for method in DEVIG_METHODS:
            st = (bs["by_method"].get(method) or {}).get("overall")
            if not st:
                continue
            mark = "  <-- attivo" if method == res["devig_method"] else ""
            out.append(f"  {method:<15} n={st['n']:<5} Brier={st['brier']:.5f} "
                       f"skill={st['skill']}{mark}")
        if bs.get("shin_z_n"):
            out.append(f"  z di Shin medio : {bs['shin_z_mean']} "
                       f"(su {bs['shin_z_n']} mercati) — quota di denaro informato")
    for w in res.get("warnings", []):
        out.append(f"\n⚠️  {w}")
    return "\n".join(out)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="CLV e Brier Score dal ledger (sola lettura, offline)")
    ap.add_argument("--db", default=None, help="path del DB (default: tracker.DB_PATH)")
    ap.add_argument("--since", default=None,
                    help="filtro d'era ISO (es. 2026-09-19) sulla data dell'osservazione")
    ap.add_argument("--book", default=None,
                    help=f"book sharp in price_snapshots (default {DEFAULT_BOOK})")
    ap.add_argument("--method", default="shin", choices=list(DEVIG_METHODS),
                    help="metodo di de-vig per il CLV vig-free (default shin)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    book = args.book
    if not book:
        try:
            import steam_move
            book = steam_move.book_name()
        except Exception:
            book = DEFAULT_BOOK
    db_path = Path(args.db) if args.db else default_db_path()

    res = analyze(db_path, since=args.since, book=book, method=args.method)
    if args.json:
        print(json.dumps(res, indent=2, ensure_ascii=False))
    else:
        print(format_report(res))
    return 1 if res.get("error") else 0


if __name__ == "__main__":                                    # pragma: no cover
    sys.exit(main())
