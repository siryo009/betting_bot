"""Diagnostica del collo di bottiglia: dove si perde (o non si guadagna).

Tre domande, tutte in sola lettura e tutte con intervalli:

1. **Corsia reale vs ledger** (`live_vs_ledger`): i soldi veri sono stati messi
   sulle stesse righe che il laboratorio misura come "giocabili"? Se no, il
   ROI del ledger non descrive la produzione (ed e' successo davvero: il ledger
   1X2 e' positivo e le puntate live 1X2 sono negative).
2. **Fasce di quota** (`band_table`): il modello batte il mercato in tutte le
   fasce o solo in una? Una fascia che perde in modo stabile e' una regola
   ex ante (non un parametro scelto a posteriori).
3. **Celle ex ante** (`cell_scan`): mercato x fascia quota x fascia edge, con
   correzione di Bonferroni. Se nessuna cella sopravvive alla correzione, il
   problema non e' "trovare la soglia giusta": e' il campione (o l'edge).
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from . import stats
from .strategies import select

PLAYABLE = ("value", "strong_value", "moderate")
MARKETS = ("1X2", "OU", "AH", "TENNIS")
ODDS_EDGES = (1.20, 1.40, 1.60, 1.80, 2.00, 2.50, 99.0)
EDGE_EDGES = (-0.05, 0.0, 0.02, 0.05, 0.10, 1.0)


def _canon_esito(value, home=None, away=None) -> Optional[str]:
    """Esito canonico per la giunzione bets<->predictions (mai indovinare)."""
    try:
        from decision.adapters import canonical_outcome
        return canonical_outcome(value, home, away)
    except Exception:
        v = str(value or "").strip()
        return v if v in ("1", "X", "2") else None


def _summ(df: pd.DataFrame, label: str) -> dict:
    vals = pd.to_numeric(df["profit"], errors="coerce").dropna().tolist()
    if not vals:
        return {"label": label, "n": 0, "status": "NON VERIFICABILE"}
    s = stats.summarize(
        vals, clusters=df.loc[df["profit"].notna(), "match_id"].tolist(),
        odds=pd.to_numeric(df.get("quota"), errors="coerce").tolist(),
        wins=int((df["esito_finale"] == "won").sum()), n=len(vals))
    s["label"] = label
    s["status"] = stats.classify(s)
    return s


def live_vs_ledger(preds: pd.DataFrame, bets: pd.DataFrame) -> dict:
    """Quanto la corsia reale assomiglia alla popolazione misurata dal lab."""
    b = bets.copy()
    b["stake"] = pd.to_numeric(b["stake"], errors="coerce").fillna(0.0)
    b["profit"] = pd.to_numeric(b["profit"], errors="coerce").fillna(0.0)
    b["price"] = pd.to_numeric(b["price"], errors="coerce")
    live = b[(b["mode"] == "live") & (b["stake"] > 0)].copy()
    if live.empty:
        return {"status": "NON VERIFICABILE"}

    live["esito_key"] = [
        _canon_esito(r["esito"], r.get("home"), r.get("away"))
        for _, r in live.iterrows()
    ]

    # righe di ledger chiuse e giocabili, una per (match, esito)
    p = preds[preds["status"].isin(PLAYABLE)].copy()
    p["profit"] = pd.to_numeric(p["profit"], errors="coerce")
    p = p.dropna(subset=["profit"]).sort_values("created_at")
    p["esito_key"] = [
        _canon_esito(r["esito"], None, None) for _, r in p.iterrows()
    ]
    p_dedup = p.drop_duplicates(subset=["match_id", "esito_key"], keep="first")

    matched = live.merge(
        p_dedup[["match_id", "esito_key", "ev", "market_edge", "quota", "status"]],
        on=["match_id", "esito_key"], how="left", suffixes=("", "_pred"))

    def _roi(df):
        st = float(df["stake"].sum())
        return (float(df["profit"].sum()) / st) if st else None

    by_market = {}
    for mkt, grp in live.groupby("mercato"):
        ci = stats.bootstrap_mean_ci(
            (grp["profit"] / grp["stake"]).tolist(), grp["match_id"].tolist())
        by_market[mkt] = {
            "n": int(len(grp)), "stake": float(grp["stake"].sum()),
            "pl": float(grp["profit"].sum()), "roi": _roi(grp),
            "roi_ci95": [ci["lo"], ci["hi"]],
            "avg_price": float(grp["price"].mean()) if grp["price"].notna().any() else None,
            "won": int((grp["esito_finale"] == "won").sum()),
            "lost": int((grp["esito_finale"] == "lost").sum()),
            "push": int((grp["esito_finale"] == "push").sum()),
        }

    # fascia di prezzo sulle puntate reali (la quota PAGATA, non quella del
    # segnale: sono due cose diverse e il ledger registra la seconda)
    bands = []
    for lo, hi in zip(ODDS_EDGES[:-1], ODDS_EDGES[1:]):
        grp = live[(live["price"] >= lo) & (live["price"] < hi)]
        if grp.empty:
            continue
        ci = stats.bootstrap_mean_ci(
            (grp["profit"] / grp["stake"]).tolist(), grp["match_id"].tolist())
        bands.append({
            "band": f"{lo}-{hi if hi < 90 else '+'}", "n": int(len(grp)),
            "stake": float(grp["stake"].sum()), "pl": float(grp["profit"].sum()),
            "roi": _roi(grp), "roi_ci95": [ci["lo"], ci["hi"]],
            "won": int((grp["esito_finale"] == "won").sum()),
        })

    ev_matched = pd.to_numeric(matched.get("ev"), errors="coerce")
    all_live_ci = stats.bootstrap_mean_ci(
        (live["profit"] / live["stake"]).tolist(), live["match_id"].tolist())

    return {
        "n_live": int(len(live)),
        "stake_total": float(live["stake"].sum()),
        "pl_total": float(live["profit"].sum()),
        "roi": _roi(live),
        "roi_ci95": [all_live_ci["lo"], all_live_ci["hi"]],
        "won": int((live["esito_finale"] == "won").sum()),
        "lost": int((live["esito_finale"] == "lost").sum()),
        "push": int((live["esito_finale"] == "push").sum()),
        "by_market": by_market,
        "by_price_band": bands,
        "join": {
            "matched": int(ev_matched.notna().sum()),
            "matched_share": float(ev_matched.notna().mean()),
            "avg_ev_of_bets": float(ev_matched.mean()) if ev_matched.notna().any() else None,
            "ledger_playable_rows": int(len(p_dedup)),
            "ledger_roi": _summ(p_dedup, "ledger playable").get("roi"),
        },
    }


def band_table(preds: pd.DataFrame, markets=MARKETS) -> dict:
    """ROI per fascia di quota sulle righe giocabili (una per match/esito)."""
    p = preds[preds["status"].isin(PLAYABLE)].copy()
    p["profit"] = pd.to_numeric(p["profit"], errors="coerce")
    p["quota"] = pd.to_numeric(p["quota"], errors="coerce")
    p = p.dropna(subset=["profit", "quota"]).sort_values("created_at")
    p = p.drop_duplicates(subset=["match_id", "esito"], keep="first")
    out = {}
    for mkt in markets:
        rows = []
        sub = p[p["mercato"] == mkt]
        for lo, hi in zip(ODDS_EDGES[:-1], ODDS_EDGES[1:]):
            grp = sub[(sub["quota"] >= lo) & (sub["quota"] < hi)]
            if not len(grp):
                continue
            s = _summ(grp, f"{mkt} {lo}-{hi if hi < 90 else '+'}")
            rows.append({"band": s["label"], "n": s["n"], "roi": s.get("roi"),
                         "ci95": [s["roi_ci95"]["lo"], s["roi_ci95"]["hi"]],
                         "hit_rate": s.get("hit_rate"),
                         "avg_odds": s.get("avg_odds"),
                         "status": s["status"]})
        if rows:
            out[mkt] = rows
    return out


def live_price_bands(bets: pd.DataFrame) -> list:
    """ROI delle puntate REALI per fascia di **prezzo pagato**.

    Perche' separata da `band_table`: li' la fascia e' quella della quota del
    SEGNALE sulle righe giocabili, qui e' il prezzo effettivamente eseguito
    (`bets.price`). Sono due domande diverse ("il segnale funziona?" vs "quanto
    ho pagato?") e mescolarle ha gia' prodotto una lettura sbagliata nel
    progetto (il ROI aggregato che univa giocabili e `rejected`).

    Le bande sono dichiarate PRIMA (`ODDS_EDGES`, le stesse del resto del lab),
    i puntate sono clusterizzate per partita e ogni riga dichiara il suo stato.
    """
    b = bets.copy()
    if b.empty:
        return []
    b["stake"] = pd.to_numeric(b["stake"], errors="coerce").fillna(0.0)
    b["profit"] = pd.to_numeric(b["profit"], errors="coerce")
    b["quota"] = pd.to_numeric(b["price"], errors="coerce")
    b = b[(b["mode"] == "live") & (b["stake"] > 0)]
    b = b.dropna(subset=["profit", "quota"])
    rows = []
    for lo, hi in zip(ODDS_EDGES[:-1], ODDS_EDGES[1:]):
        grp = b[(b["quota"] >= lo) & (b["quota"] < hi)]
        if not len(grp):
            continue
        s = _weighted_roi(grp)
        s["band"] = f"{lo}-{hi if hi < 90 else '+'}"
        s["status"] = stats.classify({"n": s["n"], "roi": s["roi"],
                                       "roi_ci95": {"lo": s["ci95"][0],
                                                    "hi": s["ci95"][1]}})
        rows.append(s)
    return rows


def _cluster_pairs(profit, stake, clusters):
    """Somme (profitto, stake) per partita: la base del bootstrap a cluster."""
    df = pd.DataFrame({"p": np.asarray(profit, dtype=float),
                       "s": np.asarray(stake, dtype=float),
                       "c": list(clusters)})
    g = df.groupby("c")[["p", "s"]].sum()
    return g["p"].to_numpy(), g["s"].to_numpy()


def _weighted_bootstrap(p, s, n_boot: int = 2000, seed: int = 7):
    """IC95 di sum(profitto)/sum(stake) con ricampionamento DELLE PARTITE.

    Il bootstrap sui singoli gettoni mentirebbe su questa metrica: le puntate
    della stessa partita condividono esito e prezzo, quindi l'unita' di
    indipendenza e' la partita (stessa scelta del resto del lab).
    """
    rng = np.random.default_rng(seed)
    k = len(p)
    if k == 0:
        return None, None
    idx = rng.integers(0, k, size=(int(n_boot), k))
    P = p[idx].sum(axis=1)
    S = s[idx].sum(axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        r = np.where(S > 0, P / S, np.nan)
    r = r[~np.isnan(r)]
    if not r.size:
        return None, None
    return float(np.percentile(r, 2.5)), float(np.percentile(r, 97.5))


def _weighted_roi(grp: pd.DataFrame) -> dict:
    """ROI **pesato sullo stake** (non media dei ROI) + IC a cluster."""
    stake = float(grp["stake"].sum())
    pl = float(grp["profit"].sum())
    p, s = _cluster_pairs(grp["profit"], grp["stake"], grp["match_id"])
    lo, hi = _weighted_bootstrap(p, s)
    n = int(len(grp))
    wins = int((grp["esito_finale"] == "won").sum())
    return {"n": n, "roi": (pl / stake) if stake else None,
            "ci95": [lo, hi], "stake": stake, "pl": pl,
            "hit_rate": (wins / n) if n else None,
            "avg_odds": float(grp["quota"].mean()) if n else None,
            "clusters": int(len(p))}


def clv_cohorts(preds: pd.DataFrame, clv_df: pd.DataFrame) -> dict:
    """Il CLV predice il risultato? Confronto delle coorti a CLV + e CLV -.

    Perche' conta: il CLV e' la metrica di PROCESSO; se non ordinasse gli esiti
    (coorte positiva >= coorte negativa) non sarebbe un predittore, e l'unico
    argomento a favore del progetto cadrebbe. La chiusura segue la STESSA
    priorita' di `_clv_block` (`closing_odds` -> `closing_quota` ->
    `pinnacle_quota`) e il campione e' quello NON-ECO: un CLV 0 da eco del
    segnale non e' un campione, e' artefatto.
    """
    c = clv_df.copy()
    if c.empty:
        return {"status": "NON VERIFICABILE"}
    for col in ("closing_odds", "closing_quota", "pinnacle_quota"):
        c[col] = pd.to_numeric(c.get(col), errors="coerce")
    c["closing"] = (c["closing_odds"].fillna(c["closing_quota"])
                     .fillna(c["pinnacle_quota"]))
    c["signal_quota"] = pd.to_numeric(c["signal_quota"], errors="coerce")
    c = (c.dropna(subset=["closing", "signal_quota"])
         .sort_values("updated_at")
         .drop_duplicates(subset=["match_id", "esito"], keep="last"))
    c["clv"] = c["signal_quota"] / c["closing"] - 1.0
    ne = c[c["clv"].abs() > 1e-12]

    p = preds[preds["status"].isin(PLAYABLE)].copy()
    p["profit"] = pd.to_numeric(p["profit"], errors="coerce")
    p = (p.dropna(subset=["profit"]).sort_values("created_at")
         .drop_duplicates(subset=["match_id", "esito"], keep="first"))

    m = ne.merge(p[["match_id", "esito", "profit"]], on=["match_id", "esito"])
    if m.empty:
        return {"status": "NON VERIFICABILE", "joined": 0}

    out = {"joined": int(len(m)), "non_echo": int(len(ne))}
    for sign, key in ((1, "positive"), (-1, "negative")):
        grp = m[np.sign(m["clv"]) == sign]
        vals = grp["profit"].tolist()
        ci = stats.bootstrap_mean_ci(vals, clusters=grp["match_id"].tolist())
        out[key] = {"n": int(len(grp)), "clusters": int(grp["match_id"].nunique()),
                    "roi": float(np.mean(vals)) if vals else None,
                    "ci95": [ci["lo"], ci["hi"]],
                    "status": stats.classify({"n": len(grp),
                                               "roi": float(np.mean(vals)) if vals else None,
                                               "roi_ci95": ci})}
    pos, neg = out.get("positive", {}), out.get("negative", {})
    if (pos.get("roi") is not None and neg.get("roi") is not None):
        out["verdict"] = ("CLV ordina i risultati (coorte positiva migliore)"
                          if pos["roi"] > neg["roi"] else
                          "CLV NON ordina i risultati: non e' un predittore")
    return out


def cell_scan(preds: pd.DataFrame, markets=MARKETS, min_n: int = 25) -> dict:
    """Griglia ex ante mercato x quota x edge, con Bonferroni.

    Le celle sono definite PRIMA di guardare i risultati (le stesse soglie per
    tutti i mercati). Il numero di test e' dichiarato: se nessuna cella supera
    la soglia corretta, allora il "vincitore" che si vede nel campione e'
    rumore - non la strategia giusta da adottare.
    """
    p = preds[preds["status"].isin(PLAYABLE)].copy()
    p["profit"] = pd.to_numeric(p["profit"], errors="coerce")
    p["quota"] = pd.to_numeric(p["quota"], errors="coerce")
    p["market_edge"] = pd.to_numeric(p["market_edge"], errors="coerce")
    p = p.dropna(subset=["profit", "quota"]).sort_values("created_at")
    p = p.drop_duplicates(subset=["match_id", "esito"], keep="first")

    cells, tested = [], 0
    for mkt in markets:
        sub = p[p["mercato"] == mkt]
        if sub.empty:
            continue
        for qlo, qhi in zip(ODDS_EDGES[:-1], ODDS_EDGES[1:]):
            for elo, ehi in zip(EDGE_EDGES[:-1], EDGE_EDGES[1:]):
                grp = sub[(sub["quota"] >= qlo) & (sub["quota"] < qhi)
                          & (sub["market_edge"] >= elo) & (sub["market_edge"] < ehi)]
                if len(grp) < min_n:
                    continue
                tested += 1
                s = _summ(grp, f"{mkt} q{qlo}-{qhi if qhi < 90 else '+'} e{elo}-{ehi}")
                t = s.get("t")
                cells.append({
                    "market": mkt, "odds": [qlo, qhi], "edge": [elo, ehi],
                    "n": s["n"], "roi": s.get("roi"),
                    "ci95": [s["roi_ci95"]["lo"], s["roi_ci95"]["hi"]],
                    "t": t, "p_two_sided": _p(t),
                    "hit_rate": s.get("hit_rate"), "status": s["status"],
                })
    if not tested:
        return {"status": "NON VERIFICABILE", "cells": [], "tests": 0}
    alpha = 0.05 / tested
    for c in cells:
        c["survives_bonferroni"] = bool(
            c["p_two_sided"] is not None and c["p_two_sided"] < alpha
            and (c["roi"] or 0) > 0)
    survivors = [c for c in cells if c["survives_bonferroni"]]
    best = max(cells, key=lambda c: (c["roi"] or -9))
    return {
        "tests": tested, "alpha_bonferroni": alpha,
        "cells": sorted(cells, key=lambda c: -(c["roi"] or -9)),
        "best_raw": best,
        "survivors": len(survivors),
        "verdict": ("nessuna cella sopravvive alla correzione per test multipli"
                    if not survivors else f"{len(survivors)} celle sopravvivono"),
    }


def _p(t) -> Optional[float]:
    """p-value a due code da t (approssimazione normale, n grande)."""
    if t is None:
        return None
    try:
        from scipy import stats as sps
        return float(2 * (1 - sps.norm.cdf(abs(float(t)))))
    except Exception:
        import math
        return float(math.erfc(abs(float(t)) / math.sqrt(2)))


def ev_calibration(preds: pd.DataFrame, bins=(0.0, 0.02, 0.04, 0.06, 0.10, 0.20, 9.0)) -> dict:
    """L'EV del modello e' calibrato con la REALTA'? Il test che conta.

    Se l'EV atteso (media) e' stabilmente sopra il ROI realizzato, il modello e'
    sovrastimato e NESSUNA soglia lo salva (alzare la soglia sposta la
    selezione su edge ancora piu' gonfiati). Se invece l'EV e' calibrato, il
    problema e' solo il campione. Le due diagnosi portano a decisioni opposte.
    """
    p = preds[preds["status"].isin(PLAYABLE)].copy()
    p["profit"] = pd.to_numeric(p["profit"], errors="coerce")
    p["ev"] = pd.to_numeric(p["ev"], errors="coerce")
    p["prob"] = pd.to_numeric(p["prob"], errors="coerce")
    # UNA sola popolazione per il totale e per le fasce: se le righe senza EV
    # restassero nel totale, "atteso" (media sulle fasce) e "realizzato"
    # (ROI su tutte) misurerebbero due campioni diversi e il gap mentirebbe.
    p = p.dropna(subset=["profit", "ev"]).sort_values("created_at")
    p = p.drop_duplicates(subset=["match_id", "esito"], keep="first")

    rows = []
    for lo, hi in zip(bins[:-1], bins[1:]):
        grp = p[(p["ev"] >= lo) & (p["ev"] < hi)]
        if len(grp) < 10:
            continue
        s = _summ(grp, f"ev {lo}-{hi if hi < 9 else '+'}")
        rows.append({
            "ev_band": [lo, hi], "n": s["n"],
            "expected_roi": float(grp["ev"].mean()),
            "realized_roi": s.get("roi"),
            "gap_pp": ((s.get("roi") or 0) - float(grp["ev"].mean())) * 100,
            "ci95": [s["roi_ci95"]["lo"], s["roi_ci95"]["hi"]],
        })

    calib = []
    probs = (0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 1.01)
    for lo, hi in zip(probs[:-1], probs[1:]):
        grp = p[(p["prob"] >= lo) & (p["prob"] < hi)]
        if len(grp) < 10:
            continue
        decided = grp[grp["esito_finale"].isin(["won", "lost"])]
        if not len(decided):
            continue
        wins = int((decided["esito_finale"] == "won").sum())
        lo_ci, hi_ci = stats.wilson_ci(wins, len(decided))
        calib.append({
            "prob_band": [lo, hi], "n": int(len(decided)),
            "predicted": float(grp["prob"].mean()),
            "observed": wins / len(decided),
            "gap_pp": (wins / len(decided) - float(grp["prob"].mean())) * 100,
            "observed_ci95": [lo_ci, hi_ci],
        })

    total = _summ(p, "tutte le giocabili")
    exp = float(p["ev"].mean()) if len(p) else None
    return {
        "n": total["n"], "expected_roi": exp,
        "realized_roi": total.get("roi"),
        "by_ev": rows, "by_prob": calib,
        "verdict": _calibration_verdict(rows, total, exp),
    }


def _calibration_verdict(rows, total, exp=None) -> str:
    if not rows or not total.get("n"):
        return "NON VERIFICABILE"
    if exp is None:
        exp = sum(r["expected_roi"] * r["n"] for r in rows) / sum(r["n"] for r in rows)
    real = total.get("roi") or 0.0
    gap = (real - exp) * 100
    if gap < -3:
        return (f"EV SOVRASTIMATO: atteso {exp*100:.1f}% contro realizzato "
                f"{real*100:.1f}% (gap {gap:.1f}pp) - alzare la soglia non salva")
    if gap > 3:
        return (f"EV SOTTOSTIMATO: atteso {exp*100:.1f}% contro realizzato "
                f"{real*100:.1f}% (gap +{gap:.1f}pp)")
    return (f"EV CALIBRATO entro 3pp: atteso {exp*100:.1f}% contro realizzato "
            f"{real*100:.1f}% - il problema e' il campione, non la stima")


def ev_monotonicity(preds: pd.DataFrame, n_boot: int = 2000,
                    seed: int = 7) -> dict:
    """L'EV ORDINA i risultati? (Spearman ev vs P/L, bootstrap a cluster)

    Se la correlazione e' nulla o negativa, l'EV non e' una misura di valore:
    e' un punteggio che seleziona. E' il test piu' scomodo del laboratorio,
    perche' se fallisce non si aggiusta con una soglia.
    """
    import random

    from scipy import stats as sps

    p = preds[preds["status"].isin(PLAYABLE)].copy()
    for col in ("profit", "ev"):
        p[col] = pd.to_numeric(p[col], errors="coerce")
    p = p.dropna(subset=["profit", "ev"]).sort_values("created_at")
    p = p.drop_duplicates(subset=["match_id", "esito"], keep="first")
    if len(p) < 20:
        return {"status": "NON VERIFICABILE", "n": int(len(p))}
    rho = float(sps.spearmanr(p["ev"], p["profit"]).statistic)
    groups: dict = {}
    for _, r in p.iterrows():
        groups.setdefault(r["match_id"], []).append((float(r["ev"]), float(r["profit"])))
    keys = list(groups)
    rng = random.Random(seed)
    boots = []
    for _ in range(int(n_boot)):
        sample = []
        for _ in range(len(keys)):
            sample.extend(groups[keys[rng.randrange(len(keys))]])
        if len(sample) < 10:
            continue
        xs = [s[0] for s in sample]
        ys = [s[1] for s in sample]
        if len(set(ys)) < 2:
            continue
        boots.append(float(sps.spearmanr(xs, ys).statistic))
    boots.sort()
    lo = boots[int(0.025 * len(boots))] if boots else None
    hi = boots[int(0.975 * len(boots)) - 1] if boots else None
    return {
        "n": int(len(p)), "spearman_rho": rho, "ci95": [lo, hi],
        "verdict": ("EV NON ORDINA i risultati (rho<=0): non e' una misura di valore"
                    if rho <= 0 else
                    "EV ordina debolmente i risultati (rho>0 ma intervallo da leggere)"),
    }


def stage_returns(preds: pd.DataFrame, bets: pd.DataFrame) -> dict:
    """Dove si perde lungo la catena: segnale -> giocabile -> ordine -> esito."""
    playable = preds[preds["status"].isin(PLAYABLE)].copy()
    closed = playable[pd.to_numeric(playable["profit"], errors="coerce").notna()]
    b = bets[(bets["mode"] == "live")].copy()
    b["stake"] = pd.to_numeric(b["stake"], errors="coerce").fillna(0.0)
    b["profit"] = pd.to_numeric(b["profit"], errors="coerce").fillna(0.0)
    decided = preds[pd.to_numeric(preds["profit"], errors="coerce").notna()]
    return {
        "rows_scanned": int(len(preds)),
        "decided_rows": int(len(decided)),
        "playable_rows": int(len(playable)),
        "playable_closed": int(len(closed)),
        "playable_share_of_decided": (len(playable) / len(decided)) if len(decided) else None,
        "live_bets": int(len(b)),
        "bets_per_playable_closed": (len(b) / len(closed)) if len(closed) else None,
        "ledger_roi_playable": stats.roi(
            pd.to_numeric(closed["profit"], errors="coerce").dropna().tolist()) if len(closed) else None,
        "live_roi": (float(b["profit"].sum()) / float(b["stake"].sum())
                     if b["stake"].sum() else None),
    }
