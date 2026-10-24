"""Esegue il laboratorio e scrive gli artefatti (JSON/CSV/log).

Uso:
    venv/bin/python -m research_isolated.lab.run_lab
    venv/bin/python research_isolated/lab/run_lab.py --json out/STRATEGY_RESULTS.json
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from . import backtest, diagnostics, loader, soft_book, stats
from .strategies import STRATEGIES, attach_clv, leak_audit, select

OUT_DIR = Path(__file__).resolve().parents[1] / "out"
CAPITAL_NOTE = {
    # Misurato sul container con il client del progetto (lettura, nessun
    # ordine): `python3 execution_engine.py --provider sxbet --balance`.
    # availableAmount 21100430 (6 decimali) = 21.10043 USDC, escrow 0.
    "measured_at": "2026-10-20T03:25Z",
    "sx_available_usdc": 21.1004,
    "sx_exposure_usdc": 0.0,
    "sx_escrow_usdc": 0.0,
    "source": "execution_engine.py --provider sxbet --balance (SX user/balance-v3), dal container",
    "exchange_constraints": {
        "min_order_usdc": 1.0,
        "min_ticket_motore_usdc": 1.0,
        "cap_per_ordine_pct": 0.12,
        "cap_per_ordine_usdc": 2.5321,
        "recinto_esposizione_pct": 0.70,
    },
    "kill_switch_patrimoniale": {
        "armed": True,
        "triggered_at": "2026-10-18T04:42:54Z",
        "wallet_equity_usdc": 23.6901,
        "threshold_usdc": 25.0,
        "consequence": "nessun ordine reale da 2 giorni (ultima bet 18/10 04:01)",
    },
}


def _clv_block(clv_df: pd.DataFrame) -> dict:
    from market_calib import clv_raw

    c = clv_df.copy()
    for col in ("closing_odds", "closing_quota", "pinnacle_quota"):
        c[col] = pd.to_numeric(c.get(col), errors="coerce")
    c["closing"] = c["closing_odds"].fillna(c["closing_quota"]).fillna(c["pinnacle_quota"])
    c["signal_quota"] = pd.to_numeric(c["signal_quota"], errors="coerce")
    c = c.dropna(subset=["closing", "signal_quota"]).sort_values("updated_at")
    c = c.drop_duplicates(subset=["match_id", "esito"], keep="last")
    vals, sources, non_echo = [], [], []
    for _, r in c.iterrows():
        v = clv_raw(float(r["signal_quota"]), float(r["closing"]))
        if v is None:
            continue
        vals.append(v)
        sources.append(str(r.get("closing_source") or "none"))
        if abs(v) > 1e-12:
            non_echo.append(v)
    if not vals:
        return {"status": "NON VERIFICABILE"}
    arr = np.array(vals)
    ci = stats.bootstrap_mean_ci(vals)
    ne = np.array(non_echo) if non_echo else np.array([])
    return {
        "n": len(vals),
        "mean": float(arr.mean()),
        "median": float(np.median(arr)),
        "share_positive": float((arr > 0).mean()),
        "p10": float(np.quantile(arr, 0.10)),
        "p90": float(np.quantile(arr, 0.90)),
        "ci95": [ci["lo"], ci["hi"]],
        "t": stats.t_stat(vals),
        "non_echo": {
            "n": int(len(ne)),
            "mean": float(ne.mean()) if len(ne) else None,
            "share_positive": float((ne > 0).mean()) if len(ne) else None,
        },
        "by_source": _group_means(vals, sources),
    }


def _group_means(vals, keys) -> dict:
    buckets: dict = {}
    for v, k in zip(vals, keys):
        buckets.setdefault(k, []).append(float(v))
    out = {}
    for k, arr in buckets.items():
        ci = stats.bootstrap_mean_ci(arr)
        out[k] = {"n": len(arr), "mean": float(np.mean(arr)),
                  "ci95": [ci["lo"], ci["hi"]]}
    return out


def _ledger_block(bets_df: pd.DataFrame) -> dict:
    b = bets_df.copy()
    b["stake"] = pd.to_numeric(b["stake"], errors="coerce").fillna(0.0)
    b["profit"] = pd.to_numeric(b["profit"], errors="coerce").fillna(0.0)
    b["price"] = pd.to_numeric(b["price"], errors="coerce")
    out = {"n_bets": int(len(b)), "stake_total": float(b["stake"].sum()),
           "pl": float(b["profit"].sum()),
           "roi": float(b["profit"].sum() / b["stake"].sum()) if b["stake"].sum() else None,
           "by_market": {}}
    for mkt, grp in b.groupby("mercato"):
        out["by_market"][mkt] = {
            "n": int(len(grp)), "stake": float(grp["stake"].sum()),
            "pl": float(grp["profit"].sum()),
            "roi": float(grp["profit"].sum() / grp["stake"].sum()) if grp["stake"].sum() else None,
            "avg_price": float(grp["price"].mean()) if grp["price"].notna().any() else None,
            "won": int((grp["esito_finale"] == "won").sum()),
            "lost": int((grp["esito_finale"] == "lost").sum()),
            "push": int((grp["esito_finale"] == "push").sum()),
        }
    ci = stats.bootstrap_mean_ci((b["profit"] / b["stake"]).tolist(),
                                 clusters=b["match_id"].tolist())
    out["roi_ci95"] = [ci["lo"], ci["hi"]]
    # fascia di PREZZO PAGATO (domanda diversa dal ROI del segnale): il
    # report la cita, quindi deve stare nel JSON per essere riproducibile.
    out["price_bands"] = diagnostics.live_price_bands(bets_df)
    return out


def run() -> dict:
    audit = loader.audit()
    preds = loader.predictions()
    bets_df = loader.bets()
    clv_df = loader.clv()
    results_df = loader.results()
    snaps = loader.snapshots()

    out: dict = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "snapshot": audit,
        "capital": CAPITAL_NOTE,
        "ledger": _ledger_block(bets_df),
        "clv_process": _clv_block(clv_df),
        "strategies": {},
        "soft_book": {},
        "diagnostics": {},
        "leak_audit": leak_audit(preds),
        "protocol": {
            "split": "cronologico 70/30 su created_at",
            "min_validation_n": 30,
            "walk_forward": "parametri scelti solo sul passato, warmup 8 giorni",
            "dedup": "(match_id, esito), prima riga in ordine di created_at",
        },
    }

    for name, st in STRATEGIES.items():
        full = select(st, preds, dedup_match=True)
        # taglio calcolato sull'UNIVERSO della strategia (non sul dataset intero:
        # altrimenti la validation del 1X2 sono 15 righe e la griglia muore per
        # il calendario invece che per i dati)
        val_df, hold_df = backtest.chronological_split_for(preds, st.universe, 0.7)
        val_pick = select(st, val_df, dedup_match=True)
        hold_pick = select(st, hold_df, dedup_match=True)
        out["strategies"][name] = {
            "family": st.family,
            "description": st.description,
            "ex_ante": st.ex_ante,
            "params": st.params,
            "full": backtest.evaluate(full, clv_df, label=f"{name} (tutto)"),
            "validation": backtest.evaluate(val_pick, clv_df, label=f"{name} (validation)"),
            "holdout": backtest.evaluate(hold_pick, clv_df, label=f"{name} (holdout)"),
        }

    # griglia 1X2: scelta su validation, poi holdout intatto
    val_df, hold_df = backtest.chronological_split_for(
        preds, STRATEGIES["value_1x2"].universe, 0.7)
    grid = backtest.parameter_grid()
    chosen = backtest.select_on_validation(val_df, grid)
    hold_pick = (backtest.apply_params(hold_df, chosen["params"])
                 if chosen.get("params") else hold_df.iloc[0:0])
    wf = backtest.walk_forward(preds, grid)
    out["grid_1x2"] = {
        "chosen_on_validation": chosen,
        "holdout": backtest.evaluate(hold_pick, clv_df, label="griglia 1X2 (holdout)"),
        "walk_forward": backtest.evaluate(wf["picked"], clv_df, label="walk-forward 1X2"),
        "walk_forward_days": wf["days"],
        "choices": wf["choices"],
    }

    out["soft_book"] = soft_book.run(snaps, results_df)
    out["diagnostics"] = {
        "stage_returns": diagnostics.stage_returns(preds, bets_df),
        "live_vs_ledger": diagnostics.live_vs_ledger(preds, bets_df),
        "bands": diagnostics.band_table(preds),
        "ev_calibration": diagnostics.ev_calibration(preds),
        "ev_monotonicity": diagnostics.ev_monotonicity(preds),
        "clv_cohorts": diagnostics.clv_cohorts(preds, clv_df),
        "cells": diagnostics.cell_scan(preds),
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "STRATEGY_RESULTS.json").write_text(
        json.dumps(out, indent=2, ensure_ascii=False, default=str))
    _write_csv(out)
    _write_log(out)
    return out


def _rows(out: dict):
    def row(name, st, label, res):
        ci = res.get("roi_ci95") or {}
        return {
            "strategy": name, "family": st.family, "split": label,
            "status": res.get("status"), "n": res.get("n"),
            "roi_pct": None if res.get("roi") is None else round(res["roi"] * 100, 2),
            "roi_ci_lo_pct": None if ci.get("lo") is None else round(ci["lo"] * 100, 2),
            "roi_ci_hi_pct": None if ci.get("hi") is None else round(ci["hi"] * 100, 2),
            "hit_rate": None if res.get("hit_rate") is None else round(res["hit_rate"], 4),
            "avg_odds": None if res.get("avg_odds") is None else round(res["avg_odds"], 3),
            "max_dd_units": None if res.get("max_drawdown") is None else round(res["max_drawdown"], 2),
            "clv_mean_pct": None if not res.get("clv") else round(res["clv"]["mean"] * 100, 2),
            "clv_n": None if not res.get("clv") else res["clv"]["n"],
            "matches": res.get("matches"),
            "min_detectable_edge_pct": None if not (res.get("power") or {}).get(
                "min_detectable_edge") else round(res["power"]["min_detectable_edge"] * 100, 1),
            "required_n_for_2pct": (res.get("power") or {}).get("required_n_for_2pct"),
        }
    for name, block in out["strategies"].items():
        st = STRATEGIES[name]
        for label in ("full", "validation", "holdout"):
            yield row(name, st, label, block[label])
    g = out["grid_1x2"]
    yield {"strategy": "grid_1x2", "family": "value_betting", "split": "holdout",
           "status": g["holdout"].get("status"), "n": g["holdout"].get("n"),
           "roi_pct": None if g["holdout"].get("roi") is None else round(g["holdout"]["roi"] * 100, 2)}
    yield {"strategy": "grid_1x2", "family": "value_betting", "split": "walk_forward",
           "status": g["walk_forward"].get("status"), "n": g["walk_forward"].get("n"),
           "roi_pct": None if g["walk_forward"].get("roi") is None else round(g["walk_forward"]["roi"] * 100, 2)}


def _write_csv(out: dict):
    rows = list(_rows(out))
    df = pd.DataFrame(rows)
    df.to_csv(OUT_DIR / "STRATEGY_COMPARISON.csv", index=False)


def _write_log(out: dict):
    lines = ["QuotaVerace — laboratorio isolato", "=" * 60,
             f"generato: {out['generated_at']}",
             f"snapshot: {out['snapshot']['path']} (quick_check={out['snapshot']['quick_check']})",
             f"capitale SX disponibile: {out['capital']['sx_available_usdc']} USDC",
             "", "LEDGER LIVE",
             json.dumps(out["ledger"], indent=2, ensure_ascii=False, default=str),
             "", "CLV (processo)",
             json.dumps(out["clv_process"], indent=2, ensure_ascii=False, default=str),
             "", "STRATEGIE"]
    for name, block in out["strategies"].items():
        lines.append(f"- {name}: {block['description']}")
        for label in ("full", "validation", "holdout"):
            r = block[label]
            lines.append(f"    {label:10s} n={r.get('n')} roi={r.get('roi')} "
                         f"status={r.get('status')}")
    lines += ["", "GRIGLIA 1X2",
              json.dumps({k: v for k, v in out["grid_1x2"].items() if k != "choices"},
                         indent=2, ensure_ascii=False, default=str),
              "", "SOFT BOOK vs SHARP",
              json.dumps(out["soft_book"], indent=2, ensure_ascii=False, default=str),
              "", "DIAGNOSTICA — funnel",
              json.dumps(out["diagnostics"].get("stage_returns", {}),
                         indent=2, ensure_ascii=False, default=str),
              "", "DIAGNOSTICA — fasce di prezzo PAGATO (puntate reali)"]
    for r in out["ledger"].get("price_bands", []):
        lo, hi = r["ci95"]
        lines.append(f"  {r['band']:>8} n={r['n']:>3} partite={r.get('clusters')} "
                     f"roi={None if r['roi'] is None else round(r['roi'] * 100, 1)}% "
                     f"ci=[{round(lo * 100, 1)}, {round(hi * 100, 1)}] {r['status']}")
    ev = out["diagnostics"].get("ev_calibration", {})
    lines += ["", "DIAGNOSTICA — calibrazione EV",
              f"  atteso {round(ev.get('expected_roi', 0) * 100, 2)}% "
              f"realizzato {round(ev.get('realized_roi', 0) * 100, 2)}%"
              f" — {ev.get('verdict')}"]
    for r in ev.get("by_ev", []):
        lines.append(f"  EV {r['ev_band'][0]}-{r['ev_band'][1]}: n={r['n']} "
                     f"atteso={round(r['expected_roi'] * 100, 1)}% "
                     f"realizzato={round(r['realized_roi'] * 100, 1)}% "
                     f"gap={round(r['gap_pp'], 1)}pp")
    mono = out["diagnostics"].get("ev_monotonicity", {})
    lines += ["", "DIAGNOSTICA — monotonicita' EV",
              f"  spearman={mono.get('spearman_rho')} ci={mono.get('ci95')} "
              f"— {mono.get('verdict')}"]
    co = out["diagnostics"].get("clv_cohorts", {})
    lines += ["", "DIAGNOSTICA — coorti CLV (il CLV ordina i risultati?)",
              f"  agganciati {co.get('joined')} su {co.get('non_echo')} non-eco"]
    for key in ("positive", "negative"):
        b = co.get(key, {})
        lines.append(f"  {key:9s} n={b.get('n')} roi={b.get('roi')} "
                     f"ci={b.get('ci95')} {b.get('status')}")
    lines.append(f"  verdetto: {co.get('verdict')}")
    cells = out["diagnostics"].get("cells", {})
    lines += ["", "DIAGNOSTICA — celle ex ante (Bonferroni)",
              f"  testate {cells.get('tests')} | alpha {cells.get('alpha_bonferroni')} "
              f"| sopravvissute {cells.get('survivors')}",
              f"  {cells.get('verdict')}"]
    (OUT_DIR / "lab_log.txt").write_text("\n".join(lines) + "\n")


def main():
    ap = argparse.ArgumentParser(description="laboratorio di ricerca isolato")
    ap.add_argument("--json", default=None)
    args = ap.parse_args()
    out = run()
    if args.json:
        Path(args.json).write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"scritto {OUT_DIR/'STRATEGY_RESULTS.json'}")


if __name__ == "__main__":
    main()
