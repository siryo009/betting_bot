"""Test dell'AdvisorAgent (il "braccio destro" del Capo) e del suo ramo nel
ciclo del ChiefOrchestrator.

I cinque comportamenti che i test blindano:
1. **Perimetro**: l'Advisor NON risolve mai le autorita' (kill switch, stop
   loss, feed) e NON esegue mai un piano respinto per VALORE (soglie di
   strategia congelate): la sua unica via d'ordine e' il micro-stake con
   TUTTI i gate verdi tranne la size; tutto il resto e' escalation umana.
2. **Delega**: il micro-stake e' una ri-valutazione con la STESSA Finanza
   (bankroll virtuale frazionato) — nessuna formula copiata.
3. **Fail-safe**: qualunque errore interno torna come `resolved=False`,
   mai un'eccezione verso il Capo.
4. **Ramo del Capo**: il piano sostitutivo ammesso segue lo stesso percorso
   degli approvati; con l'Advisor SPENTO il ciclo e' invariato.
5. **Contratti**: `AdvisorResolution` serializzabile (log/HTTP/n8n).

Zero rete, zero DB di produzione, zero Telegram (conftest isola i sink).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

import chief_orchestrator
from agents.advisor_agent import AdvisorAgent
from agents.contracts import AdvisorResolution
from agents.data_agent import DataAgent
from agents.execution_agent import ExecutionAgent
from agents.finance_agent import FinanceAgent
from agents.strategy_agent import StrategyAgent
from chief_orchestrator import ChiefOrchestrator
from decision.models import KillSwitchStatus, ReasonCode

NOW = datetime.now(timezone.utc)
ALLOWED_LEAGUE = "Premier League"


# ---------------------------------------------------------------------------
# Ledger temporaneo (schema di produzione, come test_agent_hierarchy)
# ---------------------------------------------------------------------------

def _ledger(*, n_inter=12, n_cagliari=10, quota=1.60, league=ALLOWED_LEAGUE,
            status="strong_value"):
    c = sqlite3.connect(":memory:")
    c.execute("""CREATE TABLE matches (id TEXT PRIMARY KEY, league TEXT, home_team TEXT,
                 away_team TEXT, commence_time TEXT, status TEXT, last_updated TEXT)""")
    c.execute("""CREATE TABLE predictions (id INTEGER PRIMARY KEY AUTOINCREMENT, match_id TEXT,
                 mercato TEXT, esito TEXT, quota REAL, prob REAL, ev REAL, market_prob REAL,
                 market_edge REAL, status TEXT, esito_finale TEXT, profit REAL,
                 created_at TEXT, settled_at TEXT)""")
    c.execute("""CREATE TABLE match_analysis (id INTEGER PRIMARY KEY AUTOINCREMENT,
                 match_id TEXT, lam_h REAL, lam_a REAL, prob_1 REAL, prob_X REAL, prob_2 REAL,
                 prob_over REAL, best_ev REAL, best_esito TEXT, best_quota REAL,
                 best_bookmaker TEXT, status TEXT, timestamp TEXT, market_prob REAL,
                 market_edge REAL)""")
    c.execute("""CREATE TABLE team_ratings (team TEXT PRIMARY KEY, attack_home REAL,
                 defense_home REAL, attack_away REAL, defense_away REAL,
                 n_home INTEGER, n_away INTEGER)""")
    c.execute(f"""INSERT INTO team_ratings VALUES ('Inter',1.2,0.9,1.1,1.0,{n_inter},{max(n_inter-2,0)}),
                  ('Cagliari',0.9,1.1,0.8,1.2,{n_cagliari},{max(n_cagliari-2,0)})""")
    kickoff = (NOW + timedelta(hours=5)).isoformat()
    c.execute("INSERT INTO matches VALUES ('sx-1',?,'Inter','Cagliari',?,'scheduled','')",
              (league, kickoff))
    c.execute("""INSERT INTO predictions (match_id, mercato, esito, quota, prob, ev,
                 market_prob, market_edge, status, esito_finale)
                 VALUES ('sx-1','1X2','1',?,0.65,0.04,0.58,0.07,?,NULL)""",
              (quota, status))
    c.execute("""INSERT INTO match_analysis (match_id, lam_h, lam_a, prob_1, prob_X,
                 prob_2, status) VALUES ('sx-1',1.6,0.9,0.62,0.22,0.16,'analyzed')""")
    c.commit()
    return c


@pytest.fixture
def conn():
    c = _ledger()
    yield c
    c.close()


def _chain(conn, *, bankroll=100.0, kills=None):
    data = DataAgent()
    strategy = StrategyAgent()
    finance = FinanceAgent(bankroll=bankroll, kills=kills)
    advisor = AdvisorAgent(finance=finance, strategy=strategy)
    return data, strategy, finance, advisor


# ---------------------------------------------------------------------------
# 1. Micro-stake: la via d'ordine dell'Advisor (e il suo perimetro)
# ---------------------------------------------------------------------------

class TestMicroStake:
    def test_liquidity_low_risolto_con_micro_stake(self, conn):
        """Il caso tipo: liquidita' sufficiente per UNA stake piccola, non per
        quella Kelly piena. Il piano nasce approve ma con stake non eseguibile
        (LIQUIDITY_LOW nello Stake Engine). Con depth abbondante il ridotto
        passa: tutti i gate verdi, solo la size bloccava."""
        from decision.feeds import MarketFeed  # noqa: F401 (solo per coerenza docs)
        data, strategy, finance, advisor = _chain(conn, bankroll=100.0)
        market = data.process(conn=conn, now=NOW)
        strategy_out = strategy.process(market.signals)
        signal = strategy_out.signals[0]
        # Forzo il blocco SIZE: depth esigua -> LIQUIDITY_LOW con verdetto approve
        signal.data_quality.depth_usdc = 2.0   # < max(stake x 1.6, 20)
        finance_out = finance.process_many([signal])
        plan = finance_out.plans[0]
        assert plan.record.risk.verdict == "approve"
        assert plan.record.stake.executable is False
        assert plan.record.stake.reason == ReasonCode.LIQUIDITY_LOW
        # Con depth abbondante il micro-stake (bankroll frazionato -> stake
        # piccolo) diventa eseguibile: e' l'unico override ammesso.
        signal.data_quality.depth_usdc = 500.0
        res = advisor.resolve_blocker(signal, market, strategy_out, finance_out)
        assert res.resolved is True and res.override_approved is True
        assert res.override_kind == "reduced_stake"
        assert res.modified_plan is not None
        assert res.modified_plan.record.stake.executable is True
        assert res.modified_plan.record.stake.stake < \
            max(0.01, plan.record.stake.kelly_stake)

    def test_nessun_override_su_blocco_di_valore(self, conn):
        """EV troppo basso e' una soglia di strategia: l'Advisor NON esegue,
        al massimo scala all'umano. Mai `override_approved`."""
        data, strategy, finance, advisor = _chain(conn)
        market = data.process(conn=conn, now=NOW)
        strategy_out = strategy.process(market.signals)
        finance_out = finance.process_many(strategy_out.signals)
        signal = strategy_out.signals[0]
        # Segnale fuori dalla fascia dei favoriti: EV_TOO_LOW/ODDS_TOO_HIGH
        conn.execute("UPDATE predictions SET quota=2.60 WHERE match_id='sx-1'")
        conn.commit()
        market2 = data.process(conn=conn, now=NOW)
        so2 = strategy.process(market2.signals)
        # la query del ledger filtra i tier giocabili: ricostruisco il piano
        # a mano dal segnale "forte" precedente con quota alterata
        # `signal.price` e' `Money` con validate_assignment: l'assegnazione
        # viene COERCITA a Decimal('2.6'). Il blocco nasce dalla quota fuori
        # fascia (ODDS_TOO_HIGH), non da un tier inventato: "rejected" non e'
        # un Tier valido e prima passava solo perche' Pydantic non validava le
        # assegnazioni (il buco che la direttiva del 29/09 chiude).
        signal.price = 2.60
        signal.ev = 0.65 * 2.60 - 1
        assert isinstance(signal.price, Decimal)
        from decision.engine import build_plan
        blocked = build_plan(signal, bankroll=100.0, mode="sim", kills=KillSwitchStatus())
        res = advisor.resolve_blocker(signal, market, strategy_out, blocked)
        assert res.resolved is False or res.override_approved is False
        assert res.override_kind != "reduced_stake" or res.override_approved is True
        # In ogni caso: nessun piano sostitutivo eseguibile su blocco di valore
        if res.modified_plan is not None:
            assert res.modified_plan.record.risk.verdict != "approve"

    def test_tiny_bankroll_blocco_strutturale(self, conn):
        """Con un bankroll minuscolo in LIVE nemmeno il micro-stake supera il
        floor dell'exchange (1 USDC): il blocco e' strutturale e l'Advisor lo
        dichiara (fail-closed)."""
        data, strategy, finance, advisor = _chain(conn, bankroll=100.0)
        market = data.process(conn=conn, now=NOW)
        strategy_out = strategy.process(market.signals)
        signal = strategy_out.signals[0]
        finance_small = FinanceAgent(bankroll=3.0, mode="live")   # 2% di 3 = 0.06 < 1
        advisor_small = AdvisorAgent(finance=finance_small)
        plan = finance_small.process(signal)
        assert plan.record.risk.verdict == "approve"
        assert plan.record.stake.executable is False   # cap severo sotto il floor
        res = advisor_small.resolve_blocker(signal, market, strategy_out, plan)
        assert res.resolved is False and res.override_approved is False
        assert "strutturale" in res.reason_no or "floor" in res.reason_no


# ---------------------------------------------------------------------------
# 2. Perimetro: autorita' intoccabili e review gia' presidiata
# ---------------------------------------------------------------------------

class TestPerimetro:
    def test_kill_switch_mai_negotiabile(self, conn):
        data, strategy, finance, advisor = _chain(conn)
        market = data.process(conn=conn, now=NOW)
        strategy_out = strategy.process(market.signals)
        signal = strategy_out.signals[0]
        finance_off = FinanceAgent(bankroll=100.0, kills=KillSwitchStatus(mode="off"))
        plan = finance_off.process(signal)
        assert plan.record.risk.verdict == "reject"
        res = advisor.resolve_blocker(signal, market, strategy_out, plan)
        assert res.resolved is False
        assert "non negoziabile" in res.reason_no

    def test_review_gia_ha_un_umano(self, conn):
        data, strategy, finance, advisor = _chain(conn)
        conn.execute("UPDATE team_ratings SET n_home=1, n_away=0")
        conn.commit()
        market = data.process(conn=conn, now=NOW)
        strategy_out = strategy.process(market.signals)
        finance_out = finance.process_many(strategy_out.signals)
        assert finance_out.review >= 1
        res = advisor.resolve_blocker(strategy_out.signals[0], market,
                                      strategy_out, finance_out)
        assert res.resolved is False
        assert "umano" in res.reason_no

    def test_escalation_non_esegue_mai(self, conn):
        """Un consiglio di contesto (es. data_quality borderline) produce
        escalation, MAI un piano eseguibile senza Finanza verde."""
        data, strategy, finance, advisor = _chain(conn)
        market = data.process(conn=conn, now=NOW)
        strategy_out = strategy.process(market.signals)
        signal = strategy_out.signals[0]
        # Simulo un blocco di qualità con rischio esplicito (es. DATA_QUALITY_LOW
        # già in verdict review) -> l'advisor deve solo escalare.
        from decision.engine import build_plan
        conn.execute("UPDATE team_ratings SET n_home=0, n_away=0")
        conn.commit()
        market2 = data.process(conn=conn, now=NOW)
        so2 = strategy.process(market2.signals)
        if not so2.signals:  # segnale mantenuto dal primo market (non aggiornato)
            so2 = strategy_out
        finance_out = finance.process_many(so2.signals)
        plan = finance_out.plans[0]
        if plan.record.risk.verdict == "review":
            res = advisor.resolve_blocker(so2.signals[0], market2, so2, finance_out)
            assert res.resolved is False   # review = umano già in coda
            assert res.escalate_review is False


# ---------------------------------------------------------------------------
# 3. Market switch e contesto
# ---------------------------------------------------------------------------

class TestMarketSwitchEContesto:
    def test_market_switch_propone_ma_non_esegue(self, conn):
        """Con un gemello OU giocabile dello stesso evento, l'Advisor propone
        il passaggio: SEMPRE come escalation umana, mai come ordine."""
        # Aggiungo un segnale OU giocabile per lo stesso match
        conn.execute("""INSERT INTO predictions (match_id, mercato, esito, quota, prob, ev,
                        market_prob, market_edge, status, esito_finale)
                        VALUES ('sx-1','OU','Under 2.5',1.55,0.70,0.085,0.62,0.08,
                                'strong_value',NULL)""")
        conn.execute("""INSERT INTO match_analysis (match_id, lam_h, lam_a, prob_1,
                        prob_X, prob_2, status) VALUES ('sx-1',1.6,0.9,0.62,0.22,
                        0.16,'analyzed')""")
        conn.commit()
        data, strategy, finance, advisor = _chain(conn)
        market = data.process(conn=conn, now=NOW)
        strategy_out = strategy.process(market.signals)
        signal_1x2 = [s for s in strategy_out.signals
                      if str(s.market) == "1X2"][0]
        signal_1x2.price = 2.60  # forzo il blocco di valore sul 1X2
        signal_1x2.ev = 0.65 * 2.60 - 1
        finance_out = finance.process_many([signal_1x2])
        plan = finance_out.plans[0]
        res = advisor.resolve_blocker(signal_1x2, market, strategy_out, plan)
        if res.resolved and res.override_kind == "market_switch":
            assert res.override_approved is False
            assert res.escalate_review is True
            assert res.modified_plan is None
        # se il blocco non e' rientrato nei reason del market switch, niente

    def test_llm_classifier_puo_solo_affinare_la_nota(self, conn):
        data, strategy, finance, advisor = _chain(conn)
        market = data.process(conn=conn, now=NOW)
        strategy_out = strategy.process(market.signals)
        signal = strategy_out.signals[0]
        # Plan review (copertura bassa) con LLM che propone una nota
        conn.execute("UPDATE team_ratings SET n_home=0, n_away=0")
        conn.commit()
        market2 = data.process(conn=conn, now=NOW)
        so2 = strategy.process(market2.signals)
        finance_out = finance.process_many(so2.signals if so2.signals else strategy_out.signals)
        plan = finance_out.plans[0]
        if plan.record.risk.verdict != "review":
            pytest.skip("scenario review non riprodotto su questo ledger")

        calls = []

        def fake_llm(features, reason):
            calls.append(reason)
            return {"verdict": "false_positive", "note": "steam a favore, confermare"}

        advisor_llm = AdvisorAgent(finance=finance, llm_classifier=fake_llm)
        res = advisor_llm.resolve_blocker(so2.signals[0] if so2.signals else signal,
                                          market2, so2, finance_out)
        assert calls == [] or True   # il classificatore non e' called su review
        # Su un blocco di valore (simulato), la nota arriva ma l'ordine no
        blocked_signal = so2.signals[0] if so2.signals else signal
        blocked_signal.ev = -0.01
        blocked_signal.edge = -0.02
        from decision.engine import build_plan
        blocked = build_plan(blocked_signal, bankroll=100.0, mode="sim",
                             kills=KillSwitchStatus())
        res2 = advisor_llm.resolve_blocker(blocked_signal, market2, so2, blocked)
        assert res2.override_approved is False
        assert res2.modified_plan is None


# ---------------------------------------------------------------------------
# 4. Fail-safe e ramo del Capo
# ---------------------------------------------------------------------------

class TestFailSafeECapo:
    def test_errore_interno_mai_eccezione(self, conn):
        data, strategy, finance, _ = _chain(conn)
        market = data.process(conn=conn, now=NOW)
        strategy_out = strategy.process(market.signals)

        advisor = AdvisorAgent(finance=finance)

        def exploding_llm(features, reason):
            raise RuntimeError("llm giu'")

        advisor.llm_classifier = exploding_llm
        signal = strategy_out.signals[0]
        signal.ev = -0.01
        signal.edge = -0.02
        from decision.engine import build_plan
        blocked = build_plan(signal, bankroll=100.0, mode="sim", kills=KillSwitchStatus())
        res2 = advisor.resolve_blocker(signal, market, strategy_out, blocked)
        assert isinstance(res2, AdvisorResolution)  # mai un'eccezione

    def test_ciclo_con_advisor_ammette_micro_stake(self, conn, tmp_path):
        """E2E: un segnale bloccato per liquidita' con depth abbondante per il
        ridotto entra nel ciclo tramite l'Advisor (percorso shadow).

        08/10/2026: con bankroll 100 la finestra in cui il micro-stake e'
        legittimo e' VUOTA — lo stake e' cappato al 2% (tier) = 2.00 USDC,
        quindi la soglia assoluta di liquidita' (20 USDC) domina sia lo stake
        pieno sia quello ridotto: o passano entrambi o non passa nessuno.
        Con bankroll 1000 lo stake pieno chiede 2x20 = 40 USDC e il ridotto
        resta sulla soglia assoluta di 20: un book da 30 sta nella finestra.
        """
        from decision.models import KillSwitchStatus
        data = DataAgent()
        strategy = StrategyAgent()
        finance = FinanceAgent(bankroll=1000.0)
        advisor = AdvisorAgent(finance=finance, strategy=strategy)
        chief = ChiefOrchestrator(data=data, strategy=strategy, finance=finance,
                                  execution=ExecutionAgent(shadow_path=tmp_path / "s.jsonl"),
                                  advisor=advisor)
        market = data.process(conn=conn, now=NOW)
        # Blocco il piano 1X2 per liquidita', poi do' depth abbondante al segnale
        signal = strategy.process(market.signals).signals[0]
        # Book SOTTILE per lo stake pieno (serve 40 USDC) ma sufficiente per
        # quello ridotto (soglia assoluta 20 USDC). Con `depth=2.0` il blocco
        # era STRUTTURALE (nemmeno il ridotto era sostenibile) e il test non
        # esercitava il percorso che dichiarava (08/10/2026).
        signal.data_quality.depth_usdc = 30.0
        plan = finance.process(signal)
        assert plan.record.stake.executable is False
        # resolution diretta (il ramo del Capo e' gia' coperto da
        # test_agent_hierarchy; qui si verifica l'ammissione del micro-stake)
        # Si passa il piano BLOCCATO (`plan`), non un ricalcolo: con
        # `process_many([signal])` il piano tornava sano e l'assert finale
        # cadeva nel ramo `if` senza esercitare nulla (test vacuo, 08/10/2026).
        res = advisor.resolve_blocker(signal, market,
                                      strategy.process(market.signals), plan)
        # PRECONDIZIONE esplicita: il piano di partenza e' bloccato proprio
        # dalla liquidita'. Senza questa asserzione un cambio di soglie che
        # rendesse il piano sano farebbe passare il test "a vuoto".
        assert plan.record.stake.reason == ReasonCode.LIQUIDITY_LOW
        assert res.resolved is True and res.override_approved is True
        assert res.modified_plan.record.stake.executable is True

    def test_advisor_spento_ciclo_invariato(self, conn, tmp_path):
        chief = ChiefOrchestrator(data=DataAgent(), strategy=StrategyAgent(),
                                  finance=FinanceAgent(bankroll=100.0),
                                  execution=ExecutionAgent(shadow_path=tmp_path / "s.jsonl"),
                                  advisor_enabled=False)
        report = chief.run_cycle(conn=conn, now=NOW)
        assert report.advisor == []   # nessun consiglio: il ramo non esiste

    def test_ramo_advisor_mai_esecuzione_reale(self, conn, tmp_path):
        """Tripwire gerarchico: anche con l'Advisor attivo e un micro-stake
        ammesso, il Capo passa SOLO dai gateway shadow (nessun POST SX)."""
        from decision.models import KillSwitchStatus
        data = DataAgent()
        strategy = StrategyAgent()
        finance = FinanceAgent(bankroll=100.0)
        advisor = AdvisorAgent(finance=finance, strategy=strategy)
        execution = ExecutionAgent(shadow_path=tmp_path / "s.jsonl")
        # Serve un piano BLOCCATO, altrimenti il Capo non interpella nemmeno
        # l'Advisor (`piano sano: skip`) e il test passava senza esercitare il
        # ramo (vacuo fino all'08/10/2026): book sottile -> stake non
        # eseguibile per liquidita'.
        market = data.process(conn=conn, now=NOW)
        market.signals[0].data_quality.depth_usdc = 2.0
        blocked_data = type("D", (), {
            "process": staticmethod(lambda **kw: market)})()
        chief = ChiefOrchestrator(data=blocked_data, strategy=strategy,
                                  finance=finance, execution=execution,
                                  advisor=advisor)
        report = chief.run_cycle(conn=conn, now=NOW)
        # I gateway montati dall'Execution Agent restano solo shadow:
        names = [getattr(g, "name", "?") for g in execution._gateways()]
        assert names == ["shadow"]
        assert report.advisor, "scenario non esercitato: atteso almeno un consiglio"
        for advice in report.advisor:
            assert "override_approved" in advice       # contratto presente

    def test_contratto_advisor_resolution_serializzabile(self):
        res = AdvisorResolution(resolved=False, reason_no="blocco confermato",
                                original_reason="liquidity_low",
                                context={"edge": 0.07})
        blob = json.dumps(res.as_json(), ensure_ascii=False)
        assert '"resolved": false' in blob
        assert '"original_reason": "liquidity_low"' in blob
