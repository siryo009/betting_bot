"""stress_test_bankroll.py — Stress-test Monte Carlo del bankroll (OFFLINE).

Direttiva del proprietario (02/10/2026): misurare **Probability of Ruin** e
**Maximum Drawdown** sul *recinto* del bankroll, cioe' la configurazione di
rischio realmente attiva in produzione — non un bankroll teorico:

  stake fisso per ordine      `auto_bet.fixed_order_stake()`  (1.50 USDC)
  tetto per ordine            `auto_bet.ORDER_MAX_STAKE_USDC` (inviolabile)
  recinto di esposizione      `auto_bet.OPEN_EXPOSURE_CAP_PCT` (0.40)
                              -> max `floor(bankroll * 0.40 / stake)` ordini
                                 APERTI CONTEMPORANEAMENTE (8 con 30.40 USDC,
                                 cioe' 12.16 USDC di capitale impegnato)

Le tre costanti NON sono copiate: si importano da `auto_bet`, che e' l'unico
posto dove vivono (regola di progetto: una formula/soglia in un solo posto).
Se `auto_bet` non e' importabile (es. run isolato) si usano i default
DICHIARATI e il report lo segnala in `constants_source`.

Metodo (numpy vettorizzato, nessuna dipendenza nuova — `numba` NON serve):
la simulazione avanza a ROUND di `max_concurrent` puntate simultanee (il
recinto). Dentro un round, con `k` puntate aperte il P/L dipende SOLO dal
numero di vittorie, che e' `Binomiale(k, p)`: basta quindi **UNA uniforme per
percorso e per round**, mappata con una tabella inverse-CDF quantizzata
(precalcolata via `math.comb`, quindi esatta a meno di 1/Q).
E' **esattamente equivalente** alla matrice di esiti Bernoulli indipendenti
(stesso processo, stessa distribuzione) ma costa ~1/k draw casuali e non
alloca mai la matrice `(n_sims, n_bets)` (~400 MB): e' cio' che porta
100.000 percorsi x 500 puntate da ~20 s (prima versione, draw a matrice) a
~1 s. Con Q=65.536 livelli l'errore di quantizzazione e' ~1,5e-5, sotto il
rumore Monte Carlo di 100.000 percorsi.
Micro-ottimizzazioni misurate (63 round su 100k percorsi): buffer **float32**
preallocati con operazioni in-place (`out=`) invece di temporanei float64, e
il lookup risolto con un solo gather (`tabella[k, quantile]`) invece di una
`searchsorted` per round (che era ~30% del tempo).

Due nozioni di "rovina", entrambe dichiarate nel report perche' sono diverse:
  - `ruin_pct`        equity <= `--ruin-floor` (default 0.0, bancarotta contabile)
  - `inoperable_pct`  equity < stake: non c'e' piu' capitale per la prossima
                      puntata (la "rovina operativa" del recinto)

La matrice ha una riga per quota della griglia: per ognuna il **Win Rate di
pareggio** (`1/quota`, il break-even a stake fisso), il RoR e il MaxDD. Con
`--edge-pp` si sposta il win rate assunto sopra il pareggio (edge ipotetico),
cosi' la matrice mostra quanto margine serve perche' il drawdown resti
tollerabile — e sotto quale win rate il conto si spegne.

CLI:
  venv/bin/python scripts/stress_test_bankroll.py
  venv/bin/python scripts/stress_test_bankroll.py --sims 200000 --edge-pp 0.02
  venv/bin/python scripts/stress_test_bankroll.py --json
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

# --- Bootstrap del path: lo script vive in scripts/, i moduli in root -------
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import numpy as np  # noqa: E402

# --- Default DICHIARATI (fallback se auto_bet non e' importabile) -----------
DEFAULT_STAKE_USDC = 1.50
DEFAULT_MAX_STAKE_USDC = 1.50
DEFAULT_CAP_PCT = 0.40
#: Bankroll di riferimento della direttiva: 40% = 12.16 USDC -> 30.40 USDC.
DEFAULT_BANKROLL_USDC = 12.16 / DEFAULT_CAP_PCT

DEFAULT_SIMS = 100_000
DEFAULT_BETS = 500
DEFAULT_ODDS_GRID = (1.30, 1.45, 1.60, 1.80, 2.00, 2.20, 2.50)
DEFAULT_RUIN_FLOOR = 0.0
DEFAULT_SEED = 7


# ---------------------------------------------------------------------------
# Costanti di produzione (lette, mai copiate)
# ---------------------------------------------------------------------------

def _production_constants() -> Dict[str, Any]:
    """Stake/tetto/recinto REALI da `auto_bet`. Fallback dichiarato.

    Ritorna `{stake, max_stake, cap_pct, source}` dove `source` e' `auto_bet`
    (letto) oppure `default` (modulo non importabile): un numero diverso dal
    vero deve essere VISIBILE nel report, non indistinguibile.
    """
    out = {"stake": float(DEFAULT_STAKE_USDC),
           "max_stake": float(DEFAULT_MAX_STAKE_USDC),
           "cap_pct": float(DEFAULT_CAP_PCT),
           "source": "default"}
    try:
        import auto_bet as ab
        stake = ab.fixed_order_stake() if hasattr(ab, "fixed_order_stake") \
            else float(getattr(ab, "ORDER_FIXED_STAKE_USDC", DEFAULT_STAKE_USDC))
        cap = float(getattr(ab, "OPEN_EXPOSURE_CAP_PCT", DEFAULT_CAP_PCT))
        mx = float(getattr(ab, "ORDER_MAX_STAKE_USDC", DEFAULT_MAX_STAKE_USDC))
        out.update({"stake": float(stake) if stake and stake > 0 else float(DEFAULT_STAKE_USDC),
                    "max_stake": mx if mx and mx > 0 else float(DEFAULT_MAX_STAKE_USDC),
                    "cap_pct": cap if cap > 0 else float(DEFAULT_CAP_PCT),
                    "source": "auto_bet"})
    except Exception:
        pass
    return out


def max_concurrent(bankroll: float, stake: float, cap_pct: float) -> int:
    """Ordini APERTI contemporaneamente consentiti dal recinto (>= 1)."""
    if stake <= 0:
        return 1
    return max(1, int((float(bankroll) * float(cap_pct)) // float(stake)))


# ---------------------------------------------------------------------------
# Simulazione Monte Carlo (vettorizzata sui percorsi)
# ---------------------------------------------------------------------------

def _binomial_cdf(k: int, p: float) -> np.ndarray:
    """CDF esatta di Binomiale(k, p) — base della tabella inverse-CDF.

    L'ultimo elemento e' forzato a 1.0: la somma in floating point puo' dare
    0.9999999 e un quantile vicino a 1 finirebbe fuori dalla tabella.
    """
    p = min(max(float(p), 0.0), 1.0)
    pmf = np.array([math.comb(int(k), i) * p ** i * (1.0 - p) ** (int(k) - i)
                    for i in range(int(k) + 1)], dtype=np.float64)
    cdf = np.cumsum(pmf)
    if cdf.size:
        cdf[-1] = 1.0
    return cdf


#: Livelli massimi della tabella inverse-CDF (errore di quantizzazione ~1,5e-5).
Q_LEVELS = 65536
#: Tetto di memoria per la tabella (conc+1 righe x Q): con recinti enormi
#: (stake minuscolo / bankroll grande) si riduce Q invece di allocare GB.
Q_TABLE_BUDGET = 8_000_000


def _q_levels(conc: int) -> int:
    """Livelli di quantizzazione compatibili con il recinto (>= 64)."""
    return max(64, min(Q_LEVELS, Q_TABLE_BUDGET // (int(conc) + 1)))


def _inverse_cdf_table(conc: int, p: float, levels: int) -> np.ndarray:
    """Tabella `[k, quantile] -> vittorie` di Binomiale(k, p) per k in 0..conc.

    UNICO punto di campionamento: nel round si fa un solo gather
    `tabella[k, quantile]` invece di una `searchsorted` per percorso.
    """
    quantiles = (np.arange(levels, dtype=np.float64) + 0.5) / levels
    table = np.empty((conc + 1, levels), dtype=np.int8)
    for kv in range(conc + 1):
        table[kv] = np.searchsorted(_binomial_cdf(kv, p), quantiles,
                                    side="right").astype(np.int8)
    return table


def simulate(n_sims: int, n_bets: int, bankroll: float, stake: float,
             odds: float, win_prob: float, cap_pct: float,
             ruin_floor: float = DEFAULT_RUIN_FLOOR,
             seed: int = DEFAULT_SEED) -> Dict[str, Any]:
    """`n_sims` percorsi di `n_bets` puntate. Ritorna le metriche aggregate.

    I round hanno `max_concurrent` puntate SIMULTANEE (il recinto): l'esposizione
    e' quella reale, non una sequenza una-per-volta che il recinto non morde mai.
    """
    if n_sims <= 0 or n_bets <= 0:
        raise ValueError("n_sims e n_bets devono essere positivi")
    if stake <= 0 or odds <= 1.0:
        raise ValueError("stake > 0 e odds > 1 richiesti")
    win_prob = min(max(float(win_prob), 0.0), 1.0)

    rng = np.random.default_rng(int(seed))
    conc = max_concurrent(bankroll, stake, cap_pct)
    rounds = max(1, int(math.ceil(n_bets / conc)))
    levels = _q_levels(conc)
    table = _inverse_cdf_table(conc, win_prob, levels)

    # float32: l'equity di un recinto da ~30 USDC non ha bisogno di 15 cifre,
    # e dimezza il traffico di memoria (la voce di costo dominante).
    dt = np.float32
    equity = np.full(n_sims, float(bankroll), dtype=dt)
    peak = equity.copy()
    max_dd = np.zeros(n_sims, dtype=dt)
    ruined = np.zeros(n_sims, dtype=bool)
    inoperable = np.zeros(n_sims, dtype=bool)
    placed = np.zeros(n_sims, dtype=np.int64)

    stake_f = float(stake)
    span = dt(stake_f * float(odds))       # delta fra una vittoria e una sconfitta
    stake_c = dt(stake_f)
    inv_stake = dt(1.0 / stake_f)
    ruin_f = float(ruin_floor)

    # Buffer preallocati: zero temporanei per round.
    tmp = np.empty(n_sims, dtype=dt)
    quant_f = np.empty(n_sims, dtype=np.float64)
    quant_i = np.empty(n_sims, dtype=np.int64)
    kf = np.empty(n_sims, dtype=dt)
    k_buf = np.empty(n_sims, dtype=np.int64)
    tg = np.empty(n_sims, dtype=bool)

    for _ in range(rounds):
        # Quante puntate puo' aprire ogni percorso con l'equity che ha adesso.
        np.multiply(equity, inv_stake, out=tmp)
        np.floor(tmp, out=tmp)
        np.clip(tmp, 0.0, float(conc), out=tmp)
        k_buf[:] = tmp

        # Uniforme del round -> quantile -> vittorie (un solo gather).
        np.multiply(rng.random(n_sims), float(levels), out=quant_f)
        np.floor(quant_f, out=quant_f)
        np.clip(quant_f, 0.0, float(levels - 1), out=quant_f)
        quant_i[:] = quant_f
        wins = table[k_buf, quant_i]

        # P/L del round = wins*stake*odds - k*stake, tutto in-place.
        np.multiply(wins, span, out=tmp)
        np.multiply(k_buf, stake_f, out=kf)
        np.subtract(tmp, kf, out=tmp)
        np.add(equity, tmp, out=equity)
        placed += k_buf

        np.maximum(peak, equity, out=peak)
        np.subtract(peak, equity, out=tmp)
        np.divide(tmp, peak, out=tmp)
        np.maximum(max_dd, tmp, out=max_dd)

        np.less_equal(equity, ruin_f, out=tg)
        np.logical_or(ruined, tg, out=ruined)
        np.less(equity, stake_c, out=tg)
        np.logical_or(inoperable, tg, out=inoperable)

    equity = equity.astype(np.float64)
    max_dd = max_dd.astype(np.float64)

    return {
        "odds": round(float(odds), 4),
        "win_prob": round(win_prob, 6),
        "break_even_win_rate": round(1.0 / float(odds), 6),
        "edge_pp": round((win_prob - 1.0 / float(odds)) * 100.0, 3),
        "ruin_pct": round(float(ruined.mean()) * 100.0, 4),
        "inoperable_pct": round(float(inoperable.mean()) * 100.0, 4),
        "max_dd_mean_pct": round(float(max_dd.mean()) * 100.0, 3),
        "max_dd_p95_pct": round(float(np.percentile(max_dd, 95)) * 100.0, 3),
        "max_dd_worst_pct": round(float(max_dd.max()) * 100.0, 3),
        "equity_final_mean": round(float(equity.mean()), 4),
        "equity_final_p5": round(float(np.percentile(equity, 5)), 4),
        "equity_final_p95": round(float(np.percentile(equity, 95)), 4),
        "avg_bets_placed": round(float(placed.mean()), 2),
    }


def build_matrix(odds_grid: Sequence[float], *, n_sims: int, n_bets: int,
                 bankroll: float, stake: float, cap_pct: float,
                 edge_pp: float = 0.0, ruin_floor: float = DEFAULT_RUIN_FLOOR,
                 seed: int = DEFAULT_SEED) -> List[Dict[str, Any]]:
    """Una riga per quota: il win rate assunto e' il pareggio + `edge_pp`."""
    rows: List[Dict[str, Any]] = []
    for i, odds in enumerate(odds_grid):
        odds = float(odds)
        if odds <= 1.0:
            continue
        rows.append(simulate(n_sims=n_sims, n_bets=n_bets, bankroll=bankroll,
                             stake=stake, odds=odds,
                             win_prob=1.0 / odds + float(edge_pp),
                             cap_pct=cap_pct, ruin_floor=ruin_floor,
                             seed=int(seed) + i))
    return rows


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _hdr(title: str) -> str:
    return f"\n{title}\n" + "=" * len(title)


def format_report(env: Dict[str, Any], rows: List[Dict[str, Any]],
                  elapsed: float) -> str:
    """Report testuale: intestazione del recinto + matrice sintetica."""
    out = [_hdr("Stress-test Monte Carlo del bankroll (recinto di produzione)")]
    out.append(
        f"bankroll      : {env['bankroll']:.4f} USDC\n"
        f"stake/ordine  : {env['stake']:.2f} USDC "
        f"(tetto {env['max_stake']:.2f}, fonte costanti: {env['constants_source']})\n"
        f"recinto       : {env['cap_pct'] * 100:.0f}% = "
        f"{env['bankroll'] * env['cap_pct']:.4f} USDC impegnati\n"
        f"ordini aperti : {env['max_concurrent']} simultanei "
        f"(= floor({env['bankroll'] * env['cap_pct']:.2f} / {env['stake']:.2f}))\n"
        f"simulazioni   : {env['n_sims']:,} percorsi x {env['n_bets']} puntate\n"
        f"edge assunto  : {env['edge_pp'] * 100:+.2f}pp sul win rate di pareggio\n"
        f"rovina        : equity <= {env['ruin_floor']:.2f} USDC (contabile) | "
        f"equity < stake (operativa)")
    out.append(_hdr("Matrice"))
    out.append(f"{'quota':>6} {'WR pareggio':>11} {'WR assunto':>10} "
               f"{'RoR %':>8} {'inop. %':>8} {'MaxDD med':>10} "
               f"{'MaxDD p95':>10} {'eq.media':>10}")
    for r in rows:
        out.append(f"{r['odds']:>6.2f} {r['break_even_win_rate'] * 100:>10.2f}% "
                   f"{r['win_prob'] * 100:>9.2f}% "
                   f"{r['ruin_pct']:>8.3f} {r['inoperable_pct']:>8.3f} "
                   f"{r['max_dd_mean_pct']:>9.2f}% {r['max_dd_p95_pct']:>9.2f}% "
                   f"{r['equity_final_mean']:>10.2f}")
    per_scenario = elapsed / max(1, len(rows))
    out.append(f"\ntempo di calcolo: {elapsed:.3f} s "
               f"({per_scenario:.3f} s per scenario, {len(rows)} scenari)")
    out.append(f"un singolo scenario da {env['n_sims']:,} percorsi e' il costo "
               "di riferimento: --sims/--bets lo alzano o lo abbassano.")
    if rows and all(r["ruin_pct"] == 0.0 for r in rows):
        out.append("nota: RoR 0.000% significa 'mai in "
                   f"{env['n_sims']:,} percorsi' — non 'impossibile'. Il rischio "
                   "reale del recinto si legge in MaxDD p95.")
    return "\n".join(out)


def build_env(args: argparse.Namespace) -> Dict[str, Any]:
    consts = _production_constants()
    stake = float(args.stake) if args.stake is not None else consts["stake"]
    # Il tetto per ordine e' inviolabile anche in simulazione: si replica qui
    # la riduzione che fa `auto_bet`, cosi' la matrice non misura uno stake
    # che il bot non potrebbe mai piazzare.
    max_stake = consts["max_stake"]
    if max_stake > 0:
        stake = min(stake, max_stake)
    stake = max(stake, 0.01)
    bankroll = float(args.bankroll)
    cap_pct = float(args.cap_pct) if args.cap_pct is not None else consts["cap_pct"]
    return {"bankroll": bankroll, "stake": round(stake, 4),
            "max_stake": max_stake, "cap_pct": cap_pct,
            "constants_source": consts["source"],
            "max_concurrent": max_concurrent(bankroll, stake, cap_pct),
            "n_sims": int(args.sims), "n_bets": int(args.bets),
            "edge_pp": float(args.edge_pp), "ruin_floor": float(args.ruin_floor),
            "seed": int(args.seed)}


def parse_odds_grid(text: str) -> List[float]:
    out = []
    for tok in str(text or "").split(","):
        try:
            val = float(tok.strip())
        except (TypeError, ValueError):
            continue
        if val > 1.0:
            out.append(val)
    return out or list(DEFAULT_ODDS_GRID)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Stress-test Monte Carlo del bankroll (offline, sola lettura di costanti)")
    ap.add_argument("--sims", type=int, default=DEFAULT_SIMS,
                    help=f"percorsi Monte Carlo (default {DEFAULT_SIMS})")
    ap.add_argument("--bets", type=int, default=DEFAULT_BETS,
                    help=f"puntate per percorso (default {DEFAULT_BETS})")
    ap.add_argument("--bankroll", type=float, default=DEFAULT_BANKROLL_USDC,
                    help="bankroll di partenza (default: quello del recinto "
                         "12.16 USDC = 40%)")
    ap.add_argument("--stake", type=float, default=None,
                    help="stake per ordine (default: lo stake fisso di produzione)")
    ap.add_argument("--cap-pct", type=float, default=None,
                    help="recinto di esposizione (default: OPEN_EXPOSURE_CAP_PCT)")
    ap.add_argument("--edge-pp", type=float, default=0.0,
                    help="edge ipotetico in frazione sul pareggio (0.02 = +2pp)")
    ap.add_argument("--odds-grid", default=",".join(str(o) for o in DEFAULT_ODDS_GRID),
                    help="quote da testare, separate da virgola")
    ap.add_argument("--ruin-floor", type=float, default=DEFAULT_RUIN_FLOOR,
                    help="equity <= soglia = rovinato (default 0.0)")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    env = build_env(args)
    started = time.perf_counter()
    rows = build_matrix(parse_odds_grid(args.odds_grid),
                        n_sims=env["n_sims"], n_bets=env["n_bets"],
                        bankroll=env["bankroll"], stake=env["stake"],
                        cap_pct=env["cap_pct"], edge_pp=env["edge_pp"],
                        ruin_floor=env["ruin_floor"], seed=env["seed"])
    elapsed = time.perf_counter() - started

    if args.json:
        print(json.dumps({"enclosure": env, "matrix": rows,
                          "elapsed_s": round(elapsed, 4)}, indent=2,
                         ensure_ascii=False))
    else:
        print(format_report(env, rows, elapsed))
    return 0


if __name__ == "__main__":                                    # pragma: no cover
    sys.exit(main())
