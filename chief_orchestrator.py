"""Chief Orchestrator — la catena di comando gerarchica (FASE 1, shadow).

    ChiefOrchestrator.run_cycle()
        1. DataAgent.process()        -> MarketData (feed + segnali)
           └─ se il gate di mercato NON e' validato: ciclo bloccato (fail-closed)
        2. StrategyAgent.process()    -> segnali giocabili (tier)
        3. FinanceAgent.process()     -> un CommandPlan per segnale
           └─ approve / review / reject (kill switch > rischio > stake)
        4. ExecutionAgent.process()   -> SOLO i piani `approve` arrivano qui

Requisito gerarchico: l'esecuzione riceve solo piani con verdetto `approve`
E stake eseguibile — e' la mappa comandi di `decision.engine` (place_order
esiste solo li'), filtrata qui per difesa in profondita'.

**Fase 1 = shadow**: l'esecuzione usa gateway che non eseguono nulla; il
denaro resta in `auto_bet.run_today_bets`. Headless-safe: zero Telegram,
zero interattivita' — richiamabile da job APScheduler, CLI o HTTP/n8n.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from agents.contracts import CycleReport
from agents.data_agent import DataAgent
from agents.execution_agent import ExecutionAgent
from agents.finance_agent import FinanceAgent
from agents.strategy_agent import StrategyAgent

logger = logging.getLogger("chief_orchestrator")


class ChiefOrchestrator:
    """Incatena i 4 Lead Agent. Tutte le dipendenze sono iniettabili (test
    offline senza DB, rete, provider o Telegram)."""

    def __init__(self, data: Optional[DataAgent] = None,
                 strategy: Optional[StrategyAgent] = None,
                 finance: Optional[FinanceAgent] = None,
                 execution: Optional[ExecutionAgent] = None,
                 advisor: Optional[Any] = None, *,
                 advisor_enabled: Optional[bool] = None) -> None:
        self.data = data
        self.strategy = strategy or StrategyAgent()
        self.finance = finance
        self.execution = execution or ExecutionAgent()
        # --- Braccio destro (co-pilota per le eccezioni) ---
        # Interpellato SOLO su blocchi (reject di valore / stake non eseguibile);
        # i suoi consigli diventano ordini SOLO via `modified_plan` di micro-stake
        # e passando dallo stesso percorso shadow degli approvati. Interruttore:
        # `DECISION_ADVISOR` (default ON), `advisor_enabled` da codice vince.
        if advisor is not None:
            self.advisor: Optional[Any] = advisor
        else:
            if advisor_enabled is None:
                import os
                advisor_enabled = str(os.getenv("DECISION_ADVISOR", "1")).strip().lower() \
                    not in ("0", "false", "no", "off")
            if advisor_enabled:
                from agents.advisor_agent import AdvisorAgent
                self.advisor = AdvisorAgent(finance=self.finance,
                                            review_queue=None)
            else:
                self.advisor = None
        if self.data is None or self.finance is None:
            # UN MarketFeed condiviso: il refresh e' forzato UNA volta per ciclo
            # (dal Data Agent) e lo STESSO feed valuta i piani ( Finance Agent):
            # due feed farebbero due letture SX e due identita' di mercato diverse.
            from decision.feeds import MarketFeed, feed_enabled
            feed = MarketFeed() if feed_enabled() else None
            if self.data is None:
                self.data = DataAgent(feed=feed)
            if self.finance is None:
                self.finance = FinanceAgent(feed=feed)
        # L'Advisor di default RIUSA SEMPRE la Finanza del ciclo (il micro-stake
        # e' una ri-valutazione con lo stesso motore, bankroll frazionato) e la
        # stessa Strategia/Data per il market switch: anche quando gli agenti
        # sono iniettati dai test, il co-pilota non puo' restare senza motori.
        if self.advisor is not None:
            if getattr(self.advisor, "finance", None) is None:
                self.advisor.finance = self.finance
            if getattr(self.advisor, "strategy", None) is None:
                self.advisor.strategy = self.strategy
            if getattr(self.advisor, "data", None) is None:
                self.advisor.data = self.data
            # --- Stato degli ORDINI APERTI (direttiva 28/09/2026) ---------
            # La lettura arriva dall'Execution Engine (unico che sa cosa e'
            # stato eseguito) e delega al tetto unico di `auto_bet`: il
            # consigliere e la corsia che spende NON possono misurare due
            # esposizioni diverse. Nessun import di produzione a livello di
            # modulo: si passa il bound method, l'import resta pigro.
            if getattr(self.advisor, "exposure_reader", None) is None:
                self.advisor.exposure_reader = self.execution.open_exposure

    def run_cycle(self, *, conn=None, now=None) -> CycleReport:
        report = CycleReport(
            started_at=datetime.now(timezone.utc).isoformat())
        try:
            return self._run(report, conn=conn, now=now)
        except Exception as exc:  # fail-safe: un giro mai rompe il chiamante
            logger.warning("chief: ciclo fallito: %s", exc, exc_info=True)
            report.blocked_reason = f"orchestrator_error: {exc}"
            report.finished_at = datetime.now(timezone.utc).isoformat()
            return report

    def _run(self, report: CycleReport, *, conn=None, now=None) -> CycleReport:
        # --- 1. DATI & MERCATO -------------------------------------------
        market = self.data.process(conn=conn, now=now)
        report.market = market.as_json()
        if not market.validated:
            # Fail-closed: senza mercato validato il ciclo si ferma PRIMA di
            # qualunque valutazione (FEED_MISSING/UNAVAILABLE/STALE/...).
            report.blocked_reason = f"market_gate: {market.gate.reason.value}"
            report.finished_at = datetime.now(timezone.utc).isoformat()
            return report

        # --- 2. STRATEGIA & MODELLAZIONE ---------------------------------
        strategy = self.strategy.process(market.signals)
        report.strategy = strategy.as_json()

        # --- 3. FINANZA & RISCHIO ----------------------------------------
        finance = self.finance.process_many(strategy.signals, now=now)
        report.finance = finance.as_json()

        # --- 3b. BRACCIO DESTRO: risoluzione delle eccezioni --------------
        # Per ogni piano bloccato (reject di valore o stake non eseguibile)
        # l'Advisor consiglia. Le vie d'uscita: (a) micro-stake con override
        # (tutti i gate verdi, solo la size bloccava) -> il piano sostitutivo
        # segue lo STESSO percorso degli approvati (shadow in Fase 1);
        # (b) escalation umana (market switch, falso positivo di contesto).
        # MAI esecuzione diretta di un piano respinto: le soglie di strategia
        # sono congelate (22/09) — si sale all'umano, non si scavalcano.
        approved_plans = [plan for plan in finance.plans
                          if plan.record.risk.verdict == "approve"
                          and (plan.record.stake is None
                               or bool(getattr(plan.record.stake, "executable", False)))]
        if self.advisor is not None:
            for plan in finance.plans:
                stake_exec = plan.record.stake is None or \
                    bool(getattr(plan.record.stake, "executable", False))
                if plan.record.risk.verdict == "approve" and stake_exec:
                    continue  # piano sano: l'Advisor non viene interpellato
                try:
                    resolution = self.advisor.resolve_blocker(
                        plan.record.signal, market, strategy, finance)
                    report.advisor.append(resolution.as_json())
                    if resolution.resolved and resolution.override_approved \
                            and resolution.modified_plan is not None:
                        mod = resolution.modified_plan
                        mod_stake = mod.record.stake
                        if mod.record.risk.verdict == "approve" and mod_stake is not None \
                                and bool(getattr(mod_stake, "executable", False)):
                            approved_plans.append(mod)  # micro-stake ammesso
                            logger.info("chief: advisor micro-stake ammesso (%s): %s",
                                        plan.record.signal.signal_id, resolution.note)
                        else:
                            logger.info("chief: advisor override non eseguibile (%s)",
                                        plan.record.signal.signal_id)
                    elif resolution.escalate_review:
                        logger.info("chief: advisor escalation umana (%s): %s",
                                    plan.record.signal.signal_id, resolution.note)
                except Exception as exc:  # l'Advisor non deve MAI rompere il ciclo
                    logger.warning("chief: advisor fallito su %s: %s",
                                   plan.record.signal.signal_id, exc)

        # --- 3c. RECINTO DI ESPOSIZIONE APERTA (direttiva 28/09/2026) -----
        # L'Advisor interroga lo stato degli ORDINI APERTI a OGNI ciclo: se il
        # capitale immobilizzato (40% del bankroll, ricalcolato sull'equity
        # corrente) non lascia spazio a un nuovo ordine, ogni piano approvato
        # viene RESPINTO — resta shadow finche' un settlement non chiude un
        # match. Il rilascio e' dinamico: nessuna finestra giornaliera da
        # riarmare, il ciclo successivo riparte da solo.
        if self.advisor is not None:
            bankroll = float(getattr(self.finance, "bankroll", 0.0) or 0.0)
            try:
                report.exposure = self.advisor.exposure_status(bankroll)
            except Exception as exc:  # la telemetria non rompe il ciclo
                logger.warning("chief: stato esposizione non leggibile: %s", exc)
            approved_plans = self._apply_exposure_gate(approved_plans, bankroll,
                                                       report)

        # --- 4. ESECUZIONE (solo approvati, difesa in profondita') --------
        # Il requisito gerarchico e' il VERDETTO: solo cio' che la Finanza ha
        # approvato (piu' i micro-stake dell'Advisor passati dallo stesso gate)
        # passa all'Esecuzione. Che un piano approvato contenga o no
        # `place_order` lo decide la mappa comandi del motore (in `sim` non
        # c'e': il ledger delle puntate simulate resta di `auto_bet`).
        execution = self.execution.process(approved_plans)
        report.execution = execution.as_json()

        report.finished_at = datetime.now(timezone.utc).isoformat()
        return report


    def _apply_exposure_gate(self, plans: list, bankroll: float,
                             report: "CycleReport") -> list:
        """Filtra i piani approvati con il recinto di esposizione APERTA.

        Un piano entra solo se la PROIEZIONE (esposizione aperta + il suo
        stake) resta entro il tetto del 40% del bankroll corrente. Il valore
        del tetto NON e' replicato qui: vive una volta sola in `auto_bet` e
        arriva dall'Execution Agent (nessuna soglia duplicata). Un gate non
        leggibile RESPINGE (fail-closed): un recinto di cui non si sa nulla
        non autorizza ordini.
        """
        if self.advisor is None or not plans:
            return plans
        kept: list = []
        for plan in plans:
            stake = float(getattr(plan.record.stake, "stake", 0.0) or 0.0)
            try:
                gate = self.advisor.exposure_gate(bankroll=bankroll, stake=stake)
            except Exception as exc:
                logger.warning("chief: gate esposizione fallito (%s): piano "
                               "respinto", exc)
                report.advisor.append({"resolved": False,
                                       "reason_no": f"exposure_gate_error: {exc}",
                                       "original_reason": "exposure_cap"})
                continue
            if gate.resolved:
                kept.append(plan)
                continue
            report.advisor.append(gate.as_json())
            logger.info("chief: piano %s RESPINTO dal recinto di esposizione "
                        "(%.2f/%.2f USDC, %s ordini aperti)",
                        plan.record.signal.signal_id,
                        gate.exposure.get("open_stake"),
                        gate.exposure.get("cap"), gate.exposure.get("count"))
        return kept


def main(argv: Optional[list[str]] = None) -> int:
    """CLI diagnostica: `python chief_orchestrator.py [--json]`.

    Gira il ciclo in shadow (gateway che non eseguono). Per grana più fina:
    `python -m decision shadow|status|compare|feed`.
    """
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Chief Orchestrator (shadow)")
    parser.add_argument("--json", action="store_true", help="output JSON")
    parser.add_argument("--hours", type=float, default=24.0,
                        help="finestra dei segnali aperti (default 24h)")
    args = parser.parse_args(argv)

    chief = ChiefOrchestrator(
        data=DataAgent(hours=args.hours),
        execution=ExecutionAgent(),  # solo shadow: nessun gateway reale
    )
    report = chief.run_cycle()
    if args.json:
        print(json.dumps(report.as_json(), indent=2, ensure_ascii=False))
    else:
        blocked = f" — BLOCCATO: {report.blocked_reason}" if report.blocked_reason else ""
        print(f"Ciclo orchestratore{blocked}")
        print(f"  mercato   : {report.market}")
        print(f"  strategia : {report.strategy}")
        print(f"  finanza   : {report.finance}")
        print(f"  esecuzione: {report.execution}")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
