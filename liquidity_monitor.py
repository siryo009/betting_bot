"""Monitor degli scarti per liquidita' su SX Bet (11/09/2026).

SX Bet e' un exchange: la quota mostrata esiste solo se c'e' qualcuno
dall'altra parte. Quando il book e' sottile il sistema NON entra a mercato —
scarta il segnale o salta l'ordine — per evitare slippage o riempimenti
parziali. Questo modulo registra OGNI scarto in un log JSONL sul volume e
produce un riepilogo per capire quanto edge stiamo perdendo e su quali
mercati: se gli scarti crescono, o le soglie sono troppo severe o i mercati
scansionati sono troppo illiquidi.

Fonti (kind):
  - "scan":    `sx_signals.scan` esclude il mercato prima di generare segnali
               (profondita' totale o del singolo esito sotto soglia);
  - "order":   `auto_bet._live_fill` salta l'ordine reale (size BACK al floor
               inferiore allo stake);
  - "partial": l'ordine reale si riempie solo in parte (riempimento parziale
               materializzato: il resto dello stake non e' stato eseguito).

Il modulo NON importa bot/tracker (autonomo e testabile): l'aggregazione e
l'invio Telegram vivono nel chiamante (`bot.py`).

Diagnostica, non decisionale: nessuna funzione qui blocca o autorizza una
puntata. Tutte le scritture sono fail-safe (un errore di I/O non deve mai
propagarsi al giro delle puntate).
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

from config import DATA_DIR

logger = logging.getLogger("liquidity_monitor")

# Log append-only sul volume (sopravvive ai redeploy). JSONL: una riga per
# scarto, lettura semplice e nessun lock (append atomico sotto i 4KB,
# sufficiente per righe di questa dimensione).
SKIP_LOG = Path(os.getenv(
    "LIQUIDITY_SKIP_LOG", str(DATA_DIR / "execution" / "liquidity_skips.jsonl")))

# Soglie di default usate SOLO per il report (etichette), non per decidere:
# il monitor NON blocca nulla, si limita a misurare gli scarti decisi dai
# guardrail di `sx_signals` (scan) e `auto_bet._live_fill` (ordine).
# Allineate alla taratura del 21/09/2026 (see AGENTS.md): profondita' totale
# del match, minimo per esito, minimo della leg giocata e margine richiesto
# sullo stake (multiplo del book al floor). Erano 25/5/25/x2.0 (11/09).
DEFAULT_DEPTH_USDC = float(os.getenv("SX_MIN_DEPTH_USDC", "20.0"))
DEFAULT_LEG_DEPTH_USDC = float(os.getenv("SX_MIN_LEG_DEPTH_USDC", "4.0"))
DEFAULT_EXEC_DEPTH_USDC = float(os.getenv("SX_MIN_EXEC_DEPTH_USDC", "20.0"))
DEFAULT_DEPTH_MULTIPLIER = float(os.getenv("SX_DEPTH_MULTIPLIER", "1.6"))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def record_skip(kind: str, reason: str, *, match_id: Optional[str] = None,
                home: Optional[str] = None, away: Optional[str] = None,
                esito: Optional[str] = None, quota: Optional[float] = None,
                depth: Optional[float] = None,
                threshold: Optional[float] = None,
                leg_depth: Optional[float] = None,
                stake: Optional[float] = None,
                ev: Optional[float] = None,
                extra: Optional[dict] = None) -> dict:
    """Registra uno scarto per liquidita' (fail-safe: mai eccezioni).

    Ritorna l'evento scritto (utile nei test); in caso di errore ritorna
    comunque un dict con `"error"` valorizzato invece di propagare.
    """
    now = _now()
    evt: dict = {
        "ts": now.isoformat(),
        "ts_epoch": now.timestamp(),
        "kind": kind,
        "reason": reason,
    }
    for key, val in (("match_id", match_id), ("home", home), ("away", away),
                     ("esito", esito), ("quota", quota), ("depth", depth),
                     ("threshold", threshold), ("leg_depth", leg_depth),
                     ("stake", stake), ("ev", ev)):
        if val is not None:
            evt[key] = val
    # Profitto stimato NON realizzato (EV del segnale x stake che sarebbe
    # stato investito): e' la metrica che quantifica l'edge perso.
    if ev is not None and stake:
        try:
            evt["missed_profit"] = round(float(ev) * float(stake), 4)
        except (TypeError, ValueError):
            pass
    if extra:
        evt["extra"] = extra
    try:
        SKIP_LOG.parent.mkdir(parents=True, exist_ok=True)
        with SKIP_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(evt, ensure_ascii=False) + "\n")
    except Exception as e:  # pragma: no cover - difensivo
        logger.warning("liquidity_monitor: scrittura log fallita (%s)", e)
        evt["error"] = str(e)
    return evt


def iter_events(days: Optional[float] = None) -> List[dict]:
    """Eventi dal log (dal piu' recente al piu' vecchio).

    `days` limita alla finestra [now - days, now]; None = tutto lo storico.
    Righe corrotte vengono ignorate (il log non deve mai bloccare il report).

    Legge il file vivo **e** le sue generazioni `.gz` (`telemetry_logs`): il
    03/10/2026 la rotazione automatica comprime i log inattivi, e comprimere
    non deve significare perdere la storia che questo riepilogo misura.
    """
    from telemetry_logs import iter_events as _iter_jsonl
    try:
        return list(_iter_jsonl(SKIP_LOG, days=days))
    except Exception as e:  # pragma: no cover - difensivo
        logger.warning("liquidity_monitor: lettura log fallita (%s)", e)
        return []


def summary(days: float = 7.0) -> dict:
    """Riepilogo degli scarti nella finestra: conteggi, motivi, edge perso."""
    events = iter_events(days)
    by_kind: Dict[str, int] = {}
    by_reason: Dict[str, int] = {}
    matches: set = set()
    missed = 0.0
    stake_lost = 0.0
    for e in events:
        k = str(e.get("kind") or "?")
        by_kind[k] = by_kind.get(k, 0) + 1
        r = str(e.get("reason") or "?")
        by_reason[r] = by_reason.get(r, 0) + 1
        mid = e.get("match_id")
        if mid:
            matches.add(str(mid))
        try:
            missed += float(e.get("missed_profit") or 0.0)
        except (TypeError, ValueError):
            pass
        try:
            stake_lost += float(e.get("stake") or 0.0)
        except (TypeError, ValueError):
            pass
    return {
        "window_days": days,
        "thresholds": {
            "depth_usdc": DEFAULT_DEPTH_USDC,
            "leg_depth_usdc": DEFAULT_LEG_DEPTH_USDC,
            "exec_depth_usdc": DEFAULT_EXEC_DEPTH_USDC,
            "depth_multiplier": DEFAULT_DEPTH_MULTIPLIER,
        },
        "events": len(events),
        "by_kind": by_kind,
        "by_reason": by_reason,
        "unique_matches": len(matches),
        "missed_profit": round(missed, 2),
        "stake_skipped": round(stake_lost, 2),
        "last": events[0] if events else None,
        "file": str(SKIP_LOG),
    }


def _fmt_event(e: dict) -> str:
    label = f"{e.get('home') or '?'} vs {e.get('away') or '?'}"
    if e.get("esito"):
        label += f" ({e['esito']})"
    bits = []
    if e.get("depth") is not None:
        bits.append(f"depth {float(e['depth']):.1f}")
    if e.get("threshold") is not None:
        bits.append(f"soglia {float(e['threshold']):.1f}")
    if e.get("stake") is not None:
        bits.append(f"stake {float(e['stake']):.2f}")
    if e.get("missed_profit") is not None:
        bits.append(f"edge perso {float(e['missed_profit']):.2f}")
    return f"{label} — {e.get('reason')} ({', '.join(bits)})"


def format_report(days: float = 7.0) -> Optional[str]:
    """Testo Telegram del monitor (None se non c'e' nulla da segnalare)."""
    s = summary(days)
    if not s["events"]:
        return None
    lines = [f"💧 *MONITOR LIQUIDITA' SX — ultimi {int(days)}g*", ""]
    lines.append(f"• Soglie attive (USDC): totale {DEFAULT_DEPTH_USDC:.0f}, "
                 f"esito {DEFAULT_LEG_DEPTH_USDC:.0f}, leg giocata "
                 f"{DEFAULT_EXEC_DEPTH_USDC:.0f}, margine "
                 f"x{DEFAULT_DEPTH_MULTIPLIER:.1f} sullo stake")
    lines.append(f"• Scarti totali: *{s['events']}* "
                 f"(su {s['unique_matches']} mercati)")
    if s["by_kind"]:
        parts = ", ".join(f"{k}: {v}" for k, v in sorted(s["by_kind"].items()))
        lines.append(f"• Tipo: {parts}")
    if s["by_reason"]:
        parts = ", ".join(f"{k}: {v}"
                          for k, v in sorted(s["by_reason"].items(),
                                             key=lambda kv: -kv[1]))
        lines.append(f"• Motivo: {parts}")
    if s["missed_profit"]:
        lines.append(f"• Edge NON realizzato (stimato): "
                     f"*{s['missed_profit']:.2f}* USDC")
    if s["stake_skipped"]:
        lines.append(f"• Stake non investito: {s['stake_skipped']:.2f} USDC")
    if s["last"]:
        lines.append("")
        lines.append(f"_Ultimo:_ {_fmt_event(s['last'])}")
    if s["events"] >= 20 and s["missed_profit"] > 5:
        lines.append("")
        lines.append("⚠️ Molti scarti con edge stimato rilevante: valutare "
                     "soglie (`SX_MIN_DEPTH_USDC`, `SX_MIN_LEG_DEPTH_USDC`, "
                     "`SX_MIN_EXEC_DEPTH_USDC`, `SX_DEPTH_MULTIPLIER`) o una "
                     "selezione dei mercati piu' liquidi.")
    return "\n".join(lines)


def main() -> None:  # pragma: no cover - CLI
    import argparse
    p = argparse.ArgumentParser(description="Monitor scarti liquidita' SX Bet")
    p.add_argument("--days", type=float, default=7.0,
                   help="finestra in giorni (default 7)")
    p.add_argument("--json", action="store_true", help="output JSON")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if args.json:
        print(json.dumps(summary(args.days), indent=2, ensure_ascii=False))
        return
    text = format_report(args.days)
    print(text if text else f"Nessuno scarto negli ultimi {int(args.days)}g.")


if __name__ == "__main__":  # pragma: no cover
    main()
