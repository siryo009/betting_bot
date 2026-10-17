"""fill_rate.py — FILL RATE **PER PICK**, non per tentativo (17/10/2026).

PERCHE' ESISTE (misura, non decisione). Dal 25/09 al 02/10 il metrico "ordini
riempiti / POST inviati" diceva **7 su 221 (5,5%)** e veniva letto come "il bot
non riesce a eseguire". Era una lettura sbagliata del dato: il giro puntate
ripete lo STESSO pick ogni 60 secondi finche' non esce dalla finestra, quindi
un pick che si riempie al 15o tentativo produce 14 POST a vuoto e **un solo
ordine**. Il numero che risponde alla domanda "il bot esegue?" e':

    fill rate per pick = pick che si sono riempiti / pick che hanno avuto
                         almeno un ORDINE INVIATO all'exchange

Il denominatore sono i PICK (uno per partita+esito), non i POST. Il secondo
numero resta utile — `attempts_per_pick` misura quanto costa in tentativi un
riempimento — ma non e' la probabilita' di eseguire.

COSA MANCAVA. Nel progetto NIENTE registrava i tentativi: il ledger `bets`
scrive solo i RIEMPIMENTI (per scelta: un ordine non eseguito non e' denaro),
`market_quotes` ha i prezzi, `liquidity_monitor` gli scarti per profondita'.
I tentativi esistevano solo nei log del container, che hanno **retention di
minuti** (`railway logs` copre solo la vita del container, `--since` su giorni
passati restituisce 0 righe) e l'exchange **non espone uno storico ordini**
(verificato il 17/10 con 7 endpoint: `/trades` 404, `/user/trades` 400,
`/fills` 404, `/user/fills` 400, `/orders/history` 404, `/user/orders-v3` 400,
`/orders-v3?status=CANCELLED` -> solo gli ordini APERTI). Quindi la metrica
NON era ricostruibile a posteriori: o si strumentava, o si continuava a
misurare il numero sbagliato.

DESIGN (stesso schema di `liquidity_monitor`, regola di progetto: una sola
definizione, delegando dove esiste gia'):
  - un evento JSONL per OGNI tentativo, scritto da `auto_bet._live_fill`
    (l'UNICO punto da cui passa un ordine reale: corsia storica, T-60, chief,
    hedging);
  - `telemetry_logs.iter_events` come lettore (rotazione `.gz` inclusa: una
    metrica che sparisce quando il log viene compresso non e' una metrica);
  - aggregazione per `(match_id, esito)` -> pick, con fallback su `market_id`;
  - `from_log()` per misurare una finestra STORICA quando si ha un estratto
    dei log (`--log file`), senza toccare il volume.

FAIL-SAFE TOTALE: `record_attempt` non solleva MAI (una telemetria rotta non
deve fermare un giro puntate). Non importa `tracker`/`auto_bet`/`bot`: e'
diagnostica, non un percorso d'ordine.

CLI: venv/bin/python fill_rate.py [--days N] [--json] [--log FILE]
"""
from __future__ import annotations

import json
import logging
import os
import re
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from config import DATA_DIR

logger = logging.getLogger("fill_rate")

#: Soglia di affidabilita' del campione: la stessa del progetto
#: (`significance.MIN_SAMPLES`, `multi_market.MIN_RELIABLE_CLOSED`).
MIN_ATTEMPTS = 30

#: Esiti che significano "un ordine e' ARRIVATO all'exchange" (denominatore).
#: `skipped` e' l'unico escluso: li' non e' partito nessun POST.
ATTEMPT_OUTCOMES = ("filled", "partial", "no_fill", "no_bet_id",
                    "resting_open", "error")
#: Esiti che significano "il pick ha portato denaro a casa".
FILLED_OUTCOMES = ("filled", "partial")

#: Etichette dei tentativi (machine-readable, niente prosa nei dati).
OUTCOME_LABELS = {
    "filled": "riempito",
    "partial": "riempimento parziale",
    "no_fill": "inviato, non riempito (IOC senza controparte)",
    "no_bet_id": "riempito ma senza bet_id (fail-closed, non registrato)",
    "resting_open": "parcheggiato come ordine RESTING",
    "error": "errore del provider (esito ignoto)",
    "skipped": "nessun ordine inviato (salto prima del POST)",
}


def enabled() -> bool:
    """Interruttore della telemetria (default ON: misurare non costa nulla)."""
    return os.getenv("FILL_RATE_ENABLED", "1").strip().lower() in (
        "1", "true", "yes", "on")


def log_path() -> Path:
    """Percorso del JSONL (letto a RUNTIME: l'env deve valere subito)."""
    return Path(os.getenv(
        "FILL_RATE_LOG",
        str(DATA_DIR / "execution" / "order_attempts.jsonl")))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _f(val) -> Optional[float]:
    try:
        out = float(val)
    except (TypeError, ValueError):
        return None
    return out


def lane_of(pick: dict) -> str:
    """Corsia del pick (etichetta per il report, nessuna soglia qui dentro).

    Il pick puo' dichiararla esplicitamente (`lane`); altrimenti si deduce dal
    mercato: e' la stessa convenzione usata dai percorsi d'ordine
    (`mercato` = "ML" eSports, "TENNIS", flag gridiron).
    """
    try:
        explicit = str((pick or {}).get("lane") or "").strip().lower()
        if explicit:
            return explicit
        mercado = str((pick or {}).get("mercato") or "").upper()
        if (pick or {}).get("gridiron_lane"):
            return "gridiron"
        if mercado == "TENNIS":
            return "tennis"
        if mercado == "ML":
            return "esports"
        if mercado in ("OU", "AH", "OU_OT", "AH_OT", "ML_OT"):
            return mercado.lower()
        return "football"
    except Exception:                                            # pragma: no cover
        return "unknown"


def pick_key(pick: dict, result: Optional[dict] = None) -> Optional[Tuple[str, str]]:
    """Chiave del PICK: `(match_id, esito)`; fallback sull'identita' di mercato.

    Un pick senza entrambe le identita' non e' aggregabile: viene contato a
    parte (`unkeyed`) invece di essere attribuito a un pick inventato.
    """
    for src in (pick or {}), (result or {}):
        mid = str(src.get("match_id") or "").strip()
        esito = str(src.get("esito_key") or src.get("esito") or "").strip()
        if mid and esito:
            return (mid, esito)
    mid = str((pick or {}).get("market_id")
              or (result or {}).get("market_id") or "").strip()
    if mid:
        sel = str((pick or {}).get("selection_id")
                  or (result or {}).get("selection_id") or "").strip()
        return (mid, sel or "-")
    return None


def classify(result: Optional[dict], stake: Optional[float],
             trace: Optional[dict] = None) -> str:
    """Esito di UN tentativo, dal valore di ritorno di `_live_fill`.

    `trace["outcome"]` (impostato da `auto_bet` dove il solo valore di ritorno
    e' ambiguo) ha la precedenza. Poi: `None` = nessun ordine inviato,
    `resting` = parcheggiato, `ok` = riempito (parziale se lo stake eseguito e'
    inferiore al richiesto), `ok=False` senza `bet_id` ma con stake = il caso
    "riempito ma non confermabile".
    """
    if trace:
        forced = str(trace.get("outcome") or "").strip()
        if forced:
            return forced
    if result is None:
        return "skipped"
    try:
        if result.get("resting"):
            return "resting_open"
        filled = _f(result.get("stake")) or 0.0
        if result.get("ok"):
            req = _f(stake) or 0.0
            if req > 0 and filled + 1e-9 < req:
                return "partial"
            return "filled"
        if filled > 0 and not result.get("bet_id"):
            return "no_bet_id"
        return "no_fill"
    except Exception:                                            # pragma: no cover
        return "error"


def record_attempt(pick: dict, stake: Optional[float], floor: Optional[float],
                   result: Optional[dict] = None,
                   trace: Optional[dict] = None) -> dict:
    """Registra UN tentativo (fail-safe: non solleva mai).

    Ritorna l'evento scritto; su errore di I/O ritorna comunque l'evento con
    `error` valorizzato (come `liquidity_monitor.record_skip`).
    """
    pick = pick or {}
    now = _now()
    outcome = classify(result, stake, trace)
    evt: Dict[str, Any] = {
        "ts": now.isoformat(),
        "ts_epoch": now.timestamp(),
        "outcome": outcome,
        "lane": lane_of(pick),
        "stake": _f(stake),
        "floor": _f(floor),
    }
    key = pick_key(pick, result)
    if key:
        evt["match_id"], evt["esito"] = key
    for name in ("home", "away", "league", "mercato", "market_id",
                 "selection_id"):
        val = pick.get(name)
        if val is None and result:
            val = result.get(name)
        if val not in (None, ""):
            evt[name] = val
    for name in ("ev", "best_ev"):
        val = _f(pick.get(name))
        if val is not None:
            evt["ev"] = val
            break
    if result:
        evt["status"] = result.get("status") or None
        evt["stake_filled"] = _f(result.get("stake"))
        if result.get("bet_id"):
            evt["bet_id"] = result.get("bet_id")
        if result.get("resting"):
            evt["resting"] = True
        if result.get("error"):
            evt["error_message"] = str(result.get("error"))[:160]
    if not enabled():
        evt["skipped_telemetry"] = True
        return evt
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(evt, ensure_ascii=False, default=str) + "\n")
    except Exception as e:                                       # pragma: no cover
        logger.warning("fill_rate: scrittura log fallita (%s)", e)
        evt["error"] = str(e)
    return evt


def iter_attempts(days: Optional[float] = None) -> List[dict]:
    """Tentativi registrati (dal piu' recente), rotazione `.gz` inclusa."""
    from telemetry_logs import iter_events
    try:
        return [e for e in iter_events(log_path(), days=days)
                if isinstance(e, dict) and e.get("outcome")]
    except Exception as e:                                       # pragma: no cover
        logger.warning("fill_rate: lettura log fallita (%s)", e)
        return []


def _bucket(n: int) -> str:
    if n <= 1:
        return "1"
    if n == 2:
        return "2"
    if n <= 5:
        return "3-5"
    if n <= 10:
        return "6-10"
    return ">10"


def summarize(events: List[dict], *, window_days: Optional[float] = None,
              min_attempts: int = MIN_ATTEMPTS) -> Dict[str, Any]:
    """Aggrega i tentativi in **fill rate per pick** + diagnostica dei retry.

    Il denominatore dei `picks_attempted` sono solo gli esiti in
    `ATTEMPT_OUTCOMES`: un pick saltato PRIMA del POST (mercato non risolto,
    prezzo sotto il floor, profondita' insufficiente) non e' un tentativo
    fallito, e contarlo abbasserebbe il fill rate per un motivo che col
    riempimento non c'entra.
    """
    res: Dict[str, Any] = {
        "window_days": window_days,
        "events": len(events),
        "picks_attempted": 0,
        "picks_filled": 0,
        "fill_rate_per_pick": None,
        "attempts_total": 0,
        "fill_rate_per_attempt": None,
        "attempts_per_pick": {"avg": None, "median": None, "max": None},
        "by_outcome": {},
        "by_lane": {},
        "retry_histogram": {},
        "no_fill_status": {},
        "skipped_picks": 0,
        "unkeyed": 0,
        "min_attempts": min_attempts,
        "reliable": False,
    }
    per_pick: Dict[Tuple[str, str], Dict[str, Any]] = {}
    by_outcome: Dict[str, int] = {}
    by_lane: Dict[str, Dict[str, Any]] = {}
    status_no_fill: Dict[str, int] = {}
    skipped_keys = set()

    for evt in events:
        outcome = str(evt.get("outcome") or "unknown")
        by_outcome[outcome] = by_outcome.get(outcome, 0) + 1
        lane = str(evt.get("lane") or "unknown")
        bucket = by_lane.setdefault(lane, {
            "picks_attempted": 0, "picks_filled": 0, "attempts_total": 0,
            "fill_rate_per_pick": None, "fill_rate_per_attempt": None})
        key = None
        if evt.get("match_id") and evt.get("esito"):
            key = (str(evt["match_id"]), str(evt["esito"]))
        elif evt.get("market_id"):
            key = (str(evt["market_id"]), str(evt.get("selection_id") or "-"))
        if key is None:
            res["unkeyed"] += 1
            continue
        if outcome == "skipped":
            skipped_keys.add(key)
            continue
        if outcome not in ATTEMPT_OUTCOMES:
            continue
        entry = per_pick.setdefault(key, {
            "lane": lane, "attempts": 0, "filled": False,
            "no_fill": 0, "resting": 0, "last_outcome": outcome})
        entry["attempts"] += 1
        entry["last_outcome"] = outcome
        bucket["attempts_total"] += 1
        if outcome in FILLED_OUTCOMES:
            entry["filled"] = True
        elif outcome == "resting_open":
            entry["resting"] += 1
        elif outcome == "no_fill":
            entry["no_fill"] += 1
            st = str(evt.get("status") or "?").upper() or "?"
            status_no_fill[st] = status_no_fill.get(st, 0) + 1

    attempts = [e["attempts"] for e in per_pick.values()]
    for entry in per_pick.values():
        bucket = by_lane.setdefault(str(entry["lane"]), {
            "picks_attempted": 0, "picks_filled": 0, "attempts_total": 0,
            "fill_rate_per_pick": None, "fill_rate_per_attempt": None})
        bucket["picks_attempted"] += 1
        if entry["filled"]:
            bucket["picks_filled"] += 1

    res["picks_attempted"] = len(per_pick)
    res["picks_filled"] = sum(1 for e in per_pick.values() if e["filled"])
    res["attempts_total"] = len(attempts) and sum(attempts) or 0
    res["skipped_picks"] = len(skipped_keys - set(per_pick))
    res["by_outcome"] = by_outcome
    res["no_fill_status"] = status_no_fill
    if attempts:
        res["attempts_per_pick"] = {
            "avg": round(sum(attempts) / len(attempts), 3),
            "median": statistics.median(attempts),
            "max": max(attempts),
        }
        histogram: Dict[str, int] = {}
        for n in attempts:
            b = _bucket(n)
            histogram[b] = histogram.get(b, 0) + 1
        res["retry_histogram"] = histogram
    if res["picks_attempted"]:
        res["fill_rate_per_pick"] = round(
            100.0 * res["picks_filled"] / res["picks_attempted"], 2)
    if res["attempts_total"]:
        res["fill_rate_per_attempt"] = round(
            100.0 * sum(1 for e in per_pick.values() if e["filled"]) *
            1.0 / res["attempts_total"], 2)
    for lane, bucket in by_lane.items():
        if bucket["picks_attempted"]:
            bucket["fill_rate_per_pick"] = round(
                100.0 * bucket["picks_filled"] / bucket["picks_attempted"], 2)
        if bucket["attempts_total"] and bucket["picks_filled"]:
            bucket["fill_rate_per_attempt"] = round(
                100.0 * bucket["picks_filled"] / bucket["attempts_total"], 2)
    res["by_lane"] = by_lane
    res["reliable"] = res["attempts_total"] >= int(min_attempts)
    return res


# --- Lettura di un estratto di log (misura di una finestra STORICA) ---------
# I log del container non sono conservati oltre la vita del processo (verificato
# il 17/10: `railway logs --since` su giorni passati restituisce 0 righe e
# l'agente dei log richiede un ruolo che non abbiamo), ma un estratto salvato
# (`railway logs > file`) resta misurabile. `_live_fill` scrive l'IDENTITA' del
# mercato su OGNI esito, quindi i retry dello stesso pick si contano senza
# ambiguita'. Il percorso RESTING produce due righe per lo stesso evento
# ("ordine RESTING ... riempito SUBITO" + "ORDINE REALE riempito"): le righe
# dello stesso tipo sullo stesso mercato entro `_LOG_DEDUP_S` secondi sono la
# STESSA azione, mentre un retry vero arriva al ciclo successivo (60s).
_RE_TS = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)[,.](\d{0,6})")
_RE_FILLED = re.compile(
    r"ORDINE REALE riempito (\S+) \((.*?) vs (.*?), (.*?)\) @ ([0-9.]+)")
_RE_NOFILL = re.compile(
    r"ordine reale (\S+) non riempito \((.*?) vs (.*?), (\w+)\)")
_RE_RESTING_FILLED = re.compile(
    r"ordine RESTING (\S+) \((.*?) vs (.*?), (.*?)\) riempito SUBITO")
_RE_RESTING_RECON = re.compile(
    r"resting_orders: RIEMPITO (\S+) \((.*?) vs (.*?), (.*?)\) @")
_RE_RESTING_OPEN = re.compile(
    r"ORDINE RESTING (?:aperto su SX per )?(\S+) \((.*?) vs (.*?), (.*?)\) @")
#: Righe dello stesso mercato + stesso tipo entro questa finestra = 1 azione.
_LOG_DEDUP_S = 10.0


def _log_ts(line: str) -> Optional[float]:
    m = _RE_TS.match(line)
    if not m:
        return None
    frac = (m.group(2) or "0")[:6].ljust(6, "0")
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc).timestamp() + int(frac) / 1e6
    except Exception:                                            # pragma: no cover
        return None


def from_log(path_or_text: str) -> Dict[str, Any]:
    """Fill rate per pick da un estratto di log (fail-safe, nessuna scrittura).

    Ogni riga di esito e' un TENTATIVO con la sua identita' di mercato; il
    riempimento alza il contatore dei pick eseguiti. I salti PRIMA del POST non
    compaiono qui (in quel caso `_live_fill` non logga un esito) — stessa
    scelta del percorso JSONL: non sono tentativi falliti.
    """
    res: Dict[str, Any] = {"source": str(path_or_text), "lines": 0,
                           "attempts_total": 0, "picks_attempted": 0,
                           "picks_filled": 0, "fill_rate_per_pick": None,
                           "fill_rate_per_attempt": None,
                           "attempts_per_pick": {"avg": None, "median": None,
                                                 "max": None},
                           "reliable": False, "min_attempts": MIN_ATTEMPTS}
    try:
        text = str(path_or_text)
        # Il percorso si prova SOLO se l'input puo' esserlo: un estratto di log
        # multi-riga passato direttamente non e' un filename (e `Path.exists()`
        # su una stringa di migliaia di caratteri solleva `OSError: File name
        # too long`, che faceva uscire la misura con `error` invece dei numeri).
        looks_like_path = "\n" not in text and len(text) < 400
        p = Path(text) if looks_like_path else None
        if p is not None and p.exists() and p.is_file():
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        else:
            lines = text.splitlines()
    except Exception as e:                                       # pragma: no cover
        res["error"] = str(e)
        return res
    res["lines"] = len(lines)
    attempts: Dict[str, int] = {}
    filled: set = set()
    last: Dict[tuple, float] = {}
    for idx, line in enumerate(lines):
        mid, is_fill = None, False
        for pattern, kind in ((_RE_FILLED, True), (_RE_RESTING_FILLED, True),
                              (_RE_RESTING_RECON, True),
                              (_RE_NOFILL, False), (_RE_RESTING_OPEN, False)):
            m = pattern.search(line)
            if m:
                mid, is_fill = m.group(1), kind
                break
        if mid is None:
            continue
        # Dedup delle righe che descrivono la STESSA azione (percorso resting:
        # due log per un solo ordine). Un retry vero e' a ~60s di distanza.
        ts = _log_ts(line)
        if ts is None:
            ts = float(idx)
        prev = last.get((mid, is_fill))
        if prev is not None and abs(ts - prev) <= _LOG_DEDUP_S:
            continue
        last[(mid, is_fill)] = ts
        attempts[mid] = attempts.get(mid, 0) + 1
        if is_fill:
            filled.add(mid)
    res["attempts_total"] = sum(attempts.values())
    res["picks_attempted"] = len(attempts)
    res["picks_filled"] = len(filled)
    if attempts:
        vals = list(attempts.values())
        res["attempts_per_pick"] = {
            "avg": round(sum(vals) / len(vals), 3),
            "median": statistics.median(vals),
            "max": max(vals),
        }
        res["fill_rate_per_pick"] = round(100.0 * len(filled) /
                                          len(attempts), 2)
        res["fill_rate_per_attempt"] = round(
            100.0 * len(filled) / res["attempts_total"], 2)
    res["reliable"] = res["attempts_total"] >= MIN_ATTEMPTS
    return res


def format_report(res: Dict[str, Any]) -> str:
    """Report testuale (Telegram-friendly, nessuna emoji obbligatoria)."""
    lines = ["📊 FILL RATE PER PICK (SX Bet)"]
    win = res.get("window_days")
    lines.append("  finestra: {}".format(f"{win:g} giorni" if win
                                          else "tutto lo storico"))
    if res.get("fill_rate_per_pick") is None:
        lines.append("  nessun tentativo registrato ancora "
                     f"({res.get('events', 0)} eventi)")
        return "\n".join(lines)
    lines.append(
        f"  pick tentati: {res['picks_attempted']} | riempiti: "
        f"{res['picks_filled']} | fill rate PER PICK "
        f"{res['fill_rate_per_pick']:.1f}%")
    att = res.get("attempts_total", 0)
    lines.append(
        f"  invii totali: {att} | tentativi per pick: media "
        f"{(res.get('attempts_per_pick') or {}).get('avg')} (max "
        f"{(res.get('attempts_per_pick') or {}).get('max')}) | fill rate per "
        f"INVIO {res.get('fill_rate_per_attempt')}%")
    hist = res.get("retry_histogram") or {}
    if hist:
        lines.append("  distribuzione tentativi: " + " ".join(
            f"{k}:{hist[k]}" for k in ("1", "2", "3-5", "6-10", ">10")
            if k in hist))
    if res.get("no_fill_status"):
        lines.append("  esiti dei non riempiti: " + ", ".join(
            f"{k}={v}" for k, v in sorted(res["no_fill_status"].items(),
                                          key=lambda kv: -kv[1])[:4]))
    for lane, b in sorted((res.get("by_lane") or {}).items()):
        if b.get("picks_attempted"):
            lines.append(
                f"  • {lane}: {b['picks_filled']}/{b['picks_attempted']} pick"
                f" ({b['fill_rate_per_pick']}%) su {b['attempts_total']} invii")
    if not res.get("reliable"):
        lines.append(f"  ⚠️ campione {att} invii < {res.get('min_attempts')}: "
                     "numeri indicativi, non conclusivi")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:                # pragma: no cover
    import argparse
    ap = argparse.ArgumentParser(
        description="Fill rate per PICK degli ordini reali (SX Bet)")
    ap.add_argument("--days", type=float, default=7.0,
                    help="finestra in giorni (0 = tutto lo storico)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--log", help="misura un ESTRATTO di log invece del JSONL")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    if args.log:
        res = from_log(args.log)
    else:
        days = args.days if args.days and args.days > 0 else None
        res = summarize(iter_attempts(days), window_days=days)
    print(json.dumps(res, indent=2, ensure_ascii=False) if args.json
          else format_report(res))
    return 0 if res.get("picks_attempted") or args.json else 0


if __name__ == "__main__":                                        # pragma: no cover
    raise SystemExit(main())
