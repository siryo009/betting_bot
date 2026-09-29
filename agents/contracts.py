"""Contratti d'uscita degli agenti (Pydantic, serializzabili per n8n/HTTP).

Ogni `process()` di un agente ritorna uno di questi modelli: il Capo non
consuma mai dict anonimi. Sono INVOLUCRI: il contenuto di valore (`Signal`,
`CommandPlan`, `FeedGateResult`, `DispatchReport`) e' quello di
`decision/models` e `decision/commands` — qui si aggiunge solo l'identita'
dell'agente che ha prodotto il risultato e i contatori del ciclo.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from decision.commands import CommandPlan
from decision.dispatcher import DispatchReport
from decision.feeds import FeedGateResult, FeedSnapshot
from decision.models import Signal


class MarketData(BaseModel):
    """Uscita del Data Agent: istantanea del feed + segnali aperti nel ledger."""

    snapshot: Optional[FeedSnapshot] = None
    gate: FeedGateResult
    signals: list[Signal] = Field(default_factory=list)
    #: Intel live per i segnali in finestra (direttiva 29/09/2026):
    #: statistiche di stagione, ELO, news infortuni, probabili lanciatori.
    #: Vuota (o con `errors > 0`) NON blocca: e' degrado dichiarato.
    intel: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def validated(self) -> bool:
        """Il gate di mercato e' passato (fail-closed: senza feed non si decide)."""
        return bool(self.gate.allowed)

    def as_json(self) -> dict[str, Any]:
        return {
            "gate": self.gate.as_json(),
            "signals": len(self.signals),
            "snapshot": (self.snapshot.model_dump(mode="json")
                         if self.snapshot is not None else None),
            "intel": self.intel,
        }


class StrategyOutput(BaseModel):
    """Uscita dello Strategy Agent: gli stessi Signal, classificati per tier.

    In Fase 1 l'arricchimento (prob model/blend, EV, edge, tier) avviene GIA'
    in `decision.adapters.signal_from_row` quando i segnali vengono estratti
    dal ledger; l'agente li filtra per tier giocabile e dichiara i contatori.
    """

    signals: list[Signal] = Field(default_factory=list)
    playable: int = 0
    rejected: int = 0
    unclassified: int = 0

    def as_json(self) -> dict[str, Any]:
        return {
            "total": len(self.signals),
            "playable": self.playable,
            "rejected": self.rejected,
            "unclassified": self.unclassified,
        }


class FinanceOutput(BaseModel):
    """Uscita del Finance Agent: un piano per segnale (approve/review/reject)."""

    plans: list[CommandPlan] = Field(default_factory=list)
    approved: int = 0
    review: int = 0
    rejected: int = 0

    def as_json(self) -> dict[str, Any]:
        return {
            "plans": len(self.plans),
            "approved": self.approved,
            "review": self.review,
            "rejected": self.rejected,
        }


class ExecutionOutput(BaseModel):
    """Uscita dell'Execution Agent: report del dispatcher (shadow in Fase 1)."""

    reports: list[DispatchReport] = Field(default_factory=list)
    executed: int = 0
    shadow: bool = True

    def as_json(self) -> dict[str, Any]:
        return {
            "dispatched": len(self.reports),
            "executed": self.executed,
            "shadow": self.shadow,
        }


class AdvisorResolution(BaseModel):
    """Risposta dell'AdvisorAgent a un blocco. E' un CONSIGLIO, non un ordine.

    - `resolved=False`: il blocco resta (autorita' superiore, blocco
      strutturale, nessun indizio) — il Capo non fa nulla;
    - `override_approved=True` SOLO per il micro-stake (tutti i gate verdi,
      solo la size bloccava): il piano sostitutivo e' in `modified_plan` e
      segue lo STESSO percorso degli approvati (shadow in Fase 1);
    - `escalate_review=True`: proposta di SALIRE all'umano (coda revisioni
      Telegram), mai esecuzione automatica — e' la via per i falsi positivi
      di contesto e per il market switch.

    `exposure` porta lo stato del recinto di esposizione aperta quando la
    risoluzione lo riguarda (direttiva 28/09/2026): quali ordini sono aperti,
    quanto capitale immobilizzano e quanto ne resta per un nuovo piano.
    """

    resolved: bool = False
    override_approved: bool = False
    override_kind: str = ""          # reduced_stake | market_switch | context_review
    note: str = ""
    reason_no: str = ""             # perche' NON risolto (sempre dichiarato)
    modified_plan: Optional[Any] = None   # CommandPlan (import pigro per evitare cicli)
    escalate_review: bool = False
    context: dict[str, Any] = Field(default_factory=dict)
    original_reason: str = ""
    #: Stato del recinto di esposizione aperta (ordini in corso, tetto 40%).
    exposure: dict[str, Any] = Field(default_factory=dict)

    def as_json(self) -> dict[str, Any]:
        return {
            "resolved": self.resolved,
            "override_approved": self.override_approved,
            "override_kind": self.override_kind,
            "note": self.note,
            "reason_no": self.reason_no,
            "escalate_review": self.escalate_review,
            "original_reason": self.original_reason,
            "has_modified_plan": self.modified_plan is not None,
            "context": self.context,
            "exposure": self.exposure,
        }


class CycleReport(BaseModel):
    """Riepilogo del ciclo del Capo (il dato che finisce in log/HTTP/n8n)."""

    started_at: str = ""
    finished_at: str = ""
    blocked_reason: str = ""
    market: dict[str, Any] = Field(default_factory=dict)
    strategy: dict[str, Any] = Field(default_factory=dict)
    finance: dict[str, Any] = Field(default_factory=dict)
    execution: dict[str, Any] = Field(default_factory=dict)
    #: Consigli dell'Advisor sui blocchi (uno per segnale bloccato, se chiamato)
    advisor: list[dict[str, Any]] = Field(default_factory=list)
    #: Recinto di esposizione aperta (direttiva 28/09/2026): ordini in corso,
    #: capitale immobilizzato e tetto del 40% ricalcolato sull'equity corrente.
    exposure: dict[str, Any] = Field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.blocked_reason

    def as_json(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "blocked_reason": self.blocked_reason,
            "market": self.market,
            "strategy": self.strategy,
            "finance": self.finance,
            "execution": self.execution,
            "advisor": self.advisor,
            "exposure": self.exposure,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }
