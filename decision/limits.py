"""decision/limits.py — Le soglie della catena, in UN SOLO posto.

Perche' esiste: le stesse soglie sono oggi lette in dieci moduli e tre pagine
webapp, e hanno gia' iniziato a divergere (il bot applica
`adaptive_staking.MAX_STAKE_PCT` = 1% da env `STAKE_CAP_PCT`, mentre
`value_filter.MAX_STAKE_PCT` = 2% e' quello mostrato dai tool).

Regola di questo modulo: **nessun default copiato a mano**. I valori vengono
LETTI dai moduli che oggi li applicano (`value_filter`, `market_calib`,
`adaptive_staking`) e sovrascritti solo da env con lo STESSO nome usato in
produzione. Cosi' la pipeline di decisione non puo' divergere dal comportamento
reale, e `test_decision_limits.py` rompe se qualcuno cambia una soglia da una
parte sola.
"""

from __future__ import annotations

import os
from typing import Optional

from pydantic import BaseModel, Field

#: Nome dell'env che riabilita/disabilita la revisione umana dei segnali
#: borderline (`review`). Default attivo: e' la semantica scelta dal
#: proprietario il 14/09/2026 (coda + approvazione su Telegram).
REVIEW_ENABLED_ENV = "DECISION_REVIEW_ENABLED"
REVIEW_CONFIDENCE_ENV = "DECISION_REVIEW_CONFIDENCE"
DEFAULT_REVIEW_CONFIDENCE = 0.55

#: Floor del minimo ordine sull'exchange (SX Bet: 1 USDC) e floor "di codice".
#: Il primo prevale quando si ordina in LIVE; in SIM resta il floor di codice.
EXCHANGE_FLOOR_ENV = "EXCHANGE_MIN_ORDER_USDC"
DEFAULT_EXCHANGE_FLOOR = 1.0


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def _apply_stake_caps_from_env(base: float, strong: float) -> tuple[float, float]:
    """Rilegge i cap da env (stessi nomi applicati da `adaptive_staking`)."""
    return (_env_float("STAKE_CAP_PCT", base),
            _env_float("STAKE_CAP_PCT_STRONG", strong))


class RiskLimits(BaseModel):
    """Tutte le soglie che un Risk Engine puo' applicare."""

    # --- mercato / value (fonte: value_filter + market_calib) ---
    odds_min: float
    odds_max: float
    ev_min: float
    ev_max: float
    edge_min: float            # soglia di fallback (+2pp dal 21/09)
    edge_strong: float         # +4pp -> strong_value
    favourites_only: bool
    min_favourite_prob: float
    # --- revisione umana ---
    review_enabled: bool = True
    review_confidence_min: float = DEFAULT_REVIEW_CONFIDENCE
    #: Copertura minima del modello per giocare in AUTOMATICO: sotto questa
    #: soglia il segnale va in coda umana. Un modello senza rating usa il
    #: profilo NEUTRO di lega: li' una probabilita' bassa e' ignoranza, non
    #: valore (misurato l'11/09: 0 segnali value dove il modello era cieco).
    min_model_coverage: float = 0.5
    # --- stake (fonte: adaptive_staking) ---
    cap_value: float
    cap_strong: float
    kelly_min: float
    kelly_max: float
    #: cap mostrato dai tool (`value_filter.MAX_STAKE_PCT`): oggi DIVERGE dal
    #: cap applicato. Tenuto qui apposta, per non nascondere la differenza.
    cap_display: float = 0.0
    # --- liquidita' e floor ---
    min_exec_depth_usdc: float = 25.0
    depth_multiplier: float = 2.0
    order_floor: float = 0.01
    exchange_floor: float = DEFAULT_EXCHANGE_FLOOR
    stake_cap_hard: bool = True
    stake_step: float = 0.01
    # --- portafoglio ---
    total_exposure_cap_pct: float = 0.40
    correlation_cap_pct: float = 0.30

    @classmethod
    def from_env(cls) -> "RiskLimits":
        """Soglie reali del progetto (import pigro: nessun ciclo di import)."""
        import adaptive_staking as stake_mod
        import market_calib as calib
        import value_filter as vf

        cap_value, cap_strong = _apply_stake_caps_from_env(
            stake_mod.MAX_STAKE_PCT, stake_mod.MAX_STAKE_PCT_STRONG)
        return cls(
            odds_min=vf.ODDS_MIN,
            odds_max=vf.ODDS_MAX,
            ev_min=vf.EV_MIN,
            ev_max=vf.EV_MAX,
            edge_min=calib.MARKET_EDGE_MIN,
            edge_strong=calib.MARKET_EDGE_STRONG,
            favourites_only=vf.FAVOURITES_ONLY,
            min_favourite_prob=vf.MIN_FAVOURITE_MARKET_PROB,
            review_enabled=_env_bool(REVIEW_ENABLED_ENV, True),
            review_confidence_min=_env_float(REVIEW_CONFIDENCE_ENV,
                                             DEFAULT_REVIEW_CONFIDENCE),
            min_model_coverage=_env_float("DECISION_MIN_MODEL_COVERAGE", 0.5),
            cap_value=cap_value,
            cap_strong=cap_strong,
            kelly_min=stake_mod.MIN_KELLY_FRACTION,
            kelly_max=stake_mod.MAX_KELLY_FRACTION,
            cap_display=vf.MAX_STAKE_PCT,
            min_exec_depth_usdc=_env_float("SX_MIN_EXEC_DEPTH_USDC", 20.0),
            depth_multiplier=_env_float("SX_DEPTH_MULTIPLIER", 1.6),
            order_floor=stake_mod.MIN_STAKE_EUR,
            exchange_floor=_env_float(EXCHANGE_FLOOR_ENV, DEFAULT_EXCHANGE_FLOOR),
            stake_cap_hard=_env_bool("STAKE_CAP_HARD", True),
            stake_step=stake_mod.STAKE_STEP,
            total_exposure_cap_pct=_env_float("TOTAL_EXPOSURE_CAP_PCT", 0.40),
            correlation_cap_pct=_env_float("CORRELATION_CAP_PCT", 0.30),
        )

    # -- derivati ---------------------------------------------------------
    def cap_for(self, tier: str) -> float:
        """Cap percentuale del tier (value/moderate = 1%, strong = 2%)."""
        return self.cap_strong if tier == "strong_value" else self.cap_value

    def league_cap_pct(self, tier: str) -> float:
        return self.cap_for(tier)

    def floor_for(self, mode: str = "sim") -> float:
        """Floor effettivo: in LIVE e' il minimo ordine dell'exchange."""
        if mode == "live":
            return max(self.order_floor, self.exchange_floor)
        return self.order_floor

    def required_depth(self, stake: float) -> float:
        """Liquidita' richiesta al floor: max(stake x multiplo, minimo assoluto)."""
        try:
            stake_value = float(stake)
        except (TypeError, ValueError):
            stake_value = 0.0
        if stake_value <= 0:
            return self.min_exec_depth_usdc
        return max(stake_value * self.depth_multiplier, self.min_exec_depth_usdc)

    def league_min_edge(self, league: str) -> float:
        """Edge minimo per lega (stessa tabella usata da `is_sane`)."""
        import value_filter as vf
        return float(vf.get_league_strategy(league).get("min_edge", self.edge_min))

    def league_ev_min(self, league: str = "", market: str = "") -> float:
        """Soglia EV EFFETTIVA per lega+mercato (direttiva 08/10/2026).

        Delega a `value_filter.ev_min`: il Risk Engine della catena non puo'
        divergere dal gate di produzione (`is_sane`) su una soglia. Con `min`
        la lega core gioca il 1X2 a 1.5% e i mercati liquidi restano alla
        soglia di mercato (1.0%): nessuna copia di questi numeri qui.
        """
        import value_filter as vf
        try:
            return float(vf.ev_min(league or "", market or ""))
        except Exception:
            return float(self.ev_min)

    def league_max_stake_pct(self, league: str) -> float:
        """Cap di lega (`value_filter.STRATEGY_LEAGUES[...]["max_stake"]`)."""
        import value_filter as vf
        return float(vf.get_league_strategy(league).get("max_stake", self.cap_value))


def limits_from_env() -> RiskLimits:
    """Scorciatoia di lettura (usata dalla CLI e dai test)."""
    return RiskLimits.from_env()


__all__ = [
    "DEFAULT_EXCHANGE_FLOOR", "DEFAULT_REVIEW_CONFIDENCE", "EXCHANGE_FLOOR_ENV",
    "REVIEW_CONFIDENCE_ENV", "REVIEW_ENABLED_ENV", "RiskLimits", "limits_from_env",
]
