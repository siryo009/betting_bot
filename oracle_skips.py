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
volte: si scrive UNA riga per (giorno, match_id, esito, motivo). La memo e'
in-process (nessun I/O per giro); dopo un riavvio un pick ancora aperto puo'
essere registrato di nuovo — e' un'ottimizzazione di volume, non correttezza.

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


def record_skip(pick: dict, reason: str, *, detail: Optional[str] = None,
                ev: Optional[float] = None) -> Optional[dict]:
    """Registra uno scarto del gate top-down (fail-safe, dedup giornaliero).

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
    key = f"{day}|{mid}|{esito}|{reason}"
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
    }
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


def summary(days: float = 1.0) -> Dict[str, Any]:
    """Conteggi per motivo, mercato e lega nella finestra."""
    events = iter_events(days=days)
    by_reason: Dict[str, int] = defaultdict(int)
    by_market: Dict[str, int] = defaultdict(int)
    by_league: Dict[str, int] = defaultdict(int)
    for e in events:
        by_reason[str(e.get("reason") or "unknown")] += 1
        by_market[str(e.get("mercato") or "?")] += 1
        by_league[str(e.get("league") or "?")] += 1
    return {"days": days, "events": len(events),
            "by_reason": dict(by_reason), "by_market": dict(by_market),
            "by_league": dict(sorted(by_league.items(),
                                     key=lambda kv: -kv[1]))}


def format_report(days: float = 1.0) -> str:
    s = summary(days=days)
    lines = [f"🎯 Scarti del gate oracolo (ultime {days:g} gg)"]
    if not s["events"]:
        lines.append("  nessuno scarto registrato")
        return "\n".join(lines)
    lines.append(f"  scarti: {s['events']} (unici per pick/motivo/giorno)")
    for reason, n in sorted(s["by_reason"].items(), key=lambda kv: -kv[1]):
        lines.append(f"    • {reason:<10} {n}")
    mkt = ", ".join(f"{k} {v}" for k, v in sorted(s["by_market"].items(),
                                                 key=lambda kv: -kv[1]))
    lines.append(f"  per mercato: {mkt}")
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
