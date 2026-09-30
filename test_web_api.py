"""
Test unitari per l'API JSON backend (web_api.py).
"""

import json
import time

import pytest

import tracker
import web_api


@pytest.fixture(autouse=True)
def _tmp_db(monkeypatch, tmp_path):
    monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "webapi.db")
    tracker.init_db()
    yield


def _seed_signal(monkeypatch):
    # inserisce un segnale chiuso + uno pendente direttamente nel DB
    tracker.log_signal(123, "Inter vs Napoli", "Over 2.5", 2.10, 0.55, 0.157)
    conn = tracker._get_conn(); c = conn.cursor()
    c.execute("UPDATE signals SET esito_finale='won', profit=1.10 WHERE id=1")
    conn.commit(); conn.close()


class TestHealth:
    def test_health_ok(self):
        h = web_api._health_json()
        assert h["status"] == "ok"

    def test_api_football_key_flag(self, monkeypatch):
        monkeypatch.setenv("API_FOOTBALL_KEY", "x")
        assert web_api._health_json()["api_football_key"] is True
        monkeypatch.delenv("API_FOOTBALL_KEY", raising=False)
        assert web_api._health_json()["api_football_key"] is False


class TestDashboard:
    def test_dashboard_vuoto(self):
        d = web_api._dashboard_json()
        assert d["bankroll"] == pytest.approx(100.0)
        assert d["ultime_value"] == []

    def test_dashboard_con_segnale(self):
        tracker.log_signal(123, "X vs Y", "1", 2.0, 0.55, 0.10)
        d = web_api._dashboard_json()
        # il segnale pendente appare tra le ultime value bet
        assert len(d["ultime_value"]) >= 1

    def test_dashboard_bankroll_reale_da_cassa(self):
        """Con movimento in cassa, il bankroll mostrato è il SUM reale
        (colonna importo) e non il default env — bug amount/importo fixato."""
        tracker.save_cassa_entry("Inter vs Napoli", "1", 2.1, 10.0)
        tracker.save_cassa_entry("Roma vs Milan", "2", 3.0, 20.0)
        d = web_api._dashboard_json()
        assert d["bankroll"] == pytest.approx(30.0)

    def test_dashboard_include_nuove_sezioni(self):
        """La dashboard espone streak, CLV, auto_bets e per_mercato."""
        d = web_api._dashboard_json()
        assert "streaks" in d
        assert "clv" in d
        assert "auto_bets" in d
        assert "per_mercato" in d
        assert "bankroll_stats" in d

    def test_dashboard_include_market_signals(self):
        """La dashboard espone anche gli alert RLM/steam/crollo (stessi dati
        del report Telegram) per il monitor in pagina."""
        d = web_api._dashboard_json()
        assert "market_signals" in d
        assert "summary" in d["market_signals"]
        assert "signals" in d["market_signals"]
        assert d["market_signals"]["summary"]["total"] >= 0


class TestStorico:
    def test_storico_vuoto(self, monkeypatch):
        s = web_api._storico_json()
        assert s["segnali"] == []
        assert s["summary"]["closed"] == 0

    def test_storico_con_segnali(self):
        tracker.log_signal(123, "Inter vs Napoli", "1", 2.0, 0.5, 0.1)
        s = web_api._storico_json()
        assert len(s["segnali"]) >= 1
        assert s["segnali"][0]["evento"] == "Inter vs Napoli"


class TestSchedina:
    def test_schedina_vuoto(self):
        s = web_api._schedina_json()
        assert s["picks"] == []
        assert s["multipla"] is None
        assert s["bankroll"] == pytest.approx(100.0)


class TestCalibration:
    """Dashboard calibrazione: sezioni coerenti anche con DB vuoto."""

    def test_endpoint_risponde_con_sezioni(self):
        d = web_api._calibration_json()
        assert "model" in d and "calibration" in d
        assert "reliability" in d and "drift" in d
        assert isinstance(d["reliability"], list)

    def test_modello_e_calibratore_seriali(self):
        d = web_api._calibration_json()
        # modello: trained puo' essere False (nessun file) ma la chiave esiste
        assert "trained" in d["model"]
        assert "fitted" in d["calibration"]
        assert isinstance(d["calibration"]["curve"], list)

    def test_drift_integrato(self):
        d = web_api._calibration_json()
        assert d["drift"]["status"] in ("ok", "drift", "insufficient")

    def test_drift_history_presente(self):
        """La dashboard espone la serie temporale walk-forward del Brier
        rolling per il grafico di tendenza."""
        d = web_api._calibration_json()
        assert "drift_history" in d
        assert isinstance(d["drift_history"], list)


class TestCredits:
    """Crediti the-odds-api: lettura AUTOREVOLE + consumo misurato.

    La telemetria e' per-sport (una cache per lega) e si rinnova a rotazione:
    il valore mostrato deve essere l'ULTIMA lettura, mai il minimo fra file
    vecchi (12/09: reale 452, mostrato 58 -> la guardia proattiva non riduceva
    le leghe e nessun alert scattava).
    """

    def _cache(self, tmp_path, monkeypatch, name, remaining, ts,
               payload=None):
        monkeypatch.setattr(web_api, "DATA_DIR", tmp_path)
        (tmp_path / f"toa_{name}.json").write_text(json.dumps({
            "ts": ts, "remaining": remaining, "remaining_ts": ts,
            "payload": payload or []}))

    def test_usa_la_lettura_piu_recente(self, tmp_path, monkeypatch):
        now = time.time()
        self._cache(tmp_path, monkeypatch, "italy_serie_a", 58, now - 86400)
        self._cache(tmp_path, monkeypatch, "soccer_epl", 275, now)
        d = web_api._credits_json()
        assert d["remaining"] == 275          # autorevole (ultima lettura)
        assert d["remaining_min"] == 58       # diagnostica (file vecchio)
        assert d["status"] == "ok"            # stato sul valore vero

    def test_status_segue_il_valore_autorevole(self, tmp_path, monkeypatch):
        """Una cache vecchia a 5 crediti non deve far dichiarare 'critical'
        un piano che ne ha 30 (era il comportamento del minimo)."""
        now = time.time()
        self._cache(tmp_path, monkeypatch, "italy_serie_a", 5, now - 86400)
        self._cache(tmp_path, monkeypatch, "soccer_epl", 30, now)
        d = web_api._credits_json()
        assert d["remaining"] == 30 and d["status"] == "low"

    def test_nome_cache_scores_e_titolo_lega(self, tmp_path, monkeypatch):
        """`toa_scores_soccer_italy_serie_a.json` -> 'Serie A' (la vecchia
        estrazione lasciava il suffisso '.json' e non trovava il titolo)."""
        self._cache(tmp_path, monkeypatch, "scores_soccer_italy_serie_a",
                    100, time.time())
        d = web_api._credits_json()
        assert d["sports"][0]["sport"] == "Serie A"
        assert d["sports"][0]["sport_key"] == "soccer_italy_serie_a"

    def test_consumo_misurato_dalla_telemetria(self, tmp_path, monkeypatch):
        """400 -> 300 in 24h: 100/giorno MISURATI, non una stima a mano."""
        now = time.time()
        self._cache(tmp_path, monkeypatch, "italy_serie_a", 400, now - 86400)
        self._cache(tmp_path, monkeypatch, "soccer_epl", 300, now)
        # `days_to_reset` dipende dal CALENDARIO (`CREDITS_RESET` = 01/10):
        # senza questo blocco il test scade a fine mese e misurerebbe la data,
        # non il consumo. Il valore e' quello di 10 giorni al reset usato nelle
        # verifiche del progetto (30/09).
        import odds_api
        monkeypatch.setattr(odds_api, "days_to_reset", lambda *a, **k: 10)
        d = web_api._credits_json()
        assert d["consumption_source"] == "measured"
        assert d["estimated_daily_consumption"] == pytest.approx(100.0, abs=0.5)
        assert d["observed_window_hours"] == pytest.approx(24.0, abs=0.1)
        # 300 crediti / 10 giorni al reset: il sostenibile segue il valore vero
        assert d["sustainable_daily"] == pytest.approx(30.0, abs=0.1)

    def test_senza_finestra_utile_resta_stima(self, tmp_path, monkeypatch):
        """Una sola lettura (o tutte nello stesso minuto) non misura il
        ritmo: si dichiara 'heuristic', mai un numero finto."""
        self._cache(tmp_path, monkeypatch, "italy_serie_a", 400, time.time())
        d = web_api._credits_json()
        assert d["consumption_source"] == "heuristic"
        assert d["observed_window_hours"] is None

    def test_senza_telemetria_non_crasha(self, tmp_path, monkeypatch):
        monkeypatch.setattr(web_api, "DATA_DIR", tmp_path)
        d = web_api._credits_json()
        assert d["remaining"] is None and d["sports_cached"] == 0
        assert d["status"] == "ok"


class TestRoutes:
    def test_rotte_esistono(self):
        for route in ("/api/health", "/api/dashboard", "/api/storico", "/api/value",
                      "/api/schedina", "/api/scan", "/api/test_notify",
                      "/api/training", "/api/drift", "/api/calibration"):
            assert route in web_api.ROUTES
        assert "/api/test_notify" in web_api.POST_ROUTES
        assert "/api/auto_bet" in web_api.POST_ROUTES


class TestScanRemoved:
    """Betfair rimosso dal 04/09: /api/scan risponde esplicitamente 503
    con errore betfair_removed, mai dati di catalogo Exchange."""

    def test_scan_rimosso_503(self):
        code, payload = web_api._scan_json()
        assert code == 503
        assert payload["error"] == "betfair_removed"

    def test_scan_con_parametri_rimosso_503(self):
        code, payload = web_api._scan_json({"live": "1"})
        assert code == 503
        assert payload["error"] == "betfair_removed"


class TestAutoBet:
    def test_nessuna_puntata(self, monkeypatch):
        monkeypatch.setattr("auto_bet.run_today_bets", lambda **kw: [])
        d = web_api._auto_bet()
        assert d["ok"] is True and d["piazzate"] == 0

    def test_piazza_e_notifica_agli_admin(self, monkeypatch):
        fake = [{"home": "Birmingham City", "away": "Southampton",
                 "esito_key": "1", "price": 2.68, "stake": 5.0,
                 "mode": "dry-run"}]
        monkeypatch.setattr("auto_bet.run_today_bets", lambda **kw: fake)
        monkeypatch.setenv("ADMIN_CHAT_ID", "111")
        monkeypatch.setenv("QUOTAVERACE_BOT_TOKEN", "tok")
        inviati = []
        monkeypatch.setattr("web_api._telegram_send_message",
                            lambda t, c, text: inviati.append((c, text)))
        d = web_api._auto_bet()
        assert d["piazzate"] == 1
        assert len(inviati) == 1
        assert "PUNTATE AUTOMATICHE" in inviati[0][1]
        assert "DRY-RUN" in inviati[0][1]

    def test_errore_run_restituisce_500(self, monkeypatch):
        def boom(**kw):
            raise RuntimeError("Betfair giu'")
        monkeypatch.setattr("auto_bet.run_today_bets", boom)
        code, payload = web_api._auto_bet()
        assert code == 500


class TestTraining:
    def test_endpoint_risponde(self):
        d = web_api._training_json({"limit": "10"})
        assert "n" in d and "rows" in d
        assert isinstance(d["rows"], list)

    def test_limite_non_valido_non_crasha(self):
        d = web_api._training_json({"limit": "abc"})
        assert "error" in d or "rows" in d


class TestTestNotify:
    def test_disabilitato_senza_chiave(self, monkeypatch):
        monkeypatch.delenv("TEST_NOTIFY_KEY", raising=False)
        code, payload = web_api._test_notify({})
        assert code == 403
        assert payload["error"] == "disabled"

    def test_chiave_errata(self, monkeypatch):
        monkeypatch.setenv("TEST_NOTIFY_KEY", "segreta")
        code, _ = web_api._test_notify({"key": "sbagliata"})
        assert code == 401

    def test_invia_agli_admin(self, monkeypatch):
        monkeypatch.setenv("TEST_NOTIFY_KEY", "segreta")
        monkeypatch.setenv("ADMIN_CHAT_ID", "111, 222")
        monkeypatch.setenv("QUOTAVERACE_BOT_TOKEN", "tok-test")
        inviati = []

        def fake_send(token, chat_id, text):
            inviati.append((chat_id, text))
            return {"ok": True}

        monkeypatch.setattr(web_api, "_telegram_send_message", fake_send)
        code, payload = web_api._test_notify({"key": "segreta"})
        assert code == 200
        assert payload["ok"] is True
        assert payload["destinatari"] == [111, 222]
        assert len(inviati) == 2
        assert "Test notifica" in inviati[0][1]

    def test_errore_invio_riportato(self, monkeypatch):
        monkeypatch.setenv("TEST_NOTIFY_KEY", "segreta")
        monkeypatch.setenv("ADMIN_CHAT_ID", "111")
        monkeypatch.setenv("QUOTAVERACE_BOT_TOKEN", "tok-test")

        def fake_send(token, chat_id, text):
            raise RuntimeError("timeout telegram")

        monkeypatch.setattr(web_api, "_telegram_send_message", fake_send)
        code, payload = web_api._test_notify({"key": "segreta"})
        assert code == 502
        assert payload["results"][0]["ok"] is False

    def test_chat_id_extra(self, monkeypatch):
        monkeypatch.setenv("TEST_NOTIFY_KEY", "segreta")
        monkeypatch.setenv("ADMIN_CHAT_ID", "111")
        monkeypatch.setenv("QUOTAVERACE_BOT_TOKEN", "tok")
        inviati = []

        def fake_send(token, chat_id, text):
            inviati.append(chat_id)
            return {"ok": True}

        monkeypatch.setattr(web_api, "_telegram_send_message", fake_send)
        code, payload = web_api._test_notify({"key": "segreta", "chat_id": "333"})
        assert code == 200
        assert payload["destinatari"] == [111, 333]