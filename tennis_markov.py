"""tennis_markov.py — catena di MARKOV del tennis: punto -> game -> set -> match (15/10/2026).

Direttiva del proprietario: un motore probabilistico in puro Python basato sulle
**percentuali di servizio e risposta per superficie**, con matrici di transizione
**punto per punto**, per affinare la probabilita' di vittoria **pre-match e live**
nei mercati tennistici.

PERCHE' QUESTO NON E' UNA SECONDA FORMULA (regola di progetto: "ogni formula vive
in UN solo posto" — i tre bug gravi del progetto sono tutti logica copiata in due
posti):
- **hold/break**: `tennis_quant.break_rates` / `hold_rates_from_prob` (con la sua
  inversione di attribuzione corretta il 03/10) restano la fonte dei tassi di
  servizio; qui non si stima nulla da zero;
- **ELO per superficie**: `tennis_quant.SurfaceElo` (che eredita da
  `tennis_sandbox.TennisElo`, time-decay 30/60/365gg) resta il modello storico;
- **de-vig Shin**: `tennis_quant.shin_probabilities` -> `market_calib` (nessuna
  copia);
- **confronto col mercato**: `tennis_quant.expected_value` (stessa definizione di
  EV di tutto il progetto).

COSA AGGIUNGE: il modello di `tennis_quant` stima il match da due processi di
Poisson sui BREAK (approssimazione dichiarata). Qui il match si costruisce
**esattamente** dalla struttura del gioco: probabilita' del punto al servizio ->
game (con deuce e vantaggi) -> set (con tie-break a 7 punti e rotazione del
servizio) -> match (best-of-3 o best-of-5). E soprattutto: dalla stessa catena si
legge la probabilita' **LIVE** a qualunque punteggio (punto, game, set), che il
modello Poisson non sa calcolare.

DIREZIONE DEL MODULO (come il resto della roadmap): **misura e propone**. Non e'
collegato al percorso ordini: la corsia tennis continua a decidere sull'oracolo
sharp (`tennis_lane`), e questo motore vive in telemetria. Il motivo e' lo stesso
del 22/09 (strategia congelata): cambiare la probabilita' del modello cambia gli
EV di tutte le corsie, quindi e' una decisione del proprietario.

DUE DETTAGLI DICHIARATI (non nascosti):
1. le **percentuali di servizio reali** per giocatore non sono disponibili senza
   un provider a pagamento: quando non sono iniettate, i tassi si derivano dalla
   probabilita' del match con il fallback DICHIARATO di `tennis_quant`
   (`hold_rates_from_prob`) e un **delta di superficie** che e' un PRIORE del
   progetto, non una misura (`hold_source` lo dichiara sempre);
2. il tie-break si risolve con una catena **esatta** nella fase estesa (6-6 in
   poi, ciclo di servizio di 4 punti) e con una DP ricorsiva prima — nessuna
   approssimazione del pareggio a meta'.

Confini (tripwire nei test): nessuna scrittura, zero rete, zero crediti, **zero
ordini**, nessuna soglia di strategia nel sorgente, `import tennis_markov` non
carica `auto_bet`/`bot`/`tracker`/`odds_api`.

CLI:
    venv/bin/python tennis_markov.py --prob 0.62 [--surface clay] [--live "1-0,3-2,30-15"]
    venv/bin/python tennis_markov.py --compare --prob 0.62 [--json]
"""
from __future__ import annotations

import json
import logging
import math
import os
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configurazione (env letta a RUNTIME)
# ---------------------------------------------------------------------------
DEFAULT_BEST_OF = 3
DEFAULT_TIEBREAK_TO = 7
MAX_RECURSION_SCORE = 40          # guardia: la DP non esplode mai

#: DELTA DI SUPERFICIE sulla tenuta del servizio — **PRIORI DEL PROGETTO**,
#: non misure: l'erba favorisce il servizio (palla bassa, rimbalzo rapido), la
#: terra lo penalizza (rimbalzo alto e lento, il risponditore ha piu' tempo).
#: Sono applicati SOLO quando non c'e' uno stat provider reale e sono
#: dichiarati in `hold_source`. Override con `TENNIS_MARKOV_SURFACE_DELTA`.
SURFACE_DELTA = {"grass": 0.02, "hard": 0.0, "clay": -0.025}
SURFACE_DELTA_ENV = "TENNIS_MARKOV_SURFACE_DELTA"


def _env_float(name: str, default: float, *, minimum: Optional[float] = None,
               maximum: Optional[float] = None) -> float:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning("tennis_markov: %s non numerico (%r): uso %s", name, raw,
                       default)
        return default
    if not math.isfinite(value) or (minimum is not None and value < minimum) or \
            (maximum is not None and value > maximum):
        logger.warning("tennis_markov: %s fuori range (%s): uso %s", name, raw,
                       default)
        return default
    return value


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    return int(_env_float(name, default, minimum=float(minimum)))


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def surface_delta(surface: Any, *, raw: Optional[str] = None) -> Tuple[float, str]:
    """Delta di tenuta servizio per superficie + la sua PROVENIENZA dichiarata.

    `raw` (o l'env) permette di sovrascrivere la tabella con una mappa
    `superficie:delta` — un valore malformato viene scartato con warning e la
    tabella di default resta in vigore.
    """
    table = dict(SURFACE_DELTA)
    source = "prior_surface"
    raw = raw if raw is not None else os.getenv(SURFACE_DELTA_ENV)
    if raw and str(raw).strip():
        parsed: Dict[str, float] = {}
        for chunk in str(raw).split(","):
            parts = chunk.split(":")
            if len(parts) != 2:
                continue
            try:
                parsed[parts[0].strip().lower()] = float(parts[1])
            except (TypeError, ValueError):
                logger.warning("tennis_markov: delta superficie malformato %r",
                               chunk)
        if parsed:
            table, source = parsed, "env_surface_delta"
    key = str(surface or "").strip().lower()
    if not key:
        return 0.0, "surface_unknown"
    if key not in table:
        # Superficie NON riconosciuta: nessun delta e provenienza dichiarata.
        # Indovinare una superficie contaminerebbe il tasso di servizio (la
        # stessa regola di `tennis_sandbox.detect_surface`).
        return 0.0, "surface_unknown"
    return float(table[key]), source


def config() -> Dict[str, Any]:
    """Configurazione efficace (una sola lettura per valutazione)."""
    return {
        "enabled": _env_bool("TENNIS_MARKOV_ENABLED", True),
        "best_of": _env_int("TENNIS_MARKOV_BEST_OF", DEFAULT_BEST_OF,
                            minimum=1),
        "tiebreak_to": _env_int("TENNIS_MARKOV_TIEBREAK_TO", DEFAULT_TIEBREAK_TO,
                                minimum=1),
        "tiebreak": _env_bool("TENNIS_MARKOV_TIEBREAK", True),
    }


# ---------------------------------------------------------------------------
# 1) PUNTO -> GAME
# ---------------------------------------------------------------------------
def game_prob(p: float) -> float:
    """P(il servitore vince il game) da `p` = P(vince il punto al servizio).

    Formula CHIUSA (Bartlett): le vittorie senza deuce `p^4 (1 + 4q + 10q^2)`
    piu' il percorso di deuce, che e' **P(raggiungere il 3-3)** `20 p^3 q^3`
    per **P(vincere da deuce)** `p^2 / (p^2 + q^2)`.

    ⚠️ La prima stesura usava `20 p^3 q^3 / (1 - 2pq)` (una forma che circola ma
    e' ERRATA: a p = 0.6 da' 1.076, cioe' piu' di 1). L'ha colta il test di
    parita' con la DP `game_prob_dp`, che resta come tripwire: se le due non
    coincidono su tutta la griglia, la formula chiusa ha un errore di deuce.
    """
    try:
        p = max(0.0, min(1.0, float(p)))
    except (TypeError, ValueError):
        # Probabilita' illeggibile: neutro dichiarato, mai un'eccezione (il
        # modulo e' chiamato anche da diagnostiche e CLI).
        return 0.5
    q = 1.0 - p
    if abs(p - 0.5) < 1e-12:
        return 0.5
    try:
        first = p ** 4 * (1.0 + 4.0 * q + 10.0 * q ** 2)
        deuce_reach = 20.0 * p ** 3 * q ** 3
        deuce_win = (p * p) / (p * p + q * q) if (p * p + q * q) > 0 else 0.5
        value = first + deuce_reach * deuce_win
    except (ValueError, OverflowError, ZeroDivisionError):   # pragma: no cover
        return 0.5
    return max(0.0, min(1.0, value))


def game_prob_dp(p: float, *, points: int = 0, points_against: int = 0) -> float:
    """P(il servitore vince il game) da un punteggio qualsiasi (per il LIVE).

    `points` sono i punti gia' vinti dal servitore nella scala 0,1,2,3 (3 = 40).
    Nella zona deuce/vanaggio la probabilita' si chiude in forma esatta:
    `P(deuce) = p^2 / (p^2 + q^2)`, `P(vanaggio servitore) = p + q*P(deuce)`.
    """
    p = max(0.0, min(1.0, float(p)))
    q = 1.0 - p
    a, b = int(points), int(points_against)
    if a < 0 or b < 0:
        return 0.5
    if a >= 4 and a - b >= 2:
        return 1.0
    if b >= 4 and b - a >= 2:
        return 0.0
    if a >= 3 and b >= 3:
        deuce = (p * p) / (p * p + q * q) if (p * p + q * q) > 0 else 0.5
        if a == b:
            return deuce
        return p + q * deuce if a > b else p * deuce
    memo: Dict[Tuple[int, int], float] = {}

    def _rec(x: int, y: int) -> float:
        if x >= 4 and x - y >= 2:
            return 1.0
        if y >= 4 and y - x >= 2:
            return 0.0
        if x >= 3 and y >= 3:
            deuce = (p * p) / (p * p + q * q) if (p * p + q * q) > 0 else 0.5
            if x == y:
                return deuce
            return p + q * deuce if x > y else p * deuce
        if (x, y) in memo:
            return memo[(x, y)]
        value = p * _rec(x + 1, y) + q * _rec(x, y + 1)
        memo[(x, y)] = value
        return value

    return max(0.0, min(1.0, _rec(a, b)))


# ---------------------------------------------------------------------------
# 2) GAME -> SET (tie-break incluso, risolto esattamente nella fase estesa)
# ---------------------------------------------------------------------------
def _tiebreak_server(point_index: int) -> str:
    """Chi serve il punto `point_index` (0-based) nel tie-break.

    Regola ufficiale: A serve il punto 1; poi B serve due punti, A due, B due...
    Il pattern e' periodico di 4 punti (dopo il primo).
    """
    if point_index <= 0:
        return "a"
    block = (point_index - 1) // 2
    return "a" if block % 2 == 1 else "b"


def _tiebreak_extended(p_a: float, p_b: float, *, total_points: int,
                       target: int) -> Dict[int, float]:
    """P(A vince il tie-break) da un punteggio PARI nella fase estesa.

    Nella fase estesa (entrambi >= target-1) servono DUE punti di margine e il
    servizio cicla ogni 4 punti: la catena e' stazionaria, quindi si risolve in
    forma CHIUSA il sistema lineare sugli stati (differenza -1, 0, +1) x
    (posizione nel ciclo). Niente pareggio approssimato a 0.5.
    """
    states = [(d, k) for d in (-1, 0, 1) for k in range(4)]
    index = {s: i for i, s in enumerate(states)}
    n = len(states)
    mat = np.eye(n, dtype=float)
    rhs = np.zeros(n, dtype=float)
    for (d, k) in states:
        i = index[(d, k)]
        server = _tiebreak_server(total_points + k)
        pw = p_a if server == "a" else 1.0 - p_b
        for win, prob in ((True, pw), (False, 1.0 - pw)):
            nd = d + 1 if win else d - 1
            if nd >= 2:
                rhs[i] += prob * 1.0
                continue
            if nd <= -2:
                continue
            nk = (k + 1) % 4
            mat[i, index[(nd, nk)]] -= prob
    try:
        sol = np.linalg.solve(mat, rhs)
    except np.linalg.LinAlgError:                             # pragma: no cover
        return {k: 0.5 for k in range(4)}
    return {k: float(max(0.0, min(1.0, sol[index[(0, k)]]))) for k in range(4)}


def tiebreak_prob(p_a: float, p_b: float, *, points_a: int = 0,
                  points_b: int = 0, target: int = DEFAULT_TIEBREAK_TO) -> float:
    """P(A vince il tie-break) dal punteggio corrente (fase estesa esatta)."""
    p_a = max(0.0, min(1.0, float(p_a)))
    p_b = max(0.0, min(1.0, float(p_b)))
    a, b, tgt = int(points_a), int(points_b), int(target)
    played = a + b
    extended: Optional[Dict[int, float]] = None
    memo: Dict[Tuple[int, int], float] = {}

    def _rec(x: int, y: int, idx: int) -> float:
        nonlocal extended
        if x >= tgt and x - y >= 2:
            return 1.0
        if y >= tgt and y - x >= 2:
            return 0.0
        if x >= tgt - 1 and y >= tgt - 1 and x == y:
            if extended is None:
                extended = _tiebreak_extended(p_a, p_b, total_points=idx,
                                              target=tgt)
            return extended[idx % 4]
        if (x, y) in memo:
            return memo[(x, y)]
        server = _tiebreak_server(idx)
        pw = p_a if server == "a" else 1.0 - p_b
        value = pw * _rec(x + 1, y, idx + 1) + (1.0 - pw) * _rec(x, y + 1, idx + 1)
        memo[(x, y)] = value
        return value

    if a + b > MAX_RECURSION_SCORE:                           # pragma: no cover
        return 0.5
    return max(0.0, min(1.0, _rec(a, b, played)))


def set_prob(p_a: float, p_b: float, *, games_a: int = 0, games_b: int = 0,
             server: str = "a", tiebreak: bool = True,
             tiebreak_to: int = DEFAULT_TIEBREAK_TO) -> float:
    """P(A vince il SET) dal punteggio di game corrente.

    `server` = chi serve il PROSSIMO game. Il set si vince a 6 game con 2 di
    margine, oppure 7-5, oppure 7-6 col tie-break (che serve il giocatore di
    turno). Il servizio alterna a ogni game.
    """
    p_a = max(0.0, min(1.0, float(p_a)))
    p_b = max(0.0, min(1.0, float(p_b)))
    ga, gb = int(games_a), int(games_b)
    memo: Dict[Tuple[int, int, str], float] = {}

    def _rec(x: int, y: int, srv: str) -> float:
        if x >= 6 and x - y >= 2:
            return 1.0
        if y >= 6 and y - x >= 2:
            return 0.0
        if x == 7 or y == 7:                     # 7-5 o 7-6
            return 1.0 if x > y else 0.0
        if x == 6 and y == 6:
            if not tiebreak:
                # Senza tie-break il set continua a oltranza: si chiude con la
                # probabilita' del game a oltranza (formula esatta del ciclo).
                g = game_prob(p_a) if srv == "a" else 1.0 - game_prob(p_b)
                return (g * g) / (g * g + (1.0 - g) ** 2) if 0 < g < 1 else g
            return tiebreak_prob(p_a, p_b, target=tiebreak_to)
        key = (x, y, srv)
        if key in memo:
            return memo[key]
        g = game_prob(p_a) if srv == "a" else 1.0 - game_prob(p_b)
        nxt = "b" if srv == "a" else "a"
        value = g * _rec(x + 1, y, nxt) + (1.0 - g) * _rec(x, y + 1, nxt)
        memo[key] = value
        return value

    return max(0.0, min(1.0, _rec(ga, gb, server)))


# ---------------------------------------------------------------------------
# 3) SET -> MATCH
# ---------------------------------------------------------------------------
def _set_first_server(first_server: str, set_index: int) -> str:
    """Chi serve per primo nel set `set_index` (0-based): alterna ogni set."""
    if set_index % 2 == 0:
        return first_server
    return "b" if first_server == "a" else "a"


def match_prob(p_a: float, p_b: float, *, best_of: int = DEFAULT_BEST_OF,
               sets_a: int = 0, sets_b: int = 0, games_a: int = 0,
               games_b: int = 0, server: str = "a", first_server: str = "a",
               tiebreak: bool = True) -> float:
    """P(A vince il MATCH) da qualunque punteggio di set/game.

    `best_of` = numero di set (3 o 5). Un set gia' in corso entra in gioco: il
    set corrente si valuta con `set_prob` (che sa del punteggio di game), i set
    successivi partono da 0-0 col servizio che alterna.
    """
    p_a = max(0.0, min(1.0, float(p_a)))
    p_b = max(0.0, min(1.0, float(p_b)))
    need = (int(best_of) + 1) // 2
    sa, sb = int(sets_a), int(sets_b)
    if sa >= need:
        return 1.0
    if sb >= need:
        return 0.0
    memo: Dict[Tuple[int, int], float] = {}

    def _rec(x: int, y: int, current: bool) -> float:
        if x >= need:
            return 1.0
        if y >= need:
            return 0.0
        if (x, y) in memo and not current:
            return memo[(x, y)]
        idx = x + y
        fs = _set_first_server(first_server, idx)
        if current:
            p_set = set_prob(p_a, p_b, games_a=games_a, games_b=games_b,
                             server=server, tiebreak=tiebreak)
        else:
            p_set = set_prob(p_a, p_b, server=fs, tiebreak=tiebreak)
        value = p_set * _rec(x + 1, y, False) + (1.0 - p_set) * _rec(x, y + 1, False)
        if not current:
            memo[(x, y)] = value
        return value

    return max(0.0, min(1.0, _rec(sa, sb, True)))


# ---------------------------------------------------------------------------
# 4) LIVE: dal PUNTO alla probabilita' di match
# ---------------------------------------------------------------------------
def parse_live(text: Any) -> Dict[str, Any]:
    """Stato live da stringa `"set-set,games-games,punti-punti"` (es. `1-0,3-2,30-15`).

    I punti si accettano in forma numerica (0-3, 3 = 40) o tennistica
    (`0/15/30/40/AD`). Una stringa non interpretabile solleva `ValueError`: qui
    l'errore non si indovina (uno stato live sbagliato produce una probabilita'
    sbagliata, che e' peggio di nessuna probabilita').
    """
    if isinstance(text, dict):
        return dict(text)
    raw = str(text or "").strip()
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) != 3:
        raise ValueError("stato live atteso 'set-set,game-game,punti-punti'")
    out: Dict[str, Any] = {}

    def _pair(token: str, label: str) -> Tuple[int, int]:
        bits = token.split("-")
        if len(bits) != 2:
            raise ValueError("coppia %s non valida: %r" % (label, token))
        try:
            return int(bits[0]), int(bits[1])
        except (TypeError, ValueError):
            raise ValueError("coppia %s non numerica: %r" % (label, token))

    out["sets_a"], out["sets_b"] = _pair(parts[0], "set")
    out["games_a"], out["games_b"] = _pair(parts[1], "game")
    points = {"0": 0, "15": 1, "30": 2, "40": 3, "ad": 4, "a": 4, "adv": 4}
    for token, side in zip(parts[2].split("-"), ("points_a", "points_b")):
        key = token.strip().lower()
        if key in points:
            out[side] = points[key]
        else:
            try:
                out[side] = int(key)
            except (TypeError, ValueError):
                raise ValueError("punti non validi: %r" % token)
    if out.get("points_a") == 4 and out.get("points_b") == 4:
        out["points_a"] = out["points_b"] = 3      # AD-AD = deuce
    return out


def live_match_prob(p_a: float, p_b: float, state: Optional[Dict[str, Any]] = None,
                    *, best_of: int = DEFAULT_BEST_OF, server: str = "a",
                    first_server: str = "a",
                    tiebreak: bool = True) -> Dict[str, Any]:
    """P(A vince il match) dallo STATO LIVE (punto, game, set).

    Il ponte punto -> set e' esatto: il set dipende SOLO da chi vince i game,
    quindi basta la probabilita' di vincere il game corrente dal punteggio di
    punti (che a sua volta dipende da chi serve).

    `prob` E' la probabilita' live (include il punteggio del game in corso):
    e' l'attesa sui due esiti del game corrente. Il valore "a game non
    iniziato" resta esposto a parte (`prob_before_game`), cosi' i due non si
    confondono — una `prob` che ignorasse i punti sarebbe la probabilita' di
    un altro istante (la classe di errore del `match_results` live del 17/09).
    """
    st = dict(state or {})
    points_a = int(st.get("points_a") or 0)
    points_b = int(st.get("points_b") or 0)
    games_a = int(st.get("games_a") or 0)
    games_b = int(st.get("games_b") or 0)
    sets_a = int(st.get("sets_a") or 0)
    sets_b = int(st.get("sets_b") or 0)
    if server == "a":
        p_game = game_prob_dp(p_a, points=points_a, points_against=points_b)
    else:
        p_game = 1.0 - game_prob_dp(p_b, points=points_b,
                                    points_against=points_a)
    prob_before = match_prob(p_a, p_b, best_of=best_of, sets_a=sets_a,
                             sets_b=sets_b, games_a=games_a, games_b=games_b,
                             server=server, first_server=first_server,
                             tiebreak=tiebreak)
    next_server = "b" if server == "a" else "a"
    if_won = match_prob(p_a, p_b, best_of=best_of, sets_a=sets_a,
                        sets_b=sets_b, games_a=games_a + 1, games_b=games_b,
                        server=next_server, first_server=first_server,
                        tiebreak=tiebreak)
    if_lost = match_prob(p_a, p_b, best_of=best_of, sets_a=sets_a,
                         sets_b=sets_b, games_a=games_a, games_b=games_b + 1,
                         server=next_server, first_server=first_server,
                         tiebreak=tiebreak)
    prob_live = p_game * if_won + (1.0 - p_game) * if_lost
    return {"prob": float(prob_live), "p_game": float(p_game),
            "prob_if_game_won": float(if_won),
            "prob_if_game_lost": float(if_lost),
            "prob_before_game": float(prob_before), "server": server,
            "state": {"sets_a": sets_a, "sets_b": sets_b, "games_a": games_a,
                      "games_b": games_b, "points_a": points_a,
                      "points_b": points_b}}


# ---------------------------------------------------------------------------
# 5) Serve/return: dal match all'input del motore (delega a tennis_quant)
# ---------------------------------------------------------------------------
def hold_from_point(p: float) -> float:
    """Tenuta del servizio implicata dalla probabilita' del punto (inversione)."""
    return game_prob(p)


def point_from_hold(hold: float, *, tol: float = 1e-9) -> float:
    """Probabilita' del PUNTO che produce una tenuta di servizio `hold`.

    Inversione numerica (bisezione) di `game_prob`, monotona crescente: cosi'
    quando il tasso di servizio arriva da una fonte che parla in termini di
    HOLD (il fallback di `tennis_quant`) il motore lo usa senza formule nuove.
    """
    h = max(0.0, min(1.0, float(hold)))
    lo, hi = 0.0, 1.0
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if game_prob(mid) < h:
            lo = mid
        else:
            hi = mid
        if hi - lo < tol:
            break
    return (lo + hi) / 2.0


def serve_probabilities(prob_a: float, *, surface: Any = None,
                        hold_a: Optional[float] = None,
                        hold_b: Optional[float] = None,
                        delta_raw: Optional[str] = None) -> Dict[str, Any]:
    """(p_a, p_b) = probabilita' del PUNTO al servizio, dai tassi di tenuta.

    Con `hold_a`/`hold_b` espliciti (stat provider reale) si usano quelli, senza
    delta di superficie: un dato misurato non si corregge con un priore. Senza
    provider, i tassi arrivano dal fallback DICHIARATO di `tennis_quant`
    (`hold_rates_from_prob`, ELO -> differenziale di servizio) piu' il delta di
    superficie, e `hold_source` lo dichiara.
    """
    delta, source = surface_delta(surface, raw=delta_raw)
    if hold_a is not None and hold_b is not None:
        ha, hb = float(hold_a), float(hold_b)
        source = "provided"
    else:
        try:
            import tennis_quant as tq
            ha, hb = tq.hold_rates_from_prob(float(prob_a))
        except Exception as e:                                # pragma: no cover
            logger.debug("tennis_markov: fallback hold/break non disponibile "
                         "(%s): uso 0.80/0.80", e)
            ha = hb = 0.80
        ha = max(0.50, min(0.95, ha + delta))
        hb = max(0.50, min(0.95, hb - delta))
    return {"p_a": point_from_hold(ha), "p_b": point_from_hold(hb),
            "hold_a": ha, "hold_b": hb, "surface": surface,
            "surface_delta": delta, "hold_source": source}


# ---------------------------------------------------------------------------
# 6) Valutazione (pre-match + live) e confronto col modello esistente
# ---------------------------------------------------------------------------
def evaluate(*, prob_a: float, surface: Any = None,
             price_a: Optional[float] = None, price_b: Optional[float] = None,
             live: Optional[Dict[str, Any]] = None, server: str = "a",
             best_of: Optional[int] = None,
             hold_a: Optional[float] = None,
             hold_b: Optional[float] = None) -> Dict[str, Any]:
    """Verdetto del motore: probabilita' pre-match, live e (se c'e' un prezzo) EV.

    L'EV delega a `tennis_quant.expected_value` (definizione unica del progetto)
    e viene calcolato SOLO se il prezzo e' presente e valido: un EV su un prezzo
    inventato sarebbe peggio di nessun EV.
    """
    conf = config()
    bo = int(best_of or conf["best_of"])
    serves = serve_probabilities(prob_a, surface=surface, hold_a=hold_a,
                                 hold_b=hold_b)
    p_pre = match_prob(serves["p_a"], serves["p_b"], best_of=bo,
                       tiebreak=conf["tiebreak"])
    out: Dict[str, Any] = {
        "ok": True, "engine": "markov", "best_of": bo,
        "prob_a": float(p_pre), "prob_b": float(1.0 - p_pre),
        "surface": surface, "surface_delta": serves["surface_delta"],
        "hold_source": serves["hold_source"], "hold_a": serves["hold_a"],
        "hold_b": serves["hold_b"], "p_point_a": serves["p_a"],
        "p_point_b": serves["p_b"], "prob_input": float(prob_a),
        "deviation_from_input": float(p_pre) - float(prob_a),
        "live": None, "ev": None,
    }
    if live is not None:
        out["live"] = live_match_prob(serves["p_a"], serves["p_b"], live,
                                      best_of=bo, server=server,
                                      tiebreak=conf["tiebreak"])
    if price_a is not None:
        try:
            import tennis_quant as tq
            out["ev"] = {"a": tq.expected_value(float(p_pre), float(price_a))}
            if price_b is not None:
                out["ev"]["b"] = tq.expected_value(float(1.0 - p_pre),
                                                   float(price_b))
        except Exception as e:                                # pragma: no cover
            logger.debug("tennis_markov: EV non calcolabile (%s)", e)
            out["ev"] = None
    return out


def compare_with_poisson(*, prob_a: float, surface: Any = None,
                         live: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Confronto DICHIARATO fra il motore Markov e il Poisson di `tennis_quant`.

    Serve a misurare quanto i due modelli divergono sulla stessa partita: sono
    due approssimazioni dello stesso gioco e una divergenza grande dice che uno
    dei due sta sbagliando (l'EV non si costruisce su un modello solo). Nessun
    giudizio automatico: i due numeri e la loro distanza, in punti percentuali.
    """
    out: Dict[str, Any] = {"ok": False, "reason": None, "markov": None,
                           "poisson": None, "diff_pp": None, "live": None}
    try:
        serves = serve_probabilities(prob_a, surface=surface)
        p_markov = match_prob(serves["p_a"], serves["p_b"])
        import tennis_quant as tq
        p_poisson = tq.poisson_prob_from_hold_break(serves["hold_a"],
                                                    serves["hold_b"])
        out.update({"ok": True, "markov": float(p_markov),
                    "poisson": float(p_poisson),
                    "diff_pp": 100.0 * (float(p_markov) - float(p_poisson)),
                    "hold_source": serves["hold_source"],
                    "surface": surface})
        if live is not None:
            out["live"] = live_match_prob(serves["p_a"], serves["p_b"], live)
    except Exception as e:
        logger.warning("tennis_markov: confronto non riuscito (%s)", e)
        out["reason"] = "confronto_non_disponibile"
    return out


# ---------------------------------------------------------------------------
# 7) Report e CLI
# ---------------------------------------------------------------------------
def format_report(block: Dict[str, Any]) -> str:
    """Report testuale (Telegram-friendly)."""
    lines = ["🎾 TENNIS MARKOV — punto -> game -> set -> match"]
    if not block.get("ok"):
        lines.append("  ⚠️ valutazione non disponibile: %s"
                     % (block.get("reason") or "errore"))
        return "\n".join(lines)
    lines.append("  superficie %s (delta servizio %+.3f, fonte %s) | best-of %s"
                 % (block.get("surface") or "-",
                    float(block.get("surface_delta") or 0.0),
                    block.get("hold_source"), block.get("best_of")))
    lines.append("  tenuta servizio A %.3f / B %.3f -> punto %.4f / %.4f"
                 % (float(block.get("hold_a") or 0.0),
                    float(block.get("hold_b") or 0.0),
                    float(block.get("p_point_a") or 0.0),
                    float(block.get("p_point_b") or 0.0)))
    lines.append("  P(A vince) %.4f (input %.4f, scarto %+.2fpp)"
                 % (float(block.get("prob_a") or 0.0),
                    float(block.get("prob_input") or 0.0),
                    float(block.get("deviation_from_input") or 0.0) * 100.0))
    if block.get("markov") is not None:
        lines.append("  confronto col Poisson: markov %.4f vs poisson %.4f "
                     "(%+.2fpp)"
                     % (float(block["markov"]), float(block["poisson"]),
                        float(block.get("diff_pp") or 0.0)))
    if block.get("live"):
        lv = block["live"]
        st = lv.get("state") or {}
        lines.append("  LIVE %d-%d set, %d-%d game, %s-%s punti: P(A) %.4f "
                     "(game in corso %.4f)"
                     % (int(st.get("sets_a") or 0), int(st.get("sets_b") or 0),
                        int(st.get("games_a") or 0), int(st.get("games_b") or 0),
                        int(st.get("points_a") or 0), int(st.get("points_b") or 0),
                        float(lv.get("prob") or 0.0),
                        float(lv.get("p_game") or 0.0)))
    if block.get("ev"):
        lines.append("  EV: %s" % json.dumps(block["ev"], default=str))
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in args

    def _opt(name: str) -> Optional[str]:
        for i, a in enumerate(args):
            if a == name and i + 1 < len(args):
                return args[i + 1]
        return None

    prob = _opt("--prob")
    if prob is None:
        print("uso: tennis_markov.py --prob 0.62 [--surface clay] "
              "[--price 1.85] [--live '1-0,3-2,30-15'] [--compare] [--json]")
        return 2
    try:
        prob_a = float(prob)
    except (TypeError, ValueError):
        print("--prob non numerico")
        return 2
    surface = _opt("--surface")
    price = _opt("--price")
    live_raw = _opt("--live")
    try:
        live = parse_live(live_raw) if live_raw else None
    except ValueError as e:
        print("stato live non valido: %s" % e)
        return 2
    price_f = float(price) if price else None
    if "--compare" in args:
        block = compare_with_poisson(prob_a=prob_a, surface=surface, live=live)
    else:
        block = evaluate(prob_a=prob_a, surface=surface, price_a=price_f,
                         live=live)
    print(json.dumps(block, indent=2, default=str) if as_json
          else format_report(block))
    return 0 if block.get("ok") else 1


if __name__ == "__main__":                                    # pragma: no cover
    raise SystemExit(main())
