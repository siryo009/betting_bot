"""Agente CERVELLO — EV dinamico + Portfolio Shield (direttiva 04/10/2026).

Due ragionamenti che l'Agente Analisi NON fa (descrive) e la Finanza NON fa
(esegue): qui il segnale diventa un trade VALIDATO.

1. **EV dinamico.** La soglia minima di EV non e' una costante: sale quando il
   trade e' piu' fragile. Due cause, entrambe MISURATE dal segnale:
   - **liquidita' bassa** (book sottile): lo stake rischia lo slippage, quindi
     serve piu' margine perche' il prezzo eseguito sia quello sperato;
   - **volatilita' alta** (|velocity| dello sharp): un prezzo che si muove
     veloce puo' non esistere piu' quando l'ordine arriva.
   La soglia parte SEMPRE da `value_filter.EV_MIN` (fonte unica, mai copiata)
   e puo' solo SALIRE: il moltiplicatore e' `1.0 + extra`, mai < 1. Cosi' il
   Cervello non puo' allargare cio' che il resto della pipeline ha stretto
   (stessa regola del Risk Engine, 14/09).

2. **Portfolio Shield.** Prima di validare, guarda il PORTafoglio aperto
   (SQLite: puntate `mode='live'` non saldate) e misura la concentrazione per
   LEGA (il blocco correlato del progetto, cap 30% letto da `auto_bet`). Tre
   azioni: `allow` (spazio pieno), `scale` (spazio ridotto: la Finanza non puo'
   superare `shield_max_usdc`), `block` (blocco saturo o spazio sotto il ticket
   minimo). Mai una soglia ricopiata: il cap e' quello di `auto_bet`, il ticket
   quello del motore Kelly.

Confini: nessun ordine, nessuna scrittura. Legge il ledger in sola LETTURA e
l'import di `tracker`/`auto_bet` e' PIGRO (dentro le funzioni), cosi'
`import agents` resta leggero. Fail-safe: un errore di lettura non blocca il
ciclo ma NON autorizza nemmeno (focus fail-closed sul denaro), e ogni motivo e'
machine-readable (`dynamic_reason`/`shield_reason`).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable, Optional, Sequence

from .contracts import BrainOutput, OracleSignal, ValidatedTrade

logger = logging.getLogger(__name__)

__all__ = [
    "BrainAgent", "base_ev_min", "dynamic_ev_min", "ev_mult_liquidity",
    "ev_mult_volatility", "min_depth_usdc", "shield_cap_pct",
    "volatility_ref_pct_min",
]

#: Profondita' minima del libro perche' il trade non richieda margine extra.
MIN_DEPTH_ENV = "BRAIN_MIN_DEPTH_USDC"
#: Margine extra (in frazione di EV) quando il libro e' sottile.
EV_MULT_LIQ_ENV = "BRAIN_EV_MULT_LIQUIDITY"
#: Margine extra massimo quando lo sharp si muove velocemente.
EV_MULT_VOL_ENV = "BRAIN_EV_MULT_VOLATILITY"
#: Velocita' di riferimento (%/min): al di sopra il margine extra sale (cap 1x).
VOL_REF_ENV = "BRAIN_VOLATILITY_PP_MIN"
#: Percentuale del bankroll ammessa per blocco correlato (default = cap del
#: progetto, `auto_bet.CORRELATION_CAP_PCT`). Env solo per taratura.
SHIELD_CAP_ENV = "BRAIN_SHIELD_CAP_PCT"

DEFAULT_MIN_DEPTH_USDC = 20.0
DEFAULT_EV_MULT_LIQUIDITY = 0.25
DEFAULT_EV_MULT_VOLATILITY = 0.20
DEFAULT_VOL_REF_PCT_MIN = 0.20
DEFAULT_SHIELD_CAP_PCT = 0.30


def _num_env(name: str, default: float, *, minimum: float = 0.0) -> float:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return max(float(raw), minimum)
    except (TypeError, ValueError):
        logger.warning("brain: %s='%s' non numerico, uso %.3f", name, raw, default)
        return default


def min_depth_usdc() -> float:
    return _num_env(MIN_DEPTH_ENV, DEFAULT_MIN_DEPTH_USDC, minimum=1.0)


def ev_mult_liquidity() -> float:
    return _num_env(EV_MULT_LIQ_ENV, DEFAULT_EV_MULT_LIQUIDITY, minimum=0.0)


def ev_mult_volatility() -> float:
    return _num_env(EV_MULT_VOL_ENV, DEFAULT_EV_MULT_VOLATILITY, minimum=0.0)


def volatility_ref_pct_min() -> float:
    return _num_env(VOL_REF_ENV, DEFAULT_VOL_REF_PCT_MIN, minimum=1e-6)


def base_ev_min(market: str = "", league: str = "") -> float:
    """Soglia EV di base: la piu' severa fra MERCATO e TIER di lega.

    Direttiva 04/10/2026 (punto 4): i mercati LIQUIDI (Asian Handicap,
    Over/Under, Totals, BTTS, Moneyline) hanno una soglia piu' bassa
    (EV > 1.0%). Direttiva 08/10/2026: si aggiunge la dimensione del TIER di
    lega (core 1.5%, altrimenti la soglia protettiva) con precedenza "la piu'
    severa". Entrambe le regole vivono in `value_filter.ev_min`: qui non si
    ricopia nessuna soglia.
    """
    from value_filter import ev_min
    return float(ev_min(league, market))


def shield_cap_pct() -> float:
    """Cap del blocco correlato: quello di `auto_bet` (30%), fallback su env."""
    try:
        import auto_bet
        return float(auto_bet.CORRELATION_CAP_PCT)
    except Exception:
        return _num_env(SHIELD_CAP_ENV, DEFAULT_SHIELD_CAP_PCT, minimum=0.0)


def dynamic_ev_min(*, depth_usdc: Optional[float] = None,
                   velocity_pct_min: Optional[float] = None,
                   market: str = "", league: str = "") -> dict[str, Any]:
    """Soglia EV dinamica: parte dalla base e puo' solo SALIRE.

    Ritorna `{base_ev_min, ev_min, ev_multiplier, dynamic_reason}`. La
    liquidita' bassa aggiunge un margine FISSO; la volatilita' un margine
    proporzionale (fino a 1x il proprio extra quando la velocita' raddoppia
    la riferimento). Deterministico e senza metriche inventate: se un dato
    manca, quel componente non aggiunge nulla.
    """
    base = base_ev_min(market, league)
    mult = 1.0
    reasons: list[str] = []

    if depth_usdc is not None:
        try:
            depth = float(depth_usdc)
        except (TypeError, ValueError):
            depth = None
        floor = min_depth_usdc()
        if depth is not None and depth < floor:
            mult += ev_mult_liquidity()
            reasons.append(f"liquidita' {depth:.1f} < {floor:.1f} (+{ev_mult_liquidity()*100:.0f}%)")

    if velocity_pct_min is not None:
        try:
            vel = abs(float(velocity_pct_min))
        except (TypeError, ValueError):
            vel = 0.0
        ref = volatility_ref_pct_min()
        if vel > 0:
            extra = ev_mult_volatility() * min(vel / ref, 1.0)
            if extra > 1e-9:
                mult += extra
                reasons.append(f"volatilita' {vel:.3f}%/min (+{extra*100:.0f}%)")

    return {
        "base_ev_min": round(base, 6),
        "ev_min": round(base * mult, 6),
        "ev_multiplier": round(mult, 4),
        "dynamic_reason": "; ".join(reasons) if reasons else "base",
    }


def _signal_ev(signal: OracleSignal) -> Optional[float]:
    """EV del segnale: quello dichiarato, altrimenti derivato da p e quota."""
    if signal.ev is not None:
        try:
            return float(signal.ev)
        except (TypeError, ValueError):
            return None
    if signal.true_prob is None:
        return None
    try:
        return float(signal.true_prob) * float(signal.price) - 1.0
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Portfolio Shield — concentrazione sul blocco correlato (leghe)
# ---------------------------------------------------------------------------

def _open_live_bets(conn=None) -> list[dict]:
    """Puntate REALI ancora aperte (`mode='live'`, non saldate). LETTURA.

    Ritorna `[{match_id, league, stake}]`. Un errore di lettura ritorna `[]`
    ma lo dichiara il chiamante col motivo (`read_error`): un recinto di cui
    non si sa nulla non deve mai autorizzare in silenzio (fail-closed a valle).

    ⚠️ La LEGA non vive su `bets` (lo schema non ha quella colonna): arriva da
    `matches` con un LEFT JOIN sulla chiave `bets.match_id = matches.id`.
    Senza il JOIN la query sollevava `no such column: league` a OGNI lettura e
    il Portfolio Shield avrebbe bloccato TUTTI gli ordini per sempre
    (fail-closed su un errore che non era un errore di rete). LEFT e non INNER:
    una bet orfana (senza riga in `matches`, es. i batch sx-* vecchi) resta
    visibile come esposizione — con lega vuota — invece di sparire dal
    conteggio, altrimenti il recinto sottostimerebbe il capitale in gioco.
    """
    try:
        if conn is None:
            from tracker import _get_conn  # pigro: `import agents` resta leggero
            conn = _get_conn()
        cursor = conn.execute(
            "SELECT b.match_id, COALESCE(m.league, '') AS league, b.stake "
            "FROM bets b LEFT JOIN matches m ON m.id = b.match_id "
            "WHERE b.mode = 'live' "
            "AND (b.esito_finale IS NULL OR TRIM(b.esito_finale) = '')")
        out: list[dict] = []
        for row in cursor.fetchall():
            try:
                out.append({
                    "match_id": str(row[0] or ""),
                    "league": str(row[1] or ""),
                    "stake": float(row[2] or 0.0),
                })
            except (IndexError, TypeError, ValueError):
                continue
        return out
    except Exception as exc:
        logger.debug("brain: ordini aperti non leggibili (%s)", exc)
        raise


def _group_key(league: str) -> str:
    """Chiave del blocco correlato: la lega canonica, mai vuota."""
    from value_filter import canonical_league
    canon = canonical_league(league or "")
    return (canon or str(league or "")).strip().lower()


def shield_decision(*, league: str, bankroll: float, open_bets: Sequence[dict],
                    min_ticket: float) -> dict[str, Any]:
    """Verdetto del Portfolio Shield per un nuovo trade del blocco `league`.

    Ritorna `{action, factor, max_usdc, reason}`:
    - `allow`  — nessuna restrizione (nessuna esposizione sul blocco);
    - `scale`  — spazio residuo finito: la Finanza non puo' superare
      `max_usdc` (frazione = residuo/cap, mai sopra 1.0);
    - `block`  — blocco saturo o residuo sotto il ticket minimo.
    """
    try:
        bk = float(bankroll)
    except (TypeError, ValueError):
        bk = 0.0
    if bk <= 0:
        return {"action": "block", "factor": 0.0, "max_usdc": 0.0,
                "reason": "bankroll non disponibile"}
    cap = bk * shield_cap_pct()
    if cap <= 0:
        return {"action": "block", "factor": 0.0, "max_usdc": 0.0,
                "reason": "cap di correlazione non calcolabile"}
    key = _group_key(league)
    group_stake = 0.0
    total_stake = 0.0
    for row in open_bets or []:
        try:
            stake = float(row.get("stake") or 0.0)
        except (TypeError, ValueError):
            stake = 0.0
        total_stake += stake
        if key and _group_key(str(row.get("league") or "")) == key:
            group_stake += stake
    residual = cap - group_stake
    if residual <= 0:
        return {"action": "block", "factor": 0.0, "max_usdc": 0.0,
                "reason": (f"blocco '{league or key}' saturo: "
                           f"{group_stake:.2f}/{cap:.2f} USDC")}
    if residual < float(min_ticket):
        return {"action": "block", "factor": 0.0, "max_usdc": 0.0,
                "reason": (f"residuo blocco {residual:.2f} < ticket minimo "
                           f"{float(min_ticket):.2f} USDC")}
    if group_stake <= 0:
        return {"action": "allow", "factor": 1.0, "max_usdc": None,
                "reason": f"nessuna esposizione su '{league or key}'"}
    return {"action": "scale", "factor": round(min(residual / cap, 1.0), 4),
            "max_usdc": round(residual, 2),
            "reason": (f"blocco '{league or key}' a {group_stake:.2f}/{cap:.2f} "
                       f"USDC: residuo {residual:.2f}")}


class BrainAgent:
    """Valida i segnali arricchiti: soglia EV dinamica + Portfolio Shield."""

    name = "brain"

    def __init__(self, *, bankroll: float = 0.0,
                 open_bets_fn: Optional[Callable[[], Sequence[dict]]] = None,
                 min_ticket_fn: Optional[Callable[[], float]] = None) -> None:
        # Iniettabili: i test girano su un ledger/ticket finti, offline.
        self.bankroll = float(bankroll or 0.0)
        self.open_bets_fn = open_bets_fn
        self.min_ticket_fn = min_ticket_fn

    # -- dipendenze (import PIGRO) ----------------------------------------
    def _open_bets(self, conn=None) -> tuple[list[dict], str]:
        if self.open_bets_fn is not None:
            try:
                return list(self.open_bets_fn() or []), ""
            except Exception as exc:
                return [], f"read_error:{type(exc).__name__}"
        try:
            return _open_live_bets(conn), ""
        except Exception as exc:
            return [], f"read_error:{type(exc).__name__}"

    def _min_ticket(self) -> float:
        if self.min_ticket_fn is not None:
            try:
                return float(self.min_ticket_fn())
            except Exception:
                return 0.0
        try:
            from decision.stake_engine import aggressive_config
            return float(aggressive_config()["min_ticket"])
        except Exception:
            return 2.0

    # -- ciclo -------------------------------------------------------------
    def process(self, signals: Sequence[OracleSignal], *,
                conn=None) -> BrainOutput:
        out = BrainOutput()
        open_bets, read_err = self._open_bets(conn)
        min_ticket = self._min_ticket()
        for signal in signals or []:
            try:
                trade = self._one(signal, open_bets=open_bets,
                                  read_err=read_err, min_ticket=min_ticket)
            except Exception as exc:  # un segnale rotto non ferma gli altri
                logger.debug("brain: segnale %s non validato (%s)",
                             getattr(signal, "signal_id", "?"), exc)
                continue
            if trade is None:
                out.rejected += 1
                continue
            out.trades.append(trade)
            if trade.shield_action == "block":
                out.blocked += 1
            elif trade.shield_action == "scale":
                out.scaled += 1
            else:
                out.validated += 1
        return out

    def _one(self, signal: OracleSignal, *, open_bets: Sequence[dict],
             read_err: str, min_ticket: float) -> Optional[ValidatedTrade]:
        ev = _signal_ev(signal)
        dyn = dynamic_ev_min(depth_usdc=signal.depth_usdc,
                             velocity_pct_min=signal.velocity_pct_min,
                             market=signal.market,
                             league=getattr(signal, "league", ""))
        if ev is not None and ev < dyn["ev_min"]:
            logger.debug("brain: %s EV %.4f < soglia dinamica %.4f (%s)",
                         signal.signal_id, ev, dyn["ev_min"],
                         dyn["dynamic_reason"])
            return None

        # Shield fail-closed sulla LETTURA: senza sapere cosa e' aperto non si
        # autorizza un nuovo ordine sullo stesso blocco.
        if read_err:
            shield = {"action": "block", "factor": 0.0, "max_usdc": 0.0,
                      "reason": f"stato ordini aperti non leggibile ({read_err})"}
        else:
            shield = shield_decision(league=signal.league, bankroll=self.bankroll,
                                     open_bets=open_bets, min_ticket=min_ticket)

        return ValidatedTrade(
            signal_id=signal.signal_id,
            match_id=signal.match_id,
            market=signal.market,
            esito=signal.esito,
            league=signal.league,
            home=getattr(signal, "home", "") or "",
            away=getattr(signal, "away", "") or "",
            kickoff=getattr(signal, "kickoff", None),
            price=signal.price,
            true_prob=signal.true_prob,
            ev=ev,
            edge=getattr(signal, "edge", None),
            depth_usdc=signal.depth_usdc,
            devig_method=getattr(signal, "devig_method", "") or "",
            shin_z=getattr(signal, "shin_z", None),
            fair_odds=getattr(signal, "fair_odds", None),
            base_ev_min=dyn["base_ev_min"],
            dynamic_ev_min=dyn["ev_min"],
            ev_multiplier=dyn["ev_multiplier"],
            dynamic_reason=dyn["dynamic_reason"],
            shield_action=shield["action"],
            shield_factor=shield["factor"],
            shield_max_usdc=shield["max_usdc"],
            shield_reason=shield["reason"],
            analysis={
                "steam_move": signal.steam_move,
                "move_pct": signal.move_pct,
                "velocity_pct_min": signal.velocity_pct_min,
                "juice": signal.juice,
                "juice_anomaly": signal.juice_anomaly,
                "fresh": signal.fresh,
                "sources": list(signal.sources or []),
                "devig_method": getattr(signal, "devig_method", "") or "",
                "shin_z": getattr(signal, "shin_z", None),
                "fair_odds": getattr(signal, "fair_odds", None),
            },
        )
