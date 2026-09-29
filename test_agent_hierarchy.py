"""Test della gerarchia a 4 agenti (Fase 1, shadow).

Tre famiglie:
1. PARITA': ogni wrapper restituisce ESATTAMENTE cio' che restituisce il
   modulo delegato (stessi input -> stessi valori). Un wrapper che copia
   logica invece di delegare diverge: e' il bug-classe del progetto
   (stop-loss fantasma 21/09, doppio Kelly 13/09).
2. TRIPWIRE: nessun agente esegue il denaro (nessun gateway reale montato,
   nessun import di execution_engine/auto_bet), `import agents` resta
   leggero, i contratti sono serializzabili.
3. E2E OFFLINE: il ciclo completo del Capo su un ledger temporaneo con lo
   schema di produzione (segnale approvato / review per copertura / reject
   per fascia quota; blocco kill switch; gate mercato).

Zero rete, zero crediti, zero Telegram: `conftest.py` isola i sink e spegne
il feed; le sorgenti e i provider sono finti.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import chief_orchestrator
from agents.contracts import (CycleReport, ExecutionOutput, FinanceOutput,
                              MarketData, StrategyOutput)
from agents.data_agent import DataAgent
from agents.execution_agent import ExecutionAgent
from agents.finance_agent import FinanceAgent
from agents.strategy_agent import StrategyAgent
from chief_orchestrator import ChiefOrchestrator

ALLOWED_LEAGUE = "Premier League"
NOW = datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Ledger temporaneo con lo schema di produzione (come test_decision_adapters)
# ---------------------------------------------------------------------------

@pytest.fixture
def conn():
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
    c.execute("""INSERT INTO team_ratings VALUES ('Inter',1.2,0.9,1.1,1.0,12,10),
                 ('Cagliari',0.9,1.1,0.8,1.2,10,12)""")
    kickoff = (NOW + timedelta(hours=5)).isoformat()
    c.execute("INSERT INTO matches VALUES ('sx-1',?, 'Inter','Cagliari',?,'scheduled','')",
              (ALLOWED_LEAGUE, kickoff))
    c.execute("""INSERT INTO predictions (match_id, mercato, esito, quota, prob, ev,
                 market_prob, market_edge, status, esito_finale)
                 VALUES ('sx-1','1X2','1',1.60,0.65,0.04,0.58,0.07,'strong_value',NULL)""")
    c.execute("""INSERT INTO match_analysis (match_id, lam_h, lam_a, prob_1, prob_X,
                 prob_2, status) VALUES ('sx-1',1.6,0.9,0.62,0.22,0.16,'analyzed')""")
    c.commit()
    yield c
    c.close()


# ---------------------------------------------------------------------------
# 1. PARITA' wrapper <-> modulo delegato
# ---------------------------------------------------------------------------

class TestParitaDataAgent:
    def test_stessi_segnali_di_iter_signals(self, conn):
        from decision.adapters import iter_signals
        expected = iter_signals(conn=conn, hours=24.0)
        got = DataAgent().process(conn=conn, now=NOW).signals
        assert [s.signal_id for s in got] == [s.signal_id for s in expected]
        assert [s.price for s in got] == [s.price for s in expected]
        assert [s.tier for s in got] == [s.tier for s in expected]

    def test_gate_senza_feed_con_env_spenta_passa(self, conn, monkeypatch):
        # conftest: DECISION_FEED_ENABLED=0 -> verify_feed(required=False) passa
        got = DataAgent().process(conn=conn, now=NOW)
        assert got.validated is True


class TestParitaStrategyAgent:
    def test_stesso_filtro_di_playable_tiers(self, conn):
        from value_filter import PLAYABLE_TIERS
        signals = DataAgent().process(conn=conn, now=NOW).signals
        out = StrategyAgent().process(signals)
        expected = [s for s in signals if s.tier in PLAYABLE_TIERS]
        assert out.playable == len(expected)
        assert [s.signal_id for s in out.signals] == [s.signal_id for s in expected]

    def test_stato_ignoto_mai_sparito(self):
        # La query del ledger (QUERY_OPEN_SIGNALS) filtra GIA' in SQL i tier
        # giocabili: difesa alla fonte. Qui si verifica il SECONDO strato:
        # con una lista di tier iniettabile piu' stretta, un tier fuori lista
        # finisce in `unclassified` (contato, mai sparito dal ciclo).
        signal = DataAgent().process(conn=_seed_conn_unknown(), now=NOW).signals[0]
        assert signal.tier == "strong_value"  # l'adapter lo porta come giocabile
        out = StrategyAgent(playable_tiers=["value"]).process([signal])
        assert out.playable == 0 and out.unclassified == 1


def _seed_conn_unknown() -> sqlite3.Connection:
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
    kickoff = (NOW + timedelta(hours=5)).isoformat()
    c.execute("INSERT INTO matches VALUES ('sx-9','Serie B','Foo','Bar',?,'scheduled','')",
              (kickoff,))
    # Stato GIUCABILE nel ledger (la query SQL filtra gia' i tier): il test
    # verifica il secondo strato con una lista tier iniettabile piu' stretta.
    c.execute("""INSERT INTO predictions (match_id, mercato, esito, quota, prob, ev,
                 market_prob, market_edge, status, esito_finale)
                 VALUES ('sx-9','1X2','1',1.60,0.65,0.04,0.58,0.07,'strong_value',NULL)""")
    c.execute("""INSERT INTO match_analysis (match_id, lam_h, lam_a, prob_1, prob_X,
                 prob_2, status) VALUES ('sx-9',1.6,0.9,0.62,0.22,0.16,'analyzed')""")
    c.commit()
    return c


class TestParitaFinanceAgent:
    def test_stesso_piano_di_build_plan(self, conn):
        from decision.engine import build_plan
        signal = DataAgent().process(conn=conn, now=NOW).signals[0]
        agent = FinanceAgent(bankroll=100.0, mode="sim")
        got = agent.process(signal)
        expected = build_plan(signal, bankroll=100.0, mode="sim")
        assert got.record.risk.verdict == expected.record.risk.verdict
        assert got.record.risk.reason == expected.record.risk.reason
        assert (got.record.stake.executable if got.record.stake else None) == \
               (expected.record.stake.executable if expected.record.stake else None)
        assert got.kinds() == expected.kinds()

    def test_process_many_conta_i_verdetti(self, conn):
        signals = DataAgent().process(conn=conn, now=NOW).signals
        out = FinanceAgent(bankroll=100.0).process_many(signals)
        assert out.approved + out.review + out.rejected == len(out.plans) == len(signals)


# ---------------------------------------------------------------------------
# 1b. MODELLO: lo StrategyAgent DELEGA a poisson_engine (zero formule copiate)
# ---------------------------------------------------------------------------

class TestDelegaModello:
    """Il modello vive in `poisson_engine`: qui si verifica la PARITA'.

    Un agente che ricalcola il Poisson per conto suo sarebbe una seconda
    formula: il giorno che divergono non si saprebbe quale ha deciso la
    puntata (lezione 13/09, costo reale in produzione).
    """

    def test_expected_goals_delegato(self):
        import poisson_engine
        agent = StrategyAgent()
        for home, away in (("Inter", "Milan"), ("Arsenal", "Chelsea")):
            assert agent.expected_goals(home, away) == \
                poisson_engine.expected_goals(home, away)

    def test_1x2_parita_col_motore(self):
        import poisson_engine
        agent = StrategyAgent()
        lam_h, lam_a = poisson_engine.expected_goals("Inter", "Milan")
        want = poisson_engine.prob_1x2(lam_h, lam_a)
        got = agent.model_probabilities("1X2", "Inter", "Milan")
        assert (got["p1"], got["pX"], got["p2"]) == pytest.approx(want)
        assert sum(got.values()) == pytest.approx(1.0)

    @pytest.mark.parametrize("line", [1.5, 2.0, 2.5, 2.25, 3.5])
    def test_ou_parita_col_motore_incluso_il_push(self, line):
        import poisson_engine
        agent = StrategyAgent()
        lam_h, lam_a = poisson_engine.expected_goals("Inter", "Milan")
        for side in ("over", "under"):
            want = poisson_engine.ou_outcome_probs(lam_h, lam_a, line, side)
            got = agent.model_probabilities("OU", "Inter", "Milan",
                                            line=line, side=side)
            assert (got["p_win"], got["p_push"], got["p_lose"]) == \
                pytest.approx(want)

    @pytest.mark.parametrize("line,side", [(-0.75, "home"), (0.5, "away"),
                                            (-1.25, "home"), (2.0, "away")])
    def test_ah_parita_col_motore(self, line, side):
        import poisson_engine
        agent = StrategyAgent()
        lam_h, lam_a = poisson_engine.expected_goals("Inter", "Milan")
        want = poisson_engine.ah_outcome_probs(lam_h, lam_a, line, side)
        got = agent.model_probabilities("AH", "Inter", "Milan",
                                        line=line, side=side)
        assert (got["p_win"], got["p_push"], got["p_lose"]) == \
            pytest.approx(want)

    def test_btts_parita_col_motore(self):
        import poisson_engine
        agent = StrategyAgent()
        lam_h, lam_a = poisson_engine.expected_goals("Inter", "Milan")
        got = agent.model_probabilities("BTTS", "Inter", "Milan")
        assert got["p_yes"] == pytest.approx(poisson_engine.prob_btts(lam_h, lam_a))
        assert got["p_no"] == pytest.approx(1.0 - got["p_yes"])

    def test_mercato_senza_modello_e_errore_dichiarato(self):
        """Fail-closed: mai una probabilita' inventata per un mercato ignoto."""
        with pytest.raises(ValueError, match="senza modello"):
            StrategyAgent().model_probabilities("CS", "Inter", "Milan")
        with pytest.raises(ValueError):
            StrategyAgent().model_probabilities("", "Inter", "Milan")

    @pytest.mark.parametrize("market", ["OU", "AH"])
    def test_mercato_con_linea_senza_linea_e_errore(self, market):
        with pytest.raises(ValueError, match="senza linea"):
            StrategyAgent().model_probabilities(market, "Inter", "Milan")

    def test_lo_strategy_agent_non_importa_numpy_o_scipy(self):
        """La vettorizzazione vive nel MOTORE: l'agente non la reimplementa."""
        import agents.strategy_agent as sa
        src = open(sa.__file__, encoding="utf-8").read()
        for banned in ("import numpy", "import scipy", "from scipy",
                       "stats.poisson", "math.", "def prob_1x2"):
            assert banned not in src, banned

    def test_delega_al_motore_e_non_a_una_copia(self, monkeypatch):
        """Se il motore cambia, l'agente cambia con lui (nessuna copia)."""
        import poisson_engine
        monkeypatch.setattr(poisson_engine, "prob_1x2",
                            lambda lh, la, mg=10: (0.5, 0.3, 0.2))
        got = StrategyAgent().model_probabilities("1X2", "Inter", "Milan")
        assert (got["p1"], got["pX"], got["p2"]) == (0.5, 0.3, 0.2)


# ---------------------------------------------------------------------------
# 2. TRIPWIRE: niente denaro, import leggeri, contratti serializzabili
# ---------------------------------------------------------------------------

class TestTripwire:
    def test_nessun_gateway_reale_negli_agenti(self):
        import agents.data_agent, agents.execution_agent
        import agents.finance_agent, agents.strategy_agent
        for mod in (agents.data_agent, agents.strategy_agent,
                    agents.finance_agent, agents.execution_agent):
            src = open(mod.__file__, encoding="utf-8").read()
            assert "PlaceOrderGateway(" not in src, mod.__name__
            assert "execution_engine" not in src, mod.__name__

    def test_execution_agent_default_solo_shadow(self, tmp_path):
        agent = ExecutionAgent(shadow_path=tmp_path / "shadow.jsonl")
        names = [getattr(g, "name", "?") for g in agent._gateways()]
        assert names == ["shadow"]

    def test_chief_non_importa_moduli_di_produzione_a_livello_modulo(self):
        src = open(chief_orchestrator.__file__, encoding="utf-8").read()
        for forbidden in ("import tracker", "import auto_bet", "import bot",
                          "from tracker", "from auto_bet", "_live_fill"):
            assert forbidden not in src, forbidden

    def test_contratti_serializzabili(self, conn):
        market = DataAgent().process(conn=conn, now=NOW)
        strategy = StrategyAgent().process(market.signals)
        finance = FinanceAgent(bankroll=100.0).process_many(strategy.signals)
        execution = ExecutionAgent(shadow_path=None).process(
            [p for p in finance.plans if p.record.risk.verdict == "approve"])
        cycle = CycleReport(market=market.as_json(), strategy=strategy.as_json(),
                            finance=finance.as_json(), execution=execution.as_json())
        blob = json.dumps(cycle.as_json(), ensure_ascii=False)  # non solleva
        assert '"ok"' in blob


# ---------------------------------------------------------------------------
# 3. E2E OFFLINE del ciclo completo
# ---------------------------------------------------------------------------

class TestCicloCompleto:
    def _chief(self, conn, tmp_path, **finance_kwargs):
        return ChiefOrchestrator(
            data=DataAgent(),
            strategy=StrategyAgent(),
            finance=FinanceAgent(bankroll=100.0, **finance_kwargs),
            execution=ExecutionAgent(shadow_path=tmp_path / "shadow.jsonl"),
        )

    def test_segnale_approvato_arriva_alla_shadow(self, conn, tmp_path):
        report = self._chief(conn, tmp_path).run_cycle(conn=conn, now=NOW)
        assert report.ok and report.blocked_reason == ""
        assert report.finance["approved"] >= 1
        assert report.execution["dispatched"] >= 1
        assert report.execution["shadow"] is True

    def test_review_per_copertura_bassa(self, conn, tmp_path):
        # Copertura sotto DECISION_MIN_MODEL_COVERAGE (0.5): n_home+n_away=1
        # su ENTRAMBE le squadre -> model_coverage ~0.06 -> review umana.
        conn.execute("UPDATE team_ratings SET n_home=1, n_away=0")
        conn.commit()
        report = self._chief(conn, tmp_path).run_cycle(conn=conn, now=NOW)
        assert report.finance["review"] >= 1
        assert report.execution["dispatched"] == 0  # nessun ordine senza approve

    def test_reject_per_fascia_quota_non_arriva_alla_shadow(self, conn, tmp_path):
        conn.execute("UPDATE predictions SET quota=2.10 WHERE match_id='sx-1'")
        conn.commit()
        report = self._chief(conn, tmp_path).run_cycle(conn=conn, now=NOW)
        assert report.finance["rejected"] >= 1
        assert report.execution["dispatched"] == 0

    def test_blocco_kill_switch_ferma_il_ciclo(self, conn, tmp_path):
        # Lo stato KS e' INIETTABILE (le sonde reali leggono i file sul volume:
        # qui lo simuliamo spento). Il fail-fast rende il blocco una DECISIONE
        # (un piano reject per segnale), non un'eccezione: il ciclo si chiude.
        from decision.models import KillSwitchStatus
        chief = self._chief(conn, tmp_path)
        chief.finance = FinanceAgent(bankroll=100.0, kills=KillSwitchStatus(mode="off"))
        report = chief.run_cycle(conn=conn, now=NOW)
        assert report.finance.get("plans", 0) >= 1
        assert report.finance.get("approved", 0) == 0
        assert report.execution.get("dispatched", 0) == 0

    def test_gate_mercato_bloccato_termina_il_ciclo(self, tmp_path):
        from decision.feeds import FeedGateResult, ReasonCode
        gate = FeedGateResult(allowed=False, reason=ReasonCode.FEED_UNAVAILABLE,
                              detail="sorgenti giu'")
        blocked = MarketData(snapshot=None, gate=gate, signals=[])

        class DataDown:
            def process(self, **kwargs):
                return blocked

        report = ChiefOrchestrator(data=DataDown(),
                                   strategy=StrategyAgent(),
                                   finance=FinanceAgent(bankroll=100.0),
                                   execution=ExecutionAgent()).run_cycle()
        assert report.ok is False
        assert "feed_unavailable" in report.blocked_reason.lower()
        assert report.finance == {}

    def test_errore_imprevisto_fail_safe(self, tmp_path):
        class DataBoom:
            def process(self, **kwargs):
                raise RuntimeError("boom")

        report = ChiefOrchestrator(data=DataBoom(),
                                   strategy=StrategyAgent(),
                                   finance=FinanceAgent(),
                                   execution=ExecutionAgent()).run_cycle()
        assert report.ok is False and "orchestrator_error" in report.blocked_reason


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class TestCli:
    def test_main_json_non_scrive_sul_ledger(self, tmp_path, capsys):
        rc = chief_orchestrator.main(["--json"])
        assert rc in (0, 1)
        out = json.loads(capsys.readouterr().out)
        assert {"ok", "market", "strategy", "finance", "execution"} <= set(out)
