"""Soft book vs sharp: l'unica strategia di valore con dati STORICI indipendenti.

Cosa rende questo test diverso dal resto del ledger: le probabilita' fair NON
vengono dal modello del progetto ma dal **de-vig di Pinnacle** su uno snapshot
reale (`price_snapshots`), e il prezzo giocato e' quello di un ALTRO bookmaker
registrato nello stesso giro di lettura. E' la definizione operativa di
"value betting con probabilita' sharp" (famiglia 1 della missione), applicata a
dati che non sono stati prodotti dal nostro modello.

Limiti dichiarati, misurati nel report:
* il campione e' piccolo (poche decine di partite con Pinnacle completo e un
  soft book allo stesso istante): il verdetto e' quasi sempre DA VERIFICARE;
* `price_snapshots` non contiene i prezzi SX eseguibili: il prezzo giocato e'
  quello del book ritrovato nel payload the-odds-api, non quello dell'exchange.
  Un edge qui NON e' ancora un edge eseguibile su SX.
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from . import stats

SHARP = "pinnacle"
TIME_WINDOW_S = 900  # stesso giro di lettura (minuti, non ore)


def _settle(result, esito) -> Optional[str]:
    if result is None or (isinstance(result, float) and np.isnan(result)):
        return None
    r = str(result).strip().upper()
    e = str(esito).strip().upper()
    if r in ("1", "X", "2"):
        return "won" if e == r else "lost"
    return None


def build_observations(snaps: pd.DataFrame, results: pd.DataFrame,
                       window_s: int = TIME_WINDOW_S) -> pd.DataFrame:
    """Una riga per (partita, esito, book) con EV calcolato sulla fair sharp.

    La fair viene de-vigata con lo stesso `market_calib` della produzione
    (metodo Shin, default dell'oracolo dal 02/10) e richiede il mercato
    COMPLETO: con 2 esiti su 3 il margine dell'esito mancante verrebbe
    attribuito in silenzio agli altri (fail-closed, come `load_oracle`).
    """
    from market_calib import devig

    if snaps is None or snaps.empty:
        return pd.DataFrame()
    s = snaps.copy()
    s["price"] = pd.to_numeric(s["price"], errors="coerce")
    s["book"] = s["bookmaker"].astype(str).str.strip().str.lower()
    s["recorded_at"] = pd.to_datetime(s["recorded_at"], errors="coerce", utc=True)
    s = s.dropna(subset=["price", "recorded_at", "match_id", "esito"])
    s = s[s["price"] > 1.0]
    res = results.copy()
    res["result"] = res["result"].astype(str).str.strip()
    res = res.dropna(subset=["result"])
    keep = set(res["match_id"])
    s = s[s["match_id"].isin(keep)]

    rows = []
    for match_id, grp in s.groupby("match_id"):
        sharp = grp[grp["book"] == SHARP]
        if sharp.empty:
            continue
        # ultimo snapshot sharp: e' il piu' vicino al kickoff disponibile
        ts = sharp["recorded_at"].max()
        sharp_now = sharp[sharp["recorded_at"] == ts]
        by_out = (sharp_now.sort_values("price").drop_duplicates("esito", keep="last"))
        outcomes = list(by_out["esito"])
        if len(outcomes) < 2:
            continue
        odds = [float(o) for o in by_out["price"]]
        fair = devig(odds, method="shin")
        if len(fair) != len(odds):
            continue
        fair_map = dict(zip(outcomes, fair))
        universe = grp[(grp["recorded_at"] >= ts - pd.Timedelta(seconds=window_s))
                       & (grp["recorded_at"] <= ts + pd.Timedelta(seconds=window_s))]
        for _, row in universe.iterrows():
            p = fair_map.get(row["esito"])
            if p is None:
                continue
            ev = p * float(row["price"]) - 1.0
            rows.append({
                "match_id": match_id, "esito": row["esito"], "book": row["book"],
                "price": float(row["price"]), "p_fair": float(p), "ev": float(ev),
                "recorded_at": ts,
            })
    obs = pd.DataFrame(rows)
    if obs.empty:
        return obs
    obs = obs.merge(res[["match_id", "result", "league"]], on="match_id", how="left")
    obs["outcome"] = [_settle(r, e) for r, e in zip(obs["result"], obs["esito"])]
    obs["profit"] = np.where(obs["outcome"] == "won", obs["price"] - 1.0,
                             np.where(obs["outcome"] == "lost", -1.0, 0.0))
    obs = obs[obs["outcome"].notna()]
    return obs


def best_price_per_selection(obs: pd.DataFrame) -> pd.DataFrame:
    """Una sola opportunita' per (partita, esito): il prezzo MIGLIORE.

    Senza questo passaggio lo stesso match pesa quante sono le righe dei book:
    la numerosita' sembrerebbe alta e l'intervallo falso-stretto.
    """
    if obs.empty:
        return obs
    idx = obs.groupby(["match_id", "esito"])["price"].idxmax()
    return obs.loc[idx].reset_index(drop=True)


def evaluate_threshold(obs: pd.DataFrame, min_ev: float,
                       exclude_sharp: bool = True) -> dict:
    pool = obs[obs["book"] != SHARP] if exclude_sharp else obs
    picked = pool[pool["ev"] >= min_ev]
    picked = best_price_per_selection(picked)
    if picked.empty:
        return {"min_ev": min_ev, "n": 0, "status": "NON VERIFICABILE"}
    wins = int((picked["outcome"] == "won").sum())
    summary = stats.summarize(picked["profit"].tolist(),
                              clusters=picked["match_id"].tolist(),
                              odds=picked["price"].tolist(),
                              wins=wins, n=len(picked))
    summary.update({"min_ev": min_ev,
                    "mean_ev": float(picked["ev"].mean()),
                    "matches": int(picked["match_id"].nunique()),
                    "books": dict(picked["book"].value_counts().head(6))})
    summary["status"] = stats.classify(summary)
    return summary


def run(snaps: pd.DataFrame, results: pd.DataFrame,
        thresholds=(0.0, 0.01, 0.02, 0.03)) -> dict:
    obs = build_observations(snaps, results)
    out = {"observations": int(len(obs)),
           "matches": int(obs["match_id"].nunique()) if len(obs) else 0,
           "thresholds": []}
    if obs.empty:
        out["status"] = "NON VERIFICABILE"
        return out
    out["ev_distribution"] = {
        "mean": float(obs["ev"].mean()),
        "p10": float(obs["ev"].quantile(0.10)),
        "p50": float(obs["ev"].quantile(0.50)),
        "p90": float(obs["ev"].quantile(0.90)),
        "share_ev_ge_2pct": float((obs["ev"] >= 0.02).mean()),
    }
    for t in thresholds:
        out["thresholds"].append(evaluate_threshold(obs, t))
    return out
