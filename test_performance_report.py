"""Test performance report (performance_report.py)."""
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

import tracker
import performance_report


@pytest.fixture()
def temp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        monkeypatch.setattr(tracker, "DB_PATH", db_path)
        tracker.init_db()
        yield db_path


def _seed_prediction(match_id, mercato, esito, quota, prob, ev,
                     esito_finale, market_edge=0.05):
    """Helper per inserire una previsione chiusa."""
    tracker.save_prediction(match_id, mercato, esito, quota, prob, ev,
                            market_edge=market_edge)
    tracker.settle_predictions()


class TestGetPerformance:
    def test_vuoto(self, temp_db):
        res = performance_report.get_performance(days=30)
        assert res["predictions"]["total"] == 0
        assert res["clv"]["n"] == 0

    def test_con_previsioni(self, temp_db):
        # Inserisci 5 previsioni: 3 vinte (casa vince), 2 perse (trasferta vince)
        for i in range(5):
            mid = f"m{i}"
            if i < 3:
                # Casa vince 2-1 → previsione "1" vince
                tracker.save_result(mid, "Serie A", f"Home{i}", f"Away{i}",
                                    2, 1, datetime.now().isoformat())
            else:
                # Trasferta vince 0-2 → previsione "1" perde
                tracker.save_result(mid, "Serie A", f"Home{i}", f"Away{i}",
                                    0, 2, datetime.now().isoformat())
            _seed_prediction(mid, "1X2", "1", 2.00, 0.55, 0.10, "won")
        res = performance_report.get_performance(days=30)
        assert res["predictions"]["total"] == 5
        assert res["predictions"]["won"] == 3
        assert res["predictions"]["lost"] == 2

    def test_roi_calcolato(self, temp_db):
        # 1 vinta a 2.00 (+1.00), 1 persa (-1.00) → ROI 0%
        tracker.save_result("m0", "Serie A", "A", "B", 2, 0,
                            datetime.now().isoformat())
        _seed_prediction("m0", "1X2", "1", 2.00, 0.55, 0.10, "won")
        tracker.save_result("m1", "Serie A", "C", "D", 0, 2,
                            datetime.now().isoformat())
        _seed_prediction("m1", "1X2", "1", 2.00, 0.55, 0.10, "won")
        res = performance_report.get_performance(days=30)
        assert res["predictions"]["roi"] == pytest.approx(0.0, abs=1.0)

    def test_calibration_gap(self, temp_db):
        # Calibration gap = ROI - EV
        tracker.save_result("m1", "Serie A", "A", "B", 2, 0,
                            datetime.now().isoformat())
        _seed_prediction("m1", "1X2", "1", 2.00, 0.55, 0.10, "won")
        res = performance_report.get_performance(days=30)
        gap = res["predictions"]["calibration_gap"]
        assert isinstance(gap, float)


class TestClvStats:
    def test_clv_positivo(self, temp_db):
        tracker.save_clv("m1", "1", 2.10, signal_started=True)
        tracker.save_clv("m1", "1", 1.90, signal_started=False)
        res = performance_report.get_performance(days=30)
        assert res["clv"]["n"] == 1
        assert res["clv"]["avg_raw"] > 0

    def test_clv_negativo(self, temp_db):
        tracker.save_clv("m1", "1", 1.80, signal_started=True)
        tracker.save_clv("m1", "1", 2.10, signal_started=False)
        res = performance_report.get_performance(days=30)
        assert res["clv"]["avg_raw"] < 0


class TestStreaks:
    def test_streak_vincitori(self, temp_db):
        # 3 vinte di seguito (casa vince)
        for i in range(3):
            mid = f"m{i}"
            tracker.save_result(mid, "L", "A", "B", 2, 0,
                                datetime.now().isoformat())
            _seed_prediction(mid, "1X2", "1", 2.00, 0.55, 0.10, "won")
        res = performance_report.get_performance(days=30)
        s = res["streaks"]
        assert s["current_streak"] == 3
        assert s["current_type"] == "won"

    def test_streak_misti(self, temp_db):
        # vinta, persa, vinta (con timestamp diversi per garantire l'ordine)
        t1 = datetime.now().isoformat()
        t2 = (datetime.now() + __import__('datetime').timedelta(seconds=1)).isoformat()
        t3 = (datetime.now() + __import__('datetime').timedelta(seconds=2)).isoformat()
        tracker.save_result("m0", "L", "A", "B", 2, 0, t1)
        _seed_prediction("m0", "1X2", "1", 2.00, 0.55, 0.10, "won")
        tracker.save_result("m1", "L", "C", "D", 0, 2, t2)
        _seed_prediction("m1", "1X2", "1", 2.00, 0.55, 0.10, "won")
        tracker.save_result("m2", "L", "E", "F", 2, 0, t3)
        _seed_prediction("m2", "1X2", "1", 2.00, 0.55, 0.10, "won")
        res = performance_report.get_performance(days=30)
        s = res["streaks"]
        # L'ordine dipende da settled_at: m0 (won), m1 (lost), m2 (won)
        assert s["current_streak"] >= 1
        assert s["current_type"] == "won"


class TestEdgeAnalysis:
    def test_edge_counts(self, temp_db):
        from market_calib import MARKET_EDGE_MODERATE, MARKET_EDGE_STRONG
        # I bucket sono DERIVATI dalle soglie reali della strategia: un test
        # con edge fissi (0.08/0.04/0.01) scadeva in silenzio al primo cambio
        # di soglia (verificato il 21/09, quando il minimo e' passato a +2pp).
        # I bucket 'value' e 'strong' esistono solo se le due soglie sono
        # ORDINATE: dichiarato, cosi' un futuro allineamento delle costanti si
        # legge subito invece di far fallire il conteggio (16/10/2026).
        assert MARKET_EDGE_MODERATE < MARKET_EDGE_STRONG, (
            "senza MODERATE < STRONG non esiste il bucket 'value'")
        over_strong = MARKET_EDGE_STRONG + 0.03
        # Punto MEDIO fra le due soglie. Prima era `MODERATE + 0.01`, che con
        # STRONG a MODERATE+0.01 (soglie del 14/10) cadeva ESATTAMENTE sulla
        # soglia strong: bucket strong a 2, value a 0 — due bucket su tre
        # misurati dalla stessa riga (16/10/2026).
        in_value = (MARKET_EDGE_MODERATE + MARKET_EDGE_STRONG) / 2.0
        under_min = MARKET_EDGE_MODERATE - 0.01
        # 3 previsioni con edge diverso
        tracker.save_result("m0", "L", "A", "B", 2, 0,
                            datetime.now().isoformat())
        _seed_prediction("m0", "1X2", "1", 2.00, 0.55, 0.10, "won",
                         market_edge=over_strong)
        tracker.save_result("m1", "L", "C", "D", 0, 2,
                            datetime.now().isoformat())
        _seed_prediction("m1", "1X2", "1", 2.00, 0.55, 0.10, "won",
                         market_edge=in_value)
        tracker.save_result("m2", "L", "E", "F", 2, 0,
                            datetime.now().isoformat())
        _seed_prediction("m2", "1X2", "1", 2.00, 0.55, 0.10, "won",
                         market_edge=under_min)
        res = performance_report.get_performance(days=30)
        e = res["edge"]
        assert e["strong_value_n"] == 1
        assert e["value_n"] == 1
        assert e["weak_n"] == 1


def _seed_bet(match_id, mercato, price, stake, mode="live", league="Serie A",
              profit=None):
    """Puntata chiusa (o aperta se `profit` e' None) con esito noto."""
    import sqlite3
    tracker.save_bet(match_id, mercato, "1", price=price, stake=stake,
                     mode=mode, status="FULLY_FILLED",
                     home="Casa", away="Ospite", league=league)
    if profit is None:
        return
    conn = sqlite3.connect(tracker.DB_PATH)
    conn.execute("UPDATE bets SET esito_finale=?, profit=? WHERE match_id=?",
                 ("won" if profit > 0 else "lost", profit, match_id))
    conn.commit()


class TestBetsBreakdown:
    """Split delle puntate reali (27/10/2026): il totale non dice DOVE si perde."""

    def test_vuoto_ha_la_forma_completa(self, temp_db):
        conn = tracker._get_conn()
        d = performance_report._bets_stats(conn, "2020-01-01", "2030-01-01")
        assert d["n"] == 0
        for key in ("by_market", "by_band", "by_league", "by_day",
                    "open", "last"):
            assert key in d, key
        assert d["open_n"] == 0 and d["open_stake"] == 0.0

    def test_split_per_mercato_e_banda(self, temp_db):
        _seed_bet("t1", "TENNIS", 1.45, 1.0, profit=0.45)
        _seed_bet("t2", "TENNIS", 2.20, 1.0, profit=-1.0)
        _seed_bet("c1", "1X2", 1.75, 1.0, profit=0.75)
        conn = tracker._get_conn()
        d = performance_report._bets_stats(conn, "2020-01-01", "2030-01-01")
        assert d["n"] == 3 and d["pnl"] == pytest.approx(0.20)
        assert d["by_market"]["TENNIS"]["n"] == 2
        assert d["by_market"]["TENNIS"]["pnl"] == pytest.approx(-0.55)
        assert d["by_market"]["TENNIS"]["roi"] == pytest.approx(-27.5)
        assert d["by_market"]["1X2"]["roi"] == pytest.approx(75.0)
        assert d["by_band"]["1.30-1.50"]["n"] == 1
        assert d["by_band"]["2.00-2.30"]["pnl"] == pytest.approx(-1.0)

    def test_il_giorno_non_finisce_nelle_leghe(self, temp_db):
        # Regressione: il bucket del giorno veniva scritto nell'ULTIMO
        # dizionario legato dal ciclo (le leghe) perche' la closure usava la
        # variabile del for invece di un parametro.
        _seed_bet("t1", "TENNIS", 1.60, 1.0, profit=0.60)
        conn = tracker._get_conn()
        d = performance_report._bets_stats(conn, "2020-01-01", "2030-01-01")
        assert list(d["by_league"]) == ["Serie A"]
        assert len(d["by_day"]) == 1

    def test_puntate_aperte_fuori_dai_chiusi(self, temp_db):
        _seed_bet("closed", "1X2", 1.75, 1.0, profit=0.75)
        _seed_bet("open", "TENNIS", 1.40, 2.5, profit=None)
        conn = tracker._get_conn()
        d = performance_report._bets_stats(conn, "2020-01-01", "2030-01-01")
        assert d["n"] == 1 and d["total_stake"] == pytest.approx(1.0)
        assert d["open_n"] == 1
        assert d["open_stake"] == pytest.approx(2.5)
        assert d["open"][0]["match_id"] == "open"

    def test_ultime_chiuse_dalla_piu_recente(self, temp_db):
        for i in range(12):
            _seed_bet(f"m{i}", "1X2", 1.70 + i / 100, 1.0, profit=0.7)
        conn = tracker._get_conn()
        d = performance_report._bets_stats(conn, "2020-01-01", "2030-01-01")
        assert len(d["last"]) == 10
        assert d["last"][0]["match_id"] == "m11"

    def test_banda_numerica_estrema(self):
        assert performance_report._band_label(1.05) == "1.00-1.30"
        assert performance_report._band_label(3.5) == ">=2.30"
        assert performance_report._band_label(None) == "n/d"

    def test_esposto_nel_dashboard(self, temp_db):
        _seed_bet("t1", "TENNIS", 1.45, 1.0, profit=0.45)
        res = performance_report.get_performance(days=30)
        assert "by_market" in res["bets"]
        assert "open" in res["bets"]


class TestReport:
    def test_report_testuale(self, temp_db):
        res = performance_report.get_performance(days=30)
        report = performance_report._report(res)
        assert "REPORT PERFORMANCE" in report

    def test_report_json(self, temp_db):
        res = performance_report.get_performance(days=30)
        assert "predictions" in res
        assert "clv" in res
        assert "bankroll" in res


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
