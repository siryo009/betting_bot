"""Value bet filter e Kelly Criterion Pro"""

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
#   2) Fascia quote ODDS_MIN-ODDS_MAX (default 1.30-1.80)
#   3) Edge differenziato per efficienza lega
#   4) Kelly adattivo per lega (kelly_mult + max_stake)
#   5) Esclusione automatica leghe con CLV negativo cronico

EV_MIN = 0.02            # +2% minimo (frequenza + profitto)
EV_MAX = 0.20            # +20% massimo (oltre = anomalia)

# Fascia quote: favoriti NETTI (guardrail 11/09)
ODDS_MIN = 1.30          # quota minima: sotto, il ritorno non paga il rischio
ODDS_MAX = 1.80          # quota massima: sopra e' "pantano" o sfavorita

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
# Momento ottimale per piazzare: 30min - 24h prima del kickoff
TIMING_OPTIMAL_MIN_HOURS = 0.5   # 30 min minimi prima del kickoff
TIMING_OPTIMAL_MAX_HOURS = 24.0  # 24h massimo prima (troppo presto = noise)

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
STRATEGY_LEAGUES = {
    # Leghe vincenti (backtest)
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
# uguale: edge minimo +4pp (contro +2/+2.5pp), Kelly ridotto (0.4 contro
# 0.8-1.3) e cap di stake 0.5% (contro 1.5-2%). Cosi' il volume aggiunto e'
# a taglia minima — se il ledger live le promuove, si potra' allinearle al
# core (decisione futura, da misurare).
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
}

# Strategia di probation: piu' severa del core su edge, Kelly e cap.
PROBATION_STRATEGY = {"min_edge": 0.04, "kelly_mult": 0.4, "max_stake": 0.005}

# Fallback per leghe SCONOSCIUTE o assenti (il segnale resta vietato a monte:
# `league_allowed` rifiuta qualunque nome non in core/probation).
# NB: min_edge allineato alla soglia di edge corrente (+2pp dal 21/09): il
# fallback si applica solo ai segnali con lega VUOTA.
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


def league_allowed(league: str = "") -> bool:
    """True se la lega e' giocabile: core (ROI positivo) o probation (tier-2)."""
    league = canonical_league(league)
    return bool(league and (league in STRATEGY_LEAGUES
                            or league in PROBATION_LEAGUES))


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
    """Kelly Criterion frazionario (default: KELLY_BASE)"""
    if odds <= 1.0:
        return 0.0
    q = 1.0 - prob
    kelly_full = (prob * odds - q) / odds
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
            odds_movement: float | None = None) -> tuple[bool, str]:
    """Verifica se il segnale supera i filtri di sanita' con strategia per lega.

    Con market_prob disponibile, aggiunge il vincolo "beating the market":
    il segnale e' valore solo se il modello stima una probabilita' SUPERIORE
    a quella implicita nel mercato (devig).

    In piu' applica la STRATEGIA PER LEGA:
    - leghe fuori da core/probation sono vietate
    - edge minimo differenziato per lega (core +2/+2.5pp, probation +4pp)
    - fascia quote 1.30-1.80
    - odds_movement: se la quota scende > 5%, segnale +20% (sharp money)
    """
    # Lega vietata?
    if league and not league_allowed(league):
        return False, (f"lega '{league}' esclusa per ROI negativo "
                       "(strategia solo campionati vincenti)")
    if odds < ODDS_MIN:
        return False, f"quota troppo bassa ({odds:.2f} < {ODDS_MIN})"
    if odds > odds_max:
        return False, (f"quota troppo alta ({odds:.2f} > {odds_max})")
    if favourites_only and market_prob is not None \
            and market_prob < MIN_FAVOURITE_MARKET_PROB:
        return False, (f"non e' il favorito di mercato (prob. "
                       f"{market_prob*100:.1f}% < "
                       f"{MIN_FAVOURITE_MARKET_PROB*100:.0f}%)")
    if ev < EV_MIN:
        return False, f"EV troppo basso ({ev*100:.1f}% < {EV_MIN*100:.0f}%)"
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
    Piazza 0.5-24h prima del kickoff per evitare insider info tardivo
    e troppo presto (quote noise).
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
