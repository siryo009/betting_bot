"""Statistica del laboratorio: stime con intervalli, non numeri puntuali.

Il progetto ha gia' pagato due volte il prezzo di leggere un ROI puntuale come
verita' (22/09: -21,64% del 1X2 era per il 74% pre-11/09; 25/09: +21,21% dell'OU
portato da righe di un'era ritirata). Qui OGNI numero esce con il suo intervallo
e con il campione, e il bootstrap e' **a cluster** (per partita), perche' due
selezioni sullo stesso evento non sono indipendenti.
"""

from __future__ import annotations

import math
import random
from typing import Iterable, Optional, Sequence

import numpy as np

DEFAULT_BOOT = 4000
DEFAULT_SEED = 7
Z95 = 1.959963984540054


def roi(profits: Sequence[float]) -> float:
    """ROI per unita' di stake (stessa definizione del resto del progetto)."""
    vals = [float(p) for p in profits if p is not None]
    return float(np.mean(vals)) if vals else 0.0


def std(profits: Sequence[float]) -> float:
    vals = [float(p) for p in profits if p is not None]
    return float(np.std(vals, ddof=1)) if len(vals) > 1 else 0.0


def t_stat(profits: Sequence[float]) -> Optional[float]:
    vals = np.asarray([float(p) for p in profits if p is not None], dtype=float)
    if len(vals) < 3:
        return None
    sd = float(vals.std(ddof=1))
    if sd == 0:
        return None
    return float(vals.mean() / (sd / math.sqrt(len(vals))))


def bootstrap_mean_ci(values: Sequence[float], clusters: Optional[Sequence] = None,
                      n_boot: int = DEFAULT_BOOT, alpha: float = 0.05,
                      seed: int = DEFAULT_SEED) -> dict:
    """CI bootstrap (percentile) della media, con ricampionamento a CLUSTER.

    `clusters` allineato ai valori: se due righe appartengono alla stessa
    partita vengono ricampionate INSIEME, altrimenti l'intervallo sarebbe
    troppo stretto (le selezioni sullo stesso match sono correlate).
    """
    vals = np.asarray([float(v) for v in values if v is not None], dtype=float)
    if len(vals) == 0:
        return {"n": 0, "mean": None, "lo": None, "hi": None}
    if clusters is None or len(clusters) != len(vals):
        groups = [[v] for v in vals]
    else:
        buckets: dict = {}
        for c, v in zip(clusters, vals):
            buckets.setdefault(c, []).append(float(v))
        groups = list(buckets.values())
    rng = random.Random(seed)
    means = []
    for _ in range(int(n_boot)):
        pick = [groups[rng.randrange(len(groups))] for _ in range(len(groups))]
        flat = [x for g in pick for x in g]
        means.append(sum(flat) / len(flat))
    means.sort()
    lo = means[int((alpha / 2) * len(means))]
    hi = means[min(len(means) - 1, int((1 - alpha / 2) * len(means)))]
    return {"n": int(len(vals)), "mean": float(vals.mean()),
            "lo": float(lo), "hi": float(hi),
            "clusters": len(groups)}


def wilson_ci(wins: int, n: int, z: float = Z95) -> tuple:
    if n <= 0:
        return (0.0, 1.0)
    p = wins / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(max(0.0, p * (1 - p) / n + z * z / (4 * n * n))) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def max_drawdown(profits: Sequence[float]) -> float:
    """Drawdown massimo (in unita' di stake) di una serie flat, in ordine."""
    equity = 0.0
    peak = 0.0
    dd = 0.0
    for p in profits:
        equity += float(p)
        peak = max(peak, equity)
        dd = max(dd, peak - equity)
    return float(dd)


def required_bets(effect: float, sd: float, z: float = Z95) -> Optional[int]:
    """Quante bet servono per distinguere `effect` (ROI) da zero, 95%."""
    if effect == 0 or sd <= 0:
        return None
    return int(math.ceil((z * sd / effect) ** 2))


def min_detectable_effect(n: int, sd: float, z: float = Z95,
                          power: float = 0.8) -> Optional[float]:
    """L'edge minimo distinguibile da zero con `n` bet (potenza 80%).

    E' l'inverso di `required_bets`: a quota ~1.6 il sd del P/L per unita' e'
    ~0.8, quindi con 40 bet si distinguono solo edge enormi (>25%) e con 500
    si scende a ~7%. Serve a dire quanto un "non significativo" sia dovuto al
    campione invece che all'assenza di edge.
    """
    if n is None or n <= 0 or sd is None or sd <= 0:
        return None
    z_beta = {0.8: 0.8416212335729143, 0.9: 1.2815515655446004}.get(power, 0.8416212335729143)
    return float((z + z_beta) * sd / math.sqrt(n))


def summarize(profits: Iterable[float], clusters=None, odds=None,
              wins: Optional[int] = None, n: Optional[int] = None,
              n_boot: int = DEFAULT_BOOT, seed: int = DEFAULT_SEED) -> dict:
    """Pacchetto di metriche con intervalli, pronto per il JSON del report."""
    vals = [float(p) for p in profits if p is not None]
    odds_list = [float(o) for o in (odds or []) if o is not None]
    out = {
        "n": int(n if n is not None else len(vals)),
        "roi": roi(vals) if vals else None,
        "roi_ci95": bootstrap_mean_ci(vals, clusters, n_boot=n_boot, seed=seed) if vals else
                    {"n": 0, "mean": None, "lo": None, "hi": None},
        "sd": std(vals) if vals else None,
        "t": t_stat(vals) if vals else None,
        "max_drawdown": max_drawdown(vals) if vals else None,
    }
    if wins is not None and out["n"]:
        lo, hi = wilson_ci(int(wins), int(n or len(vals)))
        out["hit_rate"] = int(wins) / int(n or len(vals))
        out["hit_ci95"] = [lo, hi]
    if odds_list:
        out["avg_odds"] = float(np.mean(odds_list))
    if out["roi"] and out["sd"]:
        out["required_bets_for_2pct"] = required_bets(0.02, out["sd"])
    return out


def classify(summ: dict, min_n: int = 30) -> str:
    """Classificazione dichiarativa usata dal report.

    * NON VERIFICABILE: nessun campione valutabile.
    * DA VERIFICARE: campione sotto soglia o intervallo che include lo zero.
    * PROMETTENTE: n >= soglia, ROI > 0 e limite inferiore del CI > 0.
    * SCARTATO: n >= soglia e limite superiore del CI < 0.
    """
    if not summ or summ.get("n", 0) == 0:
        return "NON VERIFICABILE"
    if summ["n"] < min_n:
        return "DA VERIFICARE"
    lo = (summ.get("roi_ci95") or {}).get("lo")
    hi = (summ.get("roi_ci95") or {}).get("hi")
    roi_v = summ.get("roi")
    if lo is None or hi is None or roi_v is None:
        return "DA VERIFICARE"
    if lo > 0:
        return "PROMETTENTE"
    if hi < 0:
        return "SCARTATO"
    return "DA VERIFICARE"
