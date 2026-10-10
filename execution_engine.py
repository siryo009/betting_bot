"""execution_engine.py — ExecutionEngine: esecuzione ordini (exchange/aggregatori).

Dal 06/09 l'esecuzione delle puntate passa dagli aggregatori professionali
(BetInAsia BLACK / MollyBet, protocollo Betfair-compatible JSON-RPC
SportsAPING/v1.0) al posto del conto Exchange diretto. Dal 07/09 sono
disponibili anche i provider **Smarkets** (REST pubblica v3,
api.smarkets.com/v3) e **SX Bet** (V3, api.sx.bet, exchange P2P crypto su
SX Rollup/Arbitrum Orbit, chainId 4162) — scambi di quote 1X2 su calcio.

Un'unica interfaccia Python (ExecutionProvider) copre tutti i provider:
- `betinasia` / `mollybet`: JSON-RPC Betfair-compatible, credenziali
  EXECUTION_APP_KEY / EXECUTION_USERNAME / EXECUTION_PASSWORD;
- `smarkets`: REST v3, credenziali SMARKETS_USERNAME / SMARKETS_PASSWORD
  (base URL personalizzabile con SMARKETS_API_BASE);
- `sxbet`: REST V3 crypto, credenziali SX_API_KEY / SX_PRIVATE_KEY (firma
  EIP-712 degli ordini, fondi nel proxy wallet; base URL personalizzabile
  con SX_API_BASE, default api.sx.bet, testnet api.toronto.sx.bet);
- `dry_run` (default senza credenziali o con EXECUTION_DRY_RUN=1):
  nessuna rete, misura simulata.

Obiettivo immediato: MISURARE latenza e slippage reali con stake minimo
(EXECUTION_MIN_STAKE_EUR, default 1€) prima di passare a stake reali.
Ogni probe scrive una riga in data/execution/measurements.jsonl con
latency_ms, prezzo richiesto vs matched, slippage e stato dell'ordine.

Vincoli:
- Nessuna credenziale hardcoded: tutte le credenziali SOLO da env,
  coerentemente col vault segreti del progetto. Tripwire
  test_secret_hygiene.py incluso.
- Il modulo NON importa tracker/bot (indipendente, come surebet_engine).

Uso:
    venv/bin/python execution_engine.py --status                 # provider + creds configurati?
    venv/bin/python execution_engine.py --markets [--max 20]     # elenca i mercati calcio aperti
    venv/bin/python execution_engine.py --probe --market <id> --selection <id> [--price 2.0]
    venv/bin/python execution_engine.py --probe --dry-run        # probe simulata (no rete)

    # SX Bet (EXECUTION_PROVIDER=sxbet oppure --provider sxbet; discovery
    # e book pubblici senza chiave, ordini firmati EIP-712 con la private key):
    venv/bin/python execution_engine.py --provider sxbet --markets --max 10
    SX_API_KEY=... SX_PRIVATE_KEY=0x... \
        venv/bin/python execution_engine.py --provider sxbet --probe \
        --market <marketHash_hex> --selection 1 [--price 2.0]

    # Smarkets (EXECUTION_PROVIDER=smarkets oppure --provider smarkets):
    SMARKETS_USERNAME=... SMARKETS_PASSWORD=... \
        venv/bin/python execution_engine.py --provider smarkets --probe \
        --market <market_id> --selection <contract_id> [--price 2.0]
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import requests

from config import DATA_DIR

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Risposta HTTP CRUDA: strumento di diagnosi
#
# Un try/except a valle puo' mascherare la differenza fra "ordine accettato"
# e "HTTP 4xx/5xx". Queste due helper rendono la risposta grezza LEGGIBILE
# nei log (senza mai stampare credenziali) e troncata, cosi' un log non
# diventa esso stesso un problema. Nessun uso in decisioni: solo evidenza.
# ---------------------------------------------------------------------------

# Quanti caratteri del corpo si mostrano nei log (env override).
EXECUTION_RAW_LOG_LIMIT = int(os.getenv("EXECUTION_RAW_LOG_LIMIT", "4000"))

# Campi la cui VALORE non deve finire nei log (la chiave resta visibile,
# cosi' il resto del payload e' ispezionabile).
_SENSITIVE_RAW_KEYS = ("orderSignature", "signature", "privateKey",
                       "private_key", "apiKey", "api_key",
                       "x-sx-api-key", "authorization")


def redact_raw_http(text: object, limit: Optional[int] = None) -> str:
    """Tronca e maschera il corpo di una risposta HTTP, per il log.

    Maschera per NOME DI CAMPO (non per valore): la risposta di SX Bet non
    contiene la chiave privata, ma contiene la firma EIP-712 dell'ordine —
    e il filtro di secure_logging maschera per valore, quindi non la
    coprirebbe. Tutto il resto del payload resta leggibile.
    """
    raw = text if isinstance(text, str) else str(text)
    cap = limit if limit is not None else EXECUTION_RAW_LOG_LIMIT
    body = raw[:cap]
    for key in _SENSITIVE_RAW_KEYS:
        body = re.sub(r'("%s"\s*:\s*")([^"]*)(")' % re.escape(key),
                      r'\1<redacted>\3', body, flags=re.IGNORECASE)
    if len(raw) > cap:
        body += f"...[troncato: {len(raw) - cap} char non mostrati]"
    return body

# ---------------------------------------------------------------------------
# Config (env, con default sicuri)
# ---------------------------------------------------------------------------
EXECUTION_DATA_DIR = Path(os.getenv("EXECUTION_DATA_DIR", str(DATA_DIR / "execution")))
MEASUREMENTS_LOG = EXECUTION_DATA_DIR / "measurements.jsonl"

# Provider: "smarkets" | "betinasia" | "mollybet" | "" (auto: dry_run
# senza credenziali). Il selettore reale è in build_provider().
EXECUTION_PROVIDER = os.getenv("EXECUTION_PROVIDER", "").strip().lower()

# Credenziali aggregatore — SOLO da env, mai hardcoded.
EXECUTION_APP_KEY = os.getenv("EXECUTION_APP_KEY", "")
EXECUTION_USERNAME = os.getenv("EXECUTION_USERNAME", "")
EXECUTION_PASSWORD = os.getenv("EXECUTION_PASSWORD", "")

# Endpoint Betfair-compatible (default: ufficiali Betfair; gli aggregatori
# usano lo stesso protocollo — personalizzabili via env se serve).
EXECUTION_API_BASE = os.getenv(
    "EXECUTION_API_BASE",
    "https://api.betfair.com/exchange/betting/json-rpc/v1")
EXECUTION_LOGIN_URL = os.getenv(
    "EXECUTION_LOGIN_URL",
    "https://identitysso.betfair.com/api/login")

# Stake minimo per il probe di latenza/slippage (default 1€, come richiesto).
# NB: l'exchange può imporre minimi reali più alti; qui si misura, non si
# fa profitto. EXECUTION_MAX_STAKE_EUR è un tetto di sicurezza.
EXECUTION_MIN_STAKE_EUR = float(os.getenv("EXECUTION_MIN_STAKE_EUR", "1.0"))
EXECUTION_MAX_STAKE_EUR = float(os.getenv("EXECUTION_MAX_STAKE_EUR", "10.0"))
EXECUTION_TIMEOUT = float(os.getenv("EXECUTION_TIMEOUT", "10"))

# Forza la modalità DryRun anche con credenziali presenti (per test/sicurezza).
EXECUTION_DRY_RUN = os.getenv("EXECUTION_DRY_RUN", "").lower() in (
    "1", "true", "yes", "on")

# ---------------------------------------------------------------------------
# Credenziali Smarkets — SOLO da env, mai hardcoded.
# ---------------------------------------------------------------------------
# Provider Smarkets: API REST pubblica v3 (api.smarkets.com/v3).
# Autenticazione: POST {base}sessions/ con username/password -> token di
# sessione usato nell'header `Authorization: Session-Token <token>`.
# Prezzi in probabilita' * 1e4 (5000 = 50% = quota 2.0), quantita' in
# stake * 1e4 (10000 = 1.00 EUR). Mercati 1X2 calcio: event type
# `football_match`, market type `match_odds`, contratti Home/Draw/Away.
SMARKETS_USERNAME = os.getenv("SMARKETS_USERNAME", "")
SMARKETS_PASSWORD = os.getenv("SMARKETS_PASSWORD", "")
SMARKETS_API_BASE = os.getenv(
    "SMARKETS_API_BASE", "https://api.smarkets.com/v3/")

# ---------------------------------------------------------------------------
# Credenziali SX Bet — SOLO da env, mai hardcoded.
# ---------------------------------------------------------------------------
# Provider SX Bet V3 (docs.sx.bet, V3 live dal 26/08/2026): exchange P2P
# crypto su SX Rollup (Arbitrum Orbit, chainId 4162).
# - letture pubbliche (markets/book/metadata) SENZA chiave;
# - scritture (ordini, saldo, cancel) con header `x-sx-api-key`;
# - gli ordini vanno firmati EIP-712 con la chiave privata dell'EOA
#   (SX_PRIVATE_KEY); il capitale sta nel proxy wallet dell'account
#   (POST /user/deploy-proxy + funding), NON nell'EOA.
SX_API_KEY = os.getenv("SX_API_KEY", "")
SX_PRIVATE_KEY = os.getenv("SX_PRIVATE_KEY", "")
SX_API_BASE = os.getenv("SX_API_BASE", "https://api.sx.bet")
# timeInForce ordini: IOC/FOK = take immediato, GTC = resta sul book.
SX_TIME_IN_FORCE = os.getenv("SX_TIME_IN_FORCE", "IOC").upper()
# TTL dell'ordine firmato (unix seconds, dentro la firma EIP-712).
SX_EXPIRY_SECONDS = int(os.getenv("SX_EXPIRY_SECONDS", "3600"))

# ---------------------------------------------------------------------------
# Conversioni prezzo/stake Smarkets (unita' 1e-4)
# ---------------------------------------------------------------------------

def decimal_to_prob_bps(price: float) -> int:
    """Quota decimale -> probabilita' in basis point Smarkets.

    Smarkets tratta i contratti come probabilita' * 1e4: quota 2.0 -> 5000,
    quota 200.0 -> 50 (come nel sample ufficiale smk_trading_bot).
    """
    if price < 1.01:
        raise ValueError(f"quota decimale non valida per Smarkets: {price}")
    return max(1, int(round(1.0 / price * 10000)))


def prob_bps_to_decimal(bps: int) -> float:
    """Probabilita' in basis point Smarkets -> quota decimale (5000 -> 2.0)."""
    if not bps or bps <= 0:
        return 0.0
    return round(10000.0 / float(bps), 2)


def stake_to_quantity(stake: float) -> int:
    """Stake in valuta -> quantita' Smarkets (stake * 1e4): 1.0 EUR -> 10000."""
    return max(1, int(round(stake * 10000)))


def quantity_to_stake(qty: object) -> float:
    """Quantita' Smarkets -> stake in valuta (10000 -> 1.0)."""
    return round(float(qty) / 10000.0, 4)

# ---------------------------------------------------------------------------
# Conversioni prezzo/stake SX Bet (probabilita' * 1e20, ladder 0.125%)
# ---------------------------------------------------------------------------
# SX Bet tratta gli esiti come probabilita' implicita * 1e20:
# quota 2.0 -> 5.0e19, 31.5% -> 3.15e19. La scala quote (odds ladder) ha
# gradini da 0.125% (oddsLadderStepSize=125 dal metadata -> 1.25e17).
SX_PROB_SCALE = 10 ** 20


def sx_ladder_step_scaled(ladder_step_size: int) -> int:
    """Gradino della scala quote (oddsLadderStepSize) in scala 1e20.

    Dal metadata `/metadata/obv3`: oddsLadderStepSize=125 = 0.125% ->
    gradino = 125 * 1e15 = 1.25e17 in probabilita' * 1e20.
    """
    return max(1, int(ladder_step_size)) * 10 ** 15


def decimal_to_pct_scaled(price: float, step_scaled: int) -> int:
    """Quota decimale BACK -> percentageOdds (prob. * 1e20) sulla ladder.

    Arrotonda per difetto al gradino (bound del taker): si accetta al piu'
    p = 1/quota, quindi se 1/quota non e' sulla scala il gradino piu' vicino
    NON peggiore della quota richiesta e' quello inferiore — mai riempimenti
    sotto la quota richiesta.
    """
    if price < 1.01:
        raise ValueError(f"quota decimale non valida per SX Bet: {price}")
    step = max(1, int(step_scaled))
    p_raw = SX_PROB_SCALE / float(price)
    return max(step, int(p_raw // step) * step)


def pct_scaled_to_decimal(pct: object) -> float:
    """percentageOdds (prob. * 1e20) -> quota decimale (3.15e19 -> 3.1746).

    TRONCA (mai arrotonda per eccesso) a 4 decimali, con aritmetica INTERA
    per non dipendere dai float. Il valore riportato e' quindi SEMPRE <= alla
    quota reale della ladder, e `decimal_to_pct_scaled` recupera il gradino
    ESATTO (round-trip lossless su tutta la banda 1.30-2.60).

    Perche' non si arrotonda: con l'arrotondamento per eccesso il prezzo
    riportato poteva risultare MAGGIORE della quota reale, e l'encoder
    (floor alla ladder) scendeva di un gradino -> l'ordine IOC chiedeva
    PIU' della quota disponibile sul book. Misurato il 10/10/2026: 131/308
    (42,5%) dei prezzi di libro in banda 1.30-2.60 non tornavano al gradino
    originale, cioe' un ordine al prezzo del book non trovava controparte
    (`CANCELLED / NO_LIQUIDITY`) — la causa del fill-rate del 5,5%.
    """
    try:
        p = int(pct)
    except (TypeError, ValueError):
        return 0.0
    if p <= 0:
        return 0.0
    return (SX_PROB_SCALE * 10000 // p) / 10000.0


def stake_to_sx_units(stake: float, decimals: int = 6) -> int:
    """Stake in valuta -> unita' base del token (USDC: 1.0 -> 1_000_000)."""
    return max(1, int(round(stake * (10 ** int(decimals)))))


def sx_units_to_stake(units: object, decimals: int = 6) -> float:
    """Unita' base SX Bet -> stake in valuta (1_000_000 -> 1.0 USDC)."""
    try:
        return round(float(units) / (10 ** int(decimals)), 4)
    except (TypeError, ValueError):
        return 0.0


def _sx_levels_to_decimal(levels: object) -> List[Dict]:
    """Livelli del book SX Bet (maker frame o taker) -> {price, size} decimali."""
    out: List[Dict] = []
    if not isinstance(levels, list):
        return out
    for lv in levels:
        if not isinstance(lv, dict):
            continue
        try:
            p = int(lv.get("percentageOdds") or 0)
        except (TypeError, ValueError):
            continue
        if p <= 0:
            continue
        out.append({
            "price": pct_scaled_to_decimal(p),
            "probability": round(p / SX_PROB_SCALE, 8),
            "size": sx_units_to_stake(lv.get("size")),
        })
    return out

# ---------------------------------------------------------------------------
# Dataclass risultati
# ---------------------------------------------------------------------------

@dataclass
class OrderResult:
    ok: bool
    bet_id: Optional[str]
    status: str                      # SUCCESS / FAILURE / TIMEOUT / dry-run
    price_requested: float
    price_matched: Optional[float]
    size_matched: float
    latency_ms: float
    error: Optional[str] = None


@dataclass
class ProbeResult:
    provider: str
    timestamp: str
    market_id: str
    selection_id: str
    side: str
    stake: float
    price_best_available: Optional[float]   # prezzo migliore al momento del probe
    price_requested: float
    price_matched: Optional[float]
    slippage: Optional[float]               # matched - richiesto (BACK: negativo = peggio)
    slippage_vs_best: Optional[float]       # matched - best_available
    latency_ms: float
    order_status: str
    ok: bool
    error: Optional[str] = None
    bet_id: Optional[str] = None          # id ordine/scommessa generato dal provider


# ---------------------------------------------------------------------------
# Provider interface
# ---------------------------------------------------------------------------

class ExecutionProvider(ABC):
    """Interfaccia comune agli aggregatori (BetInAsia BLACK, MollyBet, ...)."""

    name: str = "base"

    def __init__(self, app_key: str = "", username: str = "",
                 password: str = "") -> None:
        self.app_key = app_key
        self.username = username
        self.password = password
        self._token: Optional[str] = None
        self._token_ts: float = 0.0

    @abstractmethod
    def get_balance(self) -> Dict:
        ...

    @abstractmethod
    def get_market_book(self, market_id: str) -> Dict:
        ...

    @abstractmethod
    def place_limit_order(self, market_id: str, selection_id: int,
                          side: str, price: float, size: float,
                          persistence: str = "LAPSE") -> OrderResult:
        ...

    @abstractmethod
    def cancel_order(self, market_id: str, bet_id: str) -> bool:
        ...

    @abstractmethod
    def best_back_price(self, market_id: str, selection_id: int) -> Optional[float]:
        """Miglior prezzo BACK disponibile per la selezione (o None)."""

    @abstractmethod
    def list_market_catalogue(self, event_type_ids: tuple = ("1",),
                              market_type: str = "MATCH_ODDS",
                              max_results: int = 20) -> List[Dict]:
        """Elenca i mercati disponibili (calcio per default: event type 1).

        Ritorna una lista di dict con almeno:
            market_id, market_name, event_name, country_code, open_date,
            total_matched, runners (lista di {selection_id, name}).
        """


class BetfairJsonRpcProvider(ExecutionProvider):
    """Provider Betfair-compatible JSON-RPC (SportsAPING/v1.0).

    Protocollo usato anche dagli aggregatori BetInAsia BLACK e MollyBet:
    login via identitysso (form username/password + header X-Application)
    e JSON-RPC su /exchange/betting/json-rpc/v1 con X-Authentication.
    """

    name = "betfair-compatible"

    def _login(self) -> str:
        """Login e cache del token di sessione."""
        if self._token and (time.time() - self._token_ts) < 3600:
            return self._token
        if not (self.app_key and self.username and self.password):
            raise RuntimeError("credenziali aggregatore mancanti "
                               "(EXECUTION_APP_KEY/USERNAME/PASSWORD)")
        resp = requests.post(
            EXECUTION_LOGIN_URL,
            data={"username": self.username, "password": self.password},
            headers={"X-Application": self.app_key},
            timeout=EXECUTION_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "SUCCESS":
            raise RuntimeError(f"login aggregatore fallito: {data.get('error') or data}")
        self._token = data["token"]
        self._token_ts = time.time()
        return self._token

    def _rpc(self, method: str, params: Dict) -> tuple:
        """Esegue una chiamata JSON-RPC; ritorna (result, latency_ms)."""
        token = self._login()
        t0 = time.perf_counter()
        resp = requests.post(
            EXECUTION_API_BASE,
            json={"jsonrpc": "2.0", "method": f"SportsAPING/v1.0/{method}",
                  "params": params, "id": "1"},
            headers={"X-Application": self.app_key,
                     "X-Authentication": token,
                     "Content-Type": "application/json"},
            timeout=EXECUTION_TIMEOUT)
        latency_ms = (time.perf_counter() - t0) * 1000.0
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError(f"{method} error: {data['error']}")
        return data.get("result"), latency_ms

    def get_balance(self) -> Dict:
        result, _ = self._rpc("getAccountFunds", {})
        return result or {}

    def list_market_catalogue(self, event_type_ids: tuple = ("1",),
                              market_type: str = "MATCH_ODDS",
                              max_results: int = 20) -> List[Dict]:
        """listMarketCatalogue (Betfair-compatible): mercati calcio match odds.

        Finestra temporale: da 1h fa (per includere partite in corso) a +48h
        (si escludono i futuri remoti, dove lo stake minimo del probe rischia
        di non trovare book). Ordinati per kickoff imminente.
        """
        now = datetime.now(timezone.utc)
        window = {
            "from": (now - timedelta(hours=1)).isoformat(),
            "to": (now + timedelta(hours=48)).isoformat(),
        }
        result, _ = self._rpc("listMarketCatalogue", {
            "filter": {
                "eventTypeIds": list(event_type_ids),
                "marketTypeCodes": [market_type],
                "marketStartTime": window,
            },
            "maxResults": max_results,
            "marketProjection": ["COMPETITION", "EVENT", "RUNNER_DESCRIPTION",
                                 "MARKET_START_TIME"],
            "sort": "FIRST_TO_START",
        })
        out: List[Dict] = []
        for m in (result or []):
            runners = [{"selection_id": r.get("selectionId"),
                        "name": r.get("runnerName")}
                       for r in m.get("runners", [])]
            out.append({
                "market_id": m.get("marketId"),
                "market_name": m.get("marketName"),
                "event_name": (m.get("event") or {}).get("name"),
                "event_id": (m.get("event") or {}).get("id"),
                "country_code": (m.get("event") or {}).get("countryCode"),
                "open_date": (m.get("event") or {}).get("openDate"),
                "total_matched": m.get("totalMatched"),
                "runners": runners,
            })
        return out

    def get_market_book(self, market_id: str) -> Dict:
        result, _ = self._rpc("listMarketBook", {
            "marketIds": [market_id],
            "priceProjection": {"priceData": ["EX_BEST_AVAILABLE"]},
        })
        books = result or []
        return books[0] if books else {}

    def best_back_price(self, market_id: str, selection_id: int) -> Optional[float]:
        book = self.get_market_book(market_id)
        for runner in book.get("runners", []):
            if runner.get("selectionId") != selection_id:
                continue
            ex = runner.get("ex") or {}
            for price, _size in ex.get("availableToBack", []):
                return float(price)
        return None

    def place_limit_order(self, market_id: str, selection_id: int,
                          side: str, price: float, size: float,
                          persistence: str = "LAPSE") -> OrderResult:
        side = side.upper()
        if side not in ("BACK", "LAY"):
            return OrderResult(False, None, "FAILURE", price, None, 0.0,
                               0.0, error=f"side non valido: {side}")
        t0 = time.perf_counter()
        try:
            result, _ = self._rpc("placeOrders", {
                "marketId": market_id,
                "instructions": [{
                    "selectionId": selection_id,
                    "side": side,
                    "orderType": "LIMIT",
                    "limitOrder": {
                        "size": size, "price": price,
                        "persistenceType": persistence,
                    },
                }],
            })
        except Exception as e:
            latency_ms = (time.perf_counter() - t0) * 1000.0
            return OrderResult(False, None, "FAILURE", price, None, 0.0,
                               latency_ms, error=str(e))
        latency_ms = (time.perf_counter() - t0) * 1000.0
        reports = (result or {}).get("instructionReports", [])
        rep = reports[0] if reports else {}
        ok = (result or {}).get("status") == "SUCCESS" and \
            rep.get("status") == "SUCCESS"
        return OrderResult(
            ok=ok,
            bet_id=str(rep.get("betId")) if rep.get("betId") else None,
            status=rep.get("status") or (result or {}).get("status") or "UNKNOWN",
            price_requested=price,
            price_matched=rep.get("averagePriceMatched"),
            size_matched=float(rep.get("sizeMatched") or 0.0),
            latency_ms=latency_ms,
            error=None if ok else str(rep.get("errorCode") or "place failed"),
        )

    def cancel_order(self, market_id: str, bet_id: str) -> bool:
        try:
            result, _ = self._rpc("cancelOrders", {
                "marketId": market_id,
                "instructions": [{"betId": bet_id}],
            })
            reports = (result or {}).get("instructionReports", [])
            return bool(reports) and reports[0].get("status") == "SUCCESS"
        except Exception:
            return False


class BetInAsiaBlackProvider(BetfairJsonRpcProvider):
    """Aggregatore BetInAsia BLACK (protocollo Betfair-compatible)."""

    name = "betinasia"


class MollyBetProvider(BetfairJsonRpcProvider):
    """Aggregatore MollyBet (protocollo Betfair-compatible)."""

    name = "mollybet"


class SmarketsProvider(ExecutionProvider):
    """Provider Smarkets — API REST pubblica v3 (https://api.smarkets.com/v3/).

    Protocollo (dal sample ufficiale Smarkets `smk_trading_bot`):
    - autenticazione: POST {base}sessions/ con {username, password} ->
      {token}; le richieste autenticate usano l'header
      `Authorization: Session-Token <token>` (token in cache 1h);
    - prezzi in probabilita' * 1e4 (50 = 0.5% = quota decimale 200.0),
      quantita' in stake * 1e4 (500000 = 50.00 EUR);
    - side: `buy` = BACK, `sell` = LAY (Smarkets non ha i persistence type
      Betfair: il parametro viene accettato e ignorato);
    - mercato 1X2 calcio: event type `football_match`, market type
      `match_odds`, contratti "Home"/"Draw"/"Away" — il contract_id
      corrisponde al selection_id dell'interfaccia.

    Le quote (`markets/<id>/quotes/`) espongono per ogni contratto il
    miglior prezzo `buy` e `sell` in basis point: `best_back_price`
    restituisce il prezzo `buy` convertito in quota decimale (la semantica
    buy/sell esatta va verificata col primo probe reale).
    """

    name = "smarkets"

    def __init__(self, username: str = "", password: str = "",
                 api_base: Optional[str] = None) -> None:
        super().__init__(app_key="", username=username, password=password)
        self.api_base = (api_base or SMARKETS_API_BASE).rstrip("/") + "/"

    # -- auth ----------------------------------------------------------
    def _login(self) -> str:
        """Login a sessions/ e cache del token di sessione (1h)."""
        if self._token and (time.time() - self._token_ts) < 3600:
            return self._token
        if not (self.username and self.password):
            raise RuntimeError(
                "credenziali Smarkets mancanti (SMARKETS_USERNAME/PASSWORD)")
        resp = requests.post(
            f"{self.api_base}sessions/",
            json={"username": self.username, "password": self.password},
            timeout=EXECUTION_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        token = data.get("token") if isinstance(data, dict) else None
        if not token:
            raise RuntimeError(f"login Smarkets fallito: {data}")
        self._token = token
        self._token_ts = time.time()
        return self._token

    def _headers(self) -> Dict:
        return {"Authorization": f"Session-Token {self._login()}"}

    def _get(self, path: str, params: Optional[Dict] = None) -> Dict:
        resp = requests.get(
            f"{self.api_base}{path}", params=params, headers=self._headers(),
            timeout=EXECUTION_TIMEOUT)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, payload: Dict) -> Dict:
        resp = requests.post(
            f"{self.api_base}{path}", json=payload, headers=self._headers(),
            timeout=EXECUTION_TIMEOUT)
        resp.raise_for_status()
        return resp.json()

    # -- interfaccia ExecutionProvider --------------------------------
    def get_balance(self) -> Dict:
        data = self._get("accounts/")
        accounts = data.get("accounts") or []
        return accounts[0] if accounts else data

    def list_market_catalogue(self, event_type_ids: tuple = ("football_match",),
                              market_type: str = "match_odds",
                              max_results: int = 20) -> List[Dict]:
        """Discovery: eventi calcio (`football_match`) + mercati + contratti.

        Finestra temporale −1h/+48h (come il provider Betfair-compatible),
        ordinati per kickoff imminente. Per ogni evento scarica i mercati
        (filtra per `market_type`, default match_odds = 1X2) e i contratti.
        Fail-soft: un evento/mercato con errori viene saltato con un log.
        """
        now = datetime.now(timezone.utc)
        params = {
            "types": ",".join(event_type_ids),
            "states": "upcoming",
            "sort": "start_datetime",
            "limit": max_results,
            "start_datetime_min": (now - timedelta(hours=1)).isoformat(),
            "start_datetime_max": (now + timedelta(hours=48)).isoformat(),
        }
        data = self._get("events/", params)
        events = data.get("events") or []
        out: List[Dict] = []
        for ev in events:
            event_id = ev.get("id")
            try:
                mdata = self._get(f"events/{event_id}/markets/",
                                  {"with_volumes": "true"})
            except Exception as e:
                logger.warning("smarkets: mercati evento %s falliti: %s",
                               event_id, e)
                continue
            for m in (mdata.get("markets") or []):
                if market_type and m.get("type") != market_type:
                    continue
                try:
                    cdata = self._get(f"markets/{m['id']}/contracts/")
                except Exception as e:
                    logger.warning("smarkets: contratti mercato %s falliti: %s",
                                   m.get("id"), e)
                    continue
                runners = [{"selection_id": c.get("id"),
                            "name": c.get("name")}
                           for c in (cdata.get("contracts") or [])]
                # volume totale dal payload dei mercati (with_volumes)
                total = None
                for c in (m.get("contracts") or []):
                    v = c.get("volume")
                    if v is not None:
                        total = (total or 0.0) + float(v)
                out.append({
                    "market_id": m.get("id"),
                    "market_name": m.get("name"),
                    "event_name": ev.get("name"),
                    "event_id": event_id,
                    "country_code": None,
                    "open_date": ev.get("start_datetime"),
                    "total_matched": total,
                    "runners": runners,
                })
        return out

    def get_market_book(self, market_id: str) -> Dict:
        quotes = self._get(f"markets/{market_id}/quotes/")
        if not isinstance(quotes, dict):
            return {"marketId": market_id, "status": "OPEN", "runners": []}
        return {
            "marketId": market_id, "status": "OPEN",
            "runners": [{"selectionId": int(cid) if str(cid).isdigit() else cid,
                          "quotes": book}
                         for cid, book in quotes.items()],
        }

    def best_back_price(self, market_id: str, selection_id: int) -> Optional[float]:
        """Miglior prezzo `buy` del contratto, convertito in quota decimale."""
        try:
            quotes = self._get(f"markets/{market_id}/quotes/")
        except Exception as e:
            logger.warning("smarkets: quote mercato %s fallite: %s",
                           market_id, e)
            return None
        if not isinstance(quotes, dict):
            return None
        book = quotes.get(str(selection_id))
        if not isinstance(book, dict):
            return None
        bps = _quotes_entry_price(book.get("buy"))
        if not bps:
            return None
        return prob_bps_to_decimal(int(bps))

    def place_limit_order(self, market_id: str, selection_id: int,
                          side: str, price: float, size: float,
                          persistence: str = "LAPSE") -> OrderResult:
        side = side.upper()
        sm_side = {"BACK": "buy", "LAY": "sell"}.get(side)
        if sm_side is None:
            return OrderResult(False, None, "FAILURE", price, None, 0.0,
                               0.0, error=f"side non valido: {side}")
        try:
            price_bps = decimal_to_prob_bps(price)
        except ValueError as e:
            return OrderResult(False, None, "FAILURE", price, None, 0.0,
                               0.0, error=str(e))
        quantity = stake_to_quantity(size)
        t0 = time.perf_counter()
        try:
            data = self._post("orders/", {
                "market_id": market_id,
                "contract_id": selection_id,
                "side": sm_side,
                "price": price_bps,
                "quantity": quantity,
                "reference_id": str(int(time.time() * 1000)),
            })
        except Exception as e:
            latency_ms = (time.perf_counter() - t0) * 1000.0
            return OrderResult(False, None, "FAILURE", price, None, 0.0,
                               latency_ms, error=str(e))
        latency_ms = (time.perf_counter() - t0) * 1000.0

        order = data.get("order") if isinstance(data.get("order"), dict) else None
        if order is None:
            orders = data.get("orders")
            if isinstance(orders, list) and orders and isinstance(orders[0], dict):
                order = orders[0]
        order = order if isinstance(order, dict) else data

        status = str(order.get("status") or "SUCCESS").upper()
        ok = status in ("CREATED", "FILLED", "PARTIAL", "SUCCESS")
        matched_price = order.get("average_executed_price") \
            or order.get("average_price")
        matched_qty = order.get("executed_quantity") \
            or order.get("matched_quantity") or 0
        return OrderResult(
            ok=ok,
            bet_id=str(order.get("id") or order.get("order_id")
                        or "") or None,
            status=status,
            price_requested=price,
            price_matched=prob_bps_to_decimal(int(matched_price))
            if matched_price else None,
            size_matched=quantity_to_stake(matched_qty),
            latency_ms=latency_ms,
            error=None if ok else str(order.get("error_type")
                                      or order.get("error") or "place failed"),
        )

    def cancel_order(self, market_id: str, bet_id: str) -> bool:
        try:
            resp = requests.delete(
                f"{self.api_base}orders/{bet_id}/", headers=self._headers(),
                timeout=EXECUTION_TIMEOUT)
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.warning("smarkets: cancel ordine %s fallita: %s", bet_id, e)
            return False


def _quotes_entry_price(entry: object) -> Optional[int]:
    """Estrae il prezzo (bps) da un'entrata quote Smarkets, difensivo.

    Supporta sia il formato dict {"price": ...} che liste di [price, qty]
    o di dict {"price": ...} (primo elemento = migliore).
    """
    if isinstance(entry, dict):
        price = entry.get("price")
        return int(price) if price else None
    if isinstance(entry, list):
        for item in entry:
            if isinstance(item, dict) and item.get("price"):
                return int(item["price"])
            if isinstance(item, (list, tuple)) and item and item[0]:
                return int(item[0])
    return None


# ---------------------------------------------------------------------------
# Provider SX Bet (V3)
# ---------------------------------------------------------------------------

# Campi firmati EIP-712 dell'ordine V3 (docs.sx.bet/api-reference/eip712-order-signing).
_SX_ORDER_TYPES = {
    "Order": [
        {"name": "marketHash", "type": "bytes32"},
        {"name": "baseToken", "type": "address"},
        {"name": "totalBetSize", "type": "uint256"},
        {"name": "percentageOdds", "type": "uint256"},
        {"name": "salt", "type": "uint256"},
        {"name": "expiry", "type": "uint256"},
        {"name": "maker", "type": "address"},
        {"name": "isMakerBettingOutcomeOne", "type": "bool"},
    ],
}


class SxBetProvider(ExecutionProvider):
    """Provider SX Bet V3 — exchange P2P crypto (SX Rollup, Arbitrum Orbit).

    Protocollo (docs.sx.bet, V3 live dal 26/08/2026; la V2 non esiste piu'):
    - letture pubbliche senza chiave: /markets/active, /orderbook-v3/snapshot,
      /metadata/obv3; scritture con header `x-sx-api-key`;
    - i mercati sono BINARI (outcomeOne/outcomeTwo). Il 1X2 calcio e' il
      market type 1, decomposto in tre mercati "X vs Not X" (Home/Tie/Away):
      ogni esito singolo del 1X2 si piazza sul suo mercato binario
      (selection_id 1 = esito X, 2 = "Not X");
    - ordine firmato EIP-712 con la chiave privata dell'EOA (SX_PRIVATE_KEY),
      domain dal metadata ("OBv3 Escrow", version "1", chainId della rete);
      `percentageOdds` = worst price accettato in probabilita' * 1e20
      (ladder 0.125%), `totalBetSize` in unita' base USDC (6 decimali);
    - `timeInForce`: GTC = resta sul book, IOC/FOK = take immediato.
      Questo provider piazza di default in modalita' taker (IOC), ovvero
      "al prezzo richiesto o meglio" — coerente col flusso value del bot;
    - il capitale sta nel proxy wallet dell'account (POST /user/deploy-proxy),
      NON nell'EOA: prima del primo ordine reale va deployato e finanziato
      (wizard UI o via API con SX_API_KEY).
    """

    name = "sxbet"

    def __init__(self, api_key: str = "", private_key: str = "",
                 api_base: Optional[str] = None,
                 time_in_force: Optional[str] = None) -> None:
        super().__init__()
        self.api_key = api_key
        self.private_key = private_key
        self.api_base = (api_base or SX_API_BASE).rstrip("/")
        self.time_in_force = (time_in_force or SX_TIME_IN_FORCE).upper()
        if self.time_in_force not in ("GTC", "IOC", "FOK"):
            self.time_in_force = "IOC"
        self._meta: Optional[Dict] = None
        self._meta_ts: float = 0.0
        self._acct = None

    # -- helpers rete ----------------------------------------------------
    def _headers(self, auth: bool = True) -> Dict:
        headers = {"Content-Type": "application/json"}
        if auth:
            if not self.api_key:
                raise RuntimeError(
                    "credenziali SX Bet mancanti (SX_API_KEY)")
            headers["x-sx-api-key"] = self.api_key
        return headers

    def _get(self, path: str, params: Optional[Dict] = None,
             auth: bool = False) -> Dict:
        resp = requests.get(f"{self.api_base}/{path}", params=params,
                            headers=self._headers(auth),
                            timeout=EXECUTION_TIMEOUT)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, payload: Dict, timeout: Optional[float] = None) -> Dict:
        # waitForOutcome puo' richiedere fino a ~15s lato server.
        t0 = time.perf_counter()
        resp = requests.post(f"{self.api_base}/{path}", json=payload,
                             headers=self._headers(auth=True),
                             timeout=timeout or (EXECUTION_TIMEOUT + 20))
        # RISPOSTA CRUDA a livello WARNING: httpx/requests e i logger dei
        # moduli restano silenziosi, quindi senza questa riga un 4xx/5xx
        # diventa indistinguibile da un ordine accettato (era la classe di
        # bug in cui il ledger diceva "successo" e l'exchange non mostrava
        # nulla). Il corpo e' troncato e la firma mascherata.
        # getattr: una response finta (test) o un wrapper possono non
        # esporre .text/.status_code; l'evidenza non deve mai far fallire
        # l'ordine.
        raw = getattr(resp, "text", "") or ""
        code = getattr(resp, "status_code", "?")
        logger.warning("sxbet: POST /%s -> HTTP %s in %.0fms | raw: %s",
                       path, code,
                       (time.perf_counter() - t0) * 1000.0,
                       redact_raw_http(raw))
        resp.raise_for_status()
        try:
            return resp.json()
        except ValueError as e:
            logger.error("sxbet: risposta NON-JSON su /%s (HTTP %s): %s | "
                         "raw: %s", path, code, e, redact_raw_http(raw))
            raise

    def _metadata(self) -> Dict:
        """Metadata V3 (chainId, domain EIP-712, baseToken, ladder, limiti) — cache 15'."""
        if self._meta and (time.time() - self._meta_ts) < 900:
            return self._meta
        data = self._get("metadata/obv3")
        meta = data.get("data") if isinstance(data, dict) else None
        if not isinstance(meta, dict):
            raise RuntimeError(f"metadata SX Bet non valido: {data}")
        self._meta = meta
        self._meta_ts = time.time()
        return self._meta

    def _account(self):
        """Account EIP-155 dalla chiave privata (eth-account, import lazy)."""
        if self._acct is None:
            if not self.private_key:
                raise RuntimeError(
                    "credenziali SX Bet mancanti (SX_PRIVATE_KEY)")
            try:
                from eth_account import Account as _EthAccount
            except ImportError as e:  # pragma: no cover
                raise RuntimeError(
                    "eth-account non installato: aggiungere eth-account a "
                    "requirements.txt (pip install eth-account)") from e
            self._acct = _EthAccount.from_key(self.private_key)
        return self._acct

    def _step_scaled(self) -> int:
        meta = self._metadata()
        return sx_ladder_step_scaled(int(meta.get("oddsLadderStepSize") or 125))

    def _sign_order(self, order: Dict) -> str:
        """Firma EIP-712 dell'ordine V3 (8 campi, salt PRIMA di expiry)."""
        acct = self._account()
        from eth_account.messages import encode_typed_data
        meta = self._metadata()
        message = {
            "marketHash": order["marketHash"],
            "baseToken": order["baseToken"],
            "totalBetSize": int(order["totalBetSize"]),
            "percentageOdds": int(order["percentageOdds"]),
            "salt": int(order["salt"], 16),
            "expiry": int(order["expiry"]),
            "maker": order["maker"],
            "isMakerBettingOutcomeOne": bool(order["isMakerBettingOutcomeOne"]),
        }
        signable = encode_typed_data(meta["domain"], _SX_ORDER_TYPES, message)
        return acct.sign_message(signable).signature.to_0x_hex()

    # -- interfaccia ExecutionProvider ---------------------------------
    def get_balance(self) -> Dict:
        """Saldo del proxy wallet: available = spendibile per gli ordini."""
        data = self._get("user/balance-v3", auth=True)
        d = data.get("data") if isinstance(data, dict) else None
        balances = (d or {}).get("balances") or []
        if not balances:
            return {"availableBalance": 0.0, "exposure": 0.0, "raw": d}
        b = balances[0]
        meta = self._metadata()
        decimals = int(meta.get("activeAsset", {}).get("decimals") or 6)

        def units(v: object) -> float:
            try:
                return round(float(str(v)) / (10 ** decimals), 4)
            except (TypeError, ValueError):
                return 0.0

        return {
            "availableBalance": units(b.get("availableAmount")),
            "exposure": (units(b.get("escrowedAmount"))
                          + units(b.get("pendingEscrowAmount"))),
            "pendingAvailable": units(b.get("pendingAvailableAmount")),
            "wallet": b.get("wallet"),
            "userAddress": b.get("userAddress"),
            "tokenAddress": b.get("tokenAddress"),
            "raw": b,
        }

    def list_market_catalogue(self, event_type_ids: tuple = ("5",),
                              market_type: str = "1X2",
                              max_results: int = 20,
                              market_type_ids: Optional[tuple] = None) -> List[Dict]:
        """Discovery calcio per TYPE ID SX: i mercati binari "X vs Not X".

        `event_type_ids` = id sport SX (default ("5",) = Soccer; vedi
        GET /sports); `market_type` e' accettato per compatibilita' (il tipo
        vero si sceglie con `market_type_ids`: 1 = 1X2, 2 = Over/Under,
        3 = Asian Handicap). Pagina da 100 con `nextKey`, fermandosi a
        `max_results`.

        Multi-mercato (19/09/2026): aggiunti `market_type` e `line` in uscita.
        La linea si legge dal NOME dell'esito quando c'e' ("Over 2.5" /
        "Cagliari -0.75"), che sull'API pubblica e' la fonte piu' affidabile,
        e in fallback dal campo `line` della fonte se plausibile.
        """
        sport_ids = ",".join(str(s) for s in (event_type_ids or ("5",)))
        types = tuple(str(t) for t in (market_type_ids or ("1",)))
        out: List[Dict] = []
        for type_id in types:
            pagination_key: Optional[str] = None
            while len(out) < max_results:
                params: Dict = {"sportIds": sport_ids, "type": type_id,
                                "pageSize": 100}
                if pagination_key:
                    params["paginationKey"] = pagination_key
                try:
                    data = self._get("markets/active", params=params)
                except Exception as e:
                    logger.warning("sxbet: discovery mercati (type %s) "
                                   "fallita: %s", type_id, e)
                    break
                d = data.get("data") if isinstance(data, dict) else {}
                markets = (d or {}).get("markets") or []
                for m in markets:
                    if len(out) >= max_results:
                        break
                    game_time = m.get("gameTime")
                    open_date = (datetime.fromtimestamp(
                        int(game_time), tz=timezone.utc).isoformat()
                        if game_time else None)
                    out.append({
                        "market_id": m.get("marketHash"),
                        "market_name": (f"{_TYPE_LABELS.get(type_id, 'type' + type_id)}"
                                         f" - {m.get('outcomeOneName')}"),
                        "event_name": f"{m.get('teamOneName')} vs "
                                      f"{m.get('teamTwoName')}",
                        "event_id": m.get("sportXeventId"),
                        "country_code": None,
                        "open_date": open_date,
                        "total_matched": None,
                        # TYPE ID e LINEA: senza questi due campi un mercato a
                        # linea non e' distinguibile da un altro (OU 2.5 vs OU
                        # 3.5) e la risoluzione d'ordine non puo' essere
                        # fail-closed.
                        "market_type_id": type_id,
                        "line": _catalogue_line(m),
                        # Etichetta di lega/torneo della fonte (`leagueLabel`).
                        # Serve alla corsia eSports per sapere QUALE gioco si
                        # sta comprando: senza, un'etichetta SX non mappata non
                        # sarebbe distinguibile da un titolo riconosciuto.
                        "league_label": m.get("leagueLabel"),
                        "main_line": bool(m.get("mainLine")),
                        # Nomi squadre/esiti del market (per la risoluzione
                        # match -> mercato in auto_bet: su SX il 1X2 e' spezzato
                        # in 3 mercati binari "X vs Not X" — uno per esito).
                        "team_one_name": m.get("teamOneName"),
                        "team_two_name": m.get("teamTwoName"),
                        "outcome_one_name": m.get("outcomeOneName"),
                        "outcome_two_name": m.get("outcomeTwoName"),
                        "runners": [
                            {"selection_id": 1,
                             "name": m.get("outcomeOneName")},
                            {"selection_id": 2,
                             "name": m.get("outcomeTwoName")},
                        ],
                    })
                pagination_key = (d or {}).get("nextKey")
                if not pagination_key or not markets:
                    break
        return out[:max_results]

    def get_market_book(self, market_id: str) -> Dict:
        """Snapshot del book (prospettiva taker): best price per esito."""
        data = self._get("orderbook-v3/snapshot", params={
            "marketHash": market_id, "showTakerPerspective": "true"})
        d = data.get("data") if isinstance(data, dict) else {}
        return {
            "marketId": market_id,
            "status": "OPEN",
            "version": (d or {}).get("version"),
            "runners": [
                {"selectionId": 1,
                 "availableToBack": _sx_levels_to_decimal(
                     (d or {}).get("outcomeOne"))},
                {"selectionId": 2,
                 "availableToBack": _sx_levels_to_decimal(
                     (d or {}).get("outcomeTwo"))},
            ],
        }

    def best_back_price(self, market_id: str,
                        selection_id: int) -> Optional[float]:
        """Miglior prezzo taker per l'esito (selection 1|2) in quota decimale.

        Book con showTakerPerspective=true: il livello [0] della side e' il
        migliore per chi vuole scommettere quell'esito (prob. piu' bassa =
        quota piu' alta).
        """
        try:
            data = self._get("orderbook-v3/snapshot", params={
                "marketHash": market_id, "showTakerPerspective": "true"})
        except Exception as e:
            logger.warning("sxbet: book %s fallito: %s", market_id, e)
            return None
        d = data.get("data") if isinstance(data, dict) else {}
        levels = ((d or {}).get("outcomeOne") if int(selection_id) == 1
                  else (d or {}).get("outcomeTwo"))
        if not isinstance(levels, list) or not levels:
            return None
        best = levels[0]
        if not isinstance(best, dict):
            return None
        return pct_scaled_to_decimal(best.get("percentageOdds")) or None

    def place_limit_order(self, market_id: str, selection_id: int,
                          side: str, price: float, size: float,
                          persistence: str = "LAPSE",
                          expiry_seconds: Optional[int] = None) -> OrderResult:
        """Ordine firmato EIP-712 su /orders-v3 (waitForOutcome).

        Mappatura: side BACK su selection 1|2 -> si scommette quell'esito;
        side LAY su selection X -> si scommette l'esito complementare al
        prezzo equivalente (prob. complementare). persistence "PERSIST" ->
        GTC (resta sul book), altrimenti il timeInForce configurato
        (default IOC = take al prezzo richiesto o meglio).

        `expiry_seconds` (10/10/2026): scadenza PER-ORDINE. Serve agli ordini
        RESTING (`resting_orders.py`): allineando la scadenza on-chain alla
        NOSTRA deadline, l'assenza dell'ordine dalla lista degli aperti diventa
        un'informazione non ambigua (prima della scadenza = riempito, dopo =
        spirato). Con la sola costante globale (default 3600s) un ordine
        resting morirebbe da solo in un'ora e "sparito dal book" non
distinguerebbe un riempimento da una scadenza naturale.
        """
        side = side.upper()
        sel = 1 if int(selection_id) == 1 else 2
        if not self.api_key:
            return OrderResult(False, None, "FAILURE", price, None, 0.0, 0.0,
                               error="credenziali SX Bet mancanti (SX_API_KEY)")
        if not self.private_key:
            return OrderResult(False, None, "FAILURE", price, None, 0.0, 0.0,
                               error="credenziali SX Bet mancanti (SX_PRIVATE_KEY)")
        if side == "LAY":
            # lay X == back not-X: prezzo complementare equivalente
            p_sel = SX_PROB_SCALE / float(price)
            p_other = SX_PROB_SCALE - p_sel
            if p_other <= 0:
                return OrderResult(False, None, "FAILURE", price, None, 0.0,
                                   0.0, error="LAY: prob. complementare non valida")
            price = SX_PROB_SCALE / p_other  # quota dell'esito opposto
            sel = 2 if sel == 1 else 1
        elif side != "BACK":
            return OrderResult(False, None, "FAILURE", price, None, 0.0, 0.0,
                               error=f"side non valido: {side}")
        if float(size) < 1.0:
            return OrderResult(False, None, "FAILURE", price, None, 0.0, 0.0,
                               error=f"stake minimo SX Bet: 1 USDC "
                                     f"(ricevuto {size})")

        try:
            meta = self._metadata()
            step = sx_ladder_step_scaled(
                int(meta.get("oddsLadderStepSize") or 125))
            base_token = meta["activeAsset"]["baseToken"]
            decimals = int(meta.get("activeAsset", {}).get("decimals") or 6)
        except Exception as e:
            return OrderResult(False, None, "FAILURE", price, None, 0.0, 0.0,
                               error=str(e))

        units = stake_to_sx_units(size, decimals)
        if units < 10 ** decimals:
            return OrderResult(False, None, "FAILURE", price, None, 0.0, 0.0,
                               error=f"stake minimo SX Bet: 1 USDC "
                                     f"(ricevuto {size})")
        try:
            p_bound = decimal_to_pct_scaled(price, step)
        except ValueError as e:
            return OrderResult(False, None, "FAILURE", price, None, 0.0, 0.0,
                               error=str(e))

        tif = "GTC" if persistence.upper() == "PERSIST" else self.time_in_force
        try:
            maker = self._account().address
        except RuntimeError as e:
            return OrderResult(False, None, "FAILURE", price, None, 0.0, 0.0,
                               error=str(e))
        order = {
            "marketHash": market_id,
            "maker": maker,
            "totalBetSize": str(units),
            "percentageOdds": str(p_bound),
            "salt": "0x" + secrets.token_hex(32),
            "expiry": int(time.time()) + (int(expiry_seconds)
                                         if expiry_seconds else
                                         SX_EXPIRY_SECONDS),
            "baseToken": base_token,
            "isMakerBettingOutcomeOne": sel == 1,
            "timeInForce": tif,
        }
        try:
            order["orderSignature"] = self._sign_order(order)
        except Exception as e:
            return OrderResult(False, None, "FAILURE", price, None, 0.0, 0.0,
                               error=str(e))

        payload = {"orders": [order], "waitForOutcome": True}
        t0 = time.perf_counter()
        try:
            data = self._post("orders-v3", payload)
        except Exception as e:
            latency_ms = (time.perf_counter() - t0) * 1000.0
            return OrderResult(False, None, "FAILURE", price, None, 0.0,
                               latency_ms, error=str(e))
        latency_ms = (time.perf_counter() - t0) * 1000.0

        # Payload COMPLETO della risposta ordine, sempre loggato: e' la
        # sorgente di verita' per ricostruire un verdetto contestato.
        logger.warning("sxbet: risposta ordine /orders-v3 | raw: %s",
                       redact_raw_http(json.dumps(data, default=str)))

        d = data.get("data") if isinstance(data, dict) else {}
        entries = (d or {}).get("orders") or []
        o = entries[0] if isinstance(entries, list) and entries else None
        if not isinstance(o, dict):
            # Nessuna entry in data.orders: NON e' un successo. Si porta il
            # corpo grezzo nell'errore, cosi' il motivo e' visibile anche a
            # valle (senza dover riaprire i log).
            return OrderResult(
                False, None, "FAILURE", price, None, 0.0, latency_ms,
                error="risposta ordine vuota (nessuna entry in data.orders): "
                      + redact_raw_http(data, 300))
        status = str(o.get("status") or "SUBMITTED").upper()
        bet_id = str(o.get("orderId") or "") or None
        if status == "FAILED":
            return OrderResult(False, bet_id, status, price, None,
                               0.0, latency_ms,
                               error=str(o.get("message") or "ordine rifiutato"))
        outcome = o.get("outcome") or {}
        state = str(outcome.get("state") or "").upper()
        filled_state = state in ("FULLY_FILLED", "PARTIAL_FILL_DONE")
        # CONFERMA OBBLIGATORIA DEL BET ID: un ordine senza orderId emesso
        # dall'exchange non e' dimostrabile sull'interfaccia reale. Lo stato
        # "riempito" da solo non basta: si marca NON piazzato (fail-closed),
        # perche' e' esattamente cio' che permetteva di scrivere un
        # "successo" sul ledger senza un ordine visibile su SX Bet.
        ok = filled_state and bet_id is not None
        if filled_state and bet_id is None:
            logger.error("sxbet: ordine %s (%s) ma NESSUN orderId nella "
                         "risposta: trattato come NON piazzato", state, status)
        blended = outcome.get("blendedOdds")
        return OrderResult(
            ok=ok,
            bet_id=bet_id,
            status=state or status,
            price_requested=price,
            price_matched=pct_scaled_to_decimal(blended)
            if blended is not None else None,
            size_matched=sx_units_to_stake(outcome.get("fillAmount"), decimals),
            latency_ms=latency_ms,
            error=None if ok else (
                "ordine riempito ma senza orderId: non confermabile"
                if filled_state else
                (None if not state else "ordine non riempito")),
        )

    def list_open_orders(self) -> Optional[List[Dict]]:
        """Ordini ANCORA APERTI dell'account (GET /orders-v3) — o `None`.

        E' l'unico modo per sapere se un ordine RESTING e' ancora sul book:
        SX non espone uno storico ordini utilizzabile. Il contratto e' a TRE
        valori, e la differenza conta:

        - `None`  = NON leggibile (errore, payload inatteso, chiave assente):
          il chiamante DEVE trattarlo come fail-closed — nessuna inferenza su
          cosa sia successo a un ordine;
        - `[]`    = leggibile e NESSUN ordine aperto (informazione vera);
        - `[...]` = gli ordini aperti.

        Un payload senza `data.orders` NON diventa una lista vuota: sarebbe
        esattamente il falso negativo che trasforma "parametro sbagliato" in
        "ordine riempito".
        """
        if not self.api_key:
            return None
        try:
            data = self._get("orders-v3", auth=True)
        except Exception as e:
            logger.warning("sxbet: lettura ordini aperti fallita: %s", e)
            return None
        d = data.get("data") if isinstance(data, dict) else None
        if not isinstance(d, dict):
            return None
        orders = d.get("orders")
        if not isinstance(orders, list):
            return None
        return [o for o in orders if isinstance(o, dict)]

    def cancel_order(self, market_id: str, bet_id: str) -> bool:
        """Cancella ordini per id (DELETE /orders-v3, solo x-sx-api-key)."""
        if not bet_id:
            return False
        try:
            resp = requests.delete(f"{self.api_base}/orders-v3",
                                   headers=self._headers(auth=True),
                                   json={"orders": [{"orderId": bet_id}]},
                                   timeout=EXECUTION_TIMEOUT)
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.warning("sxbet: cancel ordine %s fallita: %s", bet_id, e)
            return False


class DryRunProvider(ExecutionProvider):
    """Provider simulato: nessuna rete, latenza e slippage sintetici.

    Usato di default senza credenziali o con EXECUTION_DRY_RUN=1: permette
    di collaudare l'intero flusso del probe (misura, log, cancellazione)
    prima delle prime chiamate reali all'aggregatore.
    """

    name = "dry_run"

    def __init__(self, latency_ms: float = 25.0,
                 slippage: float = -0.01) -> None:
        super().__init__()
        self._latency_ms = latency_ms
        self._slippage = slippage

    def get_balance(self) -> Dict:
        return {"availableBalance": 1000.0, "exposure": 0.0}

    def get_market_book(self, market_id: str) -> Dict:
        return {"marketId": market_id, "status": "OPEN", "runners": []}

    def best_back_price(self, market_id: str, selection_id: int) -> Optional[float]:
        return None  # il prezzo lo passa il chiamante nel probe

    def list_market_catalogue(self, event_type_ids: tuple = ("1",),
                              market_type: str = "MATCH_ODDS",
                              max_results: int = 20) -> List[Dict]:
        """Catalogue simulato: due match di calcio finti (per --markets)."""
        return [
            {"market_id": "1.1001", "market_name": "Match Odds",
             "event_name": "Dry FC vs Run FC", "event_id": "3001",
             "country_code": "IT", "open_date": "2026-09-07T19:00:00Z",
             "total_matched": 12000.0,
             "runners": [{"selection_id": 5001, "name": "Dry FC"},
                          {"selection_id": 5002, "name": "Run FC"},
                          {"selection_id": 5003, "name": "Draw"}]},
            {"market_id": "1.1002", "market_name": "Match Odds",
             "event_name": "Beta FC vs Alpha FC", "event_id": "3002",
             "country_code": "EN", "open_date": "2026-09-07T19:45:00Z",
             "total_matched": 8900.0,
             "runners": [{"selection_id": 5101, "name": "Beta FC"},
                          {"selection_id": 5102, "name": "Alpha FC"},
                          {"selection_id": 5103, "name": "Draw"}]},
        ][:max_results]

    def place_limit_order(self, market_id: str, selection_id: int,
                          side: str, price: float, size: float,
                          persistence: str = "LAPSE") -> OrderResult:
        matched = round(price + self._slippage, 2)
        return OrderResult(True, f"dry-{int(time.time()*1000)}", "dry-run",
                           price, matched, size, self._latency_ms)

    def cancel_order(self, market_id: str, bet_id: str) -> bool:
        return True


# ---------------------------------------------------------------------------
# Factory provider
# ---------------------------------------------------------------------------

def _creds_configured() -> bool:
    """Credenziali del provider selezionato da EXECUTION_PROVIDER."""
    if EXECUTION_PROVIDER == "sxbet":
        return bool(SX_API_KEY and SX_PRIVATE_KEY)
    if EXECUTION_PROVIDER == "smarkets":
        return bool(SMARKETS_USERNAME and SMARKETS_PASSWORD)
    if EXECUTION_PROVIDER in ("betinasia", "mollybet"):
        return bool(EXECUTION_APP_KEY and EXECUTION_USERNAME
                    and EXECUTION_PASSWORD)
    return False


def build_provider() -> ExecutionProvider:
    """Seleziona il provider da env: EXECUTION_PROVIDER + credenziali.

    Regole:
    - EXECUTION_DRY_RUN=1                      -> DryRunProvider;
    - EXECUTION_PROVIDER=sxbet                 -> SxBetProvider
      (credenziali SX_API_KEY/SX_PRIVATE_KEY);
    - EXECUTION_PROVIDER=smarkets              -> SmarketsProvider
      (credenziali SMARKETS_USERNAME/PASSWORD);
    - EXECUTION_PROVIDER=betinasia|mollybet    -> provider aggregatore
      (credenziali EXECUTION_APP_KEY/USERNAME/PASSWORD);
    - credenziali mancanti o provider ignoto   -> DryRunProvider (default
      sicuro: nessuna chiamata di rete).
    """
    if EXECUTION_DRY_RUN:
        return DryRunProvider()
    if EXECUTION_PROVIDER == "sxbet":
        if SX_API_KEY and SX_PRIVATE_KEY:
            return SxBetProvider(SX_API_KEY, SX_PRIVATE_KEY)
        logger.warning(
            "execution: credenziali SX Bet mancanti "
            "(SX_API_KEY/SX_PRIVATE_KEY) -> DryRunProvider")
        return DryRunProvider()
    if EXECUTION_PROVIDER == "smarkets":
        if SMARKETS_USERNAME and SMARKETS_PASSWORD:
            return SmarketsProvider(SMARKETS_USERNAME, SMARKETS_PASSWORD)
        logger.warning(
            "execution: credenziali Smarkets mancanti "
            "(SMARKETS_USERNAME/PASSWORD) -> DryRunProvider")
        return DryRunProvider()
    if EXECUTION_PROVIDER in ("betinasia", "mollybet"):
        if EXECUTION_APP_KEY and EXECUTION_USERNAME and EXECUTION_PASSWORD:
            if EXECUTION_PROVIDER == "betinasia":
                return BetInAsiaBlackProvider(EXECUTION_APP_KEY,
                                              EXECUTION_USERNAME,
                                              EXECUTION_PASSWORD)
            return MollyBetProvider(EXECUTION_APP_KEY, EXECUTION_USERNAME,
                                    EXECUTION_PASSWORD)
        logger.warning(
            "execution: credenziali aggregatore mancanti "
            "(EXECUTION_APP_KEY/USERNAME/PASSWORD) -> DryRunProvider")
        return DryRunProvider()
    if EXECUTION_PROVIDER:
        logger.warning("execution: EXECUTION_PROVIDER='%s' non riconosciuto "
                       "(sxbet|smarkets|betinasia|mollybet) -> DryRunProvider",
                       EXECUTION_PROVIDER)
    return DryRunProvider()


# ---------------------------------------------------------------------------
# Risoluzione match -> mercato (per auto_bet live)
# ---------------------------------------------------------------------------

_TIE_LABELS = {"tie", "draw", "the draw", "pareggio", "x"}


#: Etichette leggibili dei type id SX (1 = 1X2, 2 = Over/Under,
#: 3 = Asian Handicap): il label deve restare umano per la diagnosi.
_TYPE_LABELS = {"1": "1X2", "2": "Over/Under", "3": "Asian Handicap",
                "52": "Moneyline (2 vie)"}

#: Linea di un mercato del catalogo: dal NOME dell'esito quando c'e'
#: ('Over 2.5' / 'Cagliari -0.75' — la fonte piu' affidabile osservata), in
#: fallback dal campo della fonte se plausibile (|v| <= 12: SX potrebbe
#: esprimerla in unita' scalate). Zero e' una linea VALIDA (handicap pari).
_CATALOGUE_LINE_MAX = 12.0


def _catalogue_line(m: Mapping) -> Optional[float]:
    """Linea di un mercato del catalogo SX (None se il mercato non ne ha una)."""
    import re as _re
    pattern = _re.compile(r"[-+]?\d+(?:\.\d+)?")
    for key in ("outcomeOneName", "outcomeTwoName"):
        match = pattern.search(str(m.get(key) or ""))
        if match:
            try:
                return float(match.group(0))
            except ValueError:
                continue
    for key in ("line", "lineValue", "line_value", "handicap"):
        raw = m.get(key)
        if raw is None:
            continue
        match = pattern.search(str(raw))
        if not match:
            continue
        try:
            value = float(match.group(0))
        except ValueError:
            continue
        if abs(value) <= _CATALOGUE_LINE_MAX:
            return value
    return None


def _catalogue_line_clean(name: object) -> str:
    """Nome squadra senza la linea attaccata ('Cagliari -0.75' -> 'Cagliari')."""
    import re as _re
    return _re.sub(r"[-+]?\d+(?:\.\d+)?", " ", str(name or "")).strip(" -+")


def _name_key(s: object) -> str:
    """Normalizza un nome per il confronto: minuscolo, accent-fold, solo
    alfanumerici (es. 'CA Osasuna' -> 'ca osasuna', 'Nueva Chicago' ->
    'nueva chicago'). Ritorna '' per input vuoti."""
    if s is None:
        return ""
    import re
    import unicodedata
    s = unicodedata.normalize("NFKD", str(s))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[^a-z0-9]+", " ", s.lower())
    return " ".join(s.split())


def _name_sim(a: object, b: object) -> float:
    """Somiglianza tra nomi squadra normalizzati (1.0 = uguali).

    Il match tra il nome del segnale (the-odds-api) e quello dell'exchange
    raramente e' identico ('Inter' vs 'Inter Milan', 'Betis' vs 'Real
    Betis'): si accetta la sovrapposizione (0.92) o la somiglianza di
    sequenza (SequenceMatcher) sopra soglia. Mai abbastanza alta da far
    scattare falsi positivi tra squadre diverse.
    """
    ka, kb = _name_key(a), _name_key(b)
    if not ka or not kb:
        return 0.0
    if ka == kb:
        return 1.0
    if ka in kb or kb in ka:
        return 0.92
    from difflib import SequenceMatcher
    return SequenceMatcher(None, ka, kb).ratio()


def _parse_kickoff(kickoff_iso: Optional[str]) -> Optional[datetime]:
    """Kickoff naive-UTC da ISO del ledger (None se non leggibile)."""
    if not kickoff_iso:
        return None
    try:
        return datetime.fromisoformat(
            str(kickoff_iso).replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def _unique_event_markets(markets: Sequence[Dict], home: str, away: str,
                          kick: Optional[datetime], *,
                          window_hours: float = 6.0) -> Optional[List[Dict]]:
    """I mercati di UN solo evento (squadre allineate + kickoff nella finestra).

    Estratto da `resolve_match_market` (19/09/2026) perche' il resolver a
    linea (OU/AH) deve applicare ESATTAMENTE la stessa logica: due copie
    divergerebbero, ed e' il modo classico in cui un percorso nuovo si
    allontana da quello in produzione.

    Fail-closed: None se l'evento non e' univoco (0 o piu' gruppi), cosi' il
    chiamante non ordina mai su un mercato ambiguo.
    """
    hk, ak = _name_key(home), _name_key(away)
    events: Dict[tuple, list] = {}
    for m in markets:
        t1, t2 = _name_key(m.get("team_one_name")), _name_key(m.get("team_two_name"))
        if not (t1 and t2):
            continue
        if _name_sim(t1, hk) < 0.82 or _name_sim(t2, ak) < 0.82:
            continue
        # Finestra kickoff: il mercato dell'exchange deve riferirsi alla
        # stessa partita del segnale (stesso orario, tolleranza finestra).
        od_bin = None
        if kick is not None:
            od = m.get("open_date")
            if od:
                try:
                    dt = datetime.fromisoformat(
                        str(od).replace("Z", "+00:00")).replace(tzinfo=None)
                    if abs((dt - kick).total_seconds()) > window_hours * 3600:
                        continue
                    # Bin temporale (minuti): i mercati dello STESSO evento
                    # hanno lo stesso gameTime e finiscono nello stesso
                    # gruppo; due eventi con gli stessi nomi ma kickoff
                    # diversi restano SEPARATI (=> ambiguo).
                    od_bin = dt.replace(second=0, microsecond=0)
                except Exception:
                    pass
        events.setdefault((t1, t2, od_bin), []).append(m)
    if len(events) != 1:
        if events:
            logger.warning("resolve: %d eventi candidati per %s vs %s, "
                           "salto (ambiguo)", len(events), home, away)
        return None
    return next(iter(events.values()))


def resolve_match_market(provider, home: str, away: str, esito_key: str,
                         kickoff_iso: Optional[str] = None,
                         window_hours: float = 6.0,
                         max_results: int = 400) -> Optional[Dict]:
    """Trova il mercato del provider per la partita (home/away) e l'esito.

    Il segnale value di auto_bet nasce dalle quote the-odds-api (match_id
    proprio); per piazzare un ordine reale serve il market_id dell'exchange
    della STESSA partita e la selezione dell'esito. Questo resolver:

    1. scarica il catalogo calcio del provider (mercati ACTIVE);
    2. trova l'evento con entrambe le squadre allineate (home/away in
       ordine, somiglianza >= 0.82) e kickoff nella finestra del segnale;
    3. dentro l'evento sceglie il mercato/selezione dell'esito richiesto:
       su SX Bet il 1X2 e' spezzato in 3 mercati binari "X vs Not X"
       (uno per esito: esito 1 -> outcomeOne = casa, 2 -> trasferta,
       X -> outcomeOne = 'Tie').

    Fail-closed: ritorna None se l'evento non e' univoco (0 o piu' match)
    o l'esito non e' mappabile — il chiamante NON deve scommettere su un
    mercato ambiguo. Provider supportati oggi: sxbet.

    Ritorna un dict con market_id, selection_id, event_name, label, oppure
    None.
    """
    pname = str(getattr(provider, "name", "")).lower()
    if pname != "sxbet":
        logger.warning("resolve_match_market: provider '%s' non ancora "
                       "supportato (solo sxbet)", pname or "?")
        return None

    kick = None
    if kickoff_iso:
        try:
            kick = datetime.fromisoformat(
                str(kickoff_iso).replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            kick = None

    try:
        markets = provider.list_market_catalogue(
            event_type_ids=("5",), max_results=max_results)
    except Exception as e:
        logger.warning("resolve_match_market: discovery fallita: %s", e)
        return None
    if not markets:
        return None

    event_markets = _unique_event_markets(markets, home, away, kick,
                                         window_hours=window_hours)
    if event_markets is None:
        return None
    # Chiavi dei nomi per la scelta dell'esito (il grouping le usa dentro
    # `_unique_event_markets`, qui servono di nuovo: bug trovato dai test del
    # 19/09 — senza questa riga ogni ordine 1X2 falliva con NameError).
    hk, ak = _name_key(home), _name_key(away)
    es = str(esito_key or "").strip().lower()
    for m in event_markets:
        o1 = _name_key(m.get("outcome_one_name"))
        if es in ("x", "draw", "pareggio"):
            if o1 in _TIE_LABELS:
                return {"market_id": m["market_id"], "selection_id": 1,
                        "event_name": m.get("event_name"), "label": "X",
                        "provider": pname}
        else:
            target = hk if es == "1" else (ak if es == "2" else None)
            if target and _name_sim(o1, target) >= 0.82:
                return {"market_id": m["market_id"], "selection_id": 1,
                        "event_name": m.get("event_name"), "label": es,
                        "provider": pname}
    return None


def resolve_market_for(provider, home: str, away: str, market_type: str,
                       line: float, side: str,
                       kickoff_iso: Optional[str] = None,
                       window_hours: float = 6.0,
                       max_results: int = 400) -> Optional[Dict]:
    """Mercato a LINEA (OU/AH) dell'evento, per il piazzamento reale.

    `market_type` = "OU" | "AH"; `line` e' dal punto di vista di teamOne (per
    l'OU il totale, per l'AH l'handicap di teamOne); `side` = "over"/"under"
    per l'OU oppure "home"/"away" per l'AH.

    Fail-closed (nessun ordine se qualcosa non torna): provider non sxbet,
    type id non mappato, evento non univoco, LINEA diversa da quella richiesta
    (un OU 3.5 non e' un OU 2.5), lato non riconoscibile dal nome dell'esito.
    """
    pname = str(getattr(provider, "name", "")).lower()
    if pname != "sxbet":
        logger.warning("resolve_market_for: provider '%s' non supportato "
                       "(solo sxbet)", pname or "?")
        return None
    wanted_type = str(market_type or "").upper()
    type_id = {"OU": "2", "AH": "3"}.get(wanted_type)
    if type_id is None or line is None:
        return None
    try:
        target_line = float(line)
    except (TypeError, ValueError):
        return None
    kick = _parse_kickoff(kickoff_iso)
    try:
        markets = provider.list_market_catalogue(
            event_type_ids=("5",), market_type_ids=(type_id,),
            max_results=max_results)
    except Exception as e:
        logger.warning("resolve_market_for: discovery type %s fallita: %s",
                       type_id, e)
        return None
    if not markets:
        return None
    event_markets = _unique_event_markets(markets, home, away, kick,
                                          window_hours=window_hours)
    if event_markets is None:
        return None
    side_key = str(side or "").lower()
    for m in event_markets:
        if str(m.get("market_type_id") or "") != type_id:
            continue
        mline = m.get("line")
        if mline is None:
            continue
        try:
            if abs(float(mline) - target_line) > 1e-6:
                continue
        except (TypeError, ValueError):
            continue
        outcome_one = str(m.get("outcome_one_name") or "")
        if wanted_type == "OU":
            low = outcome_one.strip().lower()
            if low.startswith("over"):
                selection = 1 if side_key == "over" else 2
            elif low.startswith("under"):
                selection = 2 if side_key == "over" else 1
            else:
                continue
        else:
            clean = _catalogue_line_clean(outcome_one)
            if _name_sim(_name_key(clean), _name_key(home)) >= 0.82:
                selection = 1 if side_key == "home" else 2
            elif _name_sim(_name_key(clean), _name_key(away)) >= 0.82:
                selection = 2 if side_key == "home" else 1
            else:
                continue
        return {"market_id": m["market_id"], "selection_id": selection,
                "event_name": m.get("event_name"),
                "label": f"{wanted_type} {float(mline):g}",
                "market_type": wanted_type, "line": float(mline),
                "provider": pname}
    return None


def resolve_moneyline_market(provider, home: str, away: str, team: str,
                             kickoff_iso: Optional[str] = None,
                             window_hours: float = 6.0,
                             max_results: int = 400,
                             sport_id: str = "9",
                             type_id: str = "52") -> Optional[Dict]:
    """Mercato 2 VIE (moneyline) dell'evento: BACK sull'esito `team`.

    Corsia eSports (30/09/2026). Su SX un mercato binario e' "X vs Not X" e
    `outcomeOne` E' il lato che si compra con selection 1 — la stessa
    semantica del 1X2 spezzato in 3 mercati — ma qui i lati sono DUE (niente
    pareggio): un mercato per squadra. Si sceglie il mercato il cui
    `outcomeOne` aggancia la squadra da comprare.

    Fail-closed (nessun ordine se qualcosa non torna): provider non sxbet,
    `team` che non e' nessuno dei due partecipanti, evento non univoco,
    `outcomeOne` che non aggancia la squadra richiesta. **Mai indovinare il
    lato**: un incrocio sbagliato comprerebbe l'esito dell'avversario, e il
    P/L non lo direbbe finche' il conteggio non torna.
    """
    pname = str(getattr(provider, "name", "")).lower()
    if pname != "sxbet":
        logger.warning("resolve_moneyline_market: provider '%s' non "
                       "supportato (solo sxbet)", pname or "?")
        return None
    hk, ak, tk = _name_key(home), _name_key(away), _name_key(team)
    if not tk or (_name_sim(tk, hk) < 0.82 and _name_sim(tk, ak) < 0.82):
        # La squadra da comprare deve essere una delle due dell'evento: se non
        # lo e' la richiesta stessa e' incoerente (fail-closed).
        logger.warning("resolve_moneyline_market: '%s' non e' un partecipante "
                       "di %s vs %s", team, home, away)
        return None
    kick = _parse_kickoff(kickoff_iso)
    try:
        markets = provider.list_market_catalogue(
            event_type_ids=(str(sport_id),),
            market_type_ids=(str(type_id),),
            max_results=max_results)
    except Exception as e:
        logger.warning("resolve_moneyline_market: discovery sport %s type %s "
                       "fallita: %s", sport_id, type_id, e)
        return None
    if not markets:
        return None
    event_markets = _unique_event_markets(markets, home, away, kick,
                                          window_hours=window_hours)
    if event_markets is None:
        return None
    for m in event_markets:
        if str(m.get("market_type_id") or "") != str(type_id):
            continue
        if m.get("line") is not None:
            continue                    # il moneyline non ha linea
        if _name_sim(_name_key(m.get("outcome_one_name")), tk) >= 0.82:
            return {"market_id": m["market_id"], "selection_id": 1,
                    "event_name": m.get("event_name"),
                    "label": str(m.get("outcome_one_name") or team),
                    "market_type": "ML", "line": None,
                    "sport_id": str(sport_id), "market_type_id": str(type_id),
                    "provider": pname}
    return None


# ---------------------------------------------------------------------------
# ExecutionEngine
# ---------------------------------------------------------------------------

class ExecutionEngine:
    """Facade per l'esecuzione: probe a stake minimo con misura latenza/slippage."""

    def __init__(self, provider: Optional[ExecutionProvider] = None,
                 min_stake: Optional[float] = None) -> None:
        self.provider = provider or build_provider()
        self.min_stake = min_stake if min_stake is not None else EXECUTION_MIN_STAKE_EUR
        self.min_stake = max(0.0, self.min_stake)
        self.min_stake = min(self.min_stake, EXECUTION_MAX_STAKE_EUR)

    def status(self) -> Dict:
        """Stato del provider + credenziali configurate (senza mai stamparle)."""
        return {
            "provider": self.provider.name,
            "dry_run": isinstance(self.provider, DryRunProvider),
            "creds": _creds_configured(),
            "creds_env": ("SX_API_KEY/SX_PRIVATE_KEY"
                           if EXECUTION_PROVIDER == "sxbet" else
                           "SMARKETS_USERNAME/PASSWORD"
                           if EXECUTION_PROVIDER == "smarkets" else
                           "EXECUTION_APP_KEY/USERNAME/PASSWORD"
                           if EXECUTION_PROVIDER in ("betinasia", "mollybet")
                           else ""),
            "min_stake_eur": self.min_stake,
            "max_stake_eur": EXECUTION_MAX_STAKE_EUR,
            "measurements_log": str(MEASUREMENTS_LOG),
        }

    def discover_markets(self, max_results: int = 20) -> List[Dict]:
        """Elenca i mercati calcio (match odds) disponibili per il probe.

        Fail-closed: in caso di errore ritorna la lista vuota e logga il
        problema (il chiamante decide se considerarlo bloccante).
        """
        try:
            return self.provider.list_market_catalogue(max_results=max_results)
        except Exception as e:
            logger.warning("execution: discovery mercati fallita: %s", e)
            return []

    # -- probe latenza/slippage ------------------------------------------
    def probe(self, market_id: str, selection_id: int,
              price: Optional[float] = None,
              side: str = "BACK",
              stake: Optional[float] = None,
              cancel_if_unfilled: bool = True) -> ProbeResult:
        """Piazza un ordine LIMIT a stake minimo e misura latenza + slippage.

        Flusso:
        1. best available price (se price non fornito);
        2. place order (stake minimo, prezzo = richiesto);
        3. slippage = matched - requested (e vs best available);
        4. cancella l'eventuale residuo non matched (default);
        5. log della misura in measurements.jsonl.

        Ritorna SEMPRE un ProbeResult (fail-closed: mai eccezioni verso il
        chiamante, l'errore è nel campo `error`).
        """
        ts = datetime.now(timezone.utc).isoformat()
        stake = stake if stake is not None else self.min_stake
        stake = max(0.0, min(stake, EXECUTION_MAX_STAKE_EUR))

        best = None
        try:
            best = self.provider.best_back_price(market_id, selection_id)
        except Exception as e:
            logger.debug("probe: best_back_price fallita (%s), uso price", e)
        price_req = float(price) if price else (best if best else 2.0)

        t0 = time.perf_counter()
        try:
            order = self.provider.place_limit_order(
                market_id, selection_id, side, price_req, stake)
        except Exception as e:
            latency_ms = (time.perf_counter() - t0) * 1000.0
            res = ProbeResult(
                provider=self.provider.name, timestamp=ts,
                market_id=market_id, selection_id=selection_id, side=side,
                stake=stake, price_best_available=best,
                price_requested=price_req, price_matched=None,
                slippage=None, slippage_vs_best=None, latency_ms=latency_ms,
                order_status="FAILURE", ok=False, error=str(e), bet_id=None)
            self._log_measurement(res)
            return res
        wall_ms = (time.perf_counter() - t0) * 1000.0
        # Latenza: quella misurata dal provider (include la chiamata di rete
        # nel provider reale; nel DryRun è il valore simulato). Se il
        # provider non l'ha misurata, si usa il wall-time del probe.
        latency_ms = order.latency_ms if order.latency_ms > 0 else wall_ms

        matched = order.price_matched
        slippage = (matched - price_req) if matched is not None else None
        slip_vs_best = (matched - best) if (matched is not None
                                            and best is not None) else None

        if cancel_if_unfilled and order.bet_id and order.size_matched < stake:
            try:
                self.provider.cancel_order(market_id, order.bet_id)
            except Exception as e:
                logger.warning("probe: cancel fallita (%s)", e)

        res = ProbeResult(
            provider=self.provider.name, timestamp=ts,
            market_id=market_id, selection_id=selection_id, side=side,
            stake=stake, price_best_available=best,
            price_requested=price_req, price_matched=matched,
            slippage=slippage, slippage_vs_best=slip_vs_best,
            latency_ms=latency_ms, order_status=order.status,
            ok=order.ok, error=order.error, bet_id=order.bet_id)
        self._log_measurement(res)
        return res

    def _log_measurement(self, res: ProbeResult) -> None:
        """Appende la misura al log JSONL (data/execution/measurements.jsonl)."""
        try:
            EXECUTION_DATA_DIR.mkdir(parents=True, exist_ok=True)
            with open(MEASUREMENTS_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(res)) + "\n")
        except Exception as e:
            logger.warning("execution: log misura fallito: %s", e)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        description="ExecutionEngine — esecuzione via exchange/aggregatore "
                    "(sxbet | smarkets | betinasia | mollybet | dry-run)")
    ap.add_argument("--provider", type=str, default="",
                    choices=("sxbet", "smarkets", "betinasia", "mollybet",
                             "dry_run"),
                    help="forza il provider (default: EXECUTION_PROVIDER env; "
                         "dry_run = nessuna rete)")
    ap.add_argument("--status", action="store_true",
                    help="stato provider + credenziali (senza stamparle)")
    ap.add_argument("--balance", action="store_true",
                    help="saldo del provider (SX Bet: proxy wallet; "
                         "richiede credenziali)")
    ap.add_argument("--probe", action="store_true",
                    help="probe a stake minimo: misura latenza e slippage")
    ap.add_argument("--market", type=str, default="",
                    help="market_id (SX Bet: marketHash hex; es. 1.234567890)")
    ap.add_argument("--selection", type=int, default=0,
                    help="selection_id dell'esito")
    ap.add_argument("--price", type=float, default=None,
                    help="prezzo LIMIT (default: best available)")
    ap.add_argument("--side", type=str, default="BACK", choices=("BACK", "LAY"))
    ap.add_argument("--stake", type=float, default=None,
                    help=f"stake probe (default EXECUTION_MIN_STAKE_EUR="
                         f"{EXECUTION_MIN_STAKE_EUR})")
    ap.add_argument("--markets", action="store_true",
                    help="elenca i mercati calcio aperti (discovery, senza"
                         " piazzare nulla)")
    ap.add_argument("--max", type=int, default=20,
                    help="numero massimo di mercati da elencare (default 20)")
    ap.add_argument("--dry-run", action="store_true",
                    help="forza DryRunProvider (nessuna chiamata di rete)")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    if args.dry_run:
        os.environ["EXECUTION_DRY_RUN"] = "1"
        # ricarica i flag di config letti a import
        global EXECUTION_DRY_RUN
        EXECUTION_DRY_RUN = True
    if args.provider:
        os.environ["EXECUTION_PROVIDER"] = args.provider
        global EXECUTION_PROVIDER
        EXECUTION_PROVIDER = args.provider

    engine = ExecutionEngine()

    if args.status:
        print(json.dumps(engine.status(), indent=2))
        return 0

    if args.balance:
        try:
            bal = engine.provider.get_balance()
        except Exception as e:
            print(f"ERRORE: saldo non disponibile: {e}")
            return 1
        out = dict(bal or {})
        out["provider"] = engine.provider.name
        out["dry_run"] = isinstance(engine.provider, DryRunProvider)
        print(json.dumps(out, indent=2))
        return 0

    if args.markets:
        markets = engine.discover_markets(max_results=args.max)
        if not markets:
            print("Nessun mercato disponibile (o discovery fallita).")
            return 1
        for m in markets:
            runners = ", ".join(
                f"{r['name']} ({r['selection_id']})" for r in m.get("runners", []))
            print(f"[{m['market_id']}] {m['event_name']} "
                  f"({m.get('country_code')}, kickoff "
                  f"{(m.get('open_date') or '?')[:16]}): "
                  f"{m.get('total_matched') or 0:.0f} matched — {runners}")
        return 0

    if args.probe:
        if not args.market or not args.selection:
            print("ERRORE: --probe richiede --market <id> --selection <id> "
                  "(usa --markets per elencarli)")
            return 2
        res = engine.probe(args.market, args.selection,
                           price=args.price, side=args.side, stake=args.stake)
        print(json.dumps(asdict(res), indent=2))
        return 0 if res.ok else 1

    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())