"""M3 — Rating BAYESIANI GERARCHICI (MAP) da `match_results`. FASE 1: MISURA.

Perche' esiste (QUANT_MODULES.md, roadmap del 14/10/2026): il modello di gol stima
i coefficienti di squadra da **medie osservate con uno shrink a mano** — in
`rating_engine` il prior e' `PRIOR_MATCHES = 6.0` partite *fisse* e ogni
coefficiente e' calcolato da SOLO i gol della squadra (l'avversario non entra
mai nella stima). Un rating bayesiano gerarchico stima invece TUTTI i
coefficienti INSIEME (attacco e difesa di ogni squadra, cosi' che la forza
dell'avversario entri nella stima) sotto un prior Gaussiano la cui larghezza
`sigma` viene **stimata dai dati** (empirical Bayes, metodo dei momenti): lo
shrinkage smette di essere un numero scelto a mano e diventa una conseguenza
della quantita' di informazione disponibile.

Questo modulo NON e' collegato alla produzione: nessun percorso d'ordine lo
importa (tripwire in `test_bayes_ratings.py`), nessuna scrittura sul ledger
(connessioni in sola lettura) e nessuna formula copiata da altri moduli:
- i match, i pesi time-decay e la metrica Brier arrivano da `dixon_coles`
  (`load_matches`, `brier_1x2`), cosi' il confronto col modello di produzione
  usa la STESSA definizione di errore;
- le medie di lega e la mappa squadra->lega arrivano da `poisson_engine`
  (tabelle di DATI: nessuna copia).

MODELLO. Poisson log-lineare con pesi temporali, la stessa forma che il
progetto usa gia' per i gol:

    log lam_h = log(avg_hg) + a_h + d_a
    log lam_a = log(avg_ag) + a_a + d_h

con `a_*` attacco in casa/trasferta e `d_*` difesa in casa/trasferta (log
spazio: 0 = squadra media, il che riproduce il profilo NEUTRO per le squadre
senza storico). L'identificabilita' e' fissata dal prior (che tira tutti i
coefficienti verso 0) piu' un ri-centraggio che lascia invariato ogni prodotto
`a + d` e quindi ogni lambda.

STIMA (MAP). Discesa di gradiente sul log-posterior con passi deterministici:
Poisson log-verosimiglianza pesata + prior N(0, sigma^2). Il gradiente e' la
forma chiusa del GLM Poisson (`grad = w * (gol - lam)`, piu' `-theta/sigma^2`
dal prior), quindi non serve nessuna libreria di ottimizzazione: numpy basta
(coerente con il resto dell'engine, che e' numpy-only).

EMPIRICAL BAYES (perche' `sigma` non e' a mano). Con il metodo dei momenti:
due giri "fit -> sigma^2 = max(media(theta^2) - media(var_theta), pavimento)
-> refit", dove `var_theta` e' l'inversa dell'informazione di Fisher osservata
(la diagonale dell'Hessiano: per il GLM Poisson e' `somma(w * lam) + 1/sigma^2`).
Un solo giro non basta perche' la stima di `var_theta` dipende da `sigma`
stesso; due giri convergono in pratica su questi campioni e la cosa e'
dichiarata nel report (`sigma_rounds`).

FAIL-CLOSED dove serve: campione sotto `BAYES_MIN_MATCHES`, `sigma` non
stimabile o match senza punteggio -> nessuna conclusione (mai un rating
inventato su 3 partite). FAIL-OPEN sulla sola telemetria di lettura: un DB
assente o corrotto produce un report con il motivo, non un'eccezione.

USO (diagnostica, zero crediti, zero ordini):
    venv/bin/python bayes_ratings.py [--json] [--days N] [--max N]
        [--folds N] [--save] [--db PATH]
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent

# --------------------------------------------------------------------------
# Configurazione (tutti i parametri sono env, letti a RUNTIME)
# --------------------------------------------------------------------------
DEFAULT_ITERATIONS = 60         # iterazioni di Fisher-scoring per ogni fit
DEFAULT_LR = 0.85               # ammortizzatore del passo (il passo e' gia'
                                # normalizzato dall'informazione osservata)
_CLIP_LOG = 3.0                 # coefficienti in [e^-3, e^3]: mai exp esplosi
DEFAULT_MIN_MATCHES = 200       # sotto questo campione NON si conclude nulla
DEFAULT_FOLDS = 3               # blocchi temporali del walk-forward
DEFAULT_MIN_FOLD = 20           # minimo di righe perche' un fold conti
DEFAULT_SIGMA = 0.35            # prior di partenza (log spazio)
DEFAULT_SIGMA_FLOOR = 0.05      # pavimento: mai uno shrink infinito
DEFAULT_SIGMA_CEIL = 2.0        # soffitto: mai un prior che non shrinka nulla
# Sigma SEME del primo stadio dell'empirical Bayes (≈ massima verosimiglianza).
# Deve essere LARGO: il metodo dei momenti presume che i coefficienti di stage 1
# siano MLE, e un prior stretto li comprime verso zero facendo collassare sigma
# al pavimento (misurato su 3000 match sintetici con spread vero 0.30: seme 1.0
# -> sigma 0.05; seme 5.0 -> sigma ~0.30, il valore vero). Con il seme largo la
# varianza di campionamento (1/informazione, NON quella a posteriori) e' l'unica
# cosa sottratta dalla varianza osservata.
DEFAULT_EB_SEED = 5.0


def _env_float(name: str, default: float, *, minimum: Optional[float] = None,
               maximum: Optional[float] = None) -> float:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        val = float(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning("bayes_ratings: %s=%r non numerico, uso il default %s",
                       name, raw, default)
        return default
    if not math.isfinite(val):
        return default
    if minimum is not None and val < minimum:
        return minimum
    if maximum is not None and val > maximum:
        return maximum
    return val


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        val = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        logger.warning("bayes_ratings: %s=%r non numerico, uso il default %s",
                       name, raw, default)
        return default
    return max(minimum, val)


def config() -> Dict[str, Any]:
    """Parametri effettivi (env a RUNTIME, default di codice)."""
    return {
        "iterations": _env_int("BAYES_ITERATIONS", DEFAULT_ITERATIONS, minimum=5),
        "lr": _env_float("BAYES_LR", DEFAULT_LR, minimum=1e-4, maximum=1.0),
        "min_matches": _env_int("BAYES_MIN_MATCHES", DEFAULT_MIN_MATCHES,
                                minimum=10),
        "folds": _env_int("BAYES_FOLDS", DEFAULT_FOLDS, minimum=1),
        "min_fold": _env_int("BAYES_MIN_FOLD", DEFAULT_MIN_FOLD, minimum=5),
        "sigma": _env_float("BAYES_SIGMA", DEFAULT_SIGMA, minimum=1e-3),
        "sigma_floor": _env_float("BAYES_SIGMA_FLOOR", DEFAULT_SIGMA_FLOOR,
                                  minimum=1e-3),
        "sigma_ceil": _env_float("BAYES_SIGMA_CEIL", DEFAULT_SIGMA_CEIL,
                                 minimum=1e-3),
        "eb_seed": _env_float("BAYES_EB_SEED", DEFAULT_EB_SEED, minimum=1e-3),
        "days": _env_int("BAYES_DAYS", 0, minimum=0) or None,
        "max_matches": _env_int("BAYES_MAX_MATCHES", 0, minimum=0) or None,
    }


def state_path() -> Path:
    """Dove vive il fit salvato (env `BAYES_STATE`, default `DATA_DIR/bayes_ratings.json`)."""
    raw = (os.getenv("BAYES_STATE") or "").strip()
    if raw:
        return Path(raw)
    try:
        from config import DATA_DIR
    except Exception:                                          # pragma: no cover
        return ROOT / "data" / "bayes_ratings.json"
    return Path(DATA_DIR) / "bayes_ratings.json"


def _np():
    """numpy pigro: importare il modulo non paga numpy (tripwire)."""
    import numpy as np
    return np


# --------------------------------------------------------------------------
# 1) Dati: i match arrivano da dixon_coles (stessa lettura, stessi pesi)
# --------------------------------------------------------------------------
def load_matches(*, days: Optional[int] = None, max_matches: Optional[int] = None,
                 conn: Optional[sqlite3.Connection] = None,
                 now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Match conclusi con punteggio e peso time-decay.

    Delega a `dixon_coles.load_matches` (connessione in SOLA LETTURA, stessa
    finestra e stesso half-life del modello di gol): il campione non e'
    "ricostruito" qui, cosi' i due modelli si misurano sugli stessi dati.
    """
    try:
        import dixon_coles as dc
    except Exception as e:
        logger.warning("bayes_ratings: dixon_coles non importabile (%s)", e)
        return []
    return list(dc.load_matches(days=days, max_matches=max_matches,
                                conn=conn, now=now) or [])


def _canonical(name: Any) -> str:
    """Nome canonico del DB per `name` (stessa delega di `rating_engine`).

    Fail-safe: qualunque errore o dubbio -> nome originale (mai un rating
    attribuito a un'altra squadra).
    """
    if not name:
        return str(name or "")
    try:
        from team_names import resolve_team
        return resolve_team(str(name)) or str(name)
    except Exception:
        return str(name)


def _league_avg(league: Any) -> Tuple[float, float]:
    """(gol medi casa, gol medi trasferta) della lega — DATI, non una formula.

    Le tabelle vivono in `poisson_engine` (`LEAGUE_AVGS`); per una lega
    sconosciuta (coppe, campionati non in tabella) vale il profilo globale
    1.50/1.30, esattamente come fa il modello di produzione quando le due
    squadre non condividono una lega nota.
    """
    try:
        import poisson_engine as pe
        avg = pe.LEAGUE_AVGS.get(str(league or ""))
        if avg:
            return float(avg[0]), float(avg[1])
    except Exception:
        pass
    return 1.50, 1.30


def neutral_expected_goals(home: Any, away: Any,
                           *, league: Optional[str] = None) -> Tuple[float, float]:
    """Baseline NEUTRA: solo le medie di lega, nessun rating.

    E' il terzo termine di paragone della misura (l'"ignoranza"): se ne'
    `rating_engine` ne' il rating bayesiano battono questa baseline, il modello
    di gol non sta aggiungendo nulla — un dato che va visto, non nascosto.
    """
    if league is None:
        try:
            import poisson_engine as pe
            hl, al = pe._find_team_league(_canonical(home)), pe._find_team_league(_canonical(away))
            league = hl if hl == al else None
        except Exception:
            league = None
    return _league_avg(league)


# --------------------------------------------------------------------------
# 2) Il fit MAP (numpy-only, deterministico)
# --------------------------------------------------------------------------
def _accum(n: int, idx, values):
    """Somma `values` per indice (equivalente numpy di np.add.at, una volta sola)."""
    np = _np()
    out = np.zeros(n)
    np.add.at(out, idx, values)
    return out


def _index_teams(matches: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    idx: Dict[str, int] = {}
    for m in matches or []:
        for key in ("home", "away"):
            name = _canonical(m.get(key))
            if name and name not in idx:
                idx[name] = len(idx)
    return idx


def _empty_params() -> Dict[str, Any]:
    return {"teams": {}, "sigma": None, "sigma_rounds": 0, "n_matches": 0,
            "iterations": 0, "logpost": None}


def fit(matches: Sequence[Dict[str, Any]], *, sigma: Optional[float] = None,
        iterations: Optional[int] = None, lr: Optional[float] = None,
        empirical_bayes: bool = True) -> Dict[str, Any]:
    """Stima MAP dei coefficienti di squadra (attacco/difesa, casa/trasferta).

    Ritorna un dict serializzabile:
      `teams` — {nome: {attack_home, attack_away, defense_home, defense_away,
                        n_home, n_away}} con i coefficienti in scala
                MOLTIPLICATIVA (1.0 = squadra media, la convenzione con cui
                `poisson_engine` compone i lambda);
      `sigma` — larghezza del prior stimata (None se non stimabile);
      `n_matches`, `iterations`, `logpost`, `sigma_rounds`.

    Non solleva mai: un campione vuoto o degenere ritorna `_empty_params()`.
    """
    conf = config()
    iterations = int(iterations if iterations is not None else conf["iterations"])
    lr = float(lr if lr is not None else conf["lr"])
    rows = []
    for m in matches or []:
        try:
            sh, sa = int(m.get("score_home")), int(m.get("score_away"))
        except (TypeError, ValueError):
            continue
        home, away = _canonical(m.get("home")), _canonical(m.get("away"))
        if not home or not away or sh < 0 or sa < 0:
            continue
        w = float(m.get("weight") or 0.0)
        if not math.isfinite(w) or w <= 0:
            continue
        rows.append((home, away, sh, sa, w, _league_avg(m.get("league"))))
    if not rows:
        return _empty_params()

    idx = _index_teams([{"home": r[0], "away": r[1]} for r in rows])
    n = len(idx)
    if n < 2:
        return _empty_params()

    sigma0 = max(float(sigma if sigma is not None else conf["sigma"]),
                 conf["sigma_floor"])

    rounds = 1
    if sigma is not None:
        # sigma imposto dal chiamante: nessuna stima, il fit e' deterministico
        core = _fit_core(rows, idx, sigma=sigma0, iterations=iterations, lr=lr)
        final = sigma0
    elif not empirical_bayes:
        core = _fit_core(rows, idx, sigma=sigma0, iterations=iterations, lr=lr)
        final = sigma0
    else:
        # STADIO 1: fit con un prior LARGO (≈ massima verosimiglianza)
        seed = max(sigma0, conf["eb_seed"])
        core = _fit_core(rows, idx, sigma=seed, iterations=iterations, lr=lr)
        # STADIO 2: sigma dal metodo dei momenti, poi refit con quella larghezza
        est = _estimate_sigma(core, floor=conf["sigma_floor"],
                              ceiling=conf["sigma_ceil"])
        if est is None:
            final = sigma0
        else:
            core = _fit_core(rows, idx, sigma=est, iterations=iterations, lr=lr)
            final = est
            rounds = 2
    core["sigma"] = float(final)
    core["sigma_rounds"] = rounds
    core["n_matches"] = len(rows)
    core["iterations"] = iterations
    return core


def _fit_core(rows, idx, *, sigma: float, iterations: int, lr: float) -> Dict[str, Any]:
    """Iterazioni di Fisher-scoring DIAGONALE sul log-posterior (Poisson + prior).

    Gradiente in forma chiusa del GLM Poisson: per ogni match il residuo
    `w * (gol - lam)` alimenta l'attacco di chi segna e la difesa di chi
    subisce; il prior contribuisce `-theta / sigma^2` (che e' anche cio' che
    tiene le squadre senza partite esattamente a 1.0 = profilo neutro).

    ⚠️ Il passo NON e' un gradiente puro scalato dal learning rate: la somma
    dei residui cresce con il numero di match, quindi con un passo costante il
    fit **diverge** (osservato: `OverflowError` su 700 match). Si divide per
    l'INFORMAZIONE osservata della coordinata (la diagonale dell'Hessiano,
    `somma(w * lam) + 1/sigma^2`): il passo diventa invariante di scala
    rispetto al campione, converge in poche iterazioni anche da coefficienti
    nulli e non ha bisogno di tuning. `lr` resta come ammortizzatore (< 1) e i
    coefficienti sono limitati a ±`_CLIP_LOG` per non far esplodere `exp` su
    dati patologici.
    """
    np = _np()
    n = len(idx)
    att_h = np.zeros(n)
    att_a = np.zeros(n)
    def_h = np.zeros(n)
    def_a = np.zeros(n)
    h_idx = np.array([idx[r[0]] for r in rows], dtype=np.int64)
    a_idx = np.array([idx[r[1]] for r in rows], dtype=np.int64)
    sh = np.array([float(r[2]) for r in rows])
    sa = np.array([float(r[3]) for r in rows])
    w = np.array([float(r[4]) for r in rows])
    avg_h = np.array([float(r[5][0]) for r in rows])
    avg_a = np.array([float(r[5][1]) for r in rows])
    log_h = np.log(np.maximum(avg_h, 1e-9))
    log_a = np.log(np.maximum(avg_a, 1e-9))
    inv_s2 = 1.0 / max(sigma * sigma, 1e-12)
    lam_h = np.zeros_like(w)
    lam_a = np.zeros_like(w)
    inf_att_h = inf_att_a = inf_def_h = inf_def_a = np.zeros(n)
    for _it in range(max(1, iterations)):
        lam_h = np.exp(np.clip(log_h + att_h[h_idx] + def_a[a_idx],
                               -_CLIP_LOG, _CLIP_LOG))
        lam_a = np.exp(np.clip(log_a + att_a[a_idx] + def_h[h_idx],
                               -_CLIP_LOG, _CLIP_LOG))
        gh = w * (sh - lam_h)
        ga = w * (sa - lam_a)
        # informazione osservata per coordinata (diagonale dell'Hessiano)
        inf_att_h = _accum(n, h_idx, w * lam_h)
        inf_def_a = _accum(n, a_idx, w * lam_h)
        inf_att_a = _accum(n, a_idx, w * lam_a)
        inf_def_h = _accum(n, h_idx, w * lam_a)
        grad_att_h = _accum(n, h_idx, gh)
        grad_def_a = _accum(n, a_idx, gh)
        grad_att_a = _accum(n, a_idx, ga)
        grad_def_h = _accum(n, h_idx, ga)
        att_h = np.clip(att_h + lr * (grad_att_h - att_h * inv_s2) /
                        (inf_att_h + inv_s2), -_CLIP_LOG, _CLIP_LOG)
        def_a = np.clip(def_a + lr * (grad_def_a - def_a * inv_s2) /
                        (inf_def_a + inv_s2), -_CLIP_LOG, _CLIP_LOG)
        att_a = np.clip(att_a + lr * (grad_att_a - att_a * inv_s2) /
                        (inf_att_a + inv_s2), -_CLIP_LOG, _CLIP_LOG)
        def_h = np.clip(def_h + lr * (grad_def_h - def_h * inv_s2) /
                        (inf_def_h + inv_s2), -_CLIP_LOG, _CLIP_LOG)
    lam_h = np.exp(np.clip(log_h + att_h[h_idx] + def_a[a_idx],
                           -_CLIP_LOG, _CLIP_LOG))
    lam_a = np.exp(np.clip(log_a + att_a[a_idx] + def_h[h_idx],
                           -_CLIP_LOG, _CLIP_LOG))
    # log-posterior completo (verosimiglianza + prior), come diagnostica
    ll = float(np.sum(w * (sh * np.log(np.maximum(lam_h, 1e-12)) - lam_h)))
    ll += float(np.sum(w * (sa * np.log(np.maximum(lam_a, 1e-12)) - lam_a)))
    prior = -0.5 * inv_s2 * float(np.sum(att_h ** 2) + np.sum(att_a ** 2)
                                  + np.sum(def_h ** 2) + np.sum(def_a ** 2))
    logpost = ll + prior
    # ri-centraggio: aggiungo k agli attacchi e lo tolgo alle difese => ogni
    # prodotto a+d (e quindi ogni lambda) resta invariato, ma i coefficienti
    # diventano confrontabili in scala.
    k = float((att_h.mean() + att_a.mean() - def_h.mean() - def_a.mean()) / 2.0)
    att_h = att_h + k
    att_a = att_a + k
    def_h = def_h - k
    def_a = def_a - k

    teams: Dict[str, Any] = {}
    names = [None] * n
    for name, i in idx.items():
        names[i] = name
    n_home = np.zeros(n)
    n_away = np.zeros(n)
    np.add.at(n_home, h_idx, 1)
    np.add.at(n_away, a_idx, 1)
    for i, name in enumerate(names):
        if name is None:                                       # pragma: no cover
            continue
        teams[name] = {
            "attack_home": float(math.exp(att_h[i])),
            "attack_away": float(math.exp(att_a[i])),
            "defense_home": float(math.exp(def_h[i])),
            "defense_away": float(math.exp(def_a[i])),
            "n_home": int(n_home[i]),
            "n_away": int(n_away[i]),
        }
    # coefficienti e informazione OSSERVATA per ruolo (empirical Bayes: la
    # varianza da sottrarre e' quella del MLE, `1/informazione`)
    coef: Dict[str, Dict[str, Tuple[float, float]]] = {
        "att_h": {}, "att_a": {}, "def_h": {}, "def_a": {}}
    for i, name in enumerate(names):
        if name is None:                                       # pragma: no cover
            continue
        coef["att_h"][name] = (float(att_h[i]), float(inf_att_h[i]))
        coef["att_a"][name] = (float(att_a[i]), float(inf_att_a[i]))
        coef["def_h"][name] = (float(def_h[i]), float(inf_def_h[i]))
        coef["def_a"][name] = (float(def_a[i]), float(inf_def_a[i]))
    return {"teams": teams, "sigma": float(sigma), "logpost": logpost,
            "_coef": coef}


def _estimate_sigma(core: Dict[str, Any], *, floor: float,
                    ceiling: float = DEFAULT_SIGMA_CEIL) -> Optional[float]:
    """Empirical Bayes (metodo dei momenti): sigma^2 = E[theta^2] - E[var_MLE].

    Formula classica del prior normale gerarchico: la varianza OSSERVATA dei
    coefficienti contiene anche la loro varianza di CAMPIONAMENTO, che va
    sottratta. La varianza da sottrarre e' `1/informazione` (la varianza del
    MLE), **non** quella a posteriori: mescolare le due sottostima sigma e
    schiaccia i coefficienti (l'errore trovato al primo giro su 120 match).

    Se il risultato e' negativo (nessuna eterogeneita' rilevabile: le squadre
    sono indistinguibili ai dati disponibili) vale il PAVIMENTO, non zero — un
    pavimento tiene il modello identificabile e il prior a un ruolo minimo ma
    non nullo. Il risultato e' sempre limitato a [floor, ceiling].
    """
    coef = (core.get("_coef") or {})
    if not coef:
        return None
    ests: List[float] = []
    for _role, entries in coef.items():
        tot = 0.0
        tot_var = 0.0
        cnt = 0
        for _name, (theta, info) in (entries or {}).items():
            if not (info and info > 0):
                continue                      # nessuna partita in quel ruolo
            tot += float(theta) ** 2
            tot_var += 1.0 / float(info)      # varianza del MLE
            cnt += 1
        if cnt:
            ests.append(tot / cnt - tot_var / cnt)
    if not ests:
        return None
    est = sum(ests) / len(ests)
    if not math.isfinite(est):
        return None
    return min(max(math.sqrt(max(est, 0.0)), floor), max(ceiling, floor))


# --------------------------------------------------------------------------
# 3) Lambdas del modello bayesiano (fallback neutro per squadre senza storico)
# --------------------------------------------------------------------------
def expected_goals(home: Any, away: Any, *, params: Optional[Dict[str, Any]] = None,
                   league: Optional[str] = None,
                   state: Optional[Path] = None) -> Tuple[float, float]:
    """(lam casa, lam trasferta) dal fit bayesiano.

    Squadra con storico -> coefficiente stimato; squadra SENZA storico ->
    coefficiente 1.0, cioe' il profilo neutro di lega (la stessa scelta che fa
    `poisson_engine` quando `rating_engine` non conosce la squadra: mai un
    rating inventato). Non solleva mai: qualunque problema ricade sulla
    baseline neutra.
    """
    try:
        p = params if params is not None else load_fit(path=state).get("params") or {}
        teams = (p or {}).get("teams") or {}
        avg_h, avg_a = neutral_expected_goals(home, away, league=league)
        h = teams.get(_canonical(home)) or {}
        a = teams.get(_canonical(away)) or {}
        lam_h = avg_h * float(h.get("attack_home", 1.0)) * float(a.get("defense_away", 1.0))
        lam_a = avg_a * float(a.get("attack_away", 1.0)) * float(h.get("defense_home", 1.0))
        if not (math.isfinite(lam_h) and math.isfinite(lam_a)) or lam_h <= 0 or lam_a <= 0:
            return avg_h, avg_a
        return lam_h, lam_a
    except Exception as e:                                     # pragma: no cover
        logger.warning("bayes_ratings: expected_goals in errore (%s)", e)
        try:
            return neutral_expected_goals(home, away, league=league)
        except Exception:
            return 1.50, 1.30


def expected_goals_fn(params: Dict[str, Any]):
    """Adapter per `dixon_coles.brier_1x2(expected_goals_fn=...)`."""
    def _fn(home, away):
        return expected_goals(home, away, params=params)
    return _fn


# --------------------------------------------------------------------------
# 4) Walk-forward: chi batte chi, senza look-ahead
# --------------------------------------------------------------------------
def _ordered_by_time(matches: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Match dal PIU' RECENTE al piu' vecchio (stessa convenzione di dixon_coles)."""
    return sorted(matches or [], key=lambda m: float(m.get("days_ago") or 0.0))


def _folds(ordered: Sequence[Dict[str, Any]], n_folds: int,
           min_fold: int) -> List[Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]]:
    """Blocchi temporali contigui: il fold TEST e' SEMPRE piu' recente del TRAIN.

    Senza questa direzione il confronto conterrebbe look-ahead (il modello
    addestrato sul futuro) — l'errore che falsa qualunque backtest. `ordered` e'
    dal piu' recente: il test e' la testa di ogni blocco, il train tutto cio' che
    viene DOPO nel tempo (cioe' piu' vecchio).
    """
    total = len(ordered)
    if total < 2:
        return []
    n_folds = max(1, min(int(n_folds), 6))
    # con campioni piccoli si riduce il NUMERO di fold invece di rinunciare alla
    # misura: un fold da 12 righe non conclude nulla, ma un solo blocco da 60 si'
    while n_folds > 1 and total // (n_folds + 1) < min_fold:
        n_folds -= 1
    size = total // (n_folds + 1)
    if size < min_fold:
        return []
    out = []
    for i in range(n_folds):
        start = i * size
        end = start + size
        test = list(ordered[start:end])
        train = list(ordered[end:])
        if len(test) < min_fold or len(train) < min_fold:
            continue
        out.append((train, test))
    return out


def _brier(test: Sequence[Dict[str, Any]], expected_goals_fn=None) -> Optional[float]:
    """Brier 1X2 pesato — STESSA metrica del modello di produzione.

    Delega a `dixon_coles.brier_1x2`, che usa `poisson_engine.prob_1x2` col rho
    del motore: cosi' le due stime si confrontano a parita' di correzione
    Dixon-Coles (nessuna formula ricopiata qui).
    """
    try:
        import dixon_coles as dc
        import poisson_engine as pe
        return dc.brier_1x2(list(test), float(pe.current_rho()),
                            expected_goals_fn=expected_goals_fn)
    except Exception as e:
        logger.warning("bayes_ratings: brier non calcolabile (%s)", e)
        return None


def walk_forward(matches: Optional[Sequence[Dict[str, Any]]] = None, *,
                 folds: Optional[int] = None, min_matches: Optional[int] = None,
                 days: Optional[int] = None, max_matches: Optional[int] = None,
                 conn: Optional[sqlite3.Connection] = None,
                 now: Optional[datetime] = None) -> Dict[str, Any]:
    """Confronta bayesiano / rating di produzione / baseline neutra, fuori campione.

    Un verdetto onesto richiede TRE numeri, non due: il modello bayesiano puo'
    battere il rating di produzione e restare peggiore dell'ignoranza (medie di
    lega) — in quel caso il valore aggiunto non c'e'.
    """
    conf = config()
    folds_req = int(folds if folds is not None else conf["folds"])
    min_matches = int(min_matches if min_matches is not None else conf["min_matches"])
    data = list(matches) if matches is not None else load_matches(
        days=days if days is not None else conf["days"],
        max_matches=max_matches if max_matches is not None else conf["max_matches"],
        conn=conn, now=now)
    ordered = _ordered_by_time(data)
    out: Dict[str, Any] = {
        "ok": False, "reason": None, "n_matches": len(ordered), "n_folds": 0,
        "folds": [], "brier_bayes": None, "brier_rating": None,
        "brier_neutral": None, "gain_vs_rating": None, "gain_vs_neutral": None,
        "sigma": None, "verdict": "insufficient", "min_matches": min_matches,
    }
    if len(ordered) < min_matches:
        out["reason"] = "campione_insufficiente"
        return out
    blocks = _folds(ordered, folds_req, conf["min_fold"])
    if not blocks:
        out["reason"] = "fold_insufficienti"
        return out

    import poisson_engine as pe  # baseline di produzione (rating_engine via expected_goals)

    per_fold = []
    b_bayes: List[float] = []
    b_rating: List[float] = []
    b_neutral: List[float] = []
    sigmas: List[float] = []
    for i, (train, test) in enumerate(blocks):
        params = fit(train)
        if not params.get("teams"):
            continue
        sigmas.append(float(params.get("sigma") or 0.0))
        bb = _brier(test, expected_goals_fn=expected_goals_fn(params))
        br = _brier(test, expected_goals_fn=pe.expected_goals)
        bn = _brier(test, expected_goals_fn=lambda h, a: neutral_expected_goals(h, a))
        entry = {"fold": i, "n_train": len(train), "n_test": len(test),
                 "brier_bayes": bb, "brier_rating": br, "brier_neutral": bn,
                 "sigma": params.get("sigma")}
        if bb is not None and br is not None and bn is not None:
            entry["gain_vs_rating"] = br - bb
            entry["gain_vs_neutral"] = bn - bb
            b_bayes.append(bb)
            b_rating.append(br)
            b_neutral.append(bn)
        per_fold.append(entry)

    out["folds"] = per_fold
    out["n_folds"] = len(b_bayes)
    if not b_bayes:
        out["reason"] = "brier_non_calcolabile"
        return out
    mean = lambda xs: sum(xs) / len(xs)
    out["brier_bayes"] = mean(b_bayes)
    out["brier_rating"] = mean(b_rating)
    out["brier_neutral"] = mean(b_neutral)
    out["gain_vs_rating"] = out["brier_rating"] - out["brier_bayes"]
    out["gain_vs_neutral"] = out["brier_neutral"] - out["brier_bayes"]
    out["sigma"] = mean(sigmas) if sigmas else None
    out["ok"] = True
    # Verdetto: il guadagno deve essere POSITIVO su ENTRAMBE le baseline, non
    # solo su quella di produzione (vedi docstring).
    if out["gain_vs_rating"] > 0 and out["gain_vs_neutral"] > 0:
        out["verdict"] = "bayes_better"
    elif out["gain_vs_rating"] > 0:
        out["verdict"] = "batte_rating_ma_non_la_baseline_neutra"
    else:
        out["verdict"] = "rating_engine_migliore"
    out["reason"] = "ok"
    # robustezza: quanti fold vanno nella stessa direzione (un guadagno medio
    # positivo trainato da un solo fold e' rumore, non evidenza)
    wins = sum(1 for f in per_fold if (f.get("gain_vs_rating") or 0) > 0)
    out["folds_a_favore"] = wins
    return out


# --------------------------------------------------------------------------
# 5) Persistenza del fit (opt-in, atomica, fail-safe)
# --------------------------------------------------------------------------
def save_fit(out: Dict[str, Any], *, path: Optional[Path] = None) -> Dict[str, Any]:
    """Scrive il fit su disco in modo atomico. Mai eccezioni (ritorna l'esito)."""
    p = Path(path) if path is not None else state_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {"saved_at": datetime.now().isoformat(timespec="seconds"),
                   "params": out}
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)
        return {"ok": True, "path": str(p)}
    except Exception as e:
        logger.warning("bayes_ratings: salvataggio non riuscito (%s)", e)
        return {"ok": False, "error": str(e), "path": str(p)}


def load_fit(*, path: Optional[Path] = None) -> Dict[str, Any]:
    """Legge il fit salvato. File assente o corrotto -> {} (nessuna eccezione)."""
    p = Path(path) if path is not None else state_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


# --------------------------------------------------------------------------
# 6) Report e CLI
# --------------------------------------------------------------------------
def format_report(res: Dict[str, Any], *, params: Optional[Dict[str, Any]] = None) -> str:
    """Report testuale (Telegram-friendly) del confronto walk-forward."""
    lines = ["🧠 Rating bayesiani gerarchici (M3) — misura walk-forward"]
    n_folds = res.get("n_folds") or 0
    if not res.get("ok"):
        lines.append(f"❌ nessuna conclusione: {res.get('reason')} "
                     f"(match {res.get('n_matches')}, minimo {res.get('min_matches')})")
        return "\n".join(lines)
    lines.append(f"Campione: {res['n_matches']} match chiusi | fold utili: {n_folds}")
    lines.append("• Brier 1X2 fuori campione (pesato, come il modello di produzione)")
    lines.append(f"  - bayesiano      : {res['brier_bayes']:.4f}")
    lines.append(f"  - rating attuale : {res['brier_rating']:.4f}")
    lines.append(f"  - baseline neutra: {res['brier_neutral']:.4f}")
    lines.append(f"• Guadagno vs rating attuale : {res['gain_vs_rating']:+.4f} "
                 f"({'migliore' if res['gain_vs_rating'] > 0 else 'peggiore'})")
    lines.append(f"• Guadagno vs neutra         : {res['gain_vs_neutral']:+.4f}")
    lines.append(f"• sigma stimato (empirical Bayes): {res['sigma']:.3f} "
                 f"| fold a favore: {res.get('folds_a_favore', 0)}/{n_folds}")
    lines.append(f"Verdetto: {res['verdict']}")
    if params:
        teams = params.get("teams") or {}
        top = sorted(teams.items(),
                     key=lambda kv: -(kv[1].get('attack_home', 1.0)))
        lines.append(f"Squadre stimate: {len(teams)} (attacco casa piu' alto: "
                     f"{top[0][0]} {top[0][1]['attack_home']:.3f})" if top else "")
    return "\n".join([ln for ln in lines if ln])


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Rating bayesiani gerarchici (misura)")
    ap.add_argument("--json", action="store_true", help="output JSON")
    ap.add_argument("--days", type=int, default=None, help="finestra in giorni")
    ap.add_argument("--max", type=int, default=None, help="max match letti")
    ap.add_argument("--folds", type=int, default=None)
    ap.add_argument("--save", action="store_true", help="salva il fit su BAYES_STATE")
    ap.add_argument("--db", default=None, help="DB alternativo (diagnostica)")
    args = ap.parse_args(argv)

    conn = None
    if args.db:
        try:
            conn = sqlite3.connect("file:%s?mode=ro" % args.db, uri=True)
        except Exception as e:
            print(f"DB non leggibile: {e}")
            return 2
    try:
        res = walk_forward(folds=args.folds, days=args.days,
                           max_matches=args.max, conn=conn)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    if args.save and res.get("ok"):
        data = load_matches(days=args.days, max_matches=args.max)
        fitres = fit(data)
        save = save_fit(fitres)
        res["saved"] = save
    if args.json:
        print(json.dumps(res, ensure_ascii=False, default=str))
    else:
        print(format_report(res))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":                                     # pragma: no cover
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sys.exit(main())
