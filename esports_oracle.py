"""esports_oracle.py — Oracolo eSports (OddsPapi) per il gate TOP-DOWN.

Perche' esiste: la corsia live del progetto non decide col modello bottom-up ma
con lo SCARTO fra una probabilita' "vera" indipendente e il prezzo disponibile
sull'exchange. Per il calcio quella verita' e' la Pinnacle letta dalla cache
the-odds-api (`pinnacle_oracle`). Per l'eSports **the-odds-api non ha il
catalogo**: verificato il 29/09/2026 leggendo per intero la lista ufficiale
(`the-odds-api.com/sports-odds-data/sports-apis.html`) — nessun gruppo eSports,
solo american football / aussie rules / baseball / basketball / boxing /
cricket / golf / handball / ice hockey / lacrosse / MMA / politics / rugby /
soccer / tennis. Senza oracolo il gate risponde `no_oracle` e **nessun ordine
eSports puo' partire**: non e' una soglia da allentare, e' fail-closed per
progetto. Da qui il provider dedicato.

⚠️ DUE COSE MISURATE PRIMA DI SCRIVERE QUESTO CODICE (29/09/2026, sola lettura,
   zero crediti, zero ordini):

1. **Cosa SX pubblica davvero per l'eSports** (`GET /markets/active`,
   `sportId=9`, lettura pubblica): **67 mercati**, di cui `type 52` = moneyline
   **2 vie senza pareggio** (23), `type 3` = Asian Handicap con linea (23),
   `type 1536` = Over/Under sul totale delle mappe (21). `liveEnabled: false`
   su TUTTI e 67 -> sono mercati **pre-match**. Il moneyline eSports ha quindi
   la STESSA forma del tennis (`type 52 = "12"`), non quella del calcio 1X2.
2. **Cosa serve per l'oracolo**: un book sharp indipendente. Il free tier di
   OddsPapi include **Pinnacle** sugli eSports (~4,5% di margine mediano sul
   Match Winner), con il Match Winner come **2 vie** — la stessa forma del
   `type 52` di SX. Il concorrente `odds-api.io` e' stato **scartato sui fatti**:
   il suo free tier espone 2 bookmaker *ricreativi*, cioe' nessuno sharp, e da'
   un soft book non si estrae una probabilita' vera ma il suo margine.

Il settlement NON e' un problema e non richiede questo provider: il percorso
**SX-native** (12/09/2026) legge l'esito direttamente dall'exchange con
`markets/find` sul `market_id` salvato sulla bet — gratis e senza matching per
nome. Questo modulo serve SOLO a decidere il prezzo.

**Cosa NON fa questo modulo** (verificato dai tripwire in `test_esports_oracle`):
non consulta Poisson ne' alcun motore statistico, non scrive sul ledger, non
place ordini, e non fa rete all'import.

**Il gate EV resta definito UNA volta sola**, in `pinnacle_oracle.ev_gate`:

    EV = p_true x (quota - 1) - (1 - p_true)

e le due letture coincidono (`EV >= ev_min` <=> `quota >= true_odd x (1 + ev_min)`).
Qui l'EV non viene riscritto: viene **importato**. Stessa cosa per il de-vig,
che delega a `market_calib.market_implied` (metodi power/multiplicative/shin) —
una seconda implementazione del devig sarebbe un secondo standard di probabilita'
nella stessa pipeline.

CLI:
  venv/bin/python esports_oracle.py --titles                    # zero rete
  venv/bin/python esports_oracle.py --account                   # non consuma quota
  venv/bin/python esports_oracle.py --fixtures lol --days 7
  venv/bin/python esports_oracle.py --odds id1704591169167084
  venv/bin/python esports_oracle.py --fixtures lol --json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from config import load_dotenv

load_dotenv()

logger = logging.getLogger("esports_oracle")

#: Endpoint base di OddsPapi v4. L'autenticazione e' un PARAMETRO DI QUERY
#: (`apiKey`): nessun header, quindi il valore non finisce mai nei log degli
#: header ed e' l'unico punto da mascherare nelle URL.
BASE_URL = os.getenv("ODDSPAPI_BASE", "https://api.oddspapi.io/v4")

#: Nome dell'env che contiene la chiave. Il VALORE non compare mai nel sorgente.
KEY_ENV = "ODDSPAPI_KEY"

#: Titoli eSports che SX Bet pubblica E OddsPapi copre, con lo sportId di
#: OddsPapi (documentato il 29/09/2026). I titoli che OddsPapi ha e SX NON
#: quota (Overwatch, Rainbow Six, StarCraft, ...) restano fuori di proposito:
#: senza un prezzo sull'exchange non c'e' niente da confrontare.
ESPORTS_TITLES: Dict[str, int] = {
    "dota2": 16,
    "cs2": 17,
    "lol": 18,
    "cod": 56,
    "rocket_league": 59,
    "valorant": 61,
}

#: Etichetta di lega letta da SX (`leagueLabel`) -> titolo. Tabella ESPLICITA
#: e ordinata, mai fuzzy: un'etichetta nuova deve risultare SCONOSCIUTA
#: (`None`) invece di diventare "il titolo piu' simile". Un titolo sbagliato
#: confronterebbe il prezzo con l'oracolo di un altro gioco — cioe' il modo
#: piu' rapido per generare un falso value.
SX_LABEL_TITLES: Tuple[Tuple[str, str], ...] = (
    ("counter-strike", "cs2"),
    ("cs2", "cs2"),
    ("dota", "dota2"),
    ("league of legends", "lol"),
    ("lol", "lol"),
    ("valorant", "valorant"),
    ("call of duty", "cod"),
    ("rocket league", "rocket_league"),
)

#: Nomi accettati per il mercato "chi vince la partita" nel catalogo OddsPapi.
#: Market id documentato del Match Winner e i suoi due esiti (Team 1 / Team 2).
#: Usato come fallback quando `/markets` non risponde: il catalogo resta la
#: fonte preferita (gli id dei mercati a linea cambiano per linea e periodo).
#: ⚠️ MISURA LIVE 29/09/2026 (container, fixture reali): il Match Winner
#: compare col market id **185** ed esiti **185/186** — la forma documentata
#: 171/172 non e' apparsa in NESSUN payload reale. L'ordine e' FUORI DI
#: PROPOSITO: `winner_market` prova la forma osservata sul campo PRIMA della
#: documentazione (i vicoli ciechi vanno seguiti, non documentati).
WINNER_MARKET_ID = "185"
WINNER_OUTCOMES: Tuple[str, str] = ("185", "186")
#: Forma documentata (171/172): provata come FALLBACK, mai come primaria.
WINNER_MARKET_ID_LEGACY = "171"
WINNER_OUTCOMES_LEGACY: Tuple[str, str] = ("171", "172")

#: Bookmaker dell'oracolo: Pinnacle (il benchmark sharp). Il nome e' quello
#: usato da OddsPapi negli slug di `bookmakerOdds`.
ORACLE_BOOK = os.getenv("ODDSPAPI_BOOK", "pinnacle")

#: Metodo di de-vig: default del progetto (`power`, corregge il
#: favourite-longshot bias). Override con `ESPORTS_DEVIG_METHOD` o per chiamata.
DEVIG_METHOD = os.getenv("ESPORTS_DEVIG_METHOD",
                         os.getenv("PINNACLE_DEVIG_METHOD", "power"))

#: Finestra massima di discovery (giorni) e timeout di trasporto.
MAX_DAYS_AHEAD = 7
DEFAULT_TIMEOUT = 20.0

#: Contratto di un getter HTTP iniettabile: `(url, params, timeout) -> (status, payload)`.
#: I test iniettano un fake e girano OFFLINE (nessuna rete, nessuna chiave).
HttpGet = Callable[..., Tuple[int, Any]]


class EsportsOracleError(RuntimeError):
    """Errore del provider eSports (l'oracolo e' fail-closed, mai silenzioso)."""


# ---------------------------------------------------------------------------
# 1. Titoli e identificatori
# ---------------------------------------------------------------------------

def title_of_league(label: Any) -> Optional[str]:
    """Titolo eSports dedotto dall'etichetta di lega di SX. None = sconosciuto.

    Deterministico e prudente: un'etichetta non riconosciuta torna `None`, e
    il chiamante NON deve indovinare (una partita di Valorant confrontata con
    l'oracolo di LoL e' un falso value, non un'opportunita').
    """
    text = " ".join(str(label or "").strip().casefold().split())
    if not text:
        return None
    for needle, title in SX_LABEL_TITLES:
        if needle in text:
            return title
    return None


def sport_id_of(title_or_id: Any) -> Optional[int]:
    """Titolo (o id numerico) -> sportId OddsPapi. None se non eSports noto."""
    text = str(title_or_id or "").strip()
    if not text:
        return None
    if text.isdigit():
        value = int(text)
        return value if value in set(ESPORTS_TITLES.values()) else None
    key = text.casefold()
    if key in ESPORTS_TITLES:
        return ESPORTS_TITLES[key]
    deduced = title_of_league(key)
    return ESPORTS_TITLES.get(deduced) if deduced else None


def titles_of(labels: Iterable[Any]) -> List[str]:
    """Titoli noti fra le etichette date (dedup, ordine stabile)."""
    out: List[str] = []
    for label in labels:
        title = title_of_league(label)
        if title and title not in out:
            out.append(title)
    return out


# ---------------------------------------------------------------------------
# 2. Trasporto (chiave SOLO da env o iniettata; mai un valore di default)
# ---------------------------------------------------------------------------

def api_key(explicit: Optional[str] = None) -> str:
    """Chiave di accesso: quella iniettata vince sull'ambiente."""
    value = explicit if explicit is not None else os.getenv(KEY_ENV)
    return (value or "").strip()


def configured(explicit: Optional[str] = None) -> bool:
    """True se esiste una chiave: senza, l'oracolo e' fail-closed."""
    return bool(api_key(explicit))


def _default_http_get(url: str, params: Optional[Dict[str, Any]] = None,
                      timeout: float = DEFAULT_TIMEOUT) -> Tuple[int, Any]:
    """Getter reale. `requests` importato PIGRO: nessuna rete all'import."""
    import requests                                    # import locale voluto
    response = requests.get(url, params=params or {}, timeout=timeout)
    try:
        payload: Any = response.json()
    except Exception:
        payload = None
    return response.status_code, payload


def _call(path: str, params: Optional[Dict[str, Any]] = None, *,
          http_get: Optional[HttpGet] = None, key: Optional[str] = None,
          timeout: float = DEFAULT_TIMEOUT) -> Dict[str, Any]:
    """Una chiamata all'API, con l'esito SEMPRE dichiarato (mai un'eccezione).

    Ritorna `{ok, status, payload, error, code}`. Senza chiave non si chiama
    affatto: un oracolo che "prova lo stesso" produrrebbe errori 401 a ogni
    giro del job invece di un motivo leggibile.
    """
    token = api_key(key)
    if not token:
        return {"ok": False, "status": 0, "payload": None, "code": None,
                "error": f"{KEY_ENV} assente: nessuna chiamata (fail-closed)"}
    query = dict(params or {})
    query["apiKey"] = token
    getter = http_get or _default_http_get
    url = f"{BASE_URL.rstrip('/')}/{str(path).lstrip('/')}"
    try:
        status, payload = getter(url, query, timeout)
    except Exception as exc:                                  # trasporto rotto
        return {"ok": False, "status": 0, "payload": None, "code": None,
                "error": f"trasporto {type(exc).__name__}: {exc}"}
    if int(status or 0) != 200:
        message = payload.get("message") if isinstance(payload, dict) else None
        code = payload.get("code") if isinstance(payload, dict) else None
        return {"ok": False, "status": int(status or 0), "payload": payload,
                "code": code, "error": str(message or f"HTTP {status}")}
    return {"ok": True, "status": 200, "payload": payload, "code": None,
            "error": None}


def _as_list(payload: Any) -> List[Dict[str, Any]]:
    """Lista di dizionari da una risposta (lista diretta o incapsulata)."""
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("fixtures", "data", "results", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
    return []


# ---------------------------------------------------------------------------
# 3. Endpoint di lettura
# ---------------------------------------------------------------------------

def account(*, http_get: Optional[HttpGet] = None,
            key: Optional[str] = None) -> Dict[str, Any]:
    """Stato della sottoscrizione e quota residua. **Non consuma richieste.**

    Documentato: `/v4/account` e' l'unico endpoint sempre accessibile, anche a
    quota esaurita. E' quindi il posto giusto dove leggere il consumo senza
    pagarlo — lo stesso ruolo di `get_remaining` per the-odds-api.
    """
    return _call("account", {}, http_get=http_get, key=key)


def fixtures(title_or_id: Any, *, days_ahead: int = MAX_DAYS_AHEAD,
             has_odds: bool = True, http_get: Optional[HttpGet] = None,
             key: Optional[str] = None, now: Optional[datetime] = None
             ) -> Dict[str, Any]:
    """Partite eSports in finestra per un titolo. 1 richiesta.

    `hasOdds=true` e' il filtro che conta: i tornei eSports hanno centinaia di
    righe dormienti fuori circuito, e chiedere partite senza quote brucerebbe
    richieste per nulla.
    """
    sport_id = sport_id_of(title_or_id)
    if sport_id is None:
        return {"ok": False, "sport_id": None, "fixtures": [], "requests": 0,
                "error": f"titolo eSports non riconosciuto: {title_or_id!r}"}
    today = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    span = max(0, min(int(days_ahead or 0), MAX_DAYS_AHEAD))
    res = _call("fixtures", {
        "sportId": sport_id,
        "from": today.strftime("%Y-%m-%d"),
        "to": (today + timedelta(days=span)).strftime("%Y-%m-%d"),
        "hasOdds": "true" if has_odds else "false",
    }, http_get=http_get, key=key)
    out = {"ok": res["ok"], "sport_id": sport_id, "fixtures": [],
           "requests": 1 if res.get("status") else 0, "error": res["error"],
           "status": res["status"]}
    if res["ok"]:
        out["fixtures"] = _as_list(res["payload"])
    return out


def odds(fixture_id: Any, *, bookmaker: Optional[str] = ORACLE_BOOK,
         http_get: Optional[HttpGet] = None, key: Optional[str] = None
         ) -> Dict[str, Any]:
    """Quote di UNA partita. 1 richiesta (anche a risposta vuota)."""
    fid = str(fixture_id or "").strip()
    if not fid:
        return {"ok": False, "payload": None, "requests": 0,
                "error": "fixture_id mancante"}
    # ⚠️ MISURA LIVE 29/09/2026 (container, fixture reali CBLOL/LCS): il
    # parametro documentato `bookmakers=<slug>` FA SVUOTARE `bookmakerOdds`
    # (payload ridotto ai soli metadati, 12 chiavi); SENZA il parametro il
    # payload porta TUTTI i book (89 su CBLOL). Il filtro del book avviene
    # quindi LATO CLIENT in `winner_market(payload, bookmaker=...)`: il
    # parametro resta nella firma solo come SELEZIONE del book da estrarre.
    params: Dict[str, Any] = {"fixtureId": fid}
    res = _call("odds", params, http_get=http_get, key=key)
    return {"ok": res["ok"], "payload": res["payload"],
            "requests": 1 if res.get("status") else 0, "error": res["error"],
            "status": res["status"]}


def markets(title_or_id: Any, *, http_get: Optional[HttpGet] = None,
            key: Optional[str] = None) -> Dict[str, Any]:
    """Catalogo dei mercati del titolo. 1 richiesta.

    Serve a NON hardcodare gli id dei mercati a linea (che cambiano per linea e
    periodo): il Match Winner ha un id stabile, ma handicap e totali no.
    """
    sport_id = sport_id_of(title_or_id)
    if sport_id is None:
        return {"ok": False, "sport_id": None, "markets": [], "requests": 0,
                "error": f"titolo eSports non riconosciuto: {title_or_id!r}"}
    res = _call("markets", {"sportId": sport_id}, http_get=http_get, key=key)
    return {"ok": res["ok"], "sport_id": sport_id,
            "markets": _as_list(res["payload"]) if res["ok"] else [],
            "requests": 1 if res.get("status") else 0, "error": res["error"],
            "status": res["status"]}


# ---------------------------------------------------------------------------
# 4. Estrazione del Match Winner (2 vie)
# ---------------------------------------------------------------------------

def _price_of(node: Any) -> Optional[float]:
    """Prezzo di un nodo esito di OddsPapi. None se assente, sospeso o <= 1.

    Il payload annida il prezzo sotto `players["0"]` (le "quote giocatore" sono
    la forma generale del provider, usata anche quando il mercato non ha
    giocatori). Il campo diretto `price` e' accettato come seconda forma: se il
    provider appiattisce lo schema, il parser non deve smettere di funzionare.
    """
    if not isinstance(node, dict):
        return None
    if node.get("active") is False:      # bookmaker ha SOSPESO quel prezzo
        return None
    candidates: List[Any] = [node.get("price")]
    players = node.get("players")
    if isinstance(players, dict):
        for key in sorted(players, key=str):
            entry = players.get(key)
            if isinstance(entry, dict):
                candidates.append(entry.get("price"))
    for value in candidates:
        try:
            price = float(value)
        except (TypeError, ValueError):
            continue
        if price > 1.0:
            return price
    return None


def winner_market(payload: Any, bookmaker: str = ORACLE_BOOK,
                  team_names: Optional[Tuple[str, str]] = None
                  ) -> Optional[Dict[str, Any]]:
    """Il mercato 'chi vince' di UN bookmaker dal payload di `odds`.

    FAIL-CLOSED su ENTRAMBI gli esiti: con un solo lato il margine mancante
    verrebbe attribuito all'altro e la probabilita' "vera" risulterebbe
    sbagliata **senza che nulla lo dica**. Meglio nessun oracolo che un
    oracolo distorto (stessa regola del 1X2 a tre esiti).

    ⚠️ MISURA LIVE 29/09: il payload `/odds` reale NON porta i nomi dei
    partecipanti (solo `participant1Id`/`participant2Id`). I nomi arrivano
    dalla RIGA FIXTURE via `team_names=(nome1, nome2)` — il chiamante e'
    responsabile dell'aggancio fixture->odds per ID (vedi `oracle_for_fixture`).
    """
    if not isinstance(payload, dict):
        return None
    books = payload.get("bookmakerOdds")
    if not isinstance(books, dict):
        return None
    wanted = str(bookmaker or "").strip().casefold()
    node: Optional[dict] = None
    for slug, value in books.items():
        if str(slug or "").strip().casefold() == wanted:
            node = value if isinstance(value, dict) else None
            break
    if node is None:
        return None
    book_markets = node.get("markets")
    if not isinstance(book_markets, dict):
        return None
    # Forma osservata sul campo (185/186) prima, forma documentata (171/172)
    # come fallback. La forma 171/172 NON e' mai apparsa nei payload reali
    # misurati: resta solo per robustezza se il provider cambia schema.
    for market_id, outcome_ids in (
            (WINNER_MARKET_ID, WINNER_OUTCOMES),
            (WINNER_MARKET_ID_LEGACY, WINNER_OUTCOMES_LEGACY)):
        mkt = book_markets.get(market_id)
        if not isinstance(mkt, dict):
            continue
        outcomes = mkt.get("outcomes")
        if not isinstance(outcomes, dict):
            continue
        first = _price_of(outcomes.get(outcome_ids[0]))
        second = _price_of(outcomes.get(outcome_ids[1]))
        if first is None or second is None:
            continue
        label1 = label2 = ""
        if isinstance(team_names, (tuple, list)) and len(team_names) == 2:
            label1 = str(team_names[0] or "").strip()
            label2 = str(team_names[1] or "").strip()
        return {
            "market_id": market_id,
            "team1": label1 or str(payload.get("participant1Name") or "").strip(),
            "team2": label2 or str(payload.get("participant2Name") or "").strip(),
            "odds": {"1": first, "2": second},
            "bookmaker": str(bookmaker or "").strip().casefold(),
        }
    return None


# ---------------------------------------------------------------------------
# 5. Probabilita' vera (de-vig: DELEGA a market_calib, nessuna copia)
# ---------------------------------------------------------------------------

def true_probabilities(odds_map: Dict[str, float], *,
                       method: Optional[str] = None
                       ) -> Optional[Dict[str, Any]]:
    """Quote 2 vie -> probabilita' fair (somma 1) + `overround`.

    Delega a `market_calib.market_implied`, la stessa funzione del percorso
    calcio: due implementazioni del de-vig produrrebbero due nozioni di
    "probabilita' vera" nella stessa pipeline.
    """
    if not odds_map or len(odds_map) < 2:
        return None
    try:
        from market_calib import market_implied
    except Exception as exc:                                # pragma: no cover
        logger.warning("esports_oracle: market_calib non disponibile (%s)", exc)
        return None
    result = market_implied({str(k): float(v) for k, v in odds_map.items()},
                            method=method or DEVIG_METHOD)
    if not result:
        return None
    return result


def _same_team(a: str, b: str) -> bool:
    """Confronto nomi fra provider DIVERSI (deterministico, mai fuzzy)."""
    try:
        from team_names import same_team
    except Exception:                                       # pragma: no cover
        return str(a or "").strip().casefold() == str(b or "").strip().casefold()
    return bool(same_team(a or "", b or ""))


def oracle(payload: Any, *, home: str = "", away: str = "",
           bookmaker: str = ORACLE_BOOK, method: Optional[str] = None,
           team_names: Optional[Tuple[str, str]] = None
           ) -> Optional[Dict[str, Any]]:
    """Probabilita' "vera" 2 vie orientate su (home, away). None = nessun oracolo.

    Struttura di ritorno compatibile con `pinnacle_oracle.ev_gate`/`fair_odds`:
    le chiavi numeriche sono gli esiti, il resto sono METADATI dichiarati
    (`_META_KEYS`), che il gate salta e non puo' mai usare in un calcolo.

    L'orientamento e' IMPORTANTE: se l'oracolo elenca le squadre al contrario
    rispetto al chiamante, le probabilita' vengono scambiate — un incrocio non
    rilevato comprerebbe l'esito sbagliato "con valore".
    """
    market = winner_market(payload, bookmaker, team_names=team_names)
    if market is None:
        return None
    probs = true_probabilities(market["odds"], method=method)
    if not probs:
        return None
    first, second = market["team1"], market["team2"]
    if home and away:
        if _same_team(home, first) and _same_team(away, second):
            pass                                        # orientamento coerente
        elif _same_team(home, second) and _same_team(away, first):
            probs = {"1": probs["2"], "2": probs["1"],
                     "overround": probs.get("overround")}
        else:
            # Nessun aggancio: NON si indovina. Un player match approssimativo
            # qui vale un verdetto preso sull'oracolo di un'altra partita.
            return None
    out: Dict[str, Any] = {esito: round(float(probs[esito]), 6)
                           for esito in ("1", "2") if esito in probs}
    if len(out) < 2:
        return None
    out["overround"] = probs.get("overround")
    out["sources"] = [market["bookmaker"] or str(bookmaker).casefold()]
    out["n_sources"] = 1
    out["consensus_method"] = "single_book"
    out["validated"] = None
    out["fallback"] = "single_book"
    out["agreement_pp"] = None
    out["_teams"] = {"1": first, "2": second}
    return out


def oracle_for_fixture(fixture: Dict[str, Any], *, home: str = "", away: str = "",
                       bookmaker: str = ORACLE_BOOK,
                       http_get: Optional[HttpGet] = None,
                       key: Optional[str] = None) -> Dict[str, Any]:
    """Scorciatoia: fixture -> quote -> oracolo, con una sola chiamata.

    `fixture` e' una riga di `fixtures()`: da li' arrivano `fixtureId` e i nomi
    dei partecipanti. Ritorna sempre un oggetto con `ok` e `error` dichiarati.
    """
    fid = None
    if isinstance(fixture, dict):
        fid = fixture.get("fixtureId") or fixture.get("fixture_id")
    fixture = fixture if isinstance(fixture, dict) else {}
    # ⚠️ MISURA LIVE 29/09: i NOMI arrivano dalla riga fixture (il payload
    # /odds ha solo gli ID). Guardia di orientamento per ID: se il payload
    # elenca i partecipanti AL CONTRARIO rispetto alla fixture, l'ordine dei
    # nomi va scambiato; se gli ID non coincidono per niente, fail-closed
    # (mai un verdetto orientato a caso).
    name1 = str(fixture.get("participant1Name") or "").strip()
    name2 = str(fixture.get("participant2Name") or "").strip()
    home = home or name1
    away = away or name2
    res = odds(fid, bookmaker=bookmaker, http_get=http_get, key=key)
    if not res["ok"]:
        return {"ok": False, "fixture_id": fid, "oracle": None,
                "requests": res["requests"], "error": res["error"]}
    payload = res["payload"]
    pid1 = str((payload or {}).get("participant1Id") or "")
    pid2 = str((payload or {}).get("participant2Id") or "")
    fpid1 = str(fixture.get("participant1Id") or "")
    fpid2 = str(fixture.get("participant2Id") or "")
    if pid1 and pid2 and fpid1 and fpid2:
        if pid1 == fpid2 and pid2 == fpid1:
            name1, name2 = name2, name1               # ordine invertito
        elif not (pid1 == fpid1 and pid2 == fpid2):
            return {"ok": False, "fixture_id": fid, "oracle": None,
                    "requests": res["requests"],
                    "error": "partecipanti del payload odds non coincidono "
                             "con la fixture (id): aggancio rifiutato"}
    probs = oracle(payload, home=home, away=away, bookmaker=bookmaker,
                   team_names=(name1, name2))
    if probs is None:
        return {"ok": False, "fixture_id": fid, "oracle": None,
                "requests": res["requests"],
                "error": f"nessun Match Winner completo per '{bookmaker}' "
                         f"(o nomi non agganciabili): {home} vs {away}".strip()}
    return {"ok": True, "fixture_id": fid, "oracle": probs,
            "requests": res["requests"], "error": None}


# ---------------------------------------------------------------------------
# 6. Gate EV: IMPORTATO, non riscritto (una sola definizione nel progetto)
# ---------------------------------------------------------------------------

def ev_gate(true_probs: Dict[str, Any], prices: Dict[str, float], *,
            ev_min: Optional[float] = None, method: Optional[str] = None
            ) -> List[Dict[str, Any]]:
    """Candidati value (EV, true-odd, quota minima di trigger).

    `method` e' di `pinnacle_oracle` e resta disponibile per compatibilita':
    la funzione e' la STESSA usata dal calcio, cosi' i due percorsi non possono
    adottare due standard di valore.
    """
    from pinnacle_oracle import ev_gate as _gate
    return _gate(true_probs, prices, ev_min=ev_min, method=method)


def value_candidates(true_probs: Dict[str, Any], prices: Dict[str, float], *,
                     ev_min: Optional[float] = None
                     ) -> List[Dict[str, Any]]:
    """Solo gli esiti che fanno SCATTARE il trigger (EV >= soglia)."""
    from pinnacle_oracle import value_candidates as _value
    return _value(true_probs, prices, ev_min=ev_min)


def min_ev() -> float:
    """Soglia EV di produzione (`value_filter.EV_MIN`), mai un default copiato."""
    from pinnacle_oracle import DEFAULT_EV_MIN
    return float(DEFAULT_EV_MIN)


def candidate_for(true_probs: Dict[str, Any], price: float, outcome: str,
                  *, ev_min: Optional[float] = None
                  ) -> Optional[Dict[str, Any]]:
    """Il verdetto per UN esito a UN prezzo (SX Bet). None se non valutabile."""
    if not outcome:
        return None
    rows = ev_gate(true_probs, {outcome: price}, ev_min=ev_min)
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# 7. CLI (diagnostica: letture dichiarate, nessun ordine, nessuna scrittura)
# ---------------------------------------------------------------------------

def _print_titles() -> None:
    print("Titoli eSports con oracolo (sportId OddsPapi):")
    for title, sid in sorted(ESPORTS_TITLES.items(), key=lambda kv: kv[1]):
        print(f"  {sid:>3}  {title}")
    print("\nEtichette di lega SX riconosciute:")
    for needle, title in SX_LABEL_TITLES:
        print(f"  '{needle}' -> {title}")


def _print_fixtures(res: Dict[str, Any]) -> None:
    if not res["ok"]:
        print(f"KO  [{res.get('status')}] {res['error']}")
        return
    rows = res["fixtures"]
    print(f"Titolo sportId={res['sport_id']} — {len(rows)} partite con quote "
          f"(finestra {MAX_DAYS_AHEAD}gg)")
    for row in rows:
        print(f"  {row.get('participant1Name')} vs {row.get('participant2Name')}"
              f" | {row.get('tournamentName')} | {row.get('startTime')}"
              f" | {row.get('fixtureId')}")


def _print_odds(res: Dict[str, Any]) -> None:
    if not res["ok"]:
        print(f"KO  [{res.get('status')}] {res['error']}")
        return
    payload = res["payload"] or {}
    market = winner_market(payload)
    if market is None:
        print("Nessun Match Winner completo da Pinnacle in questa risposta.")
        return
    probs = true_probabilities(market["odds"]) or {}
    print(f"{market['team1']} vs {market['team2']}  ({market['bookmaker']})")
    for esito in ("1", "2"):
        price = market["odds"][esito]
        prob = probs.get(esito)
        team = market["team1"] if esito == "1" else market["team2"]
        extra = f" -> p_true {prob:.4f} (true odd {1/prob:.3f})" if prob else ""
        print(f"  {esito} {team:<24} @ {price:<8.4g}{extra}")
    if probs.get("overround"):
        print(f"  overround {probs['overround']:.4f}")
    print(f"\nSoglia EV di produzione: {min_ev()*100:.1f}%")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Oracolo eSports (OddsPapi) — sola lettura, nessun ordine.")
    parser.add_argument("--titles", action="store_true",
                        help="elenca i titoli coperti (zero rete)")
    parser.add_argument("--account", action="store_true",
                        help="stato/quota della sottoscrizione (non consuma quota)")
    parser.add_argument("--fixtures", metavar="TITOLO",
                        help="partite con quote del titolo (es. lol, cs2, 17)")
    parser.add_argument("--odds", metavar="FIXTURE_ID",
                        help="Match Winner Pinnacle di una partita")
    parser.add_argument("--days", type=int, default=MAX_DAYS_AHEAD,
                        help=f"finestra di discovery in giorni (max {MAX_DAYS_AHEAD})")
    parser.add_argument("--book", default=ORACLE_BOOK, help="bookmaker oracolo")
    parser.add_argument("--json", action="store_true", help="output JSON")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.titles:
        _print_titles()
        return 0

    if not configured():
        print(f"{KEY_ENV} assente: l'oracolo eSports e' fail-closed "
              f"(imposta la chiave su Railway e in locale).")
        return 1

    if args.account:
        res = account()
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
        elif res["ok"]:
            payload = res["payload"] or {}
            limit = payload.get("request_limit")
            used = payload.get("request_count")
            print(f"Quota OddsPapi: {used}/{limit} richieste usate")
        else:
            print(f"KO  [{res.get('status')}] {res['error']}")
        return 0 if res["ok"] else 2

    if args.fixtures:
        res = fixtures(args.fixtures, days_ahead=args.days)
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
        else:
            _print_fixtures(res)
        return 0 if res["ok"] else 2

    if args.odds:
        res = odds(args.odds, bookmaker=args.book)
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
        else:
            _print_odds(res)
        return 0 if res["ok"] else 2

    parser.print_help()
    return 0


__all__ = [
    "BASE_URL", "DEVIG_METHOD", "ESPORTS_TITLES", "EsportsOracleError",
    "HttpGet", "KEY_ENV", "MAX_DAYS_AHEAD", "ORACLE_BOOK", "SX_LABEL_TITLES",
    "WINNER_MARKET_ID", "WINNER_MARKET_NAMES", "WINNER_OUTCOMES",
    "account", "api_key", "candidate_for", "configured", "ev_gate", "fixtures",
    "main", "markets", "min_ev", "odds", "oracle", "oracle_for_fixture",
    "sport_id_of", "title_of_league", "titles_of", "true_probabilities",
    "value_candidates", "winner_market",
]


if __name__ == "__main__":                                  # pragma: no cover
    raise SystemExit(main())
