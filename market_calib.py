"""
market_calib.py — Calibrazione del modello contro il mercato.

Implementa i due pilastri della strategia value betting confermati dalla
ricerca (2025-2026):

1. DEVIGGING
   La probabilita' "vera" di un esito e' quella implicita nelle quote di
   mercato PRIVATE del margine del bookmaker. La closing line e' la stima
   piu' accurata della probabilita' reale: se il modello non batte il
   mercato, l'edge e' un'illusione del modello.

2. BEATING THE MARKET
   Un segnale e' valore SOLO se il modello stima una probabilita'
   SUPERIORE alla probabilita' implicita del mercato (devig), non solo se
   l'EV contro un singolo bookmaker e' positivo. I bookmaker caricano piu'
   margine sui longshot (favourite-longshot bias): il metodo power (e Shin
   per mercati a piu' esiti) corregge questo bias dando piu' credito ai
   favoriti.

Metodi di devig supportati:
- "multiplicative": p_i / somma(p_i) — default semplice, buono su mercati
  bilanciati;
- "power": trova k tale che somma(p_i^k) = 1 — corregge il
  favourite-longshot bias, default consigliato per 1X2 e totals;
- "shin": risolve il parametro z del modello di insider trading di
  Shin (1993) — piu' aggressivo sul bias, consigliato su mercati lopsided
  a molti esiti.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

# --- Soglie di mercato (ricerca: servono almeno 3-5 punti percentuali
# --- di edge vs la closing line perche' il valore non sia rumore)
# Edge minimo vs mercato (ABBASSATO il 21/09/2026 su direttiva del
# proprietario, per aumentare il volume di pick): il modello deve stimare
# una prob. di vittoria SUPERIORE a quella devigata dei bookmaker di almeno
# +2pp; +4pp per "strong_value". Non si scommette mai il favorito "alla
# cieca".
# ⚠️ Direzione OPPOSTA alla prudenza dell'11/09 (+3pp/+5pp), che era stata
# ripristinata il 13/09 dopo il commit "FREQUENZA boost": la soglia e' una
# SCELTA di strategia del proprietario, non un guadagno tecnico. Il tripwire
# `test_risk_guards.TestFasciaFavoriti` e' stato riallineato di conseguenza
# (prima asseriva >= 0.03): la protezione e' la DECISIONE tracciata qui, non
# un numero che si puo' cambiare in silenzio.
MARKET_EDGE_MIN = 0.02          # +2pp vs mercato (direttiva 21/09/2026)
MARKET_EDGE_MODERATE = 0.02     # +2pp per moderate
MARKET_EDGE_STRONG = 0.04       # +4pp vs mercato (strong_value)

# Peso del modello nel blending modello+mercato (0.5 = pari peso).
# Il mercato e' quasi sempre piu' calibrato del modello: non superare 0.6.
BLEND_WEIGHT = 0.5

# Sopra questa quota il modello tende a sovrastimare (longshot bias):
# la probabilita' del modello viene compressa verso il mercato.
LONG_SHOT_ODDS = 3.5

# --- Efficiency score per lega/campionato (ricerca 2026) ---
# Mercati piu' liquidi = piu' efficienti = il mercato ha ragione piu' spesso.
# Score 0.0 = mercato inefficiente (il modello ha piu' spazio)
# Score 1.0 = mercato perfettamente efficiente (il mercato e' sempre giusto)
# Fonte: Unabated (2026), XCLSV Media (2026), analisi backtest su CLV.
LEAGUE_EFFICIENCY = {
    # Top 5 europei: molta liquidita', sharps presenti, mercato efficiente
    "Premier League": 0.85,
    "La Liga": 0.82,
    "Serie A": 0.80,
    "Bundesliga": 0.78,
    "Ligue 1": 0.75,
    # Coppe europee: efficiente ma meno partite -> campione ridotto
    "Champions League": 0.80,
    "Europa League": 0.70,
    "Conference League": 0.60,
    # Secondo tier: meno liquidita', il modello ha piu' spazio
    "Eredivisie": 0.65,
    "EFL Championship": 0.60,
    "Serie B": 0.55,
    "La Liga 2": 0.50,
    "2. Bundesliga": 0.55,
    "Ligue 2": 0.50,
    # Coppe nazionali e leghe minori: mercato meno efficiente
    "FA Cup": 0.55,
    "Coppa Italia": 0.55,
    "DFB Pokal": 0.55,
    "Coupe de France": 0.50,
    # Leghe internazionali: dati limitati, mercato meno attivo
    "MLS": 0.55,
    "Brasileir\u00e3o": 0.50,
    "Argentina Liga Profesional": 0.45,
    "Liga Portugal": 0.55,
    "Scottish Premiership": 0.50,
    # Leghe aggiunte l'11/09/2026 (roster + sync): mercati medi, poca
    # liquidita' sharp -> il modello ha piu' spazio del top 5.
    "Austrian Bundesliga": 0.45,
    "Russian Premier League": 0.45,
    "Turkey Super Lig": 0.50,
    "Belgian First Div": 0.50,
    "Greek Super League": 0.45,
    "Polish Ekstraklasa": 0.45,
    "Sweden Superettan": 0.35,
    "Brazil Serie B": 0.40,
    "3. Liga": 0.45,
    "Liga MX": 0.50,
    "J1 League": 0.40,
    "K League 1": 0.40,
    "A-League": 0.45,
    "Indian Super League": 0.35,
    "Saudi Pro League": 0.50,
    "Egyptian Premier League": 0.35,
    "South African PSL": 0.35,
    "Chile Liga Profesional": 0.40,
    "Colombia Primera A": 0.40,
    "Peru Liga 1": 0.35,
    "Ecuador Liga Pro": 0.35,
    "Bolivia Liga Profesional": 0.30,
    "Paraguay Primera Divisi\u00f3n": 0.30,
    "Uruguay Primera Divisi\u00f3n": 0.35,
    "Copa Libertadores": 0.55,
    "Copa Sudamericana": 0.45,
    "CONCACAF Champions Cup": 0.45,
    # Nazionali (29/09/2026): liquide durante le finestre FIFA ma campione
    # storico ridotto -> il modello ha piu' spazio del top 5 (stessa logica
    # delle leghe minori). Nomi = chiavi di `odds_api.SPORTS_MAP`.
    "UEFA Nations League": 0.55,
    "Africa Cup of Nations": 0.50,
}

# Default per leghe non mappate: mercato medio
LEAGUE_EFFICIENCY_DEFAULT = 0.50


def get_league_efficiency(league: str) -> float:
    """Score di efficienza del mercato per una lega (0-1).

    Score alto = mercato efficiente -> il blend pesa piu' il mercato.
    Score basso = mercato inefficiente -> il blend pesa piu' il modello.
    """
    return LEAGUE_EFFICIENCY.get(league, LEAGUE_EFFICIENCY_DEFAULT)


def dynamic_blend_weight(model_prob: float, market_prob: Optional[float],
                          league: str = "", odds: float = 2.0,
                          model_samples: int = 0) -> float:
    """Calcola il peso dinamico del modello nel blend.

    Il peso dipende da 3 fattori:
    1. Efficienza del mercato (per lega): mercato efficiente -> peso modello basso
    2. Confidence del modello: piu' dati storici -> peso modello alto
    3. Quote: longshot -> peso modello piu' basso (longshot bias)

    Range: 0.25 (mercato forte, modello debole) -> 0.65 (mercato debole, modello forte)
    """
    # Base: efficienza del mercato inversa
    eff = get_league_efficiency(league) if league else LEAGUE_EFFICIENCY_DEFAULT
    market_weight = eff  # quanto pesa il mercato nel blend
    model_weight = 1.0 - eff  # quanto pesa il modello

    # Adjust per confidence del modello: piu' campioni = piu' fiducia
    # model_samples = numero di partite storiche usate per il rating
    if model_samples > 0:
        import math
        confidence_bonus = min(0.15, 0.05 * math.log1p(model_samples / 10))
        model_weight += confidence_bonus
        market_weight -= confidence_bonus

    # Adjust per longshot bias: quote alte -> meno fiducia nel modello
    if odds > 3.0:
        longshot_penalty = min(0.10, (odds - 3.0) * 0.03)
        model_weight -= longshot_penalty
        market_weight += longshot_penalty

    # Normalizza e limita
    total = model_weight + market_weight
    if total <= 0:
        return BLEND_WEIGHT
    weight = model_weight / total
    return max(0.25, min(0.65, weight))

_MIN_P = 1e-9


# ---------------------------------------------------------------------------
# Devigging
# ---------------------------------------------------------------------------

def devig_multiplicative(probs: List[float]) -> List[float]:
    """Devig proporzionale: p_i / somma(p_i)."""
    total = sum(max(p, _MIN_P) for p in probs)
    if total <= 0:
        return [1.0 / len(probs)] * len(probs)
    return [max(p, _MIN_P) / total for p in probs]


def devig_power(probs: List[float], max_iter: int = 100) -> List[float]:
    """Devig con metodo power: trova k tale che somma(p_i^k) = 1.

    Correzione del favourite-longshot bias: i longshot assorbono piu'
    margine dei favoriti, quindi la probabilita' fair del favorito sale.
    Richiede ricerca iterativa (nessuna soluzione in forma chiusa).
    """
    n = len(probs)
    if n == 0:
        return []
    if n == 1:
        return [1.0]
    ps = [max(p, _MIN_P) for p in probs]
    lo, hi = 0.01, 20.0
    for _ in range(max_iter):
        k = (lo + hi) / 2.0
        s = sum(p ** k for p in ps)
        if s > 1.0:
            lo = k          # serve k piu' grande per ridurre la somma
        else:
            hi = k
    k = (lo + hi) / 2.0
    fair = [p ** k for p in ps]
    total = sum(fair)
    return [f / total for f in fair]


def shin_devig(ps: List[float], max_iter: int = 200) -> Tuple[List[float], float]:
    """Modello di Shin (1993): ritorna (probabilita' fair, parametro z).

    z e' la quota di denaro INFORMATO presente nel mercato (0 = mercato senza
    insider, cioe' il de-vig proporzionale; -> 1 = mercato quasi tutto
    informato). Si risolve per bisezione imponendo somma(p_fair) = 1 con:
        p_fair_i = (sqrt(z^2 + 4*(1-z)*p_i^2) - z) / (2*(1-z))
    Il modello corregge il **favourite-longshot bias** in modo piu' deciso del
    `power`: il margine viene attribuito quasi tutto ai longshot, quindi il
    favorito sale e lo sfavorito scende. z e' ESPOSTO (non solo consumato)
    perche' e' la misura diagnostica del modello: un z alto dice che il
    mercato e' guidato da denaro informato.
    """
    n = len(ps)
    if n == 0:
        return [], 0.0
    if n == 1:
        return [1.0], 0.0

    def _fair_for_z(z: float) -> List[float]:
        denom = 2.0 * (1.0 - z) if z < 1.0 else 1e-9
        out = []
        for p in ps:
            disc = z * z + 4.0 * (1.0 - z) * p * p
            out.append((max(disc, 0.0) ** 0.5 - z) / denom)
        return out

    lo, hi = 0.0, 1.0
    for _ in range(max_iter):
        z = (lo + hi) / 2.0
        s = sum(_fair_for_z(z))
        if s > 1.0:
            lo = z
        else:
            hi = z
    z_star = (lo + hi) / 2.0
    fair = _fair_for_z(z_star)
    total = sum(fair)
    if total <= 0:
        return devig_multiplicative(ps), 0.0
    return [f / total for f in fair], z_star


def _shin_fair(ps: List[float], max_iter: int = 200) -> List[float]:
    """Solo la distribuzione fair di Shin (retrocompatibilita')."""
    return shin_devig(ps, max_iter=max_iter)[0]


def devig(odds: List[float], method: str = "power") -> List[float]:
    """Quote decimali -> probabilita' fair (somma 1.0).

    method: "multiplicative" | "power" (default) | "shin".
    """
    probs = [1.0 / o for o in odds if o and o > 1.0]
    if not probs:
        return []
    if method == "multiplicative":
        return devig_multiplicative(probs)
    if method == "shin":
        return _shin_fair(probs)
    return devig_power(probs)


def devig_with_z(odds: List[float],
                 method: str = "power") -> Tuple[List[float], Optional[float]]:
    """Come `devig` ma espone anche il parametro z del modello di Shin.

    z e' `None` per i metodi che non lo hanno (power/multiplicative): un
    valore inventato sarebbe peggio di nessun valore. Unico punto di verita'
    del calcolo di z (i chiamanti NON ricopiano la formula).
    """
    probs = [1.0 / o for o in odds if o and o > 1.0]
    if not probs:
        return [], None
    if method == "shin":
        return shin_devig(probs)
    return devig(odds, method=method), None


def market_implied(odds_map: Dict[str, float],
                   method: str = "power") -> Optional[Dict[str, float]]:
    """Devig di un intero mercato.

    odds_map: {esito: quota_decimale} (es. {"1": 2.1, "X": 3.4, "2": 3.8}).

    Ritorna {esito: probabilita_fair, "overround": margine_brutto} oppure
    None se mancano almeno 2 esiti validi.
    """
    items = [(k, o) for k, o in odds_map.items() if o and o > 1.0]
    if len(items) < 2:
        return None
    keys = [k for k, _ in items]
    odds_list = [o for _, o in items]
    fair = devig(odds_list, method=method)
    overround = sum(1.0 / o for o in odds_list)
    result = {keys[i]: fair[i] for i in range(len(keys))}
    result["overround"] = overround
    return result


# ---------------------------------------------------------------------------
# Confronto modello vs mercato
# ---------------------------------------------------------------------------

def market_edge(model_prob: float, market_prob: Optional[float]) -> Optional[float]:
    """Edge del modello sul mercato in punti di probabilita'.

    > 0 => il modello vede piu' probabilita' di quanta il mercato ne prezza
    (candidato value). None se il mercato non e' disponibile.
    """
    if market_prob is None:
        return None
    return model_prob - market_prob


def is_beating_market(model_prob: float, market_prob: Optional[float],
                      threshold: float = MARKET_EDGE_MIN) -> bool:
    """True se il modello batte il mercato di almeno `threshold` punti."""
    if market_prob is None:
        # Senza riferimento di mercato non possiamo verificare: lasciamo
        # passare il segnale (comportamento storico) ma senza garanzia.
        return True
    return (model_prob - market_prob) >= threshold


def blend_probability(model_prob: float, market_prob: Optional[float],
                      weight: float = BLEND_WEIGHT,
                      league: str = "", odds: float = 2.0,
                      model_samples: int = 0) -> float:
    """Probabilita' finale = blend modello + mercato.

    Il mercato e' quasi sempre meglio calibrato del modello: mescolare le
    due stime riduce l'overconfidence del modello (causa n.1 dei falsi
    segnali nei backtest). Se il mercato manca, resta la stima del modello.

    Se vengono forniti league/odds/model_samples, usa il peso DINAMICO
    (dynamic_blend_weight) che adatta il blend in base all'efficienza del
    mercato, la confidence del modello e il longshot bias.
    """
    if market_prob is None:
        return model_prob
    # Se i parametri dinamici sono forniti, calcola il peso ottimale
    if league or model_samples > 0 or odds != 2.0:
        weight = dynamic_blend_weight(model_prob, market_prob,
                                      league=league, odds=odds,
                                      model_samples=model_samples)
    return weight * model_prob + (1.0 - weight) * market_prob


def favourite_longshot_adjust(model_prob: float, market_prob: Optional[float],
                              odds: float, long_shot_odds: float = LONG_SHOT_ODDS) -> float:
    """Corregge l'overconfidence del modello sui longshot.

    Il favourite-longshot bias dice che i longshot (quote alte) sono
    sopravvalutati dal pubblico e dai modelli naive. Sopra LONG_SHOT_ODDS
    la probabilita' del modello viene compressa verso la probabilita' di
    mercato, in modo proporzionale alla quota.
    """
    if market_prob is None or odds <= long_shot_odds:
        return model_prob
    t = min(1.0, (odds - long_shot_odds) / 2.0)   # 0..1 all'aumentare della quota
    return model_prob * (1.0 - 0.5 * t) + market_prob * (0.5 * t)


# ---------------------------------------------------------------------------
# CLV vig-free: Closing Line Value calcolato su closing line devigata
# ---------------------------------------------------------------------------

def clv_vig_free(signal_odds: float, closing_odds: float,
                  all_closing_odds: List[float] = None,
                  method: str = "power") -> Optional[float]:
    """CLV corretto per il vig: confronta la quota del segnale con la
    closing line DEVIGATA (fair probability).

    Il CLV tradizionale (signal / closing - 1) sovrastima l'edge perche'
    la closing line include il vig del bookmaker. Questa funzione:
    1. Deviga la closing line (se disponibili tutti gli esiti del mercato)
    2. Converte la quota fair in quota equivalente (senza vig)
    3. Calcola CLV = signal_odds / fair_closing_odds - 1

    Args:
        signal_odds: quota presa al momento del segnale.
        closing_odds: closing price dell'esito scommesso.
        all_closing_odds: [closing_1, closing_X, closing_2] del mercato
                          completo (serve per deviggare correttamente).
                          Se None, usa una stima del vig da closing_odds
                          solo (meno preciso).
        method: metodo di devig ("power" di default, corregge longshot bias).

    Returns:
        CLV vig-free in decimale (es. 0.04 = +4%) o None se invalido.
    """
    if signal_odds <= 1.0 or closing_odds <= 1.0:
        return None

    if all_closing_odds and len(all_closing_odds) >= 2:
        # Devig del mercato completo: probabilita' fair.
        fair_probs = devig(all_closing_odds, method=method)
        # Trova la posizione dell'esito scommesso nella lista.
        # approx: usa la probabilita' implicita per trovare l'indice
        # corrispondente.
        implied_closing = 1.0 / closing_odds
        best_idx = min(range(len(fair_probs)),
                       key=lambda i: abs(fair_probs[i] - implied_closing))
        fair_prob = fair_probs[best_idx]
        # Quota equivalente fair (senza vig)
        fair_closing_odds = 1.0 / fair_prob if fair_prob > _MIN_P else closing_odds
    else:
        # Stima del vig: overround = sum(1/odds) per tutti gli esiti.
        # Se abbiamo solo 1 closing odds, stima il vig tipico del calcio
        # (~3-5% per soft book, ~2% per Pinnacle).
        implied = 1.0 / closing_odds
        # Stima conservativa: il vig e' distribuito proporzionalmente.
        # Per un mercato 1X2 tipico, l'overround e' ~1.03-1.06.
        overround_est = 1.04  # stima media per soft books
        fair_prob = implied / overround_est
        fair_closing_odds = 1.0 / fair_prob if fair_prob > _MIN_P else closing_odds

    if fair_closing_odds <= 1.0:
        return None

    return (signal_odds / fair_closing_odds) - 1.0


def clv_raw(signal_odds: float, closing_odds: float) -> Optional[float]:
    """CLV tradizionale (senza devig): signal / closing - 1.

    Mantenuto per backward compatibility e confronto. Per il report
    professionale, usare sempre clv_vig_free().
    """
    if signal_odds <= 1.0 or closing_odds <= 1.0:
        return None
    return (signal_odds / closing_odds) - 1.0


def vig_percentage(all_odds: List[float]) -> Optional[float]:
    """Margine (vig) del mercato in percentuale.

    overround = sum(1/odds) per tutti gli esiti.
    vig% = (overround - 1) * 100.

    Args:
        all_odds: lista di quote decimali per tutti gli esiti del mercato.

    Returns:
        Vig in percentuale (es. 4.5 = 4.5%) o None se dati insufficienti.
    """
    valid = [o for o in all_odds if o and o > 1.0]
    if len(valid) < 2:
        return None
    overround = sum(1.0 / o for o in valid)
    return (overround - 1.0) * 100.0
