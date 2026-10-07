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
KELLY_MIN_FRACTION_ENV = "KELLY_AGGRESSIVE_MIN_FRACTION"
KELLY_CAP_PCT_ENV = "KELLY_MAX_STAKE_PCT"
KELLY_MIN_TICKET_ENV = "KELLY_MIN_TICKET_USDC"
#: BANDA del Kelly dinamico (direttiva 04/10/2026, punto 2): k non e' piu'
#: fisso. Il MAX e' `KELLY_AGGRESSIVE_FRACTION` (0.25), il MIN
#: `KELLY_AGGRESSIVE_MIN_FRACTION` (0.15); il valore effettivo e' scalato
#: dentro la banda da edge e confidenza della lega (`dynamic_kelly_fraction`).
DEFAULT_KELLY_AGGRESSIVE_FRACTION = 0.25
DEFAULT_KELLY_AGGRESSIVE_MIN_FRACTION = 0.15
DEFAULT_KELLY_MAX_STAKE_PCT = 0.12
#: Ticket minimo per il MOTORE Kelly: **1.00 USDC** (direttiva 04/10/2026,
#: punto 2). Coincide col floor operativo dell'exchange SX Bet: sotto questa
#: soglia l'operazione viene scartata (stake 0.0). Era 2.00 nella direttiva
#: precedente; con k piu' basso (0.15-0.25) il ticket 2.00 avrebbe scartato
#: quasi tutto il flusso, quindi il proprietario ha chiesto l'allineamento.
DEFAULT_KELLY_MIN_TICKET = 1.00
#: Riferimenti per NORMALIZZARE edge ed EV dentro la banda di k. Letti da
#: `value_filter`/`market_calib` (mai copiati): qui solo i fallback.
_KELLY_EV_REF_FALLBACK = 0.04
_KELLY_LEAGUE_MULT_MIN = 0.4
_KELLY_LEAGUE_MULT_MAX = 1.3


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
    """Parametri del motore, letti a RUNTIME (tarabili da env).

    `kelly_fraction` e' il MASSIMO della banda dinamica, `kelly_min_fraction`
    il minimo: il valore applicato a un trade lo calcola
    `dynamic_kelly_fraction` (edge + confidenza lega).
    """
    frac = _num_env(KELLY_FRACTION_ENV, DEFAULT_KELLY_AGGRESSIVE_FRACTION)
    frac = min(max(frac, 0.0), 1.0)
    k_min = _num_env(KELLY_MIN_FRACTION_ENV, DEFAULT_KELLY_AGGRESSIVE_MIN_FRACTION)
    k_min = min(max(k_min, 0.0), 1.0)
    if k_min > frac:          # banda coerente: il max non puo' stare sotto il min
        k_min = frac
    pct = _num_env(KELLY_CAP_PCT_ENV, DEFAULT_KELLY_MAX_STAKE_PCT)
    ticket = _num_env(KELLY_MIN_TICKET_ENV, DEFAULT_KELLY_MIN_TICKET)
    return {
        "kelly_fraction": frac,
        "kelly_min_fraction": k_min,
        "max_stake_pct": min(max(pct, 0.0), 1.0),
        "min_ticket": max(ticket, 0.0),
    }


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def dynamic_kelly_fraction(*, ev: Optional[float] = None,
                           edge: Optional[float] = None,
                           league: Optional[str] = None,
                           market: Optional[str] = None) -> dict[str, Any]:
    """k effettivo nella banda [k_min, k_max] da edge + confidenza lega.

    Direttiva 04/10/2026 (punto 2): il frazionamento scende da 0.65 fisso a
    **0.15-0.25 dinamico**. Il valore sale verso il massimo quando:

    - l'**EV** del trade e' forte (normalizzato fra la soglia minima EFFETTIVA
      dell'operazione — `value_filter.ev_min(league, market)`, quindi 1.5% su
      un 1X2 core e 1.0% su un mercato liquido — e `EV_STRONG_REF`, 4%): un
      margine grande regge piu' Kelly. Direttiva 08/10/2026 (revisione): la
      soglia di riferimento e' quella REALE della lega/mercato, non il 2.5%
      generico — un'operazione core a EV 2% non finisce piu' schiacciata a 0;
    - l'**edge** sul mercato e' ampio (normalizzato fra `MARKET_EDGE_MIN` e
      `MARKET_EDGE_STRONG`);
    - la **lega** e' affidabile (`value_filter.get_league_strategy`):
      `kelly_mult` 0.4 (probation) -> 1.3 (core) normalizzato a [0, 1].

    Determinisico e senza metriche inventate: i componenti mancanti non
    contano, e senza alcun dato la forza e' 0.5 (meta' banda). Il risultato e'
    SEMPRE dentro [k_min, k_max]: la banda non si allarga con un input strano.
    """
    cfg = aggressive_config()
    k_min = cfg["kelly_min_fraction"]
    k_max = cfg["kelly_fraction"]
    components: dict[str, float] = {}
    reasons: list[str] = []

    try:
        if ev is not None:
            import value_filter as _vf
            # Soglia di riferimento REALE per questa lega/mercato (direttiva
            # 08/10/2026): usare il 2.5% generico schiacciava a zero il
            # componente EV di ogni operazione core a EV 1.5-2.5%.
            ev_lo = float(_vf.ev_min(league or "", market))
            ev_hi = _KELLY_EV_REF_FALLBACK
            try:
                from market_calib import MARKET_EDGE_STRONG as _strong
                ev_hi = min(max(float(_strong) + 0.01, ev_lo + 1e-9),
                            _KELLY_EV_REF_FALLBACK)
            except Exception:
                ev_hi = max(ev_hi, ev_lo + 1e-9)
            span = max(ev_hi - ev_lo, 1e-9)
            components["ev"] = _clamp01((float(ev) - ev_lo) / span)
            reasons.append(f"ev {components['ev']:.2f}")
    except Exception:
        pass

    try:
        if edge is not None:
            from market_calib import MARKET_EDGE_MIN as _e0, MARKET_EDGE_STRONG as _e1
            span = max(float(_e1) - float(_e0), 1e-9)
            components["edge"] = _clamp01((float(edge) - float(_e0)) / span)
            reasons.append(f"edge {components['edge']:.2f}")
    except Exception:
        pass

    try:
        if league:
            from value_filter import get_league_strategy
            mult = float(get_league_strategy(league).get("kelly_mult", 1.0))
            span = max(_KELLY_LEAGUE_MULT_MAX - _KELLY_LEAGUE_MULT_MIN, 1e-9)
            components["league"] = _clamp01(
                (mult - _KELLY_LEAGUE_MULT_MIN) / span)
            reasons.append(f"lega {components['league']:.2f}")
    except Exception:
        pass

    if components:
        weights = {"ev": 0.45, "edge": 0.35, "league": 0.20}
        total_w = sum(weights[k] for k in components)
        strength = sum(components[k] * weights[k] for k in components) / total_w
    else:
        strength = 0.5          # nessun segnale: centro banda (mai 0 o 1)
        reasons.append("nessun dato (centro banda)")
    strength = _clamp01(strength)
    k = k_min + (k_max - k_min) * strength
    return {
        "kelly_fraction": round(min(max(k, k_min), k_max), 6),
        "kelly_min_fraction": k_min,
        "kelly_max_fraction": k_max,
        "strength": round(strength, 4),
        "components": {k2: round(v, 4) for k2, v in components.items()},
        "reason": "; ".join(reasons),
    }


def calculate_kelly_stake(true_prob: float, offered_odds: float,
                         bankroll: Money, *,
                         kelly_fraction: Optional[float] = None,
                         max_stake_pct: Optional[float] = None,
                         min_ticket: Optional[float] = None,
                         ev: Optional[float] = None,
                         edge: Optional[float] = None,
                         league: Optional[str] = None,
                         market: Optional[str] = None) -> dict[str, Any]:
    """Stake aggressivo: Kelly frazionato + cap dinamico + ticket minimo.

    `f = (b*p - q)/b` con `b` = quota decimale: la formula vive in
    `value_filter.kelly_fraction` (mai ricopiata). Sull'importo:

    1. `stake_raw = bankroll x kelly_pieno x k` con **k dinamico** nella banda
       0.15-0.25 scalata da EV/edge/lega/mercato (`dynamic_kelly_fraction`:
       l'EV si normalizza sulla soglia REALE di lega+mercato, direttiva
       08/10/2026); passare `kelly_fraction` esplicito la vince (test e
       override);
    2. cap dinamico: `bankroll x max_stake_pct` (12%) — TRONCA, mai alza;
    3. ticket minimo (1.00 USDC): sotto soglia lo stake e' **0.0**
       (operazione scartata) — mai un ordine piu' piccolo del ticket.

    Ritorna SEMPRE un dict (mai eccezioni): `executable` dice se l'operazione
    puo' partire, `reason` il motivo machine-readable. Il bankroll entra a
    ogni chiamata: e' cosi' che il compounding usa il capitale aggiornato.
    """
    cfg = aggressive_config()
    dyn = None
    if kelly_fraction is None:
        dyn = dynamic_kelly_fraction(ev=ev, edge=edge, league=league,
                                    market=market)
        frac = float(dyn["kelly_fraction"])
    else:
        frac = float(kelly_fraction)
    pct = cfg["max_stake_pct"] if max_stake_pct is None else float(max_stake_pct)
    ticket = cfg["min_ticket"] if min_ticket is None else float(min_ticket)
    out: dict[str, Any] = {
        "stake": 0.0, "raw_stake": 0.0, "kelly_full": 0.0,
        "kelly_fraction": frac, "max_stake_pct": pct, "min_ticket": ticket,
        "cap_usdc": 0.0, "bankroll": 0.0, "capped": False,
        "executable": False, "reason": "",
        # Provenienza del k applicato (telemetria del sizing: il Cervello
        # alimenta EV/edge/lega e la Finanza li passa qui — tracciabile).
        "kelly_dynamic": bool(dyn is not None),
        "kelly_strength": None if dyn is None else dyn["strength"],
        "kelly_reason": "" if dyn is None else dyn["reason"],
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
