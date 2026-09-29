"""Test del client realtime SX (`sx_realtime.py`) — TUTTI OFFLINE.

Nessuna rete, nessuna credenziale, nessun ordine: il connettore WebSocket e il
client HTTP del token sono finti e iniettati; le publication sono costruite a
mano nella forma reale dell'API SX.
"""

from __future__ import annotations

import ast
import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

import sx_realtime as rt


# ---------------------------------------------------------------------------
# Trasporti finti
# ---------------------------------------------------------------------------

class FakeWS:
    """WebSocket finto: risponde a copione, registra i frame inviati.

    Quando il copione e' finito si mette in attesa (come un canale muto): e'
    cosi' che i test del timeout misurano la guardia vera, non un'eccezione
    di comodo.
    """

    def __init__(self, script):
        self.script = list(script)
        self.sent: list[dict] = []
        self.closed = False

    async def send(self, raw):
        self.sent.append(json.loads(raw) if isinstance(raw, str) else raw)

    async def recv(self):
        if not self.script:
            await asyncio.sleep(3600)  # canale muto
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return json.dumps(item) if not isinstance(item, str) else item

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed = True
        return False


def fake_connect(script, sink=None):
    """Connettore finto con la firma `(url, **kwargs)` di `websockets.connect`."""
    holder = {}

    async def connect(url, **kwargs):
        ws = FakeWS(script)
        holder["url"] = url
        holder["kwargs"] = kwargs
        holder["ws"] = ws
        if sink is not None:
            sink.append(ws)
        return ws

    connect.holder = holder
    return connect


def connect_reply(msg_id=1):
    return {"id": msg_id, "connect": {"client": "fake", "version": "1"}}


def subscribe_reply(msg_id):
    return {"id": msg_id, "subscribe": {"recoverable": True}}


def book_push(market_hash="0xmkt", version="v1", price_one=2.0, price_two=4.0):
    """Push del canale `orderbook_v3:{hash}` nella forma reale di SX."""
    scale = 10 ** 20
    return {"push": {"channel": f"orderbook_v3:{market_hash}",
                     "pub": {"data": {
                         "marketHash": market_hash,
                         "eventId": "L1",
                         "version": version,
                         "outcomeOne": [{"percentageOdds": str(scale // int(price_one)),
                                         "size": "1000000"}],
                         "outcomeTwo": [{"percentageOdds": str(scale // int(price_two)),
                                         "size": "2000000"}],
                     }}}}


class FakeResponse:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeHttp:
    """Client httpx finto: registra URL e header, risponde a copione."""

    def __init__(self, response):
        self.response = response
        self.calls: list[tuple] = []
        self.closed = False

    async def get(self, url, headers=None):
        self.calls.append((url, dict(headers or {})))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    async def aclose(self):
        self.closed = True


# ---------------------------------------------------------------------------
# Canali e URL
# ---------------------------------------------------------------------------

class TestCanali:
    def test_canale_del_book(self):
        assert rt.book_channel("0xabc") == "orderbook_v3:0xabc"

    def test_canale_dell_evento(self):
        assert rt.event_channel("L13058397") == "orderbook_v3_event:L13058397"

    @pytest.mark.parametrize("value", ["", "   ", None])
    def test_hash_vuoto_e_errore(self, value):
        with pytest.raises(rt.RealtimeError):
            rt.book_channel(value)
        with pytest.raises(rt.RealtimeError):
            rt.event_channel(value)


class TestUrl:
    def test_default_produzione(self, monkeypatch):
        monkeypatch.delenv("SX_REALTIME_URL", raising=False)
        monkeypatch.delenv("SX_API_BASE", raising=False)
        assert rt.realtime_url() == rt.DEFAULT_REALTIME_URL

    def test_env_vince_su_tutto(self, monkeypatch):
        monkeypatch.setenv("SX_REALTIME_URL", "wss://esempio/ws")
        monkeypatch.setenv("SX_API_BASE", "https://api.toronto.sx.bet")
        assert rt.realtime_url() == "wss://esempio/ws"

    def test_testnet_derivato_dalla_base(self, monkeypatch):
        monkeypatch.delenv("SX_REALTIME_URL", raising=False)
        assert rt.realtime_url(api_base="https://api.toronto.sx.bet") == \
            "wss://realtime.toronto.sx.bet/connection/websocket"

    def test_base_di_produzione(self, monkeypatch):
        monkeypatch.delenv("SX_REALTIME_URL", raising=False)
        assert rt.realtime_url(api_base="https://api.sx.bet") == \
            rt.DEFAULT_REALTIME_URL


# ---------------------------------------------------------------------------
# Token
# ---------------------------------------------------------------------------

class TestToken:
    def test_senza_chiave_fail_closed(self, monkeypatch):
        """Senza `SX_API_KEY` NON si tenta nemmeno la chiamata."""
        monkeypatch.delenv("SX_API_KEY", raising=False)
        client = FakeHttp(FakeResponse(200, {"token": "x"}))
        with pytest.raises(rt.RealtimeConfigError):
            asyncio.run(rt.fetch_realtime_token(client=client))
        assert client.calls == []          # nessuna richiesta partita

    def test_token_da_data_token(self, monkeypatch):
        client = FakeHttp(FakeResponse(200, {"status": "success",
                                             "data": {"token": "TK1"}}))
        assert asyncio.run(rt.fetch_realtime_token(api_key="k",
                                                   client=client)) == "TK1"
        url, headers = client.calls[0]
        assert url.endswith(rt.REALTIME_ENDPOINT_PATH)
        assert headers["x-sx-api-key"] == "k"

    def test_token_flat(self):
        client = FakeHttp(FakeResponse(200, {"token": "TK2"}))
        assert asyncio.run(rt.fetch_realtime_token(api_key="k",
                                                   client=client)) == "TK2"

    def test_token_come_stringa(self):
        client = FakeHttp(FakeResponse(200, "TK3"))
        assert asyncio.run(rt.fetch_realtime_token(api_key="k",
                                                   client=client)) == "TK3"

    def test_401_e_errore_dichiarato(self):
        client = FakeHttp(FakeResponse(401, {"message": "BAD_AUTH"}))
        with pytest.raises(rt.RealtimeError, match="401"):
            asyncio.run(rt.fetch_realtime_token(api_key="k", client=client))

    def test_risposta_senza_token_e_errore(self):
        client = FakeHttp(FakeResponse(200, {"status": "success", "data": {}}))
        with pytest.raises(rt.RealtimeError, match="token"):
            asyncio.run(rt.fetch_realtime_token(api_key="k", client=client))

    def test_token_vuoto_non_passa(self):
        client = FakeHttp(FakeResponse(200, {"data": {"token": "   "}}))
        with pytest.raises(rt.RealtimeError):
            asyncio.run(rt.fetch_realtime_token(api_key="k", client=client))

    def test_errore_di_rete_propagato(self):
        client = FakeHttp(ConnectionError("dns"))
        with pytest.raises(ConnectionError):
            asyncio.run(rt.fetch_realtime_token(api_key="k", client=client))


# ---------------------------------------------------------------------------
# Normalizzazione del book (conversioni delegate a execution_engine)
# ---------------------------------------------------------------------------

class TestBook:
    def test_book_valido(self):
        scale = 10 ** 20
        book = rt.normalize_book({
            "marketHash": "0xm", "eventId": "L1", "version": "5",
            "outcomeOne": [{"percentageOdds": str(scale // 2), "size": "1000000"}],
            "outcomeTwo": [{"percentageOdds": str(scale // 4), "size": "2000000"}],
        })
        assert book["market_hash"] == "0xm" and book["version"] == "5"
        assert book["one"]["price"] == pytest.approx(2.0)
        assert book["one"]["size_usdc"] == pytest.approx(1.0)   # 1e6 unita' = 1 USDC
        assert book["two"]["price"] == pytest.approx(4.0)

    def test_parita_con_execution_engine(self):
        """La conversione non e' reimplementata: e' quella di produzione."""
        from execution_engine import pct_scaled_to_decimal
        scale = 10 ** 20
        pct = str(scale // 2)
        assert rt.normalize_book({
            "outcomeOne": [{"percentageOdds": pct, "size": "1000000"}]})["one"]["price"] \
            == pytest.approx(pct_scaled_to_decimal(pct))

    def test_livello_inutilizzabile_salta_al_successivo(self):
        scale = 10 ** 20
        book = rt.normalize_book({
            "outcomeOne": [{"percentageOdds": "0", "size": "0"},
                           {"percentageOdds": str(scale // 3), "size": "500000"}],
        })
        assert book["one"]["price"] == pytest.approx(3.0)
        assert book["one"]["levels"] == 2

    @pytest.mark.parametrize("payload", [
        None, "boh", {}, {"outcomeOne": [], "outcomeTwo": []},
        {"outcomeOne": [{"percentageOdds": "0", "size": "0"}]},
        {"outcomeOne": "non-lista", "outcomeTwo": None},
    ])
    def test_payload_inutilizzabile_nessun_book_inventato(self, payload):
        assert rt.normalize_book(payload) is None


# ---------------------------------------------------------------------------
# Client: protocollo
# ---------------------------------------------------------------------------

class TestClientProtocollo:
    def _run(self, script, channels=("orderbook_v3:0xm",), **kwargs):
        sink: list = []
        connect = fake_connect(script, sink)
        got: list = []
        client = rt.SxRealtimeClient(connect=connect, token_fn=lambda: "TK",
                                     on_publication=lambda c, p: got.append((c, p)),
                                     recv_timeout=0.2, **kwargs)
        stats = asyncio.run(client.run(channels, max_messages=1, timeout_s=2.0))
        return stats, got, connect, sink

    def test_flusso_completo(self):
        script = [connect_reply(1), subscribe_reply(2), book_push("0xm")]
        stats, got, connect, sink = self._run(script)
        assert stats["connected"] is True
        assert stats["subscribed"] == ["orderbook_v3:0xm"]
        assert stats["publications"] == 1 and stats["books"] == 1
        assert not stats["errors"]
        assert got and got[0][0] == "orderbook_v3:0xm"
        assert got[0][1]["one"]["price"] == pytest.approx(2.0)
        # i frame inviati sono il protocollo Centrifugo
        sent = sink[0].sent
        assert sent[0] == {"id": 1, "connect": {"token": "TK"}}
        assert sent[1] == {"id": 2, "subscribe": {"channel": "orderbook_v3:0xm"}}

    def test_ping_vuoto_viene_risposto(self):
        script = [connect_reply(1), subscribe_reply(2), "{}", book_push("0xm")]
        stats, _, _, sink = self._run(script)
        assert stats["pings"] == 1
        assert {} in sink[0].sent or "{}" in sink[0].sent

    def test_push_prima_della_conferma_non_si_perde(self):
        """Il push arrivato durante l'attesa della subscribe entra nella telemetria."""
        script = [connect_reply(1), book_push("0xm"), subscribe_reply(2),
                  book_push("0xm")]
        stats, got, _, _ = self._run(script)
        assert stats["publications"] >= 1     # il primo push e' stato contato
        assert not stats["errors"]

    def test_canale_muto_esce_per_timeout(self):
        script = [connect_reply(1), subscribe_reply(2)]
        stats, _, _, _ = self._run(script)
        assert stats["stopped"] == "timeout"
        assert stats["connected"] is True and not stats["errors"]

    def test_nessun_canale_e_errore(self):
        connect = fake_connect([])
        client = rt.SxRealtimeClient(connect=connect, token_fn=lambda: "TK")
        with pytest.raises(rt.RealtimeError):
            asyncio.run(client.run([]))

    def test_token_vuoto_fail_closed(self):
        connect = fake_connect([connect_reply(1)])
        client = rt.SxRealtimeClient(connect=connect, token_fn=lambda: "")
        with pytest.raises(rt.RealtimeConfigError):
            asyncio.run(client.run(["orderbook_v3:0xm"]))

    def test_errore_di_connessione_catturato_e_dichiarato(self):
        async def boom(url, **kwargs):
            raise ConnectionError("tls")

        client = rt.SxRealtimeClient(connect=boom, token_fn=lambda: "TK")
        stats = asyncio.run(client.run(["orderbook_v3:0xm"], timeout_s=1.0))
        assert stats["connected"] is False
        assert stats["errors"] and "ConnectionError" in stats["errors"][0]

    def test_errore_su_connect_diventa_errors(self):
        """`connect` risponde con errore: la sottoscrizione non parte."""
        script = [{"id": 1, "error": {"code": 3501, "message": "bad request"}}]
        connect = fake_connect(script)
        client = rt.SxRealtimeClient(connect=connect, token_fn=lambda: "TK")
        stats = asyncio.run(client.run(["orderbook_v3:0xm"], timeout_s=1.0))
        assert stats["subscribed"] == []
        assert stats["errors"] and "bad request" in stats["errors"][0]

    def test_consumatore_rotto_non_ferma_lo_stream(self):
        script = [connect_reply(1), subscribe_reply(2), book_push("0xm")]

        def _boom(channel, payload):
            raise RuntimeError("consumatore giu'")

        connect = fake_connect(script)
        client = rt.SxRealtimeClient(connect=connect, token_fn=lambda: "TK",
                                     on_publication=_boom, recv_timeout=0.2)
        stats = asyncio.run(client.run(["orderbook_v3:0xm"], max_messages=1,
                                       timeout_s=2.0))
        assert stats["publications"] == 1 and not stats["errors"]

    def test_canali_globali_sottoscritti(self):
        script = [connect_reply(1), subscribe_reply(2), subscribe_reply(3)] + \
                 ["{}"]
        connect = fake_connect(script)
        client = rt.SxRealtimeClient(connect=connect, token_fn=lambda: "TK",
                                     recv_timeout=0.2)
        stats = asyncio.run(client.run(list(rt.GLOBAL_CHANNELS), timeout_s=1.0))
        assert stats["subscribed"] == list(rt.GLOBAL_CHANNELS)

    def test_url_dal_modulo_passato_al_connettore(self, monkeypatch):
        monkeypatch.setenv("SX_REALTIME_URL", "wss://test/ws")
        connect = fake_connect([connect_reply(1), subscribe_reply(2)])
        client = rt.SxRealtimeClient(connect=connect, token_fn=lambda: "TK",
                                     recv_timeout=0.2)
        asyncio.run(client.run(["orderbook_v3:0xm"], timeout_s=1.0))
        assert connect.holder["url"] == "wss://test/ws"

    def test_default_connect_importa_websockets_a_pigrizia(self):
        """Il connettore di default esiste ma la libreria NON e' importata
        finche' non lo si usa (verificato nel processo dall'import leggero)."""
        client = rt.SxRealtimeClient(token_fn=lambda: "TK")
        assert client._connect is rt._default_connect


class TestReport:
    def test_report_ok(self):
        txt = rt.format_probe({"connected": True, "channels": ["a"],
                               "subscribed": ["a"], "publications": 3,
                               "books": 2, "pings": 1, "errors": []})
        assert "✅" in txt and "3 push" in txt

    def test_report_errore(self):
        txt = rt.format_probe({"connected": False, "channels": ["a"],
                               "errors": ["ConnectionError: tls"]})
        assert "❌" in txt and "ConnectionError" in txt


# ---------------------------------------------------------------------------
# Tripwire: sola lettura, import leggero
# ---------------------------------------------------------------------------

class TestTripwire:
    BANNED = ("place_limit_order", "orders-v3", "_live_fill", "save_bet",
              "save_prediction", "save_market_quotes", "sqlite3", "INSERT",
              "UPDATE ", "DELETE ")

    def _body(self) -> str:
        """Sorgente FUORI dal docstring (che cita i divieti per documentarli)."""
        src = Path(rt.__file__).read_text(encoding="utf-8")
        return src.split('"""', 2)[2]

    @pytest.mark.parametrize("token", BANNED)
    def test_nessun_ordine_ne_scrittura(self, token):
        assert token not in self._body(), token

    def test_nessun_import_di_produzione_nel_sorgente(self):
        tree = ast.parse(Path(rt.__file__).read_text(encoding="utf-8"))
        banned = {"auto_bet", "bot", "tracker", "sx_signals", "multi_market",
                  "poisson_engine", "decision"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(a.name.split(".")[0] not in banned for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] not in banned

    def test_import_leggero_in_subprocesso(self):
        out = subprocess.run(
            [sys.executable, "-c",
             "import sys, sx_realtime; "
             "print('HEAVY:' + ','.join(m for m in ('websockets','httpx',"
             "'execution_engine','auto_bet','tracker') if m in sys.modules))"],
            capture_output=True, text=True,
            cwd=str(Path(rt.__file__).parent))
        assert out.stdout.split("HEAVY:")[1].strip() == ""

    def test_nessuna_soglia_di_strategia(self):
        body = self._body()
        for token in ("EV_MIN", "MARKET_EDGE", "ODDS_MAX", "STAKE_CAP"):
            assert token not in body, token

    def test_cli_registrata(self):
        body = self._body()
        assert "--probe" in body and "--markets" in body
