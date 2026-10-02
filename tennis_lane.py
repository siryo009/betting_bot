"""tennis_lane.py — corsia TENNIS in SOLA TELEMETRIA (30/09/2026).

Perche' esiste: il tennis ha un oracolo sharp **completo e gratis** sulla
chiave the-odds-api che il progetto gia' paga (Pinnacle `h2h` a 2 esiti,
16/16 eventi misurati il 30/09), e i prezzi SX sono gia' leggibili
(`sportId 6`, `type 52`). Manca solo il pezzo che li mette in relazione.

⚠️ **NON E' UNA CORSIA DI ORDINI — e la scelta e' MISURATA, non prudenziale.**
Il 30/09/2026 il gate e' stato applicato a 66 match SX reali su 3 tornei
ATP/WTA con Pinnacle a 2 esiti: 55 coppie valutate, 7 lati con EV > 0,
**EV MASSIMO +0.69%** e **zero candidati sopra qualunque soglia >= 1%**.
Il prezzo SX del tennis e' allineato a Pinnacle entro ~0.7% (spread
d'exchange `inv_sum` 1.003-1.014): il ritardo di prezzo che la strategia
top-down compra sul tennis non c'e'. Accendere ordini reali produrrebbe zero
ordini, quindi questo modulo **misura** (registra i candidati nel ledger e
lascia la telemetria nel tempo) e non tocca mai il percorso del denaro:
`auto_bet` non lo importa, e un tripwire lo verifica.

Cosa fa:
1. `refresh_oracle()` — elenca le chiavi tennis ATTIVE con `/v4/sports`
   (endpoint **gratuito**) e scarica `h2h` dei tornei con cache scaduta
   (**1 credito/torneo**), scrivendo `toa_<key>.json` nella stessa cartella
   delle cache del calcio: cosi' `pinnacle_oracle.load_oracle` la legge senza
   sapere nulla del tennis. Budget giornaliero e rispetto dell'hard-stop
   crediti; fail-closed senza chiave.
2. `discover()` — match SX tennis in finestra (lettura PUBBLICA: zero chiavi,
   zero crediti, zero ordini).
3. `picks()` — per ogni match: oracolo a 2 esiti + gate EV + **fascia quota
   `TENNIS_ODDS_MIN`-`TENNIS_ODDS_MAX`** (1.30-2.50 dal 02/10/2026).
4. `scan()` — registra la telemetria nel ledger (`predictions`, mercato
   `TENNIS`) con `match_id` `sx-tennis-<hash>`: il prefisso `sx-` fa si' che il
   settlement SX-native esistente (`sx_signals._results_from_sx`) la saldi
   GRATIS, perche' il market hash e' salvato.

⚠️ STRUTTURA SX DEL TENNIS (misurata, non dedotta): il tennis e' **UN mercato
per match**, e i due lati stanno sulle chiavi del book `1` (= `outcome_one`,
cioe' `team_one_name`) e `2` (= "Not X", cioe' l'avversario). NON sono due
mercati separati come negli eSports: leggere solo `book[1]` (comportamento di
`esports_lane.discover`) perderebbe meta' del mercato.

Fail-safe totale: qualunque errore -> [] / dizionario con `error`, mai
un'eccezione verso il chiamante. `TENNIS_LANE=0` spegne tutto.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configurazione (tutto da env: una soglia e' una decisione, non un dettaglio)
# ---------------------------------------------------------------------------

#: Identita' del mercato su SX Bet: sportId 6 = tennis, type 52 = moneyline
#: "12" (chi vince, senza pareggio). ⚠️ NON e' sportId 2.
SX_SPORT_ID = os.getenv("TENNIS_SX_SPORT_ID", "6")
SX_TYPE_ID = os.getenv("TENNIS_SX_TYPE_ID", "52")

#: Mercato nel ledger. NUOVO di proposito: nessuna corsia di ordini lo legge,
#: quindi una riga `TENNIS` non puo' diventare una puntata per sbaglio.
MARKET = "TENNIS"

#: Finestra del palinsesto tennis: piu' lunga del calcio (i tornei pubblicano
#: il tabellone con giorni di anticipo).
HOURS_AHEAD = float(os.getenv("TENNIS_HOURS_AHEAD", "48"))
MAX_MARKETS = int(os.getenv("TENNIS_MAX_MARKETS", "300"))

#: Soglia EV. La direttiva del 30/09 chiedeva "EV > 2.5%" per il tennis: e' una
#: soglia PROPRIA e dichiarata (il calcio resta a `value_filter.EV_MIN`), cosi'
#: il valore si legge da un solo posto e si cambia senza toccare il codice.
EV_MIN = float(os.getenv("TENNIS_EV_MIN", "0.025"))

#: Fascia quota della corsia (02/10/2026). E' PROPRIA del tennis e piu' larga
#: di quella calcistica 1.30-1.80 (`value_filter`): qui si seleziona su
#: ENTRAMBI i lati di un mercato a 2 vie, e il lato sfavorito di un match
#: equilibrato vale spesso 1.80-2.50. Oltre la banda non c'e' un edge da
#: comprare, solo varianza: un EV alto su un longshot e' quasi sempre rumore
#: del book (stessa lezione del `MAX_ODDS` dei surebet).
#: MISURA che l'ha motivata — i 7 ordini reali piazzati dal 01/10 (P/L
#: -6,75 USDC, ROI -75,0%, hit 1/6): 5 delle 6 sconfitte erano a quota >= 2,42
#: con punte a 17,02. Con questa banda sarebbero passati 2 ordini su 7
#: (+0,75 e -1,50 = -0,75 netto invece di -6,75).
ODDS_MIN = float(os.getenv("TENNIS_ODDS_MIN", "1.30"))
ODDS_MAX = float(os.getenv("TENNIS_ODDS_MAX", "2.50"))


def in_odds_band(price: Any) -> bool:
    """True se la quota e' nella fascia giocabile della corsia tennis.

    UNICO punto di verita' della fascia: la corsia ordini la riusa come difesa
    in profondita' (`auto_bet._tennis_picks`), cosi' il limite non puo' vivere
    in due posti e divergere. Fail-closed: una quota non numerica (o assente)
    NON e' nella banda.
    """
    try:
        value = float(price)
    except (TypeError, ValueError):
        return False
    return ODDS_MIN <= value <= ODDS_MAX


#: Coerenza del mercato: 1/prezzo_1 + 1/prezzo_2 su un exchange ~1. Fuori banda
#: il book e' sporco e l'EV finto e' un artefatto aritmetico (stessa lezione
#: del `MAX_ODDS` dei surebet).
MIN_INV_SUM = float(os.getenv("TENNIS_MIN_INV_SUM", "0.98"))
MAX_INV_SUM = float(os.getenv("TENNIS_MAX_INV_SUM", "1.08"))

#: TTL delle cache dell'oracolo. 12h = 3 tornei x 2 giri = ~6 crediti/giorno.
ORACLE_TTL_MIN = float(os.getenv("TENNIS_ORACLE_TTL_MIN", "720"))
REQ_BUDGET_DAY = int(os.getenv("TENNIS_REQ_BUDGET_DAY", "8"))

#: TTL della DISCOVERY SX (order book). Il giro ordini gira ogni 60s e la
#: discovery legge ~50 order book per volta: senza memo sarebbero ~3000
#: richieste/ora sull'API pubblica per un prezzo usato SOLO dal gate EV (il
#: prezzo d'ordine viene riletto vivo al momento dell'ordine). Default 5 minuti.
DISCOVERY_TTL_S = float(os.getenv("TENNIS_DISCOVERY_TTL_S", "300"))

_DISCOVERY_MEMO: Dict[str, Any] = {"key": None, "ts": 0.0, "rows": []}


def reset_cache() -> None:
    """Azzera la memo di discovery (usata dai test e a cambio finestra)."""
    _DISCOVERY_MEMO["key"] = None
    _DISCOVERY_MEMO["ts"] = 0.0
    _DISCOVERY_MEMO["rows"] = []

#: Ledger/keyword tennis nel payload the-odds-api.
TENNIS_KEY_PREFIX = "tennis_"
ODDS_BASE = os.getenv("TENNIS_ODDS_BASE", "https://api.the-odds-api.com/v4")


def _num_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return float(default)


def enabled() -> bool:
    """Interruttore della corsia (`TENNIS_LANE=0` per spegnerla)."""
    raw = os.getenv("TENNIS_LANE")
    if raw is None or raw == "":
        return True
    return str(raw).strip().lower() not in ("0", "false", "no", "off")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def oracle_cache_dir() -> Path:
    """Cartella delle cache dell'oracolo: QUELLA del calcio per default.

    Non e' pigrizia: `pinnacle_oracle.load_oracle` legge li' senza sapere nulla
    del tennis. Una cartella separata obbligherebbe a passarla a ogni chiamata
    e a ricordarsene in ogni percorso futuro.
    """
    raw = os.getenv("TENNIS_ORACLE_CACHE")
    if raw:
        return Path(raw)
    try:
        from config import DATA_DIR
        return Path(DATA_DIR)
    except Exception:                                          # pragma: no cover
        return Path("data")


def state_path() -> Path:
    raw = os.getenv("TENNIS_LANE_STATE")
    if raw:
        return Path(raw)
    return oracle_cache_dir() / "tennis_lane" / "state.json"


# ---------------------------------------------------------------------------
# Stato (budget giornaliero) — scrittura atomica, lettura fail-safe
# ---------------------------------------------------------------------------

def _empty_state() -> dict:
    return {"day": _now().strftime("%Y-%m-%d"), "requests": 0}


def _load_state() -> Tuple[dict, bool]:
    """(stato, sano). Un file corrotto NON fa fallire la corsia: si riparte.

    `sano=False` segnala che il budget di oggi non e' affidabile: chi consuma
    credito lo tratta come "budget probabilmente intaccato" e decide di
    conseguenza (qui: non si spende, vedi `budget_left`).
    """
    path = state_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return _empty_state(), False
        return data, True
    except FileNotFoundError:
        return _empty_state(), True
    except Exception as exc:
        logger.warning("tennis_lane: stato illeggibile (%s) — riparto da zero", exc)
        return _empty_state(), False


def _save_state(state: dict) -> None:
    path = state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except Exception as exc:
        logger.warning("tennis_lane: stato non salvato (%s)", exc)


def _roll_day(state: dict) -> dict:
    today = _now().strftime("%Y-%m-%d")
    if str(state.get("day") or "") != today:
        state["day"] = today
        state["requests"] = 0
    return state


def budget_left(state: dict, *, healthy: bool = True) -> bool:
    """True se si puo' ancora spendere una richiesta oggi."""
    if not healthy:
        return False
    if int(state.get("requests") or 0) >= int(REQ_BUDGET_DAY):
        return False
    try:
        import odds_api
        if odds_api.credits_hard_stopped():
            logger.info("tennis_lane: hard-stop crediti attivo — nessuna richiesta")
            return False
    except Exception:
        pass
    return True


# ---------------------------------------------------------------------------
# 1. ORACOLO: chiavi tennis attive (gratis) + quote (1 credito/torneo)
# ---------------------------------------------------------------------------

def _cache_is_fresh(path: Path) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        age_min = (time.time() - float(data.get("ts") or 0)) / 60.0
        return age_min <= ORACLE_TTL_MIN
    except Exception:
        return False


def active_tennis_keys(*, http_get: Any = None) -> List[str]:
    """Chiavi tennis ATTIVE secondo `/v4/sports`. **Endpoint gratuito.**

    Non si tengono in una tabella: i tornei cambiano ogni settimana e una mappa
    scritta a mano sarebbe da rifare (lezione del 24/09 sulle leghe dormienti).
    """
    if http_get is None:
        import requests
        http_get = requests.get
    key = os.getenv("ODDS_API_KEY")
    if not key:
        raise RuntimeError("ODDS_API_KEY assente")
    r = http_get(f"{ODDS_BASE}/sports", params={"apiKey": key}, timeout=20)
    if getattr(r, "status_code", 0) != 200:
        raise RuntimeError(f"GET /sports -> {getattr(r, 'status_code', '?')}")
    out = []
    for s in r.json() or []:
        k = str(s.get("key") or "")
        if k.startswith(TENNIS_KEY_PREFIX) and s.get("active"):
            out.append(k)
    return out


def refresh_oracle(*, http_get: Any = None, now: Optional[float] = None
                   ) -> dict:
    """Aggiorna le cache dell'oracolo tennis. Ritorna un riepilogo, mai eccezioni.

    Costo: 1 credito per torneo la cui cache e' scaduta (`ORACLE_TTL_MIN`).
    L'elenco delle chiavi e' gratuito, quindi il costo dipende dai TORNEI
    ATTIVI, non dai giri. Rispetta il budget giornaliero e l'hard-stop crediti.
    """
    out: dict = {"keys": 0, "fetched": 0, "skipped": 0, "requests": 0,
                 "remaining": None, "error": None, "cached": []}
    if not enabled():
        out["error"] = "corsia spenta (TENNIS_LANE=0)"
        return out
    state, healthy = _load_state()
    state = _roll_day(state)
    try:
        keys = active_tennis_keys(http_get=http_get)
    except Exception as exc:
        out["error"] = f"chiavi tennis non leggibili ({exc})"
        _save_state(state)
        return out
    out["keys"] = len(keys)
    folder = oracle_cache_dir()
    if http_get is None:
        import requests
        http_get = requests.get
    api_key = os.getenv("ODDS_API_KEY")
    changed = False
    for key in keys:
        path = folder / f"toa_{key}.json"
        if _cache_is_fresh(path):
            out["skipped"] += 1
            out["cached"].append(key)
            continue
        if not budget_left(state, healthy=healthy):
            out["skipped"] += 1
            logger.info("tennis_lane: budget OddsPapi esaurito (%d/%d) — cache "
                        "di %s non aggiornata", int(state.get("requests") or 0),
                        REQ_BUDGET_DAY, key)
            continue
        try:
            r = http_get(f"{ODDS_BASE}/sports/{key}/odds",
                         params={"apiKey": api_key, "regions": "eu",
                                 "markets": "h2h", "oddsFormat": "decimal"},
                         timeout=25)
            status = getattr(r, "status_code", 0)
            if status != 200:
                out["error"] = f"{key}: HTTP {status}"
                continue
            payload = r.json() or []
            headers = getattr(r, "headers", {}) or {}
            remaining = headers.get("x-requests-remaining")
            entry = {"ts": time.time(), "payload": payload,
                     "remaining": int(remaining) if str(remaining or "").isdigit()
                     else None,
                     "remaining_ts": time.time(), "source": "tennis_lane"}
            folder.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(entry), encoding="utf-8")
            os.replace(tmp, path)
            state["requests"] = int(state.get("requests") or 0) + 1
            out["fetched"] += 1
            out["requests"] += 1
            out["cached"].append(key)
            if remaining is not None:
                out["remaining"] = remaining
            changed = True
        except Exception as exc:
            out["error"] = f"{key}: {exc}"
            logger.warning("tennis_lane: fetch %s fallita (%s)", key, exc)
    if changed or not healthy:
        _save_state(state)
    out["requests_today"] = int(state.get("requests") or 0)
    return out


# ---------------------------------------------------------------------------
# 2. DISCOVERY SX (pubblica: zero chiavi, zero crediti, zero ordini)
# ---------------------------------------------------------------------------

def _norm(name: Any) -> str:
    try:
        from team_names import normalize
        return normalize(str(name or ""))
    except Exception:                                          # pragma: no cover
        return str(name or "").strip().casefold()


def discover(provider: Any = None) -> List[dict]:
    """Match tennis SX in finestra, coi DUE lati comprabili e i loro prezzi.

    Ritorna `[{market_id, event_id, league_label, kickoff, team_one, team_two,
    sides: [{key, team, price, depth}], depth, inv_sum}]`.

    Fail-safe: qualunque errore -> [].
    """
    try:
        from execution_engine import SxBetProvider
        from sx_signals import _books_parallel, MIN_EXEC_DEPTH_USDC
    except Exception as exc:                                   # pragma: no cover
        logger.warning("tennis_lane: dipendenze non disponibili (%s)", exc)
        return []
    # Memo di discovery (vedi `DISCOVERY_TTL_S`): il prezzo d'ordine viene
    # riletto vivo al momento dell'ordine, quindi un memo breve non puo' far
    # comprare a un prezzo stantio — evita solo di interrogare l'exchange 60
    # volte l'ora per un dato che decide soltanto il gate EV.
    _key = f"{SX_SPORT_ID}|{SX_TYPE_ID}|{HOURS_AHEAD}|{MAX_MARKETS}"
    _now_ts = time.time()
    if (provider is None and _DISCOVERY_MEMO.get("key") == _key
            and _now_ts - float(_DISCOVERY_MEMO.get("ts") or 0)
            < float(DISCOVERY_TTL_S)):
        return [dict(r) for r in _DISCOVERY_MEMO.get("rows") or []]
    prov = provider
    if prov is None:
        try:
            prov = SxBetProvider()
        except Exception as exc:
            logger.warning("tennis_lane: provider SX non avviato (%s)", exc)
            return []
    try:
        markets = prov.list_market_catalogue(
            event_type_ids=(str(SX_SPORT_ID),),
            market_type_ids=(str(SX_TYPE_ID),),
            max_results=int(MAX_MARKETS))
    except Exception as exc:
        logger.warning("tennis_lane: discovery SX fallita (%s)", exc)
        return []
    markets = [m for m in markets or []
               if m.get("line") is None and m.get("market_id")]
    if not markets:
        return []
    now = _now()
    limit = now + timedelta(hours=float(HOURS_AHEAD))
    rows = []
    for m in markets:
        kickoff = None
        try:
            kickoff = datetime.fromisoformat(
                str(m.get("open_date") or "").replace("Z", "+00:00"))
            if kickoff.tzinfo is None:
                kickoff = kickoff.replace(tzinfo=timezone.utc)
        except Exception:
            pass
        if kickoff is None or not (now <= kickoff <= limit):
            continue
        rows.append((m, kickoff))
    if not rows:
        return []
    books = _books_parallel(prov, sorted({str(m["market_id"]) for m, _ in rows}))
    out: List[dict] = []
    for m, kickoff in rows:
        mid = str(m["market_id"])
        t1 = str(m.get("team_one_name") or "").strip()
        t2 = str(m.get("team_two_name") or "").strip()
        if not (t1 and t2):
            continue
        book = books.get(mid) or {}
        sides: List[dict] = []
        # I due lati sono le chiavi 1 e 2 dello STESSO mercato (struttura
        # misurata: non sono due mercati separati come negli eSports).
        for key, team in (("1", t1), ("2", t2)):
            side = book.get(int(key) if str(key).isdigit() else key) or {}
            price = (side.get("best") or {}).get("price")
            depth = float(side.get("depth") or 0.0)
            try:
                price = float(price) if price else None
            except (TypeError, ValueError):
                price = None
            if not price or price <= 1.0:
                continue
            if depth < MIN_EXEC_DEPTH_USDC:
                continue
            sides.append({"key": key, "team": team,
                          "price": round(price, 4), "depth": round(depth, 2)})
        if len(sides) < 2:
            logger.debug("tennis_lane: %s vs %s con meno di due lati eseguibili",
                         t1, t2)
            continue
        prices = {s["key"]: s["price"] for s in sides}
        inv = 1.0 / prices["1"] + 1.0 / prices["2"]
        if not (MIN_INV_SUM <= inv <= MAX_INV_SUM):
            logger.debug("tennis_lane: %s vs %s book sporco (inv_sum %.3f)",
                         t1, t2, inv)
            continue
        out.append({
            "market_id": mid, "event_id": str(m.get("event_id") or ""),
            "league_label": str(m.get("league_label") or ""),
            "kickoff": kickoff, "team_one": t1, "team_two": t2,
            "sides": sides,
            "depth": round(sum(s["depth"] for s in sides), 2),
            "inv_sum": round(inv, 4),
        })
    if provider is None:
        _DISCOVERY_MEMO["key"] = _key
        _DISCOVERY_MEMO["ts"] = _now_ts
        _DISCOVERY_MEMO["rows"] = out
    return out


# ---------------------------------------------------------------------------
# 3. PICKS: oracolo Pinnacle a 2 esiti + gate EV
# ---------------------------------------------------------------------------

def _oracle(team_one: str, team_two: str) -> Optional[dict]:
    """p_true a 2 esiti dalla cache. None = fail-closed (nessun oracolo)."""
    try:
        import pinnacle_oracle as po
        return po.load_oracle(team_one, team_two,
                              cache_dir=oracle_cache_dir(),
                              outcomes=("1", "2"))
    except Exception as exc:
        logger.debug("tennis_lane: oracolo non disponibile (%s)", exc)
        return None


def picks(*, provider: Any = None, http_get: Any = None) -> List[dict]:
    """Candidati tennis +EV. **Sola telemetria**: nessun ordine, mai.

    L'oracolo e' fail-closed: un match senza Pinnacle de-vigato a 2 esiti NON
    produce candidati (senza una verita' di riferimento non c'e' un edge da
    misurare, solo il rumore del modello).
    """
    if not enabled():
        return []
    out: List[dict] = []
    try:
        import pinnacle_oracle as po
        for ev in discover(provider=provider):
            probs = _oracle(ev["team_one"], ev["team_two"])
            if not probs:
                continue
            prices = {s["key"]: float(s["price"]) for s in ev["sides"]}
            for row in po.value_candidates(probs, prices, ev_min=EV_MIN):
                side = next((s for s in ev["sides"]
                             if s["key"] == row["esito"]), None)
                if side is None:
                    continue
                price = float(row["price"])
                # Fascia quota PRIMA di costruire il pick: una quota fuori
                # banda non e' un candidato, quindi non puo' ne' essere
                # registrata nel ledger ne' diventare un ordine. Lo scarto e'
                # loggato (un gate silenzioso e' un bug).
                if not in_odds_band(price):
                    logger.info(
                        "tennis_lane: skip %s vs %s -> %s @ %.2f "
                        "(fuori fascia quota %.2f-%.2f)",
                        ev["team_one"], ev["team_two"], side["team"],
                        price, ODDS_MIN, ODDS_MAX)
                    continue
                p_true = float(row["prob"])
                m_prob = None
                try:
                    fair = po.true_probabilities(prices, min_outcomes=2)
                    if fair:
                        m_prob = fair.get(row["esito"])
                except Exception:
                    m_prob = None
                edge = (p_true - float(m_prob)) if m_prob else None
                out.append({
                    "match_id": f"sx-tennis-{ev['market_id']}",
                    "home": ev["team_one"], "away": ev["team_two"],
                    "commence": ev["kickoff"].isoformat(),
                    "league": ev.get("league_label") or "",
                    "mercato": MARKET, "esito_key": row["esito"],
                    "esito_raw": side["team"], "team": side["team"],
                    "quota": price, "price": price,
                    "market_id": ev["market_id"],
                    "selection_id": int(row["esito"]),
                    "p_true": p_true, "true_odd": row["true_odd"],
                    "best_ev": float(row["ev"]),
                    "market_prob": m_prob, "market_edge": edge,
                    "depth_usdc": side.get("depth"),
                    "inv_sum": ev.get("inv_sum"),
                    "status": _tier(float(row["ev"]), edge),
                    "tennis_lane": True,
                })
                logger.info("tennis_lane: %s vs %s -> %s @ %.2f EV %+.2f%% "
                            "(p_fair %.3f, quota equa %.3f) [telemetria]",
                            ev["team_one"], ev["team_two"], side["team"],
                            price, float(row["ev"]) * 100.0,
                            p_true, row["true_odd"])
    except Exception as exc:
        logger.warning("tennis_lane: corsia non disponibile (%s)", exc)
        return []
    return out


def _tier(ev: float, edge: Optional[float]) -> str:
    """Tier del ledger con le soglie di PRODUZIONE (`value_filter`)."""
    try:
        from value_filter import get_signal_tier
        return get_signal_tier(ev, edge)
    except Exception:                                          # pragma: no cover
        return "value" if ev >= EV_MIN else "moderate"


# ---------------------------------------------------------------------------
# 4. SCAN: telemetria nel ledger (nessun ordine, mai)
# ---------------------------------------------------------------------------

def scan(*, provider: Any = None, http_get: Any = None,
         register: bool = True) -> dict:
    """Misura la corsia e registra i candidati come TELEMETRIA nel ledger.

    La registrazione usa gli stessi ledger del calcio (`matches` +
    `predictions`) con `match_id` `sx-tennis-<hash>`: il prefisso `sx-` e' cio'
    che permette al settlement SX-native esistente di saldarla **gratis**,
    perche' il market hash e' salvato. Cosi' la misura nel tempo e' possibile
    senza una pipeline nuova.
    """
    out: dict = {"enabled": enabled(), "candidates": 0, "registered": 0,
                 "events": 0, "error": None}
    if not enabled():
        return out
    try:
        events = discover(provider=provider)
        out["events"] = len(events)
        cands = picks(provider=provider, http_get=http_get)
        out["candidates"] = len(cands)
        if register and cands:
            from tracker import save_match, save_prediction
            for c in cands:
                try:
                    save_match(c["match_id"], c.get("league") or "Tennis",
                               c["home"], c["away"], c["commence"])
                    save_prediction(
                        c["match_id"], c["mercato"], c["esito_key"],
                        c["quota"], c["p_true"], c["best_ev"],
                        market_prob=c["market_prob"],
                        market_edge=c["market_edge"], status=c["status"],
                        league=c.get("league"))
                    out["registered"] += 1
                except Exception as exc:
                    out["error"] = f"registrazione: {exc}"
                    logger.warning("tennis_lane: telemetria non registrata "
                                   "(%s)", exc)
    except Exception as exc:
        out["error"] = str(exc)
        logger.warning("tennis_lane: scan non disponibile (%s)", exc)
    return out


# ---------------------------------------------------------------------------
# 5. REPORT
# ---------------------------------------------------------------------------

def summary() -> dict:
    """Istantanea della corsia per log/report/CLI. Fail-safe."""
    out: dict = {"enabled": enabled(), "market": MARKET,
                 "sx_sport_id": SX_SPORT_ID, "sx_type_id": SX_TYPE_ID,
                 "ev_min": EV_MIN, "hours_ahead": HOURS_AHEAD,
                 "odds_min": ODDS_MIN, "odds_max": ODDS_MAX,
                 "oracle_ttl_min": ORACLE_TTL_MIN,
                 "request_budget": REQ_BUDGET_DAY,
                 "requests_today": 0, "keys_cached": 0,
                 "candidates": 0, "events": 0}
    try:
        state, healthy = _load_state()
        state = _roll_day(state)
        out["requests_today"] = int(state.get("requests") or 0)
    except Exception:
        healthy = False
    out["budget_state_healthy"] = bool(healthy)
    try:
        folder = oracle_cache_dir()
        out["keys_cached"] = sum(
            1 for p in folder.glob("toa_tennis_*.json")
            if _cache_is_fresh(p))
    except Exception:
        pass
    try:
        out["events"] = len(discover())
    except Exception:
        out["events"] = 0
    try:
        out["candidates"] = len(picks())
    except Exception:
        out["candidates"] = 0
    return out


def format_report(block: Optional[dict] = None) -> str:
    """Report testuale per Telegram/CLI."""
    s = block if isinstance(block, dict) else summary()
    lines = ["🎾 Corsia TENNIS (telemetria, nessun ordine)"]
    if not s.get("enabled"):
        lines.append("  • SPENTA (TENNIS_LANE=0)")
        return "\n".join(lines)
    lines.append(f"  • SX sportId {s.get('sx_sport_id')} / type "
                 f"{s.get('sx_type_id')} | soglia EV {float(s.get('ev_min') or 0) * 100:.1f}%")
    lines.append(f"  • match in palinsesto: {s.get('events')} | candidati +EV: "
                 f"{s.get('candidates')}")
    lines.append(f"  • oracolo: cache fresche {s.get('keys_cached')} tornei "
                 f"(TTL {s.get('oracle_ttl_min')} min) | richieste oggi "
                 f"{s.get('requests_today')}/{s.get('request_budget')}")
    if not s.get("budget_state_healthy"):
        lines.append("  ⚠️ stato del budget illeggibile: nessuna richiesta "
                     "finché non torna leggibile")
    lines.append("  ℹ️ misura del 30/09: edge massimo +0.69% su 55 coppie — "
                 "l'edge tennis e' atteso basso finche' il mercato resta "
                 "allineato a Pinnacle")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Corsia TENNIS in telemetria")
    ap.add_argument("--scan", action="store_true",
                    help="misura e registra la telemetria nel ledger")
    ap.add_argument("--refresh", action="store_true",
                    help="aggiorna le cache dell'oracolo (consuma credito)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    if args.refresh:
        res = refresh_oracle()
        print(json.dumps(res, indent=2, ensure_ascii=False)
              if args.json else
              f"refresh: tornei {res['keys']} | scaricati {res['fetched']} | "
              f"richieste oggi {res.get('requests_today')} | errore {res['error']}")
    if args.scan:
        res = scan()
        print(json.dumps(res, indent=2, ensure_ascii=False)
              if args.json else
              f"scan: match {res['events']} | candidati {res['candidates']} | "
              f"registrati {res['registered']}")
    if args.report or not (args.scan or args.refresh):
        s = summary()
        print(json.dumps(s, indent=2, ensure_ascii=False)
              if args.json else format_report(s))
    return 0


if __name__ == "__main__":                                      # pragma: no cover
    raise SystemExit(main())
