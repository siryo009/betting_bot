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

import logging
from typing import Any, Optional

from decision.engine import build_plan
from decision.feeds import MarketFeed, FeedSnapshot
from decision.models import Signal, as_float, money
from decision.review_queue import ReviewQueue

from .contracts import FinanceOutput, SizingOutput, ValidatedTrade

logger = logging.getLogger(__name__)


class FinanceAgent:
    """Valuta ogni segnale: kill switch -> rischio -> stake -> piano comandi."""

    name = "finance"

    def __init__(self, *, bankroll: Any = 0.0, mode: str = "sim",
                 kills: Optional[Any] = None, feed: Optional[Any] = None,
                 review_queue: Optional[ReviewQueue] = None,
                 provider: str = "", balance_fn: Optional[Any] = None,
                 **engine_kwargs: Any) -> None:
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
        #: Lettore del saldo USDC fresco (direttiva 04/10/2026: il fetch 
        #: "pulito" avviene SUBITO prima del calcolo size). None = usa il
        #: bankroll del ciclo. Iniettabile -> test offline senza provider.
        self.balance_fn = balance_fn

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

    # ------------------------------------------------------------------
    # Percorso ValidatedTrade (Cervello -> Finanza): sizing finale
    # ------------------------------------------------------------------
    # La Finanza e' l'unico agente che TRASFORMA una decisione in un importo.
    # Il motore Kelly aggressivo vive in `decision.stake_engine` (k=0.65, cap
    # dinamico 12% del bankroll, ticket minimo 2.00): qui si compone con i
    # vincoli che il Cervello ha gia' misurato (Portfolio Shield) e col saldo
    # reale letto AL MOMENTO. Nessuna soglia e' ricopiata: il cap e' quello
    # del motore, il ticket pure, lo spazio del shield arriva dal payload.

    def fresh_bankroll(self, *, override: Optional[Any] = None) -> float:
        """Saldo USDC corrente per il sizing (fetch pulito pre-ordine).

        Ordine di precedenza: override esplicito > `balance_fn` iniettato >
        bankroll del ciclo. Un lettore rotto NON genera un importo casuale:
        ricade sul bankroll del ciclo (dichiarato nel log) e il resto della
        catena applica comunque i propri fail-closed.
        """
        if override is not None:
            return as_float(money(override))
        if self.balance_fn is not None:
            try:
                got = self.balance_fn()
            except Exception as exc:
                logger.warning("finance: saldo fresco non leggibile (%s): uso il "
                               "bankroll del ciclo", exc)
                return as_float(self.bankroll)
            if isinstance(got, dict):
                got = got.get("equity", got.get("available"))
            if got is not None:
                return as_float(money(got))
        return as_float(self.bankroll)

    def size_trade(self, trade: ValidatedTrade, *, bankroll: Optional[Any] = None,
                   ) -> ValidatedTrade:
        """Riempie lo stake del trade col motore Kelly aggressivo.

        Ordine dei vincoli: motore Kelly (k, cap dinamico, ticket) -> Portfolio
        Shield (`block` = non eseguibile, `scale` = cap a `shield_max_usdc`).
        Un blocco del shield non viene mai "aggiustato" abbassando lo stake a
        meta' del ticket: se il residuo non basta, l'ordine non parte.
        """
        bk = self.fresh_bankroll(override=bankroll)
        trade.bankroll = bk
        if trade.shield_action == "block":
            trade.stake = 0.0
            trade.executable = False
            trade.reason = "shield_block"
            return trade
        if trade.true_prob is None:
            trade.stake = 0.0
            trade.executable = False
            trade.reason = "no_true_prob"
            return trade

        from decision.stake_engine import calculate_kelly_stake
        # Denaro in transito: l'unica conversione passa da `as_float` (regola
        # "Decimal a riposo, float in transito"), mai da un `float()` locale —
        # il tripwire di `test_money_decimal` la difende.
        # k DINAMICO (04/10/2026): EV/edge/lega del trade scalano il
        # frazionamento dentro la banda 0.15-0.25 (`dynamic_kelly_fraction`).
        # I tre valori arrivano dal Cervello: la Finanza non ricalcola nulla.
        res = calculate_kelly_stake(as_float(trade.true_prob),
                                    as_float(trade.price), bk,
                                    ev=trade.ev, edge=getattr(trade, "edge", None),
                                    league=trade.league)
        trade.kelly_fraction = as_float(res.get("kelly_fraction") or 0.0)
        trade.kelly_full = as_float(res.get("kelly_full") or 0.0)
        trade.raw_stake = as_float(res.get("raw_stake") or 0.0)
        trade.cap_usdc = as_float(res.get("cap_usdc") or 0.0)
        trade.max_stake_pct = as_float(res.get("max_stake_pct") or 0.0)
        trade.min_ticket = as_float(res.get("min_ticket") or 0.0)
        trade.capped = bool(res.get("capped"))
        stake = as_float(res.get("stake") or 0.0)
        if not res.get("executable"):
            trade.stake = 0.0
            trade.executable = False
            trade.reason = str(res.get("reason") or "not_executable")
            return trade

        # --- Portfolio Shield: lo spazio del blocco correlato prevale ---
        max_usdc = trade.shield_max_usdc
        if max_usdc is not None and stake > as_float(max_usdc):
            stake = round(max(as_float(max_usdc), 0.0), 2)
            trade.reason = "shield_scaled"
        if stake < trade.min_ticket:
            trade.stake = 0.0
            trade.executable = False
            trade.reason = trade.reason or "below_min_ticket"
            return trade
        trade.stake = stake
        trade.executable = True
        trade.reason = trade.reason or "ok"
        return trade

    def process_trades(self, trades: list[ValidatedTrade], *,
                       bankroll: Optional[Any] = None) -> SizingOutput:
        out = SizingOutput()
        for trade in trades or []:
            sized = self.size_trade(trade, bankroll=bankroll)
            out.trades.append(sized)
            out.sized += 1
            if sized.executable:
                out.executable += 1
            else:
                out.skipped += 1
        return out
