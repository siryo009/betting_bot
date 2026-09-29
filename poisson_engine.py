import math
from typing import Dict, Tuple

try:
    from leagues_data import ALL_LEAGUES
except ImportError:
    ALL_LEAGUES = {}

try:
    from leagues_data import LEAGUE_AVGS
except ImportError:
    LEAGUE_AVGS = {}

SERIE_A_2025_26 = ALL_LEAGUES.get("Serie A", {})

AVG_HOME_GOALS = 1.52
AVG_AWAY_GOALS = 1.28

def poisson_pmf(k: int, lam: float) -> float:
    return (lam ** k) * math.exp(-lam) / math.factorial(k)

def prob_score(home_goals: int, away_goals: int, lam_h: float, lam_a: float) -> float:
    return poisson_pmf(home_goals, lam_h) * poisson_pmf(away_goals, lam_a)


RHO = -0.15  # Correzione Dixon-Coles (draw correlation)

# ---------------------------------------------------------------------------
# NUCLEO VETTORIALIZZATO (direttiva 29/09/2026)
# ---------------------------------------------------------------------------
# La matrice dei punteggi era un doppio ciclo Python: O(K^2) con K=10 sono 121
# celle, e OGNI mercato (1X2, OU, BTTS, AH, margine) la RICALCOLAVA da zero.
# Qui la matrice si costruisce UNA volta con numpy e le aggregazioni sono
# maschere vettoriali.
#
# ⚠️ LA MATEMATICA NON CAMBIA: la correzione Dixon-Coles (RHO) resta applicata
# alle stesse quattro celle (0,0) (0,1) (1,0) (1,1) e la matrice e' normalizzata
# a somma 1. Proprio perche' il modello non si tocca, la parita' con la formula
# originale e' verificata da `test_poisson_vectorized.py` su una griglia di
# (lam_h, lam_a, max_goals): una vettorizzazione che cambia i numeri e' un
# cambio di strategia mascherato da ottimizzazione.
#
# numpy e' importato PIGRO (il primo uso paga l'import): `poisson_engine` e'
# importato da moduli che non toccano mai il modello (es. il bot per un
# comando) e non deve rallentare l'avvio.

_NUMPY = None


def _np():
    """numpy in cache a livello di modulo (import pigro, una volta sola)."""
    global _NUMPY
    if _NUMPY is None:
        import numpy
        _NUMPY = numpy
    return _NUMPY


def _pmf_vector(max_goals: int, lam: float):
    """PMF di Poisson su 0..max_goals (stessa formula di `poisson_pmf`).

    I fattoriali si costruiscono incrementalmente: `math.factorial` per cella
    avrebbe riportato il ciclo dentro la vettorizzazione.
    """
    np = _np()
    k = np.arange(max_goals + 1, dtype=float)
    fact = np.ones(max_goals + 1, dtype=float)
    for i in range(1, max_goals + 1):
        fact[i] = fact[i - 1] * i
    lam = float(lam)
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        pmf = (lam ** k) * math.exp(-lam) / fact
    return np.nan_to_num(pmf, nan=0.0, posinf=0.0)


def _probs_grid(lam_h: float, lam_a: float, max_goals: int = 10):
    """FONTE UNICA del modello: matrice punteggi (numpy) con Dixon-Coles.

    Ritorna un array (max_goals+1)x(max_goals+1) normalizzato a somma 1, dove
    `grid[hg, ag]` e' la probabilita' del punteggio esatto.
    """
    np = _np()
    grid = np.outer(_pmf_vector(max_goals, lam_h),
                    _pmf_vector(max_goals, lam_a))
    if max_goals >= 1:  # le quattro celle corrette esistono solo con K>=1
        grid[0, 0] *= (1.0 - lam_h * lam_a * RHO)
        grid[0, 1] *= (1.0 + lam_h * RHO)
        grid[1, 0] *= (1.0 + lam_a * RHO)
        grid[1, 1] *= (1.0 - RHO)
    total = float(grid.sum())
    return grid / total if total else grid


def _margin_grid(max_goals: int):
    """Maschere riusabili: indici di casa/trasferta e totale/margine gol."""
    np = _np()
    idx = np.arange(max_goals + 1)
    return (idx[:, None],                       # gol casa per cella
            idx[None, :],                       # gol trasferta per cella
            idx[:, None] + idx[None, :],        # totale gol
            (idx[:, None] - idx[None, :]).astype(float))  # margine (casa-trasferta)


def _probs_matrix(lam_h: float, lam_a: float, max_goals: int = 10) -> Dict:
    """Vista `{(hg, ag): p}` della griglia vettorizzata (compatibilita').

    La matematica vive in `_probs_grid`: questa e' solo una proiezione a dict
    per chi consumava la vecchia forma. Nessuna formula duplicata.
    """
    grid = _probs_grid(lam_h, lam_a, max_goals)
    return {(hg, ag): float(grid[hg, ag])
            for hg in range(max_goals + 1) for ag in range(max_goals + 1)}

def prob_1x2(lam_h: float, lam_a: float, max_goals: int = 10) -> Tuple[float, float, float]:
    """(p1, pX, p2) dalla griglia vettorizzata (maschere sul confronto gol)."""
    grid = _probs_grid(lam_h, lam_a, max_goals)
    hg, ag, _tot, _margin = _margin_grid(max_goals)
    p1 = float(grid[hg > ag].sum())
    px = float(grid[hg == ag].sum())
    p2 = float(grid[hg < ag].sum())
    return p1, px, p2

def prob_over_under(lam_h: float, lam_a: float, threshold: float = 2.5, max_goals: int = 10) -> Tuple[float, float]:
    grid = _probs_grid(lam_h, lam_a, max_goals)
    _hg, _ag, totals, _margin = _margin_grid(max_goals)
    p_over = float(grid[totals > threshold].sum())
    return p_over, 1.0 - p_over

def ou_outcome_probs(lam_h: float, lam_a: float, threshold: float,
                     side: str = "over", max_goals: int = 10):
    """Probabilita' (p_win, p_push, p_lose) a stake pieno per un Over/Under.

    Gemella di `ah_outcome_probs` per il multi-mercato (19/09/2026): serve a
    distinguere il push (linea INTERA con totale esattamente uguale alla
    linea: la puntata viene restituita, il P/L e' 0) dalla perdita. Sulle
    linee .5 il push non esiste (p_push = 0).

    Le quarter line (.25/.75) valgono come due mezze puntate, come nell'AH:
    il push di una meta' viene pesato mezzo.
    """
    grid = _probs_grid(lam_h, lam_a, max_goals)
    _hg, _ag, totals, _margin = _margin_grid(max_goals)
    if abs(float(threshold) * 2) % 1 != 0:  # quarter line -> due mezze puntate
        lo = math.floor(float(threshold) * 2) / 2.0
        halves = (lo, lo + 0.5)
        share = 0.5
    else:
        halves = (float(threshold),)
        share = 1.0
    p_win = p_push = p_lose = 0.0
    for hline in halves:
        p_win += share * float(grid[totals > hline].sum())
        p_push += share * float(grid[totals == hline].sum())
        p_lose += share * float(grid[totals < hline].sum())
    if side == "under":
        p_win, p_lose = p_lose, p_win
    return p_win, p_push, p_lose


def prob_btts(lam_h: float, lam_a: float, max_goals: int = 10) -> float:
    grid = _probs_grid(lam_h, lam_a, max_goals)
    hg, ag, _tot, _margin = _margin_grid(max_goals)
    return float(grid[(hg >= 1) & (ag >= 1)].sum())

# --- Asian Handicap (mercato a 2 esiti, margini bookmaker piu' bassi) ---
# Linee AH supportate: da -3 a +3 con passo 0.25 (intere, mezze, quarter).
AH_LINES = [i / 4.0 for i in range(-12, 13)]


def margin_distribution(lam_h: float, lam_a: float, max_goals: int = 10) -> Dict[int, float]:
    """Distribuzione del margine di gol (home - away) secondo il modello.

    Semantica IDENTICA a prima (dict margine -> probabilita', incluso 0):
    cambia solo il modo di sommarla (maschere vettoriali sul margine).
    """
    np = _np()
    grid = _probs_grid(lam_h, lam_a, max_goals)
    _hg, _ag, _tot, margins = _margin_grid(max_goals)
    dist: Dict[int, float] = {}
    for adv in np.unique(margins):
        dist[int(adv)] = float(grid[margins == adv].sum())
    return dist


def ah_outcome_probs(lam_h: float, lam_a: float, line: float, side: str = "home",
                     max_goals: int = 10) -> Tuple[float, float, float]:
    """Probabilita' (p_win, p_push, p_lose) a stake pieno per una linea AH.

    `line` e' la linea del LATO scelto col suo segno (es. home -0.75 = la
    squadra di casa dà 0.75 gol; away +0.25 = l'ospite riceve 0.25 gol).
    Le quarter line (.25/.75) valgono come due mezze puntate: il push
    di una meta' viene pesato mezzo.
    """
    grid = _probs_grid(lam_h, lam_a, max_goals)
    _hg, _ag, _tot, margins = _margin_grid(max_goals)
    if abs(line * 2) % 1 != 0:  # quarter line -> due mezze puntate
        lo = math.floor(line * 2) / 2.0
        halves = (lo, lo + 0.5)
        share = 0.5
    else:
        halves = (float(line),)
        share = 1.0
    p_win = p_push = p_lose = 0.0
    for hline in halves:
        net = (margins + hline) if side == "home" else (-margins + hline)
        p_win += share * float(grid[net > 0].sum())
        p_push += share * float(grid[net == 0].sum())
        p_lose += share * float(grid[net < 0].sum())
    return p_win, p_push, p_lose


def _find_team_league(team_name: str):
    for league, teams in ALL_LEAGUES.items():
        if team_name in teams:
            return league
    return None

# Profilo neutro di lega per le squadre fuori roster: con la copertura
# mondiale ogni partita deve essere analizzabile (le medie di lega fanno da
# prior, i rating reali arrivano con i risultati accumulati).
_DEFAULT_TEAM = {"attack_home": 1.0, "attack_away": 1.0,
                  "defense_home": 1.0, "defense_away": 1.0}

def _team_profile(team_name: str, league):
    if league is not None:
        return ALL_LEAGUES[league][team_name]
    return _DEFAULT_TEAM

def expected_goals(home_team: str, away_team: str):
    home_league = _find_team_league(home_team)
    away_league = _find_team_league(away_team)

    if home_league == away_league and home_league is not None:
        avg_hg, avg_ag = LEAGUE_AVGS.get(home_league, (1.50, 1.30))
    else:
        avg_hg, avg_ag = 1.50, 1.30

    try:
        from rating_engine import get_rating
        _rh = get_rating(home_team) or {}
        _ra = get_rating(away_team) or {}
    except Exception:
        _rh, _ra = {}, {}
    if _rh and _ra:
        home_data, away_data = _rh, _ra
    else:
        home_data = _team_profile(home_team, home_league)
        away_data = _team_profile(away_team, away_league)
    lam_h = avg_hg * home_data["attack_home"] * away_data["defense_away"]
    lam_a = avg_ag * away_data["attack_away"] * home_data["defense_home"]
    return lam_h, lam_a
