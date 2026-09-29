"""sx_realtime.py — streaming REALTIME da SX Bet (Centrifugo) — SOLA LETTURA.

Direttiva 29/09/2026: gli agenti devono competere anche sui mercati LIVE.
Il percorso attuale interroga SX a POLLING (`/markets/active`, `/orderbook-v3/
snapshot`): 15 minuti fra un giro e l'altro e' un'eternita' su un mercato in
gioco, dove la quota si muove in secondi. SX espone un canale realtime
(`wss://realtime.sx.bet/connection/websocket`, protocollo Centrifugo) che
PUSHA il book a ogni variazione.

QUESTO MODULO E' ADDITIVO E NON SOSTITUISCE NULLA: la pipeline sincrona
(`sx_signals`, `multi_market`, `auto_bet`) resta esattamente com'e'. Il
realtime e' un percorso NUOVO che si innesta dove serve (aggiornamento del book
in finestra T-60, sorveglianza live) senza riscrivere il percorso dei soldi.

PROTOCOLLO (Centrifugo JSON su WebSocket, verificato sulla doc SX del 29/09):
  1. `GET /user/realtime-token-v3/api-key` con header `x-sx-api-key` -> token
     (senza chiave risponde 401 BAD_AUTH: il realtime NON e' anonimo);
  2. `{"id": N, "connect": {"token": ...}}` -> `{"id": N, "connect": {...}}`;
  3. `{"id": N, "subscribe": {"channel": ...}}` -> `{"id": N, "subscribe": {}}`;
  4. push: `{"push": {"channel": ..., "pub": {"data": {...}}}}`;
  5. il server manda frame vuoti `{}` come PING: si risponde `{}`.

Canali (doc SX, invariati da V2 a V3): `orderbook_v3:{marketHash}` (book
completo, NON delta), `orderbook_v3_event:{eventId}` (tutti i mercati di un
evento), `markets:global`, `main_line:global`, `fixtures:*`,
`parlay_markets:global`, `line_changes`.

GARANZIE (tripwire in test_sx_realtime.py):
- nessun ordine e nessuna scrittura: nel sorgente non compaiono
  `place_limit_order` / `orders-v3` / `_live_fill` / `save_bet` / `INSERT`;
- FAIL-CLOSED senza credenziali: senza `SX_API_KEY` solleva
  `RealtimeConfigError`, non inventa un token ne' un book;
- import PIGRI: `import sx_realtime` non carica `websockets`/`httpx`, quindi
  il modulo resta leggero per chi non usa il realtime;
- conversioni DELEGATE a `execution_engine` (`pct_scaled_to_decimal`,
  `sx_units_to_stake`): la scala 1e20 e i 6 decimali USDC vivono in UN posto,
  due copie divergerebbero (lezione 13/09);
- il connettore e' INIETTABILE: i test girano offline con un trasporto finto.

CLI (diagnostica, richiede `SX_API_KEY`):
    venv/bin/python sx_realtime.py --probe
    venv/bin/python sx_realtime.py --markets <marketHash>[,<marketHash>...]
    venv/bin/python sx_realtime.py --channels markets:global --max-messages 5
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Callable, Iterable, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "RealtimeError", "RealtimeConfigError", "SxRealtimeClient",
    "book_channel", "event_channel", "fetch_realtime_token",
    "normalize_book", "realtime_url", "DEFAULT_REALTIME_URL",
]

#: Endpoint realtime di PRODUZIONE (testnet: wss://realtime.toronto.sx.bet).
DEFAULT_REALTIME_URL = "wss://realtime.sx.bet/connection/websocket"

#: PERCORSO dell'endpoint che RESTITUISCE il token realtime (richiede
#: `x-sx-api-key`). Il nome dice che cos'e': un percorso, non una credenziale
#: — un nome con "TOKEN" dentro farebbe scattare la tripwire di igiene
#: (`test_secret_hygiene`), che non puo' distinguere un path da una chiave.
REALTIME_ENDPOINT_PATH = "/user/realtime-token-v3/api-key"

#: Canali globali utili (la doc SX li dichiara invariati da V2).
GLOBAL_CHANNELS = ("markets:global", "main_line:global")


class RealtimeError(RuntimeError):
    """Errore del percorso realtime (token, connessione, protocollo)."""


class RealtimeConfigError(RealtimeError):
    """Configurazione assente: nessuna credenziale, nessuna connessione.

    E' l'errore FAIL-CLOSED: senza chiave non si tenta nemmeno di aprire il
    canale, cosi' un errore di configurazione non si confonde con una rete
    muta (stessa lezione dei provider dell'intel, 29/09).
    """


# ---------------------------------------------------------------------------
# Nomi dei canali (tabella esplicita: mai stringhe sparse nel codice)
# ---------------------------------------------------------------------------

def book_channel(market_hash: str) -> str:
    """Canale del book di UN mercato: `orderbook_v3:{marketHash}`."""
    mh = str(market_hash or "").strip()
    if not mh:
        raise RealtimeError("market_hash vuoto: nessun canale del book")
    return f"orderbook_v3:{mh}"


def event_channel(event_id: str) -> str:
    """Canale di TUTTI i mercati di un evento: `orderbook_v3_event:{id}`."""
    eid = str(event_id or "").strip()
    if not eid:
        raise RealtimeError("event_id vuoto: nessun canale dell'evento")
    return f"orderbook_v3_event:{eid}"


def realtime_url(*, api_base: Optional[str] = None) -> str:
    """URL realtime (env `SX_REALTIME_URL` vince sempre).

    Derivato da `SX_API_BASE` per non dover configurare due volte il
    testnet (api.toronto.sx.bet -> realtime.toronto.sx.bet).
    """
    env = os.getenv("SX_REALTIME_URL", "").strip()
    if env:
        return env
    base = (api_base if api_base is not None
            else os.getenv("SX_API_BASE", "")).strip()
    if not base:
        return DEFAULT_REALTIME_URL
    if "toronto" in base:
        return "wss://realtime.toronto.sx.bet/connection/websocket"
    return DEFAULT_REALTIME_URL


def _api_base() -> str:
    """Base REST SX — dalla fonte unica di `execution_engine` (import pigro)."""
    try:
        from execution_engine import SX_API_BASE  # fonte unica
        return str(SX_API_BASE).rstrip("/")
    except Exception:
        return os.getenv("SX_API_BASE", "https://api.sx.bet").rstrip("/")


# ---------------------------------------------------------------------------
# Token realtime
# ---------------------------------------------------------------------------

async def fetch_realtime_token(*, api_key: Optional[str] = None,
                               client: Any = None,
                               base: Optional[str] = None,
                               timeout: float = 15.0) -> str:
    """Token realtime da `/user/realtime-token-v3/api-key`.

    `client` e' un `httpx.AsyncClient` iniettabile (test offline). Senza
    chiave NON si prova nulla: `RealtimeConfigError` (fail-closed).

    La risposta e' letta DIFENSIVAMENTE (`data.token`, `token`, `data` come
    stringa): una risposta senza token e' un errore dichiarato, mai un
    token vuoto che farebbe fallire la connessione piu' avanti, lontano
    dalla causa.
    """
    key = (api_key if api_key is not None
           else os.getenv("SX_API_KEY", "")).strip()
    if not key:
        raise RealtimeConfigError(
            "SX_API_KEY assente: il realtime SX richiede una chiave API "
            "(senza, l'endpoint del token risponde 401 BAD_AUTH)")

    url = f"{(base or _api_base()).rstrip('/')}{REALTIME_ENDPOINT_PATH}"
    owned = client is None
    if owned:
        import httpx  # import pigro
        client = httpx.AsyncClient(timeout=timeout)
    try:
        resp = await client.get(url, headers={"x-sx-api-key": key})
        status = int(getattr(resp, "status_code", 0) or 0)
        if status != 200:
            raise RealtimeError(f"token realtime: HTTP {status}")
        payload = resp.json()
    finally:
        if owned:
            try:
                await client.aclose()
            except Exception:
                pass

    token = _extract_token(payload)
    if not token:
        raise RealtimeError(
            f"risposta del token senza campo 'token': {str(payload)[:120]}")
    return token


def _extract_token(payload: Any) -> str:
    """Token dalla risposta, accettando le forme ragionevoli (mai inventarlo)."""
    if isinstance(payload, str):
        return payload.strip()
    if not isinstance(payload, dict):
        return ""
    if isinstance(payload.get("token"), str):
        return payload["token"].strip()
    data = payload.get("data")
    if isinstance(data, str):
        return data.strip()
    if isinstance(data, dict):
        for field in ("token", "jwt", "accessToken", "access_token"):
            value = data.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


# ---------------------------------------------------------------------------
# Book: da push Centrifugo alla forma usata dal progetto
# ---------------------------------------------------------------------------

def normalize_book(payload: Any) -> Optional[dict]:
    """Book di una publication -> dict con best price/size (quota decimale).

    Riusa le conversioni di `execution_engine` (scala 1e20, USDC 6 decimali)
    e ritorna None su payload inutilizzabile: meglio nessun aggiornamento che
    un book inventato. Il lato `one`/`two` resta separato — la semantica
    T1-vs-Not-T1 e' del chiamante, non di questo modulo.
    """
    if not isinstance(payload, dict):
        return None
    from execution_engine import pct_scaled_to_decimal, sx_units_to_stake  # pigro

    def _side(levels: Any) -> Optional[dict]:
        if not isinstance(levels, list):
            return None
        for lv in levels:
            if not isinstance(lv, dict):
                continue
            price = pct_scaled_to_decimal(lv.get("percentageOdds"))
            size = sx_units_to_stake(lv.get("size"))
            if price and price > 1.0 and size:
                return {"price": float(price), "size_usdc": float(size),
                        "levels": len(levels)}
        return None

    one = _side(payload.get("outcomeOne"))
    two = _side(payload.get("outcomeTwo"))
    if one is None and two is None:
        return None
    return {
        "market_hash": str(payload.get("marketHash") or ""),
        "event_id": str(payload.get("eventId") or ""),
        "version": str(payload.get("version") or ""),
        "one": one,
        "two": two,
    }


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

async def _default_connect(url: str, **kwargs: Any):
    """Connettore di PRODUZIONE (import pigro di `websockets`)."""
    import websockets  # import pigro: `import sx_realtime` resta leggero
    return await websockets.connect(url, **kwargs)


class SxRealtimeClient:
    """Client realtime SX: connette, sottoscrive, consegna le publication.

    Tutto e' iniettabile (`connect`, `token_fn`, `sleep`) cosi' i test girano
    offline. Il client NON scrive e NON ordina: consegna i payload al
    chiamante (`on_publication`) e ritorna le statistiche del giro.
    """

    def __init__(self, *, url: Optional[str] = None,
                 api_key: Optional[str] = None,
                 connect: Optional[Callable[..., Any]] = None,
                 token_fn: Optional[Callable[[], Any]] = None,
                 on_publication: Optional[Callable[[str, dict], None]] = None,
                 open_timeout: float = 15.0,
                 recv_timeout: float = 60.0,
                 sleep: Optional[Callable[[float], Any]] = None) -> None:
        self.url = url or realtime_url()
        self.api_key = api_key
        self._connect = connect or _default_connect
        self._token_fn = token_fn
        self.on_publication = on_publication
        self.open_timeout = float(open_timeout)
        self.recv_timeout = float(recv_timeout)
        self._sleep = sleep or asyncio.sleep

    async def _token(self) -> str:
        if self._token_fn is not None:
            value = self._token_fn()
            if asyncio.iscoroutine(value):
                value = await value
            return str(value or "")
        return await fetch_realtime_token(api_key=self.api_key)

    async def run(self, channels: Iterable[str], *,
                  max_messages: int = 0,
                  timeout_s: float = 0.0) -> dict:
        """Connette, sottoscrive i canali e consuma i push.

        `max_messages` (0 = illimitato) limita i push consegnati: serve al
        probe e ai test. `timeout_s` (0 = nessun limite) e' il tempo massimo
        del giro: il chiamante non resta appeso a un canale muto.

        Ritorna una telemetria dichiarata: mai un errore silenzioso.
        """
        chans = [str(c) for c in channels if str(c or "").strip()]
        if not chans:
            raise RealtimeError("nessun canale da sottoscrivere")
        token = await self._token()
        if not token:
            raise RealtimeConfigError(
                "token realtime vuoto: nessuna sottoscrizione (fail-closed)")

        stats = {"channels": chans, "connected": False, "subscribed": [],
                 "publications": 0, "pings": 0, "books": 0, "errors": [],
                 "token": bool(token)}
        loop = asyncio.get_running_loop()
        deadline = (loop.time() + float(timeout_s) if timeout_s else None)
        try:
            async with await self._connect(self.url,
                                           open_timeout=self.open_timeout) as ws:
                stats["connected"] = True
                await self._send(ws, 1, "connect", {"token": token})
                await self._await_reply(ws, 1, stats)

                for idx, chan in enumerate(chans, start=2):
                    await self._send(ws, idx, "subscribe", {"channel": chan})
                    await self._await_reply(ws, idx, stats)
                    stats["subscribed"].append(chan)

                while True:
                    if max_messages and stats["publications"] >= max_messages:
                        break
                    if deadline is not None and loop.time() >= deadline:
                        stats["stopped"] = "timeout"
                        break
                    frame = await self._recv(ws, deadline)
                    if frame is None:
                        stats["stopped"] = "timeout"
                        break
                    self._handle(frame, stats)
        except RealtimeConfigError:
            raise
        except Exception as exc:  # il chiamante decide, mai un crash muto
            stats["errors"].append(f"{type(exc).__name__}: {str(exc)[:160]}")
            logger.warning("sx_realtime: %s", stats["errors"][-1])
        return stats

    # -- protocollo ----------------------------------------------------
    @staticmethod
    async def _send(ws: Any, msg_id: int, command: str, payload: dict) -> None:
        await ws.send(json.dumps({"id": msg_id, command: payload}))

    async def _await_reply(self, ws: Any, msg_id: int, stats: dict) -> None:
        """Attende la risposta CON lo stesso id (mai un frame qualunque).

        Un push arrivato PRIMA della conferma non si perde: passa nella
        stessa telemetria del giro (`stats`), non in un contatore usa-e-getta.
        """
        while True:
            frame = await self._recv(ws, None)
            if frame is None:
                raise RealtimeError(f"nessuna risposta alla richiesta {msg_id}")
            if frame.get("id") == msg_id:
                if frame.get("error"):
                    raise RealtimeError(
                        f"richiesta {msg_id} rifiutata: {frame['error']}")
                return
            self._handle(frame, stats)

    async def _recv(self, ws: Any, deadline: Optional[float]) -> Optional[dict]:
        """Un frame (dict) o None se scade il tempo. Frame vuoto = PING."""
        try:
            if deadline is not None:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    return None
                raw = await asyncio.wait_for(ws.recv(),
                                             timeout=min(remaining,
                                                         self.recv_timeout))
            else:
                raw = await asyncio.wait_for(ws.recv(),
                                             timeout=self.recv_timeout)
        except asyncio.TimeoutError:
            return None
        if raw in ("", "{}", b"{}", b""):
            await ws.send("{}")  # reply al ping di Centrifugo
            return {"__ping__": True}
        try:
            return json.loads(raw)
        except Exception:
            return None

    def _handle(self, frame: dict, stats: dict) -> None:
        """Instrada un frame: ping, push o risposta."""
        if frame.get("__ping__"):
            stats["pings"] = int(stats.get("pings", 0)) + 1
            return
        push = frame.get("push")
        if not isinstance(push, dict):
            return
        channel = str(push.get("channel") or "")
        pub = push.get("pub") if isinstance(push.get("pub"), dict) else {}
        data = pub.get("data") if isinstance(pub.get("data"), dict) else {}
        stats["publications"] = int(stats.get("publications", 0)) + 1
        book = normalize_book(data)
        if book is not None:
            stats["books"] = int(stats.get("books", 0)) + 1
        if self.on_publication is not None:
            try:
                self.on_publication(channel, book if book is not None else data)
            except Exception as exc:  # un consumatore rotto non ferma lo stream
                logger.debug("sx_realtime: consumatore in errore: %s", exc)


def format_probe(stats: dict) -> str:
    """Riga leggibile del probe (diagnostica: mai un esito ambiguo)."""
    if stats.get("errors"):
        return (f"❌ realtime SX — canali {len(stats.get('channels') or [])}, "
                f"connesso={stats.get('connected')}, "
                f"errore: {stats['errors'][0]}")
    return (f"✅ realtime SX — connesso, {len(stats.get('subscribed') or [])} "
            f"canali sottoscritti, {stats.get('publications')} push "
            f"({stats.get('books')} book, {stats.get('pings')} ping)")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        description="Streaming realtime SX Bet (sola lettura, zero ordini)")
    ap.add_argument("--markets", default="",
                    help="marketHash separati da virgola (canale orderbook_v3)")
    ap.add_argument("--channels", default="",
                    help="canali espliciti separati da virgola")
    ap.add_argument("--probe", action="store_true",
                    help="prova token + connessione sui canali globali")
    ap.add_argument("--max-messages", type=int, default=3)
    ap.add_argument("--timeout", type=float, default=20.0)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    channels: list[str] = []
    if args.markets:
        channels += [book_channel(m) for m in args.markets.split(",") if m.strip()]
    if args.channels:
        channels += [c.strip() for c in args.channels.split(",") if c.strip()]
    if args.probe and not channels:
        channels = list(GLOBAL_CHANNELS)
    if not channels:
        ap.error("serve --probe oppure --markets/--channels")

    seen: list[dict] = []

    def _on_pub(channel: str, payload: dict) -> None:
        seen.append({"channel": channel, "payload": payload})

    client = SxRealtimeClient(on_publication=_on_pub)
    try:
        stats = asyncio.run(client.run(channels, max_messages=args.max_messages,
                                       timeout_s=args.timeout))
    except RealtimeError as exc:
        print(f"❌ {exc}")
        return 1
    if args.json:
        print(json.dumps({"stats": stats, "publications": seen},
                         indent=2, ensure_ascii=False, default=str))
    else:
        print(format_probe(stats))
        for item in seen[:args.max_messages]:
            print(f"  {item['channel']}: {json.dumps(item['payload'])[:160]}")
    return 0 if stats.get("connected") and not stats.get("errors") else 1


if __name__ == "__main__":
    raise SystemExit(main())
