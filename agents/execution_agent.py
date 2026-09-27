"""Execution Agent — esecuzione dei piani approvati (Fase 1: SOLO shadow).

Sub-agenti delegati (esistenti, gia' testati):
- `decision.dispatcher.Dispatcher`: instrada ogni comando al primo gateway
  che lo gestisce, apre span, aggrega `DispatchReport`;
- `decision.gateways.ShadowGateway`: REGISTRA i comandi e non esegue nulla
  (dedup per `dedup_key` sul registro JSONL) — e' il set di default della
  Fase 1;
- `decision.gateways.ValidatingLedgerGateway`: opt-in (`persist=True`) —
  scrive la riga sul ledger `decisions` con stato `pending` e la convalida
  (prima persistere, poi convalidare); l'ordine resta registrato, mai
  eseguito.

**Il denaro resta fuori**: nessun gateway reale (`PlaceOrderGateway` verso
`auto_bet._live_fill`) e' montato in Fase 1 — un tripwire lo pretende
(`test_agent_hierarchy.py`). Il cutover e' la Fase 3, decisa sui numeri di
`decision_compare`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from decision.commands import CommandPlan
from decision.dispatcher import Dispatcher
from decision.gateways import ShadowGateway
from decision.middleware import Observability

from .contracts import ExecutionOutput


class ExecutionAgent:
    """Dispatch dei piani approvati. In Fase 1 i gateway sono solo shadow."""

    name = "execution"

    def __init__(self, *, shadow_path: Optional[str | Path] = None,
                 persist: bool = False,
                 observability: Optional[Observability] = None) -> None:
        self.shadow_path = shadow_path
        self.persist = bool(persist)
        self.observability = observability or Observability()

    def _gateways(self) -> list:
        gateways: list = []
        if self.persist:
            # Opt-in: persistenza + validazione sul ledger `decisions`
            # (l'ordine resta registrato, mai eseguito — vedi shadow.py).
            from decision.gateways import ValidatingLedgerGateway
            gateways.append(ValidatingLedgerGateway())
        gateways.append(ShadowGateway(self.shadow_path))
        return gateways

    def process(self, plans: list[CommandPlan]) -> ExecutionOutput:
        out = ExecutionOutput(shadow=True)
        if not plans:
            return out
        gateways = self._gateways()
        dispatcher = Dispatcher(gateways, observability=self.observability)
        for plan in plans:
            report = dispatcher.dispatch(plan)
            out.reports.append(report)
            out.executed += int(report.executed)
        # `shadow` del dispatcher e' True solo se TUTTI i gateway non eseguono:
        # con il ValidatingLedgerGateway (audit) resta True per costruzione,
        # ma lo ricalcoliamo dal primo report reale.
        if out.reports:
            out.shadow = bool(out.reports[0].shadow)
        return out
