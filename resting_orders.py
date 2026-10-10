"""resting_orders.py — ordini RESTING (GTC) su SX Bet: metti il prezzo e aspetta.

PERCHE' ESISTE (10/10/2026). Misurato sui log di produzione (25/09 -> 02/10):
**221 POST a /orders-v3, 126 accettati, 7 riempiti (5,5%)** e **119 cancellati
con `cancelReason=NO_LIQUIDITY`**. Il bot chiedeva il prezzo con ordini **IOC**
("riempi subito o muori") su un exchange dove al momento del giro non c'e' la
controparte a quel prezzo: l'ordine moriva e l'edge non veniva mai incassato.

Il fix del troncamento della quota ha rimosso una causa (42,5% dei prezzi era
sbagliato, ora 0%), ma NON cambia la natura del problema: prendere (taker)
pretende che la size sia gia' sul book. **Mettere (maker)** no: un ordine
resting aspetta la controparte.

⚠️ PERCHE' E' SICURO PER L'EV. Su SX un ordine BACK viene postato con una
probabilita' **arrotondata PER DIFETTO alla ladder** (`decimal_to_pct_scaled`),
quindi la quota effettiva dell'ordine e' **>= alla quota richiesta**: un
riempimento avviene alla quota del segnale o MEGLIO, mai peggio. Non esiste un
percorso in cui un resting si riempia sotto il floor EV — e' la stessa proprieta'
su cui poggia il floor EV del percorso taker.

⚠️ COM'E' FATTA LA SICUREZZA (denaro reale, quindi tutto fail-closed):
- `RESTING_MAX_OPEN` limita quanti ordini possono restare aperti insieme;
- ogni ordine ha una **deadline** (il piu' vicino fra `RESTING_TTL_MIN` e
  `kickoff - RESTING_CANCEL_BEFORE_MIN`) e una **scadenza on-chain** piu' larga
  (deadline + margine): cosi' l'ordine NON puo' morire da solo mentre lo
  guardiamo, e "non e' piu' fra gli aperti" significa **riempito**;
- `kickoff - 2 minuti` e' lo stesso pavimento del percorso d'ordine
  (`auto_bet.MIN_MINUTES_TO_START`): un resting non sopravvive al fischio;
- il RIEMPIMENTO viene scritto sul ledger `bets` (mode='live') **solo** dopo la
  riconciliazione, con la guardia di unicita' (match_id, esito): nessun
  doppione, nessuna riga senza un ordine vero sull'exchange;
- lettura degli ordini aperti **non disponibile** -> nessuna inferenza, nessuna
  scrittura (fail-closed), gli ordini restano `open` e si ritenta al giro dopo.

⚠️ IL LATO PERICOLOSO (dichiarato). Se gli ordini aperti diventano illeggibili
e la cancellazione fallisce, un ordine puo' scadere on-chain: la riga viene
marcata `expired_unconfirmed` (NON diventa una bet) e finisce nel report, cosi'
un umano puo' verificarla. Non si inventa mai un riempimento.

Il modulo NON piazza ordini da solo: `place()` e' chiamato da `auto_bet._live_fill`
quando il percorso taker non puo' essere eseguito.

CLI:
    venv/bin/python resting_orders.py [--status | --cycle] [--json]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from config import DATA_DIR

logger = logging.getLogger("resting_orders")

#: Stati di una riga del registro.
ST_OPEN = "open"
ST_FILLED = "filled"
ST_CANCELLED = "cancelled"
ST_DUPLICATE = "filled_duplicate"
ST_UNCONFIRMED = "expired_unconfirmed"

TERMINAL = (ST_FILLED, ST_CANCELLED, ST_DUPLICATE, ST_UNCONFIRMED)


def _num_env(name: str, default: float, *, minimum: Optional[float] = None) -> float:
    """Env numerico letto a RUNTIME (clamp + warning, mai un'eccezione)."""
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        val = float(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning("resting_orders: %s=%r non numerico, uso %s",
                       name, raw, default)
        return default
    if minimum is not None and val < minimum:
        logger.warning("resting_orders: %s=%s sotto il minimo, uso %s",
                       name, val, minimum)
        return minimum
    return val


def enabled() -> bool:
    """Interruttore (`RESTING_ORDERS`, default ON).

    ON di default perche' e' la leva che rimette in gioco il 94,5% di ordini
    uccisi dal `NO_LIQUIDITY`; `RESTING_ORDERS=0` ripristina esattamente il
    comportamento precedente (un ordine non eseguibile = nessun ordine).
    """
    return str(os.getenv("RESTING_ORDERS", "1")).strip().lower() not in (
        "0", "false", "no", "off")


def config() -> Dict[str, Any]:
    """Parametri efficaci (env a runtime)."""
    return {
        "enabled": enabled(),
        "max_open": int(_num_env("RESTING_MAX_OPEN", 5.0, minimum=1.0)),
        "ttl_min": _num_env("RESTING_TTL_MIN", 720.0, minimum=1.0),
        "cancel_before_min": _num_env("RESTING_CANCEL_BEFORE_MIN", 2.0,
                                      minimum=0.0),
        "expiry_margin_s": _num_env("RESTING_EXPIRY_MARGIN_S", 600.0,
                                    minimum=60.0),
        "state": state_path(),
    }


def state_path() -> str:
    """Percorso del registro (letto a runtime: i test lo isolano)."""
    return str(os.getenv("RESTING_STATE",
                         str(DATA_DIR / "execution" / "resting_orders.json")))


def min_ticket() -> float:
    """Ticket minimo del MOTORE (delega: una sola definizione).

    Sotto il ticket l'ordine non e' dimensionabile: stesso vincolo del
    percorso taker (`decision.stake_engine.aggressive_config`), import pigro.
    """
    try:
        from decision.stake_engine import aggressive_config
        return float(aggressive_config()["min_ticket"])
    except Exception:
        return 1.0


# ---------------------------------------------------------------------------
# Registro su volume (scrittura atomica, lettura fail-safe)
# ---------------------------------------------------------------------------

def _empty() -> Dict[str, Any]:
    return {"orders": [], "updated_at": None}


def load() -> Dict[str, Any]:
    """Registro completo. File assente/corrotto -> struttura vuota DICHIARATA.

    Non sovrascrive un file illeggibile: un registro di denaro corrotto non
    deve essere azzerato in silenzio (il file resta, e si logga).
    """
    path = state_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return _empty()
    except Exception as e:
        logger.warning("resting_orders: registro illeggibile (%s): %s", path, e)
        out = _empty()
        out["error"] = str(e)
        return out
    if not isinstance(data, dict) or not isinstance(data.get("orders"), list):
        logger.warning("resting_orders: registro in formato inatteso (%s)", path)
        out = _empty()
        out["error"] = "formato inatteso"
        return out
    return data


def save(state: Dict[str, Any]) -> bool:
    """Scrittura ATOMICA (tmp + replace). Mai un'eccezione al chiamante."""
    path = state_path()
    state = dict(state)
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    try:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d or ".", prefix=".resting-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(state, fh, ensure_ascii=False, indent=1)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        return True
    except Exception as e:
        logger.warning("resting_orders: scrittura registro fallita (%s): %s",
                       path, e)
        return False


def orders(*, status: Optional[str] = None) -> List[Dict[str, Any]]:
    """Righe del registro, eventualmente filtrate per stato."""
    rows = [r for r in (load().get("orders") or []) if isinstance(r, dict)]
    if status is None:
        return rows
    return [r for r in rows if r.get("status") == status]


def open_orders() -> List[Dict[str, Any]]:
    """Ordini RESTING ancora aperti (non ancora riempiti/cancellati)."""
    return orders(status=ST_OPEN)


def open_stake() -> float:
    """Capitale immobilizzato dagli ordini resting aperti (USDC)."""
    tot = 0.0
    for r in open_orders():
        try:
            tot += float(r.get("stake") or 0.0)
        except (TypeError, ValueError):
            continue
    return round(tot, 4)


def _ts(value: Any) -> Optional[datetime]:
    """ISO (con 'Z'/offset/naive) -> datetime UTC, difensivo."""
    if not value:
        return None
    try:
        s = str(value).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Piazzamento
# ---------------------------------------------------------------------------

def deadline_ts(kickoff: Any, *, now: Optional[datetime] = None,
                cfg: Optional[Dict[str, Any]] = None) -> float:
    """Istante (epoch) entro cui l'ordine resting deve essere ritirato.

    Il piu' VICINO fra: adesso + `RESTING_TTL_MIN` e
    `kickoff - RESTING_CANCEL_BEFORE_MIN`. Senza kickoff leggibile vale il solo
    TTL (l'ordine non puo' restare in giro per sempre).
    """
    cfg = cfg or config()
    now = now or _now()
    base = now.timestamp() + float(cfg["ttl_min"]) * 60.0
    ko = _ts(kickoff)
    if ko is None:
        return base
    return min(base, ko.timestamp() - float(cfg["cancel_before_min"]) * 60.0)


def place(prov, *, pick: Dict[str, Any], stake: float, price: float,
          market_id: str, selection_id: int,
          now: Optional[datetime] = None) -> Dict[str, Any]:
    """Piazza un ordine RESTING (GTC) al prezzo del segnale.

    Ritorna un dict con `placed` (registrato come resting), `filled` (si e'
    riempito SUBITO: e' un ordine normale, lo tratta il chiamante) oppure
    `reason` machine-readable quando non si piazza nulla. Non solleva mai.
    """
    out: Dict[str, Any] = {"placed": False, "filled": False, "reason": ""}
    cfg = config()
    if not cfg["enabled"]:
        out["reason"] = "disabled"
        return out
    now = now or _now()

    # Il ticket minimo del MOTORE (non il floor dell'exchange): sotto, l'ordine
    # non e' dimensionabile e non ha senso occupare un posto nel recinto.
    if float(stake or 0.0) + 1e-9 < min_ticket():
        out["reason"] = "below_min_ticket"
        return out

    state = load()
    rows = [r for r in (state.get("orders") or []) if isinstance(r, dict)]
    if state.get("error"):
        # Registro illeggibile: non si aggiunge nulla (non sapremmo cosa c'e').
        out["reason"] = "state_unreadable"
        return out
    if len([r for r in rows if r.get("status") == ST_OPEN]) >= cfg["max_open"]:
        out["reason"] = "max_open"
        return out

    key = (str(pick.get("match_id") or ""), str(pick.get("esito_key") or ""))
    for r in rows:
        if (str(r.get("match_id") or ""), str(r.get("esito") or "")) == key:
            out["reason"] = "duplicate"
            return out

    dl = deadline_ts(pick.get("commence"), now=now, cfg=cfg)
    if dl <= now.timestamp() + 60.0:
        # Troppo vicino alla scadenza: un ordine che vive meno di un giro non
        # ha speranza di trovare controparte.
        out["reason"] = "deadline_too_close"
        return out
    expiry_s = int(dl - now.timestamp()) + int(cfg["expiry_margin_s"])

    try:
        order = prov.place_limit_order(
            market_id, int(selection_id), "BACK", float(price), float(stake),
            persistence="PERSIST", expiry_seconds=expiry_s)
    except TypeError:                     # provider older: senza expiry per-ordine
        try:
            order = prov.place_limit_order(
                market_id, int(selection_id), "BACK", float(price),
                float(stake), persistence="PERSIST")
        except Exception as e:
            out["reason"] = f"error:{type(e).__name__}"
            logger.warning("resting_orders: ordine resting %s fallito: %s",
                           pick.get("match_id"), e)
            return out
    except Exception as e:
        out["reason"] = f"error:{type(e).__name__}"
        logger.warning("resting_orders: ordine resting %s fallito: %s",
                       pick.get("match_id"), e)
        return out

    # FAILED o assenza di orderId: NON e' un ordine (stessa regola del taker).
    status = str(getattr(order, "status", "") or "").upper()
    order_id = getattr(order, "bet_id", None)
    if status == "FAILED" or not order_id:
        out["reason"] = "failed" if status == "FAILED" else "no_order_id"
        out["error"] = str(getattr(order, "error", "") or "")
        return out

    matched = float(getattr(order, "size_matched", 0.0) or 0.0)
    if matched > 0:
        # Riempito all'istante (l'ordine ha attraversato lo spread): e' una
        # puntata normale, la registra il percorso esistente. NON si duplica
        # sul registro resting.
        out.update({"filled": True, "order_id": str(order_id),
                    "status": status or "FILLED",
                    "price": getattr(order, "price_matched", None),
                    "stake": matched, "reason": "filled_immediately"})
        return out

    entry = {
        "order_id": str(order_id),
        "status": ST_OPEN,
        "placed_at": now.isoformat(),
        "deadline": datetime.fromtimestamp(dl, tz=timezone.utc).isoformat(),
        "expires_at": datetime.fromtimestamp(
            now.timestamp() + expiry_s, tz=timezone.utc).isoformat(),
        "match_id": key[0],
        "esito": key[1],
        "mercato": pick.get("mercato"),
        "market_id": market_id,
        "selection_id": int(selection_id),
        "stake": round(float(stake), 4),
        "price": round(float(price), 4),
        "home": pick.get("home"),
        "away": pick.get("away"),
        "kickoff": pick.get("commence"),
        "sx_status": status,
        "cancel_attempted": False,
        "cancel_ok": False,
    }
    rows.append(entry)
    state["orders"] = rows
    if not save(state):
        # Non si e' potuti registrare: l'ordine E' sul book senza tracciabilita'
        # → si tenta subito la cancellazione (fail-closed sul capitale).
        logger.error("resting_orders: registro non scrivibile per %s (%s): "
                     "tento la cancellazione immediata", key[0], key[1])
        try:
            if prov.cancel_order(market_id, str(order_id)):
                out["reason"] = "state_unwritable_cancelled"
                return out
        except Exception:
            pass
        out["reason"] = "state_unwritable"
        out["order_id"] = str(order_id)
        return out

    logger.info("resting_orders: ORDINE RESTING %s (%s vs %s, %s) @ %.4f per "
                "%.2f USDC [%s] — deadline %s",
                market_id, pick.get("home"), pick.get("away"), key[1],
                float(price), float(stake), str(order_id)[:12],
                entry["deadline"])
    out.update({"placed": True, "order_id": str(order_id),
                "entry": entry, "reason": "resting"})
    return out


# ---------------------------------------------------------------------------
# Riconciliazione (riempimenti) + scadenza (cancellazione)
# ---------------------------------------------------------------------------

def _open_ids(prov) -> Optional[set]:
    """Id degli ordini aperti sull'exchange, o None se NON leggibile."""
    fn = getattr(prov, "list_open_orders", None)
    if fn is None:
        return None
    try:
        live = fn()
    except Exception as e:
        logger.warning("resting_orders: lettura ordini aperti fallita: %s", e)
        return None
    if live is None:
        return None
    ids = set()
    for o in live:
        oid = o.get("orderId") or o.get("order_id")
        if oid:
            ids.add(str(oid))
    return ids


def _bet_row_exists(match_id: str, esito: str) -> bool:
    """True se esiste GIA' una riga `bets` per (match_id, esito).

    Fail-CLOSED sul dubbio: se non si riesce a leggere, si considera esistente
    (meglio perdere la telemetria di un riempimento che sovrascrivere una
    puntata reale). Stessa guardia di `smart_hedging`.
    """
    try:
        from tracker import _get_conn
        conn = _get_conn()
        row = conn.execute(
            "SELECT 1 FROM bets WHERE match_id = ? AND esito = ? LIMIT 1",
            (match_id, esito)).fetchone()
        conn.close()
        return row is not None
    except Exception as e:
        logger.warning("resting_orders: guardia ledger illeggibile (%s)", e)
        return True


def reconcile(prov=None, *, now: Optional[datetime] = None,
              save_bet_fn=None) -> Dict[str, Any]:
    """Chiude gli ordini che NON sono piu' sul book e non li abbiamo tolti noi.

    Regola (in un verso solo): se l'ordine non e' piu' fra gli aperti, NON
    l'abbiamo cancellato noi e NON e' ancora scaduta la sua finestra on-chain,
    allora e' stato RIEMPITO. Ogni altro caso e' dichiarato, mai indovinato.
    """
    now = now or _now()
    res: Dict[str, Any] = {"checked": 0, "filled": 0, "still_open": 0,
                           "cancelled": 0, "unconfirmed": 0, "unavailable": False,
                           "errors": [], "fills": []}
    state = load()
    rows = [r for r in (state.get("orders") or []) if isinstance(r, dict)]
    pending = [r for r in rows if r.get("status") == ST_OPEN]
    if not pending:
        return res
    res["checked"] = len(pending)

    if prov is None:
        try:
            import execution_engine as ee
            prov = ee.SxBetProvider()
        except Exception as e:
            res["unavailable"] = True
            res["errors"].append(f"provider: {e}")
            return res
    live_ids = _open_ids(prov)
    if live_ids is None:
        # Fail-closed: nessuna inferenza su un ordine di denaro reale.
        res["unavailable"] = True
        return res

    if save_bet_fn is None:
        def save_bet_fn(**kw):
            from tracker import save_bet as _sb
            return _sb(**kw)

    changed = False
    for r in pending:
        oid = str(r.get("order_id") or "")
        if oid in live_ids:
            res["still_open"] += 1
            continue
        # Fuori dal book: l'abbiamo tolto NOI?
        if r.get("cancel_attempted") and r.get("cancel_ok"):
            r["status"] = ST_CANCELLED
            r["closed_at"] = now.isoformat()
            res["cancelled"] += 1
            changed = True
            continue
        exp = _ts(r.get("expires_at"))
        if exp is not None and now >= exp:
            # Finestra on-chain chiusa: assenza AMBIGUA (potrebbe essere una
            # scadenza naturale). Non si inventa un riempimento.
            r["status"] = ST_UNCONFIRMED
            r["closed_at"] = now.isoformat()
            r["note"] = "assente dal book dopo la scadenza on-chain: verificare"
            res["unconfirmed"] += 1
            logger.error("resting_orders: ordine %s (%s, %s) assente dal book "
                         "dopo la scadenza on-chain: NON registrato come bet, "
                         "da verificare a mano", oid[:12], r.get("match_id"),
                         r.get("esito"))
            changed = True
            continue
        # RIEMPITO.
        match_id, esito = str(r.get("match_id") or ""), str(r.get("esito") or "")
        if not match_id or not esito:
            r["status"] = ST_UNCONFIRMED
            r["note"] = "riga senza match_id/esito: non registrabile"
            res["unconfirmed"] += 1
            changed = True
            continue
        if _bet_row_exists(match_id, esito):
            r["status"] = ST_DUPLICATE
            r["closed_at"] = now.isoformat()
            r["note"] = "esiste gia' una riga bets per (match_id, esito)"
            res["errors"].append(f"duplicate:{match_id}:{esito}")
            changed = True
            continue
        try:
            save_bet_fn(
                match_id=match_id, mercato=r.get("mercato"), esito=esito,
                market_id=r.get("market_id"),
                selection_id=r.get("selection_id"),
                price=float(r.get("price") or 0.0),
                stake=float(r.get("stake") or 0.0),
                mode="live", status="RESTING_FILLED", bet_id=oid)
        except Exception as e:
            res["errors"].append(f"save:{match_id}:{type(e).__name__}")
            logger.warning("resting_orders: salvataggio bet %s fallito (%s)",
                           match_id, e)
            continue
        r["status"] = ST_FILLED
        r["filled_at"] = now.isoformat()
        res["filled"] += 1
        res["fills"].append(dict(r))
        logger.info("resting_orders: RIEMPITO %s (%s vs %s, %s) @ %.4f per "
                    "%.2f USDC → riga bets mode='live'",
                    str(r.get("market_id"))[:14], r.get("home"), r.get("away"),
                    esito, float(r.get("price") or 0.0),
                    float(r.get("stake") or 0.0))
        changed = True

    if changed:
        state["orders"] = rows
        save(state)
    return res


def expire(prov=None, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Oltre la deadline: cancella (l'esito lo decide `reconcile`).

    Non marca nulla come cancellato qui: se l'ordine era GIA' riempito, la
    cancellazione fallisce e `reconcile` lo chiude come riempimento. Cosi' un
    ordine riempito all'ultimo istante non viene mai perso.
    """
    now = now or _now()
    res: Dict[str, Any] = {"checked": 0, "cancel_requested": 0, "cancel_ok": 0,
                           "errors": []}
    state = load()
    rows = [r for r in (state.get("orders") or []) if isinstance(r, dict)]
    pending = [r for r in rows if r.get("status") == ST_OPEN]
    if not pending:
        return res
    res["checked"] = len(pending)
    if prov is None:
        try:
            import execution_engine as ee
            prov = ee.SxBetProvider()
        except Exception as e:
            res["errors"].append(f"provider: {e}")
            return res

    changed = False
    for r in pending:
        dl = _ts(r.get("deadline"))
        if dl is None or now < dl:
            continue
        r["cancel_attempted"] = True
        res["cancel_requested"] += 1
        try:
            ok = bool(prov.cancel_order(str(r.get("market_id") or ""),
                                        str(r.get("order_id") or "")))
        except Exception as e:
            ok = False
            res["errors"].append(f"cancel:{type(e).__name__}")
            logger.warning("resting_orders: cancellazione %s fallita: %s",
                           str(r.get("order_id"))[:12], e)
        r["cancel_ok"] = ok
        r["cancel_at"] = now.isoformat()
        if ok:
            res["cancel_ok"] += 1
            logger.info("resting_orders: ordine %s ritirato (deadline %s)",
                        str(r.get("order_id"))[:12], r.get("deadline"))
        changed = True
    if changed:
        state["orders"] = rows
        save(state)
    return res


def run_cycle(prov=None, *, now: Optional[datetime] = None,
              save_bet_fn=None) -> Dict[str, Any]:
    """Un giro completo: prima si ritira cio' che ha scaduto, poi si rilegge.

    L'ordine dei due passi NON e' arbitrario: `expire` marca l'intenzione di
    cancellare, `reconcile` decide l'esito leggendo il book (dove un ordine
    riempito all'ultimo istante risulta comunque fuori dagli aperti).
    Fail-safe: qualunque errore diventa un campo, mai un'eccezione.
    """
    out: Dict[str, Any] = {"expired": {}, "reconciled": {}, "error": ""}
    try:
        out["expired"] = expire(prov, now=now)
    except Exception as e:                                        # pragma: no cover
        out["error"] = f"expire:{type(e).__name__}"
        logger.warning("resting_orders: expire fallito: %s", e)
    try:
        out["reconciled"] = reconcile(prov, now=now, save_bet_fn=save_bet_fn)
    except Exception as e:                                        # pragma: no cover
        out["error"] = (out["error"] + " " if out["error"] else "") + \
            f"reconcile:{type(e).__name__}"
        logger.warning("resting_orders: reconcile fallito: %s", e)
    return out


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def summary() -> Dict[str, Any]:
    """Istantanea per i log, il report e gli agenti (zero costi, sola lettura)."""
    rows = orders()
    cfg = config()
    by_status: Dict[str, int] = {}
    for r in rows:
        s = str(r.get("status") or "?")
        by_status[s] = by_status.get(s, 0) + 1
    open_rows = [r for r in rows if r.get("status") == ST_OPEN]
    return {
        "enabled": cfg["enabled"],
        "max_open": cfg["max_open"],
        "ttl_min": cfg["ttl_min"],
        "cancel_before_min": cfg["cancel_before_min"],
        "total": len(rows),
        "by_status": by_status,
        "open": len(open_rows),
        "open_stake": open_stake(),
        "filled": by_status.get(ST_FILLED, 0),
        "cancelled": by_status.get(ST_CANCELLED, 0),
        "unconfirmed": by_status.get(ST_UNCONFIRMED, 0),
        "state": cfg["state"],
    }


def format_report(s: Optional[Dict[str, Any]] = None) -> str:
    """Riga Telegram-friendly dello stato degli ordini resting."""
    s = s or summary()
    lines = [
        "🅾️ ORDINI RESTING (GTC) su SX Bet",
        f"  stato: {'ATTIVO' if s.get('enabled') else 'SPENTO'} | "
        f"max aperti {s.get('max_open')} | TTL {s.get('ttl_min'):.0f}' | "
        f"ritiro a T-{s.get('cancel_before_min'):.0f}'",
        f"  aperti: {s.get('open')} ({s.get('open_stake')} USDC "
        f"immobilizzati) | riempiti: {s.get('filled')} | "
        f"ritirati: {s.get('cancelled')}",
    ]
    if s.get("unconfirmed"):
        lines.append(f"  ⚠️ da verificare a mano: {s['unconfirmed']} "
                     "(assenti dal book dopo la scadenza on-chain)")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Ordini RESTING (GTC) su SX Bet: stato e riconciliazione")
    ap.add_argument("--status", action="store_true",
                    help="solo lettura del registro (default)")
    ap.add_argument("--cycle", action="store_true",
                    help="esegue un giro reale (ritiro + riconciliazione)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    if args.cycle:
        res = run_cycle()
        if args.json:
            print(json.dumps(res, indent=2, ensure_ascii=False, default=str))
        else:
            exp, rec = res.get("expired") or {}, res.get("reconciled") or {}
            print(f"ritirati: {exp.get('cancel_ok', 0)}/{exp.get('checked', 0)} | "
                  f"riempiti: {rec.get('filled', 0)} | "
                  f"ancora aperti: {rec.get('still_open', 0)}")
    else:
        s = summary()
        print(json.dumps(s, indent=2, ensure_ascii=False)
              if args.json else format_report(s))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
