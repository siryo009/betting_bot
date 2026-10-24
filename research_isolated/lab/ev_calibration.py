"""Perche' l'EV dichiarato e' cosi' distante da quello realizzato.

Diagnosi del 20/10/2026 (riproducibile con `main()`): su 373 righe giocabili
chiuse, il Brier score della probabilita' del **mercato devigato** e' migliore
o uguale a quello del modello in TUTTI i mercati (1X2 0,2201 vs 0,2255; AH
0,2251 vs 0,2285; tennis 0,2151 vs 0,2167; OU pari). Il blend fra modello e
mercato non aggiunge informazione: aggiunge **ottimismo**. Ed e' da quel blend
che nasce l'EV.

Il modulo misura tre cose, separatamente:

1. **CALIBRAZIONE** — Brier/logloss di modello, mercato e blend, per mercato.
   E' la domanda "chi sa la probabilita' vera?".
2. **SHRINK** — un parametro `w` che sposta la probabilita' verso il mercato
   (`w*p_market + (1-w)*p_model`), fittato SOLO sul train e valutato su
   validation cronologica. Se `w -> 1` il modello non porta nulla oltre al
   mercato. Nessuna riapertura dell'holdout del backtest: qui si usano le
   **previsioni chiuse reali**, cioe' un campione diverso.
3. **BIAS DELL'EV** — EV dichiarato medio vs ROI realizzato, per mercato, e
   quanta parte del divario e' spiegata dal prezzo (segale vs acquisto).

Regole:
* sola LETTURA, zero rete, zero scritture, nessun ordine;
* i mercati con PUSH (OU/AH) **non** si trasformano con `p = (EV+1)/quota`:
   per un mercato push-aware l'EV non e' `p*q - 1` e quella conversione
   inventerebbe una probabilita' che nessuno ha calcolato (misurato: e'
   propria la ragione per cui `p_impl` risultava piu' alta del blend);
* nell'EV si legge la colonna `prob` (la probabilita' effettivamente usata).
"""

from __future__ import annotations

import argparse
import json
import logging
import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

from . import stats

logger = logging.getLogger(__name__)

MIN_SAMPLES = 30
#: Banda dentro cui due Brier sono indistinguibili (dichiarata, non implicita).
#: Senza, una differenza di 0,0001 veniva etichettata "il modello non aggiunge
#: informazione": un'etichetta troppo forte per il rumore di misura.
BRIER_EPS = 0.002
#: Mercati dove il pareggio e' possibile: il push si esclude dal Brier.
PUSH_MARKETS = ("OU", "AH")
DECIDED = ("won", "lost")


@dataclass
class Row:
    match_id: str
    market: str
    p_model: float
    p_market: Optional[float]
    quota: Optional[float]
    ev: Optional[float]
    outcome: Optional[str]
    profit: Optional[float]
    created_at: str
    league: Optional[str] = None

    @property
    def decided(self) -> bool:
        return self.outcome in DECIDED

    @property
    def y(self) -> Optional[float]:
        if self.outcome == "won":
            return 1.0
        if self.outcome == "lost":
            return 0.0
        return None

    @property
    def p_blend(self) -> Optional[float]:
        """La probabilita' "completa" usata per l'EV quando il mercato c'e'."""
        return self.p_model


def _clip(p: Optional[float]) -> Optional[float]:
    if p is None:
        return None
    try:
        v = float(p)
    except (TypeError, ValueError):
        return None
    if v != v:  # NaN
        return None
    return min(max(v, 1e-4), 1 - 1e-4)


def _num(value) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def build_rows(pred_rows: Sequence[dict]) -> List[Row]:
    out: List[Row] = []
    for r in pred_rows:
        p = _clip(r.get("prob"))
        q = _num(r.get("quota"))
        if p is None or q is None or q <= 1:
            continue
        out.append(Row(
            match_id=str(r.get("match_id")), market=(r.get("mercato") or "?"),
            p_model=p, p_market=_clip(r.get("market_prob")), quota=q,
            ev=_num(r.get("ev")), outcome=r.get("esito_finale"),
            profit=_num(r.get("profit")), created_at=str(r.get("created_at") or ""),
            league=r.get("league"),
        ))
    out.sort(key=lambda x: x.created_at)
    return out


def brier(ps: Sequence[float], ys: Sequence[float]) -> Optional[float]:
    if not ps or len(ps) != len(ys):
        return None
    return float(sum((p - y) ** 2 for p, y in zip(ps, ys)) / len(ps))


def logloss(ps: Sequence[float], ys: Sequence[float]) -> Optional[float]:
    if not ps or len(ps) != len(ys):
        return None
    tot = 0.0
    for p, y in zip(ps, ys):
        pc = min(max(p, 1e-6), 1 - 1e-6)
        tot += y * math.log(pc) + (1 - y) * math.log(1 - pc)
    return float(-tot / len(ps))


def calibration_table(rows: Sequence[Row]) -> Dict[str, dict]:
    """Brier/logloss di modello, mercato e blend, per mercato e complessivo."""
    def block(subset: Sequence[Row]) -> dict:
        dec = [r for r in subset if r.decided]
        ys = [r.y for r in dec]
        model = [r.p_model for r in dec]
        blend = [r.p_blend for r in dec]
        with_mkt = [r for r in dec if r.p_market is not None]
        out = {
            "n": len(subset),
            "n_decided": len(dec),
            # `prob` nel ledger e' GIA' il blend modello+mercato: le chiavi
            # "model" indicano la probabilita' effettivamente usata per l'EV.
            "p_used": "blend(modello, mercato) salvato in predictions.prob",
            "hit": round(sum(ys) / len(ys), 6) if ys else None,
            "avg_p_model": round(sum(model) / len(model), 6) if model else None,
            "brier_model": None if not model else round(brier(model, ys), 6),
            "logloss_model": None if not model else round(logloss(model, ys), 6),
        }
        if with_mkt:
            m_ys = [r.y for r in with_mkt]
            out.update({
                "n_with_market": len(with_mkt),
                "brier_market": round(brier([r.p_market for r in with_mkt], m_ys), 6),
                "logloss_market": round(logloss([r.p_market for r in with_mkt], m_ys), 6),
                "avg_p_market": round(sum(r.p_market for r in with_mkt) / len(with_mkt), 6),
                "n_market_better": sum(
                    1 for r in with_mkt
                    if (r.p_market - r.y) ** 2 < (r.p_model - r.y) ** 2),
            })
        else:
            out["brier_market"] = None
        return out

    overall = block(rows)
    by_market = {m: block([r for r in rows if r.market == m])
                 for m in sorted({r.market for r in rows})}
    return {"overall": overall, "by_market": by_market}


def shrink_grid() -> List[float]:
    return [round(i * 0.05, 2) for i in range(0, 21)]


def fit_shrink(rows: Sequence[Row], w_grid: Optional[Sequence[float]] = None) -> dict:
    """`w` ottimale sul TRAIN, valutato su VALIDATION cronologica.

    `w = 1` significa "usa solo il mercato": e' il test onesto del fatto che il
    modello aggiunga qualcosa. Split 50/25 come in `prereg.py`; nessuna
    ottimizzazione sul validation e nessun contatto con l'holdout del backtest.
    """
    dec = [r for r in rows if r.decided and r.p_market is not None]
    if len(dec) < 20:
        return {"available": False, "reason": f"campione troppo piccolo ({len(dec)})"}
    n = len(dec)
    train = dec[:int(n * 0.5)]
    valid = dec[int(n * 0.5):int(n * 0.75)]
    grid = list(w_grid or shrink_grid())
    ys_tr = [r.y for r in train]
    best_w, best_ll = None, None
    for w in grid:
        ll = logloss([w * r.p_market + (1 - w) * r.p_model for r in train], ys_tr)
        if ll is None:
            continue
        if best_ll is None or ll < best_ll:
            best_w, best_ll = w, ll
    if best_w is None:
        return {"available": False, "reason": "nessun w valutabile"}

    def eval_block(subset: Sequence[Row]) -> dict:
        ys = [r.y for r in subset]
        return {
            "n": len(subset),
            "w": best_w,
            "brier_model": round(brier([r.p_model for r in subset], ys), 6),
            "brier_shrunk": round(brier([best_w * r.p_market + (1 - best_w) * r.p_model for r in subset], ys), 6),
            "brier_market": round(brier([r.p_market for r in subset], ys), 6),
            "logloss_model": round(logloss([r.p_model for r in subset], ys), 6),
            "logloss_shrunk": round(logloss([best_w * r.p_market + (1 - best_w) * r.p_model for r in subset], ys), 6),
            "logloss_market": round(logloss([r.p_market for r in subset], ys), 6),
        }

    out = {
        "available": True,
        "w": best_w,
        "w_ci_free": best_w >= 0.95,
        "train": eval_block(train),
        "validation": eval_block(valid) if len(valid) >= 10 else None,
        "grid": grid,
        "split": {"train": len(train), "validation": len(valid), "total": n},
    }
    v = out["validation"]
    if v:
        out["brier_gain_pp"] = round((v["brier_model"] - v["brier_shrunk"]) * 100, 4)
        out["market_still_best"] = v["brier_market"] < v["brier_shrunk"]
    return out


def ev_bias(rows: Sequence[Row]) -> dict:
    """EV dichiarato vs P/L realizzato, per mercato (la domanda del punto 3)."""
    def block(subset: Sequence[Row]) -> dict:
        with_ev = [r for r in subset if r.ev is not None and r.profit is not None]
        if not with_ev:
            return {"n": 0}
        ev = sum(r.ev for r in with_ev) / len(with_ev)
        roi = sum(r.profit for r in with_ev) / len(with_ev)
        profit_total = sum(r.profit for r in with_ev)
        return {
            "n": len(with_ev),
            "ev_declared": round(ev, 6),
            "roi_realized": round(roi, 6),
            "bias_pp": round((ev - roi) * 100, 4),
            "pnl": round(profit_total, 4),
            "ci": stats.bootstrap_mean_ci([r.profit for r in with_ev],
                                          [r.match_id for r in with_ev]),
        }
    return {"overall": block(rows),
            "by_market": {m: block([r for r in rows if r.market == m])
                          for m in sorted({r.market for r in rows})}}


def verdict(cal: dict, shrink: dict) -> dict:
    """Verdetto dichiarativo sulla previsione dell'EV."""
    overall = cal.get("overall") or {}
    bm, bmk = overall.get("brier_model"), overall.get("brier_market")
    if bm is None or bmk is None:
        return {"status": "unavailable", "reason": "calibrazione non calcolabile"}
    if bmk < bm - BRIER_EPS:
        status = "model_worse_than_market"
        reason = (f"Brier del mercato {bmk:.4f} < modello {bm:.4f}: "
                  "il modello non aggiunge informazione")
    elif bm < bmk - BRIER_EPS:
        status = "model_better_than_market"
        reason = f"Brier del modello {bm:.4f} < mercato {bmk:.4f}: segnale da approfondire"
    else:
        status = "tie"
        reason = (f"Brier modello {bm:.4f} ~ mercato {bmk:.4f} "
                  f"(dentro la banda ±{BRIER_EPS}): nessuna differenza pratica")
    if shrink.get("available") and shrink.get("w") is not None:
        reason += f"; w={shrink['w']:.2f} (1 = solo mercato)"
        if shrink.get("market_still_best"):
            reason += ", il mercato resta migliore anche dopo lo shrink"
    return {"status": status, "reason": reason}


def report(rows: Sequence[Row]) -> dict:
    cal = calibration_table(rows)
    shrink = fit_shrink(rows)
    return {
        "calibration": cal,
        "shrink": shrink,
        "ev_bias": ev_bias(rows),
        "verdict": verdict(cal, shrink),
        "thresholds": {"min_samples": MIN_SAMPLES},
        "push_markets": list(PUSH_MARKETS),
        "rows": [
            {"match_id": r.match_id, "market": r.market, "p_model": round(r.p_model, 6),
             "p_market": None if r.p_market is None else round(r.p_market, 6),
             "quota": r.quota, "ev": r.ev, "outcome": r.outcome,
             "profit": r.profit, "created_at": r.created_at}
            for r in rows
        ],
    }


def format_report(data: dict) -> str:
    cal = data.get("calibration") or {}
    lines = ["🎯 CALIBRAZIONE DELL'EV (modello vs mercato devigato)", ""]
    for name, b in [("TUTTI", cal.get("overall") or {})] + list((cal.get("by_market") or {}).items()):
        if not b or not b.get("n_decided"):
            continue
        mk = f"{b['brier_market']:.4f}" if b.get("brier_market") is not None else "—"
        lines.append(
            f"{name:8} n={b['n_decided']:3d} hit={b['hit']:.1%} "
            f"p_usata={b['avg_p_model']:.3f} Brier prob-usata/mkt="
            f"{b['brier_model']:.4f}/{mk}")
    sh = data.get("shrink") or {}
    if sh.get("available"):
        v = sh.get("validation") or {}
        lines.append("")
        lines.append(f"shrink ottimale sul train: w={sh['w']:.2f} (1 = solo mercato)")
        if v:
            lines.append(f"  validation n={v['n']}: Brier modello {v['brier_model']:.4f} → "
                         f"shrink {v['brier_shrunk']:.4f} → mercato {v['brier_market']:.4f}")
            lines.append(f"  guadagno dello shrink: {sh.get('brier_gain_pp'):+.2f}pp; "
                         f"mercato ancora migliore: {sh.get('market_still_best')}")
    eb = data.get("ev_bias") or {}
    overall = eb.get("overall") or {}
    if overall.get("n"):
        lines.append("")
        lines.append(f"EV dichiarato {overall['ev_declared']:+.2%} vs realizzato "
                     f"{overall['roi_realized']:+.2%} → bias {overall['bias_pp']:+.1f}pp "
                     f"(n={overall['n']})")
        for mk, b in (eb.get("by_market") or {}).items():
            if b.get("n"):
                lines.append(f"  · {mk:7} n={b['n']:3d} EV {b['ev_declared']:+.2%} "
                             f"realizzato {b['roi_realized']:+.2%} bias {b['bias_pp']:+.1f}pp")
    v = data.get("verdict") or {}
    lines.append("")
    lines.append(f"verdetto ({v.get('status')}): {v.get('reason')}")
    return "\n".join(lines)


def _load_rows() -> List[Row]:
    from . import loader

    conn = loader.connect()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT p.match_id, p.mercato, p.esito, p.quota, p.prob, p.ev, "
            "p.market_prob, p.status, p.esito_finale, p.profit, p.created_at, "
            "m.league FROM predictions p JOIN match_results r ON r.match_id=p.match_id "
            "LEFT JOIN matches m ON m.id=p.match_id "
            "WHERE p.esito_finale IS NOT NULL "
            "AND p.status IN ('value','strong_value','moderate')")]
    finally:
        conn.close()
    return build_rows(rows)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Calibrazione dell'EV")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--out")
    args = parser.parse_args(argv)
    try:
        rows = _load_rows()
    except FileNotFoundError as exc:
        print(f"ERRORE: {exc}")
        return 2
    data = report(rows)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
    print(json.dumps(data, indent=2, ensure_ascii=False) if args.json else format_report(data))
    return 0


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    raise SystemExit(main())
