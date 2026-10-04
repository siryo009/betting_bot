"""decision/stake_engine.py — Quanto puntare. Si attiva SOLO se il rischio approva.

Regole:

1. **Kelly frazionato, scalato UNA VOLTA.** La frazione viene da
   `adaptive_staking.confidence_kelly_fraction` (edge, confidenza ML, CLV,
   tier); il Kelly puro viene calcolato con `value_filter.kelly_fraction(...,
   fraction=1.0)`. Moltiplicare la frazione anche dentro Kelly sarebbe il bug
   del 13/09 (stake incollato al floor con l'EV scalato due volte): qui la
   scalatura e' esattamente una.
2. **Tre cap, vince il piu' stretto**: tier (1% value/moderate, 2% strong),
   lega (`STRATEGY_LEAGUES[...]["max_stake"]`) e l'eventuale cap chiesto dal
   Risk Engine (che puo' solo stringere).
3. **Cap severo fail-closed**: se lo stake cappato sta sotto il floor
   dell'ordine, la puntata viene SALTATA invece di forzare il floor (che su un
   wallet piccolo diventa una percentuale di bankroll piu' alta del cap). Con
   `STAKE_CAP_HARD=0` si accetta il floor, come da configurazione di produzione.
4. **Liquidita' relativa allo stake**: il controllo di `auto_bet`
   (`max(stake x 2.0, 25 USDC)`) vive qui perche' serve il numero che solo
   questo motore conosce.
"""

from __future__ import annotations

import os
from typing import Any, Optional

from .limits import RiskLimits
from .models import (Mode, Money, ReasonCode, RiskDecision, Signal,
                     StakeDecision, as_float)


def _round_step(value: float, step: float) -> float:
    if step <= 0:
        return round(value, 2)
    return round(round(value / step) * step, 6)


# ---------------------------------------------------------------------------
# AGGRESSIVE KELLY — dimensionamento dinamico (direttiva 04/10/2026)
# ---------------------------------------------------------------------------
# Sostituisce i tetti FISSI (stake fisso 1.50/ordine, CB1 T-60 a 1.00) con una
# percentuale DINAMICA sul bankroll corrente: il capitale scala col bankroll
# (compounding) invece di essere soffocato da un importo costante.
#
# UN SOLO POSTO per la formula: il Kelly pieno viene da
# `value_filter.kelly_fraction` (che implementa `f = (b*p - q)/b` con `b` =
# quota decimale, la convenzione del progetto). Qui si applica il
# moltiplicatore aggressivo UNA volta e il cap percentuale: copiare la formula
# significherebbe due Kelly che divergono il giorno che qualcuno ne tocca uno
# (lezione del doppio scaling, 13/09).
KELLY_FRACTION_ENV = "KELLY_AGGRESSIVE_FRACTION"
KELLY_CAP_PCT_ENV = "KELLY_MAX_STAKE_PCT"
KELLY_MIN_TICKET_ENV = "KELLY_MIN_TICKET_USDC"
DEFAULT_KELLY_AGGRESSIVE_FRACTION = 0.65
DEFAULT_KELLY_MAX_STAKE_PCT = 0.12
#: Ticket minimo SX Bet per il MOTORE Kelly (scelta del proprietario, 04/10):
#: sotto questa soglia l'operazione viene scartata (stake 0.0). Il floor
#: dell'EXCHANGE resta 1.00 USDC (dato misurato): qui e' una soglia di
#: sizing, non una dichiarazione sul minimo dell'exchange.
DEFAULT_KELLY_MIN_TICKET = 2.00


def _num_env(name: str, default: float) -> float:
    """Numero da env: un valore impossibile ricade sul default (mai uno stake
    casuale) e si dichiara nel log — una soglia di rischio non si spegne con
    una variabile sbagliata."""
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        import logging
        logging.getLogger(__name__).warning(
            "stake_engine: %s='%s' non numerico, uso il default %.3f",
            name, raw, default)
        return default


def aggressive_config() -> dict[str, float]:
    """Parametri del motore aggressivo, letti a RUNTIME (tarabili da env)."""
    frac = _num_env(KELLY_FRACTION_ENV, DEFAULT_KELLY_AGGRESSIVE_FRACTION)
    pct = _num_env(KELLY_CAP_PCT_ENV, DEFAULT_KELLY_MAX_STAKE_PCT)
    ticket = _num_env(KELLY_MIN_TICKET_ENV, DEFAULT_KELLY_MIN_TICKET)
    return {
        "kelly_fraction": min(max(frac, 0.0), 1.0),
        "max_stake_pct": min(max(pct, 0.0), 1.0),
        "min_ticket": max(ticket, 0.0),
    }


def calculate_kelly_stake(true_prob: float, offered_odds: float,
                         bankroll: Money, *,
                         kelly_fraction: Optional[float] = None,
                         max_stake_pct: Optional[float] = None,
                         min_ticket: Optional[float] = None) -> dict[str, Any]:
    """Stake aggressivo: Kelly frazionato + cap dinamico + ticket minimo.

    `f = (b*p - q)/b` con `b` = quota decimale: la formula vive in
    `value_filter.kelly_fraction` (mai ricopiata). Sull'importo:

    1. `stake_raw = bankroll x kelly_pieno x kelly_fraction` (k = 0.65);
    2. cap dinamico: `bankroll x max_stake_pct` (12%) — TRONCA, mai alza;
    3. ticket minimo (2.00 USDC): sotto soglia lo stake e' **0.0**
       (operazione scartata) — mai un ordine piu' piccolo del ticket.

    Ritorna SEMPRE un dict (mai eccezioni): `executable` dice se l'operazione
    puo' partire, `reason` il motivo machine-readable. Il bankroll entra a
    ogni chiamata: e' cosi' che il compounding usa il capitale aggiornato.
    """
    cfg = aggressive_config()
    frac = cfg["kelly_fraction"] if kelly_fraction is None else float(kelly_fraction)
    pct = cfg["max_stake_pct"] if max_stake_pct is None else float(max_stake_pct)
    ticket = cfg["min_ticket"] if min_ticket is None else float(min_ticket)
    out: dict[str, Any] = {
        "stake": 0.0, "raw_stake": 0.0, "kelly_full": 0.0,
        "kelly_fraction": frac, "max_stake_pct": pct, "min_ticket": ticket,
        "cap_usdc": 0.0, "bankroll": 0.0, "capped": False,
        "executable": False, "reason": "",
    }
    try:
        # `Money` a riposo, `float` in transito: la conversione passa da
        # `as_float` (direttiva finanziaria del 29/09), mai da un `float()`
        # sparso che il tripwire di test_money_decimal rifiuta.
        bankroll_f = as_float(bankroll)
        prob = float(true_prob)
        odds = float(offered_odds)
    except (TypeError, ValueError):
        out["reason"] = "invalid_inputs"
        return out
    out["bankroll"] = bankroll_f
    if bankroll_f <= 0:
        out["reason"] = "no_bankroll"
        return out
    if not (0.0 < prob < 1.0) or odds <= 1.0:
        out["reason"] = "invalid_inputs"
        return out

    import value_filter as vf  # import pigro: nessun ciclo di import
    kelly_full = float(vf.kelly_fraction(prob, odds, fraction=1.0))
    out["kelly_full"] = kelly_full
    if kelly_full <= 0.0:
        out["reason"] = "no_edge"
        return out
    out["raw_stake"] = round(bankroll_f * kelly_full * frac, 6)
    out["cap_usdc"] = round(bankroll_f * pct, 6)

    stake = out["raw_stake"]
    if stake > out["cap_usdc"]:
        stake = out["cap_usdc"]       # TRONCA al cap: mai sopra il 12%
        out["capped"] = True
    stake = round(stake, 2)
    if stake < ticket:
        out["stake"] = 0.0
        out["reason"] = "below_min_ticket"
        return out
    out["stake"] = stake
    out["executable"] = True
    out["reason"] = "ok"
    return out


def aggressive_cap_usdc(bankroll: Money) -> float:
    """Tetto dinamico per singolo ordine (`bankroll x max_stake_pct`).

    UNICA fonte del cap dinamico: `auto_bet.cap_order_stake` e i percorsi T-60
    lo leggono da qui, cosi' il tetto non puo' divergere fra le corsie.
    """
    try:
        bankroll_f = as_float(bankroll)
    except (TypeError, ValueError):
        return 0.0
    if bankroll_f <= 0:
        return 0.0
    return round(bankroll_f * aggressive_config()["max_stake_pct"], 6)


def kelly_stake(signal: Signal, *, bankroll: float, limits: RiskLimits,
                ml_confidence: Optional[float] = None,
                has_clv_positive: Optional[bool] = None) -> tuple[float, float]:
    """(stake Kelly, frazione usata) — Kelly puro scalato una sola volta."""
    import adaptive_staking as stake_mod
    import value_filter as vf

    # La quota e' `Decimal` a riposo (Money) e questi motori ragionano in float:
    # conversione esplicita all'ESTREMO, una volta per chiamata.
    price = as_float(signal.price)
    fraction = stake_mod.confidence_kelly_fraction(
        prob=signal.blended_prob,
        odds=price,
        market_edge=signal.edge,
        ml_confidence=ml_confidence,
        has_clv_positive=has_clv_positive,
        status=signal.tier,
    )
    fraction = max(limits.kelly_min, min(limits.kelly_max, float(fraction)))
    full = vf.kelly_fraction(signal.blended_prob, price, fraction=1.0)
    return (bankroll * full * fraction, fraction)


def size(signal: Signal, risk: RiskDecision, *, bankroll: float,
         limits: RiskLimits, mode: Mode = "sim",
         ml_confidence: Optional[float] = None,
         has_clv_positive: Optional[bool] = None) -> StakeDecision:
    """Stake per un segnale APPROVATO (o approvato da un umano)."""
    base = StakeDecision(bankroll=bankroll, mode=mode)
    if not risk.allows_stake:
        base.reason = risk.reason
        base.detail = f"nessuno stake: verdetto '{risk.verdict}' ({risk.reason.value})"
        base.executable = False
        return base
    if bankroll <= 0:
        base.reason = ReasonCode.STAKE_BELOW_FLOOR
        base.detail = "bankroll non disponibile (saldo insufficiente o non leggibile)"
        base.executable = False
        return base

    stake_value, fraction = kelly_stake(signal, bankroll=bankroll, limits=limits,
                                        ml_confidence=ml_confidence,
                                        has_clv_positive=has_clv_positive)
    base.kelly_fraction = fraction
    base.kelly_stake = _round_step(stake_value, limits.stake_step)

    caps: list[tuple[float, str]] = [
        (limits.cap_for(signal.tier), "tier"),
        (limits.league_max_stake_pct(signal.league), "league"),
    ]
    if "max_stake_pct" in risk.tightened:
        caps.append((float(risk.tightened["max_stake_pct"]), "risk"))
    cap_pct, cap_source = min(caps, key=lambda item: item[0])
    base.cap_pct = cap_pct
    base.cap_source = cap_source

    stake_value = min(stake_value, bankroll * cap_pct)
    # --- CB1 — HARD CAP ASSOLUTO PER ORDINE (T-60, direttiva 17/09/2026) ---
    # NESSUN calcolo dinamico (Kelly incluso) puo' produrre uno stake sopra
    # il tetto del circuit breaker: viene SORSCRITTO, non negoziato. Il cap
    # protegge il DENARO REALE: si applica in mode='live' (in SIM resta la
    # telemetria del Kelly pieno, altrimenti il feedback impara su stake
    # che non verrebbero mai giocati). Import PIGRO: `import decision` non
    # carica auto_bet (tripwire); senza auto_bet la protezione resta al
    # default di env.
    if mode == "live":
        # Dal 04/10/2026 il CB1 e' DINAMICO (12% del bankroll): il tetto
        # efficace lo calcola `auto_bet.order_ceiling` (UNICO punto di
        # verita'), che legge la costante assoluta quando e' impostata e il
        # cap percentuale altrimenti. Un tetto <= 0 (bankroll ignoto) NON
        # autorizza nulla: lo stake va a 0 e l'ordine e' fail-closed.
        try:
            from auto_bet import order_ceiling as _order_ceiling
            _t60_hard_cap = float(_order_ceiling(bankroll))
        except Exception:
            import os as _os
            _t60_hard_cap = float(_os.getenv("T60_MAX_STAKE_USDC", "0.0"))
        if _t60_hard_cap <= 0:
            base.detail = ("CB1: tetto per-ordine non calcolabile (bankroll "
                           f"{bankroll:.2f}) — ordine saltato (fail-closed)")
            base.stake = 0.0
            base.reason = ReasonCode.STAKE_BELOW_FLOOR
            base.executable = False
            return base
        if stake_value > _t60_hard_cap + 1e-9:
            base.detail = (f"CB1 hard cap {_t60_hard_cap:.2f} USDC (12% del "
                           f"bankroll {bankroll:.2f}): stake Kelly "
                           f"{stake_value:.2f} sovrascritto")
            stake_value = float(_t60_hard_cap)
            base.cap_source = "t60_hard_cap"
    stake_value = _round_step(stake_value, limits.stake_step)
    base.stake = stake_value
    base.floor = limits.floor_for(mode)

    if stake_value <= 0:
        base.reason = ReasonCode.STAKE_BELOW_FLOOR
        base.detail = f"stake calcolato {stake_value:.2f} (nessun edge residuo)"
        base.executable = False
        return base

    if stake_value < base.floor:
        if limits.stake_cap_hard:
            base.reason = ReasonCode.STAKE_BELOW_FLOOR
            base.detail = (f"CAP SEVERO: stake cappato {stake_value:.2f} < minimo ordine "
                           f"{base.floor:.2f} — ordine saltato (fail-closed). "
                           f"Cap {cap_pct*100:.2f}% su bankroll {bankroll:.2f}")
            base.executable = False
            return base
        base.stake = base.floor
        # `base.stake` e' un `Decimal` (Money): la percentuale di telemetria si
        # calcola in float, altrimenti Decimal/float solleva TypeError.
        pct = as_float(base.stake) / as_float(bankroll) * 100 if bankroll else 0.0
        base.detail = (f"floor {base.floor:.2f} applicato (STAKE_CAP_HARD=0): stake "
                       f"{base.stake:.2f} = {pct:.2f}% del bankroll")

    depth = signal.data_quality.depth_usdc
    if depth is not None:
        required = limits.required_depth(base.stake)
        if depth < required:
            base.reason = ReasonCode.LIQUIDITY_LOW
            base.detail = (f"liquidita' {depth:.2f} < richiesta {required:.2f} "
                           f"(stake {base.stake:.2f} x {limits.depth_multiplier}) — "
                           "salto (rischio slippage)")
            base.executable = False
            return base

    base.executable = True
    base.reason = ReasonCode.OK
    if risk.tightened:
        base.detail = (base.detail + " | cap ridotto dal Risk Engine: "
                       + ", ".join(f"{k}={v}" for k, v in risk.tightened.items())).strip(" |")
    return base


__all__ = ["aggressive_cap_usdc", "aggressive_config", "calculate_kelly_stake",
           "kelly_stake", "size"]
