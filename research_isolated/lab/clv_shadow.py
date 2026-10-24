"""CLV SHADOW — raccolta PROSPETTICA delle quote, senza ordini (20/10/2026).

Perche' esiste
==============
Il CLV misurato il 20/10 (+3,67% su 58 puntate) e' un numero **retrospettivo**:
viene dallo stesso periodo e dalla stessa configurazione che hanno generato
l'ipotesi. Misurare la stessa era piu' a lungo non e' una verifica, e'
un'auto-conferma (lezione del 22/09 e del 25/09, dove un sottogruppo storico
positivo e' stato letto come edge).

Questo modulo apre l'unica finestra che puo' contare come test: quote
registrate **dopo** il congelamento dell'ipotesi, con la regola scritta prima
di raccogliere.

Cosa registra (e cosa NON fa)
=============================
* registra **ogni segnale 1X2 aperto**, compresi quelli che la corsia NON
  avrebbe giocato (`status` = `rejected`/`no_value`): senza quelli, la
  selezione del campione diventa una scelta che nessuno puo' controllare;
* registra il prezzo al momento della DECISIONE (prima occorrenza del
  segnale), non un prezzo scelto dopo;
* **non piazza ordini**, non scrive sul ledger, non tocca i circuit breaker
  (CB2 resta armato, LIVE resta spento): connessione `mode=ro` e append su un
  JSONL dentro `research_isolated/`;
* niente rete: la chiusura la scrive gia' la produzione (`closing_line`), qui
  si legge e si aggancia.

Perche' un JSONL append-only e non il ledger
============================================
Il ledger di produzione regola denaro: una riga scritta per errore costa. Un
file del laboratorio e' cancellabile, ispezionabile e non entra in nessuna
query del bot. La dedup e' per `signal_id` (una riga per opportunita': il job
gira ogni minuto e senza dedup lo stesso segnale diventerebbe centinaia di
righe, gonfiando `n` senza aggiungere informazione).

Uso
===
    venv/bin/python -m research_isolated.lab.clv_shadow record [--db PATH]
    venv/bin/python -m research_isolated.lab.clv_shadow status  [--json]
    venv/bin/python -m research_isolated.lab.clv_shadow analyse [--json]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from . import clv_validation as cv
from . import stats

logger = logging.getLogger(__name__)

SHADOW_DIR = Path(__file__).resolve().parents[1] / "out" / "clv_shadow"
DEFAULT_LOG = SHADOW_DIR / "signals.jsonl"

#: Giorni minimi della finestra prospettica (direttiva del 20/10).
WINDOW_DAYS = 7

#: Stati del ledger che il progetto considera giocabili (una sola definizione:
#: `value_filter.PLAYABLE_TIERS`, letta e non ricopiata).
def playable_tiers() -> tuple:
    try:
        from value_filter import PLAYABLE_TIERS
        return tuple(PLAYABLE_TIERS)
    except Exception:                       # pragma: no cover - difensivo
        return ("value", "strong_value", "moderate")


def hypothesis() -> Dict[str, Any]:
    """L'ipotesi congelata, letta dal protocollo (non scritta nel codice).

    Una sola ipotesi: se ne servissero due, si sceglierebbe quella che ha
    funzionato, che e' esattamente il modo di farsi ingannare.
    """
    prereg = cv.load_prereg()
    hyp = dict(prereg.get("hypothesis") or {})
    hyp["prereg_sha256"] = prereg.get("sha256")
    hyp["errors"] = prereg.get("errors") or []
    return hyp


def frozen_at() -> Optional[datetime]:
    """Istante di CONGELAMENTO dell'ipotesi (dal protocollo, non dal codice).

    E' il confine che rende un segnale "prospettico": un segnale NATO prima del
    congelamento appartiene al campione che ha GENERATO l'ipotesi. Registrarlo
    come se fosse nuovo e' il modo piu' comodo di riusare il vecchio holdout —
    la trappola che il protocollo vieta esplicitamente.
    """
    try:
        return cv.parse_ts((cv.load_prereg() or {}).get("frozen_at"))
    except Exception:                       # pragma: no cover - difensivo
        return None


def is_prospective(record: Dict[str, Any],
                   freeze: Optional[datetime] = None) -> bool:
    """Il segnale e' nato DOPO il congelamento? (senza data -> no)."""
    ref = freeze or frozen_at()
    born = cv.parse_ts(record.get("created_at"))
    if ref is None or born is None:
        return False
    return born >= ref


def prospective_rows(rows: Sequence[Dict[str, Any]],
                     freeze: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Solo le osservazioni ammesse nel campione prospettico."""
    ref = freeze or frozen_at()
    return [r for r in rows if is_prospective(r, ref)]


def log_path(path: Optional[Path | str] = None) -> Path:
    """Dove vive il registro (env override per test/diagnostica)."""
    return Path(path or os.getenv("QV_CLV_SHADOW_LOG") or DEFAULT_LOG)


def _day(ts: Optional[str]) -> str:
    dt = cv.parse_ts(ts)
    return dt.date().isoformat() if dt else "n/d"


def _connect(db: Optional[str | Path]) -> sqlite3.Connection:
    from . import loader
    return loader.connect(db)


def _playable_market(market: Any) -> bool:
    """Solo 1X2: e' l'universo dichiarato nell'ipotesi congelata.

    Registrare anche OU/AH sarebbe rumore: quelle corsie sono fail-closed dal
    07/10 (oracolo a linea spento) e non hanno ordini da verificare.
    """
    return str(market or "").strip().upper() == "1X2"


def collect_signals(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Tutti i segnali 1X2 APERTI, giocabili e non. Mai un'eccezione."""
    rows: List[Dict[str, Any]] = []
    try:
        cur = conn.execute(
            "SELECT match_id, mercato, esito, status, prob, quota, "
            "market_prob, ev, market_edge, league, created_at "
            "FROM predictions WHERE esito_finale IS NULL")
    except sqlite3.Error as exc:            # pragma: no cover - difensivo
        logger.warning("clv_shadow: tabella predictions non leggibile (%s)", exc)
        return rows
    matches: Dict[str, Dict[str, Any]] = {}
    try:
        for row in conn.execute(
                "SELECT id, home_team, away_team, commence_time FROM matches"):
            matches[str(row["id"])] = {"home": row["home_team"],
                                       "away": row["away_team"],
                                       "kickoff": row["commence_time"]}
    except sqlite3.Error:
        pass                            # tabella potata: si prosegue senza kickoff
    tiers = playable_tiers()
    for row in cur:
        if not _playable_market(row["mercato"]):
            continue
        info = matches.get(str(row["match_id"]), {})
        status = str(row["status"] or "").strip().lower()
        match_id = str(row["match_id"] or "")
        esito = str(row["esito"] or "").strip()
        rows.append({
            "signal_id": f"{match_id}|{esito}",
            "match_id": match_id,
            "market": str(row["mercato"] or ""),
            "selection": esito,
            "status": status,
            "selected": status in tiers,
            "quota": cv._price(row["quota"]),
            "prob": cv._num(row["prob"]),
            "market_prob": cv._num(row["market_prob"]),
            "ev": cv._num(row["ev"]),
            "edge": cv._num(row["market_edge"]),
            "league": row["league"] or "",
            "home": info.get("home") or "",
            "away": info.get("away") or "",
            "kickoff": info.get("kickoff") or "",
            "created_at": row["created_at"] or "",
        })
    return rows


def load_log(path: Optional[Path | str] = None) -> List[Dict[str, Any]]:
    """Legge il registro; le righe corrotte si saltano (mai un crash)."""
    p = log_path(path)
    out: List[Dict[str, Any]] = []
    if not p.exists():
        return out
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict) and rec.get("signal_id"):
                out.append(rec)
    except OSError as exc:                  # pragma: no cover - difensivo
        logger.warning("clv_shadow: registro non leggibile (%s)", exc)
    return out


def record(*, db: Optional[str | Path] = None, path: Optional[Path | str] = None,
           conn: Optional[sqlite3.Connection] = None,
           now: Optional[datetime] = None) -> Dict[str, Any]:
    """Registra i segnali nuovi. Append-only, dedup per `signal_id`.

    `ts` e' l'istante della PRIMA occorrenza: e' il prezzo al momento della
    decisione, l'unico che puo' essere confrontato con una chiusura senza
    introdurre una scelta a posteriori.
    """
    out: Dict[str, Any] = {"added": 0, "skipped": 0, "skipped_past": 0,
                           "skipped_pre_freeze": 0,
                           "signals": 0, "errors": [],
                           "path": str(log_path(path)),
                           "hypothesis_id": None, "note": ""}
    try:
        hyp = hypothesis()
        out["hypothesis_id"] = hyp.get("id")
        own = conn is None
        if conn is None:
            conn = _connect(db)
        try:
            signals = collect_signals(conn)
        finally:
            if own:
                conn.close()
        existing = {r.get("signal_id") for r in load_log(path)}
        ref = now or datetime.now(timezone.utc)
        freeze = frozen_at()
        ts = ref.isoformat()
        p = log_path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            for sig in signals:
                if sig["signal_id"] in existing:
                    out["skipped"] += 1
                    continue
                if not is_prospective(sig, freeze):
                    # Nato prima del congelamento: appartiene al campione che ha
                    # GENERATO l'ipotesi. Mai contato come dato nuovo.
                    out["skipped_pre_freeze"] += 1
                    continue
                kickoff = cv.parse_ts(sig.get("kickoff"))
                if kickoff is not None and kickoff <= ref:
                    # Partita gia' iniziata: la sua chiusura appartiene al
                    # passato. Registrarla nel campione prospettico sarebbe
                    # riusare il vecchio holdout come se fosse nuovo.
                    out["skipped_past"] += 1
                    continue
                rec = {"ts": ts, "day": ts[:10], "hypothesis_id": hyp.get("id"),
                       **sig}
                fh.write(json.dumps(rec, default=str) + "\n")
                existing.add(sig["signal_id"])
                out["added"] += 1
        out["signals"] = len(signals)
        if out["added"] == 0:
            out["note"] = "nessun segnale nuovo (dedup per signal_id)"
        if out["skipped_pre_freeze"]:
            out["note"] += (f"; {out['skipped_pre_freeze']} segnali NATI prima del "
                            "congelamento (campione storico, non prospettico)")
    except Exception as exc:                # pragma: no cover - difensivo
        out["errors"].append(f"{type(exc).__name__}: {exc}")
        logger.warning("clv_shadow: registrazione fallita (%s)", exc)
    return out


def status(path: Optional[Path | str] = None,
           now: Optional[datetime] = None) -> Dict[str, Any]:
    """Quanti giorni copre la finestra prospettica e se e' analizzabile."""
    every = load_log(path)
    # ⚠️ Solo i segnali NATI dopo il congelamento sono dati prospettici. Senza
    # questo filtro il registro conta come "nuovo" anche cio' che ha generato
    # l'ipotesi (il 20/10 le 45 righe erano TUTTE pre-congelamento: il report
    # diceva "45 osservazioni" mentre il campione prospettico era zero).
    rows = prospective_rows(every)
    out: Dict[str, Any] = {
        "rows": len(rows), "signals": len({r.get("signal_id") for r in rows}),
        "days": 0, "first_ts": None, "last_ts": None, "window_days": WINDOW_DAYS,
        "ready": False, "hypothesis_id": (rows[0].get("hypothesis_id") if rows else None),
        "by_day": {}, "selected": 0, "note": "",
        "pre_freeze": len(every) - len(rows), "frozen_at": None,
    }
    freeze = frozen_at()
    out["frozen_at"] = freeze.isoformat() if freeze else None
    out["hypothesis_id"] = out.get("hypothesis_id") or hypothesis().get("id")
    if not rows:
        out["note"] = ("finestra prospettica NON iniziata: nessun segnale "
                       "nato DOPO il congelamento."
                       + (f" ({out['pre_freeze']} righe registrate ma "
                          "pre-congelamento: campione storico)"
                          if out["pre_freeze"] else "")
                       + " Il verdetto sulla replicabilita' non e' esprimibile.")
        return out
    days = sorted({r.get("day") for r in rows if r.get("day")})
    out["by_day"] = {d: sum(1 for r in rows if r.get("day") == d) for d in days}
    timestamps = sorted(str(r.get("ts")) for r in rows if r.get("ts"))
    out["first_ts"] = timestamps[0] if timestamps else None
    out["last_ts"] = timestamps[-1] if timestamps else None
    out["selected"] = sum(1 for r in rows if r.get("selected"))
    out["hypothesis_id"] = out.get("hypothesis_id") or hypothesis().get("id")
    start = cv.parse_ts(out["first_ts"])
    ref = now or datetime.now(timezone.utc)
    if start is not None:
        out["days"] = max(0, (ref - start).days)
        out["days_calendar"] = len(days)
    out["ready"] = (out["days"] >= WINDOW_DAYS) and out["signals"] >= cv.MIN_SAMPLES
    if not out["ready"]:
        out["note"] = (
            f"finestra in corso: {out['days']}/{WINDOW_DAYS} giorni, "
            f"{out['signals']} segnali distinti (servono >= {cv.MIN_SAMPLES} "
            "con chiusura per concludere qualcosa)")
    return out


def analyse(*, db: Optional[str | Path] = None,
            path: Optional[Path | str] = None) -> Dict[str, Any]:
    """CLV dei segnali registrati, diviso fra SELEZIONATI e NON selezionati.

    Il confronto e' il cuore del test: se il CLV positivo esiste solo sui
    segnali che la corsia avrebbe giocato (e non su quelli scartati), allora
    il gate di selezione porta valore. Se esiste su tutti, il CLV misura il
    prezzo, non la capacita' di scegliere.
    """
    out: Dict[str, Any] = {"available": False, "error": None, "n": 0,
                           "selected": None, "unselected": None,
                           "by_status": {}, "echo": 0, "no_closing": 0,
                           "not_prospective": 0,
                           "rows": 0, "days": 0, "ready": False}
    every = load_log(path)
    rows = prospective_rows(every)
    out["pre_freeze_excluded"] = len(every) - len(rows)
    out["rows"] = len(rows)
    out["days"] = status(path).get("days", 0)
    if not rows:
        out["error"] = ("registro vuoto: nessuna osservazione prospettica"
                        if not every else
                        "nessuna osservazione prospettica: il registro contiene "
                        "solo segnali nati PRIMA del congelamento "
                        f"({out['pre_freeze_excluded']} righe, campione storico)")
        return out
    try:
        own = db is not None
        conn = _connect(db) if own else None
        clv_rows: List[dict] = []
        if conn is not None:
            try:
                clv_rows = [dict(r) for r in conn.execute("SELECT * FROM clv_history")]
            finally:
                conn.close()
    except Exception as exc:                # pragma: no cover - difensivo
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out
    index: Dict[tuple, dict] = {}
    for row in clv_rows:
        index.setdefault((str(row.get("match_id")), str(row.get("esito") or "").strip()), row)
    measured: List[Dict[str, Any]] = []
    for rec in rows:
        key = (str(rec.get("match_id")), str(rec.get("selection") or "").strip())
        row = index.get(key)
        if row is None:
            out["no_closing"] += 1
            continue
        closing, label = cv.closing_of(row)
        buy = cv._price(rec.get("quota"))
        if closing is None or buy is None:
            out["no_closing"] += 1
            continue
        signal_quota = cv._price(row.get("signal_quota"))
        if signal_quota is not None and abs(signal_quota - closing) < 1e-9:
            out["echo"] += 1
            continue
        # SOLO chiusure successive alla registrazione: una chiusura anteriore
        # al segnale e' storia, non osservazione prospettica.
        closing_ts = cv.parse_ts(row.get("updated_at"))
        signal_ts = cv.parse_ts(rec.get("ts"))
        if closing_ts is None or signal_ts is None or closing_ts <= signal_ts:
            out["not_prospective"] = out.get("not_prospective", 0) + 1
            continue
        value = cv.clv_gross(buy, closing)
        if value is None:
            out["no_closing"] += 1
            continue
        measured.append({"signal_id": rec.get("signal_id"),
                         "match_id": rec.get("match_id"),
                         "selected": bool(rec.get("selected")),
                         "status": rec.get("status"),
                         "clv": value, "source": label})
    out["available"] = True
    out["n"] = len(measured)
    if not measured:
        out["error"] = "nessun segnale con chiusura misurabile"
        return out

    def _block(subset: Sequence[dict]) -> Dict[str, Any]:
        vals = [m["clv"] for m in subset]
        block = cv.summarise(vals, [m["match_id"] for m in subset], key="prospective")
        block["n_selected"] = sum(1 for m in subset if m["selected"])
        return block

    out["selected"] = _block([m for m in measured if m["selected"]])
    out["unselected"] = _block([m for m in measured if not m["selected"]])
    out["all"] = _block(measured)
    by_status: Dict[str, Dict[str, Any]] = {}
    for stat in sorted({str(m.get("status")) for m in measured}):
        subset = [m for m in measured if str(m.get("status")) == stat]
        by_status[stat] = _block(subset)
    out["by_status"] = by_status
    out["ready"] = out["all"].get("status") in (cv.STATUS_VERIFIED, cv.STATUS_NOT_VERIFIED)
    return out


def format_status(block: Dict[str, Any]) -> str:
    lines = [
        "📈 CLV SHADOW — finestra prospettica",
        f"ipotesi: {block.get('hypothesis_id')}",
        f"giorni: {block.get('days')}/{block.get('window_days')} · "
        f"segnali distinti: {block.get('signals')} (selezionati: {block.get('selected')})",
        f"pronta per l'analisi: {'sì' if block.get('ready') else 'no'}",
    ]
    if block.get("by_day"):
        lines.append("righe per giorno: " + ", ".join(
            f"{d} {n}" for d, n in block["by_day"].items()))
    if block.get("note"):
        lines.append(block["note"])
    return "\n".join(lines)


def format_analysis(block: Dict[str, Any]) -> str:
    def _row(label: str, b: Optional[Dict[str, Any]]) -> str:
        if not b:
            return f"{label}: n/d"
        ci = b.get("ci95") or [None, None]
        return (f"{label}: n={b.get('n')} CLV={_pct(b.get('mean'))} "
                f"IC95=[{_pct(ci[0])}, {_pct(ci[1])}] stato={b.get('status')}")

    lines = ["📈 CLV SHADOW — analisi", _row("tutti", block.get("all")),
             _row("selezionati", block.get("selected")),
             _row("non selezionati", block.get("unselected"))]
    for stat, b in (block.get("by_status") or {}).items():
        lines.append(_row(f"  status={stat}", b))
    lines.append(f"echo esclusi: {block.get('echo')} · senza chiusura: "
                 f"{block.get('no_closing')} · chiusura anteriore al segnale "
                 f"(fuori finestra): {block.get('not_prospective')}")
    if block.get("error"):
        lines.append(f"⚠️ {block['error']}")
    return "\n".join(lines)


def _pct(value: Optional[float]) -> str:
    return "n/d" if value is None else f"{value * 100:+.2f}%"


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Registro prospettico CLV (shadow, zero ordini)")
    ap.add_argument("action", nargs="?", default="status",
                    choices=("record", "status", "analyse"))
    ap.add_argument("--db", default=None)
    ap.add_argument("--log", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if args.action == "record":
        res = record(db=args.db, path=args.log)
        print(json.dumps(res, indent=1) if args.json else
              f"registrati: {res['added']} nuovi · {res['skipped']} già presenti "
              f"· ipotesi {res['hypothesis_id']}")
        return 0 if not res["errors"] else 1
    if args.action == "analyse":
        res = analyse(db=args.db, path=args.log)
        print(json.dumps(res, indent=1, default=str) if args.json
              else format_analysis(res))
        return 0
    res = status(args.log)
    print(json.dumps(res, indent=1, default=str) if args.json else format_status(res))
    return 0


if __name__ == "__main__":                  # pragma: no cover
    raise SystemExit(main())
