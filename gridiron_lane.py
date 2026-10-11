"""gridiron_lane.py — corsia FOOTBALL USA (NFL / NCAA) su SX Bet (11/10/2026).

PERCHE' ESISTE (direttiva del proprietario)
--------------------------------------------
SX Bet sponsorizza il torneo **Gridiron Gauntlet** (08/10 06:00 -> 20/10 06:00,
verificato su `https://sx.bet/rewards/tournaments`): 58 partecipanti,
**$7.777 divisi fra i top 30**, regola **RETURN betting** — "wins increase your
standing, losses will have no impact". Il torneo parla di football USA (NFL +
NCAA), quindi per partecipare serve una corsia che veda quei mercati.

⚠️ IL FOOTBALL USA NON ERA VISIBILE AL SISTEMA: la discovery del calcio
(`sx_signals`, `multi_market`) e' `sportIds=5`, il tennis `6`, gli eSports `9`,
e `market_shadow` legge lo sport 8 **solo in telemetria** (`market_quotes`,
nessun pick). Questa corsia e' l'analogo di `tennis_lane`/`esports_lane` per lo
sport 8: discovery SX pubblica + oracolo sharp + gate EV + pick ordinabili.

STRUTTURA SX MISURATA (probe reale 11/10/2026, lettura pubblica, zero crediti)

    type 226  "12"                     -> 1 mercato/evento, `outcomeOneName` e
                                          `outcomeTwoName` = le DUE squadre,
                                          nessuna linea. E' il MONEYLINE.
    type 342  "Asian Handicap OT"      -> linea (2.5, ...) dal punto di vista
                                          di teamOne, `mainLine` true/false,
                                          1-4 mercati/evento.
    type 28   "Under/Over OT"          -> linea (49.5, ...), nomi "Over 49.5".

I tre tipi sono NATIVI del contratto 2.0 (`decision.market`: `ML_OT`,
`AH_OT`, `OU_OT`) e i loro lati si leggono con `multi_market.outcome_sides`
(nessuna regola riscritta qui). Su un mercato binario SX i due lati sono le
chiavi del book `1` (= `outcomeOne`) e `2`.

ORACOLO: Pinnacle in regione **EU** (misurato, non ipotizzato)
--------------------------------------------------------------
`/v4/sports` dice `americanfootball_nfl` / `americanfootball_ncaaf` /
`americanfootball_cfl` tutti **active** (endpoint gratuito). Con
`regions=eu` Pinnacle c'e' (**5/5 eventi NFL**, 15/59 NCAA, 1 credito per
chiamata); con `regions=us` Pinnacle c'e' **0 volte** (solo
betmgm/draftkings/fanduel/bovada, che non sono sharp). Il progetto usa
`regions=eu` in tutta la rotazione quote, quindi la scelta "oracolo US" si
realizza cosi': **la regione e' `eu`, lo sport e' americano**. La corsia ha un
budget proprio (`GRIDIRON_REQ_BUDGET_DAY`, default 4 = 2 leghe x 2 giri) e le
cache vivono nella STESSA cartella dell'oracolo calcio
(`toa_americanfootball_*.json`), cosi' `pinnacle_oracle.load_oracle` le legge
senza sapere nulla di quale sport contengano.

SETTLEMENT: GRATIS e NATIVO
---------------------------
`match_id` = `sx-<sportXeventId>` e `market_id` = market hash: sono le DUE
chiavi che `sx_signals._results_from_sx` usa per saldare senza toccare
the-odds-api (zero crediti). Il prefisso `sx-` fa anche si' che le previsioni
di telemetria (registrate da `scan()`) entrino in `_sx_open_matches` e
ricevano il `market_hash` da `market_quotes` — la telemetria ombra
(`market_shadow`, sport 8) scrive le stesse fixture con lo stesso id.

Il `mercato` nel ledger resta quello CANONICO del progetto, cosi' il settle,
i report e le statistiche non cambiano:
    moneyline (226) -> `1X2` con esito `1` / `2`   (vince casa / trasferta)
    spread    (342) -> `AH`  con esito `Home -2.5` / `Away +2.5`
    totale    (28)  -> `OU`  con esito `Over 49.5` / `Under 49.5`
`tracker._prediction_outcome` legge tutti e tre senza una riga di codice nuovo.

⚠️ ECCO PERCHE' SERVE UN FLAG PROPRIO (`gridiron_lane: True` nel pick): il gate
top-down di Pinnacle legge le cache del CALCIO e su un pick NFL risponderebbe
`no_oracle` (fail-closed) uccidendo ogni ordine. Il flag esenta la corsia da
quel gate esattamente come fanno gli eSports (`ML`) e il tennis (`TENNIS`) — e
il gate EV lo applica QUESTA corsia, col proprio oracolo.

⚠️ STAKING: la quota puo' essere piu' alta di quella calcistica (fascia
`GRIDIRON_ODDS_MIN`-`GRIDIRON_ODDS_MAX`, default 1.30-3.00). L'incremento di
stake del torneo return-based e' gestito da `tournament.py` (layer separato):
qui si produce solo un candidato +EV con la sua telemetria.

Fail-safe totale: qualunque errore -> [] / dict con `error`, mai un'eccezione
verso il chiamante. `GRIDIRON_LANE=0` spegne tutto.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configurazione (tutto da env: una soglia e' una decisione, non un dettaglio)
# ---------------------------------------------------------------------------

#: Identita' del mercato football USA su SX Bet: sportId 8 = American Football.
#: (Misurato: 1 = basket, 2 = hockey, 3 = baseball, 5 = calcio, 6 = tennis,
#:  8 = football USA, 9 = eSports. I mapping proposti "NBA=2 / NHL=4" erano
#:  SBAGLIATI: 4 non ha mercati.)
SX_SPORT_ID = os.getenv("GRIDIRON_SX_SPORT_ID", "8")

#: Tipi SX da leggere. Il default e' il SOLO moneyline (226): e' quello che
#: l'oracolo `h2h` copre a 1 credito e che Pinnacle pubblica davvero. Aggiungere
#: "342"/"28" e' possibile da env, ma quei mercati richiedono l'oracolo a LINEA
#: (`h2h,totals,spreads`, 3 crediti a fetch) — oggi `ORACLE_ENABLED=0`.
DEFAULT_TYPES: Tuple[str, ...] = ("226",)

#: Chiavi the-odds-api del football USA (verificate active su /v4/sports).
DEFAULT_SPORTS: Tuple[str, ...] = ("americanfootball_nfl",
                                   "americanfootball_ncaaf")

#: Finestra del palinsesto (le partite NFL/NCAA si giocano in giorni definiti).
HOURS_AHEAD = float(os.getenv("GRIDIRON_HOURS_AHEAD", "48"))
MAX_MARKETS = int(os.getenv("GRIDIRON_MAX_MARKETS", "400"))

#: Soglia EV: UNA definizione per tutta la pipeline (`value_filter.EV_MIN`).
#: Un default copiato qui sarebbe un secondo standard di valore in casa.
def ev_min() -> float:
    try:
        from value_filter import EV_MIN as _EV
        return float(_EV)
    except Exception:                                          # pragma: no cover
        return float(os.getenv("GRIDIRON_EV_MIN", "0.025"))


#: Fascia quota della corsia. Piu' larga della calcistica: su un moneyline NFL
#: il favorito sta spesso a 1.30-1.60 e lo sfavorito di un match equilibrato
#: a 2.00-3.00. Sopra il tetto non c'e' un edge, c'e' varianza (stessa lezione
#: del 02/10 sul tennis: 5 delle 6 sconfitte a quota >= 2.42).
ODDS_MIN = float(os.getenv("GRIDIRON_ODDS_MIN", "1.30"))
ODDS_MAX = float(os.getenv("GRIDIRON_ODDS_MAX", "3.00"))


def in_odds_band(price: Any) -> bool:
    """True se la quota e' nella fascia giocabile della corsia gridiron.

    UNICO punto di verita' della fascia: la corsia ordini la riusa come difesa
    in profondita' (`auto_bet._gridiron_picks`). Fail-closed: una quota non
    numerica (o assente) NON e' nella banda.
    """
    try:
        value = float(price)
    except (TypeError, ValueError):
        return False
    return ODDS_MIN <= value <= ODDS_MAX


#: Coerenza del mercato: 1/prezzo_1 + 1/prezzo_2 su un exchange ~1. Fuori banda
#: il book e' sporco e l'EV finto e' un artefatto aritmetico.
MIN_INV_SUM = float(os.getenv("GRIDIRON_MIN_INV_SUM", "0.98"))
MAX_INV_SUM = float(os.getenv("GRIDIRON_MAX_INV_SUM", "1.08"))

#: TTL delle cache dell'oracolo e budget giornaliero della corsia.
ORACLE_TTL_MIN = float(os.getenv("GRIDIRON_ORACLE_TTL_MIN", "720"))
REQ_BUDGET_DAY = int(os.getenv("GRIDIRON_REQ_BUDGET_DAY", "4"))

#: TTL della DISCOVERY SX (order book). Il giro ordini gira ogni 60s: senza memo
#: sarebbero centinaia di richieste/ora sull'API pubblica per un prezzo usato
#: SOLO dal gate EV (il prezzo d'ordine viene riletto vivo al momento dell'ordine).
DISCOVERY_TTL_S = float(os.getenv("GRIDIRON_DISCOVERY_TTL_S", "300"))

#: Radice delle API della the-odds-api (separabile per i test).
ODDS_BASE = os.getenv("GRIDIRON_ODDS_BASE", "https://api.the-odds-api.com/v4")

#: Marcatore del pick: dice al resto della pipeline "questa e' la corsia
#: football USA". Serve a (a) esentarla dal gate top-down del CALCIO, (b) non
#: farla entrare nell'harvesting dell'oracolo a linea, (c) far usare a
#: `_live_fill` l'identita' di mercato GIA' presente nel pick.
PICK_FLAG = "gridiron_lane"

_DISCOVERY_MEMO: Dict[str, Any] = {"key": None, "ts": 0.0, "rows": []}


def reset_cache() -> None:
    """Azzera la memo di discovery (test/cambio finestra)."""
    _DISCOVERY_MEMO["key"] = None
    _DISCOVERY_MEMO["ts"] = 0.0
    _DISCOVERY_MEMO["rows"] = []


def _num_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return float(default)


def enabled() -> bool:
    """Interruttore della corsia (`GRIDIRON_LANE=0` per spegnerla)."""
    raw = os.getenv("GRIDIRON_LANE")
    if raw is None or raw == "":
        return True
    return str(raw).strip().lower() not in ("0", "false", "no", "off")


def _types() -> Tuple[str, ...]:
    raw = os.getenv("GRIDIRON_TYPES")
    if raw is None or not str(raw).strip():
        return DEFAULT_TYPES
    return tuple(p.strip() for p in str(raw).split(",") if p.strip())


def _sports() -> Tuple[str, ...]:
    raw = os.getenv("GRIDIRON_SPORTS")
    if raw is None or not str(raw).strip():
        return DEFAULT_SPORTS
    return tuple(p.strip() for p in str(raw).split(",") if p.strip())


def _now() -> datetime:
    return datetime.now(timezone.utc)


def oracle_cache_dir() -> Path:
    """Cartella delle cache dell'oracolo: QUELLA del calcio per default.

    Non e' pigrizia: `pinnacle_oracle.load_oracle` legge li' senza sapere nulla
    dello sport. Una cartella separata obbligherebbe a passarla a ogni chiamata
    e a ricordarsene in ogni percorso futuro.
    """
    raw = os.getenv("GRIDIRON_ORACLE_CACHE")
    if raw:
        return Path(raw)
    try:
        from config import DATA_DIR
        return Path(DATA_DIR)
    except Exception:                                          # pragma: no cover
        return Path("data")


def state_path() -> Path:
    raw = os.getenv("GRIDIRON_LANE_STATE")
    if raw:
        return Path(raw)
    return oracle_cache_dir() / "gridiron_lane" / "state.json"


# ---------------------------------------------------------------------------
# Stato (budget giornaliero) — scrittura atomica, lettura fail-safe
# ---------------------------------------------------------------------------

def _empty_state() -> dict:
    return {"day": _now().strftime("%Y-%m-%d"), "requests": 0}


def _load_state() -> Tuple[dict, bool]:
    """(stato, sano). Un file corrotto NON fa fallire la corsia: si riparte."""
    path = state_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return _empty_state(), False
        return data, True
    except FileNotFoundError:
        return _empty_state(), True
    except Exception as exc:
        logger.warning("gridiron_lane: stato illeggibile (%s) — riparto da zero",
                       exc)
        return _empty_state(), False


def _save_state(state: dict) -> None:
    path = state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except Exception as exc:
        logger.warning("gridiron_lane: stato non salvato (%s)", exc)


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
            logger.info("gridiron_lane: hard-stop crediti attivo — nessuna "
                        "richiesta")
            return False
    except Exception:
        pass
    return True


# ---------------------------------------------------------------------------
# 1. ORACOLO: quote Pinnacle NFL/NCAA (1 credito/lega)
# ---------------------------------------------------------------------------

def _cache_is_fresh(path: Path) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        age_min = (time.time() - float(data.get("ts") or 0)) / 60.0
        return age_min <= ORACLE_TTL_MIN
    except Exception:
        return False


def refresh_oracle(*, http_get: Any = None) -> dict:
    """Aggiorna le cache dell'oracolo football USA. Mai eccezioni.

    Costo: **1 credito per lega** la cui cache e' scaduta (`ORACLE_TTL_MIN`).
    Le chiavi sono fisse e verificate (`americanfootball_nfl`, `..._ncaaf`),
    quindi non si paga nulla per elencarle. Rispetta il budget giornaliero
    della corsia e l'hard-stop crediti globale (dentro `budget_left`, l'UNICA
    definizione della regola) e registra ogni spesa in `credit_calls.jsonl`.

    `regions=eu` e' la regione dell'ORACOLO (misurata: e' l'unica dove Pinnacle
    pubblica il football USA). Con `regions=us` il payload contiene solo book
    ricreativi e `load_oracle` darebbe l'oracolo assente.
    """
    out: dict = {"keys": 0, "fetched": 0, "skipped": 0, "requests": 0,
                 "remaining": None, "error": None, "cached": [], "sports": []}
    if not enabled():
        out["error"] = "corsia spenta (GRIDIRON_LANE=0)"
        return out
    state, healthy = _load_state()
    state = _roll_day(state)
    sports = list(_sports())
    out["keys"] = len(sports)
    out["sports"] = sports
    folder = oracle_cache_dir()
    if http_get is None:
        import requests
        http_get = requests.get
    api_key = os.getenv("ODDS_API_KEY")
    if not api_key:
        out["error"] = "ODDS_API_KEY assente"
        _save_state(state)
        return out
    changed = False
    for key in sports:
        path = folder / f"toa_{key}.json"
        if _cache_is_fresh(path):
            out["skipped"] += 1
            out["cached"].append(key)
            continue
        if not budget_left(state, healthy=healthy):
            out["skipped"] += 1
            logger.info("gridiron_lane: budget the-odds-api esaurito (%d/%d) — "
                        "cache di %s non aggiornata",
                        int(state.get("requests") or 0), REQ_BUDGET_DAY, key)
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
                     "remaining_ts": time.time(), "source": PICK_FLAG}
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
            _log_credit_call(key, remaining, r)
            changed = True
        except Exception as exc:
            out["error"] = f"{key}: {exc}"
            logger.warning("gridiron_lane: fetch %s fallita (%s)", key, exc)
    if changed or not healthy:
        _save_state(state)
    out["requests_today"] = int(state.get("requests") or 0)
    return out


def _log_credit_call(sport: str, remaining: Any, resp: Any) -> None:
    """Registra la chiamata PAGATA nella telemetria crediti (fail-safe).

    Delega a `tennis_lane._log_credit_call`: la riga di `credit_calls.jsonl`
    ha UNA sola forma (source `oracle`, campo `sport` con la lega), cosi'
    `credit_diagnose` attribuisce il costo senza una terza variante.
    """
    try:
        from tennis_lane import _log_credit_call as _log
        _log(sport, remaining, resp)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 2. DISCOVERY SX (pubblica: zero chiavi, zero crediti, zero ordini)
# ---------------------------------------------------------------------------

def discover(provider: Any = None) -> List[dict]:
    """Mercati football USA SX in finestra, coi due lati e i loro prezzi.

    Ritorna una voce per MERCATO:
    `{market_id, event_id, market_type, line, home, away, league_label,
    kickoff, sides:[{key,label,price,depth}], depth, inv_sum}`.

    Fail-safe: qualunque errore -> [].
    """
    if not enabled():
        return []
    try:
        from execution_engine import SxBetProvider
        from sx_signals import _books_parallel, _kickoff_utc_ms, MIN_EXEC_DEPTH_USDC
        from multi_market import _discover_type, _market_line
        from decision.market import SX_TYPE_IDS, spec_for
    except Exception as exc:                                   # pragma: no cover
        logger.warning("gridiron_lane: dipendenze non disponibili (%s)", exc)
        return []
    wanted = _types()
    _key = f"{SX_SPORT_ID}|{','.join(wanted)}|{HOURS_AHEAD}|{MAX_MARKETS}"
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
            logger.warning("gridiron_lane: provider SX non avviato (%s)", exc)
            return []
    now = _now()
    now_ms = int(now.timestamp() * 1000)
    lo = now_ms - 60 * 60 * 1000                    # -1h: live appena iniziati
    hi = now_ms + int(HOURS_AHEAD * 3600 * 1000)
    per_type = max(1, int(MAX_MARKETS) // max(1, len(wanted)))
    raw: List[Tuple[str, dict]] = []
    for type_id in wanted:
        try:
            markets = _discover_type(prov, str(type_id), per_type,
                                     sport_ids=str(SX_SPORT_ID))
        except Exception as exc:
            logger.warning("gridiron_lane: discovery type %s fallita (%s)",
                           type_id, exc)
            continue
        for m in markets or []:
            if isinstance(m, dict):
                raw.append((str(type_id), m))
    if not raw:
        return []
    # Order book: una richiesta per mercato (le stesse chiavi 1/2 del calcio).
    hashes = sorted({str(m.get("marketHash")) for _, m in raw
                     if m.get("marketHash")})
    books = _books_parallel(prov, hashes)
    out: List[dict] = []
    for type_id, m in raw:
        market_hash = m.get("marketHash")
        event_id = m.get("sportXeventId")
        ko_ms = _kickoff_utc_ms(m.get("gameTime"))
        home = str(m.get("teamOneName") or "").strip()
        away = str(m.get("teamTwoName") or "").strip()
        if not (market_hash and event_id and home and away) or ko_ms is None \
                or not (lo <= ko_ms <= hi):
            continue
        try:
            market = SX_TYPE_IDS.get(int(type_id))
        except (TypeError, ValueError):
            market = None
        if market is None:
            continue
        spec = spec_for(market)
        has_lines = bool(spec and spec.has_lines)
        line = _market_line(m) if has_lines else None
        if has_lines and line is None:
            continue
        # Lati: su un mercato binario SX sono le chiavi del book 1 (= outcomeOne)
        # e 2. L'etichetta di ledger la decide il tipo (moneyline/spread/OU).
        labels = _side_labels(type_id, line, m.get("outcomeOneName"))
        if labels is None:
            continue
        book = books.get(str(market_hash)) or {}
        sides: List[dict] = []
        for idx in (1, 2):
            info = book.get(idx) or {}
            best = info.get("best") or {}
            try:
                price = float(best.get("price") or 0.0)
            except (TypeError, ValueError):
                price = 0.0
            depth = float(info.get("depth") or 0.0)
            if price <= 1.0 or depth < MIN_EXEC_DEPTH_USDC:
                continue
            sides.append({"key": str(idx), "label": labels[idx - 1],
                          "price": round(price, 4), "depth": round(depth, 2)})
        if len(sides) < 2:
            logger.debug("gridiron_lane: %s vs %s con meno di due lati "
                         "eseguibili", home, away)
            continue
        prices = {s["key"]: s["price"] for s in sides}
        inv = 1.0 / prices["1"] + 1.0 / prices["2"]
        if not (MIN_INV_SUM <= inv <= MAX_INV_SUM):
            logger.debug("gridiron_lane: %s vs %s book sporco (inv_sum %.3f)",
                         home, away, inv)
            continue
        out.append({
            "market_id": str(market_hash), "event_id": str(event_id),
            "market_type": market.value, "sx_type": type_id, "line": line,
            "home": home, "away": away,
            "league_label": str(m.get("leagueLabel") or "").strip(),
            "kickoff_ms": ko_ms,
            "kickoff": datetime.fromtimestamp(ko_ms / 1000, timezone.utc),
            "sides": sides,
            "depth": round(sum(s["depth"] for s in sides), 2),
            "inv_sum": round(inv, 4),
        })
    if provider is None:
        _DISCOVERY_MEMO["key"] = _key
        _DISCOVERY_MEMO["ts"] = _now_ts
        _DISCOVERY_MEMO["rows"] = out
    return out


def _side_labels(type_id: str, line: Optional[float],
                 outcome_one: Any) -> Optional[Tuple[str, str]]:
    """Etichette di ledger dei due lati (side 1 = outcomeOne).

    Ritorna `(label_1, label_2)` oppure None se il tipo non e' riconoscibile.
    Gli esiti restano nelle convenzioni GIA' lette dal settlement
    (`tracker._prediction_outcome`): `1`/`2` per il 1X2, `Home ±x`/`Away ±x`
    per l'AH, `Over x`/`Under x` per i totali.
    """
    t = str(type_id)
    if t == "226":
        return ("1", "2")
    if t == "342":
        if line is None:
            return None
        return (f"Home {line:+g}", f"Away {-line:+g}")
    if t == "28":
        if line is None:
            return None
        first = str(outcome_one or "").strip().lower()
        if first.startswith("under"):
            return (f"Under {line:g}", f"Over {line:g}")
        return (f"Over {line:g}", f"Under {line:g}")
    return None


# ---------------------------------------------------------------------------
# 3. PICKS: oracolo Pinnacle a 2 esiti + gate EV
# ---------------------------------------------------------------------------

def _oracle(home: str, away: str) -> Optional[dict]:
    """p_true a 2 esiti dalla cache. None = fail-closed (nessun oracolo)."""
    try:
        import pinnacle_oracle as po
        return po.load_oracle(home, away, cache_dir=oracle_cache_dir(),
                              outcomes=("1", "2"))
    except Exception as exc:
        logger.debug("gridiron_lane: oracolo non disponibile (%s)", exc)
        return None


def picks(*, provider: Any = None) -> List[dict]:
    """Candidati football USA +EV, pronti per il percorso ordini.

    L'oracolo e' fail-closed: un match senza Pinnacle de-vigato a 2 esiti NON
    produce candidati. L'EV e' calcolato sul MONEYLINE (il mercato a 2 esiti che
    l'oracolo `h2h` copre a 1 credito); gli eventuali spread/totali con linea
    restano fuori finche' l'oracolo a linea non e' attivo.

    Il pick porta `market_id` + `selection_id` (l'ordine NON deve ri-risolvere
    il mercato: la struttura football e' a due esiti con chiavi 1/2), il flag
    `gridiron_lane` e gli esiti di ledger che il settlement sa leggere.
    """
    if not enabled():
        return []
    out: List[dict] = []
    threshold = ev_min()
    try:
        import pinnacle_oracle as po
        for ev in discover(provider=provider):
            # Solo i mercati testa-a-testa senza linea: l'oracolo a 2 esiti
            # copre il moneyline, non uno spread (che vive in `spreads`).
            if ev.get("sx_type") != "226":
                continue
            probs = _oracle(ev["home"], ev["away"])
            if not probs:
                logger.debug("gridiron_lane: oracolo assente per %s vs %s "
                             "(fail-closed)", ev["home"], ev["away"])
                continue
            prices = {s["key"]: float(s["price"]) for s in ev["sides"]}
            labels = {s["key"]: s["label"] for s in ev["sides"]}
            for row in po.value_candidates(probs, prices, ev_min=threshold):
                side = next((s for s in ev["sides"]
                             if s["key"] == row["esito"]), None)
                if side is None:
                    continue
                price = float(row["price"])
                if not in_odds_band(price):
                    logger.info(
                        "gridiron_lane: skip %s vs %s -> %s @ %.2f (fuori "
                        "fascia quota %.2f-%.2f)", ev["home"], ev["away"],
                        labels.get(row["esito"]), price, ODDS_MIN, ODDS_MAX)
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
                    "match_id": f"sx-{ev['event_id']}",
                    "home": ev["home"], "away": ev["away"],
                    "commence": ev["kickoff"].isoformat(),
                    "league": ev.get("league_label") or "",
                    "mercato": "1X2", "esito_key": row["esito"],
                    "esito_raw": labels.get(row["esito"]),
                    "team": labels.get(row["esito"]),
                    "quota": price, "price": price,
                    "market_id": ev["market_id"],
                    "selection_id": int(row["esito"]),
                    "p_true": p_true, "true_odd": row["true_odd"],
                    "best_ev": float(row["ev"]),
                    "market_prob": m_prob, "market_edge": edge,
                    "depth_usdc": side.get("depth"),
                    "inv_sum": ev.get("inv_sum"),
                    "status": _tier(float(row["ev"]), edge),
                    PICK_FLAG: True,
                })
                logger.info("gridiron_lane: %s vs %s -> %s @ %.2f EV %+.2f%% "
                            "(p_fair %.3f, quota equa %.3f)",
                            ev["home"], ev["away"], labels.get(row["esito"]),
                            price, float(row["ev"]) * 100.0, p_true,
                            row["true_odd"])
    except Exception as exc:
        logger.warning("gridiron_lane: corsia non disponibile (%s)", exc)
        return []
    return out


def _tier(ev: float, edge: Optional[float]) -> str:
    """Tier del ledger con le soglie di PRODUZIONE (`value_filter`)."""
    try:
        from value_filter import get_signal_tier
        return get_signal_tier(ev, edge)
    except Exception:                                          # pragma: no cover
        return "value" if ev >= ev_min() else "moderate"


# ---------------------------------------------------------------------------
# 4. SCAN: telemetria nel ledger (nessun ordine)
# ---------------------------------------------------------------------------

def scan(*, provider: Any = None, register: bool = True) -> dict:
    """Misura la corsia e registra i candidati come TELEMETRIA nel ledger.

    La registrazione usa gli stessi ledger del calcio (`matches` +
    `predictions`) con `match_id` `sx-<eventId>`: e' la chiave che permette al
    settlement SX-native di saldare GRATIS sia le puntate (market_id) sia le
    sole previsioni (market_hash letto da `market_quotes`, dove la telemetria
    ombra scrive le stesse fixture). Cosi' la misura nel tempo e' possibile
    senza una pipeline nuova.
    """
    out: dict = {"enabled": enabled(), "events": 0, "candidates": 0,
                 "registered": 0, "error": None}
    if not enabled():
        return out
    try:
        events = discover(provider=provider)
        out["events"] = len(events)
        cands = picks(provider=provider)
        out["candidates"] = len(cands)
        if register and cands:
            from tracker import save_match, save_prediction
            for c in cands:
                try:
                    save_match(c["match_id"], c.get("league") or "Football USA",
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
                    logger.warning("gridiron_lane: telemetria non registrata "
                                   "(%s)", exc)
    except Exception as exc:
        out["error"] = str(exc)
        logger.warning("gridiron_lane: scan non disponibile (%s)", exc)
    return out


# ---------------------------------------------------------------------------
# 5. REPORT
# ---------------------------------------------------------------------------

def summary() -> dict:
    """Istantanea della corsia per log/report/CLI. Fail-safe."""
    out: dict = {"enabled": enabled(), "sx_sport_id": SX_SPORT_ID,
                 "types": list(_types()), "sports": list(_sports()),
                 "ev_min": ev_min(), "hours_ahead": HOURS_AHEAD,
                 "odds_min": ODDS_MIN, "odds_max": ODDS_MAX,
                 "oracle_ttl_min": ORACLE_TTL_MIN,
                 "request_budget": REQ_BUDGET_DAY,
                 "requests_today": 0, "keys_cached": 0,
                 "events": 0, "candidates": 0}
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
            1 for p in folder.glob("toa_americanfootball_*.json")
            if _cache_is_fresh(p))
    except Exception:
        pass
    return out


def format_report(block: Optional[dict] = None) -> str:
    """Report testuale per Telegram/CLI."""
    s = block if isinstance(block, dict) else summary()
    lines = ["🏈 Corsia FOOTBALL USA (NFL/NCAA su SX Bet)"]
    if not s.get("enabled"):
        lines.append("  • SPENTA (GRIDIRON_LANE=0)")
        return "\n".join(lines)
    lines.append(f"  • SX sportId {s.get('sx_sport_id')} | tipi "
                 f"{', '.join(s.get('types') or []) or '-'} | soglia EV "
                 f"{float(s.get('ev_min') or 0) * 100:.1f}%")
    lines.append(f"  • fascia quota {s.get('odds_min')}-{s.get('odds_max')} | "
                 f"finestra {s.get('hours_ahead')}h")
    lines.append(f"  • oracolo: cache fresche {s.get('keys_cached')} leghe "
                 f"(TTL {s.get('oracle_ttl_min')} min) | richieste oggi "
                 f"{s.get('requests_today')}/{s.get('request_budget')}")
    if not s.get("budget_state_healthy"):
        lines.append("  ⚠️ stato del budget illeggibile: nessuna richiesta "
                     "finche' non torna leggibile")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Corsia football USA (NFL/NCAA)")
    ap.add_argument("--scan", action="store_true",
                    help="misura e registra la telemetria nel ledger")
    ap.add_argument("--refresh", action="store_true",
                    help="aggiorna le cache dell'oracolo (consuma credito)")
    ap.add_argument("--picks", action="store_true",
                    help="elenca i candidati +EV correnti")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    if args.refresh:
        res = refresh_oracle()
        print(json.dumps(res, indent=2, ensure_ascii=False) if args.json else
              f"refresh: leghe {res['keys']} | scaricate {res['fetched']} | "
              f"richieste oggi {res.get('requests_today')} | errore "
              f"{res['error']}")
    if args.scan:
        res = scan()
        print(json.dumps(res, indent=2, ensure_ascii=False) if args.json else
              f"scan: match {res['events']} | candidati {res['candidates']} | "
              f"registrati {res['registered']}")
    if args.picks:
        rows = picks()
        print(json.dumps(rows, indent=2, ensure_ascii=False, default=str)
              if args.json else
              f"candidati: {len(rows)}\n" + "\n".join(
                  f"  {r['home']} vs {r['away']} -> {r['esito_raw']} @ "
                  f"{r['quota']:.2f} EV {r['best_ev'] * 100:+.2f}%"
                  for r in rows))
    if args.report or not (args.scan or args.refresh or args.picks):
        s = summary()
        print(json.dumps(s, indent=2, ensure_ascii=False) if args.json
              else format_report(s))
    return 0


if __name__ == "__main__":                                      # pragma: no cover
    raise SystemExit(main())
