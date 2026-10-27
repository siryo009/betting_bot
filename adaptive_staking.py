"""adaptive_staking.py — Kelly frazionato dinamico con drawdown protection.

Il Kelly Criterion classico (frazione piena) è troppo aggressivo per il
calcio: il drawdown del 50% prima della crescita è insostenibile. Questo
modulo implementa:

1. **Confidence-Weighted Kelly**: la frazione di Kelly dipende dalla
   confidenza nel segnale (ensemble ML, edge sul mercato, CLV).
2. **Drawdown Protection**: durante losing streaks, lo stake si riduce
   proporzionalmente al drawdown dal picco del bankroll.
3. **Market-Adjusted Stake**: mercati più efficienti ricevono stakes
   più cautelativi (meno edge = meno rischio).

Riferimenti:
- Kelly (1956) "A New Interpretation of Information Rate"
- Thorp (2006) "The Kelly Criterion in Blackjack, Sports Betting..."
- Vince (1990) "The mathematics of money management"
- Research UPenn (2026): modified Kelly con coefficiente 0.50, cap 10%

CLI:
  venv/bin/python adaptive_staking.py --bankroll 1000 --prob 0.55 --odds 2.10
  venv/bin/python adaptive_staking.py --stats  # statistiche bankroll
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from tracker import _get_conn

logger = logging.getLogger(__name__)


def _ratio_env(name: str, default: float) -> float:
    """Legge un RAPPORTO da env con clamp in [0, 1] e fallback al default.

    Parametrizzare lo staking non deve poter introdurre una frazione di
    Kelly o un drawdown impossibili: un valore negativo, > 1 o non numerico
    viene IGNORATO (con un warning, mai in silenzio) e vale il default.
    """
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        val = float(raw)
    except (TypeError, ValueError):
        logger.warning("%s=%r non numerico: uso il default %s", name, raw, default)
        return default
    if val < 0.0 or val > 1.0:
        logger.warning("%s=%s fuori intervallo [0, 1]: uso il default %s",
                       name, val, default)
        return default
    return val


# --- Config (tutto env-configurabile; staking 100% dinamico dal 08/09) ---
# Il Kelly frazionato calcola lo stake per OGNI scommessa a partire dal
# bankroll corrente: nessun importo fisso, solo vincoli di sicurezza
# percentuali (Kelly + cap per segnale) e il floor dell'exchange.
#
# FRAZIONE DI KELLY — parametrizzabile da env SENZA toccare la formula:
#   KELLY_MIN_FRACTION / KELLY_MAX_FRACTION = intervallo della frazione
#     dinamica (confidence-weighted);
#   KELLY_BASE_FRACTION = punto di PARTENZA della frazione (usato anche nei
#     messaggi "sopra/sotto il base Kelly").
# ⚠️ Con il bankroll attuale il floor dell'exchange (1 USDC) prevale su
# qualunque frazione: alzare questi valori non cambia lo stake finche' il
# cap percentuale resta sotto 1 USDC (soglie misurate il 17/09: il cap morde
# da 50.25 USDC per strong_value e da 100.50 per value).
BASE_KELLY_FRACTION = _ratio_env("KELLY_BASE_FRACTION", 0.25)
MIN_KELLY_FRACTION = _ratio_env("KELLY_MIN_FRACTION", 0.05)
MAX_KELLY_FRACTION = _ratio_env("KELLY_MAX_FRACTION", 0.40)
if MAX_KELLY_FRACTION < MIN_KELLY_FRACTION:
    # Configurazione incoerente (max < min): il tetto deve poter contenere il
    # pavimento, altrimenti la frazione dinamica non ha intervallo valido.
    logger.warning("KELLY_MAX_FRACTION (%s) < KELLY_MIN_FRACTION (%s): allineo",
                   MAX_KELLY_FRACTION, MIN_KELLY_FRACTION)
    MAX_KELLY_FRACTION = MIN_KELLY_FRACTION
# Cap % del bankroll per singola bet: cresce SOLO sui segnali a forte
# margine matematico (strong_value). Il Kelly frazionato resta il freno
# principale (mai oltre il 40% del Kelly pieno).
# CAP SEVERO (11/09/2026, con la strategia solo-favoriti): 1% per value e
# moderate, 2% per strong_value. Le quote corte dei favoriti fanno calcolare
# al Kelly frazioni MAGGIORI (stake piu' grandi a parita' di bankroll),
# quindi il cap per singola operazione e' la seconda barriera al rischio di
# bancarotta. ⚠️ Il floor ordine exchange (1 USDC) prevale: con bankroll
# sotto ~50-100 USDC lo stake effettivo resta 1 USDC.
MAX_STAKE_PCT = float(os.getenv("STAKE_CAP_PCT", "0.01"))        # value
MAX_STAKE_PCT_STRONG = float(os.getenv("STAKE_CAP_PCT_STRONG", "0.02"))
DRAWDOWN_THRESHOLD = _ratio_env("DRAWDOWN_THRESHOLD", 0.10)   # > 10% = riduci
DRAWDOWN_REDUCTION = _ratio_env("DRAWDOWN_REDUCTION", 0.50)   # min 50% stake
MIN_STAKE_EUR = float(os.getenv("STAKE_MIN_EUR", "0.01"))
STAKE_STEP = float(os.getenv("STAKE_STEP_EUR", "0.01"))


def confidence_kelly_fraction(prob: float, odds: float,
                               market_edge: float = None,
                               ml_confidence: float = None,
                               has_clv_positive: bool = None,
                               status: str = "value") -> float:
    """Kelly frazionato con frazione dinamica basata sulla confidenza.

    La frazione varia da MIN_KELLY_FRACTION a MAX_KELLY_FRACTION (default
    0.05-0.40) in base a multipli segnali di confidenza:

    - **edge sul mercato**: più alto → più confidenza
    - **ML confidence**: ensemble addestrato → più confidenza
    - **CLV positivo**: se stiamo batte la closing line → conferma
    - **status**: strong_value → bonus confidenza

    Args:
        prob: probabilità stimata dell'esito
        odds: quota decimale
        market_edge: edge del modello sul mercato (0.03 = +3pp)
        ml_confidence: confidenza del modello ML (0-1)
        has_clv_positive: True se il CLV storico è positivo
        status: "value" o "strong_value"

    Returns:
        Frazione di Kelly (0.10 - 0.35)
    """
    if odds <= 1.0:
        return 0.0

    # Base: 1/4 Kelly
    fraction = BASE_KELLY_FRACTION

    # Bonus/malus per confidenza
    confidence_score = 0.0  # -1.0 (bassa) a +1.0 (alta)

    # 1. Edge sul mercato: più alto = più confidenza
    if market_edge is not None:
        if market_edge >= 0.08:
            confidence_score += 0.4  # edge forte
        elif market_edge >= 0.05:
            confidence_score += 0.2  # edge buono
        elif market_edge >= 0.03:
            confidence_score += 0.0  # edge minimo
        else:
            confidence_score -= 0.3  # edge debole

    # 2. ML confidence
    if ml_confidence is not None:
        confidence_score += (ml_confidence - 0.5) * 0.6  # -0.3 a +0.3

    # 3. CLV positivo: conferma dell'edge
    if has_clv_positive is True:
        confidence_score += 0.2
    elif has_clv_positive is False:
        confidence_score -= 0.2

    # 4. Status
    if status == "strong_value":
        confidence_score += 0.3
    elif status == "moderate":
        confidence_score += 0.1  # segnale moderato: piccolo bonus

    # Mappa confidence_score → frazione di Kelly
    # score -1.0 → MIN, score +1.0 → MAX
    t = max(0.0, min(1.0, (confidence_score + 1.0) / 2.0))
    fraction = MIN_KELLY_FRACTION + t * (MAX_KELLY_FRACTION - MIN_KELLY_FRACTION)

    return fraction


def drawdown_factor(bankroll: float, peak_bankroll: float) -> float:
    """Fattore di riduzione basato sul drawdown.

    Se il bankroll è sceso di più del 10% dal picco, riduce gli stakes
    proporzionalmente (massimo 50% di riduzione).

    Args:
        bankroll: bankroll attuale
        peak_bankroll: massimo storico del bankroll

    Returns:
        Fattore 0.5 - 1.0 (1.0 = nessuna riduzione)
    """
    if peak_bankroll <= 0:
        return 1.0
    drawdown = 1.0 - (bankroll / peak_bankroll)
    if drawdown <= DRAWDOWN_THRESHOLD:
        return 1.0
    # Riduzione lineare da 1.0 a DRAWDOWN_REDUCTION
    excess = drawdown - DRAWDOWN_THRESHOLD
    max_excess = 1.0 - DRAWDOWN_THRESHOLD  # drawdown massimo teorico
    reduction = min(1.0, excess / max_excess) * (1.0 - DRAWDOWN_REDUCTION)
    return max(DRAWDOWN_REDUCTION, 1.0 - reduction)


def adaptive_stake(bankroll: float, prob: float, odds: float,
                   market_edge: float = None,
                   ml_confidence: float = None,
                   has_clv_positive: bool = None,
                   status: str = "value",
                   peak_bankroll: float = None,
                   round_to_step: bool = True) -> Dict:
    """Calcola lo stake adattivo con Kelly dinamico e drawdown protection.

    Ritorna dizionario con:
    - stake: importo in euro
    - kelly_fraction: frazione di Kelly usata
    - confidence_score: punteggio di confidenza
    - drawdown_factor: fattore drawdown
    - raw_stake: stake prima del cap
    - capped: True se lo stake è stato cappeggiato
    - reason: spiegazione della decisione
    """
    if odds <= 1.0:
        return {"stake": 0.0, "reason": "quota invalida"}

    # 1. Kelly frazionato dinamico
    fraction = confidence_kelly_fraction(
        prob, odds, market_edge=market_edge,
        ml_confidence=ml_confidence,
        has_clv_positive=has_clv_positive,
        status=status)

    # 2. Kelly completo e stake. La formula vive in `value_filter.kelly_fraction`
    # (fix 27/10/2026): fino a ieri qui c'era una COPIA della formula sbagliata
    # `(p*o - q)/o`, che sovrastimava lo stake di 2,5-7x sulle quote basse e
    # allocava denaro anche su scontri a EV negativo. Una sola definizione.
    import value_filter as _vf
    kelly_full = float(_vf.kelly_fraction(prob, odds, fraction=1.0))
    kelly_raw = max(0.0, kelly_full * fraction)
    raw_stake = bankroll * kelly_raw

    # 3. Drawdown protection
    peak = peak_bankroll or bankroll
    dd_factor = drawdown_factor(bankroll, peak)
    stake_after_dd = raw_stake * dd_factor

    # 4. Cap per tipo di segnale
    # strong_value -> 2%; value e moderate (segnali piu' deboli) -> 1%.
    cap_pct = MAX_STAKE_PCT_STRONG if status == "strong_value" else MAX_STAKE_PCT
    cap = bankroll * cap_pct
    stake = min(stake_after_dd, cap)
    capped = stake_after_dd > cap

    # 5. Arrotonda allo step (0.01) e forza il floor dell'exchange (1.0)
    if round_to_step and stake > 0:
        stake = max(MIN_STAKE_EUR, round(stake / STAKE_STEP) * STAKE_STEP)

    # 6. Confidence score per log
    conf = 0.0
    if market_edge is not None:
        conf += market_edge * 5  # normalizza
    if ml_confidence is not None:
        conf += (ml_confidence - 0.5) * 0.5
    if has_clv_positive:
        conf += 0.1
    if status == "strong_value":
        conf += 0.15
    elif status == "moderate":
        conf += 0.05

    reason_parts = []
    if kelly_full <= 0:
        reason_parts.append("EV negativo o zero")
    if dd_factor < 1.0:
        reason_parts.append(f"drawdown {dd_factor:.0%}")
    if capped:
        reason_parts.append(f"cap {cap_pct:.0%}")
    if fraction < BASE_KELLY_FRACTION:
        reason_parts.append(f"bassa confidenza ({fraction:.0%} Kelly)")
    elif fraction > BASE_KELLY_FRACTION:
        reason_parts.append(f"alta confidenza ({fraction:.0%} Kelly)")

    return {
        "stake": round(stake, 2),
        "kelly_fraction": round(fraction, 4),
        "kelly_full": round(kelly_full, 4),
        "raw_stake": round(raw_stake, 2),
        "drawdown_factor": round(dd_factor, 3),
        "confidence_score": round(conf, 3),
        "capped": capped,
        "reason": "; ".join(reason_parts) if reason_parts else "OK",
    }


def get_peak_bankroll(chat_id: int = None) -> float:
    """Recupera il picco storico del bankroll."""
    conn = _get_conn()
    try:
        # Ultimo bankroll dal ledger cassa
        row = conn.execute(
            "SELECT MAX(importo) FROM ("
            "  SELECT SUM(importo) as importo FROM cassa"
            ")").fetchone()
        if row and row[0]:
            return float(row[0])
    except Exception:
        pass
    finally:
        conn.close()
    return 0.0


def bankroll_stats() -> Dict:
    """Statistiche del bankroll per il report."""
    conn = _get_conn()
    try:
        # Bankroll attuale (colonna reale della cassa: importo, non amount)
        row = conn.execute(
            "SELECT COALESCE(SUM(importo), 0) FROM cassa").fetchone()
        current = float(row[0]) if row else 0.0

        # Peak
        peak_row = conn.execute(
            "SELECT COALESCE(MAX(running_total), 0) FROM ("
            "  SELECT SUM(importo) OVER (ORDER BY id) as running_total"
            "  FROM cassa"
            ")").fetchone()
        peak = float(peak_row[0]) if peak_row else current

        # Puntate recenti (ultime 24h)
        yesterday = (datetime.now() - timedelta(days=1)).isoformat()
        bets_row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(importo), 0) "
            "FROM cassa WHERE data >= ?", (yesterday,)).fetchone()
        bets_24h = bets_row[0] if bets_row else 0
        spent_24h = float(bets_row[1]) if bets_row else 0.0

        dd = drawdown_factor(current, peak) if peak > 0 else 1.0

        return {
            "current": round(current, 2),
            "peak": round(peak, 2),
            "drawdown_pct": round((1 - current / peak) * 100, 1) if peak > 0 else 0,
            "drawdown_factor": round(dd, 3),
            "bets_24h": bets_24h,
            "spent_24h_eur": round(spent_24h, 2),
            "risk_level": ("🟢 OK" if dd >= 0.95 else
                           "🟡 cautela" if dd >= 0.85 else
                           "🔴 HIGH drawdown"),
        }
    except Exception as e:
        return {"error": str(e)}
    finally:
        conn.close()


# --- CLI ---

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Adaptive staking: Kelly frazionato dinamico")
    ap.add_argument("--bankroll", type=float, default=1000)
    ap.add_argument("--prob", type=float, required=True)
    ap.add_argument("--odds", type=float, required=True)
    ap.add_argument("--edge", type=float, default=None)
    ap.add_argument("--ml-conf", type=float, default=None)
    ap.add_argument("--clv", action="store_true", default=None)
    ap.add_argument("--status", type=str, default="value",
                    choices=["value", "strong_value"])
    ap.add_argument("--peak", type=float, default=None)
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if args.stats:
        stats = bankroll_stats()
        if args.json:
            print(json.dumps(stats, indent=2))
        else:
            print(f"💰 Bankroll: €{stats.get('current', 0):.2f}")
            print(f"   Peak: €{stats.get('peak', 0):.2f}")
            print(f"   Drawdown: {stats.get('drawdown_pct', 0):.1f}% "
                  f"({stats.get('risk_level', '?')})")
            print(f"   Fattore: {stats.get('drawdown_factor', 1.0):.1%}")
        return 0

    result = adaptive_stake(
        bankroll=args.bankroll, prob=args.prob, odds=args.odds,
        market_edge=args.edge, ml_confidence=args.ml_conf,
        has_clv_positive=args.clv, status=args.status,
        peak_bankroll=args.peak)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"🎯 Stake: €{result['stake']:.2f}")
        print(f"   Kelly: {result['kelly_fraction']:.1%} "
              f"(full: {result['kelly_full']:.1%})")
        print(f"   Drawdown factor: {result['drawdown_factor']:.1%}")
        print(f"   Confidenza: {result['confidence_score']:.2f}")
        if result['reason'] != "OK":
            print(f"   ⚠️  {result['reason']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
