"""test_betfair_it.py — provider Betfair Italia (ADM), TUTTI i test OFFLINE.

Perche' esistono: `BetfairItProvider` e' l'unica strada di monetizzazione
legale in Italia (ADM, imposta alla fonte) e il suo valore sta in DUE cose che
un test deve difendere, perche' sbagliarle costa denaro reale:

1. **le regole nazionali** — puntata minima 2,00 EUR e vincita massima 10.000
   EUR per scommessa (compresa la puntata). Un ordine fuori regola non deve
   nemmeno toccare la rete: qui si verifica che il provider sia fail-closed.
2. **la ladder dei prezzi** — un BACK arrotondato per ECCESSO paga piu' del
   dovuto (e' la classe di difetto del 09/10 su SX, dove l'arrotondamento
   rendeva la quota irraggiungibile). Per un BACK si arrotonda per difetto,
   per un LAY per eccesso.

Nessuna rete: `requests.post` e' sostituito in ogni test che arriva all'HTTP, e
i test "fail-closed" verificano che l'HTTP **non** venga chiamato (una spia che
solleva).
"""

from __future__ import annotations

import time

import pytest

import execution_engine as ee


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, data, status_code=200):
        self._data = data
        self.status_code = status_code

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _provider(**kw):
    p = ee.BetfairItProvider(
        kw.get("app_key", "app-test"),
        kw.get("username", "user-test"),
        kw.get("password", "pass-test"),
        kw.get("cert_path", "/tmp/fake.crt"),
        kw.get("key_path", "/tmp/fake.key"),
    )
    p._token = kw.get("token", "sess-test")
    p._token_ts = time.time()
    return p


def _boom(*a, **k):
    raise AssertionError("non deve chiamare la rete")


# ---------------------------------------------------------------------------
# Ladder dei prezzi
# ---------------------------------------------------------------------------

class TestLadder:
    @pytest.mark.parametrize("price,expected", [
        (1.01, 1.01),
        (1.99, 1.99),
        (1.995, 1.99),      # BACK: mai sopra il richiesto
        (2.00, 2.00),
        (2.03, 2.02),       # passo 0.02
        (2.05, 2.04),
        (2.99, 2.98),
        (3.00, 3.00),
        (3.07, 3.05),       # passo 0.05
        (5.99, 5.90),       # passo 0.10
        (9.99, 9.80),       # passo 0.20
        (15.0, 15.00),      # passo 0.50
        (25.0, 25.00),      # passo 1.00
        (45.0, 44.00),      # passo 2.00
        (99.0, 95.00),      # passo 5.00
        (999.0, 990.00),    # passo 10.00
    ])
    def test_back_arrotonda_per_difetto(self, price, expected):
        assert ee.betfair_tick(price, "BACK") == expected

    @pytest.mark.parametrize("price,expected", [
        (1.995, 2.00),
        (2.03, 2.04),
        (3.07, 3.10),
        (5.99, 6.00),
        (2.00, 2.00),      # gia' sulla ladder: nessun movimento
        (1.01, 1.01),
    ])
    def test_lay_arrotonda_per_eccesso(self, price, expected):
        assert ee.betfair_tick(price, "LAY") == expected

    @pytest.mark.parametrize("price", [1.00, 1.005, 0.5, 1000.0, 5000, -3])
    def test_fuori_ladder_ritorna_none(self, price):
        assert ee.betfair_tick(price) is None

    @pytest.mark.parametrize("price", [None, "abc", [], {}])
    def test_prezzo_illeggibile_ritorna_none(self, price):
        assert ee.betfair_tick(price) is None

    def test_il_valore_restituito_e_sempre_sulla_ladder(self):
        # Proprieta' generale: il risultato di un BACK non supera MAI la
        # richiesta e resta nel segmento di partenza.
        p = 1.01
        while p < 1000:
            t = ee.betfair_tick(p, "BACK")
            assert t is not None and t <= p + 1e-9
            assert abs(round(t * 100) - t * 100) < 1e-6
            p = round(p * 1.01, 3)


# ---------------------------------------------------------------------------
# Login certificato
# ---------------------------------------------------------------------------

class TestLogin:
    def test_login_certificato_payload_cert_e_token(self, monkeypatch):
        captured = {}

        def fake_post(url, data=None, headers=None, json=None, timeout=None,
                      cert=None):
            captured.update(url=url, data=data, headers=headers, cert=cert)
            return _Resp({"loginStatus": "SUCCESS", "sessionToken": "sess-cert"})

        monkeypatch.setattr(ee.requests, "post", fake_post)
        p = ee.BetfairItProvider("app-test", "user-test", "pass-test",
                                 "/tmp/fake.crt", "/tmp/fake.key")
        token = p._login()
        assert token == "sess-cert"
        assert captured["url"] == ee.BETFAIR_IT_LOGIN_URL
        assert "identitysso-cert" in captured["url"]
        assert captured["headers"]["X-Application"] == "app-test"
        assert captured["data"] == {"username": "user-test",
                                    "password": "pass-test"}
        assert captured["cert"] == ("/tmp/fake.crt", "/tmp/fake.key")

    def test_token_in_cache_non_richiama_la_rete(self, monkeypatch):
        monkeypatch.setattr(ee.requests, "post", _boom)
        p = _provider()
        assert p._login() == "sess-test"

    def test_login_rifiutato_solleva(self, monkeypatch):
        monkeypatch.setattr(ee.requests, "post",
                            lambda *a, **k: _Resp(
                                {"loginStatus": "INVALID_USERNAME_OR_PASSWORD"}))
        p = ee.BetfairItProvider("app-test", "user-test", "pass-test")
        with pytest.raises(RuntimeError, match="login Betfair.it"):
            p._login()

    def test_login_senza_session_token_solleva(self, monkeypatch):
        monkeypatch.setattr(ee.requests, "post",
                            lambda *a, **k: _Resp({"loginStatus": "SUCCESS"}))
        p = ee.BetfairItProvider("app-test", "user-test", "pass-test")
        with pytest.raises(RuntimeError, match="sessionToken"):
            p._login()

    def test_credenziali_mancanti_sollevano_senza_rete(self, monkeypatch):
        monkeypatch.setattr(ee.requests, "post", _boom)
        p = ee.BetfairItProvider("", "", "")
        with pytest.raises(RuntimeError, match="credenziali Betfair.it"):
            p._login()

    def test_senza_certificato_cert_e_none(self, monkeypatch):
        captured = {}

        def fake_post(url, data=None, headers=None, cert=None, timeout=None,
                      json=None):
            captured["cert"] = cert
            return _Resp({"loginStatus": "SUCCESS", "sessionToken": "t"})

        monkeypatch.setattr(ee.requests, "post", fake_post)
        p = ee.BetfairItProvider("app-test", "user-test", "pass-test")
        p._login()
        assert captured["cert"] is None

    def test_endpoint_rpc_e_quello_italiano(self, monkeypatch):
        captured = {}

        def fake_post(url, data=None, headers=None, json=None, timeout=None,
                      cert=None):
            captured.update(url=url, headers=headers, json=json)
            return _Resp({"result": {"availableToBetBalance": 100.0}})

        monkeypatch.setattr(ee.requests, "post", fake_post)
        p = _provider()
        p.get_balance()
        assert captured["url"] == ee.BETFAIR_IT_API_BASE
        assert "api.betfair.it" in captured["url"]
        assert captured["headers"]["X-Authentication"] == "sess-test"
        assert captured["json"]["method"] == "SportsAPING/v1.0/getAccountFunds"


# ---------------------------------------------------------------------------
# Regole ADM (fail-closed, PRIMA della rete)
# ---------------------------------------------------------------------------

class TestRegoleAdm:
    def test_puntata_sotto_il_minimo_non_tocca_la_rete(self, monkeypatch):
        monkeypatch.setattr(ee.requests, "post", _boom)
        p = _provider()
        res = p.place_limit_order("1.234", 98765, "BACK", 2.0, 1.50)
        assert not res.ok
        assert res.status == "REJECTED"
        assert "minimo ADM" in (res.error or "")
        assert res.size_matched == 0.0

    def test_minimo_rispettato_al_bordo(self, monkeypatch):
        captured = {}

        def fake_post(url, data=None, headers=None, json=None, timeout=None,
                      cert=None):
            captured["json"] = json
            return _Resp({"result": {"status": "SUCCESS", "instructionReports": [
                {"status": "SUCCESS", "betId": "B1",
                 "averagePriceMatched": 2.0, "sizeMatched": 2.0}]}})

        monkeypatch.setattr(ee.requests, "post", fake_post)
        p = _provider()
        res = p.place_limit_order("1.234", 98765, "BACK", 2.0, 2.0)
        assert res.ok and res.bet_id == "B1"
        instr = captured["json"]["params"]["instructions"][0]
        assert instr["limitOrder"]["size"] == 2.0

    def test_vincita_massima_adm(self, monkeypatch):
        monkeypatch.setattr(ee.requests, "post", _boom)
        p = _provider()
        # 5000 * 3.0 = 15.000 di payout: oltre i 10.000 ADM.
        res = p.place_limit_order("1.234", 98765, "BACK", 3.0, 5000.0)
        assert not res.ok and "massimo ADM" in (res.error or "")

    def test_vincita_massima_rispettata(self, monkeypatch):
        captured = {}

        def fake_post(url, data=None, headers=None, json=None, timeout=None,
                      cert=None):
            captured["json"] = json
            return _Resp({"result": {"status": "SUCCESS", "instructionReports": [
                {"status": "SUCCESS", "betId": "B2",
                 "averagePriceMatched": 2.0, "sizeMatched": 3000.0}]}})

        monkeypatch.setattr(ee.requests, "post", fake_post)
        p = _provider()
        res = p.place_limit_order("1.234", 98765, "BACK", 3.0, 3000.0)  # 9.000
        assert res.ok
        # Il prezzo INVIATO e' quello della ladder, non quello grezzo.
        assert captured["json"]["params"]["instructions"][0]["limitOrder"]["price"] == 3.0

    def test_prezzo_non_valido_scarta(self, monkeypatch):
        monkeypatch.setattr(ee.requests, "post", _boom)
        p = _provider()
        for bad in (1.0, 0.9, 2000.0):
            res = p.place_limit_order("1.234", 1, "BACK", bad, 2.0)
            assert not res.ok
            assert res.status == "REJECTED"

    def test_side_non_valido(self, monkeypatch):
        monkeypatch.setattr(ee.requests, "post", _boom)
        p = _provider()
        res = p.place_limit_order("1.234", 1, "MIDDLE", 2.0, 2.0)
        assert not res.ok and "side non valido" in (res.error or "")

    def test_precheck_espone_il_tick(self):
        p = ee.BetfairItProvider("a", "u", "p")
        tick, err = p.precheck_order("BACK", 2.03, 10.0)
        assert err is None and tick == 2.02
        tick, err = p.precheck_order("BACK", 2.03, 1.0)
        assert tick is None and "minimo ADM" in err

    def test_il_prezzo_inviato_e_sempre_quello_della_ladder(self, monkeypatch):
        captured = {}

        def fake_post(url, data=None, headers=None, json=None, timeout=None,
                      cert=None):
            captured["json"] = json
            return _Resp({"result": {"status": "SUCCESS", "instructionReports": [
                {"status": "SUCCESS", "betId": "B3",
                 "averagePriceMatched": 2.02, "sizeMatched": 2.0}]}})

        monkeypatch.setattr(ee.requests, "post", fake_post)
        p = _provider()
        res = p.place_limit_order("1.234", 7, "BACK", 2.037, 2.0)
        assert res.ok
        sent = captured["json"]["params"]["instructions"][0]["limitOrder"]["price"]
        assert sent == 2.02          # arrotondato per difetto, MAI 2.04
        assert sent <= 2.037

    def test_errore_operatore_diventa_ok_false(self, monkeypatch):
        monkeypatch.setattr(ee.requests, "post",
                            lambda *a, **k: _Resp({"result": {
                                "status": "FAILURE",
                                "instructionReports": [{"status": "FAILURE",
                                                        "errorCode": "INSUFFICIENT_FUNDS"}]}}))
        p = _provider()
        res = p.place_limit_order("1.234", 1, "BACK", 2.0, 2.0)
        assert not res.ok
        assert "INSUFFICIENT_FUNDS" in (res.error or "")


# ---------------------------------------------------------------------------
# Settlement
# ---------------------------------------------------------------------------

class TestSettlement:
    def test_list_cleared_orders_payload(self, monkeypatch):
        captured = {}

        def fake_post(url, data=None, headers=None, json=None, timeout=None,
                      cert=None):
            captured["json"] = json
            return _Resp({"result": {"clearedOrders": [
                {"betId": "B1", "betOutcome": "WON", "profit": 1.5,
                 "priceMatched": 1.8}]}})

        monkeypatch.setattr(ee.requests, "post", fake_post)
        p = _provider()
        out = p.list_cleared_orders(bet_ids=["B1", "B2"],
                                    from_iso="2026-10-01T00:00:00Z",
                                    to_iso="2026-10-02T00:00:00Z")
        assert out["clearedOrders"][0]["betOutcome"] == "WON"
        body = captured["json"]
        assert body["method"] == "SportsAPING/v1.0/listClearedOrders"
        params = body["params"]
        assert params["betStatus"] == "SETTLED"
        assert params["groupBy"] == "BET"
        assert params["betIds"] == ["B1", "B2"]
        assert params["settledDateRange"] == {"from": "2026-10-01T00:00:00Z",
                                             "to": "2026-10-02T00:00:00Z"}

    def test_senza_bet_ids_nessun_filtro(self, monkeypatch):
        captured = {}

        def fake_post(url, data=None, headers=None, json=None, timeout=None,
                      cert=None):
            captured["json"] = json
            return _Resp({"result": {}})

        monkeypatch.setattr(ee.requests, "post", fake_post)
        p = _provider()
        p.list_cleared_orders()
        assert "betIds" not in captured["json"]["params"]
        assert "settledDateRange" not in captured["json"]["params"]


# ---------------------------------------------------------------------------
# Factory e architettura
# ---------------------------------------------------------------------------

class TestFactory:
    def test_build_provider_betfair_it_con_credenziali(self, monkeypatch):
        monkeypatch.setattr(ee, "EXECUTION_PROVIDER", "betfair_it")
        monkeypatch.setattr(ee, "EXECUTION_DRY_RUN", False)
        monkeypatch.setattr(ee, "BETFAIR_IT_APP_KEY", "app-x")
        monkeypatch.setattr(ee, "BETFAIR_IT_USERNAME", "user-x")
        monkeypatch.setattr(ee, "BETFAIR_IT_PASSWORD", "pass-x")
        monkeypatch.setattr(ee, "BETFAIR_IT_CERT_PATH", "/tmp/c.crt")
        monkeypatch.setattr(ee, "BETFAIR_IT_KEY_PATH", "/tmp/c.key")
        p = ee.build_provider()
        assert isinstance(p, ee.BetfairItProvider)
        assert p.name == "betfair_it"
        assert p.cert_path == "/tmp/c.crt"
        assert ee._creds_configured() is True

    def test_build_provider_betfair_it_senza_credenziali_e_dry_run(self, monkeypatch):
        monkeypatch.setattr(ee, "EXECUTION_PROVIDER", "betfair_it")
        monkeypatch.setattr(ee, "EXECUTION_DRY_RUN", False)
        monkeypatch.setattr(ee, "BETFAIR_IT_APP_KEY", "")
        monkeypatch.setattr(ee, "BETFAIR_IT_USERNAME", "")
        monkeypatch.setattr(ee, "BETFAIR_IT_PASSWORD", "")
        # `build_provider` legge i globali del modulo: senza credenziali deve
        # degradare a DryRun (nessuna rete), MAI sollevare all'avvio.
        assert isinstance(ee.build_provider(), ee.DryRunProvider)
        assert ee._creds_configured() is False

    def test_dry_run_forzato_vince(self, monkeypatch):
        monkeypatch.setattr(ee, "EXECUTION_PROVIDER", "betfair_it")
        monkeypatch.setattr(ee, "EXECUTION_DRY_RUN", True)
        monkeypatch.setattr(ee, "BETFAIR_IT_APP_KEY", "app-x")
        assert isinstance(ee.build_provider(), ee.DryRunProvider)


class TestArchitettura:
    def test_e_una_sottoclasse_del_protocollo_betfair(self):
        # Non un modulo nuovo: stessa implementazione, due endpoint diversi.
        assert issubclass(ee.BetfairItProvider, ee.BetfairJsonRpcProvider)

    def test_non_duplica_place_orders(self):
        # Il corpo dell'ordine resta UNA definizione (nel padre). Due copie
        # possono divergere: e' la classe di bug del 26/09.
        import inspect
        src = inspect.getsource(ee.BetfairItProvider)
        assert "instructionReports" not in src
        assert '"placeOrders"' not in src and "'placeOrders'" not in src

    def test_endpoint_default_degli_aggregatori_invariati(self):
        p = ee.BetInAsiaBlackProvider("a", "u", "p")
        assert p.login_url == ee.EXECUTION_LOGIN_URL
        assert p.api_base == ee.EXECUTION_API_BASE
        assert "identitysso-cert" not in p.login_url

    def test_nessuna_credenziale_hardcoded(self):
        import inspect
        src = inspect.getsource(ee)
        for pattern in ("BETFAIR_IT_PASSWORD = \"", "BETFAIR_IT_APP_KEY = \""):
            # Le assegnazioni ammesse leggono SOLO da os.getenv (""), mai un
            # valore letterale: il tripwire di test_secret_hygiene fa il resto.
            assert pattern not in src
