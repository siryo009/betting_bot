"""test_equity_reconciler.py — RICONCILIAZIONE FINANZIARIA (P4, 17/10/2026).

Contesto: fra due campioni di equity il conto era variato di una cifra che il
P/L del ledger non spiegava (-2,26 USDC). Lo strumento misura e CLASSIFICA, non
corregge: nessuna scrittura, cause dichiarate solo con prove.

Tutti i test sono OFFLINE (SQLite temporaneo + storico in tmp).
"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import equity_reconciler as er
import tracker


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "recon.db")
    tracker.init_db()
    return tmp_path / "recon.db"


@pytest.fixture()
def hist(tmp_path, monkeypatch):
    path = tmp_path / "bankroll_history.json"
    monkeypatch.setattr(er, "DATA_DIR", tmp_path)
    return path


def _write_hist(path, samples, basis="live_equity"):
    path.write_text(json.dumps({"basis_key": basis, "samples": samples}))


def _iso(dt):
    return dt.isoformat()


def _bet(match_id, stake, profit=None, final=None, settled_at=None,
         mode="live", created_at=None):
    tracker.save_bet(match_id, "1X2", "1", market_id="0x" + match_id,
                     selection_id=1, price=1.8, stake=stake, mode=mode,
                     status="FULLY_FILLED")
    if final:
        conn = tracker._get_conn()
        conn.execute("UPDATE bets SET esito_finale=?, profit=?, settled_at=? "
                     "WHERE match_id=?", (final, profit, settled_at, match_id))
        conn.commit(); conn.close()


# ---------------------------------------------------------------------------
# 1. Parsing delle date
# ---------------------------------------------------------------------------

class TestDate:
    @pytest.mark.parametrize("value", [
        "2026-10-17T12:00:00Z",
        "2026-10-17T12:00:00+00:00",
        "2026-10-17T12:00:00.123456+00:00",
        "2026-10-17T14:00:00+02:00",
    ])
    def test_formati_del_ledger(self, value):
        ts = er._parse_ts(value)
        assert ts is not None and ts.year == 2026
        assert ts.tzinfo is not None

    def test_senza_fuso_e_ambigua(self):
        assert er._parse_ts("2026-10-17T12:00:00") is None
        assert er._parse_ts("2026-10-17 12:00:00") is None

    def test_assunzione_utc_solo_dove_dichiarata(self):
        """`bets.created_at` e' scritto naive: si accetta per una colonna sola."""
        ts = er._parse_ts("2026-10-17T12:00:00", assume_utc=True)
        assert ts is not None and ts.tzinfo is timezone.utc

    @pytest.mark.parametrize("bad", [None, "", "abc", 42])
    def test_input_non_data(self, bad):
        assert er._parse_ts(bad) is None

    def test_datetime_naive(self):
        assert er._parse_ts(datetime(2026, 10, 17, 12, 0)) is None


# ---------------------------------------------------------------------------
# 2. Campioni di equity
# ---------------------------------------------------------------------------

class TestCampioni:
    def test_file_assente_non_solleva(self, hist):
        res = er.load_samples(hist)
        assert res["samples"] == [] and res["error"]

    def test_righe_illeggibili_contate(self, hist):
        now = datetime.now(timezone.utc)
        _write_hist(hist, [[_iso(now), 10.0], ["x", 5.0], [_iso(now), "abc"],
                           [_iso(now), -1.0], ["2026-10-17T10:00:00", 3.0]])
        res = er.load_samples(hist)
        assert len(res["samples"]) == 1
        assert res["bad"] == 4

    def test_ordinati_e_filtrati(self, hist):
        now = datetime.now(timezone.utc)
        _write_hist(hist, [[_iso(now), 20.0],
                           [_iso(now - timedelta(hours=48)), 10.0],
                           [_iso(now - timedelta(hours=1)), 15.0]])
        res = er.load_samples(hist, hours=24)
        assert [s["value"] for s in res["samples"]] == [15.0, 20.0]


# ---------------------------------------------------------------------------
# 3. Ricostruzione e residuo
# ---------------------------------------------------------------------------

class TestRebuild:
    def test_spiegato(self, db, hist):
        now = datetime.now(timezone.utc)
        _write_hist(hist, [[_iso(now - timedelta(hours=3)), 100.0],
                           [_iso(now), 88.0]])
        _bet("m1", 5.0, profit=-12.0, final="lost",
             settled_at=_iso(now - timedelta(hours=1)))
        res = er.rebuild(hours=6, now=now, db=str(db), samples_path=hist)
        assert res["equity"]["delta"] == pytest.approx(-12.0)
        assert res["ledger"]["profit"] == pytest.approx(-12.0)
        assert res["residual"] == pytest.approx(0.0)
        assert res["verdict"] == "explained"

    def test_residuo_negativo_non_spiegato(self, db, hist):
        now = datetime.now(timezone.utc)
        _write_hist(hist, [[_iso(now - timedelta(hours=3)), 100.0],
                           [_iso(now), 85.00]])
        _bet("m1", 5.0, profit=-12.74, final="lost",
             settled_at=_iso(now - timedelta(hours=1)))
        res = er.rebuild(hours=6, now=now, db=str(db), samples_path=hist)
        assert res["residual"] == pytest.approx(-2.26)
        assert res["verdict"] == "unexplained"
        causes = {h["cause"] for h in res["hypotheses"]}
        assert causes, "un residuo non spiegato va accompagnato da ipotesi"
        assert any("commissione" in c or "settlement" in c for c in causes)
        assert res["notes"], "il report deve dichiarare che la causa e' ignota"

    def test_entro_la_tolleranza(self, db, hist):
        now = datetime.now(timezone.utc)
        _write_hist(hist, [[_iso(now - timedelta(hours=3)), 100.0],
                           [_iso(now), 89.995]])
        _bet("m1", 5.0, profit=-10.0, final="lost",
             settled_at=_iso(now - timedelta(hours=1)))
        res = er.rebuild(hours=6, now=now, db=str(db), samples_path=hist)
        assert res["verdict"] == "explained"

    def test_created_at_naive_e_dichiarato(self, db, hist):
        """`save_bet` scrive `created_at` senza fuso: il report lo dichiara."""
        now = datetime.now(timezone.utc)
        _write_hist(hist, [[_iso(now - timedelta(hours=3)), 10.0],
                           [_iso(now), 10.0]])
        _bet("n1", 1.0)
        res = er.rebuild(hours=6, now=now, db=str(db), samples_path=hist)
        assert res["ledger"]["created_at_naive"] >= 1
        assert any("fuso" in n for n in res["notes"])

    def test_solo_live(self, db, hist):
        now = datetime.now(timezone.utc)
        _write_hist(hist, [[_iso(now - timedelta(hours=3)), 100.0],
                           [_iso(now), 90.0]])
        _bet("sim1", 5.0, profit=-99.0, final="lost",
             settled_at=_iso(now - timedelta(hours=1)), mode="sim")
        res = er.rebuild(hours=6, now=now, db=str(db), samples_path=hist)
        assert res["ledger"]["profit"] == pytest.approx(0.0)
        assert res["verdict"] == "unexplained"

    def test_prova_delle_puntate_aperte(self, db, hist):
        now = datetime.now(timezone.utc)
        _write_hist(hist, [[_iso(now - timedelta(hours=3)), 41.0],
                           [_iso(now), 41.0]])
        _bet("open1", 3.0)
        res = er.rebuild(hours=6, now=now, db=str(db), samples_path=hist)
        ev = res["evidence"]
        assert ev["open_live_bets"] == 1
        assert ev["open_live_stake"] == pytest.approx(3.0)

    def test_db_assente_non_solleva(self, hist, tmp_path):
        now = datetime.now(timezone.utc)
        _write_hist(hist, [[_iso(now - timedelta(hours=3)), 10.0],
                           [_iso(now), 9.0]])
        res = er.rebuild(hours=6, now=now, db=str(tmp_path / "nope.db"),
                         samples_path=hist)
        assert res["ledger"]["error"]
        assert res["verdict"] in ("unknown", "unexplained")

    def test_meno_di_due_campioni(self, db, hist):
        now = datetime.now(timezone.utc)
        _write_hist(hist, [[_iso(now), 10.0]])
        res = er.rebuild(hours=6, now=now, db=str(db), samples_path=hist)
        assert res["equity"]["delta"] is None
        assert res["notes"]

    def test_report_leggibile(self, db, hist):
        now = datetime.now(timezone.utc)
        _write_hist(hist, [[_iso(now - timedelta(hours=3)), 100.0],
                           [_iso(now), 85.00]])
        _bet("m1", 5.0, profit=-12.74, final="lost",
             settled_at=_iso(now - timedelta(hours=1)))
        text = er.format_report(er.rebuild(hours=6, now=now, db=str(db),
                                           samples_path=hist))
        assert "Riconciliazione".upper() in text.upper()
        assert "residuo" in text.lower()


# ---------------------------------------------------------------------------
# 4. Timeline dello stop giornaliero
# ---------------------------------------------------------------------------

class TestTimelineStop:
    def _stop(self, path, armed_at, value, start=41.33, loss="14.0"):
        path.write_text(json.dumps({
            "day": armed_at.date().isoformat(), "start_bankroll": start,
            "value": value, "stopped_at": armed_at.isoformat(),
            "stopped_until": (armed_at + timedelta(hours=24)).isoformat(),
            "reason": "equity wallet -%s%% dall'inizio giornata (valore %.2f)"
                      % (loss, value), "basis_key": "live_equity"}))

    def test_stop_precede_i_settlement(self, db, hist, tmp_path):
        """Il caso del 17/10: stop alle 03:11, perdita registrata alle 03:40."""
        armed = datetime.now(timezone.utc).replace(hour=3, minute=11,
                                                   second=0, microsecond=0)
        stop = tmp_path / "daily_stop.json"
        self._stop(stop, armed, 35.54)
        _write_hist(hist, [[_iso(armed - timedelta(minutes=30)), 41.33],
                           [_iso(armed), 35.54]])
        _bet("late", 4.71, profit=-4.71, final="lost",
             settled_at=_iso(armed + timedelta(minutes=29)))
        res = er.stop_timeline(day=armed.date().isoformat(), db=str(db),
                               stop_path=stop, samples_path=hist)
        assert res["verdict"] == "stop_precedes_ledger"
        assert len(res["settlements_before_arm"]) == 0
        assert len(res["settlements_after_arm"]) == 1
        assert res["measured_loss"] == pytest.approx(5.79, abs=1e-6)
        assert "non puo' essere la causa" in res["notes"][0]

    def test_settlement_prima_dell_armamento(self, db, hist, tmp_path):
        armed = datetime.now(timezone.utc).replace(hour=12, minute=0,
                                                   second=0, microsecond=0)
        stop = tmp_path / "daily_stop.json"
        self._stop(stop, armed, 39.0, start=41.33, loss="5.6")
        _write_hist(hist, [[_iso(armed - timedelta(hours=1)), 41.33],
                           [_iso(armed), 39.0]])
        _bet("early", 2.33, profit=-2.33, final="lost",
             settled_at=_iso(armed - timedelta(minutes=5)))
        res = er.stop_timeline(day=armed.date().isoformat(), db=str(db),
                               stop_path=stop, samples_path=hist)
        assert res["verdict"] == "ledger_before_arm"
        assert len(res["settlements_before_arm"]) == 1

    def test_file_assente_non_solleva(self, db, hist, tmp_path):
        res = er.stop_timeline(stop_path=tmp_path / "nope.json", db=str(db),
                               samples_path=hist)
        assert res["verdict"] == "unknown"
        assert res["notes"]


# ---------------------------------------------------------------------------
# 5. Garanzie: sola lettura, nessuna rete, import leggero
# ---------------------------------------------------------------------------

class TestGaranzie:
    def test_connessione_rifiuta_le_scritture(self, db):
        conn = er._connect_ro(str(db))
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("UPDATE bets SET stake=99")
        conn.close()

    def test_il_sorgente_non_scrive(self):
        src = open("equity_reconciler.py", encoding="utf-8").read().upper()
        for verb in ("INSERT INTO", "UPDATE ", "DELETE FROM", "DROP TABLE"):
            assert verb not in src

    def test_nessun_ordine_nel_sorgente(self):
        src = open("equity_reconciler.py", encoding="utf-8").read()
        for name in ("place_limit_order", "_live_fill", "save_bet(",
                     "execution_engine"):
            assert name not in src

    def test_cli_exit_uno_su_residuo(self, db, hist, capsys):
        now = datetime.now(timezone.utc)
        _write_hist(hist, [[_iso(now - timedelta(hours=3)), 100.0],
                           [_iso(now), 90.0]])
        import equity_reconciler
        rc = equity_reconciler.main(["--hours", "6", "--db", str(db),
                                     "--samples", str(hist), "--json"])
        assert rc == 1
        data = json.loads(capsys.readouterr().out)
        assert data["verdict"] == "unexplained"
