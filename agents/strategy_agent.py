"""Strategy Agent — classificazione e arricchimento dei segnali (facciata).

Sub-agenti delegati (esistenti, gia' testati):
- `value_filter.PLAYABLE_TIERS` / `get_signal_tier`: la classificazione
  value/strong_value/moderate (le soglie restano UNA nel modulo originale);
- l'arricchimento numerico (prob modello/blend, edge, EV, confidenza,
  copertura ratings) avviene GIA' in `decision.adapters.signal_from_row`
  quando il Data Agent estrae i segnali: qui NON si ricalcola nulla, perche'
  una formula copiata e' una formula che diverge (lezione 13/09).

In Fase 2 questo agente potra' interrogare direttamente `poisson_engine`/
`ml_ensemble` per i mercati non ancora nel ledger; oggi la pipeline di
produzione (fixture_engine/multi_market) scrive gia' le probabilita' nel
ledger e l'adapter le traduce in contratto.
"""

from __future__ import annotations

from typing import Iterable, Optional

from decision.models import Signal
from value_filter import PLAYABLE_TIERS

from .contracts import StrategyOutput


class StrategyAgent:
    """Filtra i segnali per tier giocabile e dichiara i contatori del ciclo."""

    name = "strategy"

    def __init__(self, *, playable_tiers: Optional[Iterable[str]] = None) -> None:
        #: Iniettabile per test; default = la tripla di produzione (UNA fonte).
        self.playable_tiers = tuple(playable_tiers) if playable_tiers else tuple(PLAYABLE_TIERS)

    def process(self, signals: list[Signal]) -> StrategyOutput:
        playable: list[Signal] = []
        rejected = 0
        unclassified = 0
        for signal in signals:
            tier = str(getattr(signal, "tier", "") or "")
            if tier in self.playable_tiers:
                playable.append(signal)
            elif tier == "rejected":
                rejected += 1
            else:
                # Stato ignoto: mai fatto sparire dai conti (lezione 22/09).
                unclassified += 1
        return StrategyOutput(signals=playable, playable=len(playable),
                              rejected=rejected, unclassified=unclassified)
