"""Backtest cronologico con holdout intatto e walk-forward anti-leakage.

Protocollo (dichiarato PRIMA di guardare i numeri):

1. **split cronologico** 70% validation / 30% holdout, per `created_at`;
2. i parametri si scelgono SOLO sulla validation (n minimo 30);
3. l'holdout si valuta UNA volta con i parametri scelti: nessun ritocco;
4. **walk-forward**: ogni giorno i parametri si scelgono solo sulle righe
   precedenti (finestra espandente, warmup minimo), poi si applicano al giorno.
   E' la misura che conta per una strategia che deve girare in produzione;
   il 70/30 serve a mostrare quanto l'holdout "fortunato" sia rumoroso.

Nessuna funzione qui dentro legge l'esito per filtrare: `profit` si usa solo
per misurare.
"""

from __future__ import annotations

import itertools
from typing import Optional

import numpy as np
import pandas as pd

from . import stats
from .strategies import Strategy, attach_clv, select


def _date(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, errors="coerce", utc=True)


def chronological_split(df: pd.DataFrame, frac: float = 0.7) -> tuple:
    d = df.copy()
    d["created_at"] = _date(d["created_at"])
    d = d.dropna(subset=["created_at"]).sort_values("created_at")
    cut = int(len(d) * frac)
    return d.iloc[:cut].copy(), d.iloc[cut:].copy()


def chronological_split_for(df: pd.DataFrame, universe=None,
                            frac: float = 0.7) -> tuple:
    """Split 70/30 calcolato sull'UNIVERSO della strategia, non sul dataset.

    Perche' conta: tagliando il dataset INTERO, la validation del 1X2 scendeva a
    15 righe (il ledger e' dominato da altri mercati) e la griglia risultava
    "non verificabile" per il calendario, non per i dati. Qui la data di taglio
    e' il quantile dell'universo: ogni strategia ha un cut coerente col proprio
    flusso di segnali, e il holdout resta comunque un periodo SUCCESSIVO.
    """
    d = df.copy()
    d["created_at"] = _date(d["created_at"])
    d = d.dropna(subset=["created_at"]).sort_values("created_at")
    if universe is None or d.empty:
        return chronological_split(df, frac)
    mask = universe(d).fillna(False).astype(bool)
    base = d[mask]
    if len(base) < 4:
        return chronological_split(df, frac)
    idx = max(1, min(len(base) - 1, int(len(base) * frac)))
    cut = base["created_at"].iloc[idx]
    return d[d["created_at"] < cut].copy(), d[d["created_at"] >= cut].copy()


def power_block(res: dict) -> dict:
    """Potenza: che edge minimo questo campione PUO' distinguere da zero.

    Un intervallo che contiene lo zero non dice "nessun edge": dice che il
    campione non e' abbastanza numeroso per escluderlo. Questo blocco rende
    esplicito quanto un risultato nullo sia INFORMATIVO (e quanto no).
    """
    n, sd = res.get("n") or 0, res.get("sd")
    if not n or not sd:
        return {"n": n, "min_detectable_edge": None,
                "required_n_for_2pct": None,
                "required_n_for_1pct": None}
    return {
        "n": int(n),
        "min_detectable_edge": stats.min_detectable_effect(int(n), float(sd)),
        "required_n_for_2pct": stats.required_bets(0.02, float(sd)),
        "required_n_for_1pct": stats.required_bets(0.01, float(sd)),
    }


def evaluate(picked: pd.DataFrame, clv_df: Optional[pd.DataFrame] = None,
             label: str = "") -> dict:
    """Metriche di un insieme di righe chiuse, con intervalli al 95%."""
    if picked is None or len(picked) == 0:
        return {"label": label, "n": 0, "status": "NON VERIFICABILE"}
    rows = picked.copy()
    rows["profit"] = pd.to_numeric(rows["profit"], errors="coerce")
    rows = rows.dropna(subset=["profit"])
    wins = int((rows["esito_finale"] == "won").sum())
    losses = int((rows["esito_finale"] == "lost").sum())
    pushes = int((rows["esito_finale"] == "push").sum())
    decided = wins + losses
    summary = stats.summarize(rows["profit"].tolist(),
                              clusters=rows["match_id"].tolist(),
                              odds=pd.to_numeric(rows["quota"], errors="coerce").tolist(),
                              wins=wins if decided else None,
                              n=len(rows))
    summary["label"] = label
    summary["won"] = wins
    summary["lost"] = losses
    summary["push"] = pushes
    summary["push_share"] = pushes / len(rows) if len(rows) else None
    summary["matches"] = int(rows["match_id"].nunique())
    if "created_at" in rows:
        dt = _date(rows["created_at"])
        span = (dt.max() - dt.min()).total_seconds() / 86400 if dt.notna().any() else 0
        summary["days_span"] = round(float(span), 2)
        summary["bets_per_day"] = (len(rows) / span) if span > 0 else None
    if clv_df is not None:
        with_clv = attach_clv(rows, clv_df)
        vals = [float(v) for v in with_clv["clv"].tolist() if v is not None and pd.notna(v)]
        if vals:
            ci = stats.bootstrap_mean_ci(vals, with_clv.loc[with_clv["clv"].notna(),
                                                          "match_id"].tolist())
            summary["clv"] = {
                "n": len(vals),
                "mean": float(np.mean(vals)),
                "median": float(np.median(vals)),
                "positive_share": float(np.mean([v > 0 for v in vals])),
                "ci95": [ci["lo"], ci["hi"]],
                "source_mix": dict(with_clv["closing_source"].dropna()
                                   .value_counts().head(5)),
            }
    summary["status"] = stats.classify(summary)
    summary["power"] = power_block(summary)
    return summary


def parameter_grid(odds_max_opts=(1.80, 2.00), edge_opts=(0.01, 0.02, 0.03),
                   ev_opts=(0.015, 0.025), odds_min: float = 1.20) -> list:
    out = []
    for om, e, ev in itertools.product(odds_max_opts, edge_opts, ev_opts):
        out.append({"odds_min": odds_min, "odds_max": om, "min_edge": e,
                    "min_ev": ev})
    return out


def apply_params(df: pd.DataFrame, params: dict) -> pd.DataFrame:
    st = Strategy(
        "grid", "value_betting", "griglia", lambda d: (
            (d["mercato"] == "1X2")
            & (pd.to_numeric(d["quota"], errors="coerce") >= params["odds_min"])
            & (pd.to_numeric(d["quota"], errors="coerce") <= params["odds_max"])
            & (pd.to_numeric(d["market_prob"], errors="coerce") >= 0.5)
            & (pd.to_numeric(d["market_edge"], errors="coerce") >= params["min_edge"])
            & (pd.to_numeric(d["ev"], errors="coerce") >= params["min_ev"])
        ), dict(params))
    return select(st, df, dedup_match=True)


def select_on_validation(train: pd.DataFrame, grid: list,
                         min_n: int = 30) -> dict:
    """Sceglie i parametri SOLO sul train. Nessun guardo all'holdout."""
    best = None
    table = []
    for params in grid:
        picked = apply_params(train, params)
        res = evaluate(picked)
        row = {"params": params, "n": res.get("n", 0),
               "roi": res.get("roi"), "ci_lo": (res.get("roi_ci95") or {}).get("lo")}
        table.append(row)
        if row["n"] < min_n or row["roi"] is None:
            continue
        if best is None or row["roi"] > best["roi"]:
            best = row
    if best is None:
        return {"params": None, "table": table, "reason": "nessun set con n>=%d" % min_n}
    return {"params": best["params"], "table": table,
            "validation": {"n": best["n"], "roi": best["roi"],
                           "ci_lo": best["ci_lo"]}}


def walk_forward(df: pd.DataFrame, grid: Optional[list] = None,
                 warmup_days: int = 8, min_train: int = 40) -> dict:
    """Parametri scelti solo sul PASSATO, applicati al giorno successivo.

    Ritorna le righe selezionate (in `picked`) e la cronologia delle scelte:
    un indicatore di instabilita' dei parametri e' esso stesso un risultato.
    """
    d = df.copy()
    d["created_at"] = _date(d["created_at"])
    d = d.dropna(subset=["created_at"]).sort_values("created_at")
    if d.empty:
        return {"picked": d.iloc[0:0], "choices": [], "days": 0}
    grid = grid or parameter_grid()
    start = d["created_at"].min() + pd.Timedelta(days=warmup_days)
    days = sorted({ts.floor("D") for ts in d["created_at"] if ts >= start})
    picks = []
    choices = []
    for day in days:
        train = d[d["created_at"] < day]
        if len(train) < min_train:
            continue
        chosen = select_on_validation(train, grid, min_n=20)
        if not chosen.get("params"):
            continue
        today = d[(d["created_at"] >= day) & (d["created_at"] < day + pd.Timedelta(days=1))]
        if today.empty:
            continue
        picked = apply_params(today, chosen["params"])
        picks.append(picked)
        choices.append({"day": str(day.date()), "params": chosen["params"],
                        "n_train": int(len(train)), "n_today": int(len(picked)),
                        "roi_train": chosen["validation"]["roi"]})
    out = pd.concat(picks, ignore_index=True) if picks else d.iloc[0:0]
    return {"picked": out, "choices": choices, "days": len(days)}
