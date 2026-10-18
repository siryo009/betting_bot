"""test_stop_audit.py — AUDIT CONTINUO DURANTE GLI STOP (P2, 17/10/2026).

Misura che ha motivato il modulo: il 17/10 la serie di equity ha un buco di
**19 ore** (campioni 02:44 -> 22:17) proprio mentre il conto perdeva 14,59 USDC,
perche' `run_today_bets` usciva sullo stop PRIMA di registrare il campione. Un
arresto e' un evento finanziario: va tracciato con gli stessi numeri con cui si
prende una decisione, e non deve interrompere il monitoraggio.

Tutti i test sono OFFLINE: il JSONL e gli stati dei breaker vivono nella tmp
(`conftest.py`), il ledger e' un SQLite temporaneo.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

import auto_bet
import tracker


@pytest.fixture()
def db(monkeypatch, tmp_path):
    monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "test.db")
    tracker.init_db()
    return tmp_path / "test.db"


def _rows():
    path = auto_bet.STOP_AUDIT_LOG
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _arm_daily_stop(loss_pct=0.20):
    """Arma lo stop giornaliero con la sequenza reale (riferimento + crollo)."""
    auto_bet.check_daily_stop(100.0, basis="cassa", basis_key="cassa")
    return auto_bet.check_daily_stop(100.0 * (1 - loss_pct), basis="cassa",
                                     basis_key="cassa")


# ---------------------------------------------------------------------------
# 1. Contenuto dell'audit
# ---------------------------------------------------------------------------

class TestContenutoAudit:
    def test_scrive_tutti_i_campi(self, db):
        res = auto_bet.stop_audit("daily_stop", bankroll=41.0, available=38.0,
                                  exposure=3.0, basis_key="live_equity",
                                  mode="live", force=True,
                                  extra={"until": "2026-10-18T03:11:00+00:00"})
        assert res["written"] is True
        row = _rows()[-1]
        for field in ("ts", "reason", "mode", "equity", "equity_raw",
                      "available", "exposure", "open_stake_ledger",
                      "open_orders", "realized_pl_today", "basis_key",
                      "breakers", "caps", "extra"):
            assert field in row, field
        assert row["equity"] == pytest.approx(41.0)
        assert row["available"] == pytest.approx(38.0)
        assert row["exposure"] == pytest.approx(3.0)
        assert row["basis_key"] == "live_equity"
        assert row["extra"]["until"].endswith("03:11:00+00:00")
        assert set(row["breakers"]) == {"daily", "weekly", "cb2",
                                        "kill_switch"}
        assert row["caps"]["per_event"] == pytest.approx(4.92, abs=1e-6)
        assert row["caps"]["per_order"] == pytest.approx(2.05, abs=1e-6)

    def test_pl_realizzato_solo_live_e_di_oggi(self, db):
        tracker.save_bet("m1", "1X2", "1", price=1.8, stake=2.0, mode="live",
                         status="FULLY_FILLED")
        tracker.save_bet("m2", "1X2", "1", price=1.8, stake=2.0, mode="sim",
                         status="FULLY_FILLED")
        now = datetime.now(timezone.utc).isoformat()
        conn = tracker._get_conn()
        conn.execute("UPDATE bets SET esito_finale='won', profit=1.6, "
                     "settled_at=? WHERE match_id='m1'", (now,))
        conn.execute("UPDATE bets SET esito_finale='won', profit=1.6, "
                     "settled_at=? WHERE match_id='m2'", (now,))
        conn.commit(); conn.close()
        assert auto_bet._realized_pl_today() == pytest.approx(1.6)

    def test_pl_illeggibile_e_none_non_zero(self, db, monkeypatch):
        def _boom():
            raise RuntimeError("db rotto")
        monkeypatch.setattr(tracker, "_get_conn", _boom)
        assert auto_bet._realized_pl_today() is None
        row = auto_bet.stop_audit("daily_stop", bankroll=41.0, force=True)["row"]
        assert row["realized_pl_today"] is None

    def test_audit_non_solleva_mai(self, db, monkeypatch):
        monkeypatch.setattr(auto_bet, "_open_live_snapshot",
                            lambda: (_ for _ in ()).throw(RuntimeError("x")))
        res = auto_bet.stop_audit("daily_stop", bankroll=41.0, force=True)
        assert res["written"] is False
        assert _rows() == []


# ---------------------------------------------------------------------------
# 2. Throttle (il giro gira ogni 60s)
# ---------------------------------------------------------------------------

class TestThrottle:
    def test_una_riga_per_finestra(self, db):
        auto_bet.stop_audit("daily_stop", bankroll=41.0)
        auto_bet.stop_audit("daily_stop", bankroll=41.1)
        assert len(_rows()) == 1

    def test_force_scrive_la_transizione(self, db):
        auto_bet.stop_audit("daily_stop", bankroll=41.0)
        auto_bet.stop_audit("daily_stop", bankroll=41.0, force=True)
        assert len(_rows()) == 2

    def test_motivi_diversi_hanno_finestre_diverse(self, db):
        auto_bet.stop_audit("daily_stop", bankroll=41.0)
        auto_bet.stop_audit("weekly_stop", bankroll=41.0)
        auto_bet.stop_audit("cb2", bankroll=41.0)
        reasons = [r["reason"] for r in _rows()]
        assert reasons == ["daily_stop", "weekly_stop", "cb2"]


# ---------------------------------------------------------------------------
# 3. Il campione di equity si registra ANCHE con lo stop attivo
# ---------------------------------------------------------------------------

class TestCampioneDuranteLoStop:
    def test_campione_registrato_con_stop_giornaliero_attivo(self, db):
        """Regressione del buco di 19h del 17/10/2026."""
        stamp = (datetime.now(timezone.utc) + timedelta(hours=3)) \
            .isoformat().replace("+00:00", "Z")
        tracker.save_match("stop1", "Premier League", "Osasuna", "Getafe", stamp)
        tracker.save_prediction("stop1", "1X2", "Osasuna", 1.65, 0.62, 0.08,
                                market_prob=0.60, market_edge=0.07,
                                status="value")
        _seed_old_sample(100.0)
        trig = _arm_daily_stop()
        assert trig["stopped"] is True
        before = len(_samples())
        placed = auto_bet.run_today_bets(stake_eur=5.0)
        assert placed == [] and tracker.get_bets() == []
        after = _samples()
        assert len(after) > before, "nessun campione di equity con lo stop attivo"
        assert _fresh(after[-1]), "campione non aggiornato durante lo stop"
        rows = _rows()
        assert rows[-1]["reason"] == "daily_stop"
        assert rows[-1]["breakers"]["daily"]["stopped"] is True

    def test_campione_registrato_con_stop_settimanale_attivo(self, db):
        _seed_old_sample(100.0)
        weekly = _arm_weekly()
        assert weekly["stopped"] is True
        assert auto_bet.run_today_bets(stake_eur=5.0) == []
        rows = _rows()
        assert rows[-1]["reason"] == "weekly_stop"
        assert rows[-1]["breakers"]["weekly"]["stopped"] is True
        # La serie continua: l'ultimo campione e' di ADESSO, non di 3h fa.
        assert _fresh(_samples()[-1])

    def test_audit_anche_a_giro_vuoto_senza_stop(self, db):
        """Senza stop e senza candidati NON si scrive: e' rumore, non un
        arresto."""
        assert auto_bet.run_today_bets(stake_eur=5.0) == []
        assert [r for r in _rows() if r["reason"] in ("daily_stop",
                                                      "weekly_stop")] == []

    def test_modalita_off_lascia_traccia(self, db, monkeypatch):
        monkeypatch.setattr(auto_bet, "_execution_mode",
                            lambda *a, **k: "off")
        assert auto_bet.run_today_bets(stake_eur=5.0) == []
        rows = _rows()
        assert rows[-1]["reason"] == "kill_switch"
        assert rows[-1]["equity"] is None       # wallet non letto: niente finto


# ---------------------------------------------------------------------------
# 4. Live: il campione usa l'equity (disponibile + in gioco)
# ---------------------------------------------------------------------------

class TestLive:
    def test_campione_equity_non_disponibile(self, db, monkeypatch):
        monkeypatch.setattr(auto_bet, "_execution_mode",
                            lambda *a, **k: "live")
        monkeypatch.setattr(auto_bet, "_live_wallet_snapshot",
                            lambda: {"available": 23.46, "exposure": 3.46,
                                     "equity": 26.92})
        _seed_old_sample(30.0, basis_key="live_equity")
        _arm_daily_stop_for_equity(26.92)
        assert auto_bet.run_today_bets(stake_eur=5.0) == []
        row = _rows()[-1]
        assert row["reason"] == "daily_stop"
        assert row["basis_key"] == "live_equity"
        assert row["available"] == pytest.approx(23.46)
        assert row["exposure"] == pytest.approx(3.46)
        assert row["equity"] == pytest.approx(26.92)
        last = _samples()[-1]
        assert _fresh(last)
        assert last[1] == pytest.approx(26.92)
        # Nel monitoraggio della giornata c'e' anche il P/L realizzato.
        assert row["realized_pl_today"] is not None


def _samples():
    try:
        hist = json.loads(auto_bet.BANKROLL_HISTORY_FILE.read_text())
    except Exception:
        return []
    return hist.get("samples") or []


def _seed_old_sample(value, basis_key="cassa", hours=3):
    """Campione vecchio: senza, il throttle (1/ora) maschera la registrazione."""
    old = datetime.now(timezone.utc) - timedelta(hours=hours)
    auto_bet.BANKROLL_HISTORY_FILE.write_text(json.dumps(
        {"basis_key": basis_key, "samples": [[old.isoformat(), value]]}))


def _fresh(sample) -> bool:
    ts = datetime.fromisoformat(sample[0].replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - ts).total_seconds() < 300


def _arm_weekly():
    t0 = datetime.now(timezone.utc)
    auto_bet.record_bankroll_sample(100.0, basis_key="cassa", now=t0 -
                                    timedelta(hours=2))
    auto_bet.check_weekly_stop(100.0, basis="cassa", basis_key="cassa",
                              now=t0 - timedelta(hours=2))
    return auto_bet.check_weekly_stop(85.0, basis="cassa", basis_key="cassa",
                                      now=t0)


def _arm_daily_stop_for_equity(equity):
    auto_bet.check_daily_stop(equity, basis="equity wallet",
                              basis_key="live_equity")
    return auto_bet.check_daily_stop(equity * 0.8, basis="equity wallet",
                                     basis_key="live_equity")
