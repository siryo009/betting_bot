"""oracle_skips.py — Perche' il gate top-down scarta un candidato (03/10/2026).

PERCHE' ESISTE. Il gate top-down (`auto_bet._top_down_eval`) e' fail-closed:
senza una verita' sharp non si ordina. Nel log questo appare come
`top-down SKIP [no_oracle]` / `[linea]` / `no value`, ma **il log non e'
persistente** (retention ~5h) e non si puo' contare nulla: il 01/10 la domanda
"quanti pick perde l'oracolo perche' la linea non e' prezzabile?" non aveva
risposta misurabile. Questo modulo registra OGNI scarto in un JSONL sul volume
e produce il conteggio per motivo, mercato e lega.

Motivi (machine-readable, mai prosa):
  - `no_oracle` — la partita non e' in nessuna cache sharp (assente/incompleta);
  - `linea`     — la cache h2h c'e' ma e' stantia: la linea va pagata
                  (follow-the-money) e l'oracolo arriva al giro dopo;
  - `no_value`  — l'oracolo c'e' ma l'EV non raggiunge la soglia;
  - altri motivi restituiti dal gate (es. quota non valida) sono riportati
    con la loro chiave.

DEDUP. Il giro gira ogni 60s e lo stesso pick verrebbe registrato centinaia di
volte: si scrive UNA riga per (giorno, match_id, esito, motivo, finestra,
esito del fetch, CAUSA del rifiuto). La memo e' in-process (nessun I/O per
giro); dopo un riavvio un pick ancora aperto puo' essere registrato di nuovo —
e' un'ottimizzazione di volume, non correttezza. La causa del rifiuto entra
nella chiave per CLASSE (la parte stabile, senza i secondi) cosi' due rifiuti
diversi dello stesso pick restano distinguibili senza riempire il log.

Diagnostica pura: sola scrittura del proprio log, nessun ordine, nessuna
decisione. Fail-safe totale (mai eccezioni verso il giro puntate).

CLI: venv/bin/python oracle_skips.py [--days N] [--json]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import DATA_DIR
from telemetry_logs import iter_events as _iter_jsonl_events

logger = logging.getLogger("oracle_skips")



def skip_log_path() -> Path:
    """Path del log letto a RUNTIME (env `ORACLE_SKIP_LOG`).

    NON una costante di modulo: letto all'import, un cambio di env (isolamento
    nei test, path diverso in produzione) non avrebbe effetto e le scritture
    finirebbero nel file di PRODUZIONE — bug reale osservato in
    `tennis_quant` il 03/10.
    """
    return Path(os.getenv(
        "ORACLE_SKIP_LOG", str(DATA_DIR / "execution" / "oracle_skips.jsonl")))

#: Memo di dedup: {chiave: True}. Si azzera al cambio di giorno UTC.
_SEEN: Dict[str, bool] = {}
_SEEN_DAY: Optional[str] = None


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def reset_dedup() -> None:
    """Azzera la memo di dedup (usato dai test e al cambio giorno)."""
    global _SEEN, _SEEN_DAY
    _SEEN = {}
    _SEEN_DAY = None


def _window_bucket(in_window: Optional[bool]) -> str:
    """Bucket del dedup: 'in' / 'out' / '?' (finestra non valutabile)."""
    if in_window is None:
        return "?"
    return "in" if in_window else "out"


def _refusal_class(refusal: Optional[str]) -> str:
    """Classe STABILE del rifiuto, per la chiave di dedup.

    Il TESTO di un rifiuto contiene valori che cambiano di secondo in secondo
    (`dedup (73s < 120s)`, `budget oracolo esaurito (2/2 oggi)`): mettendo la
    stringa intera nella chiave si scriverebbe una riga per ciclo di 60s —
    esattamente il flood che il dedup esiste per evitare. Si tiene la CAUSA,
    cioe' la parte prima della parentesi: stabile per costruzione.
    """
    txt = str(refusal or "").strip()
    if not txt:
        return ""
    return txt.split("(", 1)[0].strip()


def record_skip(pick: dict, reason: str, *, detail: Optional[str] = None,
                ev: Optional[float] = None,
                in_window: Optional[bool] = None,
                action: Optional[str] = None,
                refusal: Optional[str] = None) -> Optional[dict]:
    """Registra uno scarto del gate top-down (fail-safe, dedup giornaliero).

    `in_window` dice se il pick era nella FINESTRA ESECUTIVA
    (`auto_bet.t60_window` = "within"): il gate gira su OGNI candidato del
    board, PRIMA del controllo T-60, quindi fuori finestra uno scarto NON e'
    un ordine perso. Il dedup tiene separati i due bucket, cosi' lo stesso
    pick puo' comparire una volta fuori e una dentro la finestra (la
    transizione e' l'informazione utile).

    `action`/`refusal` (06/10/2026) sono l'ESITO STRUTTURATO del fetch
    on-demand tentato per quel pick (`fetched`, `refused`, `tier_not_paid`,
    `outside_window`, `not_recoverable`, `tier_unreadable`, `error`) e, per i
    rifiuti, la causa dichiarata dal gate (`budget oracolo esaurito (...)`, i
    checkpoint, la dedup...). Prima quell'informazione viveva solo nel TESTO
    di `detail`: leggibile a occhio, non contabile ("quante fetch pagate e
    quante rifiutate dal budget?" non aveva risposta numerica).

    Ritorna l'evento scritto, oppure None se era un duplicato del giorno o se
    la scrittura e' fallita (mai eccezioni).
    """
    global _SEEN, _SEEN_DAY
    day = _today()
    if _SEEN_DAY != day:
        _SEEN, _SEEN_DAY = {}, day
    mid = str((pick or {}).get("match_id") or "?")
    esito = str((pick or {}).get("esito_key") or "?")
    reason = str(reason or "unknown")
    bucket = _window_bucket(in_window)
    act = str(action or "")
    # L'esito del fetch entra nella CHIAVE di dedup (come il bucket di
    # finestra): lo stesso pick puo' comparire una volta rifiutato per tier e
    # una volta, piu' tardi, con la fetch PAGATA — la transizione e'
    # l'informazione utile, non un duplicato.
    #
    # ⚠️ 06/10/2026 — la CAUSA del rifiuto e' nella chiave (classe stabile,
    # non il testo che contiene i secondi). Senza, due rifiuti DIVERSI dello
    # stesso pick collassavano in una riga e la telemetria mentiva: i due
    # rifiuti per budget di AFCON non sono mai comparsi nel log e i conteggi
    # mostravano il motivo vecchio ("kickoff oltre la finestra di fetch").
    ref = _refusal_class(refusal)
    key = f"{day}|{mid}|{esito}|{reason}|{bucket}|{act}|{ref}"
    if _SEEN.get(key):
        return None
    evt = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "ts_epoch": datetime.now(timezone.utc).timestamp(),
        "match_id": mid,
        "esito_key": esito,
        "mercato": str((pick or {}).get("mercato") or "1X2"),
        "league": (pick or {}).get("league"),
        "sport": (pick or {}).get("sport"),
        "quota": (pick or {}).get("quota"),
        "reason": reason,
        "in_window": in_window,
    }
    if act:
        evt["action"] = act
    if refusal:
        evt["refusal"] = str(refusal)
    if detail:
        evt["detail"] = detail
    if ev is not None:
        try:
            evt["ev"] = round(float(ev), 4)
        except (TypeError, ValueError):
            pass
    try:
        path = skip_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(evt, ensure_ascii=False) + "\n")
    except Exception as e:                                       # pragma: no cover
        logger.warning("oracle_skips: scrittura fallita (%s)", e)
        return None
    _SEEN[key] = True
    return evt


def iter_events(days: Optional[float] = None) -> List[dict]:
    """Eventi dal log (piu' recenti prima), generazioni `.gz` incluse."""
    try:
        return list(_iter_jsonl_events(skip_log_path(), days=days))
    except Exception as e:                                       # pragma: no cover
        logger.warning("oracle_skips: lettura fallita (%s)", e)
        return []


def window_label() -> str:
    """Etichetta della finestra esecutiva dai valori di PRODUZIONE.

    NON una stringa copiata a mano: il 03/10 la chiusura e' scesa a T-5 e una
    etichetta hardcoded (`T-120..T-15`) dichiarerebbe una banda che non esiste
    piu' — la classe di bug dei "testi derivati dalle costanti" (13/09, 21/09).
    Fail-safe: se i valori non sono leggibili, l'etichetta resta generica.
    """
    try:
        import auto_bet
        lo = float(auto_bet.T60_WINDOW_MIN_MIN)
        hi = float(auto_bet.T60_WINDOW_MAX_MIN)
        return f"T-{lo:g}..T-{hi:g}"
    except Exception:
        return "finestra esecutiva"


def summary(days: float = 1.0) -> Dict[str, Any]:
    """Conteggi per motivo, mercato e lega, separando la FINESTRA esecutiva.

    `orders_blocked` e' il numero che conta: scarti di pick che erano gia'
    nella finestra esecutiva (`auto_bet.t60_window` = "within"), cioe' ordini
    che NON sono partiti per colpa dell'oracolo. Fuori finestra uno scarto e'
    atteso (il pick verra' rivalutato quando entra in finestra) e finisce in
    `outside_window`.
    """
    events = iter_events(days=days)
    by_reason: Dict[str, int] = defaultdict(int)
    by_market: Dict[str, int] = defaultdict(int)
    by_league: Dict[str, int] = defaultdict(int)
    in_reason: Dict[str, int] = defaultdict(int)
    in_market: Dict[str, int] = defaultdict(int)
    by_action: Dict[str, int] = defaultdict(int)
    by_refusal: Dict[str, int] = defaultdict(int)
    by_refusal_class: Dict[str, int] = defaultdict(int)
    counts = {"in": 0, "out": 0, "?": 0}
    for e in events:
        reason = str(e.get("reason") or "unknown")
        mkt = str(e.get("mercato") or "?")
        by_reason[reason] += 1
        by_market[mkt] += 1
        by_league[str(e.get("league") or "?")] += 1
        # Esito del fetch on-demand (06/10/2026). Se l'evento non ha il campo
        # (righe scritte prima) NON si inventa un'azione: finisce in `assenti`.
        act = str(e.get("action") or "assenti")
        by_action[act] += 1
        if e.get("refusal"):
            by_refusal[str(e["refusal"])] += 1
            by_refusal_class[_refusal_class(str(e["refusal"]))] += 1
        win = e.get("in_window")
        if win is True:
            counts["in"] += 1
            in_reason[reason] += 1
            in_market[mkt] += 1
        elif win is False:
            counts["out"] += 1
        else:
            counts["?"] += 1
    return {"days": days, "events": len(events),
            "orders_blocked": counts["in"],
            "outside_window": counts["out"],
            "window_unknown": counts["?"],
            "by_reason": dict(by_reason), "by_market": dict(by_market),
            "by_reason_in_window": dict(in_reason),
            "by_market_in_window": dict(in_market),
            "by_action": dict(sorted(by_action.items(), key=lambda kv: -kv[1])),
            "by_refusal": dict(sorted(by_refusal.items(),
                                      key=lambda kv: -kv[1])),
            "by_refusal_class": dict(sorted(by_refusal_class.items(),
                                            key=lambda kv: -kv[1])),
            "by_league": dict(sorted(by_league.items(),
                                     key=lambda kv: -kv[1]))}


def format_report(days: float = 1.0) -> str:
    s = summary(days=days)
    lines = [f"🎯 Scarti del gate oracolo (ultime {days:g} gg)"]
    if not s["events"]:
        lines.append("  nessuno scarto registrato")
        return "\n".join(lines)
    lines.append(f"  scarti: {s['events']} (unici per pick/motivo/giorno)")
    # La riga che risponde a "l'oracolo sta bloccando ordini?": solo gli
    # scarti IN FINESTRA sono ordini persi (fuori finestra il pick verra'
    # rivalutato quando entra in finestra).
    lines.append(f"  ordini bloccati (in finestra {window_label()}): "
                 f"{s['orders_blocked']} | fuori finestra: "
                 f"{s['outside_window']}"
                 + (f" | non valutabili: {s['window_unknown']}"
                    if s["window_unknown"] else ""))
    for reason, n in sorted(s["by_reason"].items(), key=lambda kv: -kv[1]):
        lines.append(f"    • {reason:<10} {n}")
    if s["by_reason_in_window"]:
        lines.append("  motivi degli ORDINI BLOCCATI: " + ", ".join(
            f"{k} {v}" for k, v in sorted(
                s["by_reason_in_window"].items(), key=lambda kv: -kv[1])))
    mkt = ", ".join(f"{k} {v}" for k, v in sorted(s["by_market"].items(),
                                                 key=lambda kv: -kv[1]))
    lines.append(f"  per mercato: {mkt}")
    # Fetch on-demand: quante ne sono state PAGATE e quante RIFIUTATE (e da
    # chi). E' la riga che dice se il tetto crediti o il tiering stanno
    # bloccando l'oracolo, senza dover leggere i log (retention ~5h).
    act = ", ".join(f"{k} {v}" for k, v in s["by_action"].items())
    if act:
        lines.append(f"  fetch on-demand: {act}")
    if s["by_refusal"]:
        lines.append("  rifiuti dichiarati: " + ", ".join(
            f"{k} {v}" for k, v in list(s["by_refusal"].items())[:3]))
    if s.get("by_refusal_class"):
        lines.append("  cause dei rifiuti: " + ", ".join(
            f"{k} {v}" for k, v in list(s["by_refusal_class"].items())[:4]))
    top = list(s["by_league"].items())[:5]
    if top:
        lines.append("  per lega: " + ", ".join(f"{k} {v}" for k, v in top))
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:                # pragma: no cover
    ap = argparse.ArgumentParser(description="Scarti del gate oracolo")
    ap.add_argument("--days", type=float, default=1.0)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    if args.json:
        print(json.dumps(summary(days=args.days), indent=2, ensure_ascii=False))
    else:
        print(format_report(days=args.days))
    return 0


if __name__ == "__main__":                                        # pragma: no cover
    raise SystemExit(main())
