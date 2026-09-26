"""test_weekly_stop.py — Circuit breaker SETTIMANALE (26/09/2026).

Direttiva: risk management & circuit breaker. Il drawdown ROLLING su 7 giorni
(-12% dal picco delle ultime 168h) blocca le puntate per 24h e si ri-arma da
solo quando il picco esce dalla finestra. Stessa disciplina dello stop
giornaliero: base EQUITY in LIVE, mai confronti fra basi diverse, fail-open su
file corrotto ma blocco attivo rispettato.

Tutti i test sono OFFLINE: lo stato vive in tmp (isolato da `conftest.py`).
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

import auto_bet
import tracker


@pytest.fixture()
def temp_db(monkeypatch, tmp_path):
    monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "test.db")
    tracker.init_db()
    yield tmp_path / "test.db"


def _now():
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# 1. Storico e drawdown rolling
# ---------------------------------------------------------------------------

class TestDrawdownRolling:
    def test_documenta_le_soglie(self):
        assert auto_bet.WEEKLY_STOP_LOSS_PCT == 0.12
        assert auto_bet.WEEKLY_STOP_WINDOW_H == 168.0

    def test_drawdown_dal_picco(self):
        t0 = _now()
        auto_bet.record_bankroll_sample(100.0, basis_key="live_equity", now=t0)
        auto_bet.record_bankroll_sample(90.0, basis_key="live_equity",
                                        now=t0 + timedelta(hours=2))
        dd = auto_bet.weekly_drawdown(90.0, now=t0 + timedelta(hours=2))
        assert dd["drawdown_pct"] == pytest.approx(10.0, abs=0.01)
        assert dd["peak"] == pytest.approx(100.0)

    def test_campione_max_uno_all_ora(self):
        t0 = _now()
        auto_bet.record_bankroll_sample(100.0, basis_key="live_equity", now=t0)
        auto_bet.record_bankroll_sample(101.0, basis_key="live_equity",
                                        now=t0 + timedelta(minutes=5))
        auto_bet.record_bankroll_sample(102.0, basis_key="live_equity",
                                        now=t0 + timedelta(hours=2))
        hist = json.loads(auto_bet.BANKROLL_HISTORY_FILE.read_text())
        assert len(hist["samples"]) == 2          # 100 e 102, non 101
        assert hist["basis_key"] == "live_equity"

    def test_picco_fuori_finestra_non_conta(self):
        old = _now() - timedelta(hours=200)
        # Il campione vecchio viene POTATO allo scrittura/pruning successiva.
        auto_bet.record_bankroll_sample(500.0, basis_key="live_equity",
                                        now=old)
        dd = auto_bet.weekly_drawdown(100.0, now=_now())
        assert dd["peak"] == pytest.approx(100.0)
        assert dd["drawdown_pct"] == 0.0

    def test_storico_corrotto_non_solleva(self):
        auto_bet.BANKROLL_HISTORY_FILE.write_text("{non-json")
        dd = auto_bet.weekly_drawdown(100.0)
        assert dd["drawdown_pct"] == 0.0
        assert auto_bet.record_bankroll_sample(100.0,
                                               basis_key="live_equity")["recorded"]


# ---------------------------------------------------------------------------
# 2. Trigger e riarmo
# ---------------------------------------------------------------------------

class TestTriggerEWiarmo:
    def test_blocco_sopra_la_soglia(self):
        t0 = _now()
        auto_bet.check_weekly_stop(100.0, basis="equity wallet",
                                   basis_key="live_equity", now=t0)
        r = auto_bet.check_weekly_stop(87.0, basis="equity wallet",
                                       basis_key="live_equity",
                                       now=t0 + timedelta(hours=1))
        assert r["stopped"] is True and r["just_triggered"] is True
        assert r["drawdown_pct"] == pytest.approx(13.0, abs=0.05)
        assert r["until"]

    def test_sotto_la_soglia_non_blocca(self):
        t0 = _now()
        auto_bet.check_weekly_stop(100.0, basis_key="live_equity", now=t0)
        r = auto_bet.check_weekly_stop(90.0, basis_key="live_equity",
                                       now=t0 + timedelta(hours=1))
        assert r["stopped"] is False and r["just_triggered"] is False

    def test_blocco_attivo_rispettato(self):
        t0 = _now()
        auto_bet.check_weekly_stop(100.0, basis_key="live_equity", now=t0)
        auto_bet.check_weekly_stop(80.0, basis_key="live_equity",
                                   now=t0 + timedelta(hours=1))
        # Anche se il drawdown rientra, il blocco resta fino a `until`.
        r = auto_bet.check_weekly_stop(99.0, basis_key="live_equity",
                                       now=t0 + timedelta(hours=2))
        assert r["stopped"] is True and r["just_triggered"] is False

    def test_riarmo_dopo_la_scadenza(self):
        past = (_now() - timedelta(hours=1)).isoformat()
        auto_bet.WEEKLY_STOP_FILE.write_text(json.dumps({
            "stopped_until": past, "drawdown_pct": 20.0, "peak": 100.0,
            "basis_key": "live_equity"}))
        assert auto_bet.weekly_stop_status()["stopped"] is False
        r = auto_bet.check_weekly_stop(99.0, basis_key="live_equity")
        assert r["stopped"] is False

    def test_file_corrotto_fail_open(self):
        auto_bet.WEEKLY_STOP_FILE.write_text("{non-json")
        r = auto_bet.check_weekly_stop(100.0, basis_key="live_equity")
        assert r["stopped"] is False

    def test_clear_riattiva(self):
        t0 = _now()
        auto_bet.check_weekly_stop(100.0, basis_key="live_equity", now=t0)
        auto_bet.check_weekly_stop(80.0, basis_key="live_equity",
                                   now=t0 + timedelta(hours=1))
        assert auto_bet.weekly_stop_status()["stopped"] is True
        auto_bet.clear_weekly_stop()
        assert auto_bet.weekly_stop_status()["stopped"] is False


# ---------------------------------------------------------------------------
# 3. Isolamento delle basi (mai equity vs cassa)
# ---------------------------------------------------------------------------

class TestBasi:
    def test_base_meno_autorevole_ignorata(self):
        t0 = _now()
        auto_bet.record_bankroll_sample(100.0, basis_key="live_equity", now=t0)
        r = auto_bet.check_weekly_stop(20.0, basis="cassa", basis_key="cassa",
                                       now=t0 + timedelta(hours=1))
        assert r["stopped"] is False and r.get("basis_mismatch") is True
        # Lo storico resta quello dell'equity: nessun falso -80%.
        dd = auto_bet.weekly_drawdown(now=t0 + timedelta(hours=1))
        assert dd["peak"] == pytest.approx(100.0)

    def test_base_piu_autorevole_riparte(self):
        t0 = _now()
        auto_bet.record_bankroll_sample(20.0, basis_key="cassa", now=t0)
        r = auto_bet.check_weekly_stop(100.0, basis="equity wallet",
                                       basis_key="live_equity",
                                       now=t0 + timedelta(hours=1))
        assert r["stopped"] is False
        hist = json.loads(auto_bet.BANKROLL_HISTORY_FILE.read_text())
        assert hist["basis_key"] == "live_equity"
        # Lo storico riparte dalla base autorevole: nessun residuo di cassa.
        assert [v for _, v in hist["samples"]] == [pytest.approx(100.0)]


# ---------------------------------------------------------------------------
# 4. Il giro ordini si ferma davvero
# ---------------------------------------------------------------------------

class TestBloccaIlGiro:
    def _seed(self):
        start = (_now() + timedelta(hours=3)).isoformat().replace("+00:00", "Z")
        tracker.save_match("wk1", "Premier League", "Osasuna", "Getafe", start)
        tracker.save_prediction("wk1", "1X2", "Osasuna", 1.65, 0.62, 0.08,
                                market_prob=0.60, market_edge=0.07,
                                status="value")

    def test_run_today_bets_non_piazza_col_settimanale_attivo(self, temp_db):
        self._seed()
        t0 = _now() - timedelta(hours=2)
        auto_bet.check_weekly_stop(100.0, basis="equity wallet",
                                   basis_key="live_equity", now=t0)
        auto_bet.check_weekly_stop(85.0, basis="equity wallet",
                                   basis_key="live_equity",
                                   now=t0 + timedelta(hours=1))
        placed = auto_bet.run_today_bets(stake_eur=5.0)
        assert placed == []
        assert tracker.get_bets() == []

    def test_controprova_senza_settimanale(self, temp_db):
        self._seed()
        placed = auto_bet.run_today_bets(stake_eur=5.0)
        assert len(placed) == 1


# ---------------------------------------------------------------------------
# 5. La catena di decisione vede il blocco (lezione 21/09)
# ---------------------------------------------------------------------------

class TestCatenaDecisione:
    def test_il_blocco_settimanale_entra_nella_catena(self):
        from decision import guards
        from decision.models import KillSwitchStatus, ReasonCode
        kills = KillSwitchStatus(mode="live", provider_ready=True,
                                 weekly_stop_active=True,
                                 weekly_stop_detail="drawdown rolling 13.0%")
        block = guards.first(kills)
        assert block is not None and block.name == "weekly_stop"
        assert block.reason == ReasonCode.WEEKLY_STOP_LOSS

    def test_il_giornaliero_ha_precedenza_sul_settimanale(self):
        from decision import guards
        from decision.models import KillSwitchStatus, ReasonCode
        kills = KillSwitchStatus(mode="live", daily_stop_active=True,
                                 weekly_stop_active=True)
        assert guards.first(kills).reason == ReasonCode.DAILY_STOP_LOSS

    def test_sonda_reale_del_file_settimanale(self):
        """La sonda di default deve leggere `stopped` (non `active`)."""
        from decision import kill_switch
        t0 = _now()
        auto_bet.check_weekly_stop(100.0, basis_key="live_equity", now=t0)
        auto_bet.check_weekly_stop(80.0, basis_key="live_equity",
                                   now=t0 + timedelta(hours=1))
        st = kill_switch.status()
        assert st.weekly_stop_active is True

    def test_catena_dichiara_il_settimanale(self):
        from decision.guards import RULES_BY_NAME, SAFETY_CHAIN
        assert "weekly_stop" in RULES_BY_NAME
        assert "weekly_stop" in [r.name for r in SAFETY_CHAIN]


# ---------------------------------------------------------------------------
# 6. Tripwire: il giro ordini chiama davvero il controllo
# ---------------------------------------------------------------------------

def test_run_today_bets_invoca_il_controllo_settimanale():
    from pathlib import Path
    src = Path(auto_bet.__file__).read_text(encoding="utf-8")
    assert "check_weekly_stop(" in src
