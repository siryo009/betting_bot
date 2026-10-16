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

PONTE CALIBRATO (15/10/2026). Il ponte "probabilita' -> tenute" e' DERIVATO
(nessuno stat provider): non ha informazione indipendente, quindi l'unico uso
corretto e' di essere un **cambio di variabile** dentro la catena. Il ponte a
spread lineare non lo era — MISURATO: su un 0.75 di input la catena restituiva
**0.636** (-11.4pp) e su un 0.62 **0.620** solo per caso; l'artefatto finiva
dritto nell'EV intrinseco della corsia. `calibrate_hold_pair` risolve il
**differenziale** di tenuta a media fissata (la media viene dal fallback, non si
inventa un livello) e riproduce la probabilita' d'ingresso a ~1e-7: il residuo e'
esposto, e fuori dal box la soluzione e' dichiarata `bounded`. Una coppia
**misurata** (`hold_a`/`hold_b`) non si calibra mai. Il delta di superficie, che
la calibrazione riassorbe, si legge in `surface_prob_offset` (in punti di
probabilita') — l'informazione resta, non sparisce in silenzio.

VALIDAZIONE SUL LEDGER. Il referto salva i GAME delle partite di tennis: il
modello puo' quindi essere confrontato con cio' che e' accaduto davvero
(`ledger_games_stats`), e la BASE di tenuta servizio — oggi un priore scritto a
mano (0.80) — diventa una misura (`calibrate_base_hold`).

Confini (tripwire nei test): nessuna scrittura, zero rete, zero crediti, **zero
ordini**, nessuna soglia di strategia nel sorgente, `import tennis_markov` non
carica `auto_bet`/`bot`/`tracker`/`odds_api`.

CLI:
    venv/bin/python tennis_markov.py --prob 0.62 [--surface clay] [--live "1-0,3-2,30-15"]
    venv/bin/python tennis_markov.py --compare --prob 0.62 [--json]
    venv/bin/python tennis_markov.py --games            # game del referto vs catena
    venv/bin/python tennis_markov.py --calibrate-base   # BASE_HOLD misurata
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

#: Intervallo ammesso per una tenuta di servizio (stesso box dei tassi di
#: `tennis_quant`): fuori di qui il numero non e' un tasso di servizio.
HOLD_MIN, HOLD_MAX = 0.50, 0.95

#: PONTE CALIBRATO (15/10/2026). Il ponte "probabilita' -> tenute" e' DERIVATO
#: (fallback dichiarato, nessuno stat provider): nessuna coppia di tenute ha
#: informazione indipendente, quindi il suo unico compito corretto e' di
#: **riprodurre la probabilita' d'ingresso** dentro la catena esatta — un
#: cambio di variabile, non una seconda stima. Il ponte a spread lineare lasciava
#: un artefatto MISURATO (fino a -11pp sui favoriti), che finiva nell'EV
#: intrinseco della corsia: qui l'artefatto si toglie alla radice.
#: Una coppia MISURATA (`hold_a`/`hold_b` da uno stat provider) NON si calibra.
CALIBRATE_ENV = "TENNIS_MARKOV_CALIBRATE"
CALIBRATION_SCAN = 48          # passi della scansione grossolana sul differenziale
CALIBRATION_TOL = 1e-6         # tolleranza sul differenziale (residuo ~1e-7 in pp)
CALIBRATION_BASE_GRID = (0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90)
CALIBRATION_MAX_ROWS = 25      # campione massimo della curva (costo ~0.25s per punto)
CAL_CACHE_MAX = 1024
_CAL_CACHE: Dict[Tuple[Any, ...], Dict[str, Any]] = {}

DEFAULT_BASE_HOLD_FALLBACK = 0.80     # solo se `tennis_quant` non e' importabile


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


def calibrate_default() -> bool:
    """Il ponte calibrato e' il DEFAULT (l'artefatto a spread lineare era un bug)."""
    return _env_bool(CALIBRATE_ENV, True)


def _fallback_pair(prob_a: float, delta: float) -> Tuple[float, float, float, float]:
    """Coppia di tenute del fallback DICHIARATO + la coppia SENZA delta.

    Ritorna `(ha, hb, ha_neutral, hb_neutral)`: il delta di superficie e' un
    priore del progetto e va tenuto distinto (serve a `surface_prob_offset`).
    """
    try:
        import tennis_quant as tq
        base_a, base_b = tq.hold_rates_from_prob(float(prob_a))
    except Exception as e:                                    # pragma: no cover
        logger.debug("tennis_markov: fallback hold/break non disponibile "
                     "(%s): uso %.2f/%.2f", e, DEFAULT_BASE_HOLD_FALLBACK,
                     DEFAULT_BASE_HOLD_FALLBACK)
        base_a = base_b = DEFAULT_BASE_HOLD_FALLBACK
    na = max(HOLD_MIN, min(HOLD_MAX, float(base_a)))
    nb = max(HOLD_MIN, min(HOLD_MAX, float(base_b)))
    ha = max(HOLD_MIN, min(HOLD_MAX, na + float(delta)))
    hb = max(HOLD_MIN, min(HOLD_MAX, nb - float(delta)))
    return ha, hb, na, nb


def surface_prob_offset(prob_a: float, surface: Any = None, *,
                        delta_raw: Optional[str] = None,
                        best_of: Optional[int] = None) -> Dict[str, Any]:
    """Effetto IN PROBABILITA' del priore di superficie, alla catena esatta.

    Il delta di superficie nasce come differenziale di TENUTA: qui se ne misura
    la traduzione in punti di probabilita' del match, cosi' l'informazione resta
    disponibile e dichiarata anche quando il ponte e' calibrato (dove il delta
    non puo' piu' trasparire dal numero finale, perche' la calibrazione lo
    riassorbe riproducendo la probabilita' d'ingresso).
    """
    conf = config()
    bo = int(best_of or conf["best_of"])
    delta, source = surface_delta(surface, raw=delta_raw)
    ha, hb, na, nb = _fallback_pair(prob_a, delta)
    p_delta = match_prob(point_from_hold(ha), point_from_hold(hb), best_of=bo,
                         tiebreak=conf["tiebreak"])
    p_neutral = match_prob(point_from_hold(na), point_from_hold(nb),
                           best_of=bo, tiebreak=conf["tiebreak"])
    return {"surface_delta": delta, "delta_source": source,
            "prob_delta": float(p_delta), "prob_neutral": float(p_neutral),
            "offset_pp": 100.0 * (float(p_delta) - float(p_neutral))}


def serve_probabilities(prob_a: float, *, surface: Any = None,
                        hold_a: Optional[float] = None,
                        hold_b: Optional[float] = None,
                        delta_raw: Optional[str] = None,
                        calibrate: Optional[bool] = None) -> Dict[str, Any]:
    """(p_a, p_b) = probabilita' del PUNTO al servizio, dai tassi di tenuta.

    Con `hold_a`/`hold_b` espliciti (stat provider reale) si usano quelli, senza
    delta di superficie ne' calibrazione: un dato **misurato** non si corregge
    (ne' con un priore ne' con il mercato). Senza provider, i tassi arrivano dal
    fallback DICHIARATO di `tennis_quant` (`hold_rates_from_prob`) piu' il delta
    di superficie, e `hold_source` lo dichiara.

    `calibrate` (default `TENNIS_MARKOV_CALIBRATE`, ON): sul percorso DERIVATO la
    coppia viene risolta in modo che la catena esatta riproduca `prob_a` — vedi
    `calibrate_hold_pair` per il motivo (il ponte a spread lineare comprimeva i
    favoriti fino a -11pp). Passare `calibrate=False` ripristina il ponte grezzo
    (diagnostica e test del confronto).
    """
    delta, source = surface_delta(surface, raw=delta_raw)
    info: Dict[str, Any] = {"calibrated": False, "calibration": None,
                            "raw_holds": None, "raw_prob": None,
                            "prob_input": float(prob_a) if isinstance(
                                prob_a, (int, float)) else None}
    if hold_a is not None and hold_b is not None:
        ha, hb = float(hold_a), float(hold_b)
        source = "provided"
    else:
        ha, hb, _na, _nb = _fallback_pair(prob_a, delta)
        info["raw_holds"] = [ha, hb]
        # ⚠️ Con la calibrazione attiva il delta di superficie NON puo' piu'
        # trasparire dal numero finale (la coppia viene risolta per riprodurre
        # `prob_a`): l'effetto si LEGGE qui, in punti di probabilita', invece di
        # restare implicito. Senza questo campo, passare `surface="clay"`
        # sembrerebbe non fare nulla.
        try:
            info["surface_offset_pp"] = surface_prob_offset(
                prob_a, surface, delta_raw=delta_raw)["offset_pp"]
        except Exception:                                     # pragma: no cover
            info["surface_offset_pp"] = None
        # La probabilita' del PONTE GREZZO (prima del 15/10 quella che finiva in
        # produzione): serve al report per misurare l'artefatto rimosso.
        try:
            _conf = config()
            info["raw_prob"] = float(match_prob(
                point_from_hold(ha), point_from_hold(hb),
                best_of=_conf["best_of"], tiebreak=_conf["tiebreak"]))
        except Exception:                                     # pragma: no cover
            info["raw_prob"] = None
        do = calibrate_default() if calibrate is None else bool(calibrate)
        if do:
            cal = calibrate_hold_pair(prob_a, mean=(ha + hb) / 2.0)
            if cal.get("ok"):
                ha, hb = cal["hold_a"], cal["hold_b"]
                info["calibrated"] = True
                info["calibration"] = cal
            else:
                info["calibration"] = cal      # fallito: coppia grezza + motivo
    return {"p_a": point_from_hold(ha), "p_b": point_from_hold(hb),
            "hold_a": ha, "hold_b": hb, "surface": surface,
            "surface_delta": delta, "hold_source": source, **info}


# ---------------------------------------------------------------------------
# 5b) PONTE CALIBRATO + GRANDEZZE A LIVELLO DI GAME
# ---------------------------------------------------------------------------
def _chain_at_dnet(mean: float, dnet: float, *, best_of: int,
                   tiebreak: bool) -> Tuple[float, float, float]:
    """Catena esatta per una coppia simmetrica attorno a `mean`."""
    ha = max(HOLD_MIN, min(HOLD_MAX, mean + dnet / 2.0))
    hb = max(HOLD_MIN, min(HOLD_MAX, mean - dnet / 2.0))
    prob = match_prob(point_from_hold(ha), point_from_hold(hb),
                      best_of=best_of, tiebreak=tiebreak)
    return float(prob), ha, hb


def calibrate_hold_pair(prob_a: float, *, mean: Optional[float] = None,
                        best_of: Optional[int] = None,
                        tiebreak: Optional[bool] = None,
                        cache: bool = True) -> Dict[str, Any]:
    """Coppia di tenute la cui catena esatta RIPRODUCE `prob_a`.

    Il ponte `probabilita' -> tenute` e' derivato (nessuno stat provider): non ha
    informazione indipendente, quindi il suo compito corretto e' di essere un
    **cambio di variabile** dentro la catena. Il ponte a spread lineare non lo
    era: con `hold_rates_from_prob(0.75)` la catena restituiva **0.636**
    (-11.4pp) perche' il differenziale di servizio era troppo piccolo, e quel
    buco finiva direttamente nell'EV intrinseco della corsia.

    Si risolve il **differenziale** `dn = hold_a - hold_b` a media `m` fissata
    (la media viene dal fallback dichiarato, cosi' non si inventa un livello):
    `dn = 0` da' esattamente 0.5 e la catena e' monotona in `dn`, quindi
    scansione grossolana + bisezione. Fuori dai limiti del box la soluzione e'
    dichiarata `bounded` con il residuo esposto — mai un numero spacciato per
    esatto.
    """
    conf = config()
    bo = int(best_of or conf["best_of"])
    tb = conf["tiebreak"] if tiebreak is None else bool(tiebreak)
    try:
        target = float(prob_a)
    except (TypeError, ValueError):
        return {"ok": False, "reason": "prob_non_numerica", "hold_a": None,
                "hold_b": None, "raw_prob": None}
    if not (0.0 < target < 1.0):
        return {"ok": False, "reason": "prob_fuori_range", "hold_a": None,
                "hold_b": None, "raw_prob": None}

    if mean is None:
        delta, _src = surface_delta(None)
        ha, hb, _na, _nb = _fallback_pair(target, delta)
        m = (ha + hb) / 2.0
    else:
        m = max(HOLD_MIN, min(HOLD_MAX, float(mean)))

    key = (round(target, 7), round(m, 4), bo, bool(tb))
    if cache and key in _CAL_CACHE:
        return dict(_CAL_CACHE[key])

    dn_lo = 2.0 * (HOLD_MIN - m)
    dn_hi = 2.0 * (HOLD_MAX - m)
    # 1) scansione grossolana: trova il primo intervallo che attraversa il target
    prev_dn, prev_p = dn_lo, None
    bracket: Optional[Tuple[float, float]] = None
    for step in range(CALIBRATION_SCAN + 1):
        dn = dn_lo + (dn_hi - dn_lo) * step / CALIBRATION_SCAN
        p, _ha, _hb = _chain_at_dnet(m, dn, best_of=bo, tiebreak=tb)
        if prev_p is not None and p >= target and prev_p < target:
            bracket = (prev_dn, dn)
            break
        prev_dn, prev_p = dn, p
    bounded = bracket is None and prev_p is not None
    if bracket is None:
        # nessun attraversamento: la soluzione sta a un estremo del box
        prob_lo, ha_lo, hb_lo = _chain_at_dnet(m, dn_lo, best_of=bo, tiebreak=tb)
        prob_hi, ha_hi, hb_hi = _chain_at_dnet(m, dn_hi, best_of=bo, tiebreak=tb)
        if abs(prob_hi - target) <= abs(prob_lo - target):
            dn_best, p_best, ha_b, hb_b = dn_hi, prob_hi, ha_hi, hb_hi
        else:
            dn_best, p_best, ha_b, hb_b = dn_lo, prob_lo, ha_lo, hb_lo
    else:
        # 2) bisezione dentro l'intervallo
        lo, hi = bracket
        for _ in range(200):
            mid = (lo + hi) / 2.0
            pm, _ha, _hb = _chain_at_dnet(m, mid, best_of=bo, tiebreak=tb)
            if pm < target:
                lo = mid
            else:
                hi = mid
            if hi - lo < CALIBRATION_TOL:
                break
        dn_best = (lo + hi) / 2.0
        p_best, ha_b, hb_b = _chain_at_dnet(m, dn_best, best_of=bo, tiebreak=tb)

    out = {"ok": True, "reason": None, "hold_a": ha_b, "hold_b": hb_b,
           "prob": p_best, "prob_input": target, "mean": m, "dnet": dn_best,
           "residual": p_best - target, "bounded": bool(bounded),
           "method": "scan+bisezione", "raw_prob": None,
           "best_of": bo, "tiebreak": tb}
    if cache:
        if len(_CAL_CACHE) >= CAL_CACHE_MAX:
            _CAL_CACHE.clear()
        _CAL_CACHE[key] = dict(out)
    return out


def exact_serve_prob(hold_a: float, hold_b: float, *, best_of: Optional[int] = None,
                     tiebreak: Optional[bool] = None) -> Dict[str, Any]:
    """P(A vince) dalla coppia di tenute, con la CATENA ESATTA (nessun priore).

    E' l'alternativa esatta al prodotto di Poisson sui break di
    `tennis_quant.poisson_prob_from_hold_break` (approssimazione dichiarata e
    misurata bassa di 1.5-3.5pp sulle stesse tenute). Stessa informazione in
    ingresso, stessa uscita del motore storico, matematica esatta: nessuna
    soglia e nessun peso cambiano, cambia il numero che il modello produce.
    """
    conf = config()
    bo = int(best_of or conf["best_of"])
    tb = conf["tiebreak"] if tiebreak is None else bool(tiebreak)
    try:
        ha = max(HOLD_MIN, min(HOLD_MAX, float(hold_a)))
        hb = max(HOLD_MIN, min(HOLD_MAX, float(hold_b)))
        prob = match_prob(point_from_hold(ha), point_from_hold(hb), best_of=bo,
                          tiebreak=tb)
    except Exception as exc:                                # pragma: no cover
        logger.warning("tennis_markov: catena esatta non calcolabile (%s)", exc)
        return {"ok": False, "reason": "catena_non_disponibile", "prob": None}
    return {"ok": True, "reason": None, "prob": float(prob), "hold_a": ha,
            "hold_b": hb, "best_of": bo, "engine": "markov_exact"}


def set_outcome_distribution(p_a: float, p_b: float, *, server: str = "a",
                             tiebreak: bool = True,
                             tiebreak_to: int = DEFAULT_TIEBREAK_TO
                             ) -> Dict[Tuple[str, int], float]:
    """Distribuzione congiunta (vincitore del set, game TOTALI) di UN set.

    E' la quantita' che manca alla catena per confrontarsi con il ledger: il
    referto salva i GAME, non solo il vincitore. Il set si gioca a 6 game con 2
    di margine, a 7-5, oppure a 7-6 col tie-break (13 game totali: il conteggio
    dei game di un tie-break e' per definizione 13, non 12+1).

    Con `tiebreak=False` il set puo' durare a oltranza: la distribuzione NON e'
    calcolata (solleva `ValueError`, dichiarato) — la produzione usa il
    tie-break, e un numero approssimato qui falserebbe la calibrazione.
    """
    if not tiebreak:
        raise ValueError("distribuzione dei game senza tie-break non supportata")
    pa = max(0.0, min(1.0, float(p_a)))
    pb = max(0.0, min(1.0, float(p_b)))
    memo: Dict[Tuple[int, int, str], Dict[Tuple[str, int], float]] = {}

    def _rec(ga: int, gb: int, srv: str) -> Dict[Tuple[str, int], float]:
        if ga >= 6 and ga - gb >= 2:
            return {("a", ga + gb): 1.0}
        if gb >= 6 and gb - ga >= 2:
            return {("b", ga + gb): 1.0}
        if ga == 6 and gb == 6:
            # Tie-break: chi lo vince vince il set 7-6 = 13 game totali.
            ptb = tiebreak_prob(pa, pb, target=tiebreak_to)
            winner = "a" if srv == "a" else "b"
            loser = "b" if winner == "a" else "a"
            return {(winner, 13): ptb, (loser, 13): 1.0 - ptb}
        key = (ga, gb, srv)
        if key in memo:
            return memo[key]
        g = game_prob(pa) if srv == "a" else 1.0 - game_prob(pb)
        nxt = "b" if srv == "a" else "a"
        won = _rec(ga + 1, gb, nxt)
        lost = _rec(ga, gb + 1, nxt)
        out: Dict[Tuple[str, int], float] = {}
        for (w, gm), v in won.items():
            out[(w, gm)] = out.get((w, gm), 0.0) + g * v
        for (w, gm), v in lost.items():
            out[(w, gm)] = out.get((w, gm), 0.0) + (1.0 - g) * v
        memo[key] = out
        return out

    return _rec(int(0), int(0), server)


def expected_total_games(hold_a: float, hold_b: float, *, best_of: Optional[int] = None,
                         tiebreak: Optional[bool] = None,
                         first_server: str = "a",
                         tiebreak_to: Optional[int] = None) -> Dict[str, Any]:
    """E[game totali] del match dalla coppia di tenute (catena esatta).

    Serve alla VALIDAZIONE sul ledger: il referto delle partite di tennis salva i
    game, quindi il modello puo' essere confrontato con cio' che e' successo
    davvero (`ledger_games_stats`), cosa che il solo vincitore non permette.
    La normalizzazione della distribuzione e' esposta (`total_prob`): se non
    somma a 1, il numero non e' affidabile e va visto.
    """
    conf = config()
    bo = int(best_of or conf["best_of"])
    tb = conf["tiebreak"] if tiebreak is None else bool(tiebreak)
    tbt = int(tiebreak_to or conf["tiebreak_to"])
    try:
        ha = max(HOLD_MIN, min(HOLD_MAX, float(hold_a)))
        hb = max(HOLD_MIN, min(HOLD_MAX, float(hold_b)))
        p_a = point_from_hold(ha)
        p_b = point_from_hold(hb)
        need = (bo + 1) // 2
        dist: Dict[Tuple[int, int], Dict[int, float]] = {(0, 0): {0: 1.0}}
        terminal: Dict[int, float] = {}
        for total_sets in range(0, 2 * need - 1):
            for sa in range(0, min(need, total_sets) + 1):
                sb = total_sets - sa
                if sb > need:
                    continue
                cur = dist.get((sa, sb))
                if not cur:
                    continue
                srv = _set_first_server(first_server, sa + sb)
                sd = set_outcome_distribution(p_a, p_b, server=srv,
                                              tiebreak=tb, tiebreak_to=tbt)
                for (winner, games), ps in sd.items():
                    nsa, nsb = (sa + 1, sb) if winner == "a" else (sa, sb + 1)
                    bucket = terminal if (nsa >= need or nsb >= need) else dist.setdefault(
                        (nsa, nsb), {})
                    for g, pc in cur.items():
                        bucket[g + games] = bucket.get(g + games, 0.0) + pc * ps
        total_prob = sum(terminal.values())
        expected = sum(g * p for g, p in terminal.items())
    except Exception as exc:
        logger.warning("tennis_markov: E[game] non calcolabile (%s)", exc)
        return {"ok": False, "reason": "distribuzione_non_disponibile",
                "expected_total_games": None}
    return {"ok": True, "reason": None,
            "expected_total_games": float(expected),
            "hold_a": ha, "hold_b": hb, "best_of": bo, "tiebreak": tb,
            "prob_a": float(match_prob(p_a, p_b, best_of=bo, tiebreak=tb)),
            "total_prob": float(total_prob),
            "method": "distribuzione esatta dei game (set + tie-break)"}


# ---------------------------------------------------------------------------
# 5c) VALIDAZIONE SUL LEDGER: i GAME del referto contro la catena
# ---------------------------------------------------------------------------
#: Coppie di colonne candidate per i punteggi in `match_results`: lo schema e'
#: cambiato piu' volte e i nomi sono posizionali in `save_result` — si legge
#: quali esistono davvero, senza indovinare (una colonna sbagliata darebbe
#: "0 righe" indistinguibile da "nessun dato", la classe di errore del 20/10).
_RESULT_SCORE_CANDIDATES = (("score_home", "score_away"),
                            ("home_score", "away_score"),
                            ("sh", "sa"), ("gol_home", "gol_away"))
#: Coppie candidate per i NOMI delle squadre: lo schema di produzione usa
#: `home_team`/`away_team` mentre altri percorsi del progetto usano `home`/
#: `away`. Una query nominale su una colonna assente fa fallire TUTTA la
#: lettura e il motivo dichiarato diventava un generico `lettura_fallita`
#: (misurato sul container il 16/10/2026: la validazione sui GAME non partiva
#: pur avendo 200+ risultati di tennis con gli score).
_RESULT_TEAM_CANDIDATES = (("home_team", "away_team"), ("home", "away"),
                           ("home_name", "away_name"), ("team_home", "team_away"))
GAMES_MIN_PLAUSIBLE, GAMES_MAX_PLAUSIBLE = 8, 60


def _result_columns(conn) -> Optional[Tuple[str, str]]:
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(match_results)")}
    except Exception:
        return None
    for pair in _RESULT_SCORE_CANDIDATES:
        if pair[0] in cols and pair[1] in cols:
            return pair
    return None


#: Alias storico (i test e la prima versione usavano questo nome).
def _score_columns(conn) -> Optional[Tuple[str, str]]:      # pragma: no cover
    return _result_columns(conn)


def _team_columns(conn) -> Optional[Tuple[str, str]]:
    """Colonne dei NOMI squadra realmente presenti in `match_results`."""
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(match_results)")}
    except Exception:
        return None
    for pair in _RESULT_TEAM_CANDIDATES:
        if pair[0] in cols and pair[1] in cols:
            return pair
    return None


def ledger_games_stats(rows: Optional[Sequence[Dict[str, Any]]] = None, *,
                       db: Optional[str] = None, limit: int = 400) -> Dict[str, Any]:
    """I GAME dei match di tennis nel referto: semantica + campione.

    Con `rows` iniettate la funzione e' OFFLINE (i test). Senza, legge il ledger
    in SOLA LETTURA. Il referto non salva un esito strutturato per il tennis,
    quindi la semantica del punteggio va DICHIARATA, non assunta: se i totali
    non stanno nell'intervallo plausibile di un match (8-60 game) la funzione
    risponde `semantics="unknown"` e la calibrazione non parte.
    """
    out: Dict[str, Any] = {"ok": False, "reason": None, "semantics": None,
                           "sample": 0, "rows": [], "observed_mean": None,
                           "observed_median": None, "source": None}
    data = list(rows or [])
    if not data:
        try:
            import sqlite3
            from pathlib import Path
            path = db
            if path is None:
                import tracker as _t
                path = str(getattr(_t, "DB_PATH", ""))
            if not path or not Path(path).exists():
                out["reason"] = "ledger_assente"
                return out
            conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
            try:
                cols = _result_columns(conn)
                if cols is None:
                    out["reason"] = "colonne_punteggio_assenti"
                    return out
                teams = _team_columns(conn)
                if teams is None:
                    # Senza i nomi non si costruisce la riga: dichiararlo e'
                    # meglio che restituire un campione senza intestazione.
                    out["reason"] = "colonne_squadre_assenti"
                    return out
                out["columns"] = {"teams": list(teams), "scores": list(cols)}
                # Il filtro per LEGA e' obbligatorio: i punteggi del calcio
                # (0-7) inquinerebbero la semantica "game" in modo invisibile.
                tennis_where = ("(m.league LIKE '%%ATP%%' OR m.league LIKE '%%WTA%%' "
                                "OR m.league LIKE '%%Challenger%%' "
                                "OR m.league LIKE '%%ITF%%' "
                                "OR m.league LIKE '%%Tennis%%')")
                rows: List[tuple] = []
                try:
                    q = ("SELECT m.%s, m.%s, m.%s, m.%s, MAX(p.prob) "
                         "FROM match_results m LEFT JOIN predictions p "
                         "ON p.match_id = m.match_id AND p.prob IS NOT NULL "
                         "WHERE %s AND m.%s IS NOT NULL AND m.%s IS NOT NULL "
                         "GROUP BY m.match_id ORDER BY m.rowid DESC LIMIT ?"
                         % (teams[0], teams[1], cols[0], cols[1], tennis_where,
                            cols[0], cols[1]))
                    rows = list(conn.execute(q, (int(limit),)))
                except Exception as exc:                      # pragma: no cover
                    logger.debug("tennis_markov: join predictions non "
                                 "disponibile (%s): leggo i soli punteggi", exc)
                    # Le parentesi NON sono decorative: senza, l'`AND` lega
                    # piu' stretto dell'`OR` e il filtro di lega salta (una
                    # query che sembra giusta e restituisce il campione
                    # sbagliato).
                    plain_where = ("(league LIKE '%%ATP%%' "
                                   "OR league LIKE '%%WTA%%' "
                                   "OR league LIKE '%%Challenger%%' "
                                   "OR league LIKE '%%ITF%%' "
                                   "OR league LIKE '%%Tennis%%')")
                    q = ("SELECT %s, %s, %s, %s FROM match_results "
                         "WHERE %s AND %s IS NOT NULL AND %s IS NOT NULL "
                         "ORDER BY rowid DESC LIMIT ?"
                         % (teams[0], teams[1], cols[0], cols[1], plain_where,
                            cols[0], cols[1]))
                    rows = [(h, a, sh, sa, None)
                            for h, a, sh, sa in conn.execute(q, (int(limit),))]
                for home, away, sh, sa, prob in rows:
                    data.append({"home": home, "away": away,
                                 "sh": sh, "sa": sa, "league": "",
                                 "prob": prob})
            finally:
                conn.close()
            out["source"] = "ledger"
        except Exception as exc:
            logger.debug("tennis_markov: lettura game dal ledger fallita (%s)", exc)
            out["reason"] = "lettura_fallita"
            return out
    else:
        out["source"] = "iniettate"
    out["prob_source"] = ("ledger" if any(r.get("prob") is not None for r in data)
                          else "neutro_0.5")

    totals: List[int] = []
    for r in data:
        try:
            sh, sa = int(r.get("sh")), int(r.get("sa"))
        except (TypeError, ValueError):
            continue
        if sh < 0 or sa < 0:
            continue
        totals.append(sh + sa)
    out["sample"] = len(totals)
    if not totals:
        out["reason"] = "nessun_punteggio_utilizzabile"
        return out
    ordered = sorted(totals)
    mid = len(ordered) // 2
    median = float(ordered[mid]) if len(ordered) % 2 else \
        (ordered[mid - 1] + ordered[mid]) / 2.0
    plausible = sum(1 for t in totals if GAMES_MIN_PLAUSIBLE <= t <= GAMES_MAX_PLAUSIBLE)
    out.update({"observed_mean": float(sum(totals)) / len(totals),
                "observed_median": median,
                "plausible_share": plausible / len(totals),
                "rows": totals,
                "ok": True})
    if out["plausible_share"] < 0.9:
        out["semantics"] = "unknown"
        out["reason"] = "punteggi_non_compatibili_con_i_game"
        out["ok"] = False
    else:
        out["semantics"] = "games"
    return out


def _pair_for_base(prob: float, base: float,
                   spread: Optional[float] = None) -> Tuple[float, float]:
    """Coppia del fallback a una BASE data — delega a `tennis_quant`."""
    try:
        import tennis_quant as tq
        ha, hb = tq.hold_rates_from_prob(float(prob), base=float(base),
                                         spread=spread)
    except Exception as exc:                                  # pragma: no cover
        logger.debug("tennis_markov: base hold non calcolabile (%s)", exc)
        ha = hb = float(base)
    return (max(HOLD_MIN, min(HOLD_MAX, float(ha))),
            max(HOLD_MIN, min(HOLD_MAX, float(hb))))


def predicted_mean_games(probs: Sequence[float], *, base: float,
                         spread: Optional[float] = None,
                         best_of: Optional[int] = None,
                         calibrate: bool = True) -> Dict[str, Any]:
    """E[game totali] MEDIO previsto dalla catena su un insieme di probabilita'."""
    probs = [p for p in probs if isinstance(p, (int, float)) and 0.0 < float(p) < 1.0]
    if not probs:
        return {"ok": False, "reason": "nessuna_probabilita",
                "mean_games": None, "sample": 0}
    conf = config()
    bo = int(best_of or conf["best_of"])
    total, used = 0.0, 0
    for p in probs:
        ha, hb = _pair_for_base(p, base, spread)
        if calibrate:
            cal = calibrate_hold_pair(float(p), mean=(ha + hb) / 2.0,
                                      best_of=bo)
            if not cal.get("ok"):
                continue
            ha, hb = cal["hold_a"], cal["hold_b"]
        eg = expected_total_games(ha, hb, best_of=bo)
        if not eg.get("ok"):
            continue
        total += float(eg["expected_total_games"])
        used += 1
    if not used:
        return {"ok": False, "reason": "previsione_non_disponibile",
                "mean_games": None, "sample": 0}
    return {"ok": True, "reason": None, "mean_games": total / used,
            "sample": used, "base": float(base),
            "best_of": bo, "calibrated": bool(calibrate)}


def calibrate_base_hold(*, rows: Optional[Sequence[Dict[str, Any]]] = None,
                        db: Optional[str] = None,
                        grid: Optional[Sequence[float]] = None,
                        spread: Optional[float] = None,
                        probs: Optional[Sequence[float]] = None,
                        best_of: Optional[int] = None) -> Dict[str, Any]:
    """Suggerisce la BASE di tenuta servizio che riproduce i GAME del referto.

    `tennis_quant.BASE_HOLD` (0.80) e' un PRIORE del progetto, mai misurato. Qui
    diventa una MISURA: sui match di tennis del referto si confronta la media dei
    game osservati con quella prevista dalla catena a diverse basi e si prende la
    base che le uguaglia. E' un numero da applicare via env
    (`TENNIS_QUANT_BASE_HOLD`), non un automatismo: cambio di probabilita' =
    decisione del proprietario.

    Dichiarato sempre: campione, semantica dei punteggi, monotonia della curva.
    """
    stats = ledger_games_stats(rows, db=db)
    out: Dict[str, Any] = {"ok": False, "reason": None, "semantics": stats.get("semantics"),
                           "sample": stats.get("sample", 0),
                           "observed_mean": stats.get("observed_mean"),
                           "base_current": None, "base_suggested": None,
                           "predicted_mean_at_current": None,
                           "curve": [], "monotone": None}
    if not stats.get("ok") or stats.get("semantics") != "games":
        out["reason"] = stats.get("reason") or "campione_non_utilizzabile"
        return out
    # Probabilita' d'ingresso: dal ledger se disponibili (`predictions.prob`),
    # altrimenti 0.5 e la cosa e' DICHIARATA (una partita pari merita altri game
    # di una sbilanciata: usare 0.5 su tutte distorce il livello, non la forma).
    prob_rows = [r.get("prob") for r in (rows or []) if r.get("prob") is not None]
    used_probs = probs if probs is not None else prob_rows
    if used_probs:
        probs_used = [float(p) for p in used_probs]
        out["probs_source"] = "ledger"
    else:
        probs_used = [0.5] * int(stats["sample"])
        out["probs_source"] = "neutro_0.5"
    try:
        import tennis_quant as tq
        current = float(tq.BASE_HOLD)
    except Exception:                                         # pragma: no cover
        import os as _os
        current = float(_os.getenv("TENNIS_QUANT_BASE_HOLD", "0.80"))
    out["base_current"] = current
    step_grid = list(grid) if grid else list(CALIBRATION_BASE_GRID)
    if len(probs_used) > CALIBRATION_MAX_ROWS:
        probs_used = probs_used[:CALIBRATION_MAX_ROWS]
        out["probs_truncated"] = True
    curve: List[Dict[str, float]] = []
    for base in step_grid:
        pred = predicted_mean_games(probs_used, base=float(base), spread=spread,
                                    best_of=best_of)
        if pred.get("ok"):
            curve.append({"base": float(base), "mean_games": float(pred["mean_games"])})
    out["curve"] = curve
    if len(curve) < 3:
        out["reason"] = "curva_non_calcolabile"
        return out
    values = [c["mean_games"] for c in curve]
    out["monotone"] = all(b >= a for a, b in zip(values, values[1:])) or \
        all(b <= a for a, b in zip(values, values[1:]))
    for c in curve:
        if abs(c["base"] - current) < 1e-9:
            out["predicted_mean_at_current"] = c["mean_games"]
    if out["predicted_mean_at_current"] is None:
        best = min(curve, key=lambda c: abs(c["base"] - current))
        out["predicted_mean_at_current"] = best["mean_games"]
    target = float(stats["observed_mean"])
    chosen: Optional[float] = None
    for a, b in zip(curve, curve[1:]):
        lo, hi = sorted((a["mean_games"], b["mean_games"]))
        if lo <= target <= hi and hi > lo:
            frac = (target - a["mean_games"]) / (b["mean_games"] - a["mean_games"])
            chosen = a["base"] + frac * (b["base"] - a["base"])
            break
    if chosen is None:
        nearest = min(curve, key=lambda c: abs(c["mean_games"] - target))
        chosen = nearest["base"]
        out["reason"] = "target_fuori_curva"
    out.update({"ok": True, "base_suggested": float(chosen),
                "gap_games_at_current": out["predicted_mean_at_current"] - target,
                "grid": [float(g) for g in step_grid],
                "method": "catena esatta su E[game] vs media osservata "
                          "(curva a %d punti, interpolazione lineare)"
                          % len(step_grid)})
    return out


# ---------------------------------------------------------------------------
# 6) Valutazione (pre-match + live) e confronto col modello esistente
# ---------------------------------------------------------------------------
def evaluate(*, prob_a: float, surface: Any = None,
             price_a: Optional[float] = None, price_b: Optional[float] = None,
             live: Optional[Dict[str, Any]] = None, server: str = "a",
             best_of: Optional[int] = None,
             hold_a: Optional[float] = None,
             hold_b: Optional[float] = None,
             calibrate: Optional[bool] = None) -> Dict[str, Any]:
    """Verdetto del motore: probabilita' pre-match, live e (se c'e' un prezzo) EV.

    L'EV delega a `tennis_quant.expected_value` (definizione unica del progetto)
    e viene calcolato SOLO se il prezzo e' presente e valido: un EV su un prezzo
    inventato sarebbe peggio di nessun EV.
    """
    conf = config()
    bo = int(best_of or conf["best_of"])
    serves = serve_probabilities(prob_a, surface=surface, hold_a=hold_a,
                                 hold_b=hold_b, calibrate=calibrate)
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
        "calibrated": serves.get("calibrated"),
        "raw_holds": serves.get("raw_holds"), "raw_prob": serves.get("raw_prob"),
        "raw_deviation_from_input": (
            (float(serves["raw_prob"]) - float(prob_a))
            if serves.get("raw_prob") is not None else None),
        "calibration": serves.get("calibration"),
        "expected_total_games": None,
        "live": None, "ev": None,
    }
    try:
        eg = expected_total_games(serves["hold_a"], serves["hold_b"], best_of=bo)
        out["expected_total_games"] = (round(float(eg["expected_total_games"]), 2)
                                       if eg.get("ok") else None)
    except Exception as e:                                    # pragma: no cover
        logger.debug("tennis_markov: E[game] non calcolabile (%s)", e)
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
                         live: Optional[Dict[str, Any]] = None,
                         calibrate: Optional[bool] = None) -> Dict[str, Any]:
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
                    "surface": surface,
                    "calibrated": serves.get("calibrated"),
                    "raw_prob": serves.get("raw_prob"),
                    "raw_deviation_pp": (
                        100.0 * (float(serves["raw_prob"]) - float(prob_a))
                        if serves.get("raw_prob") is not None else None)})
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
    if block.get("raw_prob") is not None:
        lines.append("  ponte %s: coppia grezza %s -> %.4f (%+.2fpp), "
                     "calibrata %.4f %s"
                     % ("CALIBRATO" if block.get("calibrated") else "GREZZO",
                        block.get("raw_holds"), float(block["raw_prob"]),
                        float(block.get("raw_deviation_from_input") or 0.0) * 100.0,
                        float(block.get("prob_a") or 0.0),
                        ("(residuo %+.2fpp)" % (100.0 * float(
                            (block.get("calibration") or {}).get("residual") or 0.0))
                         if block.get("calibration") else "")))
    if block.get("expected_total_games") is not None:
        lines.append("  E[game totali] %.2f" % float(block["expected_total_games"]))
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


def format_games_report(block: Dict[str, Any]) -> str:
    """Report dei game del referto (e della base misurata, se presente)."""
    if not block.get("ok"):
        return "🎾 GAME dal referto — non disponibile: %s" % (block.get("reason")
                                                              or "errore")
    lines = ["🎾 GAME dal referto — %d partite (%s)"
             % (int(block.get("sample") or 0), block.get("source") or "-")]
    lines.append("  semantica %s | media %.2f game | mediana %.1f"
                 % (block.get("semantics"), float(block.get("observed_mean") or 0.0),
                    float(block.get("observed_median") or 0.0)))
    if block.get("base_suggested") is not None:
        lines.append("  BASE_HOLD attuale %.3f (prevede %.2f game) -> "
                     "suggerita %.3f (scarto %+.2f)"
                     % (float(block.get("base_current") or 0.0),
                        float(block.get("predicted_mean_at_current") or 0.0),
                        float(block["base_suggested"]),
                        float(block.get("gap_games_at_current") or 0.0)))
        lines.append("  probabilita' d'ingresso: %s | curva monotona: %s"
                     % (block.get("probs_source"), block.get("monotone")))
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in args

    def _opt(name: str) -> Optional[str]:
        for i, a in enumerate(args):
            if a == name and i + 1 < len(args):
                return args[i + 1]
        return None

    if "--games" in args or "--calibrate-base" in args:
        block = (calibrate_base_hold() if "--calibrate-base" in args
                 else ledger_games_stats())
        print(json.dumps(block, indent=2, default=str) if as_json
              else format_games_report(block))
        return 0 if block.get("ok") else 1

    prob = _opt("--prob")
    if prob is None:
        print("uso: tennis_markov.py --prob 0.62 [--surface clay] "
              "[--price 1.85] [--live '1-0,3-2,30-15'] [--compare] [--json]\n"
              "     tennis_markov.py --games | --calibrate-base [--json]")
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
