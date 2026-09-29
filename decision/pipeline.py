"""decision/pipeline.py — L'orchestratore della catena di decisione.

Ordine FISSO, nessun percorso alternativo:

    kill switch  ──▶  Risk Engine  ──▶  [revisione umana]  ──▶  Stake Engine
    (autorita')       (approve/          (solo se `review`)      (quanto)
                       review/reject)

Proprieta' garantite (verificate dai test):

- **Nessuno stake senza approvazione**: lo Stake Engine viene chiamato solo con
  un verdetto `approve` (anche se l'approvazione arriva da un umano, che la
  sostituisce esplicitamente).
- **Il kill switch viene per primo**: nessun calcolo di modello o di stake
  avviene quando la modalita' e' `off` o lo stop-loss e' attivo.
- **Un solo record**: `DecisionRecord` lega input, decisione, stake e (dopo)
  ordine ed esito — e' la riga che il feedback engine legge.
- **La revisione non scavalca le regole**: un umano puo' approvare un `review`,
  non un `reject` (i `reject` non entrano mai in coda).

Questo modulo non tocca DB, Telegram o provider: e' orchestratore puro, quindi
testabile offline end-to-end.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from . import kill_switch as kill_switch_mod
from . import risk_engine, stake_engine
from .limits import RiskLimits
from .models import (
    DecisionRecord, KillSwitchStatus, Mode, ReasonCode, Signal, StakeDecision,
    as_float, utcnow,
)
from .review_queue import ReviewQueue


def decide(signal: Signal, *, kills: KillSwitchStatus, limits: RiskLimits,
           bankroll: float = 0.0, mode: Optional[Mode] = None,
           review_queue: Optional[ReviewQueue] = None, provider: str = "",
           ml_confidence: Optional[float] = None,
           has_clv_positive: Optional[bool] = None,
           already_exposed: bool = False) -> DecisionRecord:
    """Valuta un segnale e produce il record completo (o in attesa di revisione)."""
    effective_mode: Mode = mode or kills.mode            # type: ignore[assignment]
    risk = risk_engine.evaluate(signal, kills=kills, limits=limits,
                                already_exposed=already_exposed)
    record = DecisionRecord(signal=signal, kill_switch=kills, risk=risk,
                            mode=effective_mode, provider=provider)

    if risk.verdict == "reject":
        return record                                     # nessuno stake calcolato

    if risk.verdict == "review":
        if review_queue is not None:
            review_queue.add(record)
        return record                                     # in attesa di un umano

    record.stake = stake_engine.size(
        signal, risk, bankroll=bankroll, limits=limits, mode=effective_mode,
        ml_confidence=ml_confidence, has_clv_positive=has_clv_positive)
    return record


def resolve_review(queue: ReviewQueue, record_id: str, *, approve: bool,
                   reviewer: str, note: str = "", bankroll: float = 0.0,
                   limits: Optional[RiskLimits] = None, mode: Mode = "live",
                   max_stake_pct: Optional[float] = None,
                   ml_confidence: Optional[float] = None,
                   has_clv_positive: Optional[bool] = None,
                   now: Optional[datetime] = None) -> DecisionRecord:
    """Chiude una revisione: approva (e dimensiona) o rifiuta.

    Un `approve` su una voce scaduta al kickoff NON produce stake: la coda marca
    la voce `expired` e il record esce con `REVIEW_EXPIRED`.
    """
    limits = limits or RiskLimits.from_env()
    item = queue.get(record_id)
    if item is None:
        raise KeyError(f"revisione sconosciuta: {record_id}")

    record = queue.record_for(item)

    if not approve:
        item = queue.reject(record_id, reviewer=reviewer, note=note, now=now)
        record.risk.verdict = "reject"
        record.risk.reason = ReasonCode.REVIEW_REJECTED
        record.risk.detail = f"rifiutato da {reviewer}" + (f": {note}" if note else "")
        record.approved_by = reviewer
        record.review_note = note
        return record

    item = queue.approve(record_id, reviewer=reviewer, note=note, now=now)
    if item.get("status") != "approved":
        record.risk.verdict = "reject"
        record.risk.reason = ReasonCode.REVIEW_EXPIRED
        record.risk.detail = "partita gia' iniziata: revisione scaduta"
        record.approved_by = reviewer
        record.review_note = note
        return record

    tightened = risk_engine.tighten(limits, record.signal.tier, max_stake_pct)
    record.risk.verdict = "approve"
    record.risk.reason = ReasonCode.REVIEW_APPROVED
    record.risk.detail = f"approvato da {reviewer}" + (f": {note}" if note else "")
    record.risk.tightened.update(tightened)
    record.approved_by = reviewer
    record.review_note = note
    record.mode = mode
    record.stake = stake_engine.size(record.signal, record.risk, bankroll=bankroll,
                                     limits=limits, mode=mode,
                                     ml_confidence=ml_confidence,
                                     has_clv_positive=has_clv_positive)
    return record


def pending(queue: ReviewQueue, now=None) -> list[DecisionRecord]:
    """Record in attesa di revisione (senza quelli scaduti al kickoff)."""
    out: list[DecisionRecord] = []
    for item in queue.pending(now=now):
        try:
            out.append(queue.record_for(item))
        except Exception:                                  # voce malformata
            continue
    return out


def decide_many(signals: list[Signal], *, kills: Optional[KillSwitchStatus] = None,
                limits: Optional[RiskLimits] = None, bankroll: float = 0.0,
                review_queue: Optional[ReviewQueue] = None, provider: str = "") -> list[DecisionRecord]:
    """Valuta piu' segnali con la STESSA istantanea di kill switch e limiti."""
    kills = kills or kill_switch_mod.status()
    limits = limits or RiskLimits.from_env()
    return [decide(signal, kills=kills, limits=limits, bankroll=bankroll,
                   review_queue=review_queue, provider=provider)
            for signal in signals]


def summary(records: list[DecisionRecord]) -> dict:
    """Conteggio per verdetto e motivi (telemetria del feedback engine)."""
    out: dict = {"total": len(records), "by_verdict": {}, "by_reason": {},
                 "stake_total": 0.0, "executable": 0, "generated_at": utcnow().isoformat()}
    for record in records:
        out["by_verdict"][record.risk.verdict] = out["by_verdict"].get(record.risk.verdict, 0) + 1
        reason = record.risk.reason.value
        out["by_reason"][reason] = out["by_reason"].get(reason, 0) + 1
        stake: Optional[StakeDecision] = record.stake
        if stake is not None and stake.executable:
            out["executable"] += 1
            # `stake_total` e' una SOMMA di telemetria in float: il denaro
            # (Decimal) si converte all'estremo, mai dentro la formula di stake.
            out["stake_total"] = round(out["stake_total"] + as_float(stake.stake), 2)
    return out


__all__ = ["decide", "decide_many", "pending", "resolve_review", "summary"]
