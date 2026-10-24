"""Caccia all'edge: separare una REGOLA da una FETTA FORTUNATA.

Il 20/10/2026 il laboratorio ha trovato che il 1X2 di produzione, con un
**tetto sull'EV** (EV atteso fra 1,5% e 6%), passa da +2,63% a +21,37% di ROI
sulle stesse 149 righe. Quel numero e' pericoloso proprio perche' e' bello: e'
nato **guardando i bucket di calibrazione**, cioe' e' una fetta scelta DOPO aver
visto i dati. Il progetto ha gia' pagato due volte questa classe di errore
(22/09: -21,64% del 1X2 era per il 74% pre-11/09; 25/09: +21,21% dell'OU
portato da 22 righe di una pipeline ritirata).

Questo modulo risponde a UNA domanda sola: **il tetto EV e' una regola che
regge fuori dal campione in cui l'ho vista, o e' rumore?** Cinque prove
indipendenti, ognuna delle quali puo' bocciarlo da sola:

1. **MONOTONIA** — se l'EV del modello e' informativo, il ROI deve DECRESCERE
   per fasce crescenti di EV. Una relazione monotona su tutte le fasce e'
   difficile da produrre per caso; una singola fascia buona no. Misura
   Spearman(EV, profitto) **a cluster** (per partita).
2. **WALK-FORWARD** — il tetto si sceglie SOLO sulle righe precedenti e si
   applica al giorno dopo. E' l'unico numero che descrive cosa sarebbe
   successo in produzione; il 70/30 in-sample non basta perche' il tetto e'
   stato ispezionato.
3. **SPLIT D'ERA** — la strategia e' cambiata l'11/09 (favoriti netti) e la
   fascia quota il 14/10. Se il vantaggio vive solo in un'era, non e' del
   modello: e' del periodo.
4. **DENARO REALE** — le 80 puntate live hanno un EV registrato al momento
   dell'ordine: il tetto deve migliorare anche QUELLE, altrimenti sta
   descrivendo le previsioni e non gli ordini.
5. **TEST MULTIPLI** — quante ipotesi sono state provate in questa sessione?
   Il CI va letto con Bonferroni: con 60 tentativi, un p<0,05 "vince" anche
   su rumore puro (circa 3 volte su 60).

Regole del laboratorio rispettate: sola LETTURA su snapshot, zero rete, zero
crediti, zero ordini, soglie di produzione IMPORTATE da `value_filter` (mai
ricopiate), nessuna funzione guarda l'esito per filtrare la selezione.
"""

from __future__ import annotations

import argparse
import json
import math
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

import numpy as np
import pandas as pd

from . import loader, stats
from .backtest import _date, evaluate
from .strategies import PROD_EDGE_MIN, PROD_EV_MIN, PROD_ODDS_MAX, PROD_ODDS_MIN

# --------------------------------------------------------------------------- #
# Ipotesi testate in questa sessione (denominatore di Bonferroni).
# Elencate UNA per UNA: se domani se ne aggiunge una, il test lo pretende.
# --------------------------------------------------------------------------- #
HYPOTHESES = (
    # 9 strategie del laboratorio (run_lab)
    "strategy:value_1x2",
    "strategy:value_tight_1x2",
    "strategy:value_capped_ev",
    "strategy:value_capped_ev_tight",
    "strategy:market_only_favourite",
    "strategy:ou_gate",
    "strategy:ah_gate",
    "strategy:tennis_band",
    "strategy:contrarian_ou_rejected",
    # 12 parametri della griglia (odds_max x edge x EV)
    "grid:odds_max2:edge3:ev2",
    # 6 celle dello scan ex-ante
    "cell:scan",
    # 9 tetti EV di questa caccia
    "cap:0.04", "cap:0.05", "cap:0.06", "cap:0.08", "cap:0.10",
    "cap:0.12", "cap:0.15", "cap:0.20", "cap:none",
    # 4 fasce EV (monotonia) + 2 tagli d'era
    "bucket:ev", "era:11/09", "era:14/10",
    # spazzatura dichiarata: le altre combinazioni della griglia contano
    "grid:rest",
)
N_TESTS = 60  # = 9 + 12 + 6 + 9 + 4 + 2 + 18 residui dichiarati

CAP_GRID: tuple = (0.04, 0.05, 0.06, 0.08, 0.10, 0.12, 0.15, 0.20, None)
EV_BUCKETS: tuple = (None, 0.02, 0.03, 0.04, 0.06, 0.10, 0.20, None)
ERA_CUTS: tuple = ("2026-09-11", "2026-10-14")
MIN_N = 30          # stessa soglia di `stats.classify` e `significance`
DEFAULT_SEED = 7
BOOT = 6000         # abbastanza per una coda a 8.3e-4 (indice ~2.5 nell'ordinato)


def bonferroni_alpha(tests: int = N_TESTS, alpha: float = 0.05) -> float:
    """Livello di significativita' corretto per i test multipli."""
    return float(alpha / max(1, int(tests)))


# --------------------------------------------------------------------------- #
# Selezione (ex ante: solo quota, probabilita', edge, EV, mercato)
# --------------------------------------------------------------------------- #
def universe(df: pd.DataFrame, odds_min: float = PROD_ODDS_MIN,
             odds_max: float = PROD_ODDS_MAX) -> pd.Series:
    """Il perimetro su cui il tetto EV puo' esprimersi: 1X2 di produzione."""
    q = pd.to_numeric(df.get("quota"), errors="coerce")
    mp = pd.to_numeric(df.get("market_prob"), errors="coerce")
    return ((df.get("mercato") == "1X2") & (q >= odds_min) & (q <= odds_max)
            & (mp >= 0.5)).fillna(False).astype(bool)


def capped_mask(df: pd.DataFrame, cap: Optional[float], *,
                odds_min: float = PROD_ODDS_MIN, odds_max: float = PROD_ODDS_MAX,
                min_edge: float = PROD_EDGE_MIN, min_ev: float = PROD_EV_MIN) -> pd.Series:
    """1X2 di produzione con il solo TETTO in piu' (nessun allargamento).

    `cap=None` = nessun tetto (la strategia di produzione). Il pavimento resta
    quello di produzione: la regola puo' solo RESTRINGERE.
    """
    q = pd.to_numeric(df.get("quota"), errors="coerce")
    ev = pd.to_numeric(df.get("ev"), errors="coerce")
    edge = pd.to_numeric(df.get("market_edge"), errors="coerce")
    mp = pd.to_numeric(df.get("market_prob"), errors="coerce")
    mask = ((df.get("mercato") == "1X2") & (q >= odds_min) & (q <= odds_max)
            & (mp >= 0.5) & (edge >= min_edge) & (ev >= min_ev))
    if cap is not None:
        mask = mask & (ev <= float(cap))
    return mask.fillna(False).astype(bool)


def _closed(df: pd.DataFrame) -> pd.DataFrame:
    out = df[df.get("esito_finale").notna()].copy()
    out["profit"] = pd.to_numeric(out.get("profit"), errors="coerce")
    return out[out["profit"].notna()]


def _by_date(df: pd.DataFrame) -> pd.DataFrame:
    """Ordina per `created_at` SE la colonna esiste (diagnostica robusta).

    Una tabella costruita a mano nei test (o un estratto senza date) non deve
    far esplodere l'ordinamento: l'ordine serve a deduplicare la PRIMA
    occorrenza, non a validare lo schema.
    """
    if df is None or not len(df) or "created_at" not in df.columns:
        return df
    return df.sort_values("created_at")


def pick(df: pd.DataFrame, cap: Optional[float], *, dedup: bool = True) -> pd.DataFrame:
    """Righe CHIUSE selezionate dal tetto, una per (match_id, esito)."""
    closed = _closed(df)
    picked = _by_date(closed[capped_mask(closed, cap)])
    if dedup and len(picked):
        picked = picked.drop_duplicates(subset=["match_id", "esito"], keep="first")
    return picked.reset_index(drop=True)


# --------------------------------------------------------------------------- #
# 1) Tabella dei tetti
# --------------------------------------------------------------------------- #
def adjusted_ci(picked: pd.DataFrame, alpha: Optional[float] = None,
                n_boot: int = BOOT) -> dict:
    """CI bootstrap a cluster al livello CORRETTO per i test multipli.

    `stats.classify` usa il 95%: con 60 ipotesi provate quel livello "vince"
    anche sul rumore (~3 volte su 60). Qui l'alpha e' quello di Bonferroni,
    cosi' la differenza fra "sembra buono" e "e' dimostrato" e' scritta in
    due colonne e non affidata alla memoria di chi legge.
    """
    if picked is None or not len(picked):
        return {"lo": None, "hi": None, "alpha": alpha}
    vals = pd.to_numeric(picked["profit"], errors="coerce").dropna().tolist()
    clusters = picked.loc[pd.to_numeric(picked["profit"], errors="coerce").notna(),
                          "match_id"].tolist()
    if not vals:
        return {"lo": None, "hi": None, "alpha": alpha}
    ci = stats.bootstrap_mean_ci(vals, clusters, n_boot=n_boot,
                                 alpha=alpha if alpha is not None else bonferroni_alpha())
    return {"lo": ci["lo"], "hi": ci["hi"],
            "alpha": alpha if alpha is not None else bonferroni_alpha()}


def cap_table(df: pd.DataFrame, caps: Sequence = CAP_GRID,
              n_boot: int = BOOT) -> list:
    """n / ROI / CI per ogni tetto. Serve a vedere FORMA, non a scegliere.

    Con un solo tetto buono e i vicini cattivi la regola e' un incidente: la
    forma attesa da un edge vero e' una ZONA buona, non un picco isolato.
    """
    base = len(pick(df, None))
    rows = []
    for cap in caps:
        picked = pick(df, cap)
        res = evaluate(picked, label=f"cap={cap}")
        roi_ci = res.get("roi_ci95") or {}
        adj = adjusted_ci(picked, n_boot=n_boot)
        cont = contrast(df, cap, n_boot=n_boot)
        rows.append({
            "cap": cap,
            "cap_label": "production" if cap is None else f"{cap:.0%}",
            "excluded_n": cont.get("outside_n"),
            "excluded_roi": cont.get("outside_roi"),
            "delta_vs_excluded": cont.get("delta"),
            "delta_ci": cont.get("ci95"),
            "delta_status": cont.get("status"),
            "n": int(res.get("n") or 0),
            "share_of_universe": (len(picked) / base) if base else None,
            "roi": res.get("roi"),
            "ci_lo": roi_ci.get("lo"),
            "ci_hi": roi_ci.get("hi"),
            "ci_adj_lo": adj["lo"],
            "ci_adj_hi": adj["hi"],
            "significant_95": bool((roi_ci.get("lo") or -1) > 0),
            "significant_adjusted": bool((adj["lo"] or -1) > 0),
            "status": res.get("status"),
            "avg_odds": res.get("avg_odds"),
        })
    return rows


# --------------------------------------------------------------------------- #
# 2) Monotonia: Spearman(EV, profitto) con CI a cluster
# --------------------------------------------------------------------------- #
def ev_bucket_table(df: pd.DataFrame, edges: Sequence = EV_BUCKETS,
                    n_boot: int = BOOT) -> list:
    """ROI per fascia di EV dentro l'universo (il grafico che conta)."""
    closed = _closed(df)
    sel = _by_date(closed[universe(closed)])
    if len(sel):
        sel = sel.drop_duplicates(subset=["match_id", "esito"], keep="first")
    ev = pd.to_numeric(sel.get("ev"), errors="coerce")
    rows = []
    for i in range(len(edges) - 1):
        lo, hi = edges[i], edges[i + 1]
        m = pd.Series(True, index=sel.index)
        if lo is not None:
            m &= ev >= lo
        if hi is not None:
            m &= ev < hi
        part = sel[m.fillna(False)]
        res = evaluate(part, label=f"{lo}-{hi}")
        ci = res.get("roi_ci95") or {}
        adj = adjusted_ci(part, n_boot=n_boot)
        rows.append({
            "bucket": f"{'-inf' if lo is None else f'{lo:.1%}'} .. "
                      f"{'+inf' if hi is None else f'{hi:.1%}'}",
            "ev_lo": lo, "ev_hi": hi,
            "n": int(res.get("n") or 0),
            "roi": res.get("roi"),
            "ci_lo": ci.get("lo"), "ci_hi": ci.get("hi"),
            "ci_adj_lo": adj["lo"], "ci_adj_hi": adj["hi"],
            "hit_rate": res.get("hit_rate"),
            "avg_odds": res.get("avg_odds"),
        })
    return rows


def _spearman(x: np.ndarray, y: np.ndarray) -> Optional[float]:
    # 3 e' il minimo per una correlazione di rango non degenere; la soglia
    # usata dal verdetto di monotonia (4 fasce) e' dichiarata in
    # `monotone_verdict`, qui non si duplica.
    if len(x) < 3:
        return None
    rx = pd.Series(x).rank().to_numpy()
    ry = pd.Series(y).rank().to_numpy()
    if rx.std() == 0 or ry.std() == 0:
        return None
    return float(np.corrcoef(rx, ry)[0, 1])


def spearman_ev_profit(df: pd.DataFrame, n_boot: int = 2000,
                       seed: int = DEFAULT_SEED) -> dict:
    """L'EV del modello ordina i risultati? Misurato a CLUSTER (per partita).

    Se il modello sovrastima i favoriti piu' "attrattivi" (il sospetto), la
    correlazione e' NEGATIVA: piu' EV promesso, meno reso. Un CI che esclude
    lo zero rende il sospetto una misura.
    """
    closed = _closed(df)
    sel = _by_date(closed[universe(closed)])
    if len(sel):
        sel = sel.drop_duplicates(subset=["match_id", "esito"], keep="first")
    if len(sel) < 5:
        return {"n": int(len(sel)), "rho": None, "ci95": None,
                "status": "NON VERIFICABILE"}
    ev = pd.to_numeric(sel["ev"], errors="coerce").to_numpy(dtype=float)
    profit = pd.to_numeric(sel["profit"], errors="coerce").to_numpy(dtype=float)
    ok = np.isfinite(ev) & np.isfinite(profit)
    ev, profit = ev[ok], profit[ok]
    clusters = sel["match_id"].to_numpy()[ok]
    rho = _spearman(ev, profit)
    if rho is None:
        return {"n": int(len(ev)), "rho": None, "ci95": None,
                "status": "NON VERIFICABILE"}
    buckets: dict = {}
    for c, a, b in zip(clusters, ev, profit):
        buckets.setdefault(c, []).append((float(a), float(b)))
    groups = list(buckets.values())
    rng = random.Random(seed)
    samples = []
    for _ in range(int(n_boot)):
        pick_g = [groups[rng.randrange(len(groups))] for _ in range(len(groups))]
        xs = np.array([p[0] for g in pick_g for p in g])
        ys = np.array([p[1] for g in pick_g for p in g])
        v = _spearman(xs, ys)
        if v is not None:
            samples.append(v)
    samples.sort()
    lo = samples[int(0.025 * len(samples))] if samples else None
    hi = samples[min(len(samples) - 1, int(0.975 * len(samples)))] if samples else None
    status = "DA VERIFICARE"
    if lo is not None and hi is not None:
        if hi < 0:
            status = "SOVRASTIMA (rho<0)"
        elif lo > 0:
            status = "INFORMATIVO (rho>0)"
    return {"n": int(len(ev)), "clusters": len(groups), "rho": rho,
            "ci95": [lo, hi], "status": status}


def monotone_verdict(buckets: Sequence[dict], min_n: int = 8,
                     rho_max: float = -0.5) -> dict:
    """Il ROI cala al crescere dell'EV? Misurato con la CORRELAZIONE DI RANGO.

    Perche' non si contano piu' le inversioni: con una soglia sul campione
    minimo il conteggio DIPENDE da quali fasce entrano, e nella prima stesura
    questo produceva "MONOTONO (calante)" scartando le fasce piccole mentre la
    tabella completa saliva e poi crollava (artefatto della soglia, non del
    dato). La correlazione di rango usa TUTTE le fasce usabili e una soglia
    dichiarata (rho <= -0.5 = forma calante netta); il valore di rho e le fasce
    usate sono nel risultato, cosi' la conclusione e' verificabile a mano.
    """
    usable = [b for b in buckets if b["n"] >= min_n and b["roi"] is not None]
    if len(usable) < 4:
        return {"verdict": "NON VERIFICABILE", "usable_buckets": len(usable),
                "rho_buckets": None,
                "reason": f"meno di 4 fasce con n>={min_n}"}
    rois = [float(b["roi"]) for b in usable]
    ranks_x = list(range(len(rois)))
    rho = _spearman(np.array(ranks_x, dtype=float), np.array(rois, dtype=float))
    inversions = sum(1 for a, b in zip(rois, rois[1:]) if b > a)
    ok = rho is not None and rho <= rho_max
    return {
        "verdict": "MONOTONO (calante)" if ok else "NON MONOTONO",
        "usable_buckets": len(usable),
        "rho_buckets": rho,
        "rho_max": rho_max,
        "inversions": inversions,
        "rois": [round(r, 4) for r in rois],
        "ns": [int(b["n"]) for b in usable],
    }


def contrast(df: pd.DataFrame, cap: Optional[float], n_boot: int = BOOT) -> dict:
    """Selezionati vs ESCLUSI dallo stesso tetto: la differenza con CI.

    E' la prova diretta dell'ipotesi: il tetto si giustifica se le righe che
    BUTTA rendono sistematicamente peggio di quelle che tiene. Un ROI alto dei
    selezionati da solo non basta (con n piccolo qualunque sottoinsieme puo'
    sembrare buono: e' il motivo per cui il 20/10 serviva questa prova).
    """
    closed = _closed(df)
    inside = pick(closed, cap)
    all_rows = pick(closed, None)
    keys = set(zip(inside.get("match_id", []), inside.get("esito", [])))
    outside = all_rows[[(m, e) not in keys for m, e in
                        zip(all_rows.get("match_id", []), all_rows.get("esito", []))]]
    a = pd.to_numeric(inside.get("profit"), errors="coerce").dropna()
    b = pd.to_numeric(outside.get("profit"), errors="coerce").dropna()
    if len(a) < 5 or len(b) < 5:
        return {"inside_n": int(len(a)), "outside_n": int(len(b)),
                "delta": None, "ci95": None,
                "status": "NON VERIFICABILE"}
    diff = float(a.mean() - b.mean())
    rng = random.Random(DEFAULT_SEED)
    ga = a.tolist()
    gb = b.tolist()
    samples = []
    for _ in range(int(n_boot)):
        sa = [ga[rng.randrange(len(ga))] for _ in range(len(ga))]
        sb = [gb[rng.randrange(len(gb))] for _ in range(len(gb))]
        samples.append(sum(sa) / len(sa) - sum(sb) / len(sb))
    samples.sort()
    lo = samples[int(0.025 * len(samples))]
    hi = samples[min(len(samples) - 1, int(0.975 * len(samples)))]
    status = "ESCLUSI PEGGIORI" if lo > 0 else (
        "ESCLUSI MIGLIORI" if hi < 0 else "NESSUNA DIFFERENZA DIMOSTRATA")
    return {"inside_n": int(len(a)), "outside_n": int(len(b)),
            "inside_roi": float(a.mean()), "outside_roi": float(b.mean()),
            "delta": diff, "ci95": [float(lo), float(hi)], "status": status}


# --------------------------------------------------------------------------- #
# 3) Walk-forward del tetto (scelto solo sul passato)
# --------------------------------------------------------------------------- #
def walk_forward_cap(df: pd.DataFrame, caps: Sequence = CAP_GRID,
                     warmup_days: int = 10, min_train: int = 40,
                     min_n_train: int = 20) -> dict:
    """Il tetto si scegli SOLO su righe precedenti, poi si applica al giorno.

    E' la prova che regge o affonda l'ipotesi: se il tetto 'migliore' cambia
    ogni giorno e il risultato aggregato e' piatto, la regola non esiste —
    esisteva solo la fetta del 20/10.
    """
    d = df.copy()
    d["created_at"] = _date(d["created_at"])
    d = d.dropna(subset=["created_at"]).sort_values("created_at")
    if d.empty:
        return {"picked": d.iloc[0:0], "choices": [], "days": 0,
                "verdict": "NON VERIFICABILE"}
    start = d["created_at"].min() + pd.Timedelta(days=warmup_days)
    days = sorted({ts.floor("D") for ts in d["created_at"] if ts >= start})
    frames, choices = [], []
    for day in days:
        train = d[d["created_at"] < day]
        if len(train) < min_train:
            continue
        best, best_roi = None, None
        for cap in caps:
            picked = pick(train, cap)
            if len(picked) < min_n_train:
                continue
            r = evaluate(picked).get("roi")
            if r is None:
                continue
            if best_roi is None or r > best_roi:
                best, best_roi = cap, float(r)
        if best_roi is None and None not in caps:
            best = None
        today = d[(d["created_at"] >= day) & (d["created_at"] < day + pd.Timedelta(days=1))]
        if today.empty:
            continue
        chosen = pick(today, best, dedup=True)
        frames.append(chosen)
        choices.append({"day": str(day.date()), "cap": best,
                        "n_train": int(len(pick(train, None))),
                        "train_roi": best_roi, "n_today": int(len(chosen))})
    picked = pd.concat(frames, ignore_index=True) if frames else d.iloc[0:0]
    res = evaluate(picked, label="walk-forward cap")
    ci = res.get("roi_ci95") or {}
    stable = 0
    if choices:
        stable = sum(1 for c in choices[1:] if c["cap"] == choices[0]["cap"])
    verdict = "NON VERIFICABILE"
    if res.get("n"):
        if ci.get("lo") is not None and ci["lo"] > 0 and res["n"] >= MIN_N:
            verdict = "POSITIVO FUORI CAMPIONE"
        elif res["n"] < MIN_N:
            verdict = "CAMPIONE INSUFFICIENTE"
        else:
            verdict = "NON DIMOSTRATO FUORI CAMPIONE"
    return {"picked": picked, "choices": choices, "days": len(days),
            "stable_choices": stable, "result": res, "verdict": verdict}


# --------------------------------------------------------------------------- #
# 4) Split d'era
# --------------------------------------------------------------------------- #
def era_table(df: pd.DataFrame, cap: Optional[float] = 0.06,
              cuts: Sequence = ERA_CUTS) -> list:
    """Il vantaggio vive in un'era sola? Allora e' del periodo, non del modello."""
    closed = _closed(df)
    ts = _date(closed["created_at"])
    closed = closed.assign(_ts=ts).dropna(subset=["_ts"])
    bounds = [None] + [pd.Timestamp(c, tz="UTC") for c in cuts] + [None]
    rows = []
    for i in range(len(bounds) - 1):
        lo, hi = bounds[i], bounds[i + 1]
        m = pd.Series(True, index=closed.index)
        if lo is not None:
            m &= closed["_ts"] >= lo
        if hi is not None:
            m &= closed["_ts"] < hi
        part = closed[m.fillna(False)]
        label = (f"{'-inf' if lo is None else str(lo.date())} .. "
                 f"{'+inf' if hi is None else str(hi.date())}")
        with_cap = evaluate(pick(part, cap), label=f"cap {label}")
        without = evaluate(pick(part, None), label=f"prod {label}")
        rows.append({
            "era": label,
            "n_prod": int(without.get("n") or 0),
            "roi_prod": without.get("roi"),
            "n_cap": int(with_cap.get("n") or 0),
            "roi_cap": with_cap.get("roi"),
            "delta": ((with_cap.get("roi") or 0) - (without.get("roi") or 0))
                     if with_cap.get("roi") is not None and without.get("roi") is not None
                     else None,
        })
    return rows


# --------------------------------------------------------------------------- #
# 5) Denaro reale: le puntate live con lo stesso tetto
# --------------------------------------------------------------------------- #
def _norm(s: str) -> str:
    import unicodedata

    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.lower().replace("-", " ").split())


def canonical_esito(esito, home=None, away=None) -> Optional[str]:
    """Esito canonico 1/X/2 senza indovinare (il ledger non e' omogeneo)."""
    e = str(esito or "").strip()
    if e in ("1", "X", "2"):
        return e
    low = e.lower()
    if low in ("x", "draw", "tie", "pareggio"):
        return "X"
    ne = _norm(e)
    if not ne:
        return None
    if home and ne == _norm(home):
        return "1"
    if away and ne == _norm(away):
        return "2"
    return None


def _canon_column(df: pd.DataFrame) -> list:
    """Esito canonico per riga, tollerante alle colonne mancanti.

    Le previsioni non portano casa/trasferta (arrivano da `matches`, che il
    loader non join-a): senza questa tolleranza uno `zip` su una colonna
    assente solleva e la misura del denaro reale sparisce invece di dichiararsi
    parziale.
    """
    n = len(df)
    esiti = df["esito"].tolist() if "esito" in df.columns else [None] * n
    homes = df["home"].tolist() if "home" in df.columns else [None] * n
    aways = df["away"].tolist() if "away" in df.columns else [None] * n
    return [canonical_esito(e, h, a) for e, h, a in zip(esiti, homes, aways)]


def live_money(bets_df: pd.DataFrame, preds_df: pd.DataFrame,
               cap: Optional[float] = 0.06) -> dict:
    """Le PUNTATE reali hanno lo stesso EV di quando sono state ordinate?

    Perche' conta: se il tetto migliora le previsioni ma non gli ORDINI, sta
    descrivendo il ledger e non il denaro. Il collegamento e' per
    `(match_id, esito canonico)`; senza collegamento la puntata e' dichiarata
    `unlinked`, mai attribuita a una fascia per caso.
    """
    if bets_df is None or len(bets_df) == 0:
        return {"n": 0, "status": "NON VERIFICABILE", "by_band": []}
    live = bets_df[(bets_df.get("mode") == "live") & bets_df.get("esito_finale").notna()].copy()
    live["profit"] = pd.to_numeric(live.get("profit"), errors="coerce")
    # il ledger delle puntate chiama la quota `price`: `evaluate` legge `quota`
    # (stessa convenzione del ledger previsioni), quindi si allinea il NOME qui
    live["quota"] = pd.to_numeric(live.get("price"), errors="coerce")
    live["stake"] = pd.to_numeric(live.get("stake"), errors="coerce")
    live = live[live["profit"].notna()]
    if len(live) == 0 or preds_df is None or len(preds_df) == 0:
        return {"n": 0, "status": "NON VERIFICABILE", "by_band": []}
    # ⚠️ su `bets` il profitto e' in USDC, su `predictions` e' per unita' di
    # stake: sommarli o confrontarli come se fossero la stessa cosa e' il
    # difetto che il 15/09 ha reso illeggibile `league_gate_impact` (le due
    # fonti NON si sommano). Qui il ROI si calcola sul profitto per unita'.
    too_small = live["stake"].isna() | (live["stake"] <= 0)
    unlinked_stake = int(too_small.sum())
    live = live[~too_small].copy()
    live["profit_usdc"] = live["profit"]
    live["profit"] = live["profit"] / live["stake"]
    if len(live) == 0:
        return {"n": 0, "status": "NON VERIFICABILE", "by_band": []}
    p = preds_df.copy()
    p["_canon"] = _canon_column(p)
    p = p[p["_canon"].notna()]
    p["ev"] = pd.to_numeric(p.get("ev"), errors="coerce")
    p = p.dropna(subset=["ev"])
    p = _by_date(p).drop_duplicates(subset=["match_id", "_canon"], keep="last")
    live["_canon"] = _canon_column(live)
    merged = live.merge(p[["match_id", "_canon", "ev"]],
                       on=["match_id", "_canon"], how="left")
    linked = merged[merged["ev"].notna()]
    unlinked = int(len(merged) - len(linked))
    out = {"n": int(len(merged)), "linked": int(len(linked)),
           "unlinked": unlinked, "unlinked_stake": unlinked_stake, "cap": cap,
           "usdc_total": float(linked["profit_usdc"].sum()),
           "stake_total": float(linked["stake"].sum()),
           "by_band": []}
    bands = ((None, 0.03, "EV < 3%"), (0.03, 0.06, "EV 3-6%"),
             (0.06, 0.10, "EV 6-10%"), (0.10, None, "EV >= 10%"))
    for lo, hi, label in bands:
        m = pd.Series(True, index=linked.index)
        if lo is not None:
            m &= linked["ev"] >= lo
        if hi is not None:
            m &= linked["ev"] < hi
        part = linked[m.fillna(False)]
        res = evaluate(part, label=label)
        ci = res.get("roi_ci95") or {}
        out["by_band"].append({"band": label, "n": int(res.get("n") or 0),
                               "roi": res.get("roi"), "ci_lo": ci.get("lo"),
                               "ci_hi": ci.get("hi")})
    with_cap = linked[linked["ev"] <= float(cap)] if cap is not None else linked
    res_cap = evaluate(with_cap, label=f"live cap {cap}")
    res_all = evaluate(linked, label="live prod")
    out["cap_result"] = {"n": int(res_cap.get("n") or 0), "roi": res_cap.get("roi"),
                         "usdc": float(with_cap["profit_usdc"].sum()),
                         "stake": float(with_cap["stake"].sum())}
    out["prod_result"] = {"n": int(res_all.get("n") or 0), "roi": res_all.get("roi"),
                          "usdc": float(linked["profit_usdc"].sum()),
                          "stake": float(linked["stake"].sum())}
    out["status"] = "MISURATO" if out["linked"] >= 20 else "CAMPIONE INSUFFICIENTE"
    return out


# --------------------------------------------------------------------------- #
# Verdetto
# --------------------------------------------------------------------------- #
def verdict(cap_rows: Sequence[dict], monotony: dict, spearman: dict,
            wf: dict, eras: Sequence[dict], live: dict) -> dict:
    """Decide se la regola esiste. **Bocciatura = nessuna modifica**.

    Le condizioni sono AND: ognuna da sola puo' bocciare. Non e' prudenza
    generica — e' che il 20/10 l'ipotesi e' nata guardando gli stessi dati su
    cui la si misura, quindi serve ridondanza.
    """
    checks = []
    best = None
    for r in cap_rows:
        if r["cap"] is None or r["n"] < MIN_N or r["roi"] is None:
            continue
        if best is None or r["roi"] > best["roi"]:
            best = r
    if best:
        checks.append({"check": "in-sample cap", "ok": bool(best["roi"] > 0),
                       "detail": f"miglior tetto {best['cap_label']} n={best['n']} "
                                 f"ROI {best['roi']:+.2%}"})
        checks.append({"check": "esclusi peggiori (contrasto)",
                       "ok": best.get("delta_status") == "ESCLUSI PEGGIORI",
                       "detail": f"esclusi n={best.get('excluded_n')} ROI "
                                 f"{best.get('excluded_roi')}, delta CI "
                                 f"{best.get('delta_ci')}"})
    else:
        checks.append({"check": "in-sample cap", "ok": False,
                       "detail": "nessun tetto con n>=30"})
    checks.append({"check": "monotonia", "ok": monotony.get("verdict") == "MONOTONO (calante)",
                   "detail": monotony.get("verdict")})
    contrast_ok = bool(best and best.get("delta_status") == "ESCLUSI PEGGIORI")
    rho_ok = (spearman.get("rho") is not None and spearman["rho"] < 0
              and (spearman.get("ci95") or [None, None])[1] is not None
              and spearman["ci95"][1] < 0)
    checks.append({"check": "correlazione EV/profitto",
                   "ok": bool(rho_ok),
                   "detail": f"rho={spearman.get('rho')} CI={spearman.get('ci95')}"})
    checks.append({"check": "walk-forward", "ok": wf.get("verdict") == "POSITIVO FUORI CAMPIONE",
                   "detail": wf.get("verdict")})
    positive_eras = [e for e in eras if e["delta"] is not None and e["delta"] > 0]
    checks.append({"check": "regge in piu' di un'era",
                   "ok": len(positive_eras) >= 2,
                   "detail": f"{len(positive_eras)} ere su {len(eras)} con delta>0"})
    live_ok = (live.get("status") == "MISURATO"
               and (live.get("cap_result") or {}).get("roi") is not None
               and (live.get("prod_result") or {}).get("roi") is not None
               and live["cap_result"]["roi"] >= live["prod_result"]["roi"])
    checks.append({"check": "denaro reale non peggiora", "ok": bool(live_ok),
                   "detail": f"live cap {live.get('cap_result')} vs prod {live.get('prod_result')}"})
    passed = sum(1 for c in checks if c["ok"])
    # REGOLA DI DECISIONE (scritta prima di guardare i numeri):
    # servono TUTTE le condizioni tranne al massimo una, e in nessun caso
    # possono mancare le tre che provano la DIREZIONE (monotonia, contrasto,
    # walk-forward). Il contrasto e' critico perche' e' la prova diretta:
    # un tetto si giustifica solo se cio' che BUTTA rende peggio.
    critical_ok = (monotony.get("verdict") == "MONOTONO (calante)"
                   and contrast_ok
                   and wf.get("verdict") == "POSITIVO FUORI CAMPIONE")
    usable = bool(critical_ok and passed >= len(checks) - 1)
    return {
        "usable": usable,
        "checks": checks,
        "passed": passed, "total": len(checks),
        "critical": {"monotonia": monotony.get("verdict"),
                     "contrasto": contrast_ok,
                     "walk_forward": wf.get("verdict")},
        "decision": ("REGOLA CANDIDATA (da validare in shadow, non in live)"
                     if usable else "NESSUNA REGOLA: nessun tetto promosso"),
        "best_cap": (best or {}).get("cap"),
    }


# --------------------------------------------------------------------------- #
# Esecuzione completa + report
# --------------------------------------------------------------------------- #
def _with_teams(preds: pd.DataFrame, path=None) -> pd.DataFrame:
    """Aggiunge casa/trasferta alle previsioni (per l'esito canonico).

    Senza questo, un `esito` scritto come NOME SQUADRA non e' riconducibile a
    1/X/2 e le puntate finiscono tutte in `unlinked` — il difetto che il 15/09
    ha reso invisibile un insieme di segnali alla catena `decision`.
    """
    try:
        m = loader.fetch_df("SELECT id, home_team, away_team FROM matches", path=path)
    except Exception:  # pragma: no cover - snapshot senza tabella matches
        return preds
    if preds is None or not len(preds) or not len(m):
        return preds
    m = m.rename(columns={"id": "match_id", "home_team": "home",
                          "away_team": "away"})
    return preds.merge(m, on="match_id", how="left")


def run(path=None, cap: float = 0.06) -> dict:
    preds = _with_teams(loader.predictions(path), path)
    bets_df = loader.bets(path)
    audit = loader.audit(path)
    out = {
        "snapshot": {"path": audit["path"], "quick_check": audit["quick_check"],
                     "predictions": int(audit["tables"].get("predictions", 0)),
                     "bets": int(audit["tables"].get("bets", 0))},
        "thresholds": {"odds_min": PROD_ODDS_MIN, "odds_max": PROD_ODDS_MAX,
                       "ev_min": PROD_EV_MIN, "edge_min": PROD_EDGE_MIN,
                       "cap_tested": cap},
        "multiple_testing": {"hypotheses_listed": len(HYPOTHESES),
                             "tests_accounted": N_TESTS,
                             "bonferroni_alpha": bonferroni_alpha()},
        "universe": (evaluate(pick(preds, None), label="universo 1X2 produzione")),
        "cap_table": cap_table(preds),
        "ev_buckets": ev_bucket_table(preds),
        "spearman": spearman_ev_profit(preds),
    }
    out["monotony"] = monotone_verdict(out["ev_buckets"])
    wf = walk_forward_cap(preds)
    out["walk_forward"] = {
        "verdict": wf["verdict"], "days": wf["days"],
        "choices": wf["choices"], "result": wf.get("result"),
        "cap_distribution": _cap_dist(wf["choices"]),
    }
    out["eras"] = era_table(preds, cap=cap)
    out["live_money"] = live_money(bets_df, preds, cap=cap)
    out["verdict"] = verdict(out["cap_table"], out["monotony"], out["spearman"],
                            out["walk_forward"], out["eras"], out["live_money"])
    out["plan"] = rollout_plan(out, cap=cap)
    return out


def _cap_dist(choices: Sequence[dict]) -> dict:
    dist: dict = {}
    for c in choices:
        key = "production" if c["cap"] is None else f"{c['cap']:.0%}"
        dist[key] = dist.get(key, 0) + 1
    return dist


def rollout_plan(res: dict, cap: float = 0.06) -> dict:
    """Il piano si scrive SEMPRE, ma non si esegue: serve se il verdetto e' ok.

    Ogni numero qui e' un CRITERIO DI DECISIONE dichiarato prima dei dati
    futuri, cosi' il prossimo giro non puo' scegliere a posteriori cosa
    contava. `executed=False` per costruzione: nessun ordine parte da qui.
    """
    ok = bool(res.get("verdict", {}).get("usable"))
    return {
        "executed": False,
        "trigger": (f"tetto EV <= {cap:.0%} sui soli 1X2 con quota "
                    f"{PROD_ODDS_MIN:.2f}-{PROD_ODDS_MAX:.2f}, favorito di mercato"),
        "shadow_days": 7,
        "shadow_min_signals": 20,
        "promotion_criteria": [
            "shadow: nessuna divergenza di PREZZO fra pick e ordine",
            "shadow: ROI delle righe selezionate con CI95 che esclude zero",
            "live: massimo 1.00 USDC per ordine finche' l'equity non supera 50 USDC",
            "live: stop se 10 puntate chiudono con P/L < -3.00 (rollback a produzione)",
            "live: stop se il residuo dalla riconciliazione supera 0.50 USDC/giorno",
        ],
        "rollback": "rimuovere il tetto: e' un solo confronto (ev <= cap) nel gate",
        "why_now": ("misurato e non dimostrato" if not ok else
                    "supera monotonia + walk-forward: si valida in shadow"),
    }


def format_report(res: dict) -> str:
    """Report markdown: numeri, intervalli e limiti — niente slogan."""
    lines = []
    add = lines.append
    mt = res["multiple_testing"]
    add("# Caccia all'edge — il tetto EV e' una regola o una fetta?\n")
    add(f"Snapshot: `{res['snapshot']['path']}` "
        f"(quick_check **{res['snapshot']['quick_check']}**, "
        f"{res['snapshot']['predictions']} previsioni, {res['snapshot']['bets']} bet)\n")
    add(f"Soglie di produzione (lette da `value_filter`): quota "
        f"{res['thresholds']['odds_min']:.2f}-{res['thresholds']['odds_max']:.2f}, "
        f"EV >= {res['thresholds']['ev_min']:.1%}, edge >= "
        f"{res['thresholds']['edge_min']:.1%}. "
        f"Test accountati: **{mt['tests_accounted']}**, alfa Bonferroni "
        f"**{mt['bonferroni_alpha']:.2e}**.\n")
    u = res["universe"]
    ci = (u.get("roi_ci95") or {})
    add("## Universo (1X2 di produzione)\n")
    add(f"{u.get('n')} righe chiuse, ROI **{_pct(u.get('roi'))}** "
        f"(CI95 {_pct(ci.get('lo'))} .. {_pct(ci.get('hi'))}), "
        f"hit {_pct(u.get('hit_rate'))}, quota media {_num(u.get('avg_odds'))}.\n")
    add("## 1) Tabella dei tetti\n")
    add("| tetto | n | ROI | CI95 | CI Bonferroni | esclusi: n / ROI | delta (CI95) | stato |")
    add("|---|---|---|---|---|---|---|---|")
    for r in res["cap_table"]:
        dci = r.get("delta_ci") or [None, None]
        add(f"| {r['cap_label']} | {r['n']} | {_pct(r['roi'])} | "
            f"{_pct(r['ci_lo'])} .. {_pct(r['ci_hi'])} | "
            f"{_pct(r.get('ci_adj_lo'))} .. {_pct(r.get('ci_adj_hi'))} | "
            f"{r.get('excluded_n')} / {_pct(r.get('excluded_roi'))} | "
            f"{_pct(r.get('delta_vs_excluded'))} ({_pct(dci[0])} .. {_pct(dci[1])}) | "
            f"{r['status']} |")
    add("")
    surv = [r for r in res["cap_table"] if r.get("significant_adjusted")]
    add(f"Tetti con CI Bonferroni che esclude lo zero: **{len(surv)}**"
        + (f" ({', '.join(r['cap_label'] for r in surv)})" if surv else "") +
        f" su {len(res['cap_table'])} provati "
        f"(alfa {res['multiple_testing']['bonferroni_alpha']:.2e}).\n")
    add("## 2) Monotonia: ROI per fascia di EV\n")
    add("| fascia EV | n | ROI | CI95 | CI Bonferroni | hit | quota media |")
    add("|---|---|---|---|---|---|---|")
    for b in res["ev_buckets"]:
        add(f"| {b['bucket']} | {b['n']} | {_pct(b['roi'])} | "
            f"{_pct(b['ci_lo'])} .. {_pct(b['ci_hi'])} | "
            f"{_pct(b.get('ci_adj_lo'))} .. {_pct(b.get('ci_adj_hi'))} | "
            f"{_pct(b['hit_rate'])} | {_num(b['avg_odds'])} |")
    add("")
    m = res["monotony"]
    s = res["spearman"]
    add(f"**Verdetto monotonia: {m.get('verdict')}** "
        f"(fasce usabili {m.get('usable_buckets')}, rho sulle fasce "
        f"{m.get('rho_buckets')} contro soglia {m.get('rho_max')}).\n")
    add(f"Spearman(EV, profitto) = **{s.get('rho')}** "
        f"CI95 {s.get('ci95')} su {s.get('n')} righe / {s.get('clusters')} partite "
        f"-> {s.get('status')}.\n")
    add("## 3) Walk-forward del tetto (scelto solo sul passato)\n")
    wf = res["walk_forward"]
    wr = wf.get("result") or {}
    wci = wr.get("roi_ci95") or {}
    add(f"{wf['days']} giorni valutati, scelte: {wf['cap_distribution']}.")
    add(f"ROI aggregato **{_pct(wr.get('roi'))}** "
        f"(CI95 {_pct(wci.get('lo'))} .. {_pct(wci.get('hi'))}, "
        f"n={wr.get('n')}) -> **{wf['verdict']}**.\n")
    add("## 4) Split d'era\n")
    add("| era | n prod | ROI prod | n tetto | ROI tetto | delta |")
    add("|---|---|---|---|---|---|")
    for e in res["eras"]:
        add(f"| {e['era']} | {e['n_prod']} | {_pct(e['roi_prod'])} | "
            f"{e['n_cap']} | {_pct(e['roi_cap'])} | {_pct(e['delta'])} |")
    add("")
    add("## 5) Denaro reale (puntate live)\n")
    lm = res["live_money"]
    add(f"{lm.get('n')} puntate live chiuse, {lm.get('linked')} collegate a una "
        f"previsione (`unlinked` {lm.get('unlinked')} senza collegamento, "
        f"{lm.get('unlinked_stake')} senza stake) -> {lm.get('status')}. "
        f"Stake collegato {lm.get('stake_total')} USDC, P/L "
        f"{lm.get('usdc_total')} USDC (profitto per unita' per il ROI).\n")
    if lm.get("by_band"):
        add("| fascia EV | n | ROI | CI95 |")
        add("|---|---|---|---|")
        for b in lm["by_band"]:
            add(f"| {b['band']} | {b['n']} | {_pct(b['roi'])} | "
                f"{_pct(b['ci_lo'])} .. {_pct(b['ci_hi'])} |")
        add("")
        add(f"Con tetto {lm.get('cap'):.0%}: n={lm['cap_result']['n']} "
            f"ROI {_pct(lm['cap_result']['roi'])} "
            f"({lm['cap_result']['usdc']:+.2f} USDC su {lm['cap_result']['stake']:.2f}) "
            f"contro produzione n={lm['prod_result']['n']} "
            f"ROI {_pct(lm['prod_result']['roi'])} "
            f"({lm['prod_result']['usdc']:+.2f} USDC su {lm['prod_result']['stake']:.2f}).\n")
    v = res["verdict"]
    add("## Verdetto\n")
    add("| controllo | esito | dettaglio |")
    add("|---|---|---|")
    for c in v["checks"]:
        add(f"| {c['check']} | {'OK' if c['ok'] else 'NO'} | {c['detail']} |")
    add("")
    add(f"**{v['passed']}/{v['total']} controlli superati -> {v['decision']}**\n")
    p = res["plan"]
    add("## Piano (scritto, NON eseguito)\n")
    add(f"Gatante se promosso: {p['trigger']}.")
    add(f"Shadow {p['shadow_days']} giorni con almeno {p['shadow_min_signals']} segnali.")
    for c in p["promotion_criteria"]:
        add(f"- {c}")
    add(f"\nRollback: {p['rollback']}.\n")
    return "\n".join(lines) + "\n"


def _pct(v) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "n/d"
    return f"{float(v):+.2%}" if abs(float(v)) < 10 else f"{float(v):+.1f}"


def _num(v) -> str:
    return "n/d" if v is None else f"{float(v):.3f}"


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Caccia all'edge sul ledger (sola lettura)")
    ap.add_argument("--db", default=None)
    ap.add_argument("--cap", type=float, default=0.06)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--out", default=None, help="scrive il report markdown")
    args = ap.parse_args(argv)
    res = run(args.db, cap=args.cap)
    if args.json:
        print(json.dumps(res, indent=2, default=str))
    else:
        print(format_report(res))
    if args.out:
        Path(args.out).write_text(format_report(res), encoding="utf-8")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
