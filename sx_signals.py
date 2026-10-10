"""sx_signals.py — Segnali value 1X2 direttamente dai prezzi SX Bet.

Perche' questo modulo (09/09): il flusso storico nasce dalle quote
the-odds-api (fixture_engine.fetch_and_analyze_today) che serve ODDS_API_KEY
e brucia crediti; qui il MOTORE segue la stessa strategia (il valore esiste
solo se il modello batte il mercato devig, non un singolo bookmaker) usando
SOLO l'API PUBBLICA SX Bet (zero chiavi, zero crediti):

1. discovery: i 3 mercati binari "X vs Not X" (type 1, sportId 5) per
   partita -> si ricostruisce il 1X2 con i migliori prezzi BACK del book
   taker (stessi snapshot letti da scan_sx_live);
2. coerenza e liquidita': inv_sum (somma inversi) 0.98-1.08, quote sane e
   book con profondita' sufficiente (totale, per esito e sulla leg che
   verrebbe giocata: vedi le soglie MIN_DEPTH/MIN_LEG_DEPTH/MIN_EXEC_DEPTH)
   — sotto soglia il match NON genera candidati;
3. modello: Poisson del progetto (poisson_engine.expected_goals/prob_1x2,
   rating dinamici inclusi) vs mercato fair (market_calib.market_implied,
   devig power su 1/X/2);
4. segnale: EV da prob finale blend (value_filter.adjusted_probability) e
   filtri sanità (is_sane: EV 2-15%, quote 1.50-2.20, edge >= +2pp vs
   mercato) — identici al flusso the-odds-api;
5. ledger: match/s match_analysis/predictions via tracker (save_match,
   save_analysis, save_prediction): da qui in poi auto_bet.run_today_bets
   li vede come QUALSIASI altro segnale value e — in AUTO_BET_MODE=live con
   provider SX configurato — piazza l'ordine reale sullo STESSO exchange
   che ha generato il prezzo (resolve_match_market matches per nomi+kickoff).

Settlement: PRIMA la fonte NATIVA SX (`_results_from_sx`, 12/09): i
market_hash salvati sulle bet permettono di leggere l'esito saldato
(`markets/find` -> outcome + punteggi) SENZA matching per nome, SENZA
crediti e coprendo anche le leghe FUORI SPORTS_MAP (Primera A, Primera
Nacional, K2-League) che the-odds-api non copre. Poi, per i match con
riga nel ledger, i punteggi esterni: the-odds-api SE configurata (match
per NOME+LEGA), altrimenti API-Football (football_hist). Finche' nessuna
fonte e' disponibile le bet restano aperte (fail-closed).

CLI:
    venv/bin/python sx_signals.py scan        # scan + salvataggio segnali
    venv/bin/python sx_signals.py settle      # recupera punteggi e salda
    venv/bin/python sx_signals.py repair      # rimappa le leghe gia' salvate
"""

from __future__ import annotations

import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Dict, List, Optional

from execution_engine import SxBetProvider, pct_scaled_to_decimal, sx_units_to_stake
from market_calib import market_implied, MARKET_EDGE_STRONG
from poisson_engine import expected_goals, prob_1x2
from tracker import save_match, save_analysis, save_prediction
from value_filter import (compute_ev, is_sane, adjusted_probability,
                          eligible_favourites, favourites_gate_reason)

logger = logging.getLogger("sx_signals")

# --- Soglie di coerenza/liquidita' (identiche a scan_sx_live.py) -----------
MIN_INV_SUM, MAX_INV_SUM = 0.98, 1.08
# FILTRO LIQUIDITA' SX (tarato l'11/09/2026, allentato del 20% il 21/09).
# Su un exchange si scommette contro altri utenti, quindi su mercati sottili la
# quota mostrata puo' non essere disponibile e l'ordine va in slippage (o resta
# parziale). Tre soglie, tutte overridabili da env senza redeploy di codice:
#
#   SX_MIN_DEPTH_USDC      (20) -> liquidita' TOTALE del match (3 esiti
#     sommati): salute del mercato, un book complessivamente vuoto non e'
#     devigabile in modo affidabile.
#   SX_MIN_LEG_DEPTH_USDC  (4)  -> profondita' minima di OGNI esito: sotto
#     questa soglia il singolo lato e' di fatto inesistente.
#   SX_MIN_EXEC_DEPTH_USDC (20) -> profondita' minima della LEG GIOCATA
#     (il favorito scelto). E' la STESSA soglia assoluta che il guardrail
#     d'ordine applica in auto_bet (`required_depth`): il segnale viene
#     generato solo se l'ordine puo' davvero essere eseguito, altrimenti
#     sarebbe rumore destinato a uno scarto sicuro al momento dell'ordine.
# VALORI 25/5/25 (11/09) -> 20/4/20 (21/09, direttiva "volume"): la misura
# dell'11/09 diceva che a 25 USDC passava il 100% dei favoriti (42/42), quindi
# il taglio del 20% compra poco volume ma allarga la fascia dei book eseguibili
# restando sopra la taglia minima d'ordine (1 USDC). Le protezioni anti-slippage
# restano (soglia assoluta + multiplo + inv_sum + fascia quota).
MIN_DEPTH_USDC = float(os.getenv("SX_MIN_DEPTH_USDC", "20.0"))
MIN_LEG_DEPTH_USDC = float(os.getenv("SX_MIN_LEG_DEPTH_USDC", "4.0"))
MIN_EXEC_DEPTH_USDC = float(os.getenv("SX_MIN_EXEC_DEPTH_USDC", "20.0"))

# --- Finestra dei match candidati ------------------------------------------
HOURS_AHEAD = 24.0          # come auto_bet._today_value_picks (now..now+24h)
MIN_MINUTES_TO_START = 2    # auto_bet salta comunque i match vicini: qui
                            # non generiamo segnali gia' degni di salto
                            # (2 dal 04/10/2026: la finestra esecutiva arriva
                            # a T-2, la scansione non deve fermarsi prima)
MAX_RAW_MARKETS = 300       # mercati binari da scansionare (100 partite)

# --- Settlement nativo SX ---------------------------------------------------
# `markets/find` accetta al massimo 30 market hash per chiamata (docs SX).
SX_FIND_BATCH = 30
# I punteggi live su /markets/active sono affidabili come FINALE solo quando
# la partita e' veramente finita: sotto i 120' dal kickoff (stoppage, ripresa
# lunga) il punteggio puo' ancora cambiare e si chiuderebbe un verdetto
# prematuro. Il percorso serve alle leghe non coperte da the-odds-api, dove
# due ore di ritardo sono accettabili; le coperte arrivano prima.
SX_LIVE_MIN_AGE_MS = 120 * 60 * 1000

# Mercato SX a DUE ESITI ("X vs Not X"): type 52 = moneyline 2 vie
# (tennis sportId 6, eSports sportId 9, "12 senza pareggio" calcio).
# Su questi mercati il campo `outcome` dell'exchange (1 = vince
# outcomeOne, 2 = vince outcomeTwo, 0 = void/NO_CONTEST) E' il verdetto
# definitivo e NON va ricavato dai punteggi (vedi `_results_from_sx`).
SX_MONEYLINE_TYPE = 52

_LEAGUE_MAP_CACHE: Dict[str, Optional[str]] = {}

# ---------------------------------------------------------------------------
# Mapping leghe SX -> chiave SPORTS_MAP (fix 11/09/2026)
#
# BUG ("mapping leghe"): il vecchio fuzzy cercava il migliore con
# SequenceMatcher >= 0.55 e RESTITUIVA COMUNQUE un nome, anche sbagliato:
#   'Major League Soccer' -> 'League One'      (MLS salvato come League One)
#   'German Bundesliga'   -> 'Austrian Bundesliga'
#   'Jupiler League'      -> 'Premier League'
#   'K1-League'           -> 'J1 League'
# Conseguenza: il settlement interrogava la lega SBAGLIATA di the-odds-api,
# non trovava mai il punteggio e le bet restavano aperte per sempre.
#
# Qui la mappa e' DETERMINISTICA per le etichette reali osservate sull'API
# pubblica SX (sample 11/09: ~40 label). Valore None = "l'etichetta NON e'
# una competizione coperta da SPORTS_MAP" (rifiuto esplicito: mai indovinare).
SX_LEAGUE_ALIASES: Dict[str, Optional[str]] = {
    # Nord America
    "Major League Soccer": "MLS", "MLS": "MLS", "USA MLS": "MLS",
    "USA Major League Soccer": "MLS", "United States MLS": "MLS",
    "USL Championship": None,
    # Varianti con prefisso paese (viste in test/storico)
    "Italy Serie A": "Serie A", "Italy Serie B": "Serie B",
    # Sud America
    "Liga Profesional": "Argentina Primera",
    "Primera Nacional": None,          # Argentina 2: non coperta
    "Primera A": None,                 # Colombia: non coperta
    "Primera Division": "Chile Primera",
    "LigaPro": None,                   # Ecuador: non coperta
    "Division Profesional": None,      # Bolivia: non coperta
    "Campeonato Brasileiro": "Brasileirao",
    "Brasileiro Serie B": "Brazil Serie B",
    # Europa
    "English Premier League": "Premier League", "The Championship": "EFL Championship",
    "German Bundesliga": "Bundesliga", "Jupiler League": "Belgian First Div",
    "Portugal Primeira Liga": "Primeira Liga", "Super Lig": "Turkey Super Lig",
    # Varianti con prefisso paese delle leghe della STRATEGIA (value_filter.
    # STRATEGY_LEAGUES): esplicite e deterministiche, cosi' il gate non
    # puo' VIETARE per errore una lega ammessa (un falso divieto varrebbe
    # piu' di un divieto mancante: azzererebbe il flusso autorizzato).
    "England Premier League": "Premier League",
    "Germany Bundesliga": "Bundesliga",
    "France Ligue 1": "Ligue 1",
    "Netherlands Eredivisie": "Eredivisie",
    "Turkish Super Lig": "Turkey Super Lig",
    "Turkey Super Lig": "Turkey Super Lig",
    "Ligue 1": "Ligue 1", "Eredivisie": "Eredivisie",
    "Premier League": "Premier League", "Bundesliga": "Bundesliga",
    # Varianti con prefisso paese delle leghe in PROBATION (value_filter.
    # PROBATION_LEAGUES, tier-2 dal 21/09/2026): stessa ragione delle righe
    # sopra — un falso DIVIETO su una lega giocabile varrebbe piu' di un
    # divieto mancante, perche' azzererebbe il flusso autorizzato.
    "England Championship": "EFL Championship",
    "EFL Championship": "EFL Championship",
    "Argentina Primera Division": "Argentina Primera",
    "Brazil Campeonato Brasileiro": "Brasileirao",
    "Swiss Super League": "Swiss Super League",
    "Switzerland Super League": "Swiss Super League",
    "Eliteserien": "Eliteserien", "Norway Eliteserien": "Eliteserien",
    "Austrian Bundesliga": "Austrian Bundesliga",
    "Scotland Premiership": "Scottish Premiership",
    "Scottish Premiership": "Scottish Premiership",
    "Denmark Superliga": "Superliga Danimarca",
    "Sweden Allsvenskan": "Allsvenskan", "Allsvenskan": "Allsvenskan",
    "South Korea K League 1": "K League 1", "K League 1": "K League 1",
    "Japan J1 League": "J1 League", "J1 League": "J1 League",
    "Mexico Liga MX": "Liga MX", "Liga MX": "Liga MX",
    "Saudi Arabia Pro League": "Saudi Pro League",
    "Saudi Pro League": "Saudi Pro League",
    "Europa League_UEFA": "Europa League", "Champions League_UEFA": "Champions League",
    "Premiership": "Scottish Premiership", "Superettan": "Sweden Superettan",
    "Superliga": "Superliga Danimarca",
    # Asia / resto del mondo
    "K1-League": "K League 1", "K2-League": None,   # K League 2 non coperta
    "First League": None,              # Rep. Ceca: non coperta
    "Besta Deild Karla": None,         # Islanda: non coperta
}

_SX_ALIAS_NORM: Optional[Dict[str, Optional[str]]] = None


def _alias_map() -> Dict[str, Optional[str]]:
    """Alias normalizzati (etichetta -> chiave SPORTS_MAP | None), cache lazy."""
    global _SX_ALIAS_NORM
    if _SX_ALIAS_NORM is None:
        from execution_engine import _name_key
        _SX_ALIAS_NORM = {_name_key(k): v for k, v in SX_LEAGUE_ALIASES.items()}
    return _SX_ALIAS_NORM


def _strict_fuzzy_league(target: str) -> Optional[str]:
    """Fallback per etichette NON in tabella: corrispondenza STRETTA e non
    ambigua, altrimenti None (mai indovinare).

    Punteggi: 1.0 se il nome coincide; 0.97 se uno e' prefisso/suffisso di
    paese dell'altro ('Italy Serie A' -> 'Serie A'); altrimenti la
    somiglianza di sequenza, accettata solo >= 0.92 o con token contenuti.
    Si accetta solo il migliore se distanzia il secondo di >= 0.08, cosi'
    un'etichetta ambigua ('Serie B' da solo tra Italia e Brasile) NON sceglie
    a caso. 'Major League Soccer' e' in alias, quindi non arriva qui.
    """
    from difflib import SequenceMatcher
    from odds_api import SPORTS_MAP
    from execution_engine import _name_key
    tt = set(target.split())
    scored = []
    for lg in SPORTS_MAP:
        k = _name_key(lg)
        if not k:
            continue
        kt = set(k.split())
        sim = SequenceMatcher(None, target, k).ratio()
        if target == k:
            score = 1.0
        elif target.endswith(" " + k) or k.endswith(" " + target):
            score = 0.97
        elif tt <= kt or kt <= tt:
            score = sim
        elif sim >= 0.92:
            score = sim
        else:
            continue
        if score > 0:
            scored.append((score, lg))
    if not scored:
        return None
    scored.sort(key=lambda x: -x[0])
    best_score, best = scored[0]
    second = scored[1][0] if len(scored) > 1 else 0.0
    if best_score < 0.90 or (best_score - second) < 0.08:
        return None
    return best


def _league_sx_to_sports_map(league_label: str) -> Optional[str]:
    """leagueLabel SX -> nome lega SPORTS_MAP (per il settlement the-odds-api).

    Deterministico: 1) alias esplicita (incluse le etichette coperte da
    SPORTS_MAP), 2) chiave SPORTS_MAP esatta, 3) fuzzy STRETTO non ambiguo.
    None = etichetta non mappabile (settlement via fonte alternativa o da
    correggere): MAI una lega sbagliata (bug mapping leghe 11/09).
    """
    if league_label in _LEAGUE_MAP_CACHE:
        return _LEAGUE_MAP_CACHE[league_label]
    try:
        from execution_engine import _name_key
        from odds_api import SPORTS_MAP
        target = _name_key(league_label)
        result: Optional[str] = None
        if target:
            amap = _alias_map()
            if target in amap:
                result = amap[target]
            else:
                for lg in SPORTS_MAP:
                    if _name_key(lg) == target:
                        result = lg
                        break
                else:
                    result = _strict_fuzzy_league(target)
    except Exception:
        result = None
    _LEAGUE_MAP_CACHE[league_label] = result
    return result


def league_to_sport(league: Optional[str]) -> Optional[str]:
    """Nome lega (chiave SPORTS_MAP, etichetta SX grezza o alias) -> sport key.

    Usato dal settlement per NON saltare in silenzio una lega: se il nome non
    risolve a uno sport key the-odds-api, ritorna None e il chiamante logga.
    """
    if not league:
        return None
    try:
        from odds_api import SPORTS_MAP
        if league in SPORTS_MAP:
            return SPORTS_MAP[league]
    except Exception:
        pass
    lg = _league_sx_to_sports_map(league)
    if not lg:
        return None
    try:
        from odds_api import SPORTS_MAP
        return SPORTS_MAP.get(lg)
    except Exception:
        return None


def _now_ms() -> int:
    return int(time.time() * 1000)


def _kickoff_utc_ms(game_time) -> Optional[int]:
    """Kickoff del mercato SX in ms epoch; None se assente/non valido.

    Accetta i formati osservati sull'API SX: gameTime in SECONDI epoch
    (int, es. 1788980400 = 2026-09-09 19:00 UTC), ms epoch, oppure date
    ISO (open_date del catalogo del provider).
    """
    if game_time is None:
        return None
    if isinstance(game_time, (int, float)):
        gt = int(game_time)
        if gt <= 0:
            return None
        # Secondi (~1.7e9) vs ms (~1.7e12): soglia 1e11 copre anni 1973-5138.
        return gt * 1000 if gt < 10 ** 11 else gt
    try:
        dt = datetime.fromisoformat(str(game_time).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except (TypeError, ValueError):
        return None


#: Livelli del book conservati per esito in `_book`. Servono al rilevatore di
#: flusso (`book_flow.py`): riconoscere l'INGRESSO di un limit order massiccio
#: richiede i livelli, non solo il best e la profondita' totale. Cinque
#: coprono la parte giocabile del book senza appesantire i dizionari che
#: circolano nel giro (il book intero di SX puo' avere decine di livelli).
#: ADDITIVO: i chiamanti esistenti continuano a leggere `best`/`depth` e
#: nessuna decisione cambia. Override: SX_BOOK_LEVELS_KEPT.
try:
    BOOK_LEVELS_KEPT = max(0, int(os.getenv("SX_BOOK_LEVELS_KEPT", "5")))
except (TypeError, ValueError):
    BOOK_LEVELS_KEPT = 5


def _book(provider: SxBetProvider, market_id: str) -> dict:
    """Snapshot order book taker: best back, profondita' USDC e livelli."""
    data = provider._get("orderbook-v3/snapshot", params={
        "marketHash": market_id, "showTakerPerspective": "true"})
    d = data.get("data") or {}
    out: dict = {}
    for key, sel in (("outcomeOne", 1), ("outcomeTwo", 2)):
        levels = d.get(key) or []
        best = None
        depth = 0.0
        kept: list = []
        for lv in levels:
            if not isinstance(lv, dict):
                continue
            q = pct_scaled_to_decimal(lv.get("percentageOdds"))
            size = sx_units_to_stake(lv.get("size"))
            if not q or q <= 1.0:
                continue
            depth += size
            kept.append({"price": round(q, 4), "size": round(size, 4)})
            if best is None or q > best["price"]:
                best = {"price": q, "size": size}
        # Livelli dal prezzo migliore in giu': e' l'ordine in cui il mercato
        # assorbe (prima il best, poi il resto).
        kept.sort(key=lambda item: -item["price"])
        out[sel] = {"best": best, "depth": round(depth, 2),
                    "levels": kept[:BOOK_LEVELS_KEPT]}
    return out


def _books_parallel(provider: SxBetProvider, market_ids: List[str]) -> dict:
    """Snapshot paralleli (10 thread, come scan_sx_live)."""
    def _fetch(mid: str):
        try:
            return mid, _book(provider, mid)
        except Exception as e:
            return mid, {"error": str(e)}

    out: dict = {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = {ex.submit(_fetch, mid): mid for mid in market_ids}
        for fut in as_completed(futs):
            mid, res = fut.result()
            out[mid] = res
    return out


def _discover(provider: SxBetProvider,
              max_markets: int = MAX_RAW_MARKETS) -> List[dict]:
    """Partite 1X2 calcio SX nella finestra (now-1h .. now+HOURS_AHEAD).

    Discovery via endpoint RAW /markets/active (type 1 = mercati binari
    "X vs Not X"): a differenza del catalogo del provider include leagueId,
    leagueLabel, gameTime (ms) e sportXeventId, che servono per il ledger
    (lega -> SPORTS_MAP) e il match_id stabile. Raggruppa i 3 mercati per
    evento; ritorna [{event_id, league_label, kickoff_ms, teams, legs}].
    """
    raw: List[dict] = []
    pagination_key: Optional[str] = None
    while len(raw) < max_markets:
        params: Dict = {"sportIds": "5", "type": "1", "pageSize": 100}
        if pagination_key:
            params["paginationKey"] = pagination_key
        data = provider._get("markets/active", params=params)
        d = data.get("data") if isinstance(data, dict) else {}
        markets = (d or {}).get("markets") or []
        raw.extend(markets)
        pagination_key = (d or {}).get("nextKey")
        if not pagination_key or not markets:
            break
    now = _now_ms()
    lo = now - 60 * 60 * 1000          # -1h: includo live appena iniziati
    hi = now + HOURS_AHEAD * 3600 * 1000
    by_event: Dict[str, dict] = {}
    for m in raw:
        if not isinstance(m, dict):
            continue
        ev_id = m.get("sportXeventId")
        ko = _kickoff_utc_ms(m.get("gameTime"))
        if not ev_id or ko is None or not (lo <= ko <= hi):
            continue
        t1 = str(m.get("teamOneName") or "").strip()
        t2 = str(m.get("teamTwoName") or "").strip()
        if not t1 or not t2:
            continue
        ev = by_event.setdefault(str(ev_id), {
            "event_id": str(ev_id),
            "league_label": m.get("leagueLabel") or "",
            "kickoff_ms": ko,
            "teams": (t1, t2),
            "legs": [],
        })
        o1 = str(m.get("outcomeOneName") or "").strip().lower()
        # I 3 mercati binari 1X2: "Tie vs Not tie" + "T1 vs Not T1" +
        # "T2 vs Not T2" (esito scommesso = outcomeOne, selection_id 1).
        if o1 in ("tie", "draw", "pareggio"):
            ev["legs"].append({"esito": "X", "market_hash": m["marketHash"]})
        elif o1 == t1.lower():
            ev["legs"].append({"esito": "1", "market_hash": m["marketHash"]})
        elif o1 == t2.lower():
            ev["legs"].append({"esito": "2", "market_hash": m["marketHash"]})
    # Completezza: servono TUTTI e 3 gli esiti per devigare il 1X2.
    out = []
    for ev in by_event.values():
        esiti = {leg["esito"] for leg in ev["legs"]}
        if esiti == {"1", "X", "2"}:
            out.append(ev)
    out.sort(key=lambda e: e["kickoff_ms"])
    return out


def _kickoff_iso(kickoff_ms: int) -> str:
    """ms epoch -> ISO UTC con Z (formato commence_time del ledger)."""
    return datetime.fromtimestamp(kickoff_ms / 1000.0,
                                  tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _record_scan_skip(ev: dict, home: str, away: str, reason: str,
                      odds: Dict[str, float], depths: Dict[str, float],
                      total_depth: float, inv_sum: float,
                      threshold: float) -> None:
    """Registra nel monitor uno scarto per liquidita' (fail-safe).

    Il monitor di liquidita' e' diagnostico: un errore di I/O non deve MAI
    propagarsi allo scan (ne' bloccare la generazione dei segnali).
    """
    try:
        from liquidity_monitor import record_skip
        record_skip(
            "scan", reason,
            match_id=f"sx-{ev['event_id']}", home=home, away=away,
            quota=min(odds.values()) if odds else None,
            depth=total_depth, threshold=threshold,
            leg_depth=min(depths.values()) if depths else None,
            extra={"legs": depths, "inv_sum": round(inv_sum, 4)})
    except Exception:
        pass


def scan(provider: Optional[SxBetProvider] = None) -> List[dict]:
    """Un giro di scan: SX -> segnali value nel ledger. Ritorna i salvati.

    Fail-soft: una partita con errori viene saltata, il giro continua.
    """
    prov = provider or SxBetProvider()   # letture pubbliche: nessuna chiave
    try:
        events = _discover(prov)
    except Exception as e:
        logger.warning("sx_signals: discovery fallita: %s", e)
        return []
    if not events:
        logger.info("sx_signals: nessun match 1X2 completo nella finestra %dh",
                    HOURS_AHEAD)
        return []

    # Snapshot paralleli di tutti i book PRIMA di valutare.
    all_ids = [leg["market_hash"] for ev in events for leg in ev["legs"]]
    books = _books_parallel(prov, all_ids)
    # Flusso del book (26/09): stesso rilevatore del multi-mercato, sui book
    # GIA' scaricati — nessuna lettura in piu', nessun ordine, solo telemetria
    # (`book_flow.py`). Fail-safe: un errore non deve fermare lo scan.
    try:
        import book_flow
        book_flow.observe_books_from_scan(
            books, {leg["market_hash"]: {"home": ev["teams"][0],
                                         "away": ev["teams"][1],
                                         "league": ev.get("league_label"),
                                         "market": "1X2"}
                    for ev in events for leg in ev["legs"]})
    except Exception as e:
        logger.debug("sx_signals: book_flow non disponibile (%s)", e)

    saved: List[dict] = []
    now_ms = _now_ms()
    for ev in events:
        home, away = ev["teams"]
        kickoff_iso = _kickoff_iso(ev["kickoff_ms"])
        if ev["kickoff_ms"] <= now_ms + MIN_MINUTES_TO_START * 60 * 1000:
            continue  # troppo vicino al kickoff: l'auto-bet lo salterebbe
        try:
            lam_h, lam_a = expected_goals(home, away)
            p1, px, p2 = prob_1x2(lam_h, lam_a)
        except Exception as e:
            logger.warning("sx_signals: modello fallito su %s vs %s: %s",
                           home, away, e)
            continue

        # Prezzi SX per esito + profondita' + coerenza del mercato.
        odds: Dict[str, float] = {}
        depths: Dict[str, float] = {}
        coherent = True
        for leg in ev["legs"]:
            b = books.get(leg["market_hash"]) or {}
            if "error" in b:
                coherent = False
                break
            # In tutti e 3 i mercati binari ("Tie vs Not tie", "T1 vs Not T1",
            # "T2 vs Not T2") l'esito scommesso e' outcomeOne -> selection 1
            # (la selection 2 e' il lato complementare "Not X").
            info = b.get(1) or {}
            best = info.get("best")
            if not best:
                coherent = False
                break
            odds[leg["esito"]] = float(best["price"])
            depths[leg["esito"]] = float(info.get("depth") or 0.0)
        if not coherent or len(odds) != 3:
            continue
        inv_sum = sum(1.0 / o for o in odds.values())
        total_depth = sum(depths.values())
        if not (MIN_INV_SUM <= inv_sum <= MAX_INV_SUM):
            logger.info("sx_signals: %s vs %s inv_sum %.3f fuori range, skip",
                        home, away, inv_sum)
            continue
        if (total_depth < MIN_DEPTH_USDC
                or any(d < MIN_LEG_DEPTH_USDC for d in depths.values())):
            logger.info("sx_signals: %s vs %s liquidita' %.1f USDC insufficiente, skip",
                        home, away, total_depth)
            # Monitor scarti: traccia l'opportunita' persa perche' il book
            # SX e' troppo sottile nel complesso (fail-safe).
            thin = [k for k, d in depths.items() if d < MIN_LEG_DEPTH_USDC]
            _record_scan_skip(
                ev, home, away,
                "depth_totale" if total_depth < MIN_DEPTH_USDC
                else f"depth_esito_{thin[0]}",
                odds, depths, total_depth, inv_sum, MIN_DEPTH_USDC)
            continue

        match_id = f"sx-{ev['event_id']}"
        league_label = ev["league_label"]
        league_sports = _league_sx_to_sports_map(league_label)
        league_name = league_sports or league_label

        market = market_implied(odds)
        if not market:
            continue
        candidates = []
        for mkey, model_prob, price in (
                ("1", p1, odds["1"]), ("X", px, odds["X"]), ("2", p2, odds["2"])):
            market_prob = market.get(mkey)
            final_prob = adjusted_probability(model_prob, market_prob, price,
                                              league=league_name)
            ev_val = compute_ev(final_prob, price)
            edge = (model_prob - market_prob) if market_prob is not None else None
            candidates.append({
                "esito": mkey, "quota": price, "prob": final_prob, "ev": ev_val,
                "prob_model": model_prob, "market_prob": market_prob,
                "market_edge": edge,
                # Come in fixture_engine: senza la lega sul candidato la
                # classificazione per-esito (`is_sane`) salta il gate
                # STRATEGY_LEAGUES e scrive "value" in leghe vietate.
                "league": league_name,
            })

        # match_id deterministico: save_match fa INSERT OR REPLACE,
        # save_analysis sostituisce e save_prediction aggiorna le
        # predizioni non ancora saldate (idempotenza tra giri).
        # STRATEGIA SOLO FAVORITI (11/09): si gioca solo il miglior EV tra i
        # favoriti netti; senza favoriti il match non genera segnali.
        shortlist = eligible_favourites(candidates)
        if shortlist:
            best_c = max(shortlist, key=lambda c: c["ev"])
            # Coerenza col guardrail d'ordine (`auto_bet.required_depth`): la
            # leg che verrebbe GIOCATA deve avere profondita' al floor
            # sufficiente a eseguire l'ordine senza slippage. Sotto la soglia
            # assoluta il match non e' negoziabile: nessun segnale, perche'
            # sarebbe uno scarto sicuro al momento dell'ordine (rumore nel
            # ledger e nel CLV).
            leg_depth = float(depths.get(best_c["esito"]) or 0.0)
            if leg_depth < MIN_EXEC_DEPTH_USDC:
                logger.info("sx_signals: %s vs %s leg %s profonda %.1f USDC "
                            "< %.1f, skip (ordine non eseguibile)",
                            home, away, best_c["esito"], leg_depth,
                            MIN_EXEC_DEPTH_USDC)
                _record_scan_skip(ev, home, away,
                                  f"depth_exec_{best_c['esito']}",
                                  odds, depths, total_depth, inv_sum,
                                  MIN_EXEC_DEPTH_USDC)
                continue
        else:
            best_c = max(candidates,
                         key=lambda c: (c.get("market_prob") or 0.0))
        save_match(match_id, league_name, home, away, kickoff_iso)
        if shortlist:
            sane, reason = is_sane(best_c["prob"], best_c["quota"],
                                   best_c["ev"],
                                   market_prob=best_c["market_prob"],
                                   league=league_name)
        else:
            sane, reason = False, favourites_gate_reason()
        if not sane:
            status = "rejected"
        elif best_c["ev"] > 0.08 and (best_c["market_edge"] is None
                                      or best_c["market_edge"] >= MARKET_EDGE_STRONG):
            status = "strong_value"
        elif best_c["ev"] > 0.03:
            status = "value"
        else:
            status = "no_value"
        save_analysis(match_id, lam_h, lam_a, p1, px, p2, None,
                      best_c["ev"], best_c["esito"], best_c["quota"],
                      "SX Bet", status,
                      market_prob=best_c["market_prob"],
                      market_edge=best_c["market_edge"])
        for cand in shortlist:
            # Stessa classificazione per-candidato del flusso the-odds-api
            # (_candidate_status): nel ledger finiscono anche i no_value.
            csane, _ = is_sane(cand["prob"], cand["quota"], cand["ev"],
                               market_prob=cand["market_prob"],
                               league=cand.get("league", ""))
            if csane:
                if cand["ev"] > 0.08 and (cand["market_edge"] is None
                                          or cand["market_edge"] >= MARKET_EDGE_STRONG):
                    st = "strong_value"
                elif cand["ev"] > 0.03:
                    st = "value"
                else:
                    st = "no_value"
            else:
                st = "rejected"
            save_prediction(match_id, "1X2", cand["esito"], cand["quota"],
                            cand["prob"], cand["ev"],
                            market_prob=cand["market_prob"],
                            market_edge=cand["market_edge"], status=st,
                            league=cand.get("league") or league_name)
        if status in ("value", "strong_value"):
            saved.append({
                "match_id": match_id, "home": home, "away": away,
                "commence": kickoff_iso, "league": league_name,
                "esito": best_c["esito"], "quota": best_c["quota"],
                "ev": best_c["ev"], "status": status,
            })
        logger.info("sx_signals: %s vs %s (%s) %s — best %s @ %.2f EV %.1f%%",
                    home, away, league_label, status,
                    best_c["esito"], best_c["quota"], best_c["ev"] * 100)
    logger.info("sx_signals: %d partite analizzate, %d segnali value salvati",
                len(events), len(saved))
    return saved


# ---------------------------------------------------------------------------
# Settlement per le bet SX (match_id = "sx-<eventId>")
# ---------------------------------------------------------------------------

def _sx_open_matches() -> Dict[str, dict]:
    """Meta delle partite SX APERTE (bet oppure previsioni): {match_id: info}.

    info = {home, away, league, kickoff} — serve per abbinare i risultati
    per NOME+LEGA (le fonti esterne non conoscono i match_id sx-*).

    Copre ENTRAMBI i ledger: il risultato viene salvato in `match_results`
    con la chiave `sx-<eventId>`, quindi il giorno dopo salda sia le bet sia
    le PREVISIONI (`settle_predictions` aggancia per match_id). Limitarsi
    alle bet lasciava aperte per sempre le previsioni dei match senza una
    puntata, inquinando la telemetria di calibrazione.
    """
    from tracker import _get_conn
    conn = _get_conn()
    rows = conn.execute(
        "SELECT DISTINCT t.match_id, m.home_team, m.away_team, m.league, "
        "m.commence_time FROM ("
        "  SELECT match_id FROM bets WHERE esito_finale IS NULL"
        "    AND match_id LIKE 'sx-%'"
        "  UNION"
        "  SELECT match_id FROM predictions WHERE esito_finale IS NULL"
        "    AND match_id LIKE 'sx-%'"
        ") t JOIN matches m ON m.id = t.match_id").fetchall()
    conn.close()
    return {r[0]: {"home": r[1], "away": r[2], "league": r[3],
                   "kickoff": r[4]} for r in rows}


def _prediction_market_hashes() -> Dict[str, List[dict]]:
    """`market_hash` SX delle PREVISIONI aperte, letto da `market_quotes`.

    PERCHE' ESISTE (08/10/2026). Il referto nativo leggeva gli hash SOLO dalle
    bet (`bets.market_id`), quindi una partita SENZA puntata non era saldabile
    gratis: la riga restava aperta fino alla scadenza a 5 giorni e veniva
    chiusa come **PUSH** — un verdetto inventato — anche quando SX aveva il
    risultato vero disponibile.

    Misura sul container (08/10/2026) che l'ha motivata: 96 previsioni aperte
    con kickoff nel passato (Brasileirao 36, Nations League 19, ...), quasi
    tutte di leghe che `SETTLEMENT_BETS_ONLY` esclude dal referto PAGATO; il
    `market_hash` e' nell'`extra_json` di `market_quotes` (200/200 righe
    campionate) e `markets/find` risponde 200 con lo score dell'evento anche
    22h dopo il kickoff (Botafogo RJ-CR Vasco da Gama: 1-2).

    Un hash per FIXTURE basta: qualunque mercato dello stesso evento porta il
    punteggio dell'evento, ed e' il punteggio (non la gamba) a decidere il
    verdetto di una previsione (1X2/OU/AH) — la stessa semantica del percorso
    delle bet. I bersagli sono una LISTA perche' bet e previsioni dello stesso
    evento possono avere `match_id` diversi e nessuno dei due va perso.

    Solo lettura, nessuna rete: un errore torna `{}` (il percorso nativo resta
    quello di prima, fail-closed sulle fonti esterne).
    """
    out: Dict[str, List[dict]] = {}
    seen_fixtures: set = set()
    try:
        from tracker import _get_conn
        conn = _get_conn()
        try:
            rows = conn.execute(
                "SELECT q.fixture_id, q.extra_json, m.home_team, m.away_team, "
                "       m.league "
                "FROM market_quotes q "
                "JOIN (SELECT DISTINCT match_id FROM predictions "
                "      WHERE esito_finale IS NULL AND match_id LIKE 'sx-%') p "
                "  ON p.match_id = q.fixture_id "
                "LEFT JOIN matches m ON m.id = q.fixture_id").fetchall()
        finally:
            conn.close()
    except Exception as exc:
        logger.debug("sx_signals: hash delle previsioni non leggibili (%s)", exc)
        return out
    for fixture_id, extra_json, home, away, league in rows:
        mid = str(fixture_id or "")
        if not mid or mid in seen_fixtures:
            continue           # un hash per partita: basta, e costa meno
        try:
            extra = json.loads(extra_json or "{}")
        except Exception:
            continue
        if not isinstance(extra, dict):
            continue
        market_hash = extra.get("market_hash") or extra.get("marketHash")
        if not market_hash:
            continue
        # La chiave e' il `market_hash` (NON il fixture id): e' quello che
        # `markets/find` riceve. Un mercato dello stesso evento ne ha uno
        # diverso per ogni linea, ma il punteggio che ne torna e' quello
        # dell'EVENTO, quindi il primo che capita va bene.
        seen_fixtures.add(mid)
        # `moneyline_ok=False`: una PREVISIONE non prende i punteggi
        # SINTETICI (1,0)/(0,1) dei mercati a 2 esiti — il suo esito e' una
        # stringa di ledger ('Under 3.5', nome giocatore) e un punteggio
        # sintetico su un mercato che non e' un 1X2 sarebbe un verdetto
        # costruito. Si accetta solo il punteggio REALE dell'evento.
        out[str(market_hash)] = [{"mid": mid, "moneyline_ok": False,
                                  "home": home or "", "away": away or "",
                                  "league": league or ""}]
    if out:
        logger.debug("sx_signals: %d partite con sole previsioni hanno un "
                     "market_hash (referto SX-native gratuito)", len(out))
    return out


def _same_event(event: dict, home: str, away: str) -> bool:
    """True se l'evento the-odds-api E' la partita (home, away).

    Tre stadi, dal piu' stretto al piu' tollerante:

      1. `_norm_team`  — nome normalizzato (accenti, fc/cf);
      2. `_loose_team` — tollera i prefissi societari ('CA Osasuna');
      3. `team_names.same_team` — tollera apostrofi, punteggiatura e codici
         di stato: 'Club Cienciano' == 'Cienciano', 'Flamengo-RJ' ==
         'CR Flamengo', 'Vila Nova GO' == 'Vila Nova', "Newell's Old Boys"
         == 'Newells Old Boys'.

    MAI inversione casa/trasferta: l'esito 1/2 dipende dal lato.
    """
    from tracker import _norm_team, _loose_team
    from team_names import same_team
    h = event.get("home_team", "")
    a = event.get("away_team", "")
    if _norm_team(home) == _norm_team(h) and _norm_team(away) == _norm_team(a):
        return True
    if _loose_team(home) == _loose_team(h) and _loose_team(away) == _loose_team(a):
        return True
    return same_team(home, h) and same_team(away, a)


def _results_from_the_odds_api(meta: Dict[str, dict]) -> int:
    """Punteggi via the-odds-api (fetch_scores, match per NOME).

    La lega salvata sul match viene RISOLTA a uno sport key the-odds-api
    (`league_to_sport`: chiave SPORTS_MAP o alias SX) e i match raggruppati
    per sport key. Le leghe non mappabili NON vengono saltate in silenzio:
    restano aperte e vengono loggate (fix mapping leghe 11/09: prima il
    fuzzy mappava alla lega sbagliata e la bet non si saldava mai).

    Il match dei nomi usa `_same_event` (stretto -> loose -> tollerante) e la
    finestra `SCORES_DAYS_FROM` = 3 giorni (massimo dell'API): con 2 giorni
    le partite di due sere prima restavano fuori e la bet non si saldava.

    GUARDIA DI UNICITA' (12/09): il confronto tollerante puo' agganciare piu'
    di una partita ('Manchester' sta in United e City). Se i candidati sono
    piu' di uno la bet RESTA APERTA con un warning: meglio un ritardo nel
    ledger che un verdetto col risultato di un'altra partita.
    """
    from tracker import save_result
    from odds_api import fetch_scores, match_scores_by_name, SCORES_DAYS_FROM
    by_sport: Dict[str, list] = {}
    unmapped = set()
    for mid, info in meta.items():
        lg = info.get("league")
        sport = league_to_sport(lg)
        if not sport:
            unmapped.add(str(lg or "?"))
            continue
        by_sport.setdefault(sport, []).append((mid, info))
    if unmapped:
        logger.warning("sx_signals: settlement — leghe non mappate a "
                       "SPORTS_MAP, bet lasciate aperte: %s",
                       ", ".join(sorted(unmapped)))
    saved = 0
    for sport, items in by_sport.items():
        try:
            scores = fetch_scores(sport, days_from=SCORES_DAYS_FROM)
        except Exception as e:
            logger.warning("sx_signals: fetch_scores %s fallita: %s", sport, e)
            continue
        events = []
        for m in scores:
            parsed = match_scores_by_name(m)
            if parsed is None:
                continue
            events.append((m, parsed[0], parsed[1]))
        for mid, info in items:
            ih, ia = info["home"], info["away"]
            hits = [e for e in events if _same_event(e[0], ih, ia)]
            if not hits:
                continue
            if len(hits) > 1:
                logger.warning(
                    "sx_signals: settlement AMBIGUO per %s (%s vs %s): %d "
                    "partite candidate in %s — bet lasciata aperta",
                    mid, ih, ia, len(hits), sport)
                continue
            _m, sh, sa = hits[0]
            save_result(mid, info.get("league"), ih, ia, sh, sa, "")
            saved += 1
    return saved


def _roster_support(league: str, home: str, away: str) -> int:
    """Quante delle due squadre stanno nel roster di `league` (0, 1 o 2).

    Riusa la risoluzione nomi (`team_names`): 'Atlanta United' aggancia il
    roster 'Atlanta', 'Toronto FC' aggancia 'Toronto'.
    """
    if not league or not home or not away:
        return 0
    try:
        from leagues_data import ALL_LEAGUES
        from team_names import resolve_team
    except Exception:
        return 0
    teams = ALL_LEAGUES.get(league)
    if not teams:
        return 0
    return int(bool(resolve_team(home, teams))) + \
        int(bool(resolve_team(away, teams)))


def _infer_league_from_teams(home: str, away: str,
                             current: Optional[str] = None) -> Optional[str]:
    """Lega dedotta dai roster `ALL_LEAGUES` (fallback del repair).

    Serve per le partite che non sono piu' sui mercati SX (etichetta non
    piu' leggibile). Regola PRUDENTE:

      * si accetta solo se ESATTAMENTE UNA lega contiene ENTRAMBE le squadre;
      * se la lega ATTUALE ha gia' almeno una delle due squadre nel roster,
        non si tocca nulla: un'etichetta SX parziale resta piu' affidabile
        di un'inferenza (es. 'Champions League' con una rosa incompleta).

    Mai indovinare: una lega sbagliata farebbe interrogare the-odds-api sulla
    competizione sbagliata e la bet resterebbe aperta.
    """
    if not home or not away:
        return None
    if current and _roster_support(current, home, away) > 0:
        return None
    try:
        from leagues_data import ALL_LEAGUES
        from team_names import resolve_team
    except Exception:
        return None
    hits = [lg for lg, teams in ALL_LEAGUES.items()
            if resolve_team(home, teams) and resolve_team(away, teams)]
    return hits[0] if len(hits) == 1 else None


def repair_sx_leagues(provider: Optional[SxBetProvider] = None) -> dict:
    """Rimappa la lega delle partite sx-* con le etichette SX CORRENTI.

    Recupero del bug mapping leghe: le partite salvate col fuzzy vecchio
    (es. MLS -> 'League One') non si sarebbero mai saldate. Legge i mercati
    attivi (API pubblica, zero crediti) e riscrive `matches.league` con la
    lega risolta, cosi' il settlement interroga la competizione giusta.

    Per le partite NON piu' attive su SX (il loro mercato e' chiuso e
    l'etichetta non e' piu' leggibile) si usa l'inferenza dai roster: se
    entrambe le squadre stanno in una sola lega di `ALL_LEAGUES`, quella e'
    la lega (fix dei residui storici tipo 8 partite MLS salvate come
    'League One' da una versione precedente del fuzzy).

    Ritorna {checked, updated, inferred}.
    """
    from tracker import _get_conn
    prov = provider or SxBetProvider()
    try:
        # Discovery ampia: il repair deve vedere quante piu' partite attive
        # possibile (ogni partita 1X2 = 3 mercati binari).
        events = _discover(prov, max_markets=3000)
    except Exception as e:
        logger.warning("repair_sx_leagues: discovery fallita: %s", e)
        return {"checked": 0, "updated": 0}
    fresh = {f"sx-{ev['event_id']}": ev["league_label"] for ev in events}
    if not fresh:
        return {"checked": 0, "updated": 0}
    conn = _get_conn()
    rows = conn.execute(
        "SELECT id, league FROM matches WHERE id LIKE 'sx-%'").fetchall()
    conn.close()
    updated = 0
    inferred_count = 0
    for mid, old_league in rows:
        label = fresh.get(mid)
        # Serve comunque home/away per l'inferenza dai roster.
        conn = _get_conn()
        row = conn.execute("SELECT home_team, away_team, commence_time "
                           "FROM matches WHERE id=?", (mid,)).fetchone()
        conn.close()
        if not row:
            continue
        if label:
            new_league = _league_sx_to_sports_map(label) or label
        else:
            # Partita non piu' attiva: si prova a dedurre la lega dal roster
            # (solo se quella attuale NON ha supporto: mai sovrascrivere
            # un'etichetta SX parzialmente coerente).
            inferred = _infer_league_from_teams(row[0], row[1], old_league)
            if not inferred:
                continue
            new_league = inferred
        if new_league != old_league:
            from tracker import save_match
            save_match(mid, new_league, row[0], row[1], row[2])
            logger.info("repair_sx_leagues: %s lega '%s' -> '%s'%s",
                        mid, old_league, new_league,
                        "" if label else " (inferita dai roster)")
            updated += 1
            if not label:
                inferred_count += 1
    return {"checked": len(fresh), "updated": updated,
            "inferred": inferred_count}


# Stati API-Football di partita CONCLUSA: solo questi sono risultati.
# Gli stati in corso (1H/HT/2H/ET/BT/P/LIVE/SUSP...) e non iniziati
# (NS/TBD) NON lo sono: salvarli chiuderebbe la riga col punteggio live
# (stesso bug del 17/09 trovato sul percorso the-odds-api).
_FINISHED_FIXTURE_STATUS = {"FT", "AET", "PEN", "AWD", "WO"}


def _fixture_finished(fx: dict) -> bool:
    """True se API-Football marca la fixture come conclusa.

    Fail-closed: stato assente o non riconosciuto -> False, cosi' la riga
    resta aperta invece di ricevere un verdetto su dati non definitivi.
    """
    status = ((fx.get("fixture") or {}).get("status") or {}).get("short")
    return bool(status) and str(status).upper() in _FINISHED_FIXTURE_STATUS


def _results_from_api_football(meta: Dict[str, dict]) -> int:
    """Punteggi via API-Football per i match ancora senza risultato.

    Una query per (lega, giorno) su /fixtures (league+season+from+to) con
    LEAGUE_IDS di football_hist; le leghe non mappate vengono saltate.
    Ritorna il numero di match_results salvati.
    """
    if not os.getenv("API_FOOTBALL_KEY"):
        return 0
    from tracker import _get_conn, save_result, _norm_team
    import football_hist as fh
    # Salta i match che hanno GIA' un risultato (evita query inutili).
    conn = _get_conn()
    have = {r[0] for r in conn.execute(
        "SELECT match_id FROM match_results").fetchall()}
    conn.close()
    # La lega salvata puo' essere una chiave SPORTS_MAP o un'etichetta SX:
    # la risolvo alla chiave canonica prima di cercarla in LEAGUE_IDS.
    resolved = {mid: (_league_sx_to_sports_map(info.get("league"))
                      or info.get("league")) for mid, info in meta.items()}
    todo = {mid: info for mid, info in meta.items()
            if mid not in have and resolved.get(mid) in fh.LEAGUE_IDS}
    if not todo:
        return 0
    # Raggruppa per (league_id, data kickoff YYYY-MM-DD): una query ciascuno.
    groups: Dict[tuple, list] = {}
    for mid, info in todo.items():
        day = str(info.get("kickoff") or "")[:10]
        league = resolved[mid]
        lid = fh.LEAGUE_IDS[league]
        if day:
            groups.setdefault((league, lid, day), []).append((mid, info))
    saved = 0
    year = datetime.now(timezone.utc).year
    for (league, lid, day), items in groups.items():
        for season in (year, year - 1):
            body = fh._api_get("fixtures", {
                "league": lid, "season": season,
                "from": day, "to": day})
            for fx in (body or {}).get("response") or []:
                if not _fixture_finished(fx):
                    continue   # in corso / non iniziata: mai un risultato finale
                parsed = fh._parse_fixture(fx, league)
                if not parsed:
                    continue
                _fx_id, home_db, away_db, sh, sa, _date = parsed
                for mid, info in items:
                    if (_norm_team(info["home"]) == _norm_team(home_db)
                            and _norm_team(info["away"]) == _norm_team(away_db)):
                        save_result(mid, info["league"], info["home"],
                                    info["away"], sh, sa, "")
                        saved += 1
            # Se abbiamo coperto tutti i match del gruppo, stop (risparmio
            # crediti piano free: niente query sull'anno precedente).
            conn = _get_conn()
            still = conn.execute(
                "SELECT COUNT(*) FROM match_results WHERE match_id IN "
                "(%s)" % ",".join("?" * len(items)),
                tuple(mid for mid, _ in items)).fetchone()[0]
            conn.close()
            if still >= len(items):
                break
    return saved


def _results_from_sx(provider: Optional[SxBetProvider] = None) -> int:
    """Punteggi via SX Bet (FONTE NATIVA, 12/09): l'esito e' quello
    dell'exchange dove il denaro si regola davvero.

    Due percorsi, entrambi GRATUITI (letture pubbliche, zero crediti
    the-odds-api) e senza matching per nome:

    1. BET con market_id salvato sul ledger + PREVISIONI senza puntata (dal
       08/10/2026: il loro `market_hash` vive in `market_quotes.extra_json`,
       vedi `_prediction_market_hashes`): `markets/find` sui mercati
       aperti (batch da SX_FIND_BATCH). Ogni mercato binario porta SEMPRE
       i punteggi dell'evento (teamOneScore/teamTwoScore) e, se saldato,
       anche `outcome` (1 = vince outcomeOne, 2 = vince outcomeTwo,
       0 = void) — la semantica e' relativa alla GAMBA del mercato:
       per il mercato dell'esito scommesso ("T1 vs Not T1") outcome 1
       significa che l'esito scommesso ha vinto. Le colonne home/away
       vengono dalla risposta SX stessa (niente riga `matches` richiesta):
       cosi' si saldano anche le bet ORFANE senza riga nel ledger.

       MERCATI A DUE ESITI (type 52 = tennis / eSports / "12 senza
       pareggio", 03/10/2026): il verdetto si prende dal campo `outcome`,
       non dai punteggi. Sul tennis i `teamOneScore`/`teamTwoScore` sono
       GAME (possono pareggiare con un vincitore: 13-13 su Giron-Baez,
       dove il verdetto vero era `outcome` 2) e sui mercati ritirati non
       arrivano affatto (Mannarino-Basilashvili): in entrambi i casi la
       bet restava aperta o veniva chiusa col verdetto sbagliato. Su un
       mercato a 2 esiti `outcome` e' la risoluzione dell'exchange, quindi
       e' la fonte corretta (sul 1X2 calcio, type 1, resta la regola
       opposta: il verdetto si deriva dai punteggi, mai dalla gamba).
       `outcome` 0 (void) o assente -> fail-closed, nessuna chiusura.

    2. MATCH aperti (bet o sole previsioni) con riga nel ledger:
       `/markets/active` (type 1, sportId 5, paginato come `_discover`)
       espone ancora gli eventi IN CORSO (fino a ~+1h dal kickoff) con
       i punteggi live: copre le partite di oggi prima che il risultato
       arrivi da the-odds-api, incluse le leghe non mappate.

    `markets/find` ritorna i punteggi anche per mercati NON ancora
    saldati (stesso evento): si salva il punteggio ma il verdetto lo
    emette comunque settle_bets/settle_predictions (fail-closed).
    """
    if os.getenv("SX_NATIVE_SETTLEMENT", "1").strip().lower() in (
            "0", "false", "off"):
        return 0   # disattivabile da env (test offline / fallback esterno)
    from tracker import save_result, _get_conn
    prov = provider or SxBetProvider()
    conn = _get_conn()
    try:
        # IL FILTRO E' IL `market_id`, NON IL PREFISSO `sx-` (10/10/2026).
        # Il referto gratuito si aggancia al MARKET HASH, non al match_id:
        # i nomi e i punteggi arrivano dalla risposta di SX
        # (`teamOneName`/`teamTwoScore`), quindi la riga `matches` non serve.
        # Pretendere `match_id LIKE 'sx-%'` escludeva le bet piazzate su
        # partite con l'id DI THE-ODDS-API, che pure portano il `market_id`
        # dell'ordine: restavano appese per ore al percorso ESTERNO (pagato,
        # con finestra 3 giorni e cache punteggi TTL 24h) pur essendo
        # saldabili GRATIS all'istante. Caso reale misurato: la bet #31
        # Arsenal-Leeds (id the-odds-api, con market hash SX) risolta e VINTA
        # su SX mentre il ledger la teneva aperta — con lo stake contato
        # DUE volte nell'equity (payout in `available` + stake ancora
        # "aperto"), cioe' un capitale per il sizing piu' alto del reale.
        # Il `market_id` resta il discriminante: una bet senza hash (SIM,
        # o provider non-SX) non entra nel batch di `markets/find`.
        rows = conn.execute(
            "SELECT match_id, market_id, selection_id, esito FROM bets "
            "WHERE mode='live' AND esito_finale IS NULL "
            "AND market_id IS NOT NULL AND market_id != ''").fetchall()
    finally:
        conn.close()
    saved = 0
    # market_hash -> LISTA dei bersagli da saldare. Una lista e non una tupla
    # singola perche' lo stesso mercato puo' servire piu' righe dello stesso
    # evento: le bet lo facevano gia', e dal 08/10/2026 anche le sole
    # PREVISIONI portano il proprio hash (letto da `market_quotes`, non dal
    # ledger delle puntate).
    by_hash: Dict[str, List[dict]] = {}
    for mid, mkt_id, sel, esito in rows:
        entries = by_hash.setdefault(mkt_id, [])
        if not any(e["mid"] == mid for e in entries):
            entries.append({"mid": mid, "sel": sel, "esito": esito,
                            "home": "", "away": "", "league": "",
                            "moneyline_ok": True})
    # Previsioni SENZA puntata: stesso percorso gratuito. Era il buco che
    # lasciava le righe aperte fino alla scadenza push a 5 giorni.
    for _mh, _targets in _prediction_market_hashes().items():
        entries = by_hash.setdefault(_mh, [])
        for _t in _targets:
            if not any(e["mid"] == _t["mid"] for e in entries):
                entries.append(_t)

    def _save(mid, league_label, home, away, sh, sa):
        nonlocal saved
        save_result(mid, league_label, home, away, sh, sa,
                    datetime.now(timezone.utc).isoformat())
        saved += 1

    # --- 1. mercati delle bet aperte E delle sole previsioni (find, a batch) ---
    # GUARDIA DI CONCLUSIONE (17/09): lo stesso principio del percorso 2 —
    # un evento che non ha ancora finito di giocare NON ha un punteggio
    # finale. Se SX popolasse i punteggi live anche qui, salvarli chiuderebbe
    # la bet a partita in corso (il bug osservato il 17/09 sul percorso
    # the-odds-api); la soglia e' la stessa (`SX_LIVE_MIN_AGE_MS`, 120').
    now_ms = _now_ms()
    hashes = list(by_hash.keys())
    for i in range(0, len(hashes), SX_FIND_BATCH):
        batch = hashes[i:i + SX_FIND_BATCH]
        try:
            data = prov._get("markets/find", params={
                "marketHashes": ",".join(batch)})
        except Exception as e:
            logger.warning("sx_signals: settlement SX find fallita: %s", e)
            continue
        arr = data.get("data") if isinstance(data, dict) else None
        if not isinstance(arr, list):
            continue
        for m in arr:
            if not isinstance(m, dict):
                continue
            mh = m.get("marketHash")
            targets = by_hash.get(mh)
            if not targets:
                continue
            ko = _kickoff_utc_ms(m.get("gameTime"))
            if ko is None or ko > now_ms - SX_LIVE_MIN_AGE_MS:
                continue   # non conclusa: il punteggio puo' ancora cambiare
            home = m.get("teamOneName") or ""
            away = m.get("teamTwoName") or ""
            try:
                mtype = int(m.get("type"))
            except (TypeError, ValueError):
                mtype = 0
            try:
                outcome = int(m.get("outcome"))
            except (TypeError, ValueError):
                outcome = None
            # MERCATO A DUE ESITI (type 52: tennis, eSports, "12 senza
            # pareggio"): qui il campo `outcome` SALDATO DALL'EXCHANGE e' il
            # verdetto, non un dato da derivare dai punteggi. Non e'
            # un'alternativa, e' l'unica fonte corretta:
            #  - il tennis NON porta punteggi sui mercati ritirati (bet #14
            #    Mannarino-Basilashvili: outcome 1, zero score) e resterebbe
            #    aperta fino alla scadenza push, con P/L 0 inventato al posto
            #    della perdita reale;
            #  - quando li porta sono GAME, e i game possono pareggiare con un
            #    vincitore (bet #19 Giron-Baez: 13-13 ma outcome 2 — la bet era
            #    VINTA, registrata persa dai punteggi).
            # La semantica resta RELATIVA ALLA GAMBA (1 = vince outcomeOne),
            # che su un mercato a 2 esiti e' esattamente cio' che serve; sul
            # 1X2 calcio (type 1) la regola "mai dal campo outcome" resta
            # intatta. I punteggi canonici (1,0)/(0,1) servono solo a far
            # derivare il verdetto a settle_bets/_prediction_outcome.
            # outcome 0 (void) o assente -> fail-closed: nessuna chiusura qui
            # (la scadenza la chiude come push, che per un void e' corretto).
            ev_id = m.get("sportXeventId")
            for t in targets:
                mid = t["mid"]
                # GUARDIA DI EVENTO (08/10/2026): i bersagli che NON vengono
                # dal ledger delle puntate sono stati scelti per HASH, quindi
                # si verifica che il mercato appartenga davvero a QUELLA
                # partita prima di salvargli il punteggio (`sx-<eventId>` e'
                # la chiave del ledger). Senza la verifica un hash sbagliato
                # salderebbe la previsione col risultato di un altro evento.
                if not t.get("moneyline_ok") and ev_id and \
                        f"sx-{ev_id}" != str(mid):
                    continue
                t_home = t.get("home") or ""
                t_away = t.get("away") or ""
                label = m.get("leagueLabel") or t.get("league") or ""
                if (t.get("moneyline_ok") and mtype == SX_MONEYLINE_TYPE
                        and outcome in (1, 2) and home and away):
                    sh, sa = (1, 0) if outcome == 1 else (0, 1)
                    _save(mid, label, home, away, sh, sa)
                    continue
                sh, sa = m.get("teamOneScore"), m.get("teamTwoScore")
                if isinstance(sh, int) and isinstance(sa, int) \
                        and (home or t_home) and (away or t_away):
                    _save(mid, label, home or t_home, away or t_away, sh, sa)

    # --- 2. partite aperte con riga nel ledger (active, punteggi live) ---
    meta = _sx_open_matches()
    if meta:
        now_ms = _now_ms()
        raw: List[dict] = []
        pagination_key: Optional[str] = None
        while len(raw) < MAX_RAW_MARKETS:
            params: Dict = {"sportIds": "5", "type": "1", "pageSize": 100}
            if pagination_key:
                params["paginationKey"] = pagination_key
            try:
                data = prov._get("markets/active", params=params)
            except Exception as e:
                logger.warning("sx_signals: settlement SX active fallita: %s", e)
                break
            d = data.get("data") if isinstance(data, dict) else {}
            mkts = (d or {}).get("markets") or []
            raw.extend(m for m in mkts if isinstance(m, dict))
            pagination_key = (d or {}).get("nextKey")
            if not pagination_key or not mkts:
                break
        for m in raw:
            ev_id = m.get("sportXeventId")
            if not ev_id:
                continue
            mid = f"sx-{ev_id}"
            if mid not in meta:
                continue
            ko = _kickoff_utc_ms(m.get("gameTime"))
            if ko is None or ko > now_ms - SX_LIVE_MIN_AGE_MS:
                continue  # futura o ancora in gioco: punteggio non affidabile
            sh, sa = m.get("teamOneScore"), m.get("teamTwoScore")
            if isinstance(sh, int) and isinstance(sa, int):
                _save(mid, m.get("leagueLabel") or "",
                      m.get("teamOneName") or meta[mid].get("home") or "",
                      m.get("teamTwoName") or meta[mid].get("away") or "",
                      sh, sa)
    if saved:
        logger.info("sx_signals: settlement SX nativo — %d punteggi salvati",
                    saved)
    return saved


def settle_sx_bets(provider: Optional[SxBetProvider] = None) -> dict:
    """Salda le bet SX aperte: risultati (fonti esterne) + settle_bets.

    Le bet senza risultato disponibile restano aperte (fail-closed: mai
    chiudere un verdetto senza punteggio reale).

    PAUSA SETTLEMENT (11/09): con la pausa attiva si esce PRIMA di
    interrogare le fonti punteggi — nessuna chiusura e nessun credito
    the-odds-api/API-Football consumato.

    FONTE NATIVA SX (12/09): `_results_from_sx` legge gli esiti saldati
    DALL'EXCHANGE (markets/find sui market_hash delle bet + punteggi live
    su markets/active) — gratis, senza matching per nome e incluse le
    leghe fuori SPORTS_MAP. Solo per i match ancora scoperti si passa a
    the-odds-api e poi ad API-Football.
    """
    from tracker import settle_bets, settle_predictions, settlement_paused
    if settlement_paused():
        logger.info("sx_signals: settlement in PAUSA — nessun download punteggi")
        return {"open": 0, "results": 0, "settled": 0, "source": None,
                "paused": True}
    results = 0
    source = None
    # La fonte nativa SX segue la stessa logica delle altre: attiva solo se
    # il sistema e' configurato per refertare (almeno una fonte punteggi).
    # Con entrambe le chiavi ASSENTI si resta fail-closed offline (i test
    # del settlement non devono toccare la rete).
    if (os.getenv("ODDS_API_KEY") or os.getenv("API_FOOTBALL_KEY")) \
            and os.getenv("SX_NATIVE_SETTLEMENT", "1").strip().lower() not in (
                "0", "false", "off"):
        try:
            n = _results_from_sx(provider)
            if n:
                results, source = n, "sx"
        except Exception as e:
            logger.warning("sx_signals: settlement SX nativo fallito: %s", e)
    meta = _sx_open_matches()
    if meta:
        # Solo i match ANCORA senza risultato sx-* passano alle fonti
        # esterne (crediti): copre i match con sole previsioni, che il
        # percorso (1) non vede perche' non ha market_hash nel ledger.
        from tracker import _get_conn as _conn, _create_results_table
        conn = _conn()
        try:
            _create_results_table(conn)
            have = {r[0] for r in conn.execute(
                "SELECT match_id FROM match_results WHERE match_id LIKE 'sx-%'")}
        finally:
            conn.close()
        missing = {mid for mid in meta if mid not in have}
        # REFERTO SEGUE IL DENARO (04/10/2026). Il percorso SX-native (sopra,
        # gratis) copre anche le sole previsioni; le fonti ESTERNE sono
        # PAGATE (`fetch_scores` = 2 crediti/call) e devono seguire la stessa
        # politica `SETTLEMENT_BETS_ONLY` del watchdog (15/09/2026): si
        # interroga la lega di una partita solo se quella partita ha una
        # PUNTATA aperta. Senza questo filtro ogni previsione `sx-*` aperta
        # faceva interrogare la sua lega a ogni scadenza di cache punteggi
        # (~20 leghe x 2 crediti al giorno: era il consumo dominante dei
        # 46,5 crediti/giorno, mentre il watchdog da solo ne usava ~0).
        paid_ids: Optional[set] = None
        try:
            from tracker import _settlement_bets_only, open_bet_match_ids
            if _settlement_bets_only():
                paid_ids = open_bet_match_ids()
                _skipped = len(missing) - len(missing & paid_ids)
                if _skipped:
                    logger.info(
                        "sx_signals: settlement fonti esterne — %d partite "
                        "senza puntata saltate (referto segue il denaro); "
                        "%d con puntata", _skipped, len(missing & paid_ids))
        except Exception as e:
            # Fail-OPEN dichiarato: un errore di lettura NON deve impedire di
            # saldare una puntata reale (il denaro viene prima del risparmio).
            # `paid_ids` resta None -> politica estesa come prima del fix.
            logger.warning("sx_signals: filtro referto-segue-il-denaro non "
                           "valutabile (%s) — si interrogano tutte le partite "
                           "aperte (fail-open: mai perdere un referto)", e)
        paid_missing = {mid: meta[mid] for mid in missing
                        if paid_ids is None or mid in paid_ids}
        if paid_missing and os.getenv("ODDS_API_KEY"):
            try:
                n = _results_from_the_odds_api(paid_missing)
                if n:
                    results += n
                    source = source or "the-odds-api"
            except Exception as e:
                logger.warning(
                    "sx_signals: settlement the-odds-api fallito: %s", e)
        if not results and os.getenv("API_FOOTBALL_KEY"):
            paid_meta = {mid: info for mid, info in meta.items()
                         if paid_ids is None or mid in paid_ids}
            try:
                n = _results_from_api_football(paid_meta)
                if n:
                    results, source = n, "api-football"
            except Exception as e:
                logger.warning(
                    "sx_signals: settlement api-football fallito: %s", e)
    settled, pushes = settle_bets()
    # Anche le PREVISIONI: il risultato appena salvato ha la chiave sx-<id>,
    # quindi `settle_predictions` le chiude per match_id. Senza questo passo
    # restavano aperte fino al watchdog successivo (o per sempre, se il
    # match non aveva bet).
    pred_n, pred_pushes = settle_predictions()
    # SCADENZA (12/09): ULTIMA operazione — le righe sx-* rimaste aperte
    # oltre SX_STALE_DAYS giorni dal kickoff SENZA alcun risultato si chiudono
    # come push (P/L 0): senza questo passo entravano in `missing` a ogni giro
    # e provocavano fetch_scores (crediti) che non le avrebbe MAI trovate
    # (la finestra the-odds-api e' di 3 giorni). Dopo le fonti: se una fonte
    # ha appena salvato il risultato, la riga e' gia' chiusa col verdetto vero.
    try:
        from tracker import expire_stale_sx_rows
        expired = expire_stale_sx_rows()
    except Exception as e:
        logger.warning("sx_signals: scadenza righe sx-* fallita: %s", e)
        expired = {"bets": 0, "predictions": 0}
    return {"open": len(meta), "results": results, "settled": settled,
            "pushes": pushes, "predictions": pred_n,
            "prediction_pushes": pred_pushes, "source": source,
            "expired": expired}


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if len(sys.argv) > 1 and sys.argv[1] == "settle":
        res = settle_sx_bets()
        print(f"✅ settlement SX: {res}")
    elif len(sys.argv) > 1 and sys.argv[1] == "repair":
        res = repair_sx_leagues()
        print(f"🔧 repair leghe SX: {res}")
    else:
        sig = scan()
        print(f"✅ {len(sig)} segnali value salvati")
        for s in sig:
            print(f"• {s['home']} vs {s['away']} — {s['esito']} @ "
                  f"{s['quota']:.2f} (EV {s['ev'] * 100:+.1f}%) [{s['status']}]")
