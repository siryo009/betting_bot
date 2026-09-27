"""Finance Agent — rischio e dimensionamento (facciata sul motore decision/).

Sub-agenti delegati (esistenti, gia' testati):
- `decision.kill_switch.status`: KS manuale > stop-loss giornaliero > pausa
  settlement (le sonde reali leggono i file sul volume);
- `decision.engine.build_plan`: la State Machine vera — fail-fast, gate di
  mercato, Risk Engine (`risk_engine.evaluate`, parita' con `is_sane` sulla
  griglia di 54 casi), Stake Engine (`stake_engine.size`, Kelly scalato UNA
  volta, cap tier/lega/risk, cap severo fail-closed sotto il floor) e la
  fabbrica dei comandi (`persist_decision` SEMPRE, `place_order` solo se
  approve + eseguibile + mode live — l'invariante "prima l'evidenza, poi
  l'effetto").

Il Capo legge il verdetto (`plan.record.risk.verdict`) e passa
all'esecuzione SOLO i piani `approve`: il requisito gerarchico
"Finanza approva -> allora esecuzione" e' la mappa comandi del motore,
non una regola riscritta qui.
"""

from __future__ import annotations

from typing import Any, Optional

from decision.engine import build_plan
from decision.feeds import MarketFeed, FeedSnapshot
from decision.models import Signal
from decision.review_queue import ReviewQueue

from .contracts import FinanceOutput


class FinanceAgent:
    """Valuta ogni segnale: kill switch -> rischio -> stake -> piano comandi."""

    name = "finance"

    def __init__(self, *, bankroll: float = 0.0, mode: str = "sim",
                 kills: Optional[Any] = None, feed: Optional[Any] = None,
                 review_queue: Optional[ReviewQueue] = None,
                 provider: str = "", **engine_kwargs: Any) -> None:
        self.bankroll = float(bankroll)
        self.mode = mode
        self.kills = kills          # KillSwitchStatus iniettabile (sonde reali in produzione)
        self.feed = feed            # MarketFeed/snapshot: se None, build_plan decide da env
        self.review_queue = review_queue
        self.provider = provider
        self.engine_kwargs = dict(engine_kwargs)

    def process(self, signal: Signal, *, feed: Optional[MarketFeed | FeedSnapshot] = None,
                bankroll_override: Optional[float] = None, now=None) -> Any:
        """Un piano per segnale. Non esegue nulla (il piano e' solo dati).

        `bankroll_override` e' per l'AdvisorAgent (micro-stake = ri-valutazione
        con bankroll virtuale ridotto): le formule Kelly/cap sono proporzionali,
        il floor no — se anche il tentativo minimo cade sotto il floor il blocco
        e' strutturale e l'Advisor lo dichiara tale.
        """
        bankroll = float(bankroll_override) if bankroll_override is not None else self.bankroll
        return build_plan(
            signal,
            bankroll=bankroll,
            mode=self.mode,
            kills=self.kills,
            review_queue=self.review_queue,
            provider=self.provider,
            feed=feed if feed is not None else self.feed,
            **self.engine_kwargs,
        )

    def process_many(self, signals: list[Signal], *,
                     feed: Optional[MarketFeed | FeedSnapshot] = None,
                     now=None) -> FinanceOutput:
        out = FinanceOutput()
        for signal in signals:
            plan = self.process(signal, feed=feed, now=now)
            out.plans.append(plan)
            verdict = plan.record.risk.verdict
            if verdict == "approve":
                out.approved += 1
            elif verdict == "review":
                out.review += 1
            else:
                out.rejected += 1
        return out
