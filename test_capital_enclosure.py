"""Test del RECINTO DI CAPITALE (direttiva 27/09/2026).

Tre guardie, tutte con i valori REALI di produzione (il conftest le
disattiva di proposito per gli altri test — vedi la nota in conftest.py):

1. **Esposizione aperta**: tetto del 40% del bankroll sugli stake REALI non
   ancora saldate. Raggiunta la soglia il giro degrada a shadow: nessun
   nuovo ordine, ma valutazione e telemetria continuano.
2. **Micro-stake**: tetto assoluto di 1.50 USDC per singolo ordine, così
   l'esposizione si spalma su più partite invece di concentrarsi.
3. **Corsiа Chief**: `CHIEF_EXECUTION=live` fa entrare i piani approvati
   dalla catena piramidale nella STESSA coda di esecuzione della corsia
   storica — non un canale di denaro parallelo (tripwire sul sorgente).

L'esposizione APERTA è complementare al cap di portafoglio giornaliero
(`TOTAL_EXPOSURE_CAP_PCT`, che misura i flussi del giorno): due numeri
diversi, entrambi necessari.
"""
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import tracker
import auto_bet


@pytest.fixture()
def temp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        monkeypatch.setattr(tracker, "DB_PATH", db_path)
        tracker.init_db()
        yield db_path


@pytest.fixture(autouse=True)
def _soglie_reali(monkeypatch, tmp_path):
    """Valori di PRODUZIONE: il conftest li spegne, qui li riaccendiamo."""
    monkeypatch.setattr(auto_bet, "ORDER_MAX_STAKE_USDC", 1.50)
    monkeypatch.setattr(auto_bet, "OPEN_EXPOSURE_CAP_PCT", 0.40)
    monkeypatch.setattr(auto_bet, "T60_EXECUTION_ONLY", False)
    monkeypatch.setattr(auto_bet, "DAILY_STOP_FILE", tmp_path / "daily_stop.json")
    monkeypatch.setattr(auto_bet, "T60_KILL_WALLET_USDC", 0.0)
    monkeypatch.setattr(auto_bet, "T60_KILL_FILE", tmp_path / "t60_kill.json")
    monkeypatch.setattr(auto_bet, "WEEKLY_STOP_FILE", tmp_path / "weekly_stop.json")
    monkeypatch.setattr(auto_bet, "BANKROLL_HISTORY_FILE", tmp_path / "bh.json")
    monkeypatch.setattr(auto_bet, "_top_down_load",
                        lambda home, away: {"1": 0.65, "X": 0.65, "2": 0.65,
                                            "overround": 0.0})


def _open_bet(mid="sx-1", esito="1", stake=1.0):
    """Puntata REALE non ancora saldata = capitale immobilizzato."""
    tracker.save_bet(match_id=mid, mercato="1X2", esito=esito, market_id="0xm",
                     selection_id=1, price=1.65, stake=stake, mode="live",
                     status="FULLY_FILLED", bet_id="0xb")


# ---------------------------------------------------------------------------
# 1. ESPOSIZIONE APERTA
# ---------------------------------------------------------------------------

class TestEsposizioneAperta:
    def test_sotto_soglia_non_blocca(self, temp_db):
        _open_bet(stake=1.0)
        st = auto_bet.open_exposure_status(100.0)   # tetto 40
        assert st["open_stake"] == 1.0 and st["cap"] == 40.0
        assert st["blocked"] is False and st["reason"] == ""

    def test_alla_soglia_blocca(self, temp_db):
        _open_bet(stake=40.0)
        st = auto_bet.open_exposure_status(100.0)
        assert st["blocked"] is True
        assert "esposizione aperta" in st["reason"]

    def test_oltre_soglia_blocca(self, temp_db):
        _open_bet(stake=55.0)
        assert auto_bet.open_exposure_status(100.0)["blocked"] is True

    def test_cassa_simulata_non_occupa_il_recinto(self, temp_db):
        """Le puntate SIM non immobilizzano capitale reale."""
        tracker.save_bet(match_id="sim-1", mercato="1X2", esito="1",
                         price=1.65, stake=90.0, mode="sim", status="SUCCESS")
        st = auto_bet.open_exposure_status(100.0)
        assert st["open_stake"] == 0.0 and st["blocked"] is False

    def test_puntata_saldata_libera_il_capitale(self, temp_db):
        _open_bet(stake=50.0)
        assert auto_bet.open_exposure_status(100.0)["blocked"] is True
        # Firma reale di tracker.save_result: (match_id, league, home, away,
        # sh, sa, settled_at). Il 2-1 fa vincere la casa, cioe' l'esito "1"
        # della puntata: la riga si chiude e il capitale torna libero.
        tracker.save_result("sx-1", "Premier League", "Osasuna", "Getafe",
                            2, 1, datetime.now(timezone.utc).isoformat())
        tracker.settle_bets()
        st = auto_bet.open_exposure_status(100.0)
        assert st["open_stake"] == 0.0 and st["blocked"] is False

    def test_lettura_fallita_e_fail_closed(self, monkeypatch):
        """Se l'esposizione non e' leggibile il recinto non si apre: meglio
        un blocco dichiarato che un varco spalancato per errore di lettura."""
        monkeypatch.setattr(tracker, "_get_conn",
                            lambda: (_ for _ in ()).throw(RuntimeError("no db")))
        st = auto_bet.open_exposure_status(100.0)
        assert st["open_stake"] == float("inf") and st["blocked"] is True

    def test_scenario_reale_del_proprietario(self, temp_db):
        """33.55 USDC di equity, tetto 40% = 13.42: 8 micro-stake da 1.50
        (12.00) stanno sotto, il 9° porta a 13.50 e satura il recinto."""
        cap = 33.55 * 0.40
        assert round(cap, 2) == 13.42
        for i in range(8):
            _open_bet(mid=f"m{i}", esito="1", stake=1.50)
        st = auto_bet.open_exposure_status(33.55)
        assert st["open_stake"] == 12.00 and st["blocked"] is False
        _open_bet(mid="m8", esito="1", stake=1.50)
        st = auto_bet.open_exposure_status(33.55)
        assert st["open_stake"] == 13.50 and st["blocked"] is True


# ---------------------------------------------------------------------------
# 2. MICRO-STAKE (tetto per singolo ordine)
# ---------------------------------------------------------------------------

class TestMicroStake:
    def test_riduce_ma_non_alza(self):
        assert auto_bet.cap_order_stake(5.0) == 1.50
        assert auto_bet.cap_order_stake(1.50) == 1.50
        assert auto_bet.cap_order_stake(0.80) == 0.80    # non alza

    def test_tetto_inviolabile(self, monkeypatch):
        """Il tetto di 1.50 USDC è una regola di business inviolabile: non
        può essere disattivato mettendo l'ENV a 0.0, l'assertion attende sempre
        un output massimo di 1.50 anche quando l'ENV tenta di disattivarlo."""
        monkeypatch.setattr(auto_bet, "ORDER_MAX_STAKE_USDC", 0.0)
        assert auto_bet.cap_order_stake(5.0) == 1.50

    def test_tetto_applicato_in_fase_1(self, monkeypatch, temp_db):
        """Stake 5.0 in LIVE -> l'ordine effettivo e' 1.50."""
        from test_auto_bet_live import _fixed_stake, _filled, _stub_wallet
        _fixed_stake(monkeypatch)
        _stub_wallet(monkeypatch, 33.55)
        seen = {}

        def _fill(pick, stake, floor):
            seen["stake"] = stake
            out = _filled()
            out["stake"] = stake
            return out

        _seed_one(monkeypatch)
        monkeypatch.setattr(auto_bet, "_execution_mode",
                            lambda allow_sim=True: "live")
        monkeypatch.setattr(auto_bet, "_live_fill", _fill)
        placed = auto_bet.run_today_bets(stake_eur=5.0)
        assert len(placed) == 1
        assert seen["stake"] == 1.50
        assert placed[0]["stake"] == 1.50

    def test_sim_non_e_toccati_dal_tetto_reale(self, monkeypatch, temp_db):
        """La cassa simulata alimenta ML/CLV: il tetto del denaro reale non
        deve alterarne la serie storica."""
        from test_auto_bet_live import _fixed_stake
        _fixed_stake(monkeypatch)
        _seed_one(monkeypatch)
        placed = auto_bet.run_today_bets(stake_eur=5.0)
        assert len(placed) == 1 and placed[0]["mode"] == "sim"
        assert placed[0]["stake"] == 5.0


# ---------------------------------------------------------------------------
# 3. IL RECINTO BLOCCA IL GIRO REALE (degradazione a shadow)
# ---------------------------------------------------------------------------

class TestRecintoNelGiroLive:
    def test_esposizione_piena_nessun_ordine_reale(self, monkeypatch, temp_db):
        from test_auto_bet_live import _fixed_stake, _filled, _stub_wallet
        _fixed_stake(monkeypatch)
        # `_stub_wallet` riceve il DISPONIBILE (l'equity e' disponibile +
        # in gioco): per avere un'equity di 33.55 con 13.42 gia' in escrow il
        # libero e' 33.55 - 13.42. L'enclosure misura l'EQUITY, come Kelly e
        # stop-loss: il tetto 40% = 13.42 e 9 micro-stake da 1.50 (13.50) lo
        # saturano.
        _stub_wallet(monkeypatch, 33.55 - 13.42, exposure=13.42)
        for i in range(9):
            _open_bet(mid=f"old{i}", esito="1", stake=1.50)
        _seed_one(monkeypatch)
        monkeypatch.setattr(auto_bet, "_execution_mode",
                            lambda allow_sim=True: "live")
        called = []
        monkeypatch.setattr(auto_bet, "_live_fill",
                            lambda p, s, f: called.append(p) or _filled())

        placed = auto_bet.run_today_bets(stake_eur=5.0)
        assert placed == []              # nessun ordine REALE
        assert called == []              # _live_fill non e' mai stato chiamato

    def test_esposizione_sotto_soglia_lordine_confermato(self, monkeypatch,
                                                         temp_db):
        from test_auto_bet_live import _fixed_stake, _filled, _stub_wallet
        _fixed_stake(monkeypatch)
        _stub_wallet(monkeypatch, 33.55, exposure=0.0)
        _seed_one(monkeypatch)
        monkeypatch.setattr(auto_bet, "_execution_mode",
                            lambda allow_sim=True: "live")
        monkeypatch.setattr(auto_bet, "_live_fill",
                            lambda p, s, f: dict(_filled(), stake=s))
        placed = auto_bet.run_today_bets(stake_eur=5.0)
        assert len(placed) == 1 and placed[0]["mode"] == "live"


# ---------------------------------------------------------------------------
# 4. MODALITA' DELLA CORSIA CHIEF
# ---------------------------------------------------------------------------

class TestChiefExecution:
    def test_default_off(self, monkeypatch):
        monkeypatch.delenv("CHIEF_EXECUTION", raising=False)
        assert auto_bet.chief_execution_enabled() is False

    def test_live_accende(self, monkeypatch):
        monkeypatch.setenv("CHIEF_EXECUTION", "live")
        assert auto_bet.chief_execution_enabled() is True

    def test_valori_spuri_non_accendono_mai(self, monkeypatch):
        for raw in ("", "  ", "shadow", "off", "true", "1", "yes", "livex"):
            monkeypatch.setenv("CHIEF_EXECUTION", raw)
            assert auto_bet.chief_execution_enabled() is False, raw

    def test_normalizzazione_case_e_spazi(self, monkeypatch):
        """Maiuscole e spazi non possono creare un interruttore fantasma:
        sono la stessa identica direttiva, scritta diversamente."""
        for raw in ("live", "LIVE", " Live ", "  live  "):
            monkeypatch.setenv("CHIEF_EXECUTION", raw)
            assert auto_bet.chief_execution_enabled() is True, raw

    def test_lettura_a_ogni_giro_non_a_import(self, monkeypatch):
        """Cambiare la variabile si applica al giro successivo: niente
        redeploy, niente stato congelato in un modulo importato all'avvio."""
        monkeypatch.setenv("CHIEF_EXECUTION", "off")
        assert auto_bet.chief_execution_enabled() is False
        monkeypatch.setenv("CHIEF_EXECUTION", "live")
        assert auto_bet.chief_execution_enabled() is True

    def test_off_non_tocca_il_ledger(self, monkeypatch, temp_db):
        _seed_one(monkeypatch)
        assert auto_bet._chief_live_candidates(bankroll=100.0) == []

    def test_nessun_ordine_fuori_dal_canale_unico(self):
        """TRIPWIRE: la corsia Chief non può avere un proprio canale di
        esecuzione. Gli agenti non montano `PlaceOrderGateway` (la Fase 1 lo
        vietava) e i candidati chief finiscono nella coda di `run_today_bets`,
        che esegue via `_live_fill` — l'unico punto che parla con l'exchange.
        """
        src = Path(auto_bet.__file__).read_text(encoding="utf-8")
        body = src.split("def _chief_live_candidates", 1)[1]
        body = body.split("\ndef ", 1)[0]
        assert "_live_fill(" not in body      # delega, non esegue
        assert "execution_engine" not in body  # nessun canale parallelo
        import agents.execution_agent as ea
        agent_src = Path(ea.__file__).read_text(encoding="utf-8")
        assert "PlaceOrderGateway(" not in agent_src

    def test_candidati_chief_rispettano_il_tetto(self, monkeypatch, temp_db):
        """Anche se la Finanza proponesse uno stake alto, il tetto vale."""
        monkeypatch.setenv("CHIEF_EXECUTION", "live")
        _seed_one(monkeypatch, quota=1.65, strong=True)

        class _Fin:
            bankroll, mode = 100.0, "live"

            def process_many(self, signals, now=None):
                from agents.contracts import FinanceOutput
                from decision.engine import build_plan
                out = FinanceOutput()
                for s in signals:
                    p = build_plan(s, bankroll=100.0, mode="live")
                    out.plans.append(p)
                    out.approved += 1
                return out

        class _Chief:
            finance = _Fin()

            def data(self_ignored=None, **kw):
                raise AssertionError

        chief = _Chief()
        chief.data = lambda **kw: type("M", (), {
            "validated": True,
            "gate": type("G", (), {"reason": type("R", (), {"value": "ok"})}),
            "signals": [],
        })()
        chief.strategy = type("S", (), {"process": staticmethod(lambda sigs: sigs)})
        chief.finance = _Fin()
        _Fin.process_many = lambda self, signals, now=None: _plans()

        import chief_orchestrator
        monkeypatch.setattr(chief_orchestrator, "ChiefOrchestrator",
                            lambda *a, **k: chief)
        out = auto_bet._chief_live_candidates(bankroll=100.0)
        for c in out:
            assert c["stake"] <= 1.50
            assert c["lane"] == "chief"


def _plans():
    """Piano approvato eseguibile con stake sproporzionato (fuori tetto)."""
    from agents.contracts import FinanceOutput
    from decision.commands import CommandKind, Command, CommandPlan
    from decision.models import (DataQuality, RiskDecision, Signal, StakeDecision,
                                 DecisionRecord, ReasonCode)
    from pydantic import datetime as _dt
    signal = Signal(
        signal_id="sx-1|1X2|1", match_id="sx-1", outcome="1", market="1X2",
        price=1.65, home="Home", away="Away", league="Premier League",
        kickoff=(datetime.now(timezone.utc) + timedelta(hours=3))
        .isoformat().replace("+00:00", "Z"),
        model_prob=0.62, market_prob=0.58, edge=0.04, ev=0.05,
        tier="strong_value", confidence=0.6, data_quality=DataQuality(),
    )
    record = DecisionRecord(
        record_id="r1", signal=signal, mode="live",
        risk=RiskDecision(verdict="approve", reason=ReasonCode.OK),
        stake=StakeDecision(stake=99.0, executable=True),
    )
    cmd = Command(
        kind=CommandKind.PLACE_ORDER, mode="live", signal_id=signal.signal_id,
        record_id="r1", dedup_key="k1",
        payload={"match_id": "sx-1", "league": "Premier League",
                 "home": "Home", "away": "Away", "market": "1X2",
                 "outcome": "1", "selection_label": "Home",
                 "kickoff": signal.kickoff, "price": 1.65, "stake": 99.0,
                 "mode": "live", "provider": ""},
    )
    out = FinanceOutput()
    out.plans = [CommandPlan(plan_id="p1", record=record, commands=[cmd])]
    out.approved = 1
    return out


def _seed_one(monkeypatch, quota=1.65, strong=False):
    """Un segnale value/aprovato come in test_auto_bet (lega AMMESSA)."""
    from test_auto_bet import _seed_value_match
    _seed_value_match(mid="m1", home="Osasuna", away="Getafe", esito="1",
                      quota=quota,
                      status="strong_value" if strong else "value")
