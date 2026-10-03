import json, os, time, logging, requests
from datetime import datetime, timedelta, timezone
from pathlib import Path
from config import DATA_DIR, load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

CACHE_DIR = DATA_DIR
ODDS_TTL = 86400          # cache 24h = 1 chiamata/giorno per lega
MIN_REMAINING = 20        # stop sotto 20 crediti
# Cache punteggi non attendibile per il settlement oltre queste ore dal
# kickoff se la partita e' ancora completed=False (artefatto di una cache
# scritta mentre la partita era in corso: un match di calcio finisce entro
# ~2h). BUG 11/09: la costante era USATA in _cache_is_stale_for_settlement
# ma MAI DEFINITA -> NameError inghiottito dall'except -> la cache stantia
# veniva considerata valida e il settlement restava bloccato (bet aperte).
STALE_INPLAY_HOURS = 3

# Soglie proattive per rotazione intelligente
CREDIT_LOW = 50           # sotto 50: disattiva leghe non-core (intervallo >7gg)
CREDIT_CRITICAL = 30      # sotto 30: solo top 6 leghe core
CREDIT_EMERGENCY = 15     # sotto 15: solo Serie A, PL, La Liga

# --- ORACOLO A LINEA OU/AH (30/09/2026, decisione del proprietario) --------
# I pick OU/AH muoiono con `no_oracle`: il gate top-down legge un oracolo
# 1X2 che non ha MAI le chiavi 'Over 2.5' / 'Home -0.75'. L'oracolo a linea
# richiede i mercati `totals,spreads` di Pinnacle, e the-odds-api addebita
# `markets x regions` per chiamata: metterli sulla rotazione di RICERCA
# triplicherebbe OGNI chiamata (370.6 -> ~938 crediti/mese, fuori dal tetto
# 460 anche tagliando la rotazione a 7gg). Design scelto (follow-the-money):
# la rotazione di ricerca resta `h2h` (1 credito), e una SECONDA chiamata a
# `markets=h2h,totals,spreads` (3 crediti) viene fatta SOLO per le leghe con
# pick OU/AH aperti in finestra d'ordine — si paga la linea SOLO dove c'e'
# denaro in gioco. La cache oracolo e' SEPARATA (`toao_<sport>.json`): la
# cache di ricerca resta h2h-only per `fixture_engine`/`pinnacle_oracle` 1X2,
# la cache oracolo serve a `line_oracle.py` (scelta dichiarata: le due letture
# hanno consumatori diversi e un file unico confonderebbe le forme).
ORACLE_MARKETS_LIST = "h2h,totals,spreads"   # 3 crediti a chiamata (eu)
ORACLE_EXTRA_CREDITS = 2                     # over h2h (1 gia' contato a parte)
ORACLE_CACHE_PREFIX = "toao_"
# FINESTRA DI FETCH dell'oracolo a linea, in MINUTI (03/10/2026, direttiva del
# proprietario). Si ordina SOLO nella finestra esecutiva T-60..T-5: chiedere
# l'INTERO palinsesto della lega (era 24h) significa scaricare e parsare
# decine di partite che non entreranno MAI in finestra d'ordine. 70 minuti
# coprono la finestra con margine.
# ⚠️ Il costo the-odds-api e' per CHIAMATA, non per evento: restringere la
# finestra NON riduce i crediti, riduce il PAYLOAD (byte/parsing). Per questo
# la TTL della cache e' ALLINEATA alla finestra (`oracle_cache_ttl_s()`): una
# cache da 70 minuti tenuta "fresca" 24h sarebbe valida ma vuota delle
# partite che stanno entrando in finestra.
ORACLE_FETCH_WINDOW_MIN = 70


def oracle_fetch_window_min() -> int:
    """Finestra di fetch dell'oracolo a linea (minuti), letta a RUNTIME.

    Env `ORACLE_FETCH_WINDOW_MIN`: un valore assente o impossibile ricade sul
    default dichiarato (una guardia non si spegne con un env sbagliato).
    """
    raw = os.getenv("ORACLE_FETCH_WINDOW_MIN")
    if raw is None or not str(raw).strip():
        return ORACLE_FETCH_WINDOW_MIN
    try:
        val = int(float(raw))
    except (TypeError, ValueError):
        logger.warning("oracolo a linea: ORACLE_FETCH_WINDOW_MIN=%r non "
                       "numerico, uso %s", raw, ORACLE_FETCH_WINDOW_MIN)
        return ORACLE_FETCH_WINDOW_MIN
    if val <= 0:
        logger.warning("oracolo a linea: ORACLE_FETCH_WINDOW_MIN=%r non "
                       "positivo, uso %s", raw, ORACLE_FETCH_WINDOW_MIN)
        return ORACLE_FETCH_WINDOW_MIN
    return val


def oracle_cache_ttl_s() -> int:
    """TTL della cache `toao_*`, ALLINEATO alla finestra di fetch.

    Una cache scritta con una finestra di 70 minuti copre SOLO
    `[ts, ts + finestra]`: considerarla fresca 24h (com'era prima del
    03/10/2026) significherebbe fidarsi di un payload che non contiene piu'
    le partite in ingresso in finestra — il gate top-down ripiegherebbe su
    `linea`/`no_oracle` pur avendo una cache "valida". Finestra e TTL sono
    percio' la STESSA grandezza, con una sola env a governarle.
    """
    return oracle_fetch_window_min() * 60
# TETTO di leghe fetchate al giorno (3 crediti l'una = 6 crediti/giorno =
# 180/mese): con la rotazione a 7gg (~169 crediti/mese) il totale ~349 resta
# sotto il tetto 460 del piano free con margine per /scores. E' un TETTO,
# non un target: il consumo reale segue i pick in gioco (~59 righe OU/AH
# giocabili chiuse/giorno su ~2-4 leghe distinte). Env per alzarlo (ogni
# aumento va verificato contro test_budget_mensile_piano_free).
ORACLE_BUDGET_DAY = int(os.getenv("ORACLE_BUDGET_DAY", "2"))
ORACLE_ENABLED = os.getenv("ORACLE_ENABLED", "1").strip().lower() \
    in ("1", "true", "yes", "on")
_oracle_req_day = {"day": None, "n": 0}
# HARD STOP (direttiva del proprietario, 21/09/2026): sotto questa soglia
# NESSUNA chiamata HTTP verso the-odds-api, indipendentemente dalla
# rotazione ridotta. Il piano free risponde 429 quando i crediti finiscono e
# le 3 leghe "di emergenza" brucerebbero l'ultimo credito riempiendo i log di
# errori: da qui in giu' si usano SOLO le cache gia' presenti.
CREDIT_HARD_STOP = int(os.getenv("ODDS_CREDIT_HARD_STOP", "5"))
# ...ma una telemetria VECCHIA non e' una telemetria VALIDA: la cache si
# aggiorna solo con una chiamata, e le chiamate sono bloccate — una chiave
# sostituita o il reset mensile del piano non si vedrebbero MAI (blocco
# eterno). Oltre questa finestra la cache sotto soglia non basta piu' a
# bloccare: si lascia passare UN probe (la risposta 429 non consuma crediti e
# riporta il contatore fresco).
CREDIT_HARD_STOP_MAX_AGE_H = float(os.getenv("ODDS_CREDIT_PROBE_HOURS", "6"))

CORE_LEAGUES_HIGH = {"soccer_italy_serie_a", "soccer_england_pl", "soccer_spain_la_liga",
                      "soccer_germany_bundesliga", "soccer_france_ligue_one",
                      "soccer_efl_champ"}
CORE_LEAGUES_EMERGENCY = {"soccer_italy_serie_a", "soccer_england_pl", "soccer_spain_la_liga"}

def _latest_credits_detail():
    """(crediti, ts_lettura, n_cache) della lettura PIU' RECENTE.

    Ogni risposta dell'API riporta lo stesso contatore autoritativo
    (`x-requests-remaining`), quindi vale l'ULTIMA lettura per data — non il
    MINIMO tra le cache. Con il minimo un file vecchio inchioda il valore per
    settimane (12/09: la chiave nuova riportava 452 crediti ma le cache quote
    scritte con la chiave vecchia tenevano il contatore a 58, cosi' la
    rotazione veniva throttled come se i crediti fossero quasi finiti).

    Si usa `remaining_ts` (istante della LETTURA del credito) quando presente,
    altrimenti `ts`: in `fetch_scores` il `ts` della cache puo' essere
    preservato da un giro precedente, mentre `remaining_ts` e' sempre il
    momento della chiamata che ha prodotto quel valore. `ts_lettura` e' None
    per le cache di formato vecchio (senza timestamp): il chiamante che ha
    bisogno dell'ETA' (il blocco crediti) deve saperlo.
    """
    best = None            # (timestamp lettura credito, remaining)
    fallback = []
    n = 0
    if CACHE_DIR.exists():
        for f in CACHE_DIR.glob("toa_*.json"):
            try:
                d = json.loads(f.read_text())
                if d.get("remaining") is None:
                    continue
                rem = int(d["remaining"])
                n += 1
                ts = d.get("remaining_ts", d.get("ts"))
                if isinstance(ts, (int, float)):
                    if best is None or ts > best[0]:
                        best = (ts, rem)
                else:
                    fallback.append(rem)
            except Exception:
                continue
    if best is not None:
        return best[1], best[0], n
    if fallback:
        return min(fallback), None, n
    return None, None, 0


def _latest_credits():
    """(crediti_residui, n_cache) secondo la lettura PIU' RECENTE."""
    rem, _ts, n = _latest_credits_detail()
    return rem, n


def get_remaining() -> int:
    """Crediti residui dall'ultima lettura (None se non c'e' telemetria)."""
    rem, _ = _latest_credits()
    return rem

def should_query_sport(sport_key: str) -> bool:
    """Decide se una sport key deve essere interrogata in base ai crediti.

    Logica proattiva (09/09):
    - remaining >= 50: tutto attivo (default)
    - remaining < 50: solo leghe core (intervallo <= 7gg)
    - remaining < 30: solo top 6 leghe core
    - remaining < 15: solo Serie A, PL, La Liga

    Previene il rischio di esaurire i crediti a fine mese
    senza preavviso.
    """
    rem = get_remaining()
    if rem is None:
        return True  # no cache data: assume ok
    if rem >= CREDIT_LOW:
        return True
    # Sotto soglia: verifica la lega
    for lg, key in SPORTS_MAP.items():
        if key == sport_key:
            interval = SPORTS_INTERVAL_DAYS.get(lg, 7)
            if rem >= CREDIT_CRITICAL:
                # Solo leghe con intervallo <= 7gg (core)
                return interval <= 7
            elif rem >= CREDIT_EMERGENCY:
                # Solo top 6 leghe core
                return lg in CORE_LEAGUES_HIGH
            else:
                # Emergenza: solo top 3
                return lg in CORE_LEAGUES_EMERGENCY
    return True  # sport non mappato: allow (probabilmente tennis subet)


_credit_stop_logged = False
_credit_probe_logged = False


def credits_hard_stopped() -> bool:
    """True se i crediti residui sono SOTTO la soglia di blocco TOTALE.

    Direttiva del proprietario (21/09/2026): sotto `ODDS_CREDIT_HARD_STOP`
    (default 5) la rotazione ridotta (`should_query_sport`) non basta piu' —
    il piano free risponde 429 a crediti esauriti e le 3 leghe "di
    emergenza" brucerebbero l'ultimo credito riempiendo i log di errori.
    Da qui in giu' NESSUNA chiamata HTTP verso the-odds-api: quote e
    punteggi vengono serviti solo dalle cache gia' presenti.

    Due fail-safe sull'INFORMAZIONE, opposte al blocco:
    - nessuna telemetria (nessuna cache `toa_*.json`, o senza timestamp) ->
      non si blocca: senza sapere quanto resta non si ferma tutto (stessa
      direzione di `should_query_sport`).
    - telemetria sotto soglia ma PIU' VECCHIA di `CREDIT_HARD_STOP_MAX_AGE_H`
      (default 6h) -> non si blocca: il valore puo' essere obsoleto (chiave
      sostituita o reset mensile) e con il blocco attivo nessuna chiamata
      aggiornerebbe mai la cache (blocco eterno). Passa UN probe, che la
      risposta 429 riporta al costo di zero crediti.

    I warning escono UNA volta per processo (rotazione e watchdog passano di
    qui di continuo).
    """
    global _credit_stop_logged, _credit_probe_logged
    rem, ts, _n = _latest_credits_detail()
    if rem is None:
        return False
    if rem >= CREDIT_HARD_STOP:
        _credit_stop_logged = False
        _credit_probe_logged = False
        return False
    age_h = None if ts is None else (time.time() - ts) / 3600.0
    if age_h is None or age_h > CREDIT_HARD_STOP_MAX_AGE_H:
        if not _credit_probe_logged:
            logger.warning(
                "the-odds-api: crediti %s < soglia %s ma telemetria vecchia "
                "(%s) — un PROBE per rileggere i crediti (chiave o piano "
                "possono essere cambiati)", rem, CREDIT_HARD_STOP,
                "mai letta" if age_h is None else f"{age_h:.1f}h")
            _credit_probe_logged = True
        return False
    if not _credit_stop_logged:
        logger.warning(
            "the-odds-api: crediti %s < soglia %s — TUTTE le chiamate HTTP "
            "bloccate (solo cache) fino al reset", rem, CREDIT_HARD_STOP)
        _credit_stop_logged = True
    return True


def _cache_is_stale_for_settlement(payload: list) -> bool:
    """True se la cache punteggi non e' attendibile per il settlement.

    Serve a _scores_from_cache (via fetch_scores): una partita iniziata da
    oltre STALE_INPLAY_HOURS ma con completed=False e' un artefatto di una
    cache troppo vecchia, non un dato reale (una partita di calcio finisce
    entro ~2h dal kickoff; oltre quelle ore 'completed=False' significa
    'il risultato non era ancora disponibile quando la cache e' stata scritta').
    """
    if not payload:
        return False
    now = time.time()
    for m in payload:
        if m.get("completed"):
            continue
        scores = m.get("scores") or []
        if len(scores) >= 2:
            continue
        try:
            commence = (m.get("commence_time") or "").replace("Z", "+00:00")
            start = datetime.fromisoformat(commence)
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            kickoff_age_h = (now - start.timestamp()) / 3600.0
            if kickoff_age_h > STALE_INPLAY_HOURS:
                return True  # partita finita da ore ma cache dice 'in corso'
        except Exception:
            continue
    return False

def match_scores_by_name(m):
    """Punteggi (home, away) di un match the-odds-api associati per NOME.

    L'array `scores` NON ha ordine garantito: ogni elemento ha `name`
    (e opzionale `key`) da confrontare con home_team/away_team. NON si puo'
    assumere che scores[0] sia la squadra di casa: il 02/09 FC Machida
    Zelvia vs Kawasaki Frontale e' stato saldato con i punteggi invertiti
    (bet sul 2 segnata vinta per una vittoria casalinga).

    SOLO PARTITE CONCLUSE (bug 17/09/2026, `completed`). the-odds-api
    restituisce anche le partite IN CORSO con i punteggi live popolati: la
    cache ne conteneva 12 e la refertazione le salvava come risultati finali,
    chiudendo le bet ~12 minuti dopo il kickoff (bet #39-#41 del 15/09: una
    partita finita 3-1 e' stata saldata '0-0 → X'). Senza `completed`
    veritiero la funzione non emette punteggi: fail-closed, la riga resta
    aperta invece di ricevere un verdetto su un punteggio che puo' ancora
    cambiare. Il campo mancante vale come NON conclusa (stessa direzione).

    Returns:
        (score_home, score_away) oppure None se i punteggi non sono
        associabili con certezza (dati parziali, nomi non corrispondenti o
        partita non conclusa).
    """
    if not m.get("completed"):
        return None
    scores = m.get("scores") or []
    home = str(m.get("home_team") or "").strip().lower()
    away = str(m.get("away_team") or "").strip().lower()
    if not home or not away:
        return None
    sh = sa = None
    for s in scores:
        name = str(s.get("name") or "").strip().lower()
        key = str(s.get("key") or "").strip().lower()
        try:
            val = int(s.get("score"))
        except (TypeError, ValueError):
            continue
        if name == home or (key and key == home):
            sh = val
        elif name == away or (key and key == away):
            sa = val
    if sh is None or sa is None:
        return None
    return sh, sa


# TUTTE le competizioni di calcio coperte da the-odds-api (chiavi ufficiali
# verificate su the-odds-api.com/sports-apis). Le squadre senza roster in
# leagues_data usano il profilo di lega di default (expected_goals).
SPORTS_MAP = {
    # Campionati top + serie B
    "Serie A": "soccer_italy_serie_a", "Serie B": "soccer_italy_serie_b",
    "Premier League": "soccer_epl", "EFL Championship": "soccer_efl_champ",
    "League One": "soccer_england_league1", "League Two": "soccer_england_league2",
    "La Liga": "soccer_spain_la_liga", "La Liga 2": "soccer_spain_segunda_division",
    "Bundesliga": "soccer_germany_bundesliga", "Bundesliga 2": "soccer_germany_bundesliga2",
    "3. Liga": "soccer_germany_liga3", "Frauen-Bundesliga": "soccer_germany_bundesliga_women",
    "Ligue 1": "soccer_france_ligue_one", "Ligue 2": "soccer_france_ligue_two",
    "Eredivisie": "soccer_netherlands_eredivisie", "Primeira Liga": "soccer_portugal_primeira_liga",
    "Scottish Premiership": "soccer_spl", "Austrian Bundesliga": "soccer_austria_bundesliga",
    "Belgian First Div": "soccer_belgium_first_div", "Greek Super League": "soccer_greece_super_league",
    "Polish Ekstraklasa": "soccer_poland_ekstraklasa", "Russian Premier League": "soccer_russia_premier_league",
    "Turkey Super Lig": "soccer_turkey_super_league", "Swiss Super League": "soccer_switzerland_superleague",
    "Superliga Danimarca": "soccer_denmark_superliga", "Allsvenskan": "soccer_sweden_allsvenskan",
    "Sweden Superettan": "soccer_sweden_superettan", "Eliteserien": "soccer_norway_eliteserien",
    "Veikkausliiga": "soccer_finland_veikkausliiga", "League of Ireland": "soccer_league_of_ireland",
    "China Super League": "soccer_china_superleague", "J1 League": "soccer_japan_j_league",
    "K League 1": "soccer_korea_kleague1", "A-League": "soccer_australia_aleague",
    # Americhe
    "MLS": "soccer_usa_mls", "Brasileirao": "soccer_brazil_campeonato",
    "Brazil Serie B": "soccer_brazil_serie_b", "Liga MX": "soccer_mexico_ligamx",
    "Saudi Pro League": "soccer_saudi_arabia_pro_league",
    "Argentina Primera": "soccer_argentina_primera_division",
    "Chile Primera": "soccer_chile_campeonato",
    # Coppe europee
    "Champions League": "soccer_uefa_champs_league",
    "Europa League": "soccer_uefa_europa_league",
    "Conference League": "soccer_uefa_europa_conference_league",
    "UCL Qualification": "soccer_uefa_champs_league_qualification",
    "UEFA Women's Champions League": "soccer_uefa_champs_league_women",
    "Coppa Italia": "soccer_italy_coppa_italia",
    "Copa del Rey": "soccer_spain_copa_del_rey",
    "Coupe de France": "soccer_france_coupe_de_france",
    "DFB Pokal": "soccer_germany_dfb_pokal",
    "FA Cup": "soccer_fa_cup", "EFL Cup": "soccer_england_efl_cup",
    # Coppe internazionali + nazionali
    "Copa Libertadores": "soccer_conmebol_copa_libertadores",
    "Copa Sudamericana": "soccer_conmebol_copa_sudamericana",
    "Copa America": "soccer_conmebol_copa_america",
    "CONCACAF Gold Cup": "soccer_concacaf_gold_cup",
    "CONCACAF Leagues Cup": "soccer_concacaf_leagues_cup",
    "Africa Cup of Nations": "soccer_africa_cup_of_nations",
    "FIFA Club World Cup": "soccer_fifa_club_world_cup",
    "FIFA World Cup": "soccer_fifa_world_cup",
    "FIFA World Cup Qualifiers Europe": "soccer_fifa_world_cup_qualifiers_europe",
    "FIFA World Cup Qualifiers S.America": "soccer_fifa_world_cup_qualifiers_south_america",
    "FIFA Women's World Cup": "soccer_fifa_world_cup_womens",
    "UEFA Euro": "soccer_uefa_european_championship",
    "UEFA Euro Qualifiers": "soccer_uefa_euro_qualification",
    "UEFA Nations League": "soccer_uefa_nations_league",
}

# Finestra di ricerca delle partite: 7 giorni. Con la rotazione dei crediti
# una lega puo' essere interrogata 1 volta a settimana: la finestra ampia
# garantisce che NESSUNA partita sfugga (una chiamata copre l'intera
# settimana di calendario).
QUERY_WINDOW_DAYS = 7

# Rotazione interrogazioni (giorni) — PROFILO SETTEMBRE 2026 SOTTO-BUDGET.
# Audit crediti 05/09: 239/500 gia' consumati al giorno 5 (settlement + surebet
# NON erano stati conteggiati nel ~407/mese originale) → restano ~260 crediti
# per ~25 giorni (~10,3/giorno). Ripartizione del resto del mese:
#   settlement mirato ~2-3/giorno (fetch_scores solo leghe con righe aperte)
#   surebet MLB ~4/giorno (cron 15' + TTL 6h, 1 sport in stagione: NBA e'
#     off-season a settembre, riattivare a ottobre col reset crediti)
#   calendario value ~3,5/giorno = SOLO i top campionati (3gg) + coppe
#     europee/mercati maggiori (7gg); tutto il resto a 30gg = DORMIENTE.
# Costo mensile di questo profilo ~158/mese (test test_budget_mensile ok);
# nel resto di settembre le leghe a 30gg erano gia' state interrogate il 1°
# (cache fresca fino a ottobre) → costo residuo reale ~85-95 crediti.
# ⚠️ RIPRISTINARE il profilo completo il 1° ottobre (stagione NBA + reset
# crediti): commit precedente / git log per la tabella a 66 leghe.
# Con la finestra a 7 giorni, anche le leghe interrogate 1 volta a settimana
# non perdono partite: vedono tutto il calendario della settimana.
SPORTS_INTERVAL_DAYS = {
    # ⚠️ 25/09/2026 — ROTAZIONE ALLINEATA ALLA PUBBLICAZIONE DELLE QUOTE.
    # MISURA DIRETTA (API reale, finestra `now` -> `now`+7gg, dal container):
    #   MLS 15 eventi · Liga MX 9 · Nations League 38  -> le odds escono con
    #     **1-3 giorni** di anticipo;
    #   Serie A / Bundesliga / La Liga / Eredivisie: **0 eventi** — a 7 giorni
    #     di distanza l'API non ha ancora pubblicato quelle partite.
    #   Le chiamate VUOTE **non addebitano credito** (remaining invariato su 4
    #     chiamate vuote, -1 su ognuna delle 3 con dati): il costo e' dato dalle
    #     leghe CHE HANNO partite, non dal numero di interrogazioni.
    # Difetto del profilo precedente (24/09): con finestra a 7gg **e** rotazione
    # a 7gg, una lega interrogata il giorno X non vedeva mai le partite del
    # weekend X+4 (odds pubblicate solo a X+2) e alla successiva interrogazione
    # (X+7) erano passate -> **zero candidati per sempre**, qualunque soglia di
    # edge/EV. Da qui il crollo delle analisi (137 il 20/09 -> 24 il 25/09).
    # Le 20 leghe AMMESSE (core + probation) stanno quindi a **2 GIORNI**: e' il
    # massimo che il tetto crediti sostiene (costo mensile teorico 191 -> 371 su
    # un tetto di 460; a 1gg sarebbe 671). Le chiamate vuote sono gratuite,
    # quindi il costo REALE resta piu' basso del teorico.
#    "Premier League": 2, "Bundesliga": 2, "Turkey Super Lig": 2,
#    "Ligue 1": 2, "Eredivisie": 2, "EFL Championship": 2, "Serie B": 2,
#    "MLS": 2, "Brasileirao": 2, "Liga MX": 2, "Saudi Pro League": 2,
#    "Allsvenskan": 2, "Argentina Primera": 2, "Austrian Bundesliga": 2,
#    "Eliteserien": 2, "J1 League": 2, "K League 1": 2,
#    "Scottish Premiership": 2, "Superliga Danimarca": 2,
#    "Swiss Super League": 2,
#    "UEFA Nations League": 2, "Africa Cup of Nations": 2,
    # Nazionali CORE dal 29/09/2026 (il gate le ammette: mai dormienti a
    # 30gg, lezione del 24/09).
    "UEFA Nations League": 7, "Africa Cup of Nations": 7,
    # ogni 3 giorni: leghe NON ammesse ma con mercato liquido (telemetria/CLV)
    "Serie A": 7, "La Liga": 7,
    # ⚠️ 30/09/2026 — ROTAZIONE TAGLIATA A 7 GIORNI (decisa dal proprietario
    # per far entrare l'oracolo a linea totals/spreads nel budget crediti):
    # le 22 leghe ammesse erano a 2gg (370.6 crediti/mese), ora 7gg
    # (~169/mese). Le odds nascono 1-3 giorni prima del kickoff: a 7gg una
    # lega interrogata il giorno X vede le partite fino a X+7 e NON perde il
    # weekend X+4 (il difetto del 24/09 nasceva dalla COMBINAZIONE finestra
    # 7gg + rotazione 30gg, non dalla rotazione 7gg in se'). Il credito
    # risparmiato paga il follow-the-money dell'oracolo a linea
    # (`fetch_line_odds`: 2 crediti extra x lega con pick OU/AH in gioco,
    # tetto ORACLE_BUDGET_DAY). Costo totale del profilo: ~349/460 (test
    # `test_budget_mensile_piano_free`). Profilo PRECEDENTE (2gg):
    #   "Premier League": 2, "Bundesliga": 2, "Turkey Super Lig": 2,
    #   "Ligue 1": 2, "Eredivisie": 2, "EFL Championship": 2, "Serie B": 2,
    #   "MLS": 2, "Brasileirao": 2, "Liga MX": 2, "Saudi Pro League": 2,
    #   "Allsvenskan": 2, "Argentina Primera": 2, "Austrian Bundesliga": 2,
    #   "Eliteserien": 2, "J1 League": 2, "K League 1": 2,
    #   "Scottish Premiership": 2, "Superliga Danimarca": 2,
    #   "Swiss Super League": 2, "UEFA Nations League": 2,
    #   "Africa Cup of Nations": 2, "Serie A": 3, "La Liga": 3,
    "Premier League": 7, "Bundesliga": 7, "Turkey Super Lig": 7,
    "Ligue 1": 7, "Eredivisie": 7, "EFL Championship": 7, "Serie B": 7,
    "MLS": 7, "Brasileirao": 7, "Liga MX": 7, "Saudi Pro League": 7,
    "Allsvenskan": 7, "Argentina Primera": 7, "Austrian Bundesliga": 7,
    "Eliteserien": 7, "J1 League": 7, "K League 1": 7,
    "Scottish Premiership": 7, "Superliga Danimarca": 7,
    "Swiss Super League": 7,
    # ogni 7 giorni: coppe europee (restano nel profilo 25/09)
    "Champions League": 7, "Europa League": 7,
    # ogni 30 giorni (dormienti): coppe nazionali, campionati secondari,
    # resto del mondo e nazionali
    "Conference League": 30, "Coppa Italia": 30, "Copa del Rey": 30,
    "Coupe de France": 30, "DFB Pokal": 30, "FA Cup": 30, "EFL Cup": 30,
    "Primeira Liga": 30, "Veikkausliiga": 30,
    "A-League": 30,
    "Chile Primera": 30, "Copa Libertadores": 30,
    "Copa Sudamericana": 30, "Ligue 2": 30, "Bundesliga 2": 30,
    "La Liga 2": 30, "League One": 30, "League Two": 30,
    "Belgian First Div": 30, "Greek Super League": 30,
    "Polish Ekstraklasa": 30,
    "Russian Premier League": 30, "3. Liga": 30, "Brazil Serie B": 30,
    "Sweden Superettan": 30, "China Super League": 30,
    "League of Ireland": 30, "Frauen-Bundesliga": 30,
    "FIFA Club World Cup": 30, "UCL Qualification": 30,
    "UEFA Women's Champions League": 30,
    "Copa America": 30, "CONCACAF Leagues Cup": 30,
    "FIFA World Cup": 30, "FIFA World Cup Qualifiers Europe": 30,
    "FIFA World Cup Qualifiers S.America": 30, "FIFA Women's World Cup": 30,
    "UEFA Euro": 30, "UEFA Euro Qualifiers": 30,
    "CONCACAF Gold Cup": 30,
}

# Cap giornaliero di chiamate odds. Le leghe in eccedenza vengono rinviate al
# giorno dopo. ⚠️ Con le 22 leghe ammesse a 2gg (~11 dovute/giorno) un tetto di
# 8 ne rinvierebbe meta'. In produzione il valore e' 24 (`ODDS_DAILY_BUDGET`,
# env, nessun redeploy): il 25/09 la misura sul giro reale ha mostrato che
# MLS era la 16ª lega e Liga MX la 18ª nell'ordine effettivo, quindi con un
# tetto piu' basso (8 o 16) entrambe venivano rinviate al giorno dopo e le
# loro partite PERSE — era l'ultimo pezzo rimasto del buco "zero candidati".
# Il tetto mensile vero resta quello del piano free (460), verificato da
# `test_budget_mensile_piano_free`.
DAILY_QUERY_BUDGET = int(os.getenv("ODDS_DAILY_BUDGET", "12"))


def interval_for_sport(sport_key: str) -> int:
    """Giorni tra un'interrogazione e l'altra per una sport key.
    Default 7 (settimanale): se una lega manca dalla tabella, meglio
    interrogarla poco che tutti i giorni (protezione crediti)."""
    for lg, key in SPORTS_MAP.items():
        if key == sport_key:
            return SPORTS_INTERVAL_DAYS.get(lg, 7)
    return 7


def _rotation_phase(sport_key: str, interval: int) -> int:
    """Fase stabile (0..interval-1) della lega nel ciclo di rotazione.

    Spalma le scadenze delle leghe con lo STESSO intervallo su giorni
    diversi: senza, le leghe core (tutte a 3gg) si sincronizzano e vengono
    interrogate tutte lo stesso giorno -> analisi e segnali nuovi solo 1
    giorno su 3. Il costo mensile NON cambia (ogni lega resta sul suo
    intervallo), ma i giri analisi diventano giornalieri.
    """
    if interval <= 1:
        return 0
    h = 0
    for ch in sport_key:
        h = (h * 31 + ord(ch)) % 1000003
    return h % interval


def is_sport_due(sport_key: str) -> bool:
    """True se la cache della lega e' scaduta rispetto al suo intervallo
    (quindi oggi va interrogata l'API, costo 1 credito).

    Regola di stagger (10/09): oltre alla scadenza per intervallo, una
    lega core (intervallo <= 7gg) diventa "dovuta" anche sul suo giorno
    di fase (eta' cache >= 1 giorno), cosi' le leghe a 3gg non si
    sincronizzano tutte nello stesso giorno. Le leghe a 30gg restano
    dormienti puri (solo scadenza per intervallo): nessun costo extra.
    """
    cache_file = CACHE_DIR / f"toa_{sport_key}.json"
    if not cache_file.exists():
        return True
    try:
        data = json.loads(cache_file.read_text())
        interval = interval_for_sport(sport_key)
        ttl = interval * 86400
        age = time.time() - data.get("ts", 0)
        if age >= ttl:
            return True
        if interval <= 7 and age >= 86400:
            day = int(time.time() // 86400)
            if day % interval == _rotation_phase(sport_key, interval):
                return True
        return False
    except Exception:
        return True

def _env(name):
    exact = os.getenv(name)
    if exact is not None: return exact.strip()
    for k, v in os.environ.items():
        if k.strip() == name: return v.strip()
    return ""

def _get_odds(sport, frm, to, *, markets="h2h", cache_prefix="toa_", ttl_s=None):
    """Quote the-odds-api per UNA lega, con cache su `<prefix><sport>.json`.

    `markets` default "h2h" (1 credito/chiamata): e' il percorso della
    rotazione di ricerca, INVARIATO dal 09/09, cache `toa_<sport>.json`.
    `fetch_line_odds` passa `markets="h2h,totals,spreads"` (3 crediti) con
    prefisso cache `toao_` e TTL propri: mai mescolare le due cache (forme
    diverse nello stesso file renderebbero ambiguo il payload per i
    consumatori h2h-only).
    """
    cache_file = CACHE_DIR / f"{cache_prefix}{sport}.json"
    if ttl_s is None:
        ttl_s = interval_for_sport(sport) * 86400
    if cache_file.exists():
        try:
            data = json.loads(cache_file.read_text())
            if time.time() - data.get("ts", 0) < ttl_s:
                return data.get("payload", []), data.get("remaining", 999)
        except Exception: pass
    key = _env("ODDS_API_KEY")
    if not key: return [], 999
    # HARD STOP crediti: sotto soglia nessuna chiamata HTTP (la rotazione
    # ridotta e' irrilevante: il blocco e' totale e vale per ogni lega).
    if credits_hard_stopped():
        return [], 0
    # Filtro proattivo crediti: non interrogare se sotto soglia
    if not should_query_sport(sport):
        logger.info(f"Crediti bassi: {sport} saltata per risparmio crediti")
        return [], 0
    try:
        # NOTA CREDITI (30/09): la rotazione di ricerca resta `markets="h2h"`
        # (1 credito/chiamata). I mercati totals/spreads dell'oracolo a linea
        # passano SOLO da `fetch_line_odds` (follow-the-money: si pagano solo
        # le leghe con pick OU/AH in gioco) — triplicarli qui farebbe salire
        # la fattoria completa a ~938 crediti/mese (out del tetto 460).
        r = requests.get(f"https://api.the-odds-api.com/v4/sports/{sport}/odds", params={
            "apiKey": key, "regions": "eu", "markets": markets,
            "oddsFormat": "decimal", "commenceTimeFrom": frm, "commenceTimeTo": to,
        }, timeout=30)
        remaining = int(r.headers.get("x-requests-remaining", 999))
        if r.status_code in (401, 429):
            logger.warning(f"the-odds-api bloccata (codice {r.status_code})")
            return [], 0
        r.raise_for_status()
        payload = r.json()
    except Exception as e:
        logger.warning(f"Errore the-odds-api {sport}: {e}")
        return [], 999
    CACHE_DIR.mkdir(exist_ok=True)
    cache_file.write_text(json.dumps({"ts": time.time(), "payload": payload,
                                      "remaining": remaining,
                                      "remaining_ts": time.time()}))
    logger.info(f"the-odds-api {sport} ({markets}): {len(payload)} match | "
                f"crediti residui: {remaining}")
    return payload, remaining


def fetch_line_odds(sport, frm, to):
    """Quote `h2h,totals,spreads` per UNA lega (oracolo a linea, 3 crediti).

    Follow-the-money (30/09/2026): chiamata fatta SOLO per le leghe con pick
    OU/AH aperti in finestra d'ordine (`line_oracle.ensure_oracle_payload`),
    budget giornaliero dedicato (`ORACLE_BUDGET_DAY`) e cache separata
    `toao_<sport>.json`. Ritorna `(payload, remaining)`.

    `frm`/`to` arrivano da `line_oracle`, che dal 03/10/2026 usa la FINESTRA
    DI FETCH (`oracle_fetch_window_min()`, default 70 minuti): si scarica solo
    cio' che puo' entrare nella finestra esecutiva T-60..T-5, non l'intero
    palinsesto della lega. La freschezza della cache segue la STESSA
    grandezza (`oracle_cache_ttl_s()`), altrimenti una cache di 70 minuti
    resterebbe considerata valida per 24h pur non coprendo piu' nulla.
    """
    # Budget giornaliero dedicato: l'oracolo a linea NON puo' sfinire la
    # stessa risorsa (crediti) della ricerca — un tetto proprio rende il
    # costo massimo misurabile a prescindere da quante leghe abbiano pick.
    global _oracle_req_day
    _today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _oracle_req_day.get("day") != _today:
        _oracle_req_day = {"day": _today, "n": 0}
    if _oracle_req_day["n"] >= ORACLE_BUDGET_DAY:
        logger.info("oracolo a linea: budget giornaliero %s esaurito (%s "
                    "leghe), rinvio a domani", ORACLE_BUDGET_DAY,
                    _oracle_req_day["n"])
        return [], 999
    if not ORACLE_ENABLED:
        return [], 999
    # Pre-check cache: un HIT di cache NON consuma budget (zero spesa = zero
    # costo; il contatore conta SOLE le chiamate realmente fatte). La regola
    # di freschezza e' la STESSA di `_get_odds` (TTL letto a runtime).
    cache_file = CACHE_DIR / f"{ORACLE_CACHE_PREFIX}{sport}.json"
    if cache_file.exists():
        try:
            data = json.loads(cache_file.read_text())
            if time.time() - data.get("ts", 0) < oracle_cache_ttl_s():
                return data.get("payload", []), data.get("remaining", 999)
        except Exception:
            pass
    payload, remaining = _get_odds(sport, frm, to, markets=ORACLE_MARKETS_LIST,
                                   cache_prefix=ORACLE_CACHE_PREFIX,
                                   ttl_s=oracle_cache_ttl_s())
    if payload or remaining != 999:
        _oracle_req_day["n"] += 1
    return payload, remaining

def fetch_odds(sport=None, commence_time_from=None, commence_time_to=None, **kwargs):
    if not sport: return []
    payload, remaining = _get_odds(sport, commence_time_from, commence_time_to)
    if remaining < MIN_REMAINING:
        logger.warning(f"Crediti esauriti ({remaining}), nessuna quota scaricata")
        return []
    return payload

# Finestra di refertazione `daysFrom`: l'API the-odds-api copre al MASSIMO
# 3 giorni indietro (422 oltre). Con 2 giorni le partite di due sere prima
# (kickoff a >48h) restavano fuori dal payload e la bet non si saldava mai:
# il costo per chiamata NON cambia, quindi tanto vale usare il massimo.
SCORES_DAYS_FROM = 3


def fetch_scores(sport=None, days_from=SCORES_DAYS_FROM):
    """Risultati finali (stessa chiave, ~1 credito/call, cache 24h).

    La cache NON viene fidata se contiene partite iniziate da oltre
    STALE_INPLAY_HOURS ancora marcate completed=False (cache scritta mentre
    la partita era in gioco): in quel caso si richiama l'API per avere i
    risultati veri, altrimenti il settlement delle puntate resta bloccato.
    Se la chiamata fallisce (crediti esauriti, rete), si ripiega sulla cache
    comunque: meglio dati vecchi di nessun dato.
    """
    if not sport:
        return []
    cache_file = CACHE_DIR / f"toa_scores_{sport}.json"
    payload = []
    if cache_file.exists():
        try:
            data = json.loads(cache_file.read_text())
            ts = data.get("ts", 0)
            payload = data.get("payload", [])
            if time.time() - ts < ODDS_TTL:
                if not _cache_is_stale_for_settlement(payload):
                    return payload
                # Cache stantia per il settlement: forza il refresh (fallthrough)
                logger.warning("scores cache stantia (%s): refresh forzato", sport)
        except Exception:
            pass
    key = _env("ODDS_API_KEY")
    if not key:
        # Nessuna chiave: la cache e' tutto quello che abbiamo.
        return payload if cache_file.exists() else []
    # HARD STOP crediti: sotto soglia nessuna chiamata HTTP nemmeno per il
    # settlement — si usano i punteggi GIA' in cache (mai dati inventati).
    if credits_hard_stopped():
        return payload if cache_file.exists() else []
    try:
        r = requests.get(f"https://api.the-odds-api.com/v4/sports/{sport}/scores",
                         params={"apiKey": key, "daysFrom": days_from}, timeout=30)
        remaining = int(r.headers.get("x-requests-remaining", 999))
        if r.status_code in (401, 429):
            logger.warning(f"Scores bloccati ({r.status_code})")
            return payload if cache_file.exists() else []
        r.raise_for_status()
        payload = r.json()
    except Exception as e:
        logger.warning(f"Errore scores {sport}: {e}")
        return payload if cache_file.exists() else []
    CACHE_DIR.mkdir(exist_ok=True)
    # Se il payload contiene SOLO partite completate, salviamo con il
    # timestamp originale della cache precedente (se fresca): cosi' la
    # scrittura non 'ringiovanisce' artificialmente una cache che copre
    # ancora la finestra quote, e il refresh non costa piu' crediti del
    # necessario nelle ore successive.
    save_ts = time.time()
    if isinstance(payload, list) and payload and all(m.get("completed") for m in payload):
        try:
            old = json.loads(cache_file.read_text())
            if time.time() - old.get("ts", 0) < ODDS_TTL:
                save_ts = old["ts"]
        except Exception:
            pass
    # Il consumo crediti va PERSISTITO anche nelle cache dei punteggi:
    # `get_remaining()`/`get_quota()` (guardia proattiva + credit watchdog)
    # leggono le cache `toa_*.json`, quindi senza questo campo il consumo di
    # `fetch_scores` era invisibile (bug 12/09: il contatore restava a 58
    # mentre l'API ne riportava 6 -> nessun throttle, nessun alert, crediti
    # bruciati fino all'esaurimento).
    cache_file.write_text(json.dumps({"ts": save_ts, "payload": payload,
                                      "remaining": remaining,
                                      "remaining_ts": time.time()}))
    logger.info(f"the-odds-api scores {sport}: {len(payload)} | crediti residui: {remaining}")
    return payload

def oddsapi_to_records(payload, sport="calcio"):
    """Converte il payload v4 the-odds-api nel contratto normalizzato di odds_ingest.

    Righe {bookmaker, evento, sport, esito, quota_decimale, timestamp}:
    - h2h: outcome nome squadra -> "1"/"2", "Draw" -> "X";
    - totals: "Over X.5"/"Under X.5" lasciati com' sono;
    - evento = f"{home} vs {away}" (senza campionato: per il merging
      con Betfair l'accoppiata squadre e' la chiave).
    """
    rows = []
    for match in payload:
        home = (match.get("home_team") or "").strip()
        away = (match.get("away_team") or "").strip()
        if not home or not away:
            continue
        commence = match.get("commence_time") or ""
        for bm in match.get("bookmakers", []):
            bookmaker = bm.get("title") or bm.get("key") or "unknown"
            for mkt in bm.get("markets", []):
                key = mkt.get("key")
                for out in mkt.get("outcomes", []):
                    name = (out.get("name") or "").strip()
                    price = out.get("price")
                    if not name or price is None or float(price) <= 1.0:
                        continue
                    if key == "h2h":
                        if name == home:
                            esito = "1"
                        elif name == away:
                            esito = "2"
                        elif name.lower() in ("draw", "pareggio"):
                            esito = "X"
                        else:
                            continue
                    else:
                        continue
                    rows.append({
                        "bookmaker": bookmaker,
                        "evento": f"{home} vs {away}",
                        "sport": sport,
                        "esito": esito,
                        "quota_decimale": float(price),
                        "timestamp": commence,
                    })
    return rows


def get_live_odds():
    """Quote reali oggi per tutte le leghe, come lista di righe normalizzate.

    Usa la cache 24h per lega: dopo il job mattutino 6:00 (fetch_and_analyze
    today) le chiamate successive costano zero crediti. Serve ODDS_API_KEY.
    """
    if not _env("ODDS_API_KEY"):
        return []
    from datetime import datetime, timedelta
    frm = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    to = (datetime.utcnow() + timedelta(hours=28)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = []
    for sport_key in SPORTS_MAP.values():
        try:
            payload = fetch_odds(sport=sport_key, commence_time_from=frm,
                                 commence_time_to=to)
        except Exception as e:
            logger.warning(f"get_live_odds {sport_key}: {e}")
            continue
        if payload:
            rows.extend(oddsapi_to_records(payload))
    logger.info(f"get_live_odds: {len(rows)} quote normalizzate")
    return rows

def get_quota():
    """(crediti residui, n cache) dall'ultimo scan (costo zero).

    Stessa fonte di `get_remaining`: la lettura piu' recente, non il minimo.
    """
    rem, n = _latest_credits()
    if rem is None:
        return None
    return rem, n


# --- BUDGET CREDITI: ritmo e proiezione (15/09/2026) ------------------------
# Il rischio a meta' mese NON e' il livello dei crediti ma il RITMO: con 273
# crediti e 15 giorni al reset si e' tranquilli solo se il consumo sta sotto
# ~18/giorno. Le soglie fisse (50/20/10/5) avvisano quando e' tardi, e le
# medie lunghe mentono (il 15/09 la finestra a 340h diceva 15.5/giorno mentre
# le ultime 24h ne dicevano 58, perche' il cambio di chiave e la pausa del
# settlement azzerano periodi interi). Qui si misura il consumo dalla
# telemetria delle cache e si proietta la data di esaurimento.
def credits_reset(now=None) -> datetime:
    """Data del PROSSIMO reset mensile del piano (1° del mese, UTC).

    Prima era una COSTANTE hardcoded (`datetime(2026, 10, 1)`): dal 1° ottobre
    era nel PASSATO, quindi `days_to_reset()` valeva 0, `sustainable_per_day`
    era None e il credit watchdog non poteva piu' dire se il ritmo stava nel
    budget (log reale: "reset tra 0 giorni, sostenibile None/giorno") — una
    degradazione SILENZIOSA della telemetria, che tace proprio quando serve.

    La data si CALCOLA: cosi' non puo' scadere di nuovo col calendario (la
    lezione delle date fisse del 15/09 e del 17/09). Il reset del piano free
    e' il 1° del mese; `now` e' iniettabile per i test.
    """
    now = now or datetime.now(timezone.utc)
    year, month = now.year, now.month + 1
    if month > 12:
        year, month = year + 1, 1
    return datetime(year, month, 1, tzinfo=timezone.utc)


# Alias retrocompatibile (importato da `web_api`): valore del PROSSIMO reset.
CREDITS_RESET = credits_reset()
CREDIT_BURN_WINDOW_HOURS = 48.0


def days_to_reset(now=None) -> int:
    """Giorni (interi) al reset mensile del piano (ricalcolato a ogni call)."""
    now = now or datetime.now(timezone.utc)
    return max(0, (credits_reset(now) - now).days)


def credit_burn_rate(window_hours: float = CREDIT_BURN_WINDOW_HOURS,
                     min_window_hours: float = 1.0, cache_dir=None):
    """Consumo MISURATO in crediti/giorno dalla telemetria delle cache.

    Prende la lettura piu' vecchia e la piu' recente DENTRO la finestra e
    divide il delta per il tempo che le separa. La finestra corta e' voluta
    (vedi commento sopra).

    Ritorna None quando non c'e' niente da misurare: meno di due letture,
    finestra sotto `min_window_hours`, oppure crediti che RISALGONO (chiave
    cambiata/reset) — mai un numero finto.
    """
    directory = cache_dir or CACHE_DIR
    rows = []
    if directory.exists():
        for f in directory.glob("toa_*.json"):
            try:
                d = json.loads(f.read_text())
                if d.get("remaining") is None:
                    continue
                ts = d.get("remaining_ts", d.get("ts"))
                if isinstance(ts, (int, float)):
                    rows.append((float(ts), int(d["remaining"])))
            except Exception:
                continue
    cut = time.time() - window_hours * 3600
    win = sorted(r for r in rows if r[0] >= cut)
    if len(win) < 2:
        # La finestra non basta: si allarga a tutta la storia disponibile,
        # dichiarando la finestra VERA (mai spacciare 340h per 48h).
        win = sorted(rows)
    if len(win) < 2:
        return None
    oldest, newest = win[0], win[-1]
    hours = (newest[0] - oldest[0]) / 3600.0
    if hours < min_window_hours or oldest[1] < newest[1]:
        return None
    return {"rate_per_day": round((oldest[1] - newest[1]) / (hours / 24.0), 1),
            "window_hours": round(hours, 1), "samples": len(win),
            "remaining": newest[1]}


def credit_budget_status(now=None, cache_dir=None) -> dict:
    """Stato del budget: residuo, ritmo misurato e data di esaurimento.

    `alert` = True quando il ritmo misurato esaurisce i crediti PRIMA del
    reset del piano: e' il campanello che le soglie fisse non danno (il
    15/09: 273 crediti residui sembravano tanti, ma a 58/giorno finivano in
    ~5 giorni, tre settimane prima del reset).
    """
    now = now or datetime.now(timezone.utc)
    remaining = get_remaining()
    burn = credit_burn_rate(cache_dir=cache_dir)
    left = days_to_reset(now)
    out = {"remaining": remaining, "days_to_reset": left,
           "sustainable_per_day": (round(remaining / left, 1)
                                   if remaining is not None and left > 0
                                   else None),
           "rate_per_day": None, "window_hours": None, "samples": 0,
           "days_left": None, "exhaustion_date": None, "alert": False}
    if burn:
        out["rate_per_day"] = burn["rate_per_day"]
        out["window_hours"] = burn["window_hours"]
        out["samples"] = burn["samples"]
        rate = burn["rate_per_day"]
        if rate > 0 and remaining is not None:
            days_left = remaining / rate
            out["days_left"] = round(days_left, 1)
            # Proiezione sullo STESSO istante di `days_left` (cosi' il calcolo
            # e' riproducibile e testabile senza dipendere dall'orologio).
            out["exhaustion_date"] = (now + timedelta(days=days_left)
                                      ).date().isoformat()
            out["alert"] = days_left < left
    return out
