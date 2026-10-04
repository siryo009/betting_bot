"""smart_hedging.py — Copertura intelligente pre-match (contropuntata parziale).

Direttiva del proprietario (26/09/2026): se il mercato si muove in modo
**drastico** a favore di una posizione aperta (o l'oracolo rileva un'anomalia),
il bot valuta di piazzare una **contropuntata parziale** (hedge) sugli esiti
complementari per **bloccare il profitto** prima del fischio d'inizio.

Scelta confermata dal proprietario: **hedging LIVE con soglia** (ordini reali),
a differenza della ponderazione per campionato (env-gated, default OFF).

COME FUNZIONA (matematica verificabile, nessuna euristica nascosta)

Sia `S` lo stake della puntata aperta su `E` alla quota `Oe`; siano `O1`, `O2` le
quote CORRENTI dei due esiti complementari. La copertura piena e':

    H_i = f x S x Oe / O_i          (f = HEDGE_FRACTION, default 1.0)

Con `f = 1` l'incasso e' lo STESSO in tutti e tre gli esiti (`= S x Oe`) — e'
un lock esatto, non una stima:

    profitto garantito = S x Oe - S - sum(H_i)
    ROI bloccato       = profitto garantito / (S + sum(H_i))

Il lock esiste solo se `Oe x (1 - 1/O1 - 1/O2) > 1`: quando il mercato si e'
mosso verso di noi i complementari si allungano e la disuguaglianza puo'
verificarsi. Con `f < 1` la copertura e' PARZIALE: riduce il rischio e mantiene
parte dell'upside, ma NON garantisce il profitto (documentato, non nascosto).

TRIGGER (l'"anomalia"): la quota corrente della NOSTRA selezione deve essere
piu' CORTA di almeno `HEDGE_MIN_MOVE_PCT` rispetto alla quota di ingresso (il
mercato si e' mosso verso di noi). Un movimento CONTRO di noi non genera ordini:
coprirsi allora costa e non blocca nulla (si registra come telemetria).

SICUREZZE (fail-closed dove si spende denaro, fail-open sulle letture)
- ordini SOLO se l'ordine supera `HEDGE_MIN_LOCK_PCT` di ROI bloccato;
- ogni gamba dev'essere eseguibile: `[HEDGE_MIN_STAKE_USDC, HEDGE_MAX_STAKE]`
  (il minimo ordine di SX Bet e' 1 USDC: sotto, la copertura non e' fisicamente
  piazzabile e NON la si inventa);
- rispetta il kill switch, lo stop-loss giornaliero e quello settimanale,
  il DRY-RUN e la finestra pre-kickoff (`HEDGE_MIN_MINUTES`..`HEDGE_HORIZON_H`);
- **nessuna riga doppia**: se esiste GIA' una riga in `bets` per
  (match_id, esito complementare) — aperta O chiusa — non si ordina (una riga
  chiusa non puo' essere sovrascritta da `save_bet`: l'ordine resterebbe non
  registrato, ed e' esattamente il caso da evitare);
- SOLO 1X2: sugli altri mercati la copertura cambia forma (push, linee) e non
  si applica una formula pensata per il tre-esiti (`not_1x2`, mai indovinato).

L'ESECUZIONE non e' reimplementata: si delega ad `auto_bet._live_fill` (lo
stesso percorso del giro ordini: floor, liquidita', `resolve_match_market`).
Duplicarlo sarebbe il modo classico per farlo divergere dalla produzione.

Telemetria: ogni evento (proposta, skip, hedge piazzato) finisce in un JSONL sul
volume (`HEDGE_LOG`) e il modulo NON importa `bot`: aggrega e notifica il
chiamante. Diagnostica + ordini: nessun'altra funzione qui blocca le puntate.

Env:
  SMART_HEDGING                (0|1, default 1: LIVE con soglia)
  HEDGE_MIN_MOVE_PCT           (default 0.05: 5% di movimento verso di noi)
  HEDGE_MIN_LOCK_PCT           (default 0.01: 1% di ROI garantito minimo)
  HEDGE_FRACTION               (default 1.0: 1.0 = lock esatto, <1 = parziale)
  HEDGE_MIN_STAKE_USDC         (default 1.0 = minimo ordine SX Bet)
  HEDGE_MAX_STAKE_USDC         (default 5.0 per gamba)
  HEDGE_MIN_MINUTES            (default 10: mai a ridosso del fischio)
  HEDGE_HORIZON_H              (default 24: solo partite entro N ore)
  HEDGE_LOG                    (default DATA_DIR/execution/hedge_events.jsonl)

CLI:
  venv/bin/python smart_hedging.py [--opportunities] [--run] [--report] [--json]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

from config import DATA_DIR

logger = logging.getLogger("smart_hedging")


def _flag(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def _num(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return float(default)


# --- Accessori a runtime (stesse scelte di adaptive_weighting) ---------------

def enabled() -> bool:
    """Interruttore della copertura. Default ON: LIVE con soglia (direttiva)."""
    return _flag("SMART_HEDGING", "1")


def min_move_pct() -> float:
    return max(0.0, _num("HEDGE_MIN_MOVE_PCT", 0.05))


def min_lock_pct() -> float:
    return _num("HEDGE_MIN_LOCK_PCT", 0.01)


def fraction() -> float:
    f = _num("HEDGE_FRACTION", 1.0)
    return max(0.0, min(1.0, f))


def min_stake_usdc() -> float:
    return max(0.0, _num("HEDGE_MIN_STAKE_USDC", 1.0))


def max_stake_usdc() -> float:
    return max(0.0, _num("HEDGE_MAX_STAKE_USDC", 5.0))


def min_minutes() -> float:
    return max(0.0, _num("HEDGE_MIN_MINUTES", 10.0))


def horizon_h() -> float:
    return max(0.0, _num("HEDGE_HORIZON_H", 24.0))


def log_path() -> Path:
    return Path(os.getenv("HEDGE_LOG",
                          str(DATA_DIR / "execution" / "hedge_events.jsonl")))


OUTCOMES_1X2 = ("1", "X", "2")

#: Motivi machine-readable (mai prosa nei dati).
REASON_NO_PRICE = "no_price"
REASON_MOVE_INSUFFICIENT = "move_insufficient"
REASON_NO_LOCK = "no_lock"
REASON_ALREADY_OPEN = "already_open"
REASON_NOT_1X2 = "not_1x2"
REASON_NOT_LIVE = "not_live"
REASON_WINDOW = "window"
REASON_BELOW_MIN = "leg_below_min"
REASON_ABOVE_CAP = "leg_above_cap"
REASON_BLOCKED = "blocked"
REASON_DRY_RUN = "dry_run"
REASON_FILL_FAILED = "fill_failed"
REASON_OK = "ok"


# ---------------------------------------------------------------------------
# Telemetria (fail-safe: mai un'eccezione verso il chiamante)
# ---------------------------------------------------------------------------

def record_event(kind: str, reason: str, **fields) -> dict:
    """Appende un evento al JSONL. Fail-safe: un errore non propaga mai."""
    now = datetime.now(timezone.utc)
    evt = {"ts": now.isoformat(), "ts_epoch": now.timestamp(),
           "kind": kind, "reason": reason}
    for k, v in fields.items():
        if v is not None:
            evt[k] = v
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(evt, ensure_ascii=False, default=str) + "\n")
    except Exception as e:                               # pragma: no cover
        logger.debug("smart_hedging: log non scritto (%s)", e)
        evt["error"] = str(e)
    return evt


def iter_events(days: Optional[float] = None, now: Optional[datetime] = None
                ) -> List[dict]:
    """Eventi del log (piu' recenti prima). Righe corrotte ignorate.

    Legge anche le generazioni `.gz` (rotazione automatica dal 03/10/2026).
    `now` resta iniettabile: il taglio della finestra usa QUEL riferimento,
    cosi' i test che simulano l'orologio continuano a valere.
    """
    from telemetry_logs import iter_events as _iter_jsonl
    try:
        events = list(_iter_jsonl(log_path()))
    except Exception:                                            # pragma: no cover
        return []
    if days is None:
        return events
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=float(days))
    out: List[dict] = []
    for evt in events:
        ts = _parse_iso(evt.get("ts"))
        if ts is None or ts < cutoff:
            continue
        out.append(evt)
    return out


def _parse_iso(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def summary(days: float = 7, now: Optional[datetime] = None) -> dict:
    """Riepilogo della copertura: proposte, skips per motivo, hedge piazzati."""
    events = iter_events(days=days, now=now)
    by_reason: Dict[str, int] = {}
    by_kind: Dict[str, int] = {}
    placed: List[dict] = []
    locked = 0.0
    for e in events:
        by_kind[e.get("kind", "?")] = by_kind.get(e.get("kind", "?"), 0) + 1
        by_reason[e.get("reason", "?")] = by_reason.get(e.get("reason", "?"), 0) + 1
        if e.get("kind") == "placed":
            placed.append(e)
            try:
                locked += float(e.get("locked_profit") or 0.0)
            except (TypeError, ValueError):
                pass
    return {
        "days": days,
        "events": len(events),
        "by_kind": by_kind,
        "by_reason": by_reason,
        "placed": len(placed),
        "locked_profit": round(locked, 4),
        "last": placed[0] if placed else None,
    }


# ---------------------------------------------------------------------------
# Matematica della copertura (pura: nessun DB, nessuna rete)
# ---------------------------------------------------------------------------

def hedge_plan(stake: float, entry_odds: float,
               complement_odds: Dict[str, float],
               const: Optional[tuple] = None,
               f: Optional[float] = None) -> dict:
    """Piano di copertura PURO per un back su 1X2 (3 esiti).

    Il profitto bloccato e' il **peggiore** fra i tre esiti (non una stima):
    con `f = 1` i tre valori sono identici (lock esatto); con `f < 1` la
    copertura e' parziale e il peggiore puo' essere NEGATIVO -> `no_lock`.

    `const` = (min_stake, max_stake) opzionale (per i test); altrimenti env.
    Ritorna `{"ok", "reason", "legs", "outlay", "payout", "payouts",
    "locked_profit", "locked_roi"}`. Nessun ordine: solo il calcolo.
    """
    out = {"ok": False, "reason": None, "legs": [], "outlay": None,
           "payout": None, "payouts": None, "locked_profit": None,
           "locked_roi": None}
    try:
        stake = float(stake)
        entry_odds = float(entry_odds)
    except (TypeError, ValueError):
        out["reason"] = REASON_NO_PRICE
        return out
    if stake <= 0 or entry_odds <= 1.0:
        out["reason"] = REASON_NO_PRICE
        return out
    # `complement_odds` deve contenere ESATTAMENTE i due esiti complementari:
    # l'esito della nostra posizione e' quello che manca (l'entry odds arriva
    # come parametro, quindi non c'e' ambiguita'). Fail-closed se non torna.
    prices = {}
    missing: List[str] = []
    for c in OUTCOMES_1X2:
        raw = (complement_odds or {}).get(c)
        if raw is None:
            missing.append(c)
            continue
        try:
            o = float(raw)
        except (TypeError, ValueError):
            out["reason"] = REASON_NO_PRICE
            return out
        if o <= 1.0:
            out["reason"] = REASON_NO_PRICE
            return out
        prices[c] = o
    if len(prices) != 2 or len(missing) != 1:
        out["reason"] = REASON_NO_PRICE
        return out
    entry = missing[0]
    lo, hi = const if const else (min_stake_usdc(), max_stake_usdc())
    frac = fraction() if f is None else max(0.0, min(1.0, float(f)))
    legs = []
    total = 0.0
    for c, o in prices.items():
        h = round(frac * stake * entry_odds / o, 6)
        if h < lo - 1e-9:
            out["reason"] = REASON_BELOW_MIN
            out["legs"] = [{"esito": c, "odds": o, "stake": h}]
            return out
        if hi > 0 and h > hi + 1e-9:
            out["reason"] = REASON_ABOVE_CAP
            out["legs"] = [{"esito": c, "odds": o, "stake": h}]
            return out
        legs.append({"esito": c, "odds": o, "stake": h})
        total += h
    out["legs"] = legs
    out["outlay"] = round(stake + total, 6)
    # Netto su OGNI esito possibile, poi il peggiore: e' l'unico numero che
    # si puo' chiamare "bloccato" senza mentire (con f=1 i tre coincidono).
    payouts: Dict[str, float] = {}
    for o in OUTCOMES_1X2:
        others = sum(l["stake"] for l in legs if l["esito"] != o)
        if o == entry:
            payouts[o] = stake * (entry_odds - 1.0) - total
        else:
            payouts[o] = -stake + _leg_stake(legs, o) * (prices[o] - 1.0) \
                - others
    profit = min(payouts.values())
    out["payouts"] = {k: round(v, 6) for k, v in payouts.items()}
    out["locked_profit"] = round(profit, 6)
    out["payout"] = round(out["outlay"] + profit, 6)
    if profit <= 0 or out["outlay"] <= 0:
        out["reason"] = REASON_NO_LOCK
        return out
    out["locked_roi"] = round(profit / out["outlay"], 6)
    if out["locked_roi"] < min_lock_pct():
        out["reason"] = REASON_NO_LOCK
        return out
    out["ok"] = True
    out["reason"] = REASON_OK
    return out


def _leg_stake(legs: List[dict], esito: str) -> float:
    for l in legs:
        if l["esito"] == esito:
            return float(l["stake"])
    return 0.0


# ---------------------------------------------------------------------------
# Rilevamento (letture iniettabili: i test girano offline)
# ---------------------------------------------------------------------------

def _open_live_bets(now: Optional[datetime] = None) -> List[dict]:
    """Puntate LIVE aperte su partite non ancora iniziate e nella finestra."""
    out: List[dict] = []
    try:
        import tracker
        conn = tracker._get_conn()
        rows = conn.execute(
            '''SELECT b.match_id, b.esito, b.price, b.stake, b.mode,
                      m.home_team, m.away_team, m.commence_time, m.league
                 FROM bets b JOIN matches m ON m.id = b.match_id
                WHERE b.esito_finale IS NULL AND b.mode = 'live'
                  AND b.stake > 0
                ORDER BY b.id DESC''').fetchall()
        conn.close()
    except Exception as e:
        logger.debug("smart_hedging: lettura puntate fallita (%s)", e)
        return out
    now = now or datetime.now(timezone.utc)
    for (mid, esito, price, stake, mode, home, away, kick, league) in rows:
        k = _parse_iso(kick)
        if k is None:
            continue
        mins = (k - now).total_seconds() / 60.0
        if mins < min_minutes() or mins > horizon_h() * 60.0:
            continue
        out.append({"match_id": mid, "esito": (esito or "").strip(),
                    "price": float(price or 0.0), "stake": float(stake or 0.0),
                    "mode": mode, "home": home, "away": away,
                    "commence": kick, "league": league, "minutes": mins})
    return out


def _bet_row_exists(match_id: str, esito: str) -> bool:
    """True se esiste UNA riga in `bets` per la coppia (aperta O chiusa).

    Usata come guardia: `save_bet` ha UNIQUE(match_id, esito) e su una riga
    CHIUSA l'UPDATE e' filtrato -> l'ordine reale resterebbe non registrato.
    """
    try:
        import tracker
        conn = tracker._get_conn()
        row = conn.execute("SELECT 1 FROM bets WHERE match_id=? AND esito=? "
                           "LIMIT 1", (match_id, esito)).fetchone()
        conn.close()
        return row is not None
    except Exception:
        return True          # fail-closed: senza lettura NON si ordina


def _canonical_esito(esito: str) -> Optional[str]:
    """Normalizza l'esito in 1/X/2 (importa la regola di produzione)."""
    e = str(esito or "").strip()
    if e in OUTCOMES_1X2:
        return e
    low = e.lower()
    if low in ("draw", "pareggio", "tie"):
        return "X"
    return None


def complement_of(esito: str) -> str:
    """Esito complementare di una gamba hedge su un 1X2 a 2 esiti.

    Le gambe sono sempre due (i complementari dell'esito della posizione):
    la guardia `already_open` controlla UNA delle due per sapere se la
    posizione e' stata coperta. Le leg portano l'esito canonico (place_hedge
    usa leg["esito"] = esito_key canonico), quindi il confronto e' diretto.
    """
    return "2" if esito == "1" else "1"


def _sx_price(home: str, away: str, esito: str,
              kickoff_iso: Optional[str]) -> Optional[float]:
    """Miglior prezzo BACK corrente su SX per (partita, esito). None se ignoto.

    Lettura PUBBLICA: nessun ordine. Fail-safe: qualunque errore -> None (il
    chiamante non ordina senza un prezzo reale).
    """
    try:
        import execution_engine as ee
        engine = ee.ExecutionEngine()
        prov = engine.provider
        if isinstance(prov, ee.DryRunProvider):
            return None
        mkt = ee.resolve_match_market(prov, home, away, esito, kickoff_iso)
        if not mkt:
            return None
        return prov.best_back_price(mkt["market_id"], mkt["selection_id"])
    except Exception as e:
        logger.debug("smart_hedging: prezzo SX non leggibile (%s)", e)
        return None


def find_opportunities(now: Optional[datetime] = None,
                       bets: Optional[List[dict]] = None,
                       price_lookup: Optional[Callable] = None,
                       const: Optional[tuple] = None,
                       f: Optional[float] = None) -> List[dict]:
    """Proposte di copertura per le puntate LIVE aperte (sola lettura).

    Non ordina nulla: valuta e ritorna, con un `reason` per ogni scarto. Le
    letture sono iniettabili (`bets`, `price_lookup`) cosi' i test girano
    offline con dati finti.
    """
    lookup = price_lookup or _sx_price
    rows = bets if bets is not None else _open_live_bets(now=now)
    out: List[dict] = []
    for bet in rows:
        prop = {"match_id": bet.get("match_id"), "home": bet.get("home"),
                "away": bet.get("away"), "commence": bet.get("commence"),
                "league": bet.get("league"), "esito": bet.get("esito"),
                "entry_odds": bet.get("price"), "stake": bet.get("stake"),
                "minutes": bet.get("minutes")}
        entry = _canonical_esito(bet.get("esito"))
        if entry is None:
            out.append({**prop, "ok": False, "reason": REASON_NOT_1X2})
            continue
        # Posizione GIA' coperta (guardia in detection, non solo in
        # esecuzione): senza, il job ogni 15' ri-coprirebbe la stessa bet
        # all'infinito e le gambe hedge stesse (bet live X/2) verrebbero
        # valutate come posizioni da coprire (hedge dell'hedge).
        try:
            from tracker import bet_exists_open
            if bet_exists_open(bet.get("match_id"), complement_of(entry)):
                out.append({**prop, "ok": False,
                            "reason": REASON_ALREADY_OPEN})
                continue
        except Exception:
            pass  # lettura fallita: il fill gia' la ri-controlla (fail-closed)
        prices = {}
        for c in OUTCOMES_1X2:
            if c == entry:
                continue
            try:
                prices[c] = lookup(bet.get("home"), bet.get("away"), c,
                                   bet.get("commence"))
            except Exception:
                prices[c] = None
        prop["complement_odds"] = prices
        current_own = None
        try:
            current_own = lookup(bet.get("home"), bet.get("away"), entry,
                                 bet.get("commence"))
        except Exception:
            current_own = None
        prop["current_odds"] = current_own
        if current_own and float(bet.get("price") or 0) > 0:
            prop["move_pct"] = round(
                float(current_own) / float(bet["price"]) - 1.0, 6)
        if not current_own or any(v is None for v in prices.values()):
            out.append({**prop, "ok": False, "reason": REASON_NO_PRICE})
            continue
        # Il mercato deve essersi mosso VERSO di noi in modo drastico: la
        # quota della nostra selezione si e' corta.
        if prop.get("move_pct") is None or \
                prop["move_pct"] > -min_move_pct():
            out.append({**prop, "ok": False,
                        "reason": REASON_MOVE_INSUFFICIENT})
            continue
        plan = hedge_plan(float(bet.get("stake") or 0),
                          float(bet.get("price") or 0),
                          prices, const=const, f=f)
        out.append({**prop, **plan})
    return out


def _blocked_reason() -> Optional[str]:
    """Motivo del blocco (kill switch / stop giornaliero / settimanale)."""
    try:
        import auto_bet
        status = auto_bet.kill_switch_status()
        if str(status.get("effective") or "").lower() != "live":
            return f"kill_switch={status.get('effective')}"
        daily = auto_bet.daily_stop_status()
        if daily.get("stopped"):
            return "daily_stop"
        weekly = auto_bet.weekly_stop_status()
        if weekly.get("stopped"):
            return "weekly_stop"
    except Exception as e:
        logger.debug("smart_hedging: stato blocchi non leggibile (%s)", e)
        return "stato_non_leggibile"
    return None


# ---------------------------------------------------------------------------
# Esecuzione (delega a `auto_bet._live_fill`: nessuna reimplementazione)
# ---------------------------------------------------------------------------

def place_hedge(prop: dict, *, fill: Optional[Callable] = None) -> List[dict]:
    """Piazza le gambe di copertura e le registra sul ledger. Ritorna gli esiti.

    `fill` e' iniettabile (default `auto_bet._live_fill`). Ogni gamba che
    fallisce e' registrata come leg del singolo ordine: le gambe non sono
    atomiche (un exchange non offre transazioni multi-mercato) — se la prima
    va e la seconda no, si resta coperti su un solo complementare, che e'
    comunque un miglioramento rispetto a nessuna copertura, e il JSONL lo
    dice.

    La scrittura sul ledger sta QUI e non nel chiamante: un ordine reale non
    deve poter esistere senza la sua riga (stessa regola del giro ordini, dove
    `save_bet` segue immediatamente `_live_fill`).
    """
    results: List[dict] = []
    if fill is None:
        try:
            import auto_bet
            fill = auto_bet._live_fill
        except Exception as e:
            logger.warning("smart_hedging: _live_fill non disponibile (%s)", e)
            return [{"ok": False, "reason": REASON_FILL_FAILED,
                     "error": str(e)}]
    for leg in prop.get("legs") or []:
        esito, stake, odds = leg["esito"], float(leg["stake"]), float(leg["odds"])
        if _bet_row_exists(prop["match_id"], esito):
            results.append({"esito": esito, "ok": False,
                            "reason": REASON_ALREADY_OPEN})
            continue
        pick = {"match_id": prop["match_id"], "home": prop.get("home"),
                "away": prop.get("away"), "esito_key": esito,
                "mercato": "1X2", "commence": prop.get("commence"),
                "best_ev": None, "home_team": prop.get("home")}
        try:
            filled = fill(pick, stake, odds)
        except Exception as e:
            logger.warning("smart_hedging: gamba %s fallita (%s)", esito, e)
            filled = None
        if not filled or not filled.get("ok"):
            results.append({"esito": esito, "ok": False, "stake": stake,
                            "odds": odds, "reason": REASON_FILL_FAILED,
                            "error": (filled or {}).get("error")
                            if isinstance(filled, dict) else None})
            continue
        results.append({"esito": esito, "ok": True,
                        "stake": float(filled.get("stake") or stake),
                        "odds": float(filled.get("price") or odds),
                        "market_id": filled.get("market_id"),
                        "selection_id": filled.get("selection_id"),
                        "bet_id": filled.get("bet_id"),
                        "status": filled.get("status")})
    ok_legs = [l for l in results if l.get("ok")]
    if ok_legs:
        _register_bets(prop, ok_legs)
    return results


def run_hedge_cycle(now: Optional[datetime] = None, *,
                    bets: Optional[List[dict]] = None,
                    price_lookup: Optional[Callable] = None,
                    fill: Optional[Callable] = None,
                    dry_run: Optional[bool] = None) -> dict:
    """Un giro di copertura: valuta le opportunita' e piazza quelle valide.

    Ritorna `{enabled, evaluated, opportunities, placed, skipped, blocked}`.
    Fail-safe: qualunque errore diventa un campo `error`, mai un'eccezione.
    """
    res = {"enabled": enabled(), "evaluated": 0, "opportunities": 0,
           "placed": [], "skipped": [], "blocked": None, "error": None}
    if not res["enabled"]:
        return res
    try:
        if dry_run is None:
            import auto_bet
            dry_run = bool(getattr(auto_bet, "DRY_RUN", False))
    except Exception:
        dry_run = False
    props = []
    try:
        props = find_opportunities(now=now, bets=bets,
                                   price_lookup=price_lookup)
    except Exception as e:
        res["error"] = str(e)
        return res
    res["evaluated"] = len(props)
    todo = [p for p in props if p.get("ok")]
    res["opportunities"] = len(todo)
    for p in props:
        if not p.get("ok"):
            res["skipped"].append({"match_id": p.get("match_id"),
                                   "esito": p.get("esito"),
                                   "reason": p.get("reason")})
    if not todo:
        return res
    blocked = _blocked_reason()
    if blocked:
        res["blocked"] = blocked
        for p in todo:
            res["skipped"].append({"match_id": p.get("match_id"),
                                   "esito": p.get("esito"),
                                   "reason": REASON_BLOCKED})
            record_event("skip", REASON_BLOCKED, match_id=p.get("match_id"),
                         esito=p.get("esito"), detail=blocked)
        return res
    for p in todo:
        if dry_run:
            res["skipped"].append({"match_id": p.get("match_id"),
                                   "esito": p.get("esito"),
                                   "reason": REASON_DRY_RUN})
            record_event("skip", REASON_DRY_RUN, match_id=p.get("match_id"),
                         esito=p.get("esito"),
                         locked_roi=p.get("locked_roi"))
            continue
        record_event("opportunity", REASON_OK, match_id=p.get("match_id"),
                     home=p.get("home"), away=p.get("away"),
                     league=p.get("league"), esito=p.get("esito"),
                     entry_odds=p.get("entry_odds"),
                     current_odds=p.get("current_odds"),
                     move_pct=p.get("move_pct"),
                     legs=p.get("legs"), outlay=p.get("outlay"),
                     payout=p.get("payout"),
                     locked_profit=p.get("locked_profit"),
                     locked_roi=p.get("locked_roi"))
        legs = place_hedge(p, fill=fill)
        ok_legs = [l for l in legs if l.get("ok")]
        if ok_legs:
            entry = {"match_id": p.get("match_id"), "home": p.get("home"),
                     "away": p.get("away"), "league": p.get("league"),
                     "esito": p.get("esito"), "legs": legs,
                     "locked_profit": p.get("locked_profit"),
                     "locked_roi": p.get("locked_roi"),
                     "outlay": p.get("outlay")}
            res["placed"].append(entry)
            record_event("placed", REASON_OK, **entry)
        else:
            res["skipped"].append({"match_id": p.get("match_id"),
                                   "esito": p.get("esito"),
                                   "reason": REASON_FILL_FAILED})
            record_event("skip", REASON_FILL_FAILED, match_id=p.get("match_id"),
                         esito=p.get("esito"), legs=legs)
    return res


def _register_bets(prop: dict, ok_legs: List[dict]) -> None:
    """Scrive le gambe eseguite sul ledger `bets` (mode='live'). Fail-safe.

    `mercato` resta '1X2': la copertura E' un back 1X2 e il settlement e'
    guidato dall'esito — nessuna semantica nuova da insegnare al referto.
    """
    try:
        from tracker import save_bet
        for leg in ok_legs:
            save_bet(prop["match_id"], "1X2", leg["esito"],
                     market_id=leg.get("market_id"),
                     selection_id=leg.get("selection_id"),
                     price=leg.get("odds") or 0.0,
                     stake=leg.get("stake") or 0.0,
                     mode="live", status=leg.get("status"),
                     bet_id=leg.get("bet_id"))
    except Exception as e:
        logger.warning("smart_hedging: registrazione gambe fallita (%s)", e)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def format_alert(entry: dict) -> str:
    """Messaggio Telegram per un hedge piazzato (testo semplice)."""
    legs = ", ".join(f"{l['esito']} @ {float(l['odds']):.2f} "
                     f"({float(l['stake']):.2f} USDC)"
                     for l in entry.get("legs", []) if l.get("ok"))
    return (f"🛡 HEDGE piazzato — {entry.get('home')} vs {entry.get('away')}\n"
            f"Posizione coperta: {entry.get('esito')} "
            f"(lega {entry.get('league') or '?'})\n"
            f"Contropuntate: {legs}\n"
            f"Profitto bloccato ~{float(entry.get('locked_profit') or 0):+.2f} "
            f"USDC (ROI bloccato {float(entry.get('locked_roi') or 0) * 100:+.2f}%)")


def format_report(res: Optional[dict] = None) -> str:
    """Riga(s) Telegram-friendly sull'attivita' di copertura."""
    res = res or summary()
    state = "🟢 ON" if enabled() else "⚪ OFF"
    lines = [f"🛡 Copertura intelligente — {state}",
             f"Ultimi {res['days']:.0f}g: {res['placed']} hedge piazzati, "
             f"profitto bloccato {res['locked_profit']:+.2f} USDC"]
    counts = res.get("by_reason") or {}
    if counts:
        top = ", ".join(f"{k}={v}" for k, v in
                        sorted(counts.items(), key=lambda kv: -kv[1])[:5])
        lines.append(f"Motivi: {top}")
    last = res.get("last")
    if last:
        lines.append(f"Ultimo: {last.get('home')} vs {last.get('away')} "
                     f"({float(last.get('locked_roi') or 0) * 100:+.2f}%)")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Copertura intelligente pre-match (hedge parziale)")
    ap.add_argument("--opportunities", action="store_true",
                    help="elenca le proposte (sola lettura, nessun ordine)")
    ap.add_argument("--run", action="store_true",
                    help="esegue il giro (puo' piazzare ordini reali)")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if args.report:
        res = summary()
    elif args.run:
        res = run_hedge_cycle()
    else:
        props = find_opportunities()
        res = {"enabled": enabled(), "evaluated": len(props),
               "opportunities": [p for p in props if p.get("ok")],
               "proposals": props}
    if args.json:
        print(json.dumps(res, indent=2, ensure_ascii=False, default=str))
    elif args.report:
        print(format_report(res))
    else:
        print(f"Copertura: {'ON' if res.get('enabled') else 'OFF'} — "
              f"{res.get('evaluated')} puntate valutate")
        for p in res.get("proposals", res.get("opportunities", [])) or []:
            if isinstance(p, dict):
                print(f"  {p.get('match_id')} {p.get('esito')} "
                      f"{p.get('reason')} ROI={p.get('locked_roi')}")
    return 0


if __name__ == "__main__":                                # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
