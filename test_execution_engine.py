"""Test di execution_engine.py — esecuzione via aggregatore (BetInAsia BLACK/MollyBet).

Copre: interfaccia provider, protocollo JSON-RPC Betfair-compatible (login,
placeOrders con header corretti), DryRunProvider (default senza credenziali),
probe a stake minimo con misura latenza/slippage, log JSONL delle misure,
factory provider da env e sicurezza (nessuna credenziale hardcoded — il
tripwire test_secret_hygiene.py la verifica a livello repo).
"""

import json
import os
import time

import pytest

import execution_engine as ee


# ---------------------------------------------------------------------------
# Factory provider
# ---------------------------------------------------------------------------

class TestBuildProvider:
    def test_senza_credenziali_dry_run(self, monkeypatch):
        monkeypatch.delenv("EXECUTION_APP_KEY", raising=False)
        monkeypatch.delenv("EXECUTION_USERNAME", raising=False)
        monkeypatch.delenv("EXECUTION_PASSWORD", raising=False)
        monkeypatch.setattr(ee, "EXECUTION_DRY_RUN", False)
        monkeypatch.setattr(ee, "EXECUTION_APP_KEY", "")
        monkeypatch.setattr(ee, "EXECUTION_USERNAME", "")
        monkeypatch.setattr(ee, "EXECUTION_PASSWORD", "")
        p = ee.build_provider()
        assert isinstance(p, ee.DryRunProvider)

    def test_dry_run_forzato_con_credenziali(self, monkeypatch):
        monkeypatch.setattr(ee, "EXECUTION_DRY_RUN", True)
        monkeypatch.setattr(ee, "EXECUTION_APP_KEY", "key-test")
        monkeypatch.setattr(ee, "EXECUTION_USERNAME", "user-test")
        monkeypatch.setattr(ee, "EXECUTION_PASSWORD", "pass-test")
        assert isinstance(ee.build_provider(), ee.DryRunProvider)

    def test_provider_betinasia_con_credenziali(self, monkeypatch):
        monkeypatch.setattr(ee, "EXECUTION_DRY_RUN", False)
        monkeypatch.setattr(ee, "EXECUTION_PROVIDER", "betinasia")
        monkeypatch.setattr(ee, "EXECUTION_APP_KEY", "key-test")
        monkeypatch.setattr(ee, "EXECUTION_USERNAME", "user-test")
        monkeypatch.setattr(ee, "EXECUTION_PASSWORD", "pass-test")
        p = ee.build_provider()
        assert isinstance(p, ee.BetInAsiaBlackProvider)

    def test_provider_mollybet_con_credenziali(self, monkeypatch):
        monkeypatch.setattr(ee, "EXECUTION_DRY_RUN", False)
        monkeypatch.setattr(ee, "EXECUTION_PROVIDER", "mollybet")
        monkeypatch.setattr(ee, "EXECUTION_APP_KEY", "key-test")
        monkeypatch.setattr(ee, "EXECUTION_USERNAME", "user-test")
        monkeypatch.setattr(ee, "EXECUTION_PASSWORD", "pass-test")
        p = ee.build_provider()
        assert isinstance(p, ee.MollyBetProvider)

    def test_provider_sconosciuto_dry_run(self, monkeypatch):
        monkeypatch.setattr(ee, "EXECUTION_DRY_RUN", False)
        monkeypatch.setattr(ee, "EXECUTION_PROVIDER", "altro")
        monkeypatch.setattr(ee, "EXECUTION_APP_KEY", "key-test")
        monkeypatch.setattr(ee, "EXECUTION_USERNAME", "user-test")
        monkeypatch.setattr(ee, "EXECUTION_PASSWORD", "pass-test")
        assert isinstance(ee.build_provider(), ee.DryRunProvider)


# ---------------------------------------------------------------------------
# Protocollo JSON-RPC Betfair-compatible
# ---------------------------------------------------------------------------

def _fake_response(payload, status_code=200):
    class R:
        def __init__(self):
            self.status_code = status_code

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"http {self.status_code}")

        def json(self):
            return payload
    return R()


class TestJsonRpcProtocol:
    def test_login_invia_credenziali_e_come_headers(self, monkeypatch):
        captured = {}

        def fake_post(url, data=None, headers=None, json=None, timeout=None):
            captured["url"] = url
            captured["data"] = data
            captured["headers"] = headers
            return _fake_response({"status": "SUCCESS", "token": "tok-test"})

        monkeypatch.setattr(ee.requests, "post", fake_post)
        p = ee.BetInAsiaBlackProvider("app-test", "user-test", "pass-test")
        tok = p._login()
        assert tok == "tok-test"
        assert captured["headers"]["X-Application"] == "app-test"
        assert captured["data"]["username"] == "user-test"
        assert captured["data"]["password"] == "pass-test"

    def test_login_fallito_raise(self, monkeypatch):
        monkeypatch.setattr(ee.requests, "post",
                            lambda *a, **k: _fake_response(
                                {"status": "FAIL", "error": "INVALID_USERNAME_OR_PASSWORD"}))
        p = ee.BetInAsiaBlackProvider("app-test", "user-test", "pass-test")
        with pytest.raises(RuntimeError, match="login"):
            p._login()

    def test_rpc_place_orders_payload_e_header_auth(self, monkeypatch):
        captured = {}

        def fake_post(url, data=None, headers=None, json=None, timeout=None):
            captured["json"] = json
            captured["headers"] = headers
            return _fake_response({"result": {
                "status": "SUCCESS",
                "instructionReports": [{
                    "status": "SUCCESS", "betId": "123456",
                    "averagePriceMatched": 2.0, "sizeMatched": 1.0}]}})

        monkeypatch.setattr(ee.requests, "post", fake_post)
        p = ee.BetInAsiaBlackProvider("app-test", "user-test", "pass-test")
        p._token = "tok-test"
        p._token_ts = time.time()
        res = p.place_limit_order("1.234", 98765, "BACK", 2.0, 1.0)
        assert res.ok
        assert res.bet_id == "123456"
        assert res.price_matched == 2.0
        assert res.size_matched == 1.0
        body = captured["json"]
        assert body["method"] == "SportsAPING/v1.0/placeOrders"
        instr = body["params"]["instructions"][0]
        assert instr["selectionId"] == 98765
        assert instr["side"] == "BACK"
        assert instr["limitOrder"]["size"] == 1.0
        assert instr["limitOrder"]["price"] == 2.0
        assert captured["headers"]["X-Authentication"] == "tok-test"

    def test_rpc_errore_api_raise(self, monkeypatch):
        monkeypatch.setattr(ee.requests, "post",
                            lambda *a, **k: _fake_response(
                                {"error": {"message": "NO_LIQUIDITY"}}))
        p = ee.BetInAsiaBlackProvider("app-test", "user-test", "pass-test")
        p._token = "tok-test"
        p._token_ts = time.time()
        with pytest.raises(RuntimeError, match="NO_LIQUIDITY"):
            p._rpc("listMarketBook", {})

    def test_side_non_valido_non_chiama_rete(self, monkeypatch):
        def boom(*a, **k):
            raise AssertionError("non deve chiamare la rete")
        monkeypatch.setattr(ee.requests, "post", boom)
        p = ee.BetInAsiaBlackProvider("app-test", "user-test", "pass-test")
        res = p.place_limit_order("1.234", 98765, "MIDDLE", 2.0, 1.0)
        assert not res.ok
        assert "side non valido" in (res.error or "")


# ---------------------------------------------------------------------------
# Discovery mercati
# ---------------------------------------------------------------------------

class TestDiscovery:
    def test_catalogue_payload_e_parsing(self, monkeypatch):
        captured = {}

        def fake_post(url, data=None, headers=None, json=None, timeout=None):
            captured["json"] = json
            return _fake_response({"result": [{
                "marketId": "1.200", "marketName": "Match Odds",
                "totalMatched": 5000.0,
                "event": {"id": "900", "name": "Inter vs Milan",
                           "countryCode": "IT", "openDate": "2026-09-07T19:00:00Z"},
                "runners": [{"selectionId": 101, "runnerName": "Inter"},
                             {"selectionId": 102, "runnerName": "Milan"},
                             {"selectionId": 103, "runnerName": "Draw"}],
            }]})

        monkeypatch.setattr(ee.requests, "post", fake_post)
        p = ee.BetInAsiaBlackProvider("app-test", "user-test", "pass-test")
        p._token = "tok-test"
        p._token_ts = time.time()
        markets = p.list_market_catalogue()
        assert len(markets) == 1
        m = markets[0]
        assert m["market_id"] == "1.200"
        assert m["event_name"] == "Inter vs Milan"
        assert m["runners"][0] == {"selection_id": 101, "name": "Inter"}
        body = captured["json"]
        assert body["method"] == "SportsAPING/v1.0/listMarketCatalogue"
        f = body["params"]["filter"]
        assert f["eventTypeIds"] == ["1"]
        assert f["marketTypeCodes"] == ["MATCH_ODDS"]
        assert "from" in f["marketStartTime"] and "to" in f["marketStartTime"]
        assert body["params"]["sort"] == "FIRST_TO_START"

    def test_dry_run_catalogue_simulato(self):
        p = ee.DryRunProvider()
        markets = p.list_market_catalogue()
        assert len(markets) == 2
        assert markets[0]["market_id"] == "1.1001"
        assert len(markets[0]["runners"]) == 3

    def test_discover_markets_fail_closed(self, monkeypatch):
        class Boom:
            name = "boom"

            def list_market_catalogue(self, **k):
                raise RuntimeError("API giu'")

        engine = ee.ExecutionEngine(provider=Boom())  # type: ignore[arg-type]
        assert engine.discover_markets() == []


# ---------------------------------------------------------------------------
# DryRunProvider
# ---------------------------------------------------------------------------

class TestDryRun:
    def test_place_order_simulato(self):
        p = ee.DryRunProvider(latency_ms=15.0, slippage=-0.01)
        res = p.place_limit_order("1.234", 1, "BACK", 2.0, 1.0)
        assert res.ok
        assert res.status == "dry-run"
        assert res.price_matched == pytest.approx(1.99)
        assert res.latency_ms == pytest.approx(15.0)
        assert p.cancel_order("1.234", res.bet_id)

    def test_balance_simulato(self):
        assert ee.DryRunProvider().get_balance()["availableBalance"] == 1000.0


# ---------------------------------------------------------------------------
# Probe latenza/slippage
# ---------------------------------------------------------------------------

class TestProbe:
    def test_probe_dry_run_misura_e_logga(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ee, "MEASUREMENTS_LOG", tmp_path / "m.jsonl")
        engine = ee.ExecutionEngine(provider=ee.DryRunProvider(
            latency_ms=20.0, slippage=-0.01))
        res = engine.probe("1.234", 98765, price=2.0, stake=1.0)
        assert res.ok
        assert res.provider == "dry_run"
        assert res.stake == pytest.approx(1.0)
        assert res.price_requested == pytest.approx(2.0)
        assert res.price_matched == pytest.approx(1.99)
        assert res.slippage == pytest.approx(-0.01)
        assert res.latency_ms == pytest.approx(20.0)
        assert res.slippage_vs_best is None  # dry-run: best = None
        assert res.bet_id and res.bet_id.startswith("dry-")  # propagato
        # log JSONL scritto (con bet_id)
        lines = tmp_path.joinpath("m.jsonl").read_text().splitlines()
        assert len(lines) == 1
        row = json.loads(lines[0])
        assert row["provider"] == "dry_run"
        assert row["market_id"] == "1.234"
        assert row["latency_ms"] == pytest.approx(20.0)
        assert row["bet_id"] == res.bet_id

    def test_probe_stake_minimo_default(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ee, "MEASUREMENTS_LOG", tmp_path / "m.jsonl")
        engine = ee.ExecutionEngine(provider=ee.DryRunProvider())
        res = engine.probe("1.234", 98765, price=2.5)
        assert res.stake == pytest.approx(engine.min_stake)
        assert engine.min_stake == pytest.approx(ee.EXECUTION_MIN_STAKE_EUR)

    def test_probe_stake_cappato(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ee, "MEASUREMENTS_LOG", tmp_path / "m.jsonl")
        monkeypatch.setattr(ee, "EXECUTION_MAX_STAKE_EUR", 10.0)
        engine = ee.ExecutionEngine(provider=ee.DryRunProvider(),
                                    min_stake=100.0)
        assert engine.min_stake == pytest.approx(10.0)

    def test_probe_con_provider_reale_mockato(self, monkeypatch, tmp_path):
        """Probe su provider reale: prezzo best disponibile + slippage dal fill."""
        monkeypatch.setattr(ee, "MEASUREMENTS_LOG", tmp_path / "m.jsonl")
        calls = []

        class FakeProv(ee.BetInAsiaBlackProvider):
            def best_back_price(self, market_id, selection_id):
                return 2.10

            def place_limit_order(self, market_id, selection_id, side,
                                  price, size, persistence="LAPSE"):
                calls.append((market_id, selection_id, price, size))
                return ee.OrderResult(True, "bet-1", "SUCCESS", price, 2.08,
                                      1.0, 40.0)

            def cancel_order(self, market_id, bet_id):
                return True

        engine = ee.ExecutionEngine(provider=FakeProv("k", "u", "p"))
        res = engine.probe("1.234", 98765, stake=1.0)
        assert res.ok
        assert res.price_best_available == pytest.approx(2.10)
        assert res.price_requested == pytest.approx(2.10)  # usa best
        assert res.price_matched == pytest.approx(2.08)
        assert res.slippage == pytest.approx(-0.02)
        assert res.slippage_vs_best == pytest.approx(-0.02)
        assert res.bet_id == "bet-1"      # propagato da OrderResult
        assert len(calls) == 1

    def test_probe_errore_fail_closed(self, monkeypatch, tmp_path):
        """Un'eccezione del provider non propaga: ProbeResult con error."""
        monkeypatch.setattr(ee, "MEASUREMENTS_LOG", tmp_path / "m.jsonl")

        class Boom:
            name = "boom"
            min_stake = 1.0

            def best_back_price(self, *a, **k):
                raise RuntimeError("API giu'")

            def place_limit_order(self, *a, **k):
                raise RuntimeError("API giu'")

            def cancel_order(self, *a, **k):
                return True

        engine = ee.ExecutionEngine(provider=Boom())  # type: ignore[arg-type]
        res = engine.probe("1.234", 98765, price=2.0)
        assert not res.ok
        assert "API giu'" in (res.error or "")
        # anche il fallimento viene loggato
        assert tmp_path.joinpath("m.jsonl").exists()


# ---------------------------------------------------------------------------
# Conversioni prezzo/stake Smarkets
# ---------------------------------------------------------------------------

class TestSmarketsConversions:
    def test_decimal_to_prob_bps(self):
        # quota 2.0 -> 50% -> 5000 bps; 200.0 -> 0.5% -> 50 bps (sample ufficiale)
        assert ee.decimal_to_prob_bps(2.0) == 5000
        assert ee.decimal_to_prob_bps(200.0) == 50
        assert ee.decimal_to_prob_bps(1.5) == 6667  # round(10000/1.5)

    def test_quota_non_valida_raise(self):
        with pytest.raises(ValueError):
            ee.decimal_to_prob_bps(1.0)
        with pytest.raises(ValueError):
            ee.decimal_to_prob_bps(0.5)

    def test_prob_bps_to_decimal(self):
        assert ee.prob_bps_to_decimal(5000) == 2.0
        assert ee.prob_bps_to_decimal(50) == 200.0

    def test_stake_to_quantity(self):
        assert ee.stake_to_quantity(1.0) == 10000
        assert ee.stake_to_quantity(50.0) == 500000  # 50 GBP -> 500000 (sample)

    def test_quantity_to_stake(self):
        assert ee.quantity_to_stake(10000) == 1.0
        assert ee.quantity_to_stake(500000) == 50.0


# ---------------------------------------------------------------------------
# Autenticazione Smarkets (POST sessions/)
# ---------------------------------------------------------------------------

class TestSmarketsAuth:
    def test_login_sessions_payload_e_token(self, monkeypatch):
        captured = {"calls": 0}

        def fake_post(url, data=None, headers=None, json=None, timeout=None):
            captured["calls"] += 1
            captured["url"] = url
            captured["json"] = json
            return _fake_response({"token": "tok-sm"})

        monkeypatch.setattr(ee.requests, "post", fake_post)
        p = ee.SmarketsProvider("user-sm", "pass-sm")
        assert p._login() == "tok-sm"
        assert captured["url"].endswith("sessions/")
        assert captured["json"] == {"username": "user-sm", "password": "pass-sm"}
        # token in cache: la seconda chiamata non fa rete
        assert p._login() == "tok-sm"
        assert captured["calls"] == 1

    def test_login_fallito_raise(self, monkeypatch):
        monkeypatch.setattr(ee.requests, "post",
                            lambda *a, **k: _fake_response(
                                {"error_type": "INVALID_USERNAME_OR_PASSWORD"}))
        p = ee.SmarketsProvider("user-sm", "pass-sm")
        with pytest.raises(RuntimeError, match="login"):
            p._login()

    def test_login_senza_credenziali_raise(self, monkeypatch):
        def boom(*a, **k):
            raise AssertionError("non deve chiamare la rete")
        monkeypatch.setattr(ee.requests, "post", boom)
        p = ee.SmarketsProvider("", "")
        with pytest.raises(RuntimeError, match="Smarkets"):
            p._login()

    def test_headers_usano_session_token(self, monkeypatch):
        def boom(*a, **k):
            raise AssertionError("non deve chiamare la rete (token in cache)")
        monkeypatch.setattr(ee.requests, "post", boom)
        p = ee.SmarketsProvider("user-sm", "pass-sm")
        p._token = "tok-cache"
        p._token_ts = time.time()
        assert p._headers()["Authorization"] == "Session-Token tok-cache"


# ---------------------------------------------------------------------------
# Ordini Smarkets (POST orders/)
# ---------------------------------------------------------------------------

class TestSmarketsOrders:
    def test_place_back_ordine_payload_e_parsing(self, monkeypatch):
        captured = {}

        def fake_post(url, data=None, headers=None, json=None, timeout=None):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return _fake_response({"order": {
                "id": "ord-1", "status": "filled",
                "average_executed_price": 5000, "executed_quantity": 10000}})

        monkeypatch.setattr(ee.requests, "post", fake_post)
        p = ee.SmarketsProvider("user-sm", "pass-sm")
        p._token = "tok-sm"
        p._token_ts = time.time()
        res = p.place_limit_order("7289490", 24174814, "BACK", 2.0, 1.0)
        assert res.ok
        assert res.bet_id == "ord-1"
        assert res.status == "FILLED"
        assert res.price_requested == pytest.approx(2.0)
        assert res.price_matched == pytest.approx(2.0)   # 5000 bps -> 2.0
        assert res.size_matched == pytest.approx(1.0)    # 10000 qty -> 1.0
        body = captured["json"]
        assert body["market_id"] == "7289490"
        assert body["contract_id"] == 24174814
        assert body["side"] == "buy"                   # BACK -> buy
        assert body["price"] == 5000                    # 2.0 decimale -> bps
        assert body["quantity"] == 10000                # 1.0 EUR -> qty
        assert "reference_id" in body
        assert captured["headers"]["Authorization"] == "Session-Token tok-sm"

    def test_place_lay_mappa_sell(self, monkeypatch):
        captured = {}

        def fake_post(url, data=None, headers=None, json=None, timeout=None):
            captured["json"] = json
            return _fake_response({"order": {"id": "o2", "status": "created"}})

        monkeypatch.setattr(ee.requests, "post", fake_post)
        p = ee.SmarketsProvider("u", "p")
        p._token = "tok"
        p._token_ts = time.time()
        res = p.place_limit_order("m", 2, "LAY", 2.0, 1.0)
        assert res.ok
        assert captured["json"]["side"] == "sell"

    def test_place_side_non_valido_non_chiama_rete(self, monkeypatch):
        def boom(*a, **k):
            raise AssertionError("non deve chiamare la rete")
        monkeypatch.setattr(ee.requests, "post", boom)
        p = ee.SmarketsProvider("u", "p")
        res = p.place_limit_order("m", 2, "MIDDLE", 2.0, 1.0)
        assert not res.ok
        assert "side non valido" in (res.error or "")

    def test_place_quota_non_valida_non_chiama_rete(self, monkeypatch):
        def boom(*a, **k):
            raise AssertionError("non deve chiamare la rete")
        monkeypatch.setattr(ee.requests, "post", boom)
        p = ee.SmarketsProvider("u", "p")
        res = p.place_limit_order("m", 2, "BACK", 1.0, 1.0)
        assert not res.ok
        assert "quota" in (res.error or "").lower()

    def test_place_errore_api_fail_closed(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("HTTP 500")
        monkeypatch.setattr(ee.requests, "post", boom)
        p = ee.SmarketsProvider("u", "p")
        p._token = "tok"
        p._token_ts = time.time()
        res = p.place_limit_order("m", 2, "BACK", 2.0, 1.0)
        assert not res.ok
        assert "HTTP 500" in (res.error or "")
        assert res.latency_ms >= 0


# ---------------------------------------------------------------------------
# Quote Smarkets (GET markets/<id>/quotes/)
# ---------------------------------------------------------------------------

class TestSmarketsQuotes:
    def test_best_back_price_da_quote(self, monkeypatch):
        monkeypatch.setattr(ee.requests, "get",
                            lambda *a, **k: _fake_response(
                                {"24174814": {"buy": {"price": 5000,
                                                        "quantity": 100}}}))
        p = ee.SmarketsProvider("u", "p")
        p._token = "tok"
        p._token_ts = time.time()
        assert p.best_back_price("7289490", 24174814) == pytest.approx(2.0)

    def test_best_back_price_lista_entrate(self, monkeypatch):
        # formato lista: primo elemento = migliore
        monkeypatch.setattr(ee.requests, "get",
                            lambda *a, **k: _fake_response(
                                {"1": {"buy": [{"price": 6667, "quantity": 10},
                                                  {"price": 6600, "quantity": 50}]}}))
        p = ee.SmarketsProvider("u", "p")
        p._token = "tok"
        p._token_ts = time.time()
        assert p.best_back_price("m", 1) == pytest.approx(1.50)

    def test_best_back_price_senza_quote_none(self, monkeypatch):
        monkeypatch.setattr(ee.requests, "get", lambda *a, **k: _fake_response({}))
        p = ee.SmarketsProvider("u", "p")
        p._token = "tok"
        p._token_ts = time.time()
        assert p.best_back_price("m", 999) is None

    def test_best_back_price_errore_none(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("API giu'")
        monkeypatch.setattr(ee.requests, "get", boom)
        p = ee.SmarketsProvider("u", "p")
        p._token = "tok"
        p._token_ts = time.time()
        assert p.best_back_price("m", 1) is None


# ---------------------------------------------------------------------------
# Discovery mercati Smarkets (events/ + markets/ + contracts/)
# ---------------------------------------------------------------------------

class TestSmarketsCatalogue:
    def test_catalogue_football_discovery(self, monkeypatch):
        events = {"events": [{
            "id": "7289490", "name": "Inter vs Milan",
            "start_datetime": "2026-09-07T19:00:00Z",
            "type": "football_match"}]}
        markets = {"markets": [{
            "id": "111", "name": "Match Odds", "type": "match_odds",
            "contracts": [{"id": "24174814", "name": "Home", "volume": 1000.0},
                           {"id": "24174815", "name": "Draw", "volume": 200.0}]}]}
        contracts = {"contracts": [
            {"id": "24174814", "name": "Home"},
            {"id": "24174815", "name": "Draw"},
            {"id": "24174816", "name": "Away"}]}
        calls = []

        def fake_get(url, params=None, headers=None, timeout=None):
            calls.append((url, params))
            if url.endswith("events/"):
                return _fake_response(events)
            if url.endswith("contracts/"):
                return _fake_response(contracts)
            return _fake_response(markets)

        monkeypatch.setattr(ee.requests, "get", fake_get)
        p = ee.SmarketsProvider("u", "p")
        p._token = "tok"
        p._token_ts = time.time()
        out = p.list_market_catalogue()
        assert len(out) == 1
        m = out[0]
        assert m["market_id"] == "111"
        assert m["market_name"] == "Match Odds"
        assert m["event_name"] == "Inter vs Milan"
        assert m["event_id"] == "7289490"
        assert m["open_date"] == "2026-09-07T19:00:00Z"
        assert m["total_matched"] == pytest.approx(1200.0)  # somma volumi
        assert len(m["runners"]) == 3
        assert m["runners"][0] == {"selection_id": "24174814", "name": "Home"}
        # il primo params filtra gli eventi calcio (football_match)
        ev_url, ev_params = calls[0]
        assert ev_url.endswith("events/")
        assert "football_match" in ev_params["types"]
        assert ev_params["states"] == "upcoming"

    def test_catalogue_filtra_market_type(self, monkeypatch):
        events = {"events": [{"id": "e1", "name": "X vs Y",
                               "start_datetime": "2026-09-07T20:00:00Z"}]}
        markets = {"markets": [
            {"id": "111", "name": "Match Odds", "type": "match_odds"},
            {"id": "222", "name": "Over/Under 2.5", "type": "over_under_2_5"}]}

        def fake_get(url, params=None, headers=None, timeout=None):
            if url.endswith("events/"):
                return _fake_response(events)
            if url.endswith("contracts/"):
                return _fake_response({"contracts": []})
            return _fake_response(markets)

        monkeypatch.setattr(ee.requests, "get", fake_get)
        p = ee.SmarketsProvider("u", "p")
        p._token = "tok"
        p._token_ts = time.time()
        out = p.list_market_catalogue()
        # solo il match_odds (1X2), l'OU2.5 viene escluso
        assert [m["market_id"] for m in out] == ["111"]


# ---------------------------------------------------------------------------
# Account e cancel Smarkets
# ---------------------------------------------------------------------------

class TestSmarketsAccount:
    def test_get_balance_primo_account(self, monkeypatch):
        monkeypatch.setattr(ee.requests, "get",
                            lambda *a, **k: _fake_response(
                                {"accounts": [{"account_id": "a1",
                                                "available_balance": 1234.5}]}))
        p = ee.SmarketsProvider("u", "p")
        p._token = "tok"
        p._token_ts = time.time()
        bal = p.get_balance()
        assert bal["account_id"] == "a1"
        assert bal["available_balance"] == 1234.5

    def test_cancel_order_delete(self, monkeypatch):
        captured = {}

        def fake_delete(url, headers=None, timeout=None):
            captured["url"] = url
            captured["headers"] = headers
            return _fake_response({}, status_code=204)

        monkeypatch.setattr(ee.requests, "delete", fake_delete)
        p = ee.SmarketsProvider("u", "p")
        p._token = "tok"
        p._token_ts = time.time()
        assert p.cancel_order("m", "ord-1")
        assert captured["url"].endswith("orders/ord-1/")
        assert captured["headers"]["Authorization"] == "Session-Token tok"

    def test_cancel_order_errore_false(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("API giu'")
        monkeypatch.setattr(ee.requests, "delete", boom)
        p = ee.SmarketsProvider("u", "p")
        p._token = "tok"
        p._token_ts = time.time()
        assert not p.cancel_order("m", "ord-1")


# ---------------------------------------------------------------------------
# Factory provider Smarkets
# ---------------------------------------------------------------------------

class TestSmarketsFactory:
    def test_provider_smarkets_con_credenziali(self, monkeypatch):
        monkeypatch.setattr(ee, "EXECUTION_DRY_RUN", False)
        monkeypatch.setattr(ee, "EXECUTION_PROVIDER", "smarkets")
        monkeypatch.setattr(ee, "SMARKETS_USERNAME", "user-sm")
        monkeypatch.setattr(ee, "SMARKETS_PASSWORD", "pass-sm")
        p = ee.build_provider()
        assert isinstance(p, ee.SmarketsProvider)
        assert p.username == "user-sm"
        assert p.password == "pass-sm"

    def test_smarkets_senza_credenziali_dry_run(self, monkeypatch):
        monkeypatch.setattr(ee, "EXECUTION_DRY_RUN", False)
        monkeypatch.setattr(ee, "EXECUTION_PROVIDER", "smarkets")
        monkeypatch.setattr(ee, "SMARKETS_USERNAME", "")
        monkeypatch.setattr(ee, "SMARKETS_PASSWORD", "")
        assert isinstance(ee.build_provider(), ee.DryRunProvider)

    def test_smarkets_dry_run_forzato(self, monkeypatch):
        monkeypatch.setattr(ee, "EXECUTION_DRY_RUN", True)
        monkeypatch.setattr(ee, "EXECUTION_PROVIDER", "smarkets")
        monkeypatch.setattr(ee, "SMARKETS_USERNAME", "u")
        monkeypatch.setattr(ee, "SMARKETS_PASSWORD", "p")
        assert isinstance(ee.build_provider(), ee.DryRunProvider)

    def test_creds_configured_smarkets(self, monkeypatch):
        monkeypatch.setattr(ee, "EXECUTION_PROVIDER", "smarkets")
        monkeypatch.setattr(ee, "SMARKETS_USERNAME", "u")
        monkeypatch.setattr(ee, "SMARKETS_PASSWORD", "p")
        assert ee._creds_configured()
        monkeypatch.setattr(ee, "SMARKETS_PASSWORD", "")
        assert not ee._creds_configured()

    def test_cli_provider_smarkets_seleziona_provider(self, monkeypatch, capsys):
        monkeypatch.setattr(ee, "EXECUTION_DRY_RUN", False)
        monkeypatch.setattr(ee, "EXECUTION_PROVIDER", "")
        monkeypatch.setattr(ee, "SMARKETS_USERNAME", "u")
        monkeypatch.setattr(ee, "SMARKETS_PASSWORD", "p")
        prev = ee.EXECUTION_PROVIDER
        try:
            code = ee.main(["--status", "--provider", "smarkets"])
        finally:
            ee.EXECUTION_PROVIDER = prev
            os.environ.pop("EXECUTION_PROVIDER", None)
        assert code == 0
        data = json.loads(capsys.readouterr().out)
        assert data["provider"] == "smarkets"
        assert data["creds"] is True

    def test_cli_balance_dry_run(self, monkeypatch, capsys):
        """--balance senza credenziali: DryRun, saldo simulato, exit 0."""
        monkeypatch.setattr(ee, "EXECUTION_DRY_RUN", True)
        prev = ee.EXECUTION_PROVIDER
        try:
            code = ee.main(["--balance", "--dry-run"])
        finally:
            ee.EXECUTION_PROVIDER = prev
            os.environ.pop("EXECUTION_PROVIDER", None)
        assert code == 0
        data = json.loads(capsys.readouterr().out)
        assert data["dry_run"] is True
        assert data["availableBalance"] == 1000.0

    def test_cli_balance_errore_fail_closed(self, monkeypatch, capsys):
        """--balance con errore del provider: exit 1, mai eccezioni."""

        class _Boom:
            name = "boom"

            def get_balance(self):
                raise RuntimeError("api giu'")

        monkeypatch.setattr(ee, "build_provider", lambda: _Boom())
        assert ee.main(["--balance"]) == 1
        assert "ERRORE" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Conversioni prezzo/stake SX Bet
# ---------------------------------------------------------------------------

SX_KEY_TEST = "key-sx-test"
SX_PK_TEST = "0x" + "11" * 32
SX_STEP = 125 * 10 ** 15
SX_MH = "0xbb4826699a0c7d80264e31c48ac34f1a5c94cd4f451fa38033af5b5979b6390e"
SX_USDC_ADDR = "0x6629Ce1Cf35Cc1329ebB4F63202F3f197b3F050B"


def _sx_meta():
    return {"status": "success", "data": {
        "chainId": 4162,
        "domain": {"name": "OBv3 Escrow", "version": "1", "chainId": 4162,
                    "verifyingContract":
                    "0x890482680C3a0116aBB003B17e7D694D13a6c1eB"},
        "activeAsset": {"symbol": "USDC", "baseToken": SX_USDC_ADDR,
                        "escrowAddress":
                        "0x890482680C3a0116aBB003B17e7D694D13a6c1eB",
                        "decimals": 6},
        "oddsLadderStepSize": 125,
        "limits": {"orderSizeMinimum": "1",
                    "orderSizeMinimumBaseUnits": "1000000"},
    }}


def _sx_snapshot(mh=SX_MH):
    return {"status": "success", "data": {
        "marketHash": mh,
        "outcomeOne": [{"percentageOdds": "31500000000000000000",
                         "size": "63947299"}],
        "outcomeTwo": [{"percentageOdds": "70875000000000000000",
                         "size": "270228428"}],
        "version": "v1"}}


def _fake_get_sx(payloads):
    """requests.get guidato dal suffisso del path."""
    def fake_get(url, params=None, headers=None, timeout=None):
        for suffix, payload in payloads.items():
            if str(url).endswith(suffix):
                return _fake_response(payload)
        raise AssertionError(f"GET inatteso: {url}")
    return fake_get


class TestSxBetConversions:
    def test_pct_scaled_to_decimal(self):
        # 31.5% -> quota 3.1746 (book live: best level 31500000000000000000)
        assert ee.pct_scaled_to_decimal(31500000000000000000) \
            == pytest.approx(3.1746)
        # accetta anche stringhe
        assert ee.pct_scaled_to_decimal("50000000000000000000") == 2.0
        assert ee.pct_scaled_to_decimal(0) == 0.0
        assert ee.pct_scaled_to_decimal("spazzatura") == 0.0

    def test_decimal_to_pct_scaled_sulla_ladder(self):
        # quota 2.0 -> 50%; 3.1746 -> 31.5% esatto sulla ladder
        assert ee.decimal_to_pct_scaled(2.0, SX_STEP) \
            == 50000000000000000000
        assert ee.decimal_to_pct_scaled(3.1746, SX_STEP) \
            == 31500000000000000000
        # floor alla ladder: 1/2.9 = 34.48% -> gradino 34.375% (mai sotto quota)
        assert ee.decimal_to_pct_scaled(2.9, SX_STEP) \
            == 34375000000000000000

    def test_quota_non_valida_raise(self):
        with pytest.raises(ValueError):
            ee.decimal_to_pct_scaled(1.0, SX_STEP)
        with pytest.raises(ValueError):
            ee.decimal_to_pct_scaled(0.7, SX_STEP)

    def test_ladder_step_scaled(self):
        assert ee.sx_ladder_step_scaled(125) == 125000000000000000
        assert ee.sx_ladder_step_scaled(0) == 10 ** 15  # difensivo

    def test_stake_units(self):
        assert ee.stake_to_sx_units(1.0) == 1000000
        assert ee.stake_to_sx_units(0.0) == 1  # difensivo, poi il provider blocca
        assert ee.sx_units_to_stake(1000000) == 1.0
        assert ee.sx_units_to_stake("5000000") == 5.0
        assert ee.sx_units_to_stake(None) == 0.0

    def test_levels_to_decimal(self):
        levels = ee._sx_levels_to_decimal(_sx_snapshot()["data"]["outcomeOne"])
        assert levels[0]["price"] == pytest.approx(3.1746)
        assert levels[0]["size"] == pytest.approx(63.9473)
        assert ee._sx_levels_to_decimal("x") == []


class TestRoundTripLadder:
    """Round-trip libro<->ordine: e' la causa radice del fill-rate (10/10/2026).

    Se il prezzo riportato dal book e' MAGGIORE della quota reale, l'encoder
    (floor alla ladder) scende di un gradino e l'ordine IOC chiede PIU' della
    quota disponibile -> `CANCELLED / NO_LIQUIDITY`. Questi test bloccano
    quella regressione.
    """

    LO, HI = 1.30, 2.60  # banda di lavoro del bot (fascia quota)

    def test_round_trip_lossless_su_tutta_la_banda(self):
        tot = 0
        for k in range(1, 800):
            p = k * SX_STEP
            dec = ee.pct_scaled_to_decimal(p)
            if not (self.LO <= dec <= self.HI):
                continue
            tot += 1
            # il prezzo riportato NON supera mai la quota reale della ladder
            assert dec <= ee.SX_PROB_SCALE / p + 1e-9
            # e l'encoder lo riporta ESATTAMENTE al gradino di partenza
            assert ee.decimal_to_pct_scaled(dec, SX_STEP) == p, (
                f"gradino perso: p={p} dec={dec}")
        assert tot > 250, "banda troppo stretta: il test non misura nulla"

    def test_non_arrotonda_per_eccesso(self):
        # casi reali che con l'arrotondamento finivano un gradino piu' severi
        p = 309 * SX_STEP                     # quota reale 2.588996...
        dec = ee.pct_scaled_to_decimal(p)
        assert dec == 2.5889                  # troncato, NON 2.589
        assert ee.decimal_to_pct_scaled(dec, SX_STEP) == p
        p2 = 314 * SX_STEP                    # quota reale 2.547770...
        dec2 = ee.pct_scaled_to_decimal(p2)
        assert dec2 == 2.5477
        assert ee.decimal_to_pct_scaled(dec2, SX_STEP) == p2

    def test_ordine_al_prezzo_del_book_trova_controparte(self):
        """Il bound calcolato dal prezzo di libro e' il gradino del maker."""
        maker_p = 342 * SX_STEP               # un livello del book
        book_price = ee.pct_scaled_to_decimal(maker_p)   # come lo legge il bot
        bound = ee.decimal_to_pct_scaled(book_price, SX_STEP)
        # IOC: controparte accettata solo se il suo gradino e' >= al bound
        assert bound == maker_p
        assert ee.SX_PROB_SCALE / bound >= book_price


class TestSxBetAuth:
    def test_metadata_fetch_e_cache(self, monkeypatch):
        calls = {"n": 0}

        def fake_get(url, params=None, headers=None, timeout=None):
            calls["n"] += 1
            assert str(url).endswith("metadata/obv3")
            return _fake_response(_sx_meta())

        monkeypatch.setattr(ee.requests, "get", fake_get)
        p = ee.SxBetProvider(SX_KEY_TEST, SX_PK_TEST)
        assert p._metadata()["chainId"] == 4162
        assert p._metadata()["chainId"] == 4162  # cache
        assert calls["n"] == 1

    def test_step_dal_metadata(self, monkeypatch):
        monkeypatch.setattr(ee.requests, "get",
                            _fake_get_sx({"metadata/obv3": _sx_meta()}))
        p = ee.SxBetProvider(SX_KEY_TEST, SX_PK_TEST)
        assert p._step_scaled() == SX_STEP

    def test_account_dalla_private_key(self, monkeypatch):
        def boom(*a, **k):
            raise AssertionError("non deve chiamare la rete")
        monkeypatch.setattr(ee.requests, "get", boom)
        from eth_account import Account
        p = ee.SxBetProvider(SX_KEY_TEST, SX_PK_TEST)
        assert p._account().address == Account.from_key(SX_PK_TEST).address

    def test_account_senza_private_key_raise(self, monkeypatch):
        def boom(*a, **k):
            raise AssertionError("non deve chiamare la rete")
        monkeypatch.setattr(ee.requests, "get", boom)
        p = ee.SxBetProvider(SX_KEY_TEST, "")
        with pytest.raises(RuntimeError, match="SX_PRIVATE_KEY"):
            p._account()

    def test_sign_order_eip712(self, monkeypatch):
        monkeypatch.setattr(ee.requests, "get",
                            _fake_get_sx({"metadata/obv3": _sx_meta()}))
        from eth_account import Account
        acct = Account.from_key(SX_PK_TEST)
        p = ee.SxBetProvider(SX_KEY_TEST, SX_PK_TEST)
        sig = p._sign_order({
            "marketHash": SX_MH, "maker": acct.address,
            "totalBetSize": "1000000",
            "percentageOdds": "31500000000000000000",
            "salt": "0x" + "ab" * 32,
            "expiry": int(time.time()) + 3600, "baseToken": SX_USDC_ADDR,
            "isMakerBettingOutcomeOne": True,
        })
        assert sig.startswith("0x") and len(sig) == 132

    def test_headers_auth_richiesta_chiave(self):
        p = ee.SxBetProvider("", "")
        assert "x-sx-api-key" not in p._headers(auth=False)
        with pytest.raises(RuntimeError, match="SX_API_KEY"):
            p._headers(auth=True)


class TestRedactRawHttp:
    """`redact_raw_http`: l'evidenza HTTP non deve mai diventare un leak."""

    def test_maschera_la_firma_e_tronca(self):
        raw = ('{"orderSignature": "0xdeadbeef", "ok": true, '
               '"note": "' + "x" * 300 + '"}')
        out = ee.redact_raw_http(raw, limit=60)
        assert "0xdeadbeef" not in out
        assert "<redacted>" in out
        assert "troncato" in out          # il troncamento e' dichiarato

    def test_senza_troncamento_resta_identico(self):
        raw = '{"status": "success", "data": {"orders": []}}'
        assert ee.redact_raw_http(raw) == raw

    def test_accetta_oggetti_non_stringa(self):
        # una response/test che passa un dict non deve far esplodere il log
        assert "ok" in ee.redact_raw_http({"ok": True})


class TestSxBetOrders:
    def _fake_placed(self, outcome=None, status="SUBMITTED",
                     order_id="0xorder1"):
        o = {"orderId": order_id, "status": status, "commandId": "c1"}
        if outcome is not None:
            o["outcome"] = outcome
        return {"status": "success",
                "data": {"orders": [o]}}

    def _full_fill(self):
        return {"state": "FULLY_FILLED", "remainingAmount": "0",
                "fillAmount": "1000000",
                "blendedOdds": "31500000000000000000",
                "matchIds": ["0xm1"], "tradeId": "0xt1"}

    def test_place_back_payload_e_parsing(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            ee.requests, "get",
            _fake_get_sx({"metadata/obv3": _sx_meta()}))

        def fake_post(url, json=None, headers=None, timeout=None):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return _fake_response(
                self._fake_placed(outcome=self._full_fill()))

        monkeypatch.setattr(ee.requests, "post", fake_post)
        from eth_account import Account
        acct = Account.from_key(SX_PK_TEST)
        p = ee.SxBetProvider(SX_KEY_TEST, SX_PK_TEST)
        res = p.place_limit_order(SX_MH, 1, "BACK", 3.1746, 1.0)
        assert res.ok
        assert res.status == "FULLY_FILLED"
        assert res.bet_id == "0xorder1"
        assert res.price_matched == pytest.approx(3.1746)
        assert res.size_matched == pytest.approx(1.0)

        assert captured["url"].endswith("orders-v3")
        assert captured["headers"]["x-sx-api-key"] == SX_KEY_TEST
        body = captured["json"]
        assert body["waitForOutcome"] is True
        order = body["orders"][0]
        assert order["marketHash"] == SX_MH
        assert order["maker"] == acct.address
        assert order["isMakerBettingOutcomeOne"] is True
        assert order["totalBetSize"] == "1000000"      # 1 USDC
        assert order["percentageOdds"] == "31500000000000000000"  # 3.1746
        assert order["timeInForce"] == "IOC"
        assert order["expiry"] > int(time.time())
        sig = order["orderSignature"]
        assert sig.startswith("0x") and len(sig) == 132

    def test_place_selection2_esito_due(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(ee.requests, "get",
                            _fake_get_sx({"metadata/obv3": _sx_meta()}))

        def fake_post(url, json=None, headers=None, timeout=None):
            captured["json"] = json
            return _fake_response(
                self._fake_placed(outcome=self._full_fill()))

        monkeypatch.setattr(ee.requests, "post", fake_post)
        p = ee.SxBetProvider(SX_KEY_TEST, SX_PK_TEST)
        res = p.place_limit_order(SX_MH, 2, "BACK", 2.0, 1.0)
        assert res.ok
        order = captured["json"]["orders"][0]
        assert order["isMakerBettingOutcomeOne"] is False
        assert order["percentageOdds"] == "50000000000000000000"

    def test_place_lay_inverte_sull_esito_complementare(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(ee.requests, "get",
                            _fake_get_sx({"metadata/obv3": _sx_meta()}))

        def fake_post(url, json=None, headers=None, timeout=None):
            captured["json"] = json
            return _fake_response(
                self._fake_placed(outcome=self._full_fill()))

        monkeypatch.setattr(ee.requests, "post", fake_post)
        p = ee.SxBetProvider(SX_KEY_TEST, SX_PK_TEST)
        # lay del favorito a 2.0 == back del complementare a 2.0
        res = p.place_limit_order(SX_MH, 1, "LAY", 2.0, 1.0)
        assert res.ok
        order = captured["json"]["orders"][0]
        assert order["isMakerBettingOutcomeOne"] is False
        assert order["percentageOdds"] == "50000000000000000000"

    def test_persistence_persist_diventa_gtc(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(ee.requests, "get",
                            _fake_get_sx({"metadata/obv3": _sx_meta()}))

        def fake_post(url, json=None, headers=None, timeout=None):
            captured["json"] = json
            return _fake_response(
                self._fake_placed(outcome=self._full_fill()))

        monkeypatch.setattr(ee.requests, "post", fake_post)
        p = ee.SxBetProvider(SX_KEY_TEST, SX_PK_TEST)
        res = p.place_limit_order(SX_MH, 1, "BACK", 2.0, 1.0,
                                  persistence="PERSIST")
        assert res.ok
        assert captured["json"]["orders"][0]["timeInForce"] == "GTC"

    def test_fill_parziale_ok(self, monkeypatch):
        monkeypatch.setattr(ee.requests, "get",
                            _fake_get_sx({"metadata/obv3": _sx_meta()}))
        outcome = {"state": "PARTIAL_FILL_DONE", "remainingAmount": "500000",
                   "fillAmount": "500000",
                   "blendedOdds": "31500000000000000000",
                   "matchIds": ["0xm1"], "tradeId": "0xt1"}
        monkeypatch.setattr(ee.requests, "post",
                            lambda *a, json=None, **k:
                            _fake_response(self._fake_placed(outcome=outcome)))
        p = ee.SxBetProvider(SX_KEY_TEST, SX_PK_TEST)
        res = p.place_limit_order(SX_MH, 1, "BACK", 3.1746, 1.0)
        assert res.ok
        assert res.size_matched == pytest.approx(0.5)

    def test_ordine_rifiutato_failed(self, monkeypatch):
        monkeypatch.setattr(ee.requests, "get",
                            _fake_get_sx({"metadata/obv3": _sx_meta()}))

        def fake_post(url, json=None, headers=None, timeout=None):
            return _fake_response(self._fake_placed(
                status="FAILED", outcome=None))

        monkeypatch.setattr(ee.requests, "post", fake_post)
        p = ee.SxBetProvider(SX_KEY_TEST, SX_PK_TEST)
        res = p.place_limit_order(SX_MH, 1, "BACK", 3.1746, 1.0)
        assert not res.ok
        assert res.status == "FAILED"

    def test_rete_ko_fail_closed(self, monkeypatch):
        monkeypatch.setattr(ee.requests, "get",
                            _fake_get_sx({"metadata/obv3": _sx_meta()}))

        def boom(*a, **k):
            raise RuntimeError("API giu'")

        monkeypatch.setattr(ee.requests, "post", boom)
        p = ee.SxBetProvider(SX_KEY_TEST, SX_PK_TEST)
        res = p.place_limit_order(SX_MH, 1, "BACK", 3.1746, 1.0)
        assert not res.ok
        assert "API giu'" in (res.error or "")

    def test_risposta_vuota(self, monkeypatch):
        monkeypatch.setattr(ee.requests, "get",
                            _fake_get_sx({"metadata/obv3": _sx_meta()}))
        monkeypatch.setattr(ee.requests, "post",
                            lambda *a, json=None, **k:
                            _fake_response({"status": "success",
                                             "data": {"orders": []}}))
        p = ee.SxBetProvider(SX_KEY_TEST, SX_PK_TEST)
        res = p.place_limit_order(SX_MH, 1, "BACK", 3.1746, 1.0)
        assert not res.ok
        assert "vuota" in (res.error or "")

    def test_fill_senza_order_id_non_e_piazzato(self, monkeypatch):
        """stato FULLY_FILLED ma NESSUN orderId: trattato come NON piazzato.

        E' esattamente il caso che permetteva di scrivere un "successo" sul
        ledger mentre sull'exchange non esisteva alcun ordine (lo stato di
        riempimento da solo non e' una conferma dell'emissione).
        """
        monkeypatch.setattr(
            ee.requests, "get",
            _fake_get_sx({"metadata/obv3": _sx_meta()}))
        monkeypatch.setattr(
            ee.requests, "post",
            lambda *a, **k: _fake_response(
                self._fake_placed(outcome=self._full_fill(), order_id="")))
        p = ee.SxBetProvider(SX_KEY_TEST, SX_PK_TEST)
        res = p.place_limit_order(SX_MH, 1, "BACK", 3.1746, 1.0)
        assert res.ok is False
        assert res.bet_id is None
        assert "orderId" in (res.error or "")

    def test_risposta_cruda_loggata_e_firma_mascherata(self, monkeypatch,
                                                       caplog):
        """Evidenza: il corpo GREZZO della risposta ordine finisce nei log,
        con la firma EIP-712 mascherata (il resto resta ispezionabile)."""
        import logging
        monkeypatch.setattr(
            ee.requests, "get",
            _fake_get_sx({"metadata/obv3": _sx_meta()}))
        payload = self._fake_placed(outcome=self._full_fill())
        payload["data"]["orders"][0]["orderSignature"] = "0x" + "ab" * 65
        monkeypatch.setattr(ee.requests, "post",
                            lambda *a, **k: _fake_response(payload))
        p = ee.SxBetProvider(SX_KEY_TEST, SX_PK_TEST)
        with caplog.at_level(logging.WARNING, logger="execution_engine"):
            res = p.place_limit_order(SX_MH, 1, "BACK", 3.1746, 1.0)
        assert res.ok
        log = caplog.text
        assert "risposta ordine /orders-v3" in log
        assert "FULLY_FILLED" in log          # corpo grezzo leggibile
        assert "<redacted>" in log            # firma mascherata
        assert "0xabab" not in log            # firma NON in chiaro

    def test_stake_minimo_sotto_1_usdc(self, monkeypatch):
        def boom(*a, **k):
            raise AssertionError("non deve chiamare la rete")
        monkeypatch.setattr(ee.requests, "get", boom)
        monkeypatch.setattr(ee.requests, "post", boom)
        p = ee.SxBetProvider(SX_KEY_TEST, SX_PK_TEST)
        res = p.place_limit_order(SX_MH, 1, "BACK", 3.1746, 0.5)
        assert not res.ok
        assert "minimo" in (res.error or "")

    def test_side_invalido(self, monkeypatch):
        def boom(*a, **k):
            raise AssertionError("non deve chiamare la rete")
        monkeypatch.setattr(ee.requests, "get", boom)
        p = ee.SxBetProvider(SX_KEY_TEST, SX_PK_TEST)
        res = p.place_limit_order(SX_MH, 1, "BANANA", 2.0, 1.0)
        assert not res.ok
        assert "side" in (res.error or "")

    def test_senza_credenziali_fail_fast_no_rete(self, monkeypatch):
        def boom(*a, **k):
            raise AssertionError("non deve chiamare la rete")
        monkeypatch.setattr(ee.requests, "get", boom)
        monkeypatch.setattr(ee.requests, "post", boom)
        p = ee.SxBetProvider("", "")
        res = p.place_limit_order(SX_MH, 1, "BACK", 2.0, 1.0)
        assert not res.ok
        assert "mancanti" in (res.error or "")
        res2 = p.place_limit_order(SX_MH, 1, "BACK", 2.0, 1.0)
        assert not res2.ok  # stesso esito: fail fast prima della rete

    def test_cancel_order_delete(self, monkeypatch):
        captured = {}

        def fake_delete(url, headers=None, json=None, timeout=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return _fake_response({"status": "success",
                                   "data": {"cancelled": []}})

        monkeypatch.setattr(ee.requests, "delete", fake_delete)
        p = ee.SxBetProvider(SX_KEY_TEST, SX_PK_TEST)
        assert p.cancel_order(SX_MH, "0xorder1") is True
        assert captured["url"].endswith("orders-v3")
        assert captured["headers"]["x-sx-api-key"] == SX_KEY_TEST
        assert captured["json"] == {"orders": [{"orderId": "0xorder1"}]}
        # bet_id vuoto: nessuna chiamata
        def boom(*a, **k):
            raise AssertionError("non deve chiamare la rete")
        monkeypatch.setattr(ee.requests, "delete", boom)
        assert p.cancel_order(SX_MH, "") is False


class TestSxBetMarket:
    def test_best_back_price_da_book(self, monkeypatch):
        monkeypatch.setattr(ee.requests, "get",
                            _fake_get_sx({"orderbook-v3/snapshot":
                                          _sx_snapshot()}))
        p = ee.SxBetProvider(SX_KEY_TEST, SX_PK_TEST)
        assert p.best_back_price(SX_MH, 1) == pytest.approx(3.1746)
        assert p.best_back_price(SX_MH, 2) == pytest.approx(1.4109)

    def test_best_back_lato_vuoto_none(self, monkeypatch):
        snap = _sx_snapshot()
        snap["data"]["outcomeTwo"] = []
        monkeypatch.setattr(ee.requests, "get",
                            _fake_get_sx({"orderbook-v3/snapshot": snap}))
        p = ee.SxBetProvider(SX_KEY_TEST, SX_PK_TEST)
        assert p.best_back_price(SX_MH, 2) is None

    def test_best_back_errore_none(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("API giu'")
        monkeypatch.setattr(ee.requests, "get", boom)
        p = ee.SxBetProvider(SX_KEY_TEST, SX_PK_TEST)
        assert p.best_back_price(SX_MH, 1) is None

    def test_get_market_book_struttura(self, monkeypatch):
        monkeypatch.setattr(ee.requests, "get",
                            _fake_get_sx({"orderbook-v3/snapshot":
                                          _sx_snapshot()}))
        p = ee.SxBetProvider(SX_KEY_TEST, SX_PK_TEST)
        book = p.get_market_book(SX_MH)
        assert book["marketId"] == SX_MH
        assert book["runners"][0]["selectionId"] == 1
        assert book["runners"][0]["availableToBack"][0]["price"] \
            == pytest.approx(3.1746)
        assert book["runners"][0]["availableToBack"][0]["size"] \
            == pytest.approx(63.9473)
        assert book["runners"][1]["availableToBack"][0]["price"] \
            == pytest.approx(1.4109)

    def test_discovery_mercati_1x2(self, monkeypatch):
        captured = {}
        market = {"marketHash": SX_MH,
                  "outcomeOneName": "Nueva Chicago",
                  "outcomeTwoName": "Not Nueva Chicago",
                  "teamOneName": "Nueva Chicago",
                  "teamTwoName": "Quilmes",
                  "sportXeventId": "L19853741",
                  "type": 1, "gameTime": 1788811200}

        def fake_get(url, params=None, headers=None, timeout=None):
            captured["params"] = params
            return _fake_response({"status": "success",
                                   "data": {"markets": [market],
                                             "nextKey": None}})

        monkeypatch.setattr(ee.requests, "get", fake_get)
        p = ee.SxBetProvider(SX_KEY_TEST, SX_PK_TEST)
        mkts = p.list_market_catalogue(max_results=10)
        assert len(mkts) == 1
        m = mkts[0]
        assert m["market_id"] == SX_MH
        assert m["market_name"] == "1X2 - Nueva Chicago"
        assert m["event_name"] == "Nueva Chicago vs Quilmes"
        assert m["open_date"].startswith("2026-09-07")
        assert m["runners"] == [{"selection_id": 1, "name": "Nueva Chicago"},
                                 {"selection_id": 2,
                                  "name": "Not Nueva Chicago"}]
        assert captured["params"]["sportIds"] == "5"
        assert captured["params"]["type"] == "1"
        assert captured["params"]["pageSize"] == 100

    def test_discovery_paginazione_e_limite(self, monkeypatch):
        pages = {"n": 0}

        def fake_get(url, params=None, headers=None, timeout=None):
            pages["n"] += 1
            if pages["n"] == 1:
                return _fake_response({"status": "success", "data": {
                    "markets": [{"marketHash": f"0x{m}",
                                  "outcomeOneName": f"A{m}",
                                  "outcomeTwoName": f"B{m}",
                                  "teamOneName": "X", "teamTwoName": "Y",
                                  "sportXeventId": f"L{m}", "type": 1,
                                  "gameTime": 1788811200}
                                 for m in range(3)],
                    "nextKey": "next-1"}})
            return _fake_response({"status": "success", "data": {
                "markets": [{"marketHash": "0xlast",
                              "outcomeOneName": "Last",
                              "outcomeTwoName": "Not last",
                              "teamOneName": "X", "teamTwoName": "Y",
                              "sportXeventId": "L9", "type": 1,
                              "gameTime": 1788811200}],
                "nextKey": None}})

        monkeypatch.setattr(ee.requests, "get", fake_get)
        p = ee.SxBetProvider(SX_KEY_TEST, SX_PK_TEST)
        mkts = p.list_market_catalogue(max_results=4)
        assert len(mkts) == 4
        assert pages["n"] == 2
        # limite rispettato: max_results piu' piccolo della prima pagina
        pages2 = {"n": 0}

        def fake_get2(url, params=None, headers=None, timeout=None):
            pages2["n"] += 1
            return _fake_response({"status": "success", "data": {
                "markets": [{"marketHash": f"0x{m}",
                              "outcomeOneName": f"A{m}",
                              "outcomeTwoName": f"B{m}",
                              "type": 1} for m in range(5)],
                "nextKey": None}})

        monkeypatch.setattr(ee.requests, "get", fake_get2)
        mkts = p.list_market_catalogue(max_results=2)
        assert len(mkts) == 2
        assert pages2["n"] == 1

    def test_get_balance(self, monkeypatch):
        captured = {}
        payload = {"status": "success", "data": {"balances": [{
            "userAddress": "0xuser",
            "wallet": "0xproxy",
            "tokenAddress": SX_USDC_ADDR,
            "escrowAddress": "0xescrow",
            "availableAmount": "1028000014",
            "pendingAvailableAmount": "0",
            "escrowedAmount": "123000000",
            "pendingEscrowAmount": "0"}]}}

        def fake_get(url, params=None, headers=None, timeout=None):
            if str(url).endswith("user/balance-v3"):
                captured["headers"] = headers
                return _fake_response(payload)
            if str(url).endswith("metadata/obv3"):
                return _fake_response(_sx_meta())
            raise AssertionError(f"GET inatteso: {url}")

        monkeypatch.setattr(ee.requests, "get", fake_get)
        p = ee.SxBetProvider(SX_KEY_TEST, SX_PK_TEST)
        bal = p.get_balance()
        assert bal["availableBalance"] == pytest.approx(1028.0)
        assert bal["exposure"] == pytest.approx(123.0)
        assert bal["wallet"] == "0xproxy"
        assert captured["headers"]["x-sx-api-key"] == SX_KEY_TEST

    def test_get_balance_vuota_zero(self, monkeypatch):
        def fake_get(url, params=None, headers=None, timeout=None):
            return _fake_response({"status": "success",
                                   "data": {"balances": []}})

        monkeypatch.setattr(ee.requests, "get", fake_get)
        p = ee.SxBetProvider(SX_KEY_TEST, SX_PK_TEST)
        bal = p.get_balance()
        assert bal["availableBalance"] == 0.0
        assert bal["exposure"] == 0.0


class TestSxBetFactory:
    def test_factory_sxbet_con_credenziali(self, monkeypatch):
        monkeypatch.setattr(ee, "EXECUTION_DRY_RUN", False)
        monkeypatch.setattr(ee, "EXECUTION_PROVIDER", "sxbet")
        monkeypatch.setattr(ee, "SX_API_KEY", SX_KEY_TEST)
        monkeypatch.setattr(ee, "SX_PRIVATE_KEY", SX_PK_TEST)
        p = ee.build_provider()
        assert isinstance(p, ee.SxBetProvider)
        assert p.api_key == SX_KEY_TEST
        assert p.private_key == SX_PK_TEST

    def test_factory_sxbet_senza_credenziali_dry_run(self, monkeypatch):
        monkeypatch.setattr(ee, "EXECUTION_DRY_RUN", False)
        monkeypatch.setattr(ee, "EXECUTION_PROVIDER", "sxbet")
        monkeypatch.setattr(ee, "SX_API_KEY", "")
        monkeypatch.setattr(ee, "SX_PRIVATE_KEY", "")
        assert isinstance(ee.build_provider(), ee.DryRunProvider)

    def test_creds_configured_sxbet(self, monkeypatch):
        monkeypatch.setattr(ee, "EXECUTION_PROVIDER", "sxbet")
        monkeypatch.setattr(ee, "SX_API_KEY", SX_KEY_TEST)
        monkeypatch.setattr(ee, "SX_PRIVATE_KEY", SX_PK_TEST)
        assert ee._creds_configured() is True
        monkeypatch.setattr(ee, "SX_API_KEY", "")
        assert ee._creds_configured() is False

    def test_status_riporta_env_sxbet(self, monkeypatch):
        monkeypatch.setattr(ee, "EXECUTION_PROVIDER", "sxbet")
        monkeypatch.setattr(ee, "SX_API_KEY", SX_KEY_TEST)
        monkeypatch.setattr(ee, "SX_PRIVATE_KEY", SX_PK_TEST)
        engine = ee.ExecutionEngine(provider=ee.SxBetProvider(
            SX_KEY_TEST, SX_PK_TEST))
        st = engine.status()
        assert st["provider"] == "sxbet"
        assert st["creds_env"] == "SX_API_KEY/SX_PRIVATE_KEY"
        assert st["creds"] is True


# ---------------------------------------------------------------------------
# Sicurezza
# ---------------------------------------------------------------------------

class TestSegreti:
    def test_credenziali_lette_solo_da_env(self):
        """Nessun valore credenziale hardcoded nel modulo: solo os.getenv."""
        src = open("execution_engine.py").read()
        # le assegnazioni delle credenziali passano da os.getenv, mai letterali
        assert 'EXECUTION_APP_KEY = os.getenv("EXECUTION_APP_KEY", "")' in src
        assert 'EXECUTION_USERNAME = os.getenv("EXECUTION_USERNAME", "")' in src
        assert 'EXECUTION_PASSWORD = os.getenv("EXECUTION_PASSWORD", "")' in src
        # nessuna stringa che assomigli a un token/secret reale
        import re
        assert not re.search(r'"\d{8,10}:[A-Za-z0-9_-]{30,}"', src)
        assert "ghp_" not in src

    def test_credenziali_smarkets_lette_solo_da_env(self):
        """Anche Smarkets: username/password SOLO da os.getenv, mai letterali."""
        src = open("execution_engine.py").read()
        assert 'SMARKETS_USERNAME = os.getenv("SMARKETS_USERNAME", "")' in src
        assert 'SMARKETS_PASSWORD = os.getenv("SMARKETS_PASSWORD", "")' in src
        assert 'SMARKETS_API_BASE = os.getenv(' in src

    def test_credenziali_sxbet_lette_solo_da_env(self):
        """Anche SX Bet: api key e private key SOLO da os.getenv."""
        src = open("execution_engine.py").read()
        assert 'SX_API_KEY = os.getenv("SX_API_KEY", "")' in src
        assert 'SX_PRIVATE_KEY = os.getenv("SX_PRIVATE_KEY", "")' in src
        assert 'SX_API_BASE = os.getenv(' in src

    def test_indipendenza_da_tracker_e_bot(self):
        """Il modulo non importa lo stato del bot Value Bet (come surebet_engine)."""
        import ast
        src = open("execution_engine.py").read()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert node.module not in ("tracker", "bot", "auto_bet",
                                           "fixture_engine", "ml_ensemble"), \
                    f"execution_engine non deve importare {node.module}"


# ---------------------------------------------------------------------------
# Risoluzione match -> mercato (auto_bet live)
# ---------------------------------------------------------------------------

class _FakeSxProvider:
    """Provider finto per la risoluzione: espone solo il catalogo."""

    name = "sxbet"

    def __init__(self, markets):
        self._markets = markets

    def list_market_catalogue(self, event_type_ids=("5",),
                              market_type="1X2", max_results=400):
        return self._markets[:max_results]


GAME_TS = "2026-09-08T18:00:00+00:00"


def _sx_event_markets(home="Nueva Chicago", away="Quilmes",
                      ts=GAME_TS, prefix="m"):
    """Le tre binario 'X vs Not X' del 1X2 SX per un evento."""
    def row(mid, t1, t2, o1):
        return {"market_id": mid, "event_name": f"{t1} vs {t2}",
                "open_date": ts, "team_one_name": t1, "team_two_name": t2,
                "outcome_one_name": o1,
                "outcome_two_name": f"Not {o1}",
                "runners": [{"selection_id": 1, "name": o1},
                             {"selection_id": 2, "name": f"Not {o1}"}]}
    return [
        row(f"{prefix}-home", home, away, home),
        row(f"{prefix}-away", home, away, away),
        row(f"{prefix}-tie", home, away, "Tie"),
    ]


class TestNameKeySim:
    def test_key_normalizza_e_accetta(self):
        assert ee._name_key("CA Osasuna") == "ca osasuna"
        assert ee._name_key("Nueva Chicago") == "nueva chicago"
        assert ee._name_key("Nueva-Chicago!") == "nueva chicago"
        assert ee._name_key("Málaga") == "malaga"  # accent-fold
        assert ee._name_key(None) == ""

    def test_sim_sovrapposizione_e_ratio(self):
        assert ee._name_sim("Inter", "Inter Milan") >= 0.82   # contenimento
        assert ee._name_sim("Betis", "Real Betis") >= 0.82
        assert ee._name_sim("Roma", "Roma") == 1.0
        assert ee._name_sim("Roma", "Lazio") < 0.82
        assert ee._name_sim(None, "Roma") == 0.0


class TestResolveMatchMarket:
    def _prov(self, markets):
        return _FakeSxProvider(markets)

    def test_esito_casa(self):
        prov = self._prov(_sx_event_markets())
        r = ee.resolve_match_market(prov, "Nueva Chicago", "Quilmes", "1",
                                    kickoff_iso=GAME_TS)
        assert r and r["market_id"] == "m-home" and r["selection_id"] == 1

    def test_esito_trasferta(self):
        prov = self._prov(_sx_event_markets())
        r = ee.resolve_match_market(prov, "Nueva Chicago", "Quilmes", "2",
                                    kickoff_iso=GAME_TS)
        assert r and r["market_id"] == "m-away" and r["selection_id"] == 1

    def test_esito_pareggio(self):
        prov = self._prov(_sx_event_markets())
        r = ee.resolve_match_market(prov, "Nueva Chicago", "Quilmes", "X",
                                    kickoff_iso=GAME_TS)
        assert r and r["market_id"] == "m-tie" and r["selection_id"] == 1
        assert r["label"] == "X"

    def test_nomi_fuzzy_squadre(self):
        # Segnale the-odds-api "Real Betis"/"Inter" vs nomi SX piu' lunghi
        prov = self._prov(_sx_event_markets(home="Real Betis",
                                            away="Inter Milan"))
        r = ee.resolve_match_market(prov, "Real Betis", "Inter", "1",
                                    kickoff_iso=GAME_TS)
        assert r and r["market_id"] == "m-home"

    def test_orientamento_invertito_nessun_match(self):
        # Home/away scambiati: non si deve MAI scommettere sul lato sbagliato
        prov = self._prov(_sx_event_markets())
        r = ee.resolve_match_market(prov, "Quilmes", "Nueva Chicago", "1",
                                    kickoff_iso=GAME_TS)
        assert r is None

    def test_kickoff_fuori_finestra_nessun_match(self):
        prov = self._prov(_sx_event_markets(ts="2026-09-09T18:00:00+00:00"))
        r = ee.resolve_match_market(prov, "Nueva Chicago", "Quilmes", "1",
                                    kickoff_iso="2026-09-08T18:00:00Z")
        assert r is None

    def test_eventi_multipli_ambigui_nessun_match(self):
        mkts = _sx_event_markets(ts="2026-09-08T18:00:00+00:00", prefix="a")
        mkts += _sx_event_markets(ts="2026-09-08T19:00:00+00:00", prefix="b")
        prov = self._prov(mkts)
        # due kickoff diversi ma entrambi dentro la finestra di 6h -> ambiguo
        r = ee.resolve_match_market(prov, "Nueva Chicago", "Quilmes", "1",
                                    kickoff_iso="2026-09-08T18:30:00Z")
        assert r is None

    def test_catalogo_vuoto_none(self):
        prov = self._prov([])
        assert ee.resolve_match_market(prov, "A", "B", "1") is None

    def test_provider_non_sxbet_none(self):
        class _Dry:
            name = "dry_run"
        assert ee.resolve_match_market(_Dry(), "A", "B", "1") is None

    def test_esito_sconosciuto_none(self):
        prov = self._prov(_sx_event_markets())
        assert ee.resolve_match_market(prov, "Nueva Chicago", "Quilmes",
                                       "Over 2.5", kickoff_iso=GAME_TS) is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])