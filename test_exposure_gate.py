"""Test del RECINTO DI ESPOSIZIONE APERTA tra Execution Engine e Advisor
(direttiva 28/09/2026).

Tre famiglie:

1. **LETTURA**: lo stato degli ordini aperti ha una fonte sola (Execution
   Agent -> `auto_bet.exposure_allows`) e il tetto vero e' la PROIEZIONE
   (esposizione aperta + nuovo stake): con l'equity corrente e lo stake fisso
   entrano otto ordini, il nono sfora il 40% e viene respinto.
2. **ADVISOR**: il gate e' interrogato a ogni ciclo, respinge i piani al tetto,
   non si negozia col micro-stake ed e' fail-closed su lettura rotta — ma il
   kill switch resta la prima autorita' (l'esposizione non lo maschera).
3. **CICLO DEL CAPO**: i piani approvati vengono filtrati dal recinto e il
   riepilogo dichiara esposizione, tetto e numero di ordini aperti.

Perche' il rilascio e' DINAMICO e non giornaliero: la lettura filtra
`esito_finale IS NULL` (ordini in corso), non una data — chiuso un match
l'esposizione scende e il ciclo successivo riparte da solo, con il tetto
ricalcolato sul capitale aggiornato (compounding).

Zero rete, zero credito, zero Telegram: ledger SQLite temporaneo (il DB di
produzione non viene toccato: `import agents` resta leggero, la lettura e'
pigra) e gateway shadow.
"""

from __future__ import annotations

import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import auto_bet
import tracker
from agents.advisor_agent import AdvisorAgent
from agents.data_agent import DataAgent
from agents.execution_agent import ExecutionAgent
from agents.finance_agent import FinanceAgent
from agents.strategy_agent import StrategyAgent
from chief_orchestrator import ChiefOrchestrator
from decision.models import KillSwitchStatus

NOW = datetime.now(timezone.utc)
ALLOWED_LEAGUE = "Premier League"
EQUITY = 33.55                 # equity reale del wallet SX
CAP = round(EQUITY * 0.40, 2)  # tetto del 40%
FIXED = 1.50                   # stake fisso per ordine reale


# ---------------------------------------------------------------------------
# Fixtures: ledger del recinto (DB temporaneo) + ledger dei segnali (memoria)
# ---------------------------------------------------------------------------

@pytest.fixture()
def temp_db(monkeypatch):
    """`tracker` su un DB temporaneo: il recinto legge da qui, mai dal volume."""
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        monkeypatch.setattr(tracker, "DB_PATH", db_path)
        tracker.init_db()
        yield db_path


def _open_live(mid: str, stake: float = FIXED, esito: str = "1") -> None:
    """Ordine REALE aperto = capitale immobilizzato nel recinto."""
    tracker.save_bet(match_id=mid, mercato="1X2", esito=esito, market_id="0xm",
                     selection_id=1, price=1.65, stake=stake, mode="live",
                     status="FULLY_FILLED", bet_id="0xb")


def _settle(mid: str, home: str = "Home", away: str = "Away") -> None:
    """Chiusura dell'ordine (esito 1 vince coi gol 2-1): libera il capitale."""
    tracker.save_result(mid, ALLOWED_LEAGUE, home, away, 2, 1,
                        datetime.now(timezone.utc).isoformat())
    tracker.settle_bets()


# ---------------------------------------------------------------------------
# 1. LETTURA: un solo posto per il tetto, proiezione come verita'
# ---------------------------------------------------------------------------

class TestLetturaOrdiniAperti:
    def test_parita_con_il_tetto_di_auto_bet(self, temp_db):
        """L'Execution Agent non reimplementa nulla: delega e restituisce la
        STESSA risposta del tetto di produzione."""
        _open_live("m1")
        _open_live("m2")
        agent = ExecutionAgent()
        stato = agent.open_exposure(EQUITY)
        assert stato == auto_bet.exposure_allows(EQUITY, 0.0)
        assert stato["open_stake"] == 3.00 and stato["count"] == 2
        assert stato["cap"] == CAP and stato["allowed"] is True

    def test_otto_ordini_pieni_il_nono_sfora(self, temp_db):
        """Otto ordini da 1.50 (12.00 USDC) stanno sotto il tetto: il NONO
        ordine porterebbe a 13.50 > 13.42 e viene respinto dalla proiezione."""
        for i in range(8):
            _open_live(f"m{i}")
        agent = ExecutionAgent()
        dentro = agent.open_exposure(EQUITY, FIXED)
        assert dentro["open_stake"] == 12.00 and dentro["count"] == 8
        assert dentro["projected"] == 13.50
        assert dentro["allowed"] is False          # 13.50 > 13.42
        assert "13.50" in dentro["reason"]

    def test_solo_gli_ordini_reali_occupano_il_recinto(self, temp_db):
        """La cassa simulata non immobilizza capitale: il recinto resta libero."""
        tracker.save_bet(match_id="sim-1", mercato="1X2", esito="1",
                         price=1.65, stake=90.0, mode="sim", status="SUCCESS")
        stato = ExecutionAgent().open_exposure(EQUITY, FIXED)
        assert stato["open_stake"] == 0.0 and stato["count"] == 0
        assert stato["allowed"] is True

    def test_lettore_rotto_fail_closed(self):
        """Un lettore che esplode non apre il varco: nessun ordine autorizzato."""
        def _boom(bankroll, new_stake=0.0):
            raise RuntimeError("ledger giu'")

        stato = ExecutionAgent(exposure_reader=_boom).open_exposure(EQUITY, FIXED)
        assert stato["allowed"] is False and stato["blocked"] is True
        assert "fail-closed" in stato["reason"]

    def test_bankroll_non_positivo_non_autorizza(self, temp_db):
        _open_live("m1")
        stato = ExecutionAgent().open_exposure(0.0, FIXED)
        assert stato["allowed"] is False and stato["cap"] == 0.0


# ---------------------------------------------------------------------------
# 2. ADVISOR: il gate del 40%, interrogato a ogni ciclo
# ---------------------------------------------------------------------------

class TestGateAdvisor:
    def _advisor(self, **kw) -> AdvisorAgent:
        return AdvisorAgent(exposure_reader=kw.pop("exposure_reader", None), **kw)

    def test_sotto_il_tetto_passa(self, temp_db):
        for i in range(3):
            _open_live(f"m{i}")
        gate = self._advisor().exposure_gate(bankroll=EQUITY, stake=FIXED)
        assert gate.resolved is True
        assert gate.exposure["open_stake"] == 4.50
        assert gate.exposure["count"] == 3

    def test_otto_ordini_respingono_il_nuovo_piano(self, temp_db):
        """Otto partite attive (12.00 USDC) = nessun nuovo piano: il tetto del
        40% misura il capitale impegnato SIMULTANEAMENTE, non il giorno."""
        for i in range(8):
            _open_live(f"m{i}")
        gate = self._advisor().exposure_gate(bankroll=EQUITY, stake=FIXED)
        assert gate.resolved is False and gate.override_approved is False
        assert gate.original_reason == "exposure_cap"
        assert gate.exposure["projected"] == 13.50
        assert "tetto" in gate.reason_no

    def test_il_tetto_segue_il_capitale_aggiornato(self, temp_db):
        """Compounding: l'otto-ordini satura il tetto con l'equity piccola, ma
        con il capitale raddoppiato lo stesso stato e' ampiamente sotto."""
        for i in range(8):
            _open_live(f"m{i}")
        advisor = self._advisor()
        assert advisor.exposure_gate(bankroll=EQUITY, stake=FIXED).resolved is False
        raddoppiato = advisor.exposure_gate(bankroll=EQUITY * 2, stake=FIXED)
        assert raddoppiato.resolved is True
        assert raddoppiato.exposure["cap"] == round(EQUITY * 2 * 0.40, 2)

    def test_rilascio_dinamico_dopo_un_settlement(self, temp_db):
        """Chiuso un match l'esposizione scende: il ciclo successivo riparte
        senza interventi (nessuna finestra giornaliera da riarmare)."""
        for i in range(9):
            _open_live(f"m{i}")
        advisor = self._advisor()
        assert advisor.exposure_gate(bankroll=EQUITY, stake=FIXED).resolved is False
        _settle("m0", "Home0", "Away0")
        _settle("m1", "Home1", "Away1")
        dopo = advisor.exposure_gate(bankroll=EQUITY, stake=FIXED)
        assert dopo.exposure["open_stake"] == 10.50
        assert dopo.resolved is True               # 10.50 + 1.50 <= 13.42

    def test_lettore_rotto_fail_closed(self):
        def _boom(bankroll, new_stake=0.0):
            raise RuntimeError("no db")

        gate = self._advisor(exposure_reader=_boom).exposure_gate(
            bankroll=EQUITY, stake=FIXED)
        assert gate.resolved is False
        assert "fail-closed" in gate.reason_no

    def test_il_gate_non_si_negozia_con_il_micro_stake(self, conn, temp_db):
        """Un tetto di capitale respinto resta respinto: l'Advisor non prova
        nemmeno il micro-stake (un piano ridotto occuperebbe comunque il
        recinto) e non esiste piano sostitutivo."""
        for i in range(9):
            _open_live(f"open{i}")
        data, strategy, finance, advisor = _chain(conn, bankroll=EQUITY)
        market = data.process(conn=conn, now=NOW)
        strategy_out = strategy.process(market.signals)
        signal = strategy_out.signals[0]
        signal.data_quality.depth_usdc = 2.0        # blocco SIZE (LIQUIDITY_LOW)
        bloccato = finance.process_many([signal])
        assert bloccato.plans[0].record.stake.executable is False
        signal.data_quality.depth_usdc = 500.0      # il ridotto sarebbe eseguibile
        res = advisor.resolve_blocker(signal, market, strategy_out, bloccato)
        assert res.resolved is False and res.override_approved is False
        assert res.modified_plan is None
        assert res.original_reason == "exposure_cap"

    def test_kill_switch_resta_la_prima_autorita(self, conn, temp_db):
        """Con il recinto pieno il kill switch non viene mascherato: l'autorita'
        umana resta la prima risposta (l'ordine e' bloccato da entrambe)."""
        for i in range(9):
            _open_live(f"open{i}")
        data, strategy, _, advisor = _chain(conn, bankroll=EQUITY)
        market = data.process(conn=conn, now=NOW)
        strategy_out = strategy.process(market.signals)
        signal = strategy_out.signals[0]
        finance_off = FinanceAgent(bankroll=EQUITY, mode="live",
                                   kills=KillSwitchStatus(mode="off"))
        plan = finance_off.process(signal)
        assert plan.record.risk.verdict == "reject"
        res = advisor.resolve_blocker(signal, market, strategy_out, plan)
        assert res.resolved is False and "non negoziabile" in res.reason_no


# ---------------------------------------------------------------------------
# 3. CICLO DEL CAPO: il recinto filtra i piani approvati
# ---------------------------------------------------------------------------

class TestGateNelCiclo:
    def _chief(self, conn, tmp_path) -> ChiefOrchestrator:
        return ChiefOrchestrator(
            data=DataAgent(), strategy=StrategyAgent(),
            finance=FinanceAgent(bankroll=100.0),
            execution=ExecutionAgent(shadow_path=tmp_path / "shadow.jsonl"),
        )

    def test_sotto_il_tetto_i_piani_passano(self, conn, tmp_path, temp_db):
        report = self._chief(conn, tmp_path).run_cycle(conn=conn, now=NOW)
        assert report.exposure["open_stake"] == 0.0
        assert report.exposure["allowed"] is True
        assert report.execution["dispatched"] >= 1
        assert report.advisor == []          # nessun blocco da segnalare

    def test_al_tetto_i_piani_sono_respinti(self, conn, tmp_path, temp_db):
        for i in range(27):                  # 40.50 USDC >= 40% di 100
            _open_live(f"open{i}")
        report = self._chief(conn, tmp_path).run_cycle(conn=conn, now=NOW)
        assert report.exposure["blocked"] is True
        assert report.finance["approved"] >= 1        # la Strategia propone...
        assert report.execution["dispatched"] == 0    # ...il recinto respinge
        assert any(a.get("original_reason") == "exposure_cap"
                   for a in report.advisor)

    def test_report_serializzabile_con_esposizione(self, conn, tmp_path, temp_db):
        report = self._chief(conn, tmp_path).run_cycle(conn=conn, now=NOW)
        blob = report.as_json()
        assert "exposure" in blob and "advisor" in blob
        assert blob["exposure"]["cap"] == 40.0


# ---------------------------------------------------------------------------
# 4. TRIPWIRE: nessuna soglia duplicata, nessun denaro negli agenti
# ---------------------------------------------------------------------------

class TestTripwire:
    AGENTI = ("agents/advisor_agent.py", "agents/execution_agent.py")

    def test_soglie_non_duplicate(self):
        """Il tetto del 40% e lo stake fisso vivono in `auto_bet`: gli agenti
        delegano. Una seconda copia divergerebbe in silenzio."""
        for path in self.AGENTI + ("chief_orchestrator.py", "agents/contracts.py"):
            src = Path(path).read_text(encoding="utf-8")
            assert "OPEN_EXPOSURE_CAP_PCT" not in src, path
            assert "0.40" not in src, path
            assert "cap_order_stake" not in src, path

    def test_l_execution_agent_delega_il_tetto(self):
        src = Path("agents/execution_agent.py").read_text(encoding="utf-8")
        assert "auto_bet.exposure_allows" in src      # delega esplicita
        assert "PlaceOrderGateway(" not in src        # nessun ordine reale

    def test_l_advisor_interroga_il_lettore_di_produzione(self):
        src = Path("agents/advisor_agent.py").read_text(encoding="utf-8")
        assert "default_exposure_reader" in src
        assert "1.50" not in src             # lo stake fisso non si copia

    def test_env_dichiarate_in_iac(self):
        """`railway config apply` distrugge cio' che non e' in `preserve()`."""
        iac = Path(".railway/railway.ts").read_text(encoding="utf-8")
        for env in ("ORDER_FIXED_STAKE_USDC", "OPEN_EXPOSURE_CAP_PCT",
                    "ORDER_MAX_STAKE_USDC"):
            assert f"{env}: preserve()," in iac, env


# ---------------------------------------------------------------------------
# helpers: catena agenti + ledger dei segnali (schema di produzione)
# ---------------------------------------------------------------------------

def _ledger(*, n_inter=12, n_cagliari=10, quota=1.60,
            league=ALLOWED_LEAGUE, status="strong_value"):
    """Ledger temporaneo con un segnale giocabile (come test_agent_hierarchy)."""
    c = sqlite3.connect(":memory:")
    c.execute("""CREATE TABLE matches (id TEXT PRIMARY KEY, league TEXT, home_team TEXT,
                 away_team TEXT, commence_time TEXT, status TEXT, last_updated TEXT)""")
    c.execute("""CREATE TABLE predictions (id INTEGER PRIMARY KEY AUTOINCREMENT, match_id TEXT,
                 mercato TEXT, esito TEXT, quota REAL, prob REAL, ev REAL, market_prob REAL,
                 market_edge REAL, status TEXT, esito_finale TEXT, profit REAL,
                 created_at TEXT, settled_at TEXT, league TEXT)""")
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
                 market_prob, market_edge, status, esito_finale, league)
                 VALUES ('sx-1','1X2','1',?,0.65,0.04,0.58,0.07,?,NULL,?)""",
              (quota, status, league))
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
