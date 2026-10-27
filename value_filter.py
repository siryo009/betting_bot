"""Value bet filter e Kelly Criterion Pro"""

import logging
import os
from typing import List, Dict, Any

from market_calib import (
    blend_probability,
    favourite_longshot_adjust,
    market_edge as _market_edge,
    MARKET_EDGE_MIN,  # ri-esportata: soglia +2pp sul mercato (21/09)
    MARKET_EDGE_MODERATE,
    MARKET_EDGE_STRONG,
    LEAGUE_EFFICIENCY,
)

logger = logging.getLogger(__name__)


# === STRATEGIA BASE ===
# Dati backtest storico (16.273 partite, 2022-2026, gate 1.30-1.80):
#   • Totali: 215 bet, ROI +5.7%, hit 60.5%, MaxDD 7.1%
#   • CLV vig-free: -3.3% → il modello NON batte la closing line
#   • Perde in Serie A (-5.9%), La Liga (-6.3%), Grecia (-69.4%)
#   • Vince in Bundesliga (+28.3%), PL (+16.2%), Turchia (+22.1%),
#     Ligue 1 (+9.1%)
#   • Fascia 1.60-1.80 = +21.9% ROI (n=55), 1.45-1.60 = -9.9% (n=49)
#   • Fascia 1.30-1.45 = +7.4% (n=44, marginalmente positiva)
#
# Strategia corretta (basata sui dati):
#   1) SOLO campionati con ROI positivo nel backtest
#   2) Fascia quote ODDS_MIN-ODDS_MAX (default **1.20-2.00** dal 14/10/2026:
#      era 1.30-1.80 dal 11/09, allargata dalla direttiva "piu' volume")
#      con una FASCIA MORTA OPZIONALE (1.50-1.80) **spenta di default** dal
#      26/10/2026: vedi il blocco `ODDS_EXCLUDE_*` piu' sotto
#   3) Edge differenziato per efficienza lega
#   4) Kelly adattivo per lega (kelly_mult + max_stake)
#   5) Esclusione automatica leghe con CLV negativo cronico

#: Soglia EV minima **UNICA** del progetto.
#: Tutti i percorsi la IMPORTANO, mai la ricopiare: calcio 1X2 (`is_sane`),
#: multi-mercato OU/AH, eSports (`esports_oracle.min_ev`), oracolo top-down
#: (`pinnacle_oracle.DEFAULT_EV_MIN`) e catena di decisione
#: (`decision/limits.py`). Il tennis ha la soglia PROPRIA dichiarata
#: (`tennis_lane.EV_MIN`, env `TENNIS_EV_MIN`, default 0.025), lasciata piu'
#: alta per scelta: sul tennis l'EV e' misurato allineato allo sharp entro
#: ~0,7% (30/09) e la banda quota 1.30-2.30 basta da sola ad allargare.
#:
#: STORIA (perche' e' tornata su): 2,5% (prudenza 21/09) -> **1,5%**
#: (direttiva "piu' volume", 14/10/2026) -> **2,5%** (26/10/2026, dopo che il
#: capitale e' sceso da ~45 a ~14 USDC).
#:
#: MISURA che ha riportato la soglia su — ledger LIVE reale, 80 puntate chiuse
#: con la previsione agganciata (`research_isolated/snapshot/`, sola lettura).
#: Ogni riga e' un SUBSET delle bet davvero piazzate, quindi e' un "cosa
#: sarebbe successo", non un backtest:
#:   gate attuale (EV >= 1,5%)            n=56  ROI  **-8,46%**
#:   EV >= 2,5%                          n=52  ROI  -3,11%
#:   EV >= 2,5% + edge >= 2pp            n=46  ROI  **-0,25%**
#:   EV >= 2,5% + edge >= 2pp + no morta n=29  ROI  **+1,33%**
#:   EV >= 3%   + edge >= 3pp + no morta n=18  ROI -15,87%  <- NON meglio:
#:     stringere ancora seleziona i segnali a edge ENORME, che sono quelli
#:     dove il modello si allontana di piu' dal mercato (e sbaglia).
#: L'era "bot aggressivo" (14-20/10, gate allentati) e' quella che ha bruciato
#: il capitale: **-33,1% su 29 bet**, contro +7,95% delle 53 precedenti.
EV_MIN = 0.025           # +2,5% minimo (era 1,5% dal 14/10)

#: TETTO DELL'EV (26/10/2026) — guardia all'OVERCONFIDENCE del modello, non
#: una strategia. MISURA sul ledger reale: la fascia EV >= 10% ha reso
#: **-65,1% su 12 bet (-24,98 USDC)**, cioe' da sola piu' della perdita totale
#: delle 80 chiuse (-15,39). Il meccanismo e' dichiarato e noto: per avere
#: EV > 10% a quota 1,2-2,0 il modello deve stimare una probabilita' MOLTO
#: sopra il mercato devigato — e su questo progetto quel caso coincide con un
#: input cieco (rating assenti, formazioni, campione piccolo), non con un
#: edge. E' la stessa classe del bias tennis misurato (+13pp di EV dichiarato
#: contro realizzato). `EV_MAX` era 20% ("anomalia"): a 10% la guardia morde
#: dove il dato dice che si perde. Env `EV_MAX` per riaprirlo (es. 0.20).
EV_MAX = float(os.getenv("EV_MAX", "0.10"))

# ---------------------------------------------------------------------------
# GATE EV DEDICATO AI MERCATI LIQUIDI (direttiva 04/10/2026, punto 4)
# ---------------------------------------------------------------------------
# Perche': sui mercati a DUE esiti e ad alta liquidita' (Handicap Asiatico,
# Over/Under, Totals, BTTS, Moneyline) il margine devigato e' piu' stretto e
# il prezzo SX e' piu' spesso allineato: chiedere il 2.5% spegneva il volume
# proprio dove il ritardo di prezzo e' misurabile. La soglia scende a EV >1.0%
# **solo per quei mercati** — 1X2, eSports e tennis restano alla soglia unica
# `EV_MIN` (il modello e' piu' debole, e li' allentare sarebbe un allargamento
# silenzioso). MLB e BTTS sono dichiarati liquidi ma oggi NON hanno un feed
# attivo (BTTS: 0 mercati su SX; the-odds-api e' h2h-only), quindi la voce
# resta pronta e senza effetto finche' non si collega un oracolo.
#: Env: `EV_MIN_LIQUID` (default **1.0%**: riportata su il 26/10/2026, era
#: 0,5% dal 14/10) e `EV_LIQUID_MARKETS`. Nessuna misura separata giustifica
#: lo 0,5% sui mercati liquidi: la prudenza si applica a tutta la pipeline
#: finche' la corsia OU/AH non produce campione.
EV_MIN_LIQUID = float(os.getenv("EV_MIN_LIQUID", "0.010"))
_DEFAULT_LIQUID_MARKETS = "AH,OU,TOTALS,BTTS,ML"
LIQUID_MARKETS = frozenset(
    m.strip().upper()
    for m in os.getenv("EV_LIQUID_MARKETS", _DEFAULT_LIQUID_MARKETS).split(",")
    if m.strip()
)


def is_liquid_market(market: str | None) -> bool:
    """True se il mercato ha la soglia EV dedicata (mercati liquidi)."""
    return str(market or "").strip().upper() in LIQUID_MARKETS


def ev_min_for_market(market: str | None) -> float:
    """Soglia EV di un mercato: 1.0% se liquido, altrimenti la soglia unica.

    UNICA definizione della regola: la usano il Cervello (`base_ev_min`), il
    multi-mercato AH/OU, l'oracolo top-down e `is_sane`. Nessun consumatore
    ricopia la soglia.
    """
    return EV_MIN_LIQUID if is_liquid_market(market) else EV_MIN

# Fascia quote: favoriti NETTI (guardrail 11/09).
# ⚠️ ALLARGATA (direttiva del proprietario, 14/10/2026): 1.30-1.80 ->
# **1.20-2.00**. Direzione opposta al guardrail dell'11/09, che esisteva per
# escludere il "pantano" 1.45-1.60 (ROI storico -9.9%) e le sfavorite.
# MISURA su 14 giorni di ledger con EV >= 1.5%: 1.30-1.80 -> 307 righe,
# 1.20-2.00 -> **346** (~+3/giorno), 1.05-2.50 -> 435 (~+9/giorno, esclusa:
# contiene la fascia 2.00-2.50 che il tennis ha misurato a -15,1% il 13/10).
# Resta invariato il vincolo che rende la fascia sensata: `FAVOURITES_ONLY` +
# `MIN_FAVOURITE_MARKET_PROB` (l'esito deve essere il favorito di mercato),
# quindi allargare la fascia NON riammette le sfavorite.
ODDS_MIN = 1.20          # quota minima: sotto, il ritorno non paga il rischio
ODDS_MAX = 2.00          # quota massima: sopra e' "pantano" o sfavorita

# ---------------------------------------------------------------------------
# FASCIA MORTA 1.50-1.80 — **SPENTA DI DEFAULT dal 26/10/2026**
# ---------------------------------------------------------------------------
# Il MECCANISMO resta (ed e' testato); l'APPLICAZIONE e' opt-in da env.
#
# STORIA. Introdotta il 25/10 come esclusione dell'unica banda che aveva perso
# in ogni misura indipendente del P/L:
#   1.20-1.50 n=17 +16,59% · 1.50-1.80 n=31 **-16,40%** · 1.80-2.00 n=24 -1,06%
#   (per corsia: calcio 1X2 -3,98% n=20 · tennis -37,11% n=10)
# Il caveat era scritto nel codice stesso: z ~ -0,96 su 31 bet, cioe' un
# intervallo che include lo zero.
#
# LA RIMISURA del 26/10 (CLV + P/L per fascia, richiesta dal proprietario) ha
# CONTRADDETTO quella giustificazione sul piano dell'edge:
#
#   CLV per fascia (473 campioni con closing sharp, `scripts/clv_brier_analytics`
#   + ledger agganciato, CI95 nel report):
#     1.20-1.50  n=135  **+2,23%**
#     1.50-1.80  n=199  **+2,59%**  (88% dei campioni positivi)
#     1.80-2.00  n=105  **+2,58%**
#     2.00-2.30  n=  9   +6,05%
#     >= 2,50    n=  4  +15,44%   <- gonfiato dalla definizione su quote alte
#   La banda esclusa batte la closing line quanto le fasce VICINE: non e' una
#   banda senza edge, e' una banda che ha perso MOMENTANEAMENTE.
#
#   P/L della stessa popolazione misurato sulla QUOTA-SEGNALE (la popolazione
#   che il gate taglia davvero, non i prezzi di riempimento):
#     1.50-1.80  n=29  **-5,55%**   (era -16,4% sui fill price, n=31)
#     1.80-2.00  n=18  -35,55%
#   Anche il bleed si riduce a un terzo quando si toglie il rumore dei prezzi
#   di riempimento: un taglio guidato dal P/L a n=31, non dall'edge.
#
# DECISIONE (proprietario, 26/10/2026): meccanismo conservato, applicazione
# SPENTA -> la fascia 1.20-2.00 torna continua. Si riaccende senza toccare il
# codice con `ODDS_EXCLUDE_MIN=1.50 ODDS_EXCLUDE_MAX=1.80` (entrambe in
# `preserve()` in `.railway/railway.ts`): la prossima misura potra' riaprire la
# domanda con un campione piu' grande.
#
# ⚠️ Vale per i mercati di STRATEGIA (1X2 calcio, tennis): i mercati LIQUIDI
# (OU/AH) NON hanno una misura equivalente alle spalle e restavano fuori
# dall'esclusione anche quando era attiva (`is_liquid_market`).
#
# Default 0.0 = banda NON configurata = nessuna esclusione. Un valore
# illeggibile ricade sul default con un warning (una guardia non si spegne e
# non si accende per un env sbagliato).

def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning("%s=%r non numerico: uso il default %s", name, raw,
                       default)
        return default


ODDS_EXCLUDE_MIN = _env_float("ODDS_EXCLUDE_MIN", 0.0)
ODDS_EXCLUDE_MAX = _env_float("ODDS_EXCLUDE_MAX", 0.0)


def dead_band_active() -> bool:
    """True solo se la banda morta e' CONFIGURATA (entrambi gli estremi > 0).

    UNICA definizione dell'attivazione: la usano `in_dead_band` e la
    diagnostica, cosi' "banda spenta" e "banda configurata male" non possono
    dare risposte diverse in due percorsi.
    """
    return (ODDS_EXCLUDE_MIN > 0.0 and ODDS_EXCLUDE_MAX > 0.0
            and ODDS_EXCLUDE_MIN < ODDS_EXCLUDE_MAX)


def in_dead_band(price) -> bool:
    """True se la quota cade nella fascia morta ED e' stata configurata.

    UNICA definizione del buco: la usano `odds_in_band`, `is_sane` e la corsia
    tennis (`tennis_lane.in_odds_band`), cosi' il confine non puo' vivere in
    due posti e divergere. Con la banda spenta (default dal 26/10/2026)
    risponde sempre False: il gate non esiste finche' non lo si accende.
    Fail-safe: un valore illeggibile NON e' nella banda morta (lo esclude gia'
    `odds_in_band` con la sua regola).
    """
    if not dead_band_active():
        return False
    try:
        val = float(price)
    except (TypeError, ValueError):
        return False
    return ODDS_EXCLUDE_MIN <= val <= ODDS_EXCLUDE_MAX


def odds_in_band(price, *, odds_min: float | None = None,
                 odds_max: float | None = None) -> bool:
    """True se la quota e' nella fascia giocabile (bordi INCLUSI).

    UNICA definizione del confronto di fascia: la usano il pre-filtro SX di
    `auto_bet` (che decide se PAGARE l'oracolo a linea, 05/10/2026) e la
    diagnostica. I benchmark "quota entro ODDS_MIN..ODDS_MAX" sparsi nei
    moduli non devono piu' essere riscritti a mano: una fascia, un posto.

    La FASCIA MORTA (`in_dead_band`, 1.50-1.80) e' esclusa solo quando (a) si
    chiede la fascia DEL PROGETTO (nessun confine passato a mano) e (b) la
    banda e' CONFIGURATA: un chiamante che chiede "questa quota sta fra 1.9 e
    2.1?" sta facendo una domanda su quei numeri, e la risposta non deve
    dipendere da una banda misurata su un'altra popolazione (ne' da un gate
    spento di default).

    FAIL-CLOSED su un valore assente o non numerico: una quota che non si sa
    leggere NON e' nella fascia (non e' una quota giocabile).
    """
    try:
        val = float(price)
    except (TypeError, ValueError):
        return False
    lo = ODDS_MIN if odds_min is None else float(odds_min)
    hi = ODDS_MAX if odds_max is None else float(odds_max)
    if not (lo <= val <= hi):
        return False
    if odds_min is None and odds_max is None and in_dead_band(val):
        return False
    return True

# === DINAMIC KELLY ===
# Stake proporzionale all'edge: piu' EV = piu' stake, meno rischio
def dynamic_kelly(ev: float, max_ev: float = EV_MAX,
                   base_fraction: float = 0.05) -> float:
    """Frazione Kelly scalata sull'edge: 0.5% per EV_min, 5% per EV_max."""
    if ev <= 0 or max_ev <= 0:
        return 0.0
    ratio = min(ev / max_ev, 1.0)
    return base_fraction * (0.1 + ratio * 0.9)  # range: 0.1x - 1.0x

# === ODDS MOVEMENT ===
# Rilevamento sharp money: quote che scendono = informazione privilegiata
MOVEMENT_THRESHOLD = -0.05  # -5% di movimento = segnale forte
MOVEMENT_BONUS_MULTIPLIER = 1.2  # +20% stake su segnali con momentum

# === TIMING FILTER ===
# Momento ottimale per piazzare: da 2 minuti a 24h prima del kickoff.
# ⚠️ La FINESTRA ESECUTIVA (T-180..T-2, direttiva 04/10/2026 punto 3) NON vive
# qui: la decide `auto_bet.T60_WINDOW_MIN_MIN/MAX_MIN`, che e' l'autorita' del
# giro puntate. Questo filtro resta il pavimento anti-ordine-a-partita-
# imminente (2 minuti, allineato al pavimento condiviso dei moduli di scan) e
# la guardia "troppo presto = rumore" a 24h: duplicare qui il valore T-180
# creerebbe una SECONDA soglia
# capace di bloccare ordini fuori dal giro T-60 (test, diagnostica, corsia
# Chief) — la classe di bug del 13/09 (due copie della stessa regola che
# divergono).
TIMING_OPTIMAL_MIN_HOURS = 2.0 / 60.0   # 2 minuti minimi prima del kickoff
TIMING_OPTIMAL_MAX_HOURS = 24.0         # oltre = troppo presto (quote noise)

# === CORRELATION CHECK ===
# Massimo esposizione per lega + finestra temporale
MAX_LEAGUE_EXPOSURE_PCT = 0.30    # 30% bankroll per lega
MAX_BLOCK_EXPOSURE_PCT = 0.40     # 40% bankroll totale per blocco correlated

# === FREQUENCY BOOST ===
# Con soglie piu' basse, piu' segnali = piu' profitto potenziale
MIN_DAILY_BETS = 2    # minimo scommesse al giorno per sessione
MAX_DAILY_BETS = 10   # massimo per evitare over-exposure


# === STRATEGIA PER LEGA ===
# Solo campionati con EV positivo nel backtest storico.
# Ogni lega ha: min_edge (vs mercato), kelly_mult, max_stake.
# Leghe NON elencate sono escluse automaticamente.
#: ⚠️ EDGE DI LEGA DIMEZZATO (direttiva del proprietario, 14/10/2026):
#: core 2,0/2,5pp -> **1,0/1,25pp**. PERCHE' INSIEME ALLA SOGLIA EV: la
#: misura sui 14 giorni mostra che con edge >= 2pp le righe 'nuove' sono 18 a
#: qualunque soglia EV (il gate dell'EDGE assorbe tutta la riduzione dell'EV,
#: che da sola sarebbe stata un no-op). Con l'edge dimezzato i pick nuovi
#: passano da 13 a **28 in 14 giorni (2,0/giorno)**.
STRATEGY_LEAGUES = {
    # Leghe vincenti (backtest)
    # ⚠️ EDGE DI LEGA riportato a +2pp/+2,5pp il 26/10/2026 (era +1pp/+1,25pp
    # dal 14/10). MISURA sul ledger live: le bet con edge 1-2pp hanno reso
    # **-70,5% su 8** (-14,22 USDC su 20,16 puntati), mentre 2-3pp hanno reso
    # +3,73% e 3-5pp +7,30%. La fascia "quasi valore" e' quella che perde: e'
    # l'unica banda in cui il modello dichiara un vantaggio che non c'e'.
    "Premier League":       {"min_edge": 0.020, "kelly_mult": 1.2, "max_stake": 0.020, "efficiency": 0.85},
    "Bundesliga":           {"min_edge": 0.020, "kelly_mult": 1.3, "max_stake": 0.020, "efficiency": 0.78},
    "Turkey Super Lig":     {"min_edge": 0.025, "kelly_mult": 1.1, "max_stake": 0.018, "efficiency": 0.60},
    "Ligue 1":              {"min_edge": 0.025, "kelly_mult": 1.0, "max_stake": 0.018, "efficiency": 0.75},
    "Eredivisie":           {"min_edge": 0.025, "kelly_mult": 0.8, "max_stake": 0.015, "efficiency": 0.65},
    # --- NAZIONALI IN CORSO (29/09/2026) — direttiva del proprietario: CORE ---
    # Perche': nelle 24h del 24/09 AFCON + UEFA Nations League erano l'88%
    # (111 su 126) delle righe scartate e l'UNICO motivo di stop del flusso:
    # le nazionali giocano quando i campionati di club sono in pausa, quindi
    # il gate le vedeva mentre il resto del calendario era fermo.
    # I nomi sono ESATTAMENTE le chiavi di `odds_api.SPORTS_MAP`: sono i nomi
    # che il ledger contiene davvero (diagnosi 24/09: «lega 'UEFA Nations
    # League' esclusa») e quelli su cui girano rotazione quote e settlement.
    # ⚠️ ONESTA' SUL CRITERIO: queste due competizioni NON hanno un ROI
    # misurato positivo — il backtest 2022-2026 copre i campionati di club, non
    # le nazionali. Il criterio storico del core («ROI positivo misurato») qui
    # NON e' soddisfatto: e' una decisione del proprietario, non una misura.
    # Per questo i parametri sono i PIU' PRUDENTI del core (gli stessi di
    # Eredivisie, l'unica lega core con la differenza dentro il rumore) e il
    # campione va rimisurato sul ledger prima di qualunque allargamento
    # (`league_dynamic.py` fa esattamente questo, in sola lettura).
    "UEFA Nations League":  {"min_edge": 0.025, "kelly_mult": 0.8, "max_stake": 0.015, "efficiency": 0.55},
    "Africa Cup of Nations":{"min_edge": 0.025, "kelly_mult": 0.8, "max_stake": 0.015, "efficiency": 0.50},
    # Leghe perse nel backtest: generate NO segnali
    # "Serie A", "La Liga", "Belgian Pro League", "Liga Portugal",
    # "Greek Super League" — escluse per ROI negativo
}

# === TIER-2 "PROBATION" (21/09/2026) ===
# Direttiva del proprietario: allentare il gate di lega per aumentare il
# volume, con rischio LIMITATO. Il gate passa da DUE stati (ammessa/vietata) a
# TRE: `core` (STRATEGY_LEAGUES, storico misurato positivo) · `probation`
# (questo set: nessuno storico positivo misurato, ma leghe reali, liquide e
# saldabili) · `blocked` (tutto il resto).
#
# Le leghe in probation ricevono una strategia PIU' SEVERA del core, non
# uguale: edge minimo +4pp (riportato su il 26/10/2026; era +2pp dal 14/10),
# Kelly ridotto (0.4 contro 0.8-1.3) e cap di stake 0.5% (contro 1.5-2%).
# Le leghe in probation non hanno uno storico positivo MISURATO: la soglia piu'
# alta e' l'unica cosa che le distingue dal core, e i 14 giorni di gate a +2pp
# hanno prodotto la peggiore era del ledger (-33,1% su 29 bet).
# Dal 23/10/2026 la probation accoglie anche le due coppe europee (Europa e
# Conference League): non hanno un ROI misurato, quindi non possono stare nel
# core, ma "blocked" era un default e non una misura — e il loro board era il
# 100% dei pick non giocabili nelle sere infrasettimanali.
#
# CRITERI DI AMMISSIONE (tutti obbligatori):
#   1. presente in `odds_api.SPORTS_MAP` (altrimenti non e' saldabile);
#   2. scansionata davvero da SX (copertura misurata sul ledger);
#   3. NESSUN ROI misurato negativo (backtest 12/09/2026, 215 bet, fascia
#      favoriti 1.30-1.80).
# ESCLUSE di proposito per il criterio 3 (misurate negative, n=6-26):
#   Serie A -5.9%, La Liga -6.3%, Belgian Pro League -6.3%,
#   Liga Portugal -13.4%, Greek Super League -69.4%.
# Eredivisie (-1.8%, n=23) resta invece nel CORE: campione piccolo e
# differenza dentro il rumore (decisione esplicita del proprietario, 21/09).
PROBATION_LEAGUES = {
    # Inghilterra, Italia, USA, Sud America
    "EFL Championship", "Serie B", "MLS", "Brasileirao", "Argentina Primera",
    # Europa centro-nord
    "Swiss Super League", "Eliteserien", "Austrian Bundesliga",
    "Scottish Premiership", "Superliga Danimarca", "Allsvenskan",
    # Asia / Golfo
    "K League 1", "J1 League", "Liga MX", "Saudi Pro League",
    # Coppe europee (23/10/2026, direttiva del proprietario).
    # NON hanno un ROI misurato — quindi NON possono stare nel core (che
    # significa "storico positivo"), ma non sono nemmeno fra le leghe misurate
    # NEGATIVE: "blocked" era un default, non una misura. Entrano in probation,
    # cioe' con la strategia piu' severa del progetto: edge minimo piu' alto,
    # Kelly 0.4x e cap 0.5% del bankroll. Motivo operativo: il board serale di
    # coppe era il 100% dei pick non giocabili (Europa/Conference), e con il
    # campionato fermo o infrasettimanale il gate lasciava il bot a zero
    # candidati. Se il ledger le promuove si allineano al core; se le boccia si
    # riportano a blocked — in entrambi i casi con una misura, non a naso.
    "Europa League", "Conference League",
}

# Strategia di probation: piu' severa del core su edge, Kelly e cap.
PROBATION_STRATEGY = {"min_edge": 0.04, "kelly_mult": 0.4, "max_stake": 0.005}

# Fallback per leghe SCONOSCIUTE o assenti (il segnale resta vietato a monte:
# `league_allowed` rifiuta qualunque nome non in core/probation).
# NB: min_edge riportato a +2pp il 26/10/2026 (era +1pp dal 14/10): il
# fallback si applica alle leghe fuori whitelist aperte dalla corsia torneo e
# ai segnali con lega VUOTA (che restano vietati a monte da `league_allowed`).
DEFAULT_LEAGUE_STRATEGY = {"min_edge": 0.02, "kelly_mult": 0.5, "max_stake": 0.005}

FAVOURITES_ONLY = True   # mantenere: evita sfavorite ad alta quota
MIN_FAVOURITE_MARKET_PROB = 0.50   # prob. di mercato minima del favorito
KELLY_BASE = 0.015         # Kelly base frazionato (1.5% puro)
MAX_STAKE_PCT = 0.02       # cap 2% del bankroll (era 1%)

# PATCH CALIBRAZIONE bucket bassi (06/09): il gap residuo della config
# 1X2-only e' sui pareggi/trasferte (bucket 0.3-0.4 = 54% del volume con
# hit 29.4% vs 35 atteso; "2" trasferta -21.9%, "1" casa -8.76%). Sotto
# LOW_PROB_THRESHOLD la deviazione dal mercato viene compressa del fattore
# LOW_PROB_SHRINK: le pick X/2 marginali escono dal filtro EV e le
# superstiti hanno edge genuino. MISURATO sul backtest storico (catena 4+1
# run flat €20): closing -6.11% -> -3.08%, strong_value -0.3% -> +6.0%.
# (Il corrispondente shrink sui bucket ALTI e' stato misurato NEGATIVO
# nella config 1X2-only e NON e' in produzione: vedi AGENTS.md.)
LOW_PROB_THRESHOLD = 0.40
LOW_PROB_SHRINK = 0.85

# === ALIAS DEI NOMI LEGA (24/09/2026) ===
# Le competizioni arrivano da fonti DIVERSE (SX Bet usa le etichette del
# provider, the-odds-api le chiavi di `SPORTS_MAP`) e il gate di lega e' un
# confronto per stringa: un nome non allineato vale come "lega vietata".
# Misurato il 24/09/2026: la corsia multi-mercato salvava
# `Major League Soccer` mentre la lega in probation si chiama `MLS` ->
# candidati con EV +52% e edge +9.5pp scartati con "ROI negativo", che per
# quella lega NON e' vero (falso divieto su una lega ammessa).
# Solo alias UNIVOCI, mai fusioni fra leghe DIVERSE: `Brazil Serie B` (Serie B
# brasiliana) NON e' `Serie B` (italiana) e resta vietata com'e' giusto.
LEAGUE_ALIASES = {
    "major league soccer": "MLS",
    "usa mls": "MLS",
    "united states mls": "MLS",
    # Nazionali (29/09/2026): il nome canonico e' la chiave di
    # `odds_api.SPORTS_MAP` — "UEFA Nations League" / "Africa Cup of Nations".
    # ⚠️ TRAPPOLA MISURATA (29/09): NON mappare mai il nome canonico su un nome
    # ABBREVIATO ("Nations League", "AFCON"). Il gate risolve per STRINGA,
    # quindi un alias che riscrive il nome corretto in un nome assente da
    # STRATEGY_LEAGUES/PROBATION_LEAGUES lo BLOCCA: la prima stesura era
    # esattamente cosi' e risultava INERTE (`canonical_league('UEFA Nations
    # League')` -> 'Nations League' -> tier=blocked, prima e dopo la modifica).
    # Gli alias qui sotto portano le VARIANTI verso il nome canonico, mai il
    # contrario: la direzione e' cio' che rende l'alias utile o dannoso.
    "nations league": "UEFA Nations League",
    "afrika cup of nations": "Africa Cup of Nations",
    "caf africa cup of nations": "Africa Cup of Nations",
    # Coppe europee (23/10/2026): le varianti dei provider verso la chiave di
    # `SPORTS_MAP` ("Europa League", "Conference League"), MAI il contrario.
    "uefa europa league": "Europa League",
    "uefa conference league": "Conference League",
    "europa conference league": "Conference League",
    "uefa europa conference league": "Conference League",
    "uefa conference league qualifiers": "Conference League",
}


def canonical_league(league: str = "") -> str:
    """Nome canonico della lega (alias noti -> chiave della strategia).

    Difesa in profondita': il gate non deve dipendere da come una fonte
    scrive il nome. Una lega sconosciuta resta se stessa (nessun indovinare).
    """
    name = (league or "").strip()
    if not name:
        return ""
    return LEAGUE_ALIASES.get(name.lower(), name)


def get_league_strategy(league: str = "") -> dict:
    """Ritorna la configurazione strategica per una lega (core o probation).

    Tre stati: core (ROI positivo misurato) -> parametri generosi; probation
    (tier-2 dal 21/09) -> parametri severi (`PROBATION_STRATEGY`); tutto il
    resto -> fallback severo, che in pratica non arriva mai ai segnali perche'
    `league_allowed` li rifiuta prima. Lega vuota -> fallback.
    """
    league = canonical_league(league)
    if not league:
        return DEFAULT_LEAGUE_STRATEGY
    if league in STRATEGY_LEAGUES:
        return STRATEGY_LEAGUES[league]
    if league in PROBATION_LEAGUES:
        return PROBATION_STRATEGY
    return DEFAULT_LEAGUE_STRATEGY


def league_tier(league: str = "") -> str:
    """'core' | 'probation' | 'blocked' — utile per log e telemetria."""
    league = canonical_league(league)
    if league and league in STRATEGY_LEAGUES:
        return "core"
    if league and league in PROBATION_LEAGUES:
        return "probation"
    return "blocked"


def tournament_all_leagues() -> bool:
    """True se la CORSIA TORNEO apre anche le leghe `blocked` (12/10/2026).

    PERCHE' ESISTE (sprint "Gridiron Gauntlet", direttiva del proprietario del
    12/10): il torneo SX e' in formato **RETURN** ("wins increase your standing,
    losses will have no impact"), quindi i punti sono i ritorni LORDI e una
    scommessa persa NON toglie punti. Il gate di lega protegge il ROI della
    CASSA (leghe con ROI storico negativo), non il punteggio: nel torneo quella
    protezione non serve, mentre il numero di candidati e' esattamente cio' che
    decide la classifica.

    Doppia chiave, entrambe OBBLIGATORIE e lette a RUNTIME (nessun redeploy per
    spegnere, nessun residuo dopo il torneo):
      - `TOURNAMENT_MODE=1`  -> siamo nella modalita' torneo;
      - `TOURNAMENT_OPEN_LEAGUES=1` -> l'apertura e' stata decisa esplicitamente.
    Fail-closed: qualunque valore assente/non riconosciuto mantiene il gate di
    lega com'e' (una lega vietata non si apre per un env sbagliato).

    Cosa NON cambia quando e' attivo: la strategia applicata resta il FALLBACK
    SEVERO (`DEFAULT_LEAGUE_STRATEGY`: edge minimo piu' alto, kelly 0.5x, cap
    0.5%), la fascia quota `ODDS_MIN`-`ODDS_MAX` e il gate EV/oracolo. L'apertura allarga
    l'UNIVERSO delle partite valutabili, non le soglie che proteggono la cassa.
    """
    def _on(name: str) -> bool:
        try:
            raw = os.getenv(name)
        except Exception:                                    # pragma: no cover
            return False
        return str(raw or "").strip().lower() in ("1", "true", "yes", "on")

    try:
        return _on("TOURNAMENT_MODE") and _on("TOURNAMENT_OPEN_LEAGUES")
    except Exception:                                        # pragma: no cover
        return False


def league_allowed(league: str = "") -> bool:
    """True se la lega e' giocabile: core (ROI positivo) o probation (tier-2).

    Con la CORSIA TORNEO aperta (`tournament_all_leagues()`) sono ammesse anche
    le leghe `blocked`, che ricevono il fallback severo di strategia: nel
    formato RETURN le perdite non tolgono punti, quindi l'universo si allarga
    mentre il gate EV/oracolo resta il filtro sulla cassa.
    ⚠️ La lega VUOTA resta rifiutata anche col torneo aperto: senza sapere cosa
    si sta giocando non si ordina (fail-closed, stessa regola di `auto_bet`).
    """
    league = canonical_league(league)
    if not league:
        return False
    if league in STRATEGY_LEAGUES or league in PROBATION_LEAGUES:
        return True
    return tournament_all_leagues()


def is_core_league(league: str = "") -> bool:
    """True SOLO per le leghe Tier-1/Core (ROI storico misurato positivo).

    Direttiva 05/10/2026 ("League Tiering"): il refetch a PAGAMENTO
    dell'oracolo a linea e' riservato alle leghe Core. Le leghe in probation
    (tier-2) restano giocabili ma si valutano SOLO sulla cache passiva: 3
    crediti di fetch non si spendono su una lega di cui non e' stato ancora
    misurato un ROI positivo.

    E' la STESSA definizione di `league_tier` (un solo insieme di leghe nel
    progetto): un nome non canonico viene normalizzato prima del confronto,
    quindi non si puo' negare una lega Core per come la scrive la fonte.
    """
    return league_tier(league) == "core"


# ---------------------------------------------------------------------------
# SOGLIA EV PER TIER DI LEGA (08/10/2026) — seconda dimensione
# ---------------------------------------------------------------------------
# Direttiva del proprietario: oltre alla dimensione per MERCATO
# (`EV_MIN_LIQUID` sui mercati liquidi, `EV_MIN` sul resto) la soglia EV ha una
# seconda dimensione, il TIER della lega. Le leghe Tier-1/Core — mercati
# efficienti e profondi — accettano un vantaggio matematico inferiore
# (`EV_MIN_CORE`), mentre tutto il resto mantiene la soglia protettiva
# (`EV_MIN_OTHER`).
#
# ⚠️ PRECEDENZA "LA PIU' PERMISSIVA" (`min`) — revisione del proprietario
# dell'08/10/2026, dopo la prima stesura con `max`. Le due dimensioni NON si
# sommano: vince la soglia PIU' BASSA. Conseguenze dichiarate (scelte, non
# effetti collaterali):
#   • lega core + 1X2: min(2.5%, 1.5%) = **1.5%** — l'allargamento richiesto
#     per moltiplicare i volumi sui campionati validati (era 2.5%);
#   • lega core + mercato liquido (AH/OU/Totals/BTTS/ML): min(1.0%, 1.5%) =
#     **1.0%** — la soglia base dei mercati liquidi resta la sua;
#   • lega NON core (probation o sconosciuta) + qualunque mercato: resta la
#     sola soglia di MERCATO, quindi **1.0% sui mercati liquidi** anche per le
#     leghe in probation/blocked (con `max` erano a 2.5%);
#   • `EV_MIN_OTHER` (2.5%) resta il default di `ev_min_for_tier`, ma morde
#     solo se una soglia di MERCATO fosse piu' alta di 2.5%.
# ⚠️ Effetto netto: la dimensione TIER **non stringe piu' nulla** — puo' solo
# ABBASSARE la soglia delle leghe core. Chi vuole la prudenza geometrica della
# prima stesura (massimo fra le due dimensioni) rimetta `max`; chi vuole la
# soglia protettiva sui mercati liquidi senza toccare il codice ha
# `EV_MIN_LIQUID=0.025` (env, gia' in `preserve()` in `.railway/railway.ts`).
#: Env: `EV_MIN_CORE` (default **1,5%**: riportata su il 26/10/2026, era 1,0%
#: dal 14/10) e `EV_MIN_OTHER` (default `EV_MIN` = 2,5%).
#: ⚠️ Il vantaggio per le leghe core resta, ma dimezzato rispetto al 14/10:
#: la misura dice che a 1,0% il flusso peggiora, non che migliora.
EV_MIN_CORE = float(os.getenv("EV_MIN_CORE", "0.015"))
EV_MIN_OTHER = float(os.getenv("EV_MIN_OTHER", str(EV_MIN)))


def ev_min_for_tier(league: str = "") -> float:
    """Soglia EV del TIER di lega: core -> `EV_MIN_CORE`, altrimenti `EV_MIN_OTHER`."""
    return EV_MIN_CORE if is_core_league(league) else EV_MIN_OTHER


def ev_min(league: str = "", market: str | None = None) -> float:
    """Soglia EV EFFETTIVA: la PIU' PERMISSIVA fra mercato e tier di lega.

    UNICA definizione della regola combinata (direttiva 08/10/2026, revisione
    del proprietario): la usano `is_sane`, il Cervello
    (`base_ev_min`/`dynamic_ev_min`), il multi-mercato AH/OU e l'oracolo
    top-down — nessuna copia delle soglie altrove. Il tier puo' solo ABBASSARE
    la soglia (le leghe core a `EV_MIN_CORE`), mai alzarla: su 1X2 la lega core
    gioca a 1.5% invece di 2.5%, mentre sui mercati liquidi vince la soglia di
    mercato (1.0%). Fail-safe su una lega sconosciuta (nome vuoto o non
    canonico -> non e' core -> nessuna riduzione).
    """
    return min(ev_min_for_market(market), ev_min_for_tier(league))


# ---------------------------------------------------------------------------
# TIER DELL'ORACOLO A PAGAMENTO (07/10/2026) — direttiva del proprietario
# ---------------------------------------------------------------------------
# Il gate del 05/10 riservava il refetch a pagamento (`h2h,totals,spreads`, 3
# crediti) alle sole leghe Core. La misura del 06/10/07 mostra che questo
# escludeva TUTTI i candidati a linea realmente in finestra: MLS e Brasileirao
# (probation) erano gli unici pick OU/AH della notte, e restavano non
# prezzabili per scelta di tier.
#
# ⚠️ CAMBIARE QUESTO VALORE **NON** AUMENTA LA SPESA: il budget giornaliero
# (`odds_api.ORACLE_BUDGET_DAY`) e il tetto per lega
# (`ORACLE_MAX_CALLS_PER_LEAGUE`) restano identici. Cambia solo l'ALLOCAZIONE
# delle unita' gia' pagate — che vanno alla partita col kickoff piu' vicino
# (`line_oracle._league_plan` ordina per kickoff crescente), non al tier.
# Default `core` (comportamento invariato): includere le probation e' una
# decisione, non un default silenzioso.
ORACLE_PAID_TIERS_DEFAULT = "core"
# `blocked` (12/10/2026, sprint torneo): con la corsia torneo aperta le leghe
# fuori whitelist sono GIOCABILI (`tournament_all_leagues`) e senza il tier
# `blocked` resterebbero `tier_not_paid` -> `no_oracle` -> zero ordini. La
# spesa NON cresce oltre il tetto: `ORACLE_H2H_BUDGET_DAY` e il tetto per lega
# limitano comunque il numero di chiamate, cambia solo DOVE vengono allocate.
_VALID_TIERS = ("core", "probation", "blocked")


def oracle_paid_tiers() -> frozenset:
    """Tier ammessi al refetch a PAGAMENTO (`ORACLE_PAID_TIERS`, CSV).

    Fail-safe in DIREZIONE PRUDENTE: un valore assente, vuoto o con nomi
    sconosciuti ricade sul default `core`; i nomi validi non riconosciuti
    vengono scartati con un warning (meta' di un valore sbagliato non deve
    accendere una spesa diversa da quella dichiarata).
    """
    raw = os.getenv("ORACLE_PAID_TIERS")
    if raw is None or not str(raw).strip():
        return frozenset({ORACLE_PAID_TIERS_DEFAULT})
    out = set()
    for bit in str(raw).split(","):
        name = bit.strip().lower()
        if not name:
            continue
        if name == "all":
            # Scorciatoia dichiarata: `all` = tutti i tier ammessi al progetto
            # (core + probation + blocked). Un modo esplicito di dire "paga
            # tutto" senza elencare i tre nomi separatamente.
            out.update(_VALID_TIERS)
            continue
        if name in _VALID_TIERS:
            out.add(name)
        else:
            logger.warning("oracolo a pagamento: tier %r sconosciuto in "
                           "ORACLE_PAID_TIERS=%r, ignorato (validi: %s)",
                           name, raw, ", ".join(_VALID_TIERS))
    return frozenset(out) if out else frozenset({ORACLE_PAID_TIERS_DEFAULT})


def is_paid_oracle_league(league: str = "") -> bool:
    """True se quella lega puo' ricevere un refetch a PAGAMENTO dell'oracolo.

    UNICA definizione: la usano il fetch on-demand di `auto_bet` e lo
    scheduler `line_oracle._league_plan`, cosi' i due percorsi che spendono
    non possono divergere su CHI paga. Una lega non giocabile (blocked) non e'
    in nessun tier ammesso -> False.

    FAIL-CLOSED sulla lega VUOTA, e il controllo va fatto PRIMA del tier:
    `league_tier("")` risponde `blocked`, quindi col tier `blocked` ammesso
    (12/10/2026) una lega senza nome finirebbe nello stesso bucket delle leghe
    aperte dal torneo. Senza sapere cosa si sta giocando NON si paga una
    fetch (stessa regola di `league_allowed`).
    """
    if not canonical_league(league):
        return False
    return league_tier(league) in oracle_paid_tiers()


def compute_ev(prob: float, odds: float) -> float:
    """Expected Value: (prob * odds) - 1"""
    return (prob * odds) - 1.0


def combined_quota(odds: List[float]) -> float:
    """Quota combinata di una multipla (prodotto delle quote)."""
    prod = 1.0
    for o in odds:
        prod *= o
    return prod


def combined_probability(probs: List[float]) -> float:
    """Probabilita' congiunta di una multipla (prodotto, ipotesi indipendenza)."""
    prod = 1.0
    for p in probs:
        prod *= p
    return prod


# Frazioni e cap dedicati alle multiple
MULTIPLA_KELLY_FRACTION = 0.125
MULTIPLA_MAX_STAKE_PCT = 0.01
MULTIPLA_MAX_EV = 0.05


def multipla_stake(bankroll: float, prob: float, odds: float) -> float:
    """Stake per una multipla: 1/8 Kelly con cap 1% del bankroll."""
    kelly = kelly_fraction(prob, odds, MULTIPLA_KELLY_FRACTION)
    stake = bankroll * kelly
    cap = bankroll * MULTIPLA_MAX_STAKE_PCT
    return min(stake, cap)


def kelly_fraction(prob: float, odds: float, fraction: float = KELLY_BASE) -> float:
    """Kelly Criterion frazionario (default: KELLY_BASE) sulle quote DECIMALI.

    Formula CORRETTA (fix 27/10/2026): `f* = (p*o - 1) / (o - 1)`, cioe'
    `edge / (o - 1)` con `edge = p*o - 1` (il valore atteso per unita').

    ⚠️ STORIA — perche' questa riga e' cambiata. Fino al 27/10/2026 il
    progetto usava `(p*o - q) / o` (con `q = 1 - p`), che NON e' il Kelly:
    divide per `o` invece che per `o - 1` e **non usa il valore assoluto**
    dell'edge. Due conseguenze misurate sul ledger di produzione:

    1. **sovrastima di 2,5-7x** sulle quote che giochiamo (1.30-2.00): con
       p 0,5395 e quota 1,90 (EV +2,5%) la vecchia formula diceva 29,7% di
       bankroll, quella vera 2,78% — un fattore 10,7;
    2. **alloca denaro su scommesse a EV NEGATIVO**: con p 0,60 e quota 1,66
       (EV **-0,4%**) la vecchia formula dava 35,9% di bankroll, la vera
       **-0,6%** (cioe' zero, `no_edge`).

    Il risultato operativo era che il floor dell'exchange (1 USDC) sembrava
    "Kelly frazionato" quando invece era ~11x il rischio voluto: con 16,24
    USDC di equity ogni puntata rischiava il 6,2% del capitale contro lo
    0,56% di un quarto di Kelly. Da qui la serie di perdite del 21-26/10.

    La formula vive SOLO qui: `decision/stake_engine`, `adaptive_staking`,
    la schedina e i backtest delegano (tripwire in `test_aggressive_kelly`).
    """
    if odds <= 1.0:
        return 0.0
    kelly_full = (prob * odds - 1.0) / (odds - 1.0)
    return max(0.0, kelly_full * fraction)


def kelly_euro(bankroll: float, prob: float, odds: float,
               league: str = "", fraction: float | None = None) -> float:
    """Stake in euro con Kelly adattivo per lega e cap.

    Usa la strategia specifica della lega (kelly_mult e max_stake).
    """
    strat = get_league_strategy(league)
    frac = fraction if fraction is not None else KELLY_BASE * strat["kelly_mult"]
    kelly = kelly_fraction(prob, odds, frac)
    stake = bankroll * kelly
    cap = bankroll * strat["max_stake"]
    return min(stake, cap)


def market_edge(model_prob: float, market_prob: float) -> float:
    """Edge del modello sul mercato: model_prob - market_prob."""
    return _market_edge(model_prob, market_prob) or 0.0


def is_sane(prob: float, odds: float, ev: float,
            market_prob: float | None = None,
            league: str = "",
            market_edge_min: float | None = None,
            odds_max: float = ODDS_MAX,
            favourites_only: bool = FAVOURITES_ONLY,
            odds_movement: float | None = None,
            market: str = "") -> tuple[bool, str]:
    """Verifica se il segnale supera i filtri di sanita' con strategia per lega.

    Con market_prob disponibile, aggiunge il vincolo "beating the market":
    il segnale e' valore solo se il modello stima una probabilita' SUPERIORE
    a quella implicita nel mercato (devig).

    In piu' applica la STRATEGIA PER LEGA (valori 14/10/2026: core
    +1/+1,25pp, probation +2pp):
    - leghe fuori da core/probation sono vietate
    - edge minimo differenziato per lega (`get_league_strategy`)
    - fascia quote 1.20-2.00 (bordi INCLUSI: sotto `ODDS_MIN` rifiuta)
    - odds_movement: se la quota scende > 5%, segnale +20% (sharp money)

    La soglia EV effettiva e' la PIU' PERMISSIVA fra la dimensione MERCATO (i
    mercati liquidi AH/OU/Totals/BTTS/ML usano `EV_MIN_LIQUID` 0.5%, il resto
    `EV_MIN` 1.5%) e la dimensione TIER di LEGA (core `EV_MIN_CORE` 1.0%,
    altrimenti `EV_MIN_OTHER`) — vedi `ev_min`. Il tier quindi ABBASSA la
    soglia delle sole leghe core; sui mercati liquidi vale la soglia (piu'
    bassa) del mercato. Chiamanti storici senza `market` ne' `league` restano
    come prima. ⚠️ I valori qui sopra si aggiornano SOLO da questa memoria:
    un docstring che mente sulla soglia fa sbagliare la diagnosi (13/09).
    """
    # Soglia EFFETTIVA: la piu' permissiva fra mercato (04/10) e tier di lega
    # (08/10, revisione). `ev_min` e' l'unica definizione della regola.
    ev_min_eff = ev_min(league, market)
    # Lega vietata?
    if league and not league_allowed(league):
        return False, (f"lega '{league}' esclusa per ROI negativo "
                       "(strategia solo campionati vincenti)")
    if odds < ODDS_MIN:
        return False, f"quota troppo bassa ({odds:.2f} < {ODDS_MIN})"
    if odds > odds_max:
        return False, (f"quota troppo alta ({odds:.2f} > {odds_max})")
    # FASCIA MORTA (25/10/2026, SPENTA di default dal 26/10): 1.50-1.80 e' la
    # banda che aveva perso di piu' nel P/L (-16,4% su n=31) ma che il CLV
    # rimisurato (+2,59% su 199 campioni) NON distingue dalle fasce vicine.
    # Il gate si applica solo se qualcuno lo configura via `ODDS_EXCLUDE_*`.
    # Si esclude SOLO sui mercati di strategia: i mercati liquidi (OU/AH) non
    # hanno una misura equivalente e non si stringono per analogia.
    if not is_liquid_market(market) and in_dead_band(odds):
        return False, (f"quota nella fascia morta {ODDS_EXCLUDE_MIN:.2f}-"
                       f"{ODDS_EXCLUDE_MAX:.2f} (P/L negativo del 25/10 su "
                       f"31 bet, NON confermato dal CLV)")
    if favourites_only and market_prob is not None \
            and market_prob < MIN_FAVOURITE_MARKET_PROB:
        return False, (f"non e' il favorito di mercato (prob. "
                       f"{market_prob*100:.1f}% < "
                       f"{MIN_FAVOURITE_MARKET_PROB*100:.0f}%)")
    if ev < ev_min_eff:
        # `:g` e non `:.0f`: con EV_MIN = 2.5% il `:.0f` arrotterebbe a "2%"
        # (round-half-even) e il messaggio mentirebbe sulla soglia.
        return False, f"EV troppo basso ({ev*100:.1f}% < {ev_min_eff*100:g}%)"
    if ev > EV_MAX:
        return False, f"ANOMALIA: EV troppo alto ({ev*100:.1f}% > {EV_MAX*100:.0f}%)"
    if market_prob is not None:
        edge = prob - market_prob
        # Edge minimo differenziato per lega
        if market_edge_min is None:
            strat = get_league_strategy(league)
            market_edge_min = strat["min_edge"]
        if edge < market_edge_min:
            return False, (f"non batte il mercato (edge {edge*100:.1f}pp < "
                           f"{market_edge_min*100:.1f}pp vs prob. "
                           f"di mercato {market_prob*100:.1f}%)")
    # Odds movement: sharp money detection (bonus, non blocco)
    if odds_movement is not None and odds_movement <= MOVEMENT_THRESHOLD:
        pass  # Il movimento e' un BONUS, non un blocco
    return True, "OK"


def adjusted_probability(model_prob: float, market_prob: float | None,
                         odds: float, league: str = "",
                         model_samples: int = 0) -> float:
    """Probabilita' finale del segnale, calibrata sul mercato.

    Combina i due correttivi della ricerca:
    1. blending modello+mercato dinamico (riduce l'overconfidence del
       modello, adattandosi all'efficienza del mercato per lega);
    2. correzione favourite-longshot (sopra LONG_SHOT_ODDS la stima del
       modello viene compressa verso il mercato).
    """
    p = blend_probability(model_prob, market_prob,
                          league=league, odds=odds,
                          model_samples=model_samples)
    p = favourite_longshot_adjust(p, market_prob, odds)
    # PATCH CALIBRAZIONE bucket bassi: comprimi la deviazione dal mercato
    # quando la probabilità finale è bassa (pareggi/trasferte sovrastimati).
    if LOW_PROB_SHRINK < 1.0 and market_prob is not None and p < LOW_PROB_THRESHOLD:
        p = market_prob + (p - market_prob) * LOW_PROB_SHRINK
    return p


def eligible_favourites(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Filtra i candidati tenendo SOLO i favoriti netti (strategia 11/09).

    Un candidato qualifica se: ha la prob. di mercato devigata, questa e'
    >= MIN_FAVOURITE_MARKET_PROB, e' la PIU' ALTA tra i candidati dello
    stesso mercato e la sua quota non supera ODDS_MAX.

    Va chiamata sui candidati di UN SOLO mercato (i tre esiti 1X2, oppure
    le due linee di un Asian Handicap): il confronto "chi e' il favorito"
    ha senso solo tra esiti alternativi dello stesso mercato.
    Ritorna [] se nessun candidato qualifica: il match non genera segnali.
    """
    if not candidates:
        return []
    if not FAVOURITES_ONLY:
        return list(candidates)
    valid = [c for c in candidates if c.get("market_prob") is not None]
    if not valid:
        return []
    top = max(float(c["market_prob"]) for c in valid)
    out = []
    for c in valid:
        mp = float(c["market_prob"])
        if mp < MIN_FAVOURITE_MARKET_PROB:
            continue
        if mp < top - 1e-9:
            continue
        if float(c.get("quota") or 0.0) > ODDS_MAX:
            continue
        out.append(c)
    return out


def favourites_gate_reason() -> str:
    """Messaggio standard quando nessun esito e' un favorito netto."""
    return (f"nessun favorito netto (quota {ODDS_MIN:.2f}-{ODDS_MAX:.2f} e "
            f"prob. di mercato >= {MIN_FAVOURITE_MARKET_PROB*100:.0f}%)")


# === TIER GIOCABILI ===
# I tier che il bot considera giocabili: una definizione SOLA, importata da
# chiunque debba separare cio' che sarebbe stato giocato da cio' che i gate
# hanno scartato (`multi_market.PLAYABLE_STATUSES`, la diagnosi per mercato,
# la telemetria). Ricopiare la tripla e' il modo silenzioso di far divergere
# due misure: un tier nuovo conterebbe come giocabile in un posto e non
# nell'altro.
PLAYABLE_TIERS: tuple = ("value", "strong_value", "moderate")


def get_signal_tier(ev: float, market_edge_val: float | None = None) -> str:
    """Classifica un segnale in tier basato su EV e edge vs mercato.

    Tier giocabili: `PLAYABLE_TIERS` (strong_value, value, moderate).
    """
    if market_edge_val is not None:
        if market_edge_val >= MARKET_EDGE_STRONG:
            return "strong_value"
        elif market_edge_val >= MARKET_EDGE_MODERATE:
            return "value"
    if ev >= 0.05:
        return "strong_value"
    elif ev >= EV_MIN:
        return "value"
    return "moderate"


def filter_value_bets(odds_data: List[Dict[str, Any]],
                       ev_threshold: float = EV_MIN) -> List[Dict[str, Any]]:
    """Filtra le quote con EV positivo, applicando filtri di sanita' Pro
    con strategia per lega.

    Classifica ogni segnale in tier (strong_value/value/moderate).
    Backward-compatible: se la riga non ha "market_prob" mantiene il
    comportamento storico.
    """
    value_signals = []
    for odd in odds_data:
        prob = odd.get("probabilita", 0.0)
        quota = odd.get("quota_decimale", 1.0)
        league = odd.get("league", "")
        if prob <= 0 or quota <= 1.0:
            continue
        ev = compute_ev(prob, quota)
        market_prob = odd.get("market_prob")
        if market_prob is not None:
            edge = prob - market_prob
            odd["market_edge"] = edge
            odd["beats_market"] = edge >= MARKET_EDGE_MIN
        sane, reason = is_sane(prob, quota, ev, market_prob=market_prob,
                                league=league)
        odd["ev"] = ev
        odd["kelly"] = kelly_fraction(prob, quota)
        odd["sane"] = sane
        odd["sane_reason"] = reason
        if sane and ev >= ev_threshold:
            odd["tier"] = get_signal_tier(ev, odd.get("market_edge"))
            value_signals.append(odd)
    return sorted(value_signals, key=lambda x: x["ev"], reverse=True)


def get_pro_stake(bankroll: float, prob: float, odds: float,
                   league: str = "") -> dict:
    """Ritorna dizionario completo con stake, cap, e info filtri
    con Kelly adattivo per lega."""
    ev = compute_ev(prob, odds)
    sane, reason = is_sane(prob, odds, ev, league=league)
    stake = kelly_euro(bankroll, prob, odds, league)
    # Cap della strategia per lega, in euro: usato dai formatter Telegram
    # (`bot.format_segnale_pronto`) per mostrare il tetto reale applicato.
    cap_pct = get_league_strategy(league)["max_stake"]
    return {
        "ev": ev,
        "ev_pct": ev * 100,
        "sane": sane,
        "sane_reason": reason,
        "kelly_fraction": kelly_fraction(prob, odds),
        "kelly_pct": kelly_fraction(prob, odds) * 100,
        "stake_raw": bankroll * kelly_fraction(prob, odds),
        "stake": stake,
        "stake_cap": bankroll * cap_pct,
        "stake_cap_pct": cap_pct * 100,
        "stake_pct_of_bankroll": (stake / bankroll * 100) if bankroll > 0 else 0,
    }


def detect_odds_movement(current_odds: float, previous_odds: float) -> float:
    """Rileva il movimento della quota: negativo = quota scende = sharp money.

    Restituisce la variazione percentuale (negativa se la quota scende).
    Esempio: 2.00 -> 1.90 = -5.0% = segnale forte.
    """
    if previous_odds <= 0 or current_odds <= 0:
        return 0.0
    return (current_odds - previous_odds) / previous_odds


def get_optimal_timing(kickoff_str: str) -> dict:
    """Verifica se e' il momento ottimale per piazzare una scommessa.

    Ritorna: {"optimal": bool, "hours_before": float, "reason": str}
    Piazza da 2 minuti a 24h prima del kickoff per evitare insider info
    tardivo e troppo presto (quote noise). La finestra ESECUTIVA vera
    (T-180..T-2, direttiva 04/10/2026) e' in `auto_bet.T60_WINDOW_*`.
    """
    from datetime import datetime, timezone
    try:
        kickoff = datetime.fromisoformat(kickoff_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        hours_before = (kickoff - now).total_seconds() / 3600
        if hours_before < 0:
            return {"optimal": False, "hours_before": hours_before,
                    "reason": "kickoff passato"}
        if hours_before < TIMING_OPTIMAL_MIN_HOURS:
            return {"optimal": False, "hours_before": hours_before,
                    "reason": "troppo vicino al kickoff (< 30min)"}
        if hours_before > TIMING_OPTIMAL_MAX_HOURS:
            return {"optimal": False, "hours_before": hours_before,
                    "reason": "troppo presto (> 24h), quote noise"}
        return {"optimal": True, "hours_before": hours_before,
                "reason": "momento ottimale"}
    except Exception:
        return {"optimal": False, "hours_before": 0, "reason": "data invalida"}


def calculate_exposure(bets: list, bankroll: float) -> dict:
    """Calcola l'esposizione totale del portafoglio per risk management.

    Ritorna: {"total_stake": float, "total_pct": float,
              "per_league": dict, "max_league_pct": float,
              "correlation_risk": bool}
    """
    total_stake = sum(b.get("stake", 0) for b in bets)
    total_pct = total_stake / bankroll if bankroll > 0 else 0
    per_league: dict = {}
    for b in bets:
        league = b.get("league", "unknown")
        per_league[league] = per_league.get(league, 0) + b.get("stake", 0)
    if not per_league or bankroll <= 0:
        max_league_pct = 0.0
    else:
        max_league_pct = max(per_league.values()) / bankroll
    correlation_risk = max_league_pct > MAX_LEAGUE_EXPOSURE_PCT
    return {
        "total_stake": total_stake,
        "total_pct": total_pct,
        "per_league": {k: round(v, 2) for k, v in per_league.items()},
        "max_league_pct": max_league_pct,
        "correlation_risk": correlation_risk,
        "total_pct_ok": total_pct <= MAX_BLOCK_EXPOSURE_PCT,
    }
