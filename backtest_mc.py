"""backtest_mc.py — Backtest walk-forward + Monte Carlo.

Simula le performance dello stack di puntata su dati storici chiusi:

  1. WALK-FORWARD (no look-ahead): a ogni finestra si addestra l'ensemble
     SOLO sulle giornate precedenti e si predicono SOLO le giornate future.
     Il modello usato e' lo stesso della produzione: XGBoost se disponibile,
     altrimenti Logistic Regression numpy-only.
  2. STAKING: Kelly frazionato dinamico (adaptive_staking: frazione 0.10-0.35,
     drawdown protection, cap 3%/5%) — lo stesso codice che piazza le puntate.
  3. MONTE CARLO: le sequenze di puntate vengono ri-ordinate N volte per
     stimare la DISTRIBUZIONE dei risultati: ROI atteso, mediana, Max
     Drawdown (picco->minimo del bankroll), P(riduzione) e range al 5-95%.

Perche' il Max Drawdown e' centrale: il ROI medio non basta a sopravvivere —
servono la variabilita' e le perdite consecutive (piu' 5 perdite di fila ->
adaptive_staking riduce gli stake; Monte Carlo mostra quanto spesso succede).

CLI:
  venv/bin/python backtest_mc.py                       # window default 30
  venv/bin/python backtest_mc.py --window 20 --sims 500
  venv/bin/python backtest_mc.py --bankroll 100 --json
  venv/bin/python backtest_mc.py --formato            # report testuale Telegram
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sys
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_WINDOW = 30        # minimo campioni per il primo addestramento
DEFAULT_SIMS = 1000        # percorsi Monte Carlo
DEFAULT_BANKROLL = 100.0
DEFAULT_MIN_EDGE = 0.03    # filtra le puntate: solo segnali value+

# Kelly dinamico: cap per status (coerente con adaptive_staking)
KELLY_CAP = {"value": 0.03, "strong_value": 0.05}

# Minimo ordine exchange (SX Bet = 1 USDC). Coerente con MIN_STAKE_EUR di
# auto_bet/adaptive_staking (11/09: era hardcoded 2.0 e con il cap severo
# 1% su bankroll 100 lo stake (1.0) veniva scartato -> simulazione a 0 bet).
MIN_STAKE = float(os.getenv("MIN_STAKE_EUR", "1.0"))


# ---------------------------------------------------------------------------
# Dati
# ---------------------------------------------------------------------------

def load_bets_rows(source: str = "all") -> List[Dict]:
    """Righe chiuse ordinate cronologicamente (dataset ml deduplicato).

    Aggiunge home/away per la normalizzazione esiti nel ML.
    """
    from ml_dataset import build_training_rows
    rows = build_training_rows(source=source)
    rows.sort(key=lambda r: r.get("settled_at") or "")
    return rows


# ---------------------------------------------------------------------------
# Walk-forward
# ---------------------------------------------------------------------------

def _walk_forward(rows: List[Dict], window: int) -> List[Dict]:
    """Addestra a finestre progressive e predice i campioni successivi.

    Ritorna le predizioni: {ml_prob, prob (blend base), quota, status, ev,
    label, settled_at}. Se il modello non e' ancora addestrabile (dataset
    piccolo), le predizioni iniziali usano la prob base (come in produzione:
    ensemble non addestrato -> prob Poisson/blend).
    """
    import numpy as np
    from ml_ensemble import EnsemblePredictor, _build_features

    preds: List[Dict] = []
    last_trained_at = -1
    #: Modello dell'EPOCA corrente: `rows[:last_trained_at]` non cambia finche'
    #: non scatta il prossimo retrain, quindi il modello e' lo stesso per tutte
    #: le righe dell'epoca. Si conserva invece di ricostruirlo.
    model = None

    for i in range(len(rows)):
        row = rows[i]

        # Retrain SOLO sui dati passati, quando la finestra cresce di 20
        if i >= window and (i - window) % 20 == 0:
            candidato = EnsemblePredictor()
            metrics = candidato.train(rows[:i])
            if metrics.get("status") == "trained":
                last_trained_at = i
                # E' addestrato ESATTAMENTE sul campione che serve alle
                # predizioni di questa epoca: si riusa (vedi sotto).
                model = candidato

        base_prob = float(row.get("prob") or row.get("prob_1") or 0.5)
        if last_trained_at < 0 or model is None:
            ml_prob = base_prob
        else:
            # ⚠️ Qui si riaddestrava il modello AD OGNI RIGA sullo stesso
            # campione: costo O(n^2) a parita' di risultato (train e' una
            # funzione deterministica del slice). Misurato il 29/09/2026: un
            # solo file di test (7 casi) impiegava 2:47 e la regressione
            # completa sforava il budget della shell, sembrando BLOCCATA.
            features = _build_features(row)
            ml_prob = float(model.lr_model.predict_proba(
                np.array([features]))[0])

        # Ensemble: media ponderata (peso default 0.4, come produzione pre-metrica)
        # NOTA: qui usiamo il peso fisso 0.35 per semplicita' e riproducibilita'.
        ens_prob = 0.65 * base_prob + 0.35 * ml_prob

        preds.append({
            "prob": ens_prob,
            "base_prob": base_prob,
            "quota": float(row.get("quota") or 0),
            "status": row.get("status") or "value",
            "ev": float(row.get("ev") or 0.0),
            "label": int(row.get("label_ml") or 0),
            "settled_at": row.get("settled_at") or "",
        })
    return preds


def _select_bets(preds: List[Dict], min_edge: float = DEFAULT_MIN_EDGE) -> List[Dict]:
    """Seleziona le puntate del backtest: EV positive e quota valide."""
    bets = []
    for p in preds:
        q = p["quota"]
        if not q or q <= 1.0:
            continue
        ev = p["prob"] * q - 1.0
        if ev < min_edge:
            continue
        bets.append({**p, "ev_bt": ev})
    return bets


# ---------------------------------------------------------------------------
# Kelly dinamico (usa adaptive_staking, lo stesso codice di produzione)
# ---------------------------------------------------------------------------

def _kelly_stake(prob: float, odds: float, status: str,
                 bankroll: float, peak: float) -> Dict:
    """Stake Kelly dinamico: fallback 1/4 Kelly con cap se adaptive assente."""
    try:
        from adaptive_staking import adaptive_stake
        return adaptive_stake(bankroll=bankroll, prob=prob, odds=odds,
                              status=status, peak_bankroll=peak)
    except ImportError:
        b = max(1e-9, odds - 1.0)
        kelly = max(0.0, (prob * odds - 1.0) / b)
        frac = min(0.25, 0.10 + 0.15 * (1.0 if status == "strong_value" else 0.0))
        stake = bankroll * min(kelly * frac, KELLY_CAP.get(status, 0.03))
        return {"stake": round(stake, 2), "kelly_fraction": frac,
                "reason": "fallback"}


# ---------------------------------------------------------------------------
# Simulazione + Monte Carlo
# ---------------------------------------------------------------------------

def _simulate_sequence(bets: List[Dict], bankroll0: float,
                       order: Optional[List[int]] = None) -> Dict:
    """Simula la sequenza di puntate (o un riordino) e ritorna le metriche."""
    bankroll = bankroll0
    peak = bankroll0
    max_dd = 0.0
    pnl = 0.0
    stakes = 0.0
    busted = False
    n_bets = 0

    seq = bets if order is None else [bets[i] for i in order]
    for b in seq:
        if bankroll < MIN_STAKE:               # sotto il minimo ordine
            busted = True
            break
        res = _kelly_stake(b["prob"], b["quota"], b["status"],
                           bankroll, peak)
        stake = min(float(res.get("stake") or 0.0), bankroll)
        if stake < MIN_STAKE:                  # minimo ordine exchange (1 USDC)
            continue
        n_bets += 1
        stakes += stake
        if b["label"] == 1:
            profit = stake * (b["quota"] - 1.0)
        else:
            profit = -stake
        bankroll += profit
        pnl += profit
        peak = max(peak, bankroll)
        max_dd = max(max_dd, (peak - bankroll) / peak * 100.0)
        if bankroll <= 0:
            busted = True
            break

    roi = (pnl / stakes * 100.0) if stakes > 0 else 0.0
    return {"pnl": round(pnl, 2), "roi": round(roi, 2),
            "max_dd": round(max_dd, 2), "final": round(bankroll, 2),
            "n_bets": n_bets, "staked": round(stakes, 2), "busted": busted}


def monte_carlo(bets: List[Dict], bankroll0: float = DEFAULT_BANKROLL,
                sims: int = DEFAULT_SIMS, seed: int = 42) -> Dict:
    """Ri-ordina le puntate N volte (stessi eventi, ordine casuale) e
    stima la distribuzione di ROI e MaxDD.

    Nota metodologica: il riordino preserva l'insieme dei risultati ma
    cambia la SEQUENZA (cioe' la variabilita' di percorso e i drawdown) —
    e' la variabilita' che ci interessa per il rischio di rovina.
    """
    if not bets:
        return {"sims": sims, "n_bets": 0}
    rng = random.Random(seed)
    base = _simulate_sequence(bets, bankroll0)
    dd_list: List[float] = []
    roi_list: List[float] = []
    busts = 0
    streak5 = 0
    for _ in range(sims):
        order = list(range(len(bets)))
        rng.shuffle(order)
        r = _simulate_sequence(bets, bankroll0, order=order)
        dd_list.append(r["max_dd"])
        roi_list.append(r["roi"])
        if r["busted"]:
            busts += 1
        if _max_consecutive_losses(bets, order) >= 5:
            streak5 += 1

    dd_list.sort()
    roi_list.sort()
    n = len(dd_list)

    def _pct(arr, p):
        return arr[min(n - 1, int(p * n))] if n else 0.0

    return {
        "sims": sims,
        "n_bets": base["n_bets"],
        "roi_base": base["roi"],
        "pnl_base": base["pnl"],
        "staked_base": base["staked"],
        "roi_mediana": round(_pct(roi_list, 0.5), 2),
        "roi_p5": round(_pct(roi_list, 0.05), 2),
        "roi_p95": round(_pct(roi_list, 0.95), 2),
        "maxdd_base": base["max_dd"],
        "maxdd_mediana": round(_pct(dd_list, 0.5), 2),
        "maxdd_p95": round(_pct(dd_list, 0.95), 2),
        "bust_pct": round(busts / sims * 100.0, 2),
        "p_5perdite_di_fila": round(streak5 / sims * 100.0, 1),
    }


def _max_consecutive_losses(bets: List[Dict], order: List[int]) -> int:
    worst = 0
    cur = 0
    for i in order:
        if bets[i]["label"] == 1:
            cur = 0
        else:
            cur += 1
            worst = max(worst, cur)
    return worst


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def run_backtest_mc(window: int = DEFAULT_WINDOW, sims: int = DEFAULT_SIMS,
                    bankroll: float = DEFAULT_BANKROLL,
                    min_edge: float = DEFAULT_MIN_EDGE,
                    source: str = "all") -> Dict:
    """Pipeline completa: dati -> walk-forward -> selezione -> Monte Carlo."""
    rows = load_bets_rows(source=source)
    if len(rows) < 10:
        return {"status": "insufficient_data", "n": len(rows),
                "msg": "Servono almeno 10 righe chiuse per il backtest."}

    preds = _walk_forward(rows, window)
    bets = _select_bets(preds, min_edge=min_edge)
    if not bets:
        return {"status": "no_bets", "n_rows": len(rows),
                "msg": "Nessuna puntata supera la soglia EV nel backtest."}

    mc = monte_carlo(bets, bankroll0=bankroll, sims=sims)
    won = sum(1 for b in bets if b["label"] == 1)
    return {
        "status": "ok",
        "n_rows": len(rows),
        "window": window,
        "sims": sims,
        "bankroll": bankroll,
        "min_edge": min_edge,
        "n_bets": mc.get("n_bets", 0),
        "hit_rate": round(won / len(bets) * 100, 1),
        **mc,
    }


def format_backtest_report(res: Dict) -> str:
    """Report testuale (Telegram-ready, Markdown)."""
    if res.get("status") != "ok":
        return (f"📊 *BACKTEST ML+KELLY*\n"
                f"⚠️ {res.get('msg', 'dati insufficienti')} "
                f"(righe chiuse: {res.get('n', res.get('n_rows', 0))})")

    def pct(v):
        return f"{v:+.1f}%"

    lines = [
        "📊 *BACKTEST WALK-FORWARD + MONTE CARLO*",
        f"Dataset: {res['n_rows']} righe chiuse | window {res['window']} | "
        f"{res['sims']} simulazioni",
        f"Puntate selezionate (EV > {res['min_edge']*100:.0f}%): {res['n_bets']} "
        f"| hit {res['hit_rate']:.0f}%",
        "━" * 34,
        f"💹 *ROI atteso (base):* {pct(res['roi_base'])} "
        f"(P/L €{res['pnl_base']:+.2f} su €{res['staked_base']:.0f} giocate)",
        f"   ROI mediana {pct(res['roi_mediana'])} | "
        f"5-95%: [{pct(res['roi_p5'])} … {pct(res['roi_p95'])}]",
        "",
        f"📉 *MAX DRAWDOWN*",
        f"   base {res['maxdd_base']:.1f}% | mediana {res['maxdd_mediana']:.1f}% | "
        f"p95 {res['maxdd_p95']:.1f}%",
        f"💀 P(riduzione): {res['bust_pct']:.1f}% | "
        f"P(≥5 perdite di fila): {res['p_5perdite_di_fila']:.0f}%",
        "━" * 34,
        "📌 Il MaxDD p95 e' il numero chiave: dimensiona il bankroll perché "
        "una perdita di quel livello sia sopravvivibile.",
    ]
    return "\n".join(lines)


# --- CLI ---

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Backtest ML+Kelly con Monte Carlo")
    ap.add_argument("--window", type=int, default=DEFAULT_WINDOW)
    ap.add_argument("--sims", type=int, default=DEFAULT_SIMS)
    ap.add_argument("--bankroll", type=float, default=DEFAULT_BANKROLL)
    ap.add_argument("--min-edge", type=float, default=DEFAULT_MIN_EDGE)
    ap.add_argument("--source", choices=["all", "predictions", "bets"],
                    default="all")
    ap.add_argument("--formato", action="store_true",
                    help="Report testuale Telegram-ready")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    res = run_backtest_mc(window=args.window, sims=args.sims,
                          bankroll=args.bankroll, min_edge=args.min_edge,
                          source=args.source)
    if args.json:
        print(json.dumps(res, indent=2, ensure_ascii=False))
    elif args.formato:
        print(format_backtest_report(res))
    else:
        print(json.dumps(res, indent=2, ensure_ascii=False))
    return 0 if res.get("status") == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
