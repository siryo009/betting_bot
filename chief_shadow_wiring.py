"""Collegamento Fase 2: il ciclo del Chief nel giro di produzione, in SHADOW.

Cosa fa (Task 2.1 del piano di uscita dalla shadow):
- A ogni giro di `auto_bet` (dopo la corsia reale e il percorso shadow della
  catena `decision/`), il **ChiefOrchestrator valuta gli stessi segnali** con
  la propria gerarchia (Data -> Strategy -> Finance -> Advisor -> Execution)
  e REGISTRA il riepilogo del ciclo in un JSONL dedicato
  (`data/decision/chief_cycles.jsonl`, env `CHIEF_CYCLE_LOG`).
- **Nessun effetto reale**: l'Execution Agent monta solo gateway che non
  eseguono (ShadowGateway). Il denaro resta in `auto_bet.run_today_bets`.
- **Zero crediti**: il Data Agent riusa il feed gia' usato dalla catena
  (`DECISION_FEED_REFRESH_MIN_SEC` per il riuso) e legge solo il ledger.
- **Fail-safe totale**: qualunque errore e' una riga di log, mai un'eccezione
  verso `auto_bet`.

Lettura: `venv/bin/python chief_shadow_wiring.py [--days N] [--json]`
(riepilogo dei cicli: verdict, blocchi, consigli dell'Advisor).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("chief_shadow_wiring")

CYCLE_LOG_ENV = "CHIEF_CYCLE_LOG"
CYCLE_ENABLED_ENV = "CHIEF_SHADOW_ENABLED"

_LOG_TAIL = 5000


def chief_shadow_enabled(value: Optional[str] = None) -> bool:
    """Il ciclo del Chief gira nel percorso shadow? Default SI' (non esegue)."""
    raw = os.getenv(CYCLE_ENABLED_ENV) if value is None else value
    if raw is None or not str(raw).strip():
        return True
    return str(raw).strip().lower() not in ("0", "false", "no", "off")


def cycle_log_path() -> Path:
    override = os.getenv(CYCLE_LOG_ENV)
    if override:
        return Path(override)
    try:
        from config import DATA_DIR
        base = Path(DATA_DIR)
    except Exception:
        base = Path(__file__).resolve().parent / "data"
    return base / "decision" / "chief_cycles.jsonl"


def _append_jsonl(path: Path, record: dict[str, Any]) -> bool:
    """Append fail-safe: una scrittura fallita non ferma il giro."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return True
    except Exception as exc:
        logger.warning("chief wiring: registro non scrivibile (%s): %s", path, exc)
        return False


def run_chief_cycle_shadow(*, bankroll: float = 0.0, mode: str = "sim") -> Optional[dict[str, Any]]:
    """Esegue il ciclo del Chief in shadow e ne registra il riepilogo.

    Ritorna il report (o None se spento/errore). Mai eccezioni verso il
    chiamante (`auto_bet._shadow_run`).
    """
    if not chief_shadow_enabled():
        return None
    try:
        from chief_orchestrator import ChiefOrchestrator
        from agents.execution_agent import ExecutionAgent

        chief = ChiefOrchestrator(
            execution=ExecutionAgent(),  # solo gateway che non eseguono
            finance=FinanceAgent_default(bankroll, mode),
        )
        report = chief.run_cycle()
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "kind": "chief_cycle",
            "bankroll": bankroll,
            "mode": mode,
            **report.as_json(),
        }
        _append_jsonl(cycle_log_path(), record)
        return record
    except Exception as exc:
        logger.warning("chief wiring: ciclo shadow saltato (%s)", exc)
        return None


def FinanceAgent_default(bankroll: float, mode: str):
    """FinanceAgent del ciclo con bankroll/mode del giro reale (same-era)."""
    from agents.finance_agent import FinanceAgent
    return FinanceAgent(bankroll=bankroll, mode=mode)


def summarize(days: int = 7, *, path: Optional[Path] = None) -> dict[str, Any]:
    """Riepilogo dei cicli registrati: verdict, blocchi, consigli, advisor."""
    path = path or cycle_log_path()
    out: dict[str, Any] = {"cycles": 0, "ok": 0, "blocked": 0, "by_verdict": {},
                           "advisor_kinds": {}, "blocked_reasons": {}, "files": str(path)}
    if not path.exists():
        return out
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    by_verdict: dict[str, int] = {}
    advisor_kinds: dict[str, int] = {}
    blocked_reasons: dict[str, int] = {}
    cycles = ok = blocked = 0
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle.readlines()[-_LOG_TAIL:]:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                ts = rec.get("ts") or ""
                try:
                    if datetime.fromisoformat(ts.replace("Z", "+00:00")) < cutoff:
                        continue
                except Exception:
                    pass
                cycles += 1
                if rec.get("ok"):
                    ok += 1
                else:
                    blocked += 1
                    br = str(rec.get("blocked_reason") or "?").split(":")[0]
                    blocked_reasons[br] = blocked_reasons.get(br, 0) + 1
                fin = rec.get("finance") or {}
                approved = int((fin.get("approved") or 0))
                review = int((fin.get("review") or 0))
                rejected = int((fin.get("rejected") or 0))
                by_verdict["approve"] = by_verdict.get("approve", 0) + approved
                by_verdict["review"] = by_verdict.get("review", 0) + review
                by_verdict["reject"] = by_verdict.get("reject", 0) + rejected
                for advice in rec.get("advisor") or []:
                    kind = str(advice.get("override_kind") or ("escalation" if advice.get("escalate_review") else "none"))
                    advisor_kinds[kind] = advisor_kinds.get(kind, 0) + 1
    except Exception as exc:
        out["error"] = str(exc)
        return out
    out.update({"cycles": cycles, "ok": ok, "blocked": blocked,
                "by_verdict": by_verdict, "advisor_kinds": advisor_kinds,
                "blocked_reasons": blocked_reasons})
    return out


def format_report(summary: dict[str, Any]) -> str:
    lines = [
        "🏛️ CHIEF ORCHESTRATOR (shadow)",
        f"Finestra: cicli registrati {summary.get('cycles', 0)} "
        f"(ok {summary.get('ok', 0)} / bloccati {summary.get('blocked', 0)})",
        f"Verdict finanza: {summary.get('by_verdict') or {}}",
    ]
    kinds = summary.get("advisor_kinds") or {}
    if kinds:
        lines.append(f"Advisor: {kinds}")
    brs = summary.get("blocked_reasons") or {}
    if brs:
        lines.append(f"Blocchi: {brs}")
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Cicli shadow del Chief Orchestrator")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    s = summarize(days=args.days)
    print(json.dumps(s, indent=2, ensure_ascii=False) if args.json else format_report(s))
