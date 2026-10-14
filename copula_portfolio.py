"""copula_portfolio.py — Dipendenza fra scommesse SIMULTANEE (Modulo 6, Fase 2).

Perche' esiste
--------------
`auto_bet.apply_correlation_cap` (Fase 0 del progetto) raggruppa i candidati per
partita/lega+finestra e TAGLIA il blocco al 30% del bankroll: e' una protezione
giusta ma **binaria** — sotto il cap il portafoglio e' trattato come se fosse
indipendente, sopra il cap viene scalato tutto allo stesso modo.

`portfolio_risk.py` (Fase 1) ha MISURATO la dipendenza reale: per gambe sulla
STESSA partita la legge congiunta e' esatta (griglia Poisson + Dixon-Coles, non
una stima) e per partite diverse il proxy empirico esiste ma il campione e'
piccolo. Questo modulo e' la **Fase 2**: da quella dipendenza ricava un
**moltiplicatore di stake in [0, 1]** e un **budget di coda** sul portafoglio.

Le tre bande (direttiva del proprietario, 14/10/2026)
-----------------------------------------------------
| dipendenza effettiva `d`        | effetto                                    |
|---------------------------------|--------------------------------------------|
| `d <= COPULA_MAX_CORR`          | moltiplicatore **1.0** (invariato)         |
| `COPULA_MAX_CORR < d < CRITICA` | riduzione **proporzionale** dello stake    |
| `d >= COPULA_CRITICAL_CORR`     | **blocco** dell'ordine + log dello scarto  |

`d` e' la **dipendenza effettiva**, non la correlazione lineare:
`d = (1 - w) * rho + w * lambda_tail(rho, nu)` con `w = COPULA_TAIL_WEIGHT`.
La parte di coda (`lambda_tail`, coefficiente di dipendenza di coda della
t-copula) e' cio' che distingue "due esiti che si muovono insieme" da "due esiti
che **perdono insieme**": e' quest'ultima che amplifica il drawdown.

Due vincoli STRUTTURALI (non politiche, proprieta' del codice)
-------------------------------------------------------------
1. **Il moltiplicatore e' SEMPRE in [0, 1]** (`_clamp01`): il modulo puo' solo
   ridurre o azzerare. Nessun fattore > 1.0 esiste nel sorgente, e un tripwire
   lo verifica sul CODICE (non sulla prosa). Un modulo che alza lo stake sarebbe
   una decisione di strategia del proprietario, non un effetto di una copula.
2. **Solo lettura**: nessun ordine, nessuna scrittura sul ledger. L'unico accesso
   al DB e' una connessione `mode=ro` per leggere le posizioni APERTE.

Budget di coda (l'altra meta' della protezione)
-----------------------------------------------
Il moltiplicatore da solo non basta: dieci puntate poco correlate sommano una
coda rischiosa. Il modulo simula il **portafoglio intero** (posizioni aperte +
candidati accettati + il candidato in esame) con la copula e impone

    perdita di coda (quantile COPULA_DD_QUANTILE) <= COPULA_DD_LEVEL * bankroll

La perdita di coda e' **monotona nello stake** (ridurre lo stake riduce la
coda), quindi la soluzione e' sempre una RIDUZIONE frazionaria quando esiste; se
non esiste, il candidato viene bloccato (la coda e' gia' saturata dai pick
accettati). Sotto `COPULA_MIN_MULTIPLIER` si blocca invece di ridurre: un
moltiplicatore dell'1% non e' una puntata, e' un blocco mascherato, e va
dichiarato come blocco.

Terzo vincolo: **cap di CLUSTER** (`COPULA_CLUSTER_PCT` del bankroll per lega),
perche' dieci puntate su un solo campionato sono un solo rischio con dieci nomi.

Dipendenza fra partite DIVERSE: dichiarata, mai indovinata
---------------------------------------------------------
Non esiste una variabile condivisa osservabile, quindi la correlazione si
DICHIARA per classe di rischio (tutte env, tutte reversibili):

| relazione fra due unita'                        | rho dichiarato              |
|-------------------------------------------------|-----------------------------|
| **stessa partita** (griglia punteggi disponibile) | **esatta** (nessun rho)    |
| stessa partita senza griglia (lambda assenti)    | `COPULA_SAME_MATCH_RHO` (0.98) -> d 0.89 -> **BLOCCATO** |
| stessa lega/cluster + kickoff nella finestra     | `COPULA_SAME_LEAGUE_RHO`    |
| kickoff nella finestra, leghe diverse            | `COPULA_SAME_WINDOW_RHO`    |
| altrimenti                                       | **0** (indipendenti)        |

Fallback dichiarati (mai silenziosi): senza `scipy` la copula diventa
**gaussiana** (senza dipendenza di coda) e il report scrive `copula=gaussian`;
una matrice non definita positiva viene regolarizzata con jitter e, se ancora
non lo e', si degrada a indipendente **dichiarando** il motivo.

Fail-safe
---------
Nessuna funzione pubblica solleva al chiamante: un errore diventa un campo
(`error`) o un motivo machine-readable nella decisione, mai un traceback. La
politica sull'INDECIDIBILE e' **fail-open dichiarato**: se la misura non e'
calcolabile, lo stake resta quello dei gate (bloccare tutto per un guasto di
calcolo arresterebbe il bot per un motivo che non e' un rischio misurato) — la
stessa direzione di `apply_correlation_cap` e `live_intel`.

Configurazione (env, lette a RUNTIME: nessun redeploy per tarare)
----------------------------------------------------------------
`COPULA_GUARD` (1/0), `COPULA_MODE` (`enforce`|`off`), `COPULA_MAX_CORR` (0.45),
`COPULA_CRITICAL_CORR` (0.80), `COPULA_TAIL_WEIGHT` (0.5), `COPULA_TAIL_NU` (6),
`COPULA_MAX_DD_PROB` (0.35, solo report), `COPULA_DD_LEVEL` (0.35),
`COPULA_DD_QUANTILE` (0.05), `COPULA_MIN_MULTIPLIER`
(0.25), `COPULA_CLUSTER_PCT` (0.50), `COPULA_SAME_LEAGUE_RHO` (0.80),
`COPULA_SAME_WINDOW_RHO` (0.15), `COPULA_SAME_MATCH_RHO` (0.98),
`COPULA_WINDOW_MIN` (default: `auto_bet.CORRELATION_WINDOW_MIN`),
`COPULA_SIMS` (2000), `COPULA_SEED` (7), `COPULA_VIG_HAIRCUT` (0.04).

Un valore impossibile ricade sul default e LOGGA: una guardia di rischio non si
spegne con un env sbagliato (lezione del 02/10).

CLI
---
  venv/bin/python copula_portfolio.py [--json] [--bankroll N] [--db PATH]
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configurazione: costanti + lettura da env a RUNTIME
# ---------------------------------------------------------------------------
ENABLED_ENV = "COPULA_GUARD"
MODE_ENV = "COPULA_MODE"
OFF_VALUES = ("0", "false", "no", "off", "disabled", "paused")

#: Sotto questa dipendenza effettiva lo stake e' INVARIATO.
DEFAULT_MAX_CORR = 0.45
#: Da questa dipendenza effettiva in su l'ordine viene BLOCCATO.
DEFAULT_CRITICAL_CORR = 0.80
#: Peso della dipendenza di CODA (t-copula) nella dipendenza effettiva.
DEFAULT_TAIL_WEIGHT = 0.50
#: Gradi di liberta' della t-copula (piu' basso = code piu' spesse).
DEFAULT_TAIL_NU = 6.0
#: Budget di coda — criterio: PERDITA DI CODA (VaR), non probabilita' di evento.
#: `-quantile(P/L, dd_quantile) <= dd_level * bankroll`.
#: Perche' il quantile e non P(perdita >= soglia) (misurato il 14/10/2026):
#: la probabilita' di un evento e' una funzione a SCALINO del moltiplicatore —
#: su 6 gambe correlate da 5.60 valeva 0.31 al cap, e bastava abbassare la
#: soglia a 0.30 perche' nessun moltiplicatore fosse ammissibile (un blocco di
#: tutto il cluster, non una riduzione). Il quantile invece DECRESCE con lo
#: stake (5.60 -> 3.00 ne dimezza la coda) e ha sempre soluzione frazionaria,
#: che e' la direttiva ("correlazione moderata: riduce proporzionalmente").
DEFAULT_DD_LEVEL = 0.35
DEFAULT_DD_QUANTILE = 0.05
#: Soglia informativa (solo report): probabilita' di sfondare il livello di DD.
DEFAULT_MAX_DD_PROB = 0.35
#: Sotto questo moltiplicatore non si riduce: si blocca (un 1% non e' una puntata).
DEFAULT_MIN_MULTIPLIER = 0.25
#: Quota massima del BANKROLL concentrabile in UN cluster a rischio (lega).
#: Misurata sul bankroll e non sulla somma delle puntate: su un portafoglio
#: vuoto la somma vale 0 e il budget pure, quindi il primo pick veniva bloccato
#: per "concentrazione di se' stesso" (misurato: BLOCCAVA OGNI ORDINE).
DEFAULT_CLUSTER_PCT = 0.50
#: Correlazioni DICHIARATE per classe di rischio (mai inventate dai dati).
DEFAULT_SAME_LEAGUE_RHO = 0.80
DEFAULT_SAME_WINDOW_RHO = 0.15
#: Stessa PARTITA: 0.98 e non 0.90, per una ragione MISURATA. Con 0.90 la
#: dipendenza effettiva vale 0.73 — banda MODERATA — quindi due esposizioni
#: sulla stessa partita venivano solo ridotte: ma un 1X2 e un AH sulla stessa
#: partita sono lo STESSO evento con due nomi, e la direttiva li vuole
#: BLOCCATI ("correlazione critica: blocca l'ordine"). Con 0.98 la dipendenza
#: effettiva e' 0.89 >= 0.80 -> blocco. Quando la coppia e' modellabile
#: esattamente (griglia punteggi: stessa partita raggruppata in UNA unita')
#: questo rho non viene usato affatto: la legge congiunta e' calcolata, non
#: dichiarata, e il blocco non serve.
DEFAULT_SAME_MATCH_RHO = 0.98
#: Simulazioni della copula e seme: il seme fisso rende il gate RIPRODUCIBILE
#: (stesse decisioni a parita' di portafoglio: un gate di denaro che cambia
#: risposta a ogni giro senza che nulla sia cambiato non e' verificabile).
DEFAULT_SIMS = 2000
DEFAULT_SEED = 7
#: Haircut sul margine del book quando la probabilita' del modello non e'
#: disponibile: `p = (1/quota) / (1 + haircut)`. Direzione CONSERVATIVA (alza la
#: probabilita' di perdita: un 4% e' l'overround tipico di un 1X2).
DEFAULT_VIG_HAIRCUT = 0.04
#: Etichetta della dipendenza quando si calcola la copula.
COPULA_T = "t"
COPULA_GAUSSIAN = "gaussian"

#: Griglia dei moltiplicatori: si sceglie il PIU' ALTO che rispetta il budget di
#: coda. Una griglia (non una bisetzione) perche' il criterio NON e' monotono in
#: `m`: ridurre lo stake riduce la perdita dove si perde ma anche il profitto
#: dove si vince, e su una quantile EMPIRICA di una distribuzione discreta
#: aggiungere un esito positivo puo' alzare il quantile piu' di quanto affondi
#: la coda (misurato: 5 unita' -> perdita di coda 16.5, 6 unita' -> 13.0). Con
#: la griglia OGNI punto viene valutato e la scelta e' esatta e riproducibile.
M_GRID = np.round(np.linspace(0.0, 1.0, 41), 6)

#: Esiti delle decisioni (machine-readable, mai prosa).
DECISION_CLEAR = "clear"                  # d <= max_corr: nessun effetto
DECISION_SCALED = "scaled"                # riduzione proporzionale
DECISION_BLOCKED_CRITICAL = "blocked_critical"      # d >= critical
DECISION_BLOCKED_TAIL = "blocked_tail_budget"       # budget di coda sfondato
DECISION_BLOCKED_CLUSTER = "blocked_cluster"        # cluster troppo concentrato
DECISION_BLOCKED_MULTIPLIER = "blocked_min_multiplier"
DECISION_BLOCKED_TICKET = "blocked_below_ticket"
DECISION_ERROR = "error"

#: Perche' una unita' e' stata misurata in un certo modo (dichiarato nel report).
SOURCE_GRID = "grid"           # distribuzione ESATTA dai punteggi (Dixon-Coles)
SOURCE_MARKET = "market"       # 2/3 punti dalla probabilita' di mercato


def _num_env(name: str, default: float, *, minimum: float = 0.0,
             maximum: Optional[float] = None) -> float:
    """Env numerica con clamp e WARNING: un valore impossibile non spegne nulla."""
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning("copula_portfolio: %s='%s' non numerico, uso %s",
                       name, raw, default)
        return default
    if not math.isfinite(value):
        logger.warning("copula_portfolio: %s non finito, uso %s", name, default)
        return default
    if value < minimum:
        logger.warning("copula_portfolio: %s=%s sotto il minimo %s, rialzato",
                       name, value, minimum)
        value = minimum
    if maximum is not None and value > maximum:
        logger.warning("copula_portfolio: %s=%s sopra il massimo %s, abbassato",
                       name, value, maximum)
        value = maximum
    return value


def _flag_env(name: str, default: bool) -> bool:
    """Flag tri-stato: assente -> default, valore riconosciuto -> bool."""
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() not in OFF_VALUES


def window_minutes() -> float:
    """Finestra dei kickoff per il raggruppamento correlato.

    Il default e' `auto_bet.CORRELATION_WINDOW_MIN` (90'): la definizione di
    "blocco correlato" del progetto e' UNA, e questo modulo non ne inventa una
    seconda (import pigro + fail-safe sul valore di ripiego).
    """
    fallback = 90.0
    try:
        import auto_bet
        fallback = float(getattr(auto_bet, "CORRELATION_WINDOW_MIN", fallback))
    except Exception:
        pass
    return _num_env("COPULA_WINDOW_MIN", fallback, minimum=0.0)


def config() -> Dict[str, Any]:
    """Configurazione efficace (env a RUNTIME: nessun redeploy per tarare)."""
    enabled = _flag_env(ENABLED_ENV, True)
    mode = (os.getenv(MODE_ENV, "enforce") or "enforce").strip().lower()
    if mode not in ("enforce", "off"):
        logger.warning("copula_portfolio: %s='%s' non riconosciuto, uso "
                       "'enforce'", MODE_ENV, mode)
        mode = "enforce"
    max_corr = _num_env("COPULA_MAX_CORR", DEFAULT_MAX_CORR, maximum=1.0)
    critical = _num_env("COPULA_CRITICAL_CORR", DEFAULT_CRITICAL_CORR,
                        maximum=1.0)
    # Banda coerente: la soglia critica non puo' stare SOTTO quella moderata
    # (renderebbe la banda moderata vuota e il blocco immediato).
    if critical < max_corr:
        logger.warning("copula_portfolio: COPULA_CRITICAL_CORR %.2f < "
                       "COPULA_MAX_CORR %.2f: uso %.2f", critical, max_corr,
                       max_corr)
        critical = max_corr
    return {
        "enabled": bool(enabled),
        "mode": mode,
        "active": bool(enabled and mode == "enforce"),
        "max_corr": max_corr,
        "critical_corr": critical,
        "tail_weight": _num_env("COPULA_TAIL_WEIGHT", DEFAULT_TAIL_WEIGHT,
                                maximum=1.0),
        "nu": _num_env("COPULA_TAIL_NU", DEFAULT_TAIL_NU, minimum=2.01,
                       maximum=200.0),
        "max_dd_prob": _num_env("COPULA_MAX_DD_PROB", DEFAULT_MAX_DD_PROB,
                                maximum=1.0),
        "dd_level": _num_env("COPULA_DD_LEVEL", DEFAULT_DD_LEVEL, maximum=1.0),
        "dd_quantile": _num_env("COPULA_DD_QUANTILE", DEFAULT_DD_QUANTILE,
                                maximum=0.5),
        "min_multiplier": _num_env("COPULA_MIN_MULTIPLIER",
                                   DEFAULT_MIN_MULTIPLIER, maximum=1.0),
        "cluster_pct": _num_env("COPULA_CLUSTER_PCT", DEFAULT_CLUSTER_PCT,
                                maximum=1.0),
        "same_league_rho": _num_env("COPULA_SAME_LEAGUE_RHO",
                                    DEFAULT_SAME_LEAGUE_RHO, maximum=1.0),
        "same_window_rho": _num_env("COPULA_SAME_WINDOW_RHO",
                                    DEFAULT_SAME_WINDOW_RHO, maximum=1.0),
        "same_match_rho": _num_env("COPULA_SAME_MATCH_RHO",
                                   DEFAULT_SAME_MATCH_RHO, maximum=1.0),
        "sims": int(_num_env("COPULA_SIMS", DEFAULT_SIMS, minimum=200.0,
                             maximum=200000.0)),
        "seed": int(_num_env("COPULA_SEED", DEFAULT_SEED, minimum=0.0)),
        "vig_haircut": _num_env("COPULA_VIG_HAIRCUT", DEFAULT_VIG_HAIRCUT,
                                maximum=0.9),
        "window_min": window_minutes(),
    }


def _clamp01(value: Any) -> float:
    """Moltiplicatore SEMPRE in [0, 1]: il modulo puo' solo ridurre.

    E' l'invariante del modulo (direttiva 14/10/2026): nessun percorso puo'
    alzare uno stake. Un valore non numerico vale 0.0 (fail-closed sulla
    riduzione, mai un moltiplicatore inventato).
    """
    try:
        out = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(out):
        return 0.0
    return min(max(out, 0.0), 1.0)


# ---------------------------------------------------------------------------
# Copula: latente, coda, simulazione
# ---------------------------------------------------------------------------
def _scipy_stats():
    """`scipy.stats` se disponibile, altrimenti None (degrado DICHIARATO)."""
    try:
        from scipy import stats  # import locale: `import copula_portfolio` resta leggero
        return stats
    except Exception:
        return None


def _norm_cdf(x: np.ndarray) -> np.ndarray:
    """Phi(x) senza dipendere da scipy (`math.erf` e' standard library)."""
    try:
        from scipy.special import erf as _erf
        return 0.5 * (1.0 + _erf(np.asarray(x, dtype=float) / math.sqrt(2.0)))
    except Exception:
        flat = np.asarray(x, dtype=float).ravel()
        out = np.array([0.5 * (1.0 + math.erf(v / math.sqrt(2.0))) for v in flat],
                       dtype=float)
        return out.reshape(np.asarray(x, dtype=float).shape)


def copula_kind(nu: Optional[float] = None, *, cfg: Optional[Dict[str, Any]] = None) -> str:
    """Tipo di copula EFFETTIVAMENTE utilizzabile: `t` o `gaussian` (fallback)."""
    conf = cfg or config()
    nu = conf["nu"] if nu is None else float(nu)
    if nu > 0 and _scipy_stats() is not None:
        return COPULA_T
    return COPULA_GAUSSIAN


def tail_dependence(rho: float, nu: float) -> float:
    """Coefficiente di dipendenza di coda della t-copula.

    `lambda = 2 * t_{nu+1}(-sqrt((nu+1)(1-rho)/(1+rho)))` — quanto due esiti
    PERDONO insieme quando il mercato va male. Senza scipy il valore ripiega
    sulla correlazione lineare e il degrado e' dichiarato da `copula_kind()`.
    """
    rho = _clamp01(abs(rho))
    if rho >= 1.0:
        return 1.0
    if rho <= 0.0:
        return 0.0
    stats = _scipy_stats()
    if stats is None or nu <= 0:
        return rho
    try:
        arg = -math.sqrt((nu + 1.0) * (1.0 - rho) / (1.0 + rho))
        return float(min(max(2.0 * float(stats.t.cdf(arg, nu + 1.0)), 0.0), 1.0))
    except Exception:
        return rho


def effective_dependence(rho: float, *, cfg: Optional[Dict[str, Any]] = None) -> float:
    """Dipendenza EFFETTIVA: parte lineare + parte di CODA (peso da env)."""
    conf = cfg or config()
    w = _clamp01(conf["tail_weight"])
    linear = _clamp01(abs(rho))
    tail = tail_dependence(linear, float(conf["nu"]))
    return _clamp01((1.0 - w) * linear + w * tail)


def _cholesky(corr: np.ndarray) -> Optional[np.ndarray]:
    """Cholesky con jitter: None se la matrice non e' definita positiva.

    Una matrice malformata NON si 'aggiusta' in silenzio oltre il jitter
    numerico: se non e' decomponibile si degrada a indipendente e il motivo
    viene dichiarato dal chiamante (mai un rho inventato).
    """
    try:
        return np.linalg.cholesky(np.asarray(corr, dtype=float))
    except Exception:
        pass
    eye = np.eye(corr.shape[0])
    for jitter in (1e-9, 1e-7, 1e-5):
        try:
            return np.linalg.cholesky(np.asarray(corr, dtype=float) + jitter * eye)
        except Exception:
            continue
    return None


def _components(corr: np.ndarray, *, tol: float = 1e-9) -> List[np.ndarray]:
    """Componenti connesse a dipendenza POSITIVA (per il fattore di coda).

    Due unita' legate da `rho > 0` devono condividere il fattore chi-quadro
    (dipendenza di coda); due unita' indipendenti no — altrimenti la copula
    inventa una coda comune su un portafoglio dichiarato indipendente.
    """
    n = int(corr.shape[0])
    seen = [False] * n
    out: List[np.ndarray] = []
    for start in range(n):
        if seen[start]:
            continue
        stack, group = [start], []
        seen[start] = True
        while stack:
            i = stack.pop()
            group.append(i)
            for j in range(n):
                if not seen[j] and abs(float(corr[i, j])) > tol:
                    seen[j] = True
                    stack.append(j)
        out.append(np.array(sorted(group), dtype=int))
    return out


def _latent_uniforms(z: np.ndarray, nu: float) -> Tuple[np.ndarray, str]:
    """Trasforma il latente (correlato) in uniformi: t-copula, o gaussiana."""
    stats = _scipy_stats()
    if nu > 0 and stats is not None:
        try:
            return np.asarray(stats.t.cdf(z, nu), dtype=float), COPULA_T
        except Exception:                                     # pragma: no cover
            pass
    return _norm_cdf(z), COPULA_GAUSSIAN


def simulate(units: Sequence[Dict[str, Any]], corr: np.ndarray, *,
             cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Simula il portafoglio con la copula: indici di esito e P/L base.

    Ritorna {ok, base, values, idx, kind, error}: `base` e' il P/L totale di
    TUTTE le unita' passate (candidato compreso), `values`/`idx` permettono di
    valutare il candidato su una griglia di moltiplicatori SENZA ri-simulare (il
    moltiplicatore scala lo stake, non la dipendenza: gli esiti restano quelli).
    Il chiamante che vuole il P/L SENZA il candidato fa `base - values[-1][idx[-1]]`
    (una sola definizione della griglia, cosi' le due letture non divergono).
    """
    conf = cfg or config()
    n = len(units)
    out: Dict[str, Any] = {"ok": False, "base": None, "values": None, "idx": None,
                           "kind": None, "error": None}
    if n == 0:
        out.update({"ok": True, "kind": copula_kind(cfg=conf)})
        return out
    try:
        sims = int(conf["sims"])
        rng = np.random.default_rng(int(conf["seed"]))
        chol = _cholesky(np.asarray(corr, dtype=float))
        if chol is None:
            out["error"] = "matrice_non_definita_positiva"
            return out
        eps = rng.standard_normal((n, sims))
        z = chol @ eps
        nu = float(conf["nu"])
        if nu > 0 and _scipy_stats() is not None:
            # Miscelazione chi-quadro CONDIVISA per COMPONENTE a rischio.
            # Il fattore di coda e' cio' che genera la dipendenza di coda della
            # t-copula: condividerlo fra TUTTE le unita' la creerebbe anche fra
            # unita' dichiarate INDIPENDENTI (d = 0), dove la copula t non sa
            # rappresentare l'indipendenza — misurato: 6 unita' a rho=0
            # mostravano P(0 perdenti) 0.277 invece di 0.588^6 = 0.041. Le
            # componenti connesse a dipendenza POSITIVA condividono il fattore;
            # componenti diverse no. Le marginali restano t(nu) (il fattore e'
            # chi-quadro in ogni caso), quindi l'uniformizzazione resta esatta.
            for comp in _components(corr):
                chi = rng.chisquare(nu, size=sims)
                scale = np.sqrt(nu / np.maximum(chi, 1e-9))[None, :]
                z[comp, :] = z[comp, :] * scale
        u, kind = _latent_uniforms(z, nu)
        u = np.clip(u, 1e-9, 1.0 - 1e-9)
        values: List[np.ndarray] = []
        idx: List[np.ndarray] = []
        for i, unit in enumerate(units):
            dist = unit.get("distribution") or {}
            vals = dist.get("values")
            probs = dist.get("probs")
            if vals is None or probs is None:
                out["error"] = "distribuzione_assente:%s" % (unit.get("key") or i)
                return out
            cum = np.cumsum(np.asarray(probs, dtype=float))
            cum[-1] = 1.0
            values.append(np.asarray(vals, dtype=float))
            idx.append(np.searchsorted(cum, u[i], side="right"))
        base = np.zeros(sims, dtype=float)
        for vals, iarr in zip(values, idx):
            base = base + vals[np.clip(iarr, 0, len(vals) - 1)]
        out.update({"ok": True, "base": base, "values": values, "idx": idx,
                    "kind": kind})
        return out
    except Exception as e:                                    # pragma: no cover
        logger.debug("copula_portfolio: simulazione non riuscita: %s", e)
        out["error"] = "errore_simulazione"
        return out


def tail_budget_multiplier(base: np.ndarray, values: np.ndarray,
                           idx: np.ndarray, *, bankroll: float,
                           cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Moltiplicatore piu' ALTO che rispetta il budget di coda.

    Criterio: `-quantile(P/L del portafoglio, dd_quantile) <= dd_level * bankroll`
    — la PERDITA di coda ammessa. Il criterio si VALUTA su ogni punto della
    griglia, senza assumere la monotonia: su una quantile EMPIRICA di una
    distribuzione discreta la perdita non e' monotona in `m` (misurato: la
    stessa terna di pick con 5 unita' dava 16.5, con 6 unita' 13.0 — aggiungere
    un esito positivo sposta il quantile piu' di quanto affondi la coda). Ogni
    punto viene misurato e si sceglie il PIU' ALTO feasible, quindi il
    moltiplicatore restituito rispetta SEMPRE il vincolo.
    `p_loss` resta nel risultato come misura INFORMATIVA (probabilita' di
    sfondare il livello), mai come vincolo.
    """
    conf = cfg or config()
    out: Dict[str, Any] = {"multiplier": 1.0, "p_loss": None, "loss_q": None,
                           "feasible": True, "error": None}
    try:
        bankroll = float(bankroll)
        if bankroll <= 0:
            out["error"] = "bankroll_non_positivo"
            return out
        level = abs(float(conf["dd_level"])) * bankroll
        q = float(conf["dd_quantile"])
        cand_pl = np.asarray(values, dtype=float)[np.clip(
            np.asarray(idx, dtype=int), 0, len(values) - 1)]
        total = np.asarray(base, dtype=float)[:, None] + cand_pl[:, None] * M_GRID[None, :]
        loss_q = -np.quantile(total, q, axis=0)
        out["loss_q"] = float(loss_q[-1])
        out["p_loss"] = float((total <= -level).mean(axis=0)[-1])
        ok = np.nonzero(loss_q <= level + 1e-12)[0]
        if ok.size:
            out["multiplier"] = float(M_GRID[int(ok[-1])])
        else:
            out["multiplier"] = 0.0
            out["feasible"] = False
        return out
    except Exception as e:                                    # pragma: no cover
        logger.debug("copula_portfolio: budget di coda non calcolato: %s", e)
        out["error"] = "errore_budget_coda"
        return out


# ---------------------------------------------------------------------------
# Unita' di rischio (una partita = una unita': dentro e' la griglia, esatta)
# ---------------------------------------------------------------------------
def _parse_ts(value: Any) -> Optional[datetime]:
    """Istante UTC da una data del ledger. None se assente/ambigua.

    Un istante SENZA fuso orario non si indovina (regola del 15/09: un tempo
    ambiguo costa un CLV su un istante sbagliato): qui vale None e il chiamante
    dichiara `time_unknown`.
    """
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except Exception:
            return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(timezone.utc)


def _market_of(leg: Dict[str, Any]) -> str:
    raw = str(leg.get("mercato") or leg.get("market") or leg.get("market_type") or "")
    return raw.strip().upper().replace("_", " ")


def _canonical_cluster(league: Any) -> str:
    """Chiave canonica del cluster a rischio: la lega, risolta se possibile.

    Delegare a `value_filter.canonical_league` (quando esiste) evita che lo
    stesso campionato conti come due cluster per una differenza di etichetta
    fra provider — lo stesso difetto del 24/09 sul gate di lega.
    """
    text = str(league or "").strip()
    if not text:
        return ""
    try:
        from value_filter import canonical_league
        canon = canonical_league(text)
        if canon:
            return str(canon)
    except Exception:
        pass
    return text


def _probability_of(leg: Dict[str, Any], *, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Probabilita' di vincita della gamba + la sua FONTE (mai un numero muto).

    Priorita': probabilita' esplicita del segnale (`p_true`/`prob`/`model_prob`/
    `blended_prob`/`market_prob`) e, in mancanza, la probabilita' implicita di
    mercato CON haircut sul margine (direzione conservativa: alza la probabilita'
    di perdita, quindi il rischio misurato non e' sottostimato).
    """
    for key in ("p_true", "true_prob", "prob", "model_prob", "blended_prob",
                "market_prob", "p_win"):
        raw = leg.get(key)
        if raw is None:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and 0.0 < value < 1.0:
            return {"p_win": value, "prob_source": key, "haircut": 0.0}
    odds = _odds_of(leg)
    if odds is None or odds <= 1.0:
        return {"p_win": None, "prob_source": None, "haircut": None}
    haircut = float(cfg.get("vig_haircut") or 0.0)
    p = (1.0 / odds) / (1.0 + max(haircut, 0.0))
    return {"p_win": min(max(p, 1e-6), 1.0 - 1e-6),
            "prob_source": "market_implied", "haircut": haircut}


def _odds_of(leg: Dict[str, Any]) -> Optional[float]:
    raw = leg.get("quota")
    if raw is None:
        raw = leg.get("odds", leg.get("price"))
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def _stake_of(leg: Dict[str, Any]) -> float:
    try:
        value = float(leg.get("stake") or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return value if math.isfinite(value) and value > 0 else 0.0


def _stake_key(leg: Dict[str, Any]) -> float:
    """Stake del candidato: `stake` e, in fallback, la quota e' inutile -> 0."""
    return _stake_of(leg)


def distribution_of(unit: Dict[str, Any], *,
                    max_goals: Optional[int] = None) -> Dict[str, Any]:
    """Distribuzione del P/L dell'unita': ESATTA (griglia) o di mercato.

    - **griglia** (`SOURCE_GRID`): se l'unita' porta le lambda della partita, il
      P/L di TUTTE le sue gambe e' calcolato insieme cella per cella da
      `portfolio_risk.block_pl_distribution` (stessa matematica di produzione).
    - **mercato** (`SOURCE_MARKET`): altrimenti 2/3 punti dalla probabilita'
      (vincita / perdita, piu' il push a 0 quando la gamba e' a linea).
    """
    out: Dict[str, Any] = {"values": None, "probs": None, "source": None,
                           "error": None, "detail": ""}
    legs = [l for l in (unit.get("legs") or []) if _stake_of(l) > 0]
    if not legs:
        out["error"] = "nessuna_gamba_con_stake"
        return out
    lam_h, lam_a = unit.get("lam_h"), unit.get("lam_a")
    if lam_h and lam_a:
        try:
            import portfolio_risk as pr
            kwargs = {"max_goals": int(max_goals)} if max_goals else {}
            blk = pr.block_pl_distribution(legs, lam_h, lam_a, **kwargs)
        except Exception as e:
            logger.debug("copula_portfolio: griglia non disponibile (%s)", e)
            blk = {"error": "griglia_non_disponibile"}
        if blk.get("values") is not None and blk.get("probs") is not None:
            out.update({"values": np.asarray(blk["values"], dtype=float),
                        "probs": np.asarray(blk["probs"], dtype=float),
                        "source": SOURCE_GRID,
                        "detail": "griglia punteggi (Dixon-Coles)"})
            return out
        out["detail"] = str(blk.get("error") or "griglia_non_utilizzabile")
    # --- fallback di mercato: 2/3 punti per gamba, gambe trattate di volta in
    # volta (una unita' multi-gamba senza griglia non e' modellabile: il
    # chiamante la divide; qui si prende la prima gamba e si DICHIARA).
    if len(legs) > 1:
        # Piu' gambe sulla stessa partita SENZA griglia punteggi: la loro
        # dipendenza congiunta non e' rappresentabile con una distribuzione
        # marginale. Mega-lotto dichiarato: il chiamante le separa in unita'
        # indipendenti legate dal rho `same_match` (mai un conteggio parziale
        # spacciato per misura).
        out["error"] = "multi_gamba_senza_griglia"
        return out
    leg = legs[0]
    odds = _odds_of(leg)
    stake = _stake_of(leg)
    prob = _probability_of(leg, cfg=current_config())
    p_win = prob.get("p_win")
    if odds is None or p_win is None:
        out["error"] = "probabilita_non_disponibile"
        return out
    values = np.array([stake * (odds - 1.0), -stake], dtype=float)
    probs = np.array([float(p_win), 1.0 - float(p_win)], dtype=float)
    out.update({"values": values, "probs": probs, "source": SOURCE_MARKET,
                "detail": "2 punti da %s%s" % (
                    prob.get("prob_source"),
                    " (haircut %.2f)" % prob["haircut"] if prob.get("haircut") else ""),
                "p_win": float(p_win), "prob_source": prob.get("prob_source")})
    return out


#: Cache della configurazione (il gate gira nel ciclo da 60s): riletta quando
#: l'ambiente cambia, cosi' un `railway variables --set` non richiede un
#: redeploy (la taratura resta possibile a caldo) e la lettura non costa nulla.
CONF_CACHE: Dict[str, Any] = {"stamp": None, "cfg": None}


def _conf_hash() -> Tuple[Any, ...]:
    return tuple(sorted((k, v) for k, v in os.environ.items()
                        if k.startswith("COPULA_")))


def current_config() -> Dict[str, Any]:
    """`config()` memoizzata sull'AMBIENTE: nessuna lettura ripetuta inutile."""
    stamp = _conf_hash()
    if CONF_CACHE["stamp"] != stamp:
        CONF_CACHE["cfg"] = config()
        CONF_CACHE["stamp"] = stamp
    return CONF_CACHE["cfg"] or config()


def reset_config_cache() -> None:
    """Azzera la memo (test e diagnostica)."""
    CONF_CACHE["stamp"] = None
    CONF_CACHE["cfg"] = None


def unit_for(match_id: Any, legs: Sequence[Dict[str, Any]], *,
             cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Costruisce l'unita' di rischio di UNA partita (gambe + contesto)."""
    conf = cfg or current_config()
    first = legs[0] if legs else {}
    league = first.get("league") or ""
    kickoff = _parse_ts(first.get("commence") or first.get("kickoff"))
    return {
        "key": str(match_id or ""),
        "match_id": str(match_id or ""),
        "league": str(league or ""),
        "cluster": _canonical_cluster(league),
        "kickoff": kickoff,
        "time_unknown": kickoff is None,
        "lam_h": first.get("lam_h"),
        "lam_a": first.get("lam_a"),
        "legs": list(legs),
        "stake": float(sum(_stake_of(l) for l in legs)),
        "market": _market_of(first),
    }


def group_units(items: Sequence[Dict[str, Any]], *,
                cfg: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Raggruppa per `match_id`: le gambe della STESSA partita = una unita'.

    E' la proprieta' che distingue questo modulo da `apply_correlation_cap`:
    sulla stessa partita la dipendenza non si stima, si CALCOLA (griglia).
    """
    conf = cfg or current_config()
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    order: List[str] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("match_id") or "")
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(item)
    return [unit_for(k, grouped[k], cfg=conf) for k in order]


def unit_with_distribution(unit: Dict[str, Any], *,
                           cfg: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Unita' con la sua distribuzione, oppure None se non e' modellabile."""
    dist = distribution_of(unit)
    if dist.get("values") is None or dist.get("probs") is None:
        return None
    out = dict(unit)
    out["distribution"] = dist
    return out


def dependence(a: Dict[str, Any], b: Dict[str, Any], *,
               cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Dipendenza EFFETTIVA fra due unita' + la relazione che l'ha determinata.

    Relazioni (in ordine di forza): stessa partita -> rho dichiarato alto (la
    griglia, quando c'e', evita del tutto questo ramo); stesso cluster con
    kickoff nella finestra -> `same_league_rho`; finestra condivisa fra leghe
    diverse -> `same_window_rho`; altrimenti 0.
    """
    conf = cfg or current_config()
    rel, rho, detail = "independent", 0.0, ""
    same_match = bool(a.get("match_id")) and a.get("match_id") == b.get("match_id")
    same_cluster = bool(a.get("cluster")) and a.get("cluster") == b.get("cluster")
    window = float(conf["window_min"])
    ka, kb = a.get("kickoff"), b.get("kickoff")
    same_window = False
    if ka is not None and kb is not None:
        same_window = abs((ka - kb).total_seconds()) <= window * 60.0
    elif ka is None or kb is None:
        detail = "time_unknown"
    if same_match:
        rel, rho = "same_match", float(conf["same_match_rho"])
    elif same_cluster and same_window:
        rel, rho = "same_cluster_window", float(conf["same_league_rho"])
    elif same_cluster:
        rel, rho = "same_cluster", float(conf["same_league_rho"])
    elif same_window:
        rel, rho = "same_window", float(conf["same_window_rho"])
    d = effective_dependence(rho, cfg=conf)
    return {"relation": rel, "rho": rho, "dependence": d, "detail": detail}


def dependence_matrix(units: Sequence[Dict[str, Any]], *,
                      cfg: Optional[Dict[str, Any]] = None
                      ) -> Tuple[np.ndarray, List[List[Dict[str, Any]]]]:
    """Matrice di dipendenza effettiva + i dettagli delle coppie."""
    conf = cfg or current_config()
    n = len(units)
    corr = np.eye(n, dtype=float)
    pairs: List[List[Dict[str, Any]]] = [[{} for _ in range(n)] for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            info = dependence(units[i], units[j], cfg=conf)
            corr[i, j] = corr[j, i] = info["dependence"]
            pairs[i][j] = pairs[j][i] = info
    return corr, pairs


def cluster_stake(units: Sequence[Dict[str, Any]], cluster: str) -> float:
    """Stake totale delle unita' di un cluster (0 se il cluster e' vuoto)."""
    if not cluster:
        return 0.0
    return float(sum(float(u.get("stake") or 0.0) for u in units
                     if u.get("cluster") == cluster))


# ---------------------------------------------------------------------------
# Posizioni APERTE (sola lettura): il rischio sistemico include cio' che e' vivo
# ---------------------------------------------------------------------------
def _ro_conn(db_path: Optional[str] = None) -> sqlite3.Connection:
    """Connessione in SOLA LETTURA (una scrittura deve essere impossibile)."""
    path = db_path
    if not path:
        from tracker import DB_PATH  # import pigro: `import copula_portfolio` resta leggero
        path = DB_PATH
    return sqlite3.connect("file:%s?mode=ro" % path, uri=True)


def open_units(*, conn=None, db_path: Optional[str] = None,
               mode: str = "live") -> Dict[str, Any]:
    """Unita' delle puntate ANCORA APERTE (le stesse righe del recinto).

    Legge `bets` con `esito_finale IS NULL AND mode=?` (la condizione del
    capitale immobilizzato) e, quando la riga esiste, la lega e le lambda di
    partita da `matches`/`match_analysis`: senza la lega il cluster non e'
    attribuibile e la dipendenza fra partite diverse NON si inventa.
    """
    out: Dict[str, Any] = {"units": [], "error": None, "rows": 0}
    own = conn is None
    try:
        if own:
            conn = _ro_conn(db_path)
        rows = conn.execute(
            "SELECT b.match_id, b.mercato, b.esito, b.stake, b.price, m.league, "
            "       m.commence_time, a.lam_h, a.lam_a "
            "FROM bets b "
            "LEFT JOIN matches m ON m.id = b.match_id "
            "LEFT JOIN match_analysis a ON a.match_id = b.match_id "
            "WHERE b.esito_finale IS NULL AND b.mode = ?", (mode,)).fetchall()
        out["rows"] = len(rows)
        items: List[Dict[str, Any]] = []
        for (mid, mercato, esito, stake, price, league, commence,
             lam_h, lam_a) in rows:
            items.append({"match_id": mid, "mercato": mercato or "",
                          "esito": esito, "stake": stake, "quota": price,
                          "league": league, "commence": commence,
                          "lam_h": lam_h, "lam_a": lam_a})
        units: List[Dict[str, Any]] = []
        for unit in group_units(items):
            prepared = unit_with_distribution(unit)
            if prepared is not None:
                units.append(prepared)
                continue
            # Senza griglia (o con piu' gambe non rappresentabili insieme) le
            # gambe diventano unita' SEPARATE legate dal rho `same_match`: una
            # posizione aperta non si perde mai dal conto del rischio vivo.
            for leg in unit.get("legs") or []:
                single = unit_for(unit["match_id"], [leg])
                one = unit_with_distribution(single)
                if one is not None:
                    units.append(one)
        out["units"] = units
        return out
    except Exception as e:
        logger.warning("copula_portfolio: lettura posizioni aperte fallita: %s", e)
        out["error"] = "read_error"
        out["caveat"] = str(e)[:120]
        return out
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Decisione: moltiplicatore per candidato
# ---------------------------------------------------------------------------
def multiplier_bands(d_max: float, *, cfg: Dict[str, Any]) -> Tuple[float, str]:
    """Moltiplicatore delle TRE bande + motivo machine-readable.

    `d <= max_corr` -> 1.0; banda intermedia -> discesa LINEARE da 1.0 al
    pavimento; `d >= critical` -> 0.0 (blocco). Il risultato e' sempre in
    [0, 1] per costruzione (`_clamp01`).
    """
    d = _clamp01(d_max)
    lo = float(cfg["max_corr"])
    hi = float(cfg["critical_corr"])
    if d >= hi:
        return 0.0, DECISION_BLOCKED_CRITICAL
    if d <= lo:
        return 1.0, DECISION_CLEAR
    span = max(hi - lo, 1e-9)
    mult = 1.0 - (d - lo) / span
    return _clamp01(mult), DECISION_SCALED


def evaluate(candidates: Sequence[Dict[str, Any]], *, bankroll: float,
             open_positions: Sequence[Dict[str, Any]] = (),
             lambdas_lookup: Any = None,
             cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Valuta OGNI candidato contro il portafoglio (aperto + gia' accettato).

    L'ordine di valutazione e' per `best_ev` decrescente (lo stesso criterio con
    cui `apply_flat_budget` sceglie chi tenere): il rischio residuo viene
    assegnato prima ai segnali piu' deboli, non a caso. La lista di RITORNO e'
    nel suo ordine originale (l'ordine di esecuzione resta quello del giro).

    Non solleva MAI: un errore diventa `info["error"]` e i candidati restano
    invariati (fail-open dichiarato).
    """
    conf = cfg or current_config()
    info: Dict[str, Any] = {
        "enabled": bool(conf["enabled"]), "active": bool(conf["active"]),
        "mode": conf["mode"], "kept": 0, "scaled": 0, "blocked": 0,
        "clear": 0, "by_reason": {}, "copula": copula_kind(cfg=conf),
        "error": None, "open_units": 0, "max_corr": conf["max_corr"],
        "critical_corr": conf["critical_corr"], "dd_level": conf["dd_level"],
        "max_dd_prob": conf["max_dd_prob"], "min_multiplier": conf["min_multiplier"],
        "cluster_pct": conf["cluster_pct"], "coinvolti": 0, "errored": 0,
    }
    out: Dict[str, Any] = {"candidates": list(candidates or []), "info": info}
    if not conf["active"]:
        return out
    try:
        min_ticket = 0.0
        try:
            import auto_bet
            min_ticket = float(auto_bet.aggressive_min_ticket())
        except Exception:
            min_ticket = 0.0
        # 1) portafoglio di partenza: le posizioni APERTE (+ eventuali unita'
        #    gia' risolte passate dal chiamante).
        base_units: List[Dict[str, Any]] = []
        for unit in open_positions or []:
            prepared = unit_with_distribution(unit, cfg=conf)
            if prepared is not None:
                base_units.append(prepared)
        info["open_units"] = len(base_units)

        # 2) candidati come unita' (una per partita), in ordine di EV decrescente
        prepared_cands: List[Dict[str, Any]] = []
        ordered: List[int] = []
        for idx, cand in enumerate(candidates or []):
            if not isinstance(cand, dict):
                continue
            stake = _stake_key(cand)
            if stake <= 0:
                continue
            lam_h, lam_a = cand.get("lam_h"), cand.get("lam_a")
            if (lam_h is None or lam_a is None) and callable(lambdas_lookup):
                try:
                    found = lambdas_lookup(cand.get("match_id"))
                except Exception:
                    found = None
                if isinstance(found, dict):
                    lam_h = found.get("lam_h") if lam_h is None else lam_h
                    lam_a = found.get("lam_a") if lam_a is None else lam_a
            unit = unit_for(cand.get("match_id"), [{**cand, "lam_h": lam_h,
                                                    "lam_a": lam_a}], cfg=conf)
            unit["candidate_index"] = idx
            prepared_cands.append(unit)
            ordered.append(idx)
        ordered.sort(key=lambda i: -float(
            (candidates[i].get("best_ev") or 0.0) if isinstance(candidates[i], dict)
            else 0.0))
        by_index = {u["candidate_index"]: u for u in prepared_cands}
        decisions: Dict[int, Dict[str, Any]] = {}

        # 3) valutazione incrementale: il portafoglio cresce con i pick accettati
        portfolio: List[Dict[str, Any]] = list(base_units)
        for idx in ordered:
            cand = candidates[idx]
            unit = by_index.get(idx)
            decision: Dict[str, Any] = {"decision": DECISION_ERROR,
                                        "multiplier": 1.0, "stake_before": None,
                                        "stake_after": None, "detail": ""}
            # Un candidato NON modellabile non e' bloccato (fail-open) ma non e'
            # nemmeno "clear": va CONTATO, altrimenti `kept + blocked` non torna
            # e l'errore sparirebbe dal contatore (`by_reason`) restando solo
            # nella lista degli scarti.
            if unit is None:
                decision["detail"] = "candidato_non_modellabile"
                decisions[idx] = decision
                info["errored"] = int(info["errored"]) + 1
                info["by_reason"][DECISION_ERROR] = int(
                    info["by_reason"].get(DECISION_ERROR, 0)) + 1
                continue
            prepared = unit_with_distribution(unit, cfg=conf)
            if prepared is None:
                decision["detail"] = "distribuzione_non_disponibile"
                decisions[idx] = decision
                info["errored"] = int(info["errored"]) + 1
                info["by_reason"][DECISION_ERROR] = int(
                    info["by_reason"].get(DECISION_ERROR, 0)) + 1
                continue
            prepared["candidate_index"] = idx
            stake_before = float(prepared.get("stake") or 0.0)
            decision["stake_before"] = stake_before
            decision["distribution"] = prepared["distribution"]["source"]

            # 3a. dipendenza massima con il portafoglio corrente
            d_max, d_weighted, worst = 0.0, 0.0, None
            for other in portfolio:
                dep = dependence(prepared, other, cfg=conf)
                if dep["dependence"] > d_max:
                    d_max, worst = dep["dependence"], (other, dep)
                d_weighted += dep["dependence"] * float(other.get("stake") or 0.0)
            total_other = sum(float(o.get("stake") or 0.0) for o in portfolio)
            if total_other > 0:
                d_weighted /= total_other
            m_corr, reason = multiplier_bands(d_max, cfg=conf)
            decision["dependence_max"] = d_max
            decision["dependence_weighted"] = d_weighted
            if worst is not None:
                decision["dependence_with"] = {
                    "key": worst[0].get("key"), "relation": worst[1]["relation"],
                    "rho": worst[1]["rho"], "detail": worst[1].get("detail") or ""}

            # 3b. cap di CLUSTER: quanto stake puo' stare in UN cluster a rischio.
            #     Il budget del cluster e' una quota del BANKROLL (non della somma
            #     delle puntate): misurato sulla somma, un portafoglio vuoto
            #     avrebbe budget zero e il primo pick verrebbe bloccato per
            #     concentrazione di se' stesso — un gate che non lascia passare
            #     NESSUN ordine. Sul bankroll il cap e' una soglia vera di
            #     concentrazione ("non oltre meta' del capitale su una lega").
            m_cluster, cluster_detail = 1.0, ""
            cluster = prepared.get("cluster") or ""
            bankroll_f = float(bankroll) if float(bankroll) > 0 else 0.0
            if cluster and float(conf["cluster_pct"]) < 1.0 and bankroll_f > 0:
                p = float(conf["cluster_pct"])
                budget = p * bankroll_f
                in_cluster = cluster_stake(portfolio, cluster)
                room = budget - in_cluster
                if room <= 0:
                    m_cluster = 0.0
                    cluster_detail = ("cluster '%s' gia' al cap (%.2f di %.2f "
                                      "USDC = %.0f%% bankroll)"
                                      % (cluster, in_cluster, budget, p * 100.0))
                else:
                    m_cluster = _clamp01(room / max(stake_before, 1e-9))
                    if m_cluster < 1.0:
                        cluster_detail = ("cluster '%s' oltre il %.0f%% del "
                                          "bankroll (%.2f + %.2f > %.2f USDC)"
                                          % (cluster, p * 100.0, in_cluster,
                                             stake_before, budget))

            # 3c. budget di coda sul portafoglio che INCLUDE il candidato
            m_tail, tail_detail = 1.0, ""
            sim_units = list(portfolio) + [prepared]
            corr, _pairs = dependence_matrix(sim_units, cfg=conf)
            sim = simulate(sim_units, corr, cfg=conf)
            if sim.get("ok"):
                base = sim["base"]
                cand_pl = sim["values"][-1][np.clip(sim["idx"][-1], 0,
                                                    len(sim["values"][-1]) - 1)]
                # P/L del portafoglio SENZA il candidato: la sua distribuzione
                # condivisa si sottrae, cosi' il budget di coda si misura sul
                # rischio AGGIUNTO (non due volte lo stesso candidato).
                sim_base = base - cand_pl
                budget = tail_budget_multiplier(sim_base, sim["values"][-1],
                                                sim["idx"][-1], bankroll=bankroll,
                                                cfg=conf)
                m_tail = _clamp01(budget.get("multiplier", 1.0))
                if budget.get("error"):
                    tail_detail = str(budget["error"])
                elif m_tail < 1.0:
                    # I numeri riportati sono quelli MISURATI a stake pieno: e'
                    # la coda che ha sfondato il budget, non quella del valore
                    # ridotto (che per costruzione sta dentro).
                    tail_detail = ("perdita di coda (q=%.0f%%) %.2f > budget "
                                   "%.0f%% di %.2f = %.2f USDC"
                                   % (100.0 * float(conf["dd_quantile"]),
                                      float(budget.get("loss_q") or 0.0),
                                      float(conf["dd_level"]) * 100.0,
                                      float(bankroll),
                                      float(conf["dd_level"]) * float(bankroll)))
            else:
                m_tail = 1.0
                tail_detail = str(sim.get("error") or "simulazione_non_disponibile")

            # 3d. il PIU' SEVERO dei tre vincoli (il modulo puo' solo ridurre)
            multiplier = _clamp01(min(m_corr, m_cluster, m_tail))
            if multiplier < float(conf["min_multiplier"]):
                # Il motivo e' il vincolo che MORDE (quello col moltiplicatore
                # piu' basso): "sotto il pavimento" senza dire QUALE vincolo
                # l'ha chiesto rende indistinguibile un cluster pieno da una
                # dipendenza moderata (e il `detail` lo dice, il motivo no).
                if reason == DECISION_BLOCKED_CRITICAL:
                    verdict = DECISION_BLOCKED_CRITICAL
                elif m_cluster <= m_tail and m_cluster <= m_corr:
                    verdict = DECISION_BLOCKED_CLUSTER
                elif m_tail <= m_corr:
                    verdict = DECISION_BLOCKED_TAIL
                else:
                    verdict = DECISION_BLOCKED_MULTIPLIER
                multiplier = 0.0
            elif multiplier < 1.0:
                verdict = DECISION_SCALED
            else:
                verdict = DECISION_CLEAR
            new_stake = math.floor(stake_before * multiplier * 100.0) / 100.0
            if multiplier > 0.0 and min_ticket > 0 and new_stake < min_ticket:
                verdict = DECISION_BLOCKED_TICKET
                multiplier = 0.0
                new_stake = 0.0
            decision.update({
                "decision": verdict, "multiplier": multiplier,
                "stake_after": new_stake,
                "m_corr": m_corr, "m_cluster": m_cluster, "m_tail": m_tail,
                "cluster_detail": cluster_detail, "tail_detail": tail_detail,
                "detail": cluster_detail or tail_detail or
                          (decision.get("dependence_with") or {}).get("relation", ""),
            })
            decisions[idx] = decision
            info["by_reason"][verdict] = int(info["by_reason"].get(verdict, 0)) + 1
            if verdict == DECISION_CLEAR:
                info["clear"] = int(info["clear"]) + 1
            elif verdict == DECISION_SCALED:
                info["scaled"] = int(info["scaled"]) + 1
            else:
                info["blocked"] = int(info["blocked"]) + 1
            if verdict in (DECISION_CLEAR, DECISION_SCALED):
                accepted = dict(prepared)
                accepted["stake"] = new_stake if verdict == DECISION_SCALED \
                    else stake_before
                portfolio.append(accepted)
                info["kept"] = int(info["kept"]) + 1

        # 4) applica: solo il campo `stake` cambia (mai un fattore > 1).
        #    Gli scarti si raccolgono anche in una lista, cosi' il chiamante puo'
        #    loggare OGNI blocco con il suo motivo (i candidati bloccati escono
        #    dalla lista: senza questa lista il perche' resterebbe solo in un
        #    contatore, cioe' indistinguibile fra i candidati).
        blocked_detail: List[Dict[str, Any]] = []
        for idx, decision in decisions.items():
            cand = candidates[idx]
            if not isinstance(cand, dict):
                continue
            cand["copula"] = decision
            if decision["stake_after"] is not None:
                cand["stake"] = decision["stake_after"]
            if decision["decision"] not in (DECISION_CLEAR, DECISION_SCALED):
                blocked_detail.append({
                    "match_id": cand.get("match_id"),
                    "esito": cand.get("esito_key") or cand.get("esito"),
                    "reason": decision["decision"],
                    "detail": decision.get("detail") or "",
                    "stake_before": decision.get("stake_before"),
                    "dependence_max": decision.get("dependence_max"),
                })
        info["blocked_detail"] = blocked_detail
        return out
    except Exception as e:
        logger.warning("copula_portfolio: valutazione non riuscita (%s): "
                       "candidati invariati (fail-open dichiarato)", e)
        info["error"] = "errore_valutazione"
        return out


def apply(candidates: list, *, bankroll: float,
          open_positions: Optional[Sequence[Dict[str, Any]]] = None,
          lambdas_lookup: Any = None,
          cfg: Optional[Dict[str, Any]] = None) -> Tuple[list, Dict[str, Any]]:
    """Entry point per il percorso ordini: (candidati, info) — mai eccezioni.

    `open_positions=None` significa "leggi dal ledger" (posizioni live aperte);
    una lista esplicita (anche vuota) la sostituisce — cosi' i test girano senza
    DB e la produzione include sempre cio' che e' vivo.
    """
    conf = cfg or current_config()
    if not conf["active"]:
        return list(candidates or []), {"enabled": bool(conf["enabled"]),
                                        "active": False, "mode": conf["mode"],
                                        "kept": 0, "scaled": 0, "blocked": 0,
                                        "by_reason": {}, "error": None}
    positions = open_positions
    read_error = None
    if positions is None:
        got = open_units()
        positions = got.get("units") or []
        read_error = got.get("error")
    try:
        result = evaluate(candidates, bankroll=bankroll, open_positions=positions,
                          lambdas_lookup=lambdas_lookup, cfg=conf)
    except Exception as e:                                    # pragma: no cover
        # `evaluate` e' gia' fail-safe per conto suo: questa e' la SECONDA
        # cintura, cosi' la promessa "mai eccezioni" di `apply` non dipende da
        # quel che c'e' dentro l'altra funzione.
        logger.warning("copula_portfolio: valutazione interrotta (%s): "
                       "candidati invariati", e)
        return list(candidates or []), {
            "enabled": bool(conf["enabled"]), "active": bool(conf["active"]),
            "mode": conf["mode"], "kept": 0, "scaled": 0, "blocked": 0,
            "by_reason": {}, "error": "errore_valutazione", "errored": 0}
    info = result["info"]
    if read_error:
        info["open_read_error"] = read_error
    kept = [c for c in result["candidates"]
            if isinstance(c, dict) and float(c.get("stake") or 0.0) > 0]
    info["dropped"] = len(result["candidates"]) - len(kept)
    return kept, info


# ---------------------------------------------------------------------------
# Report (diagnostica: cosa rischia il portafoglio ADESSO)
# ---------------------------------------------------------------------------
def snapshot(*, bankroll: Optional[float] = None, conn=None,
             db_path: Optional[str] = None,
             cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Istantanea della dipendenza fra le posizioni APERTE (sola lettura)."""
    conf = cfg or current_config()
    out: Dict[str, Any] = {"units": 0, "stake": 0.0, "pairs": [],
                           "joint": None, "config": conf, "error": None}
    got = open_units(conn=conn, db_path=db_path)
    out["error"] = got.get("error")
    units = got.get("units") or []
    out["units"] = len(units)
    out["stake"] = round(float(sum(float(u.get("stake") or 0.0) for u in units)), 4)
    out["sources"] = sorted({(u.get("distribution") or {}).get("source") or "?"
                             for u in units})
    if not units:
        return out
    corr, pairs = dependence_matrix(units, cfg=conf)
    seen = set()
    for i in range(len(units)):
        for j in range(i + 1, len(units)):
            info = pairs[i][j]
            key = tuple(sorted((units[i]["key"], units[j]["key"]))) + (info["relation"],)
            if key in seen:
                continue
            seen.add(key)
            out["pairs"].append({"a": units[i]["key"], "b": units[j]["key"],
                                 "relation": info["relation"], "rho": info["rho"],
                                 "dependence": info["dependence"]})
    if bankroll:
        sim = simulate(units, corr, cfg=conf)
        if sim.get("ok"):
            bankroll = float(bankroll)
            base = sim["base"]
            out["joint"] = {
                "bankroll": bankroll,
                "stake": round(float(sum(float(u.get("stake") or 0.0) for u in units)), 4),
                "p_loss": float((base < 0).mean()),
                "p_loss_over_dd": float((base <= -float(conf["dd_level"]) * bankroll).mean()),
                "p5": float(np.quantile(base, 0.05)),
                "p50": float(np.quantile(base, 0.50)),
                "p95": float(np.quantile(base, 0.95)),
                "worst": float(base.min()),
                "copula": sim.get("kind"),
            }
        else:
            out["joint"] = {"error": sim.get("error")}
    return out


def format_report(data: Dict[str, Any]) -> str:
    """Report testuale (Telegram-friendly)."""
    conf = data.get("config") or {}
    lines = ["🔗 DIPENDENZA DI PORTAFOGLIO (copula %s)"
             % conf.get("copula", copula_kind(cfg=conf) if conf else "?")]
    lines.append("• Bande: dipendenza <= %.2f invariata | fino a %.2f ridotta | "
                 "oltre BLOCCATA" % (conf.get("max_corr", 0.0),
                                     conf.get("critical_corr", 0.0)))
    lines.append("• Budget di coda: perdita a q=%.0f%% <= %.0f%% del bankroll "
                 "(min moltiplicatore %.2f, cluster <= %.0f%%)"
                 % (float(conf.get("dd_quantile") or 0) * 100,
                    float(conf.get("dd_level") or 0) * 100,
                    float(conf.get("min_multiplier") or 0),
                    float(conf.get("cluster_pct") or 0) * 100))
    lines.append("• Interruttore: %s (mode %s)"
                 % ("ON" if conf.get("enabled") else "OFF", conf.get("mode")))
    lines.append("• Posizioni APERTE: %d | stake %.2f | fonti %s"
                 % (data.get("units", 0), data.get("stake", 0.0),
                    ",".join(data.get("sources") or []) or "-"))
    for pair in data.get("pairs") or []:
        lines.append("   ρ(%s, %s) = %+.3f [%s]"
                     % (pair["a"], pair["b"], pair["dependence"], pair["relation"]))
    joint = data.get("joint") or {}
    if joint.get("error"):
        lines.append("• Coda non misurabile: %s" % joint["error"])
    elif joint:
        lines.append("• Coda: P(perdita) %.1f%% | P(perdita oltre %.0f%%) %.1f%% | "
                     "p5 %+.2f / p50 %+.2f / p95 %+.2f | peggiore %+.2f"
                     % (joint["p_loss"] * 100,
                        float(conf.get("dd_level") or 0) * 100,
                        joint["p_loss_over_dd"] * 100, joint["p5"], joint["p50"],
                        joint["p95"], joint["worst"]))
    if data.get("error"):
        lines.append("⚠️ errore di lettura: %s" % data["error"])
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Dipendenza fra scommesse simultanee (sola lettura)")
    parser.add_argument("--json", action="store_true", help="output JSON")
    parser.add_argument("--bankroll", type=float, default=None)
    parser.add_argument("--db", default=None,
                        help="path del ledger (default tracker.DB_PATH)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    data = snapshot(bankroll=args.bankroll, db_path=args.db)
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False, default=str))
    else:
        print(format_report(data))
    return 0 if not data.get("error") else 1


if __name__ == "__main__":
    raise SystemExit(main())
