"""Test del monitor scarti liquidita' SX Bet (11/09/2026).

Copre:
  1. registrazione eventi + riepilogo (conteggi, edge perso, finestra);
  2. fail-safe (log non scrivibile / righe corrotte -> mai eccezioni);
  3. integrazione con `sx_signals.scan` (mercato sottile -> evento "scan");
  4. integrazione con `auto_bet._live_fill` (book sottile -> "order",
     riempimento parziale -> "partial"), senza mai un ordine reale;
  5. report Telegram.
"""
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import liquidity_monitor
import auto_bet
import tracker


@pytest.fixture(autouse=True)
def isolated_log(tmp_path, monkeypatch):
    monkeypatch.setattr(liquidity_monitor, "SKIP_LOG",
                        tmp_path / "execution" / "liquidity_skips.jsonl")


@pytest.fixture()
def temp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setattr(tracker, "DB_PATH", Path(td) / "test.db")
        tracker.init_db()
        yield


# ---------------------------------------------------------------------------
# 1. Registrazione + riepilogo
# ---------------------------------------------------------------------------

class TestRecordAndSummary:
    def test_summary_vuoto(self):
        s = liquidity_monitor.summary(days=7)
        assert s["events"] == 0 and s["last"] is None
        assert liquidity_monitor.format_report(7) is None

    def test_eventi_contati_per_tipo_e_motivo(self):
        liquidity_monitor.record_skip("scan", "depth_totale", match_id="sx-1",
                                      home="A", away="B", depth=3.0,
                                      threshold=15.0)
        liquidity_monitor.record_skip("order", "depth_vs_stake", match_id="sx-1",
                                      home="A", away="B", esito="1", stake=2.0,
                                      ev=0.08, depth=1.0, threshold=2.0)
        liquidity_monitor.record_skip("partial", "riempimento_parziale",
                                      match_id="sx-2", stake=1.5, ev=0.05)
        s = liquidity_monitor.summary(days=7)
        assert s["events"] == 3
        assert s["by_kind"] == {"scan": 1, "order": 1, "partial": 1}
        assert s["unique_matches"] == 2
        # edge perso = 0.08*2.0 + 0.05*1.5 = 0.235 -> 0.24 (arrotondato)
        assert s["missed_profit"] == pytest.approx(0.235, abs=0.01)
        assert s["last"]["kind"] == "partial"  # il piu' recente prima in lista

    def test_finestra_temporale(self, monkeypatch):
        """Un evento vecchio esce dalla finestra di 1 giorno."""
        old = (datetime.now(timezone.utc) - timedelta(days=10)).timestamp()
        evt = liquidity_monitor.record_skip("scan", "depth_totale",
                                            match_id="old")
        # riscrive la riga con un timestamp vecchio
        liquidity_monitor.SKIP_LOG.write_text(
            json.dumps({**evt, "ts_epoch": old}), encoding="utf-8")
        assert liquidity_monitor.summary(days=1)["events"] == 0
        assert liquidity_monitor.summary(days=30)["events"] == 1

    def test_righe_corrotte_ignorate(self):
        liquidity_monitor.record_skip("scan", "depth_totale", match_id="ok")
        with liquidity_monitor.SKIP_LOG.open("a", encoding="utf-8") as fh:
            fh.write("non-json\n\n{\"rotto\": \n")
        s = liquidity_monitor.summary(days=7)
        assert s["events"] == 1

    def test_report_contiene_i_numeri(self):
        liquidity_monitor.record_skip("order", "depth_vs_stake", match_id="sx-9",
                                      home="Osasuna", away="Getafe", esito="1",
                                      stake=3.0, ev=0.10, depth=1.0,
                                      threshold=3.0)
        text = liquidity_monitor.format_report(days=1)
        assert "MONITOR LIQUIDITA'" in text
        assert "Osasuna" in text and "depth_vs_stake" in text
        assert "0.30" in text  # edge perso 0.10*3.0


# ---------------------------------------------------------------------------
# 2. Fail-safe
# ---------------------------------------------------------------------------

class TestFailSafe:
    def test_log_non_scrivibile_non_solleva(self, tmp_path, monkeypatch):
        blocker = tmp_path / "not_a_dir"
        blocker.write_text("x")
        monkeypatch.setattr(liquidity_monitor, "SKIP_LOG",
                            blocker / "nested" / "skips.jsonl")
        evt = liquidity_monitor.record_skip("scan", "depth_totale")
        assert "error" in evt          # segnalato, non propagato

    def test_summary_su_log_inesistente(self):
        assert liquidity_monitor.summary(days=1)["events"] == 0


# ---------------------------------------------------------------------------
# 3. Integrazione: scan (segnale scartato per book sottile)
# ---------------------------------------------------------------------------

class TestScanIntegration:
    def test_scan_sottile_registra_evento(self, monkeypatch, temp_db):
        import sx_signals
        from test_sx_signals import FakeSxProvider
        monkeypatch.setattr(sx_signals, "expected_goals",
                            lambda h, a: (1.9, 0.8))
        monkeypatch.setattr(sx_signals, "prob_1x2",
                            lambda lh, la: (0.62, 0.23, 0.15))
        monkeypatch.setattr(sx_signals, "adjusted_probability",
                            lambda mp, mkt, price, league=None: mp)
        monkeypatch.setattr(sx_signals, "MIN_DEPTH_USDC", 1_000_000.0)
        assert sx_signals.scan(provider=FakeSxProvider()) == []
        events = liquidity_monitor.iter_events(days=1)
        assert len(events) == 1
        assert events[0]["kind"] == "scan"
        assert events[0]["reason"] == "depth_totale"

    def test_scan_leg_giocata_sottile_registra_evento(self, monkeypatch, temp_db):
        """Taratura 11/09: il match passa il filtro di mercato (totale 86 >=
        25) ma la LEG che verrebbe giocata ha 6 USDC < 25 -> nessun segnale
        (l'ordine sarebbe comunque scartato) e scarto registrato."""
        import sx_signals
        from test_sx_signals import FakeSxProvider
        monkeypatch.setattr(sx_signals, "expected_goals",
                            lambda h, a: (1.9, 0.8))
        monkeypatch.setattr(sx_signals, "prob_1x2",
                            lambda lh, la: (0.62, 0.23, 0.15))
        monkeypatch.setattr(sx_signals, "adjusted_probability",
                            lambda model_prob, market_prob, price, league=None:
                            model_prob)

        class ThinFavourite(FakeSxProvider):
            def _get(self, path, params=None):
                data = super()._get(path, params)
                if path == "orderbook-v3/snapshot" and \
                        (params or {}).get("marketHash") == "mkt1":
                    # mkt1 = favorito (leg "1"): 6 USDC al floor; gli altri
                    # due esiti restano a 40 (totale 86, sopra la soglia di
                    # mercato).
                    data["data"]["outcomeOne"][0]["size"] = 6 * 10 ** 6
                return data

        assert sx_signals.scan(provider=ThinFavourite()) == []
        events = liquidity_monitor.iter_events(days=1)
        assert len(events) == 1
        assert events[0]["kind"] == "scan"
        assert events[0]["reason"] == "depth_exec_1"
        assert events[0]["threshold"] == pytest.approx(
            sx_signals.MIN_EXEC_DEPTH_USDC)


# ---------------------------------------------------------------------------
# 4. Integrazione: _live_fill (ordine saltato / riempimento parziale)
# ---------------------------------------------------------------------------

def _catalogue(home="Osasuna", away="Getafe"):
    ts = (datetime.now(timezone.utc) + timedelta(hours=3)) \
        .isoformat().replace("+00:00", "Z")

    def row(mid, o1):
        return {"market_id": mid, "event_name": f"{home} vs {away}",
                "open_date": ts, "team_one_name": home, "team_two_name": away,
                "outcome_one_name": o1, "outcome_two_name": f"Not {o1}",
                "runners": [{"selection_id": 1, "name": o1},
                            {"selection_id": 2, "name": f"Not {o1}"}]}
    return [row("m-home", home), row("m-away", away), row("m-tie", "Tie")]


class _Prov:
    name = "sxbet"

    def __init__(self, best, book, order=None):
        self.best = best
        self.book = book
        self.order = order
        self.place_calls = []

    def best_back_price(self, market_id, selection_id):
        return self.best

    def get_market_book(self, market_id):
        return self.book

    def list_market_catalogue(self, event_type_ids=("5",), market_type="1X2",
                              max_results=400):
        return _catalogue()

    def place_limit_order(self, market_id, selection_id, side, price, size,
                          persistence="LAPSE"):
        self.place_calls.append((market_id, selection_id, side, price, size))
        return self.order


class TestOrderIntegration:
    def _pick(self):
        return {"match_id": "m1", "home": "Osasuna", "away": "Getafe",
                "esito_key": "1", "mercato": "1X2",
                "commence": (datetime.now(timezone.utc) + timedelta(hours=3))
                .isoformat().replace("+00:00", "Z")}

    def _thin_book(self, size=0.5):
        return {"runners": [{"selectionId": 1,
                             "availableToBack": [{"price": 1.65,
                                                  "size": size}]}]}

    def _engine(self, monkeypatch, prov):
        import execution_engine as ee
        engine = type("_Eng", (), {"provider": prov})()
        monkeypatch.setattr(ee, "ExecutionEngine", lambda *a, **k: engine)
        return ee

    def test_book_sottile_salta_e_registra(self, monkeypatch):
        prov = _Prov(best=1.70, book=self._thin_book(0.5))
        self._engine(monkeypatch, prov)
        res = auto_bet._live_fill(self._pick(), stake=5.0, floor=1.65)
        assert res is None
        assert prov.place_calls == []      # nessun ordine inviato
        evts = liquidity_monitor.iter_events(days=1)
        assert len(evts) == 1
        assert evts[0]["kind"] == "order"
        assert evts[0]["reason"] == "depth_vs_stake"
        assert evts[0]["depth"] == pytest.approx(0.5)
        assert evts[0]["stake"] == pytest.approx(5.0)

    def test_book_sotto_il_margine_di_sicurezza_salta(self, monkeypatch):
        """Taratura 11/09: non basta coprire lo stake, serve il MARGINE.

        Book 6 USDC con stake 5: lo stake sarebbe coperto, ma
        6 < max(5 x 2.0, 25) = 25 (minimo assoluto di libro al floor) ->
        l'ordine e' saltato.
        """
        prov = _Prov(best=1.70, book=self._thin_book(6.0))
        self._engine(monkeypatch, prov)
        assert auto_bet._live_fill(self._pick(), stake=5.0, floor=1.65) is None
        assert prov.place_calls == []
        evts = liquidity_monitor.iter_events(days=1)
        assert len(evts) == 1
        assert evts[0]["kind"] == "order"
        assert evts[0]["depth"] == pytest.approx(6.0)
        assert evts[0]["threshold"] == pytest.approx(
            auto_bet.required_depth(5.0))
        assert evts[0]["extra"]["richiesto"] == pytest.approx(
            auto_bet.required_depth(5.0))
        assert evts[0]["extra"]["multiplier"] == pytest.approx(
            auto_bet.SX_DEPTH_MULTIPLIER)

    def test_riempimento_parziale_registra(self, monkeypatch):
        import execution_engine as ee
        # size al floor >= richiesto (max(5 x 2.0, 25) = 25): la guardia
        # pre-ordine non scatta e l'ordine arriva al provider...
        prov = _Prov(best=1.70, book=self._thin_book(30.0))
        order = ee.OrderResult(True, "0x9", "PARTIAL_FILLED", 1.65, 1.66,
                               2.0, 8.0)
        prov.order = order
        self._engine(monkeypatch, prov)
        res = auto_bet._live_fill(self._pick(), stake=5.0, floor=1.65)
        assert res and res["ok"] is True
        evts = liquidity_monitor.iter_events(days=1)
        assert len(evts) == 1
        assert evts[0]["kind"] == "partial"
        assert evts[0]["stake"] == pytest.approx(3.0)  # 5.0 - 2.0 non eseguito

    def test_ordine_pieno_nessun_evento(self, monkeypatch):
        import execution_engine as ee
        prov = _Prov(best=1.70, book=self._thin_book(30.0))
        prov.order = ee.OrderResult(True, "0x9", "FULLY_FILLED", 1.65, 1.66,
                                    5.0, 8.0)
        self._engine(monkeypatch, prov)
        res = auto_bet._live_fill(self._pick(), stake=5.0, floor=1.65)
        assert res and res["ok"] is True
        assert liquidity_monitor.iter_events(days=1) == []


# ---------------------------------------------------------------------------
# 5. Difesa in profondita' sul minimo quota in auto_bet
# ---------------------------------------------------------------------------

class TestJobRegistrato:
    """Tripwire: il monitor deve girare in background nel bot."""

    def test_job_schedulato_in_bot(self):
        src = Path("bot.py").read_text(encoding="utf-8")
        assert "async def liquidity_monitor_job" in src
        assert "run_repeating(liquidity_monitor_job" in src

    def test_sezione_nel_report(self):
        src = Path("bot.py").read_text(encoding="utf-8")
        assert "Scarti liquidita' SX" in src


class TestOddsMinDownstream:
    def test_quota_sotto_1_30_mai_candidata(self, temp_db):
        start = (datetime.now(timezone.utc) + timedelta(hours=3)) \
            .isoformat().replace("+00:00", "Z")
        tracker.save_match("low", "Serie A", "Osasuna", "Getafe", start)
        tracker.save_prediction("low", "1X2", "Osasuna", 1.20, 0.72, 0.05,
                                market_prob=0.75, market_edge=0.05,
                                status="value")
        assert auto_bet._today_value_picks() == []
