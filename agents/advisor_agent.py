"""AdvisorAgent — il "braccio destro" del Capo (co-pilota per le eccezioni).

Interpellato SOLO quando un report a monte non approva (verdetto `reject`
o `review`) o quando il ciclo e' bloccato. NON e' un'autorita': e' un
CONSIGLIERE — ogni sua proposta passa per un gate prima di diventare un
ordine (in Fase 1: sempre shadow; in Fase 3: revisione umana sulla coda).

Tre strategie di risoluzione, in ordine di prudente preferenza:

1. **Riduzione rischio** (`_resolve_stake`): se il blocco riguarda la SIZE
   (`STAKE_BELOW_FLOOR`, `RISK_TIGHTENED`, `LIQUIDITY_LOW`) ricalcola una
   micro-stake con frazioni Kelly progressivamente ridotte (0.5x, 0.25x,
   minimo di progetto) fermandosi al PRIMO tentativo eseguibile. Le formule
   restano in `adaptive_staking`/`decision.stake_engine`: qui solo tentativi.
2. **Fallback di mercato** (`_resolve_market_switch`): se il 1X2 ha perso
   valore (`EV_TOO_LOW`, `EDGE_TOO_LOW`, `ODDS_TOO_HIGH`), proposta di
   passare al mercato gemello (OU/AH) SE e SOLO se esiste un segnale
   GIOCABILE registrato in quel mercato per lo stesso evento — mai un
   segno calcolato ad hoc: il segno nuovo deve aver superato gli stessi
   gate (fascia quota, EV, edge) nella pipeline multi-mercato.
3. **Analisi del contesto** (`_resolve_context`): un ReasonCode puo'
   essere un FALSO POSITIVO contestuale (mercato in movimento, segnale
   RLM a favore) o un'occasione da COGLIERE comunque (steam chasing):
   in quel caso la proposta e' **scavalcare il gate e metterla in coda
   umana** (`escalate_review=True`) — l'LLM (`analyze_with_llm`,
   opzionale) classifica la situazione, ma la decisione resta dell'umano
   con i bottoni Telegram. La directa `override_approved=True` esiste
   SOLO per il caso micro-stake con tutti i gate tranne la size gia'
   verdi, ed e' dichiarata `override_kind="reduced_stake"`.

Il Chief NON esegue mai un ordine per un piano che la Finanza ha respinto
per VALORE (EV/edge/fascia/lega/qualita'): quelle sono soglie di strategia
congelate (22/09) — l'Advisor puo' solo ESCALARE all'umano.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from agents.contracts import AdvisorResolution
from agents.data_agent import DataAgent
from agents.finance_agent import FinanceAgent
from agents.strategy_agent import StrategyAgent

logger = logging.getLogger("agents.advisor")


class AdvisorAgent:
    """Risoluzione delle eccezioni: risponde solo al Capo, consiglia solo."""

    name = "advisor"

    #: ReasonCode legati alla SIZE: il ridimensionamento e' la risposta giusta.
    SIZE_REASONS = {"stake_below_floor", "risk_tightened", "liquidity_low"}
    #: ReasonCode di VALORE: soglie di strategia — non si scavalcano, si
    #: scala all'umano se il contesto lo giustifica.
    VALUE_REASONS = {"ev_too_low", "edge_too_low", "odds_too_high", "odds_too_low",
                     "not_favourite", "league_not_allowed", "data_quality_low",
                     "confidence_low", "market_incoherent"}
    #: Fallback di mercato: il valore e' finito altrove, stessa partita.
    MARKET_SWITCH_REASONS = {"ev_too_low", "edge_too_low", "odds_too_high"}
    #: Autorita' umane/tecniche: NON risolvibili da un consigliere.
    UNTOUCHABLE_REASONS = {"kill_switch_off", "daily_stop_loss", "weekly_stop_loss",
                           "settlement_paused", "feed_missing", "feed_unavailable",
                           "feed_stale", "feed_not_validated", "already_exposed"}

    #: Frazioni Kelly decrescenti provate per il micro-stake (moltiplicatori
    #: sulla frazione calcolata dallo Stake Engine del piano rifiutato).
    STAKE_SCALE_STEPS = (0.5, 0.25)

    def __init__(self, *, finance: Optional[FinanceAgent] = None,
                 strategy: Optional[StrategyAgent] = None,
                 data: Optional[DataAgent] = None,
                 llm_classifier: Optional[Any] = None,
                 review_queue: Optional[Any] = None) -> None:
        self.finance = finance          # riusato per ri-valutare i piani ridotti
        self.strategy = strategy        # Market Switch: segnali gemelli
        self.data = data                # contesto: altri segnali dello stesso evento
        self.llm_classifier = llm_classifier   # opzionale (Gemini); None = rule engine
        self.review_queue = review_queue

    # ------------------------------------------------------------------
    # Interfaccia richiesta dal Capo
    # ------------------------------------------------------------------
    def resolve_blocker(self, signal: Any, data_report: Any,
                        strategy_report: Any, finance_report: Any) -> AdvisorResolution:
        """Punto d'ingresso: un blocco in entrata, una risoluzione in uscita.

        `finance_report` e' l'uscita del Finance Agent (`FinanceOutput` o un
        singolo `CommandPlan`); `signal` il segnale bloccato. MAI eccezioni
        verso il Capo: un errore dell'Advisor e' un blocco confermato.
        """
        try:
            plan = self._plan_of(finance_report, signal)
            if plan is None:
                return AdvisorResolution(resolved=False,
                                         reason_no="advisor: nessun piano leggibile")
            # Il motivo vero: nel Risk Engine (reject/review) oppure nello
            # STAKE (LIQUIDITY_LOW/STAKE_BELOW_FLOOR: verdetto approve con
            # stake non eseguibile).
            risk = plan.record.risk
            stake_dec = plan.record.stake
            if str(risk.verdict) == "approve" and stake_dec is not None \
                    and not bool(getattr(stake_dec, "executable", False)):
                reason = str(getattr(stake_dec.reason, "value", stake_dec.reason))
            else:
                reason = str(getattr(risk.reason, "value", risk.reason))
            verdict = str(risk.verdict)
            if reason in self.UNTOUCHABLE_REASONS:
                return AdvisorResolution(resolved=False, reason_no=(
                    f"blocco non negoziabile ({reason}): autorita' superiore"))
            if verdict == "review":
                return AdvisorResolution(resolved=False, reason_no=(
                    "verdetto review: c'e' gia' un umano sulla coda"))
            # --- 1. Riduzione rischio -----------------------------------
            if reason in self.SIZE_REASONS:
                res = self._resolve_stake(signal, plan)
                if res is not None:
                    return res
            # --- 2. Fallback di mercato ----------------------------------
            if reason in self.MARKET_SWITCH_REASONS:
                res = self._resolve_market_switch(signal, data_report)
                if res is not None:
                    return res
            # --- 3. Contesto (falso positivo / steam chasing) ------------
            return self._resolve_context(signal, plan, reason)
        except Exception as exc:  # fail-safe assoluto
            logger.warning("advisor: errore interno: %s", exc, exc_info=True)
            return AdvisorResolution(resolved=False, reason_no=f"advisor_error: {exc}")

    # ------------------------------------------------------------------
    # 1. Riduzione rischio (micro-stake)
    # ------------------------------------------------------------------
    def _resolve_stake(self, signal: Any, plan: Any) -> Optional[AdvisorResolution]:
        """Micro-stake: ri-valuta con bankroll VIRTUALE ridotto.

        Perche' il bankroll e non una fraction forzata: Kelly, cap e floor sono
        TUTTI proporzionali al bankroll — una valutazione con bankroll frazionato
        e' esattamente la valutazione "stake ridotto" fatta dal motore VERO
        (nessuna formula copiata, nessun `stake_override` inventato). Il floor
        dell'exchange non e' proporzionale: se anche l'ultimo tentativo cade
        sotto il floor, il blocco e' strutturale e resta (fail-closed)."""
        bankroll = float(getattr(getattr(plan, "record", None), "stake", None) and \
                          plan.record.stake.bankroll) or 0.0
        if bankroll <= 0 or self.finance is None:
            return None
        for scale in self.STAKE_SCALE_STEPS:
            trial_bankroll = round(bankroll * scale, 2)
            if trial_bankroll <= 0:
                continue
            # Ri-valutazione con lo STESSO motore (stessa Finanza, bankroll ridotto).
            trial = self.finance.process(signal, bankroll_override=trial_bankroll)
            record = trial.record
            if record.risk.verdict == "approve" and record.stake is not None \
                    and record.stake.executable:
                return AdvisorResolution(
                    resolved=True, override_approved=True,
                    override_kind="reduced_stake",
                    note=(f"micro-stake {record.stake.stake:g} USDC (bankroll virtuale "
                          f"{trial_bankroll:g} = {int(scale*100)}% del reale): tutti i "
                          "gate verdi, solo la size bloccava il piano originale"),
                    modified_plan=trial,
                    original_reason=str(plan.record.risk.reason.value))
        return AdvisorResolution(resolved=False, reason_no=(
            "micro-stake non eseguibile nemmeno al minimo: floor/cap non sostenibili "
            "(blocco strutturale, non negoziabile)"))

    # ------------------------------------------------------------------
    # 2. Fallback di mercato (OU/AH gemello dello stesso evento)
    # ------------------------------------------------------------------
    def _resolve_market_switch(self, signal: Any,
                               data_report: Any) -> Optional[AdvisorResolution]:
        if self.strategy is None or data_report is None:
            return None
        playable = getattr(data_report, "signals", []) or []
        twins = [s for s in playable
                 if s.match_id == signal.match_id
                 and s.signal_id != signal.signal_id
                 and str(getattr(s, "market", "")) != str(getattr(signal, "market", ""))]
        if not twins:
            return None
        best = max(twins, key=lambda s: (s.ev or 0.0))
        return AdvisorResolution(
            resolved=True, override_approved=False,
            override_kind="market_switch",
            note=(f"inefficienza spostata su {best.market} "
                  f"({best.selection_label or best.outcome}): segno GIOCABILE gia' "
                  f"registrato dai gate multi-mercato (EV {(best.ev or 0)*100:.1f}%) "
                  "— richiesta di revisione umana, non esecuzione automatica"),
            modified_plan=None,
            escalate_review=True,
            original_reason=str(best.tier))

    # ------------------------------------------------------------------
    # 3. Analisi del contesto (falso positivo / steam chasing)
    # ------------------------------------------------------------------
    def _resolve_context(self, signal: Any, plan: Any, reason: str) -> AdvisorResolution:
        context = self._context_features(signal, plan)
        verdict = self._classify(reason, context)
        if verdict is None:
            return AdvisorResolution(resolved=False, reason_no=(
                f"blocco {reason} confermato: nessun indizio di falso positivo"))
        # Un consiglio di contesto NON e' mai un ordine: e' una ESCALATION
        # verso la coda umana (l'LLM classifica, l'umano decide — 15/09).
        return AdvisorResolution(
            resolved=True, override_approved=False,
            override_kind="context_review",
            note=verdict["note"],
            modified_plan=None,
            escalate_review=True,
            context=verdict["features"],
            original_reason=reason)

    def _context_features(self, signal: Any, plan: Any) -> dict[str, Any]:
        """Feature leggere per il classificatore (rule engine o LLM)."""
        dq = getattr(signal, "data_quality", None)
        return {
            "edge": getattr(signal, "edge", None),
            "ev": getattr(signal, "ev", None),
            "price": getattr(signal, "price", None),
            "tier": getattr(signal, "tier", None),
            "market_coherent": getattr(dq, "market_coherent", None),
            "depth_usdc": getattr(dq, "depth_usdc", None),
            "warnings": list(getattr(signal, "warnings", []) or []),
            "risk_detail": str(getattr(plan.record.risk, "detail", "") or ""),
        }

    def _classify(self, reason: str, features: dict[str, Any]) -> Optional[dict[str, Any]]:
        """Classifica il blocco. Rule engine di default; LLM opzionale.

        L'LLM (`llm_classifier(features, reason) -> {"verdict": ..., "note": ...}`)
        puo' ridefinire la NOTA, mai il perimetro: se il reason e' una soglia di
        strategia, l'esito resta una escalation umana in entrambi i casi.
        """
        if self.llm_classifier is not None:
            try:
                out = self.llm_classifier(features, reason)
                if isinstance(out, dict) and out.get("note"):
                    return {"note": str(out["note"]), "features": features}
            except Exception as exc:
                logger.warning("advisor: LLM classifier fallito (%s): rule engine", exc)
        # --- rule engine (deterministico) ---
        edge = features.get("edge")
        ev = features.get("ev")
        coherent = features.get("market_coherent")
        if reason in ("data_quality_low", "confidence_low"):
            return {"note": (
                "modello poco informato su questo evento ma segnale forte: "
                "possibile falso positivo del gate di qualita' — da conferma umana"),
                "features": features}
        if reason in ("ev_too_low", "edge_too_low") and coherent and (edge or 0) > 0:
            return {"note": (
                "edge positivo con EV sotto soglia: possibile steam in corso "
                "(quota che si accorcia) — se confermato, ingresso tardivo "
                "sconsigliato; valutazione umana"),
                "features": features}
        if reason == "market_incoherent":
            return {"note": (
                "book sporco/mercati incoerenti: possibili quote errate da "
                "arbitraggio, ma rischio controparte alto — conferma umana"),
                "features": features}
        return None

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _plan_of(finance_report: Any, signal: Any) -> Optional[Any]:
        """Estrae il piano bloccato: singolo piano o FinanceOutput filtrato.

        "Bloccato" = verdetto non `approve` **oppure** stake non eseguibile:
        e' la semantica reale del motore (il caso LIQUIDITY_LOW nasce nello
        Stake Engine con verdetto `approve` e `stake.executable=False`).
        """
        plans: list[Any] = []
        if finance_report is None:
            return None
        if hasattr(finance_report, "plans"):
            plans = list(finance_report.plans)
        elif hasattr(finance_report, "record"):
            plans = [finance_report]

        def blocked(plan: Any) -> bool:
            risk = plan.record.risk
            if str(risk.verdict) != "approve":
                return True
            stake = plan.record.stake
            return stake is not None and not bool(getattr(stake, "executable", False))

        wanted = str(getattr(signal, "signal_id", "") or "")
        for plan in plans:
            if str(getattr(plan.record.signal, "signal_id", "")) == wanted and blocked(plan):
                return plan
        for plan in plans:  # fallback: il primo bloccato
            if blocked(plan):
                return plan
        return None
