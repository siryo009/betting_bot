"""Corsia eSports: discovery SX Bet (sport 9, type 52) + oracolo OddsPapi.

Perche' esiste (30/09/2026). Il percorso calcio non vede gli eSports: la
discovery di `sx_signals` (`sportIds=5, type=1`) e quella di `multi_market`
(`sportIds=5`) sono *solo* calcio, quindi nessun mercato eSports entra nel
ledger e nessun candidato arriva all'esecuzione. Aggiungere un filtro non
bastava: senza una discovery dedicata non c'e' nulla da filtrare.

Questa corsia chiude il giro in QUATTRO passi, tutti **gratuiti tranne
l'oracolo**:

1. DISCOVERY (SX Bet, lettura pubblica, zero chiavi, zero crediti):
   i mercati binari type 52 (`Moneyline`, 2 vie senza pareggio) dello sport 9.
   Su SX un mercato binario e' "X vs Not X": `outcomeOne` E' il lato che si
   compra con selection 1, e per una partita 2 vie esistono DUE mercati (uno
   per squadra). E' la stessa semantica del 1X2 spezzato in 3 mercati.
2. ORACOLO (OddsPapi, l'unica voce a pagamento): la fixture corrispondente
   (`esports_oracle.match_fixture`) e le probabilita' fair de-vigate di
   Pinnacle (`esports_oracle.oracle_for_fixture`). **the-odds-api NON ha
   eSports**: senza un oracolo esterno il gate top-down risponderebbe
   `no_oracle` e l'intera classe resterebbe non giocabile.
3. EV GATE: `esports_oracle.candidate_for` — lo STESSO gate del calcio
   (`pinnacle_oracle.ev_gate`, soglia `value_filter.EV_MIN` importata).
4. PICK: un dict nella forma attesa da `auto_bet.run_today_bets`, con
   `mercato="ML"`, `esito_key` 1|2 e `team` = il lato da comprare. L'ordine lo
   risolve `execution_engine.resolve_moneyline_market` (sport 9, type 52).

**Budget: l'oracolo e' la risorsa scarsa.** Il piano free sono 250 richieste
AL MESE: il giro di `auto_bet` gira ogni 60 secondi, quindi senza freni la
quota si esaurirebbe in poche ore (misurato il 29/09: 429 a raffica, e la
429 e' un RATE LIMIT, non una quota esaurita). Tre difese, tutte
dichiarate in `summary()`:

- **cache su disco** con TTL: fixtures per titolo, odds per fixture. Gli
  esiti NEGATIVI (`no_oracle`) hanno un TTL CORTO e separato, perche' su
  eSports Pinnacle pubblica tardivo: un "non ancora" va ritentato, non
  congelato per ore come una probabilita' vera. **15 minuti** (30/09/2026),
  tarati sulla finestra ordini T-120..T-15: danno quattro tentativi utili per
  evento (T-60, T-45, T-30, T-15) invece di due;
- **tetto giornaliero** (`ESPORTS_REQ_BUDGET_DAY`, default 8 = 240/mese, il
  piano free ne ha 250). Le richieste si contano PRIMA di farle: oltre il
  tetto la corsia si ferma e lo dichiara, non prova e fallisce. Con 4 richieste
  per evento il tetto copre **~2 eventi al giorno**;
- **priorita' agli eventi PIU' VICINI**: il budget e' scarso, quindi gli
  eventi si processano in ordine di kickoff crescente. Un evento lontano che
  consuma le richieste toglierebbe la copertura a quello che sta per iniziare;
- **finestra dell'oracolo** (`ESPORTS_ORACLE_WINDOW_H`, default 1h): a
  pagamento si va solo per gli eventi VICINI al fischio d'inizio. Discovery e
  fascia quota restano a 24h perche' sono gratuite, ma interrogare Pinnacle
  ore prima significa pagare una richiesta per un "non ancora" — e su eSports
  lo sharp pubblica tardivo, quindi la finestra larga produce solo costi;
- **pacing** (`ESPORTS_MIN_INTERVAL_S`, default 2.5s): il free tier limita le
  chiamate al MINUTO. Misurato in produzione il 30/09: senza distanziamento la
  corsia prende **HTTP 429** e la richiesta e' persa (quota pagata, zero dati).
- **fail-closed sul contatore corrotto**: un file di stato illeggibile NON
  autorizza una raffica — il ciclo si considera a budget esaurito e lo stato
  viene ricostruito (cosi' si auto-ripara al giro dopo).

**Sola lettura, nessun ordine**: questo modulo non scrive sul ledger
(niente `matches`/`predictions`/`bets`), non chiama il provider d'ordine e non
tocca `auto_bet`. Restituisce pick; chi li esegue sono i guardrail esistenti
(T-60, stake fisso, recinto 40%, liquidita', dedup).

**Settlement**: le bet eSports vivono nel percorso **SX-native** a costo zero
(`sx_signals._results_from_sx` legge `markets/find` sul `market_id` salvato
sulla bet, senza riga in `matches` e senza the-odds-api). Per questo il
`match_id` ha il prefisso `sx-` e il pick porta con se' `market_id` e
`selection_id`: senza quelli la riga non sarebbe saldabile e il capitale
resterebbe immobilizzato fino alla scadenza automatica.

⚠️ **Fascia quota e gate di lega: due cose diverse.** La FASCIA (1.30-1.80,
letta da `value_filter`, override `ESPORTS_ODDS_MIN/MAX`) si applica come nel
calcio. Il GATE DI LEGA no: `value_filter.
league_allowed` misura i campionati di calcio con un ROI storico: gli eSports
sono un'altra classe di rischio, senza misura, e le etichette SX
(`League of Legends`, `CBLOL`...) non sono nel set ammesso. Applicarlo
significherebbe che la corsia non puo' giocare nulla per definizione. La
prudenza qui sta altrove: fascia quota di produzione, profondita' minima,
oracolo sharp de-vigato e stake fisso.

⚠️ **Pinnacle pubblica tardivo sugli eSports.** Nelle misure del 29/09 ogni
fixture senza prezzi sharp ha dato `None` (fail-closed, nessun verdetto invece
di un verdetto su un book ricreativo). Aspettativa realistica: la corsia resta
inert finche' lo sharp non quota. Lo stato si legge da `summary()`, che conta
anche i `no_oracle`: "corsia accesa che non trova nulla" e "corsia rotta" non
devono essere indistinguibili.

CLI (diagnostica: letture dichiarate, nessun ordine, nessuna scrittura):
    venv/bin/python esports_lane.py [--scan] [--json] [--report]
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from config import DATA_DIR

logger = logging.getLogger(__name__)

# --- Identita' SX dei mercati eSports (misurata il 29/09/2026) --------------
#: sportId 9 = eSports; type 52 = moneyline 2 vie senza pareggio (23 mercati
#: attivi al probe, `liveEnabled: false` su tutti => pre-match).
SX_SPORT_ID = os.getenv("ESPORTS_SX_SPORT_ID", "9")
SX_TYPE_ID = os.getenv("ESPORTS_SX_TYPE_ID", "52")

# --- Finestra e volumi -----------------------------------------------------
HOURS_AHEAD = float(os.getenv("ESPORTS_HOURS_AHEAD", "24"))
MAX_EVENTS = int(os.getenv("ESPORTS_MAX_EVENTS", "20"))
MAX_MARKETS = int(os.getenv("ESPORTS_MAX_MARKETS", "120"))

# --- Budget OddsPapi (piano free: 250 richieste/mese) ----------------------
#: Default dichiarati (letti a RUNTIME dalle funzioni sotto: parametri
#: operativi come questi si tarano senza redeploy, e un valore letto all'import
#: non sarebbe ne' tarabile ne' testabile).
ORACLE_WINDOW_H_DEFAULT = 1.0
MIN_INTERVAL_S_DEFAULT = 2.5
REQ_BUDGET_DAY = int(os.getenv("ESPORTS_REQ_BUDGET_DAY", "8"))
FIXTURES_TTL_MIN = float(os.getenv("ESPORTS_FIXTURES_TTL_MIN", "720"))
ODDS_TTL_MIN = float(os.getenv("ESPORTS_ODDS_TTL_MIN", "360"))
#: TTL di un esito NEGATIVO (`no_oracle`). 15 minuti (era 60) — direttiva
#: 30/09/2026, dopo la misura sul campo: Pinnacle pubblica TARDIVO e la
#: finestra ordini e' T-120..T-15, quindi con TTL 60min i ritentativi erano
#: DUE (T-60, T-0) e un drop pubblicato a T-45 non veniva mai visto. Con 15min
#: i tentativi utili diventano QUATTRO (T-60, T-45, T-30, T-15) e ogni evento
#: costa 4 richieste -> il tetto di 8/giorno copre ~2 eventi.
#: ⚠️ TTL e finestra sono una COPPIA: allungare l'uno senza l'altro o brucia
#: quota (ritentativi fuori dalla finestra ordini) o perde i drop tardivi.
ODDS_MISS_TTL_MIN = float(os.getenv("ESPORTS_ODDS_MISS_TTL_MIN", "15"))

CACHE_PATH = Path(os.getenv(
    "ESPORTS_CACHE", str(DATA_DIR / "esports" / "lane_state.json")))

#: Etichetta di mercato/ledger della corsia. `ML` = moneyline 2 vie: NON e'
#: un mercato del calcio (1X2/OU/AH) e non deve confondersi con loro.
MARKET = "ML"


def enabled() -> bool:
    """Interruttore della corsia (`ESPORTS_LIVE=0` per spegnerla)."""
    return os.getenv("ESPORTS_LIVE", "1").strip().lower() not in (
        "0", "false", "off", "no")


def _num_env(name: str, default: float) -> float:
    """Numero da env, letto a RUNTIME. Valore impossibile -> default."""
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        logger.warning("esports_lane: %s non numerico, uso %s", name, default)
        return float(default)


def oracle_window_h() -> float:
    """Ore dal kickoff entro cui si interroga l'oracolo (a pagamento).

    Su eSports Pinnacle pubblica TARDIVO: chiedere le sue quote 4 ore prima
    significa spendere una richiesta per un "non ancora" e poi ripagarla col
    TTL corto. Discovery e fascia quota restano a 24h (sono gratis); a
    pagamento si va solo dove il verdetto e' possibile.
    """
    return max(0.0, _num_env("ESPORTS_ORACLE_WINDOW_H", ORACLE_WINDOW_H_DEFAULT))


def min_interval_s() -> float:
    """Distanza minima fra due richieste OddsPapi (secondi).

    Il piano free limita le chiamate al MINUTO: misurato il 29/09 e di nuovo
    in produzione il 30/09, due chiamate ravvicinate prendono 429 e la
    richiesta e' persa (quota pagata, zero dati).
    """
    return max(0.0, _num_env("ESPORTS_MIN_INTERVAL_S", MIN_INTERVAL_S_DEFAULT))


# ---------------------------------------------------------------------------
# Stato su disco: cache + contatore richieste (una sola scrittura atomica)
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _empty_state() -> dict:
    return {"day": _now().strftime("%Y-%m-%d"), "requests": 0,
            "fixtures": {}, "odds": {}}


def _load_state() -> Tuple[dict, bool]:
    """Stato di cache/budget. Ritorna (stato, sano).

    FAIL-CLOSED sul contatore: un file illeggibile NON autorizza una raffica
    di richieste. `sano=False` significa "budget esaurito per questo ciclo" e
    lo stato viene riscritto pulito, cosi' il giro successivo riparte.
    """
    try:
        raw = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("stato non e' un oggetto")
        for key in ("fixtures", "odds"):
            if not isinstance(raw.get(key), dict):
                raw[key] = {}
        raw["requests"] = int(raw.get("requests") or 0)
        if not raw.get("day"):
            raw["day"] = _now().strftime("%Y-%m-%d")
        return raw, True
    except FileNotFoundError:
        return _empty_state(), True
    except Exception as exc:
        logger.warning("esports_lane: stato cache illeggibile (%s) — budget "
                       "considerato esaurito per questo ciclo", exc)
        return _empty_state(), False


def _save_state(state: dict) -> bool:
    """Scrittura atomica, fail-safe (mai un'eccezione al chiamante)."""
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_PATH.with_suffix(CACHE_PATH.suffix + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, default=str),
                       encoding="utf-8")
        tmp.replace(CACHE_PATH)
        return True
    except Exception as exc:
        logger.warning("esports_lane: scrittura stato fallita (%s)", exc)
        return False


def _roll_day(state: dict) -> dict:
    """Azzera il contatore richieste al cambio di giorno (mantiene la cache)."""
    today = _now().strftime("%Y-%m-%d")
    if state.get("day") != today:
        state["day"] = today
        state["requests"] = 0
    return state


def budget_left(state: dict, *, healthy: bool = True) -> int:
    """Richieste OddsPapi ancora disponibili oggi (0 se lo stato e' corrotto)."""
    if not healthy:
        return 0
    return max(0, int(REQ_BUDGET_DAY) - int(state.get("requests") or 0))


#: Istante dell'ultima richiesta OddsPapi (pacing fra chiamate consecutive).
_LAST_CALL = [0.0]


def _pace() -> None:
    """Attesa fra due richieste OddsPapi (429 = richiesta persa).

    Il limite del free tier e' al MINUTO, quindi il distanziamento e' l'unico
    modo per NON sprecare quota: senza, la corsia brucia le richieste in una
    raffica e il provider le rifiuta. `ESPORTS_MIN_INTERVAL_S=0` disattiva
    (usato dai test, che non hanno rete da rispettare).
    """
    gap = min_interval_s()
    if gap <= 0:
        return
    import time
    wait = gap - (time.monotonic() - _LAST_CALL[0])
    if wait > 0:
        time.sleep(wait)
    _LAST_CALL[0] = time.monotonic()


def _fresh(entry: Any, ttl_min: float, now: Optional[datetime] = None) -> bool:
    """True se la voce di cache e' ancora fresca (TTL in minuti)."""
    if not isinstance(entry, dict) or not entry.get("ts"):
        return False
    try:
        ts = datetime.fromisoformat(str(entry["ts"]).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
    except Exception:
        return False
    age = (now or _now()) - ts
    return age <= timedelta(minutes=float(ttl_min))


def _ttl_for(entry: Any) -> float:
    """TTL dell'oracolo: corto per gli esiti NEGATIVI (Pinnacle pubblica dopo)."""
    if isinstance(entry, dict) and entry.get("ok"):
        return ODDS_TTL_MIN
    return ODDS_MISS_TTL_MIN


# ---------------------------------------------------------------------------
# 1. Discovery SX (pubblica, gratuita)
# ---------------------------------------------------------------------------

def _books(provider: Any, market_ids: List[str]) -> dict:
    """Order book taker per i mercati. Riusa la lettura di `sx_signals`.

    Un'unica implementazione della lettura del book: due copie
    divergerebbero nel modo classico (prezzo migliore vs profondita').
    """
    from sx_signals import _books_parallel
    return _books_parallel(provider, market_ids)


def discover(provider: Any = None) -> List[dict]:
    """Eventi eSports SX in finestra, con i lati comprabili e i loro prezzi.

    Ritorna `[{event_id, league_label, kickoff, team_one, team_two, depth,
    sides: [{team, market_id, selection_id, price, depth}]}]`.

    Fail-safe: qualunque errore -> [] (la corsia non deve poter fermare il
    giro del calcio). Nessuna scrittura, nessuna chiave, nessun credito.
    """
    try:
        from execution_engine import SxBetProvider
    except Exception as exc:                                    # pragma: no cover
        logger.warning("esports_lane: execution_engine non disponibile (%s)", exc)
        return []
    prov = provider
    if prov is None:
        try:
            prov = SxBetProvider()
        except Exception as exc:
            logger.warning("esports_lane: provider SX non avviato (%s)", exc)
            return []
    try:
        markets = prov.list_market_catalogue(
            event_type_ids=(str(SX_SPORT_ID),),
            market_type_ids=(str(SX_TYPE_ID),),
            max_results=int(MAX_MARKETS))
    except Exception as exc:
        logger.warning("esports_lane: discovery SX fallita (%s)", exc)
        return []
    if not markets:
        return []

    now = _now()
    limit = now + timedelta(hours=float(HOURS_AHEAD))
    # Raggruppa per EVENTO: i due mercati di una partita condividono i nomi dei
    # partecipanti e l'orario. La chiave include il MINUTO del kickoff, cosi'
    # due eventi con gli stessi nomi ma orari diversi restano separati.
    groups: Dict[tuple, dict] = {}
    for m in markets:
        t1 = str(m.get("team_one_name") or "").strip()
        t2 = str(m.get("team_two_name") or "").strip()
        o1 = str(m.get("outcome_one_name") or "").strip()
        mid = str(m.get("market_id") or "").strip()
        if not (t1 and t2 and o1 and mid):
            continue
        if m.get("line") is not None:
            continue                    # il moneyline non ha linea
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
        key = (_norm(t1), _norm(t2), kickoff.replace(second=0, microsecond=0))
        ev = groups.setdefault(key, {
            "event_id": str(m.get("event_id") or ""),
            "league_label": str(m.get("league_label")
                                or (m.get("extra") or {}).get("leagueLabel")
                                or ""),
            "kickoff": kickoff, "team_one": t1, "team_two": t2,
            "_markets": [],
        })
        ev["_markets"].append({"market_id": mid, "outcome_one": o1})

    if not groups:
        return []

    all_ids = [mk["market_id"] for ev in groups.values()
               for mk in ev["_markets"]]
    books = _books(prov, sorted(set(all_ids)))

    from sx_signals import MIN_EXEC_DEPTH_USDC

    out: List[dict] = []
    for ev in groups.values():
        sides: List[dict] = []
        total_depth = 0.0
        for mk in ev["_markets"]:
            book = books.get(mk["market_id"]) or {}
            side = book.get(1) or {}          # selection 1 = outcomeOne
            best = side.get("best") or {}
            price = best.get("price")
            depth = float(side.get("depth") or 0.0)
            total_depth += depth
            try:
                price = float(price) if price else None
            except (TypeError, ValueError):
                price = None
            if not price or price <= 1.0:
                continue
            sides.append({"team": mk["outcome_one"],
                          "market_id": mk["market_id"], "selection_id": 1,
                          "price": round(price, 4), "depth": round(depth, 2)})
        if not sides:
            continue
        # Guardrail di scan allineato a `sx_signals`: la leg che verrebbe
        # giocata deve poter eseguire senza slippage. Un candidato non
        # eseguibile e' rumore (finirebbe in `liquidity_monitor` come scarto).
        sides = [s for s in sides if s["depth"] >= MIN_EXEC_DEPTH_USDC]
        if not sides:
            logger.debug("esports_lane: %s vs %s scartato (leg giocabile "
                         "sotto %.1f USDC)", ev["team_one"], ev["team_two"],
                         MIN_EXEC_DEPTH_USDC)
            continue
        ev["sides"] = sides
        ev["depth"] = round(total_depth, 2)
        ev.pop("_markets", None)
        out.append(ev)
        if len(out) >= int(MAX_EVENTS):
            break
    return out


def _norm(name: Any) -> str:
    """Chiave di confronto nomi (una sola definizione: `team_names`)."""
    try:
        from team_names import normalize
        return normalize(str(name or ""))
    except Exception:                                           # pragma: no cover
        return str(name or "").strip().casefold()


def _same_team(a: Any, b: Any) -> bool:
    try:
        from team_names import same_team
        return bool(same_team(str(a or ""), str(b or "")))
    except Exception:                                           # pragma: no cover
        return _norm(a) == _norm(b)


# ---------------------------------------------------------------------------
# 2. Oracolo con cache e budget
# ---------------------------------------------------------------------------

def _cached_fixtures(state: dict, title: str, *, http_get: Any = None,
                     budget_ok: bool = True) -> Optional[List[dict]]:
    """Righe fixture del titolo, dalla cache o da UNA richiesta OddsPapi.

    None = non disponibile (budget esaurito o lettura fallita): il chiamante
    salta l'evento. Non solleva mai.
    """
    entry = (state.get("fixtures") or {}).get(title)
    if _fresh(entry, FIXTURES_TTL_MIN):
        return entry.get("rows") or []
    if not budget_ok:
        return None
    import esports_oracle as eo
    _pace()
    res = eo.fixtures(title, http_get=http_get)
    state["requests"] = int(state.get("requests") or 0) + int(
        res.get("requests") or 0)
    if not res.get("ok"):
        logger.info("esports_lane: fixtures '%s' non lette (%s)",
                    title, res.get("error"))
        state.setdefault("fixtures", {})[title] = {
            "ts": _now().isoformat(), "rows": []}
        return []
    rows = res.get("fixtures") or []
    state.setdefault("fixtures", {})[title] = {
        "ts": _now().isoformat(), "rows": rows}
    return rows


def _cached_oracle(state: dict, fixture: dict, home: str, away: str, *,
                   http_get: Any = None,
                   budget_ok: bool = True) -> Optional[dict]:
    """Probabilita' fair 2 vie per la fixture (cache, poi UNA richiesta).

    Gli esiti negativi si rinfrescano molto piu' spesso (`ODDS_MISS_TTL_MIN`,
    15 minuti): su eSports Pinnacle pubblica tardivo, quindi un "non ancora" e'
    uno stato temporaneo — congelarlo per ore significherebbe perdere l'intera
    finestra ordini.
    """
    fid = str((fixture or {}).get("fixtureId")
              or (fixture or {}).get("fixture_id") or "").strip()
    if not fid:
        return None
    entry = (state.get("odds") or {}).get(fid)
    if _fresh(entry, _ttl_for(entry)):
        return entry.get("oracle")
    if not budget_ok:
        return None
    import esports_oracle as eo
    _pace()
    res = eo.oracle_for_fixture(fixture, home=home, away=away,
                                http_get=http_get)
    state["requests"] = int(state.get("requests") or 0) + int(
        res.get("requests") or 0)
    state.setdefault("odds", {})[fid] = {
        "ts": _now().isoformat(), "ok": bool(res.get("ok")),
        "oracle": res.get("oracle"), "error": res.get("error")}
    if not res.get("ok"):
        logger.info("esports_lane: oracolo assente per %s vs %s (%s)",
                    home, away, res.get("error"))
        return None
    return res.get("oracle")


# ---------------------------------------------------------------------------
# 3. Pick (discovery + oracolo + EV gate)
# ---------------------------------------------------------------------------

def price_band() -> Tuple[float, float]:
    """Fascia quota della corsia: la STESSA della strategia di produzione.

    La soglia non si copia: si legge da `value_filter` (una sola definizione
    nel progetto). `ESPORTS_ODDS_MIN`/`ESPORTS_ODDS_MAX` permettono di
    stringere o allargare SOLO gli eSports senza toccare il calcio — e senza
    riscrivere il codice, perche' una fascia di prezzo e' una decisione di
    strategia, non un dettaglio implementativo.
    """
    from value_filter import ODDS_MIN, ODDS_MAX
    lo = float(os.getenv("ESPORTS_ODDS_MIN", str(ODDS_MIN)))
    hi = float(os.getenv("ESPORTS_ODDS_MAX", str(ODDS_MAX)))
    return lo, hi


def _status_for(ev: float, edge: Optional[float]) -> str:
    """Tier del ledger con le SOGLIE DI PRODUZIONE (mai copiate a mano).

    Serve alla telemetria e ai cap di staking: gli stessi valori che assegna
    `sx_signals` ai segnali di calcio, cosi' un tier non significa due cose
    diverse in due corsie. In LIVE lo stake e' comunque l'importo fisso.
    """
    try:
        from market_calib import MARKET_EDGE_STRONG
    except Exception:                                           # pragma: no cover
        MARKET_EDGE_STRONG = 0.04
    if ev > 0.08 and (edge is None or edge >= MARKET_EDGE_STRONG):
        return "strong_value"
    if ev > 0.03:
        return "value"
    return "moderate"


def picks(*, provider: Any = None, http_get: Any = None,
          now: Optional[datetime] = None) -> List[dict]:
    """Candidati eSports +EV pronti per il giro ordini (sola lettura).

    Fail-safe: qualunque errore -> [] con log. L'oracolo e' fail-closed: un
    evento senza Pinnacle de-vigato NON produce candidati (senza una verita' di
    riferimento non c'e' ritardo di prezzo da comprare).
    """
    if not enabled():
        return []
    state, healthy = _load_state()
    state = _roll_day(state)
    changed = False
    out: List[dict] = []
    try:
        events = discover(provider=provider)
        # PRIORITA' AL PIU' VICINO (30/09/2026): il budget e' la risorsa scarsa
        # (8 richieste/giorno, ~4 per evento) e gli eventi arrivano in ordine di
        # catalogo SX. Processandoli per kickoff crescente la quota va a chi sta
        # per iniziare — l'unico che puo' ancora diventare un ordine.
        events = sorted(events, key=lambda e: e["kickoff"])
        oracle_horizon = _now() + timedelta(hours=oracle_window_h())
        for ev in events:
            import esports_oracle as eo
            title = eo.title_of_league(ev.get("league_label"))
            if title is None:
                # Etichetta di lega SX non riconosciuta: si salta dichiarando
                # il motivo. Un titolo "indovinato" confronterebbe il prezzo
                # con l'oracolo di un ALTRO gioco (falso value).
                logger.info("esports_lane: lega SX '%s' non mappata a un "
                            "titolo, salto %s vs %s",
                            ev.get("league_label"), ev.get("team_one"),
                            ev.get("team_two"))
                continue
            if ev["kickoff"] > oracle_horizon:
                # Discovery e fascia sono gratis, l'oracolo no: su eSports lo
                # sharp pubblica tardivo, quindi interrogarlo ore prima
                # significa pagare per un "non ancora".
                logger.debug("esports_lane: %s vs %s oltre la finestra "
                             "oracolo (%.1fh), nessuna richiesta",
                             ev["team_one"], ev["team_two"], oracle_window_h())
                continue
            if not budget_left(state, healthy=healthy):
                logger.info("esports_lane: budget OddsPapi esaurito per oggi "
                            "(%d/%d) — nessuna richiesta",
                            int(state.get("requests") or 0), int(REQ_BUDGET_DAY))
                break
            rows = _cached_fixtures(state, title, http_get=http_get,
                                    budget_ok=healthy)
            changed = True
            if rows is None:
                break
            fixture = eo.match_fixture(title, ev["team_one"], ev["team_two"],
                                       fixtures_rows=rows)
            if fixture is None:
                logger.info("esports_lane: nessuna fixture OddsPapi per %s vs "
                            "%s (%s)", ev["team_one"], ev["team_two"], title)
                continue
            probs = _cached_oracle(state, fixture, ev["team_one"],
                                   ev["team_two"], http_get=http_get,
                                   budget_ok=True)
            if not probs:
                continue
            prices = {s["team"]: float(s["price"]) for s in ev["sides"]}
            market = _market_prob(prices)
            lo, hi = price_band()
            for side in ev["sides"]:
                team, price = side["team"], float(side["price"])
                # Fascia quota PRIMA dell'oracolo: un prezzo fuori fascia non
                # e' un candidato della strategia, qualunque cosa dica
                # l'oracolo (lezione del 22/09: la fascia e' congelata e
                # allargarla e' una decisione tracciata, non un effetto
                # collaterale). Cosi' non si consuma quota per valutarlo.
                if price < lo or price > hi:
                    logger.info("esports_lane: %s vs %s -> %s @ %.2f fuori "
                                "fascia %.2f-%.2f, salto", ev["team_one"],
                                ev["team_two"], team, price, lo, hi)
                    continue
                # Orientamento: l'oracolo e' allineato su (team_one, team_two)
                # di SX; il lato "1" e' team_one, il lato "2" e' team_two.
                if _same_team(team, ev["team_one"]):
                    key = "1"
                elif _same_team(team, ev["team_two"]):
                    key = "2"
                else:
                    logger.warning("esports_lane: lato '%s' non riconducibile "
                                   "a un partecipante di %s vs %s, salto",
                                   team, ev["team_one"], ev["team_two"])
                    continue
                verdict = eo.candidate_for(probs, price, key)
                if not verdict:
                    continue
                if not verdict.get("trigger"):
                    logger.info("esports_lane: %s (%s) @ %.2f EV %+.2f%% < "
                                "%.1f%%: no value", ev["team_one"],
                                team, price, verdict["ev"] * 100.0,
                                eo.min_ev() * 100.0)
                    continue
                # `ev_gate` espone la probabilita' fair come `prob` (non
                # `p_true`, che e' il nome usato dal gate del calcio) e la
                # soglia NON e' ripetuta nella riga: si legge dalla fonte
                # unica (`esports_oracle.min_ev` -> `value_filter.EV_MIN`).
                p_true = float(verdict["prob"])
                # Prob. implicita del LATO per NOME (non per posizione):
                # l'ordine dei mercati restituiti da SX non e' un contratto.
                m_prob = (market or {}).get(team)
                edge = (p_true - m_prob) if m_prob else None
                out.append({
                    "match_id": f"sx-esports-{ev['event_id'] or side['market_id']}",
                    "home": ev["team_one"], "away": ev["team_two"],
                    "commence": ev["kickoff"].isoformat(),
                    "league": ev.get("league_label") or "",
                    "mercato": MARKET, "esito_key": key,
                    "esito_raw": team, "team": team,
                    "quota": price, "price": price,
                    "market_id": side["market_id"],
                    "selection_id": side["selection_id"],
                    "market_edge": edge, "market_prob": m_prob,
                    "best_ev": float(verdict["ev"]),
                    "status": _status_for(float(verdict["ev"]), edge),
                    "esports_lane": True, "oracle_title": title,
                    "esports_fixture_id": fixture.get("fixtureId"),
                    "p_true": p_true,
                    "true_odd": verdict["true_odd"],
                    "depth_usdc": side.get("depth"),
                })
                logger.info("esports_lane: %s vs %s -> %s @ %.2f EV %+.2f%% "
                            "(p_true %.3f, true odd %.3f) [%s/%s]",
                            ev["team_one"], ev["team_two"], team, price,
                            verdict["ev"] * 100.0, p_true,
                            verdict["true_odd"], MARKET, title)
    except Exception as exc:
        logger.warning("esports_lane: corsia non disponibile (%s)", exc)
        return []
    finally:
        if changed or not healthy:
            _save_state(state)
    return out


def _market_prob(prices: Dict[str, float]) -> Optional[Dict[str, Any]]:
    """Probabilita' implicite (de-vigate) dei lati, chiave = NOME squadra.

    Serve SOLO a calcolare l'edge per il tier del ledger: il giudice del
    prezzo resta l'oracolo. Con un solo lato leggibile non si de-viga nulla
    (un margine attribuito a meta' mercato sarebbe un numero inventato).

    La chiave e' il nome, non "1"/"2": l'ordine con cui SX restituisce i due
    mercati non e' un contratto, e un edge attribuito al lato sbagliato
    sarebbe telemetria falsa su cui poi si tarano i tier.
    """
    usable = {str(k): float(v) for k, v in (prices or {}).items()
              if v and float(v) > 1.0}
    if len(usable) < 2:
        return None
    try:
        from market_calib import market_implied
    except Exception:                                           # pragma: no cover
        return None
    res = market_implied(usable)
    if not res:
        return None
    return {name: res.get(name) for name in usable}


# ---------------------------------------------------------------------------
# 4. Diagnostica
# ---------------------------------------------------------------------------

def eo_title(label: Any) -> Optional[str]:
    """Titolo eSports di un'etichetta di lega SX (None se non mappata)."""
    import esports_oracle as eo
    return eo.title_of_league(label)


def summary(*, provider: Any = None, http_get: Any = None) -> dict:
    """Stato della corsia: interruttore, finestra, budget e pick trovati.

    Serve a distinguere "accesa che non trova nulla" da "rotta": i titoli
    riconosciuti, gli eventi in finestra e le richieste consumate sono tutti
    dichiarati. Fail-safe (nessuna eccezione al chiamante).

    ⚠️ **NON e' gratis**: `picks()` interroga l'oracolo (a pagamento) per gli
    eventi dentro la finestra. La chiamata e' gratuita solo quando nessun
    evento e' vicino al kickoff — cioe' proprio quando non c'e' nulla da
    misurare. Su un ambiente a quota scarsa va eseguita con giudizio.
    """
    state, healthy = _load_state()
    state = _roll_day(state)
    events = discover(provider=provider)
    # Eventi ENTRATI nella finestra dell'oracolo: e' il numero che distingue la
    # corsia DORMIENTE (nessun evento vicino: zero richieste, zero costo) dalla
    # corsia che lavora. Il silenzio nel log e' voluto quando questo e' 0.
    horizon = _now() + timedelta(hours=oracle_window_h())
    in_oracle = [e for e in events if e["kickoff"] <= horizon]
    found = picks(provider=provider, http_get=http_get)
    return {
        "enabled": enabled(),
        "sx_sport_id": SX_SPORT_ID, "sx_type_id": SX_TYPE_ID,
        "hours_ahead": HOURS_AHEAD,
        "oracle_window_h": oracle_window_h(),
        "min_interval_s": min_interval_s(),
        "events_in_window": len(events),
        "events_in_oracle_window": [
            {"home": e["team_one"], "away": e["team_two"],
             "kickoff": e["kickoff"].isoformat(),
             "title": eo_title(e.get("league_label"))} for e in in_oracle],
        "oracle_titles": sorted({eo_title(e.get("league_label"))
                                 or f"?({e.get('league_label')})"
                                 for e in events}),
        "price_band": list(price_band()),
        "picks": found,
        "requests_today": int(state.get("requests") or 0),
        "request_budget": int(REQ_BUDGET_DAY),
        "budget_state_healthy": healthy,
        "cache": str(CACHE_PATH),
        "ttl_min": {"fixtures": FIXTURES_TTL_MIN, "odds": ODDS_TTL_MIN,
                    "odds_miss": ODDS_MISS_TTL_MIN},
    }


def format_report(s: dict) -> str:
    lines = ["🎮 Corsia eSports (SX sport %s / type %s)"
             % (s.get("sx_sport_id"), s.get("sx_type_id")),
             f"   interruttore: {'ON' if s.get('enabled') else 'OFF'}"
             f" | finestra {s.get('hours_ahead')}h"
             f" | fascia {s.get('price_band')}"
             f" | eventi in finestra: {s.get('events_in_window')}"
             f" | di cui entro la finestra oracolo "
             f"({s.get('oracle_window_h')}h): "
             f"{len(s.get('events_in_oracle_window') or [])}"
             f"   titoli: {', '.join(s.get('oracle_titles') or []) or 'nessuno'}",
             f"   richieste OddsPapi oggi: {s.get('requests_today')}/"
             f"{s.get('request_budget')}"
             f"{'' if s.get('budget_state_healthy') else ' (stato cache corrotto: budget considerato esaurito)'}",
             f"   TTL cache (min): {s.get('ttl_min')}"]
    found = s.get("picks") or []
    if not found:
        lines.append("   candidati +EV: NESSUNO")
    for p in found:
        lines.append(f"   ✅ {p['home']} vs {p['away']} -> {p['team']} "
                     f"@ {p['quota']:.2f} EV {p['best_ev'] * 100:+.2f}% "
                     f"[{p['status']}]")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Corsia eSports (diagnostica)")
    ap.add_argument("--scan", action="store_true", help="mostra gli eventi")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--report", action="store_true", help="riepilogo")
    args = ap.parse_args(argv)
    s = summary()
    if args.json:
        print(json.dumps(s, ensure_ascii=False, indent=2, default=str))
        return 0
    if args.scan:
        for ev in discover():
            sides = ", ".join(f"{x['team']} @ {x['price']:.2f} "
                              f"({x['depth']:.0f} USDC)" for x in ev["sides"])
            print(f"{ev['kickoff'].isoformat()} "
                  f"[{ev.get('league_label')}] {ev['team_one']} vs "
                  f"{ev['team_two']} :: {sides}")
        return 0
    print(format_report(s))
    return 0


if __name__ == "__main__":                                      # pragma: no cover
    raise SystemExit(main())
