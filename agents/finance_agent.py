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

**DIRETTIVA FINANZIARIA (29/09/2026) — vietato il `float` sul denaro.**
Il Finance Agent e' l'agente che tocca i soldi, quindi qui bankroll e
sovrascritture di bankroll vivono in `decimal.Decimal` (`decision.models.
Money`): `0.1 + 0.2 != 0.3` in binario e su un conto da 33 USDC con stake
da 1.50 l'errore si accumula a ogni giro. Valgono tre regole:

1. **`Decimal` in ingresso e a riposo**: `bankroll` passa da `money()`
   (stringa, mai `Decimal(float)` senza filtro) e ogni `bankroll_override`
   e' un `Decimal`.
2. **`float` SOLO in transito, in un punto solo**: il motore di stake
   (`decision.stake_engine` -> `adaptive_staking`/`value_filter`) ragiona in
   float perche' le sue formule sono statistiche (Kelly, cap percentuali).
   La conversione avviene qui, esplicitamente via `as_float()`, e non dentro
   nessuna formula: e' la regola "Decimal a riposo, float in transito".
3. **Mai un nuovo `float(...)` sparso**: il divieto e' verificato da
   `test_money_decimal.py`, che scandisce questo modulo e i contratti.

Il denaro che esce e' comunque `Decimal` a valle: `StakeDecision.bankroll`,
`.stake` e `.floor` sono `Money`, quindi il verdetto del Finance Agent non
riporta mai un importo in binario.
"""

from __future__ import annotations

from typing import Any, Optional

from decision.engine import build_plan
from decision.feeds import MarketFeed, FeedSnapshot
from decision.models import Signal, as_float, money
from decision.review_queue import ReviewQueue

from .contracts import FinanceOutput


class FinanceAgent:
    """Valuta ogni segnale: kill switch -> rischio -> stake -> piano comandi."""

    name = "finance"

    def __init__(self, *, bankroll: Any = 0.0, mode: str = "sim",
                 kills: Optional[Any] = None, feed: Optional[Any] = None,
                 review_queue: Optional[ReviewQueue] = None,
                 provider: str = "", **engine_kwargs: Any) -> None:
        # Denaro a riposo in Decimal (direttiva): `float` accettato in ingresso
        # solo perche' il chiamante puo' portarlo da un saldo provider, ma
        # viene convertito SUBITO e mai conservato.
        self.bankroll = money(bankroll)
        self.mode = mode
        self.kills = kills          # KillSwitchStatus iniettabile (sonde reali in produzione)
        self.feed = feed            # MarketFeed/snapshot: se None, build_plan decide da env
        self.review_queue = review_queue
        self.provider = provider
        self.engine_kwargs = dict(engine_kwargs)

    def process(self, signal: Signal, *, feed: Optional[MarketFeed | FeedSnapshot] = None,
                bankroll_override: Optional[Any] = None, now=None) -> Any:
        """Un piano per segnale. Non esegue nulla (il piano e' solo dati).

        `bankroll_override` e' per l'AdvisorAgent (micro-stake = ri-valutazione
        con bankroll virtuale ridotto): le formule Kelly/cap sono proporzionali,
        il floor no — se anche il tentativo minimo cade sotto il floor il blocco
        e' strutturale e l'Advisor lo dichiara tale.
        """
        bankroll = money(bankroll_override) if bankroll_override is not None else self.bankroll
        return build_plan(
            signal,
            # UNICO punto di conversione: il motore di stake e' float per
            # costruzione (formule statistiche), non per pigrizia.
            bankroll=as_float(bankroll),
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
