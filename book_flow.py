"""book_flow.py — Flusso dell'order book SX Bet: la liquidita' che ENTRA.

Perche' (26/09/2026, direttiva del proprietario): seguire i capitali che
entrano nel book. Un limit order massiccio su un esito e' informazione
("smart money") e va riconosciuto prima che la quota venga assorbita — il
punto di questo modulo e' rendere quell'ingresso MISURABILE.

Come, senza nuove dipendenze e senza letture extra: il rilevatore **non fa
chiamate proprie**. Consuma i book che `sx_signals.scan` (1X2) e
`multi_market.ingest` (OU/AH) scaricano GIA' per la scansione — stessa pagina
`/orderbook-v3/snapshot`, letture pubbliche: **zero chiavi, zero crediti
the-odds-api, zero ordini**. Non esiste una libreria WebSocket in questo
progetto (`requests` e' l'unico client HTTP) e Pinnacle non offre uno stream:
la latenza si guadagna sull'ORDINE, non sulla lettura, quindi il rilevatore
sta dove i book sono gia' in mano, senza pagare una seconda volta.

Che cosa e' un INGRESSO (e perche' non lo stato stazionario): ogni
osservazione viene confrontata con la PRECEDENTE per (market_id, esito),
tenuta in uno stato compatto sul volume. Un book gia' profondo non e' una
notizia; un book che si RIEMPIE in un giro si'.

    evento se  delta_profondita' >= BOOK_FLOW_MIN_SIZE_USDC
           e   delta_profondita' / profondita'_precedente >= BOOK_FLOW_MIN_JUMP_PCT
    oppure     un livello NUOVO (prezzo non presente prima) con
               size >= BOOK_FLOW_MIN_SIZE_USDC

Il primo e' il riempimento del book, il secondo e' l'arrivo di un ordine
fuori scala: sono due firme diverse e il registro le distingue (`reason`).

⚠️ **TELEMETRIA, NON ORDINI** — come `liquidity_monitor` e la sentinella BTTS.
Questo modulo misura, registra e riporta: **non piazza nulla, non tocca i gate
di strategia e non alimenta `auto_bet`**. Un segnale nuovo senza campione non
diventa un cambio di strategia: il congelamento del 22/09/2026 resta, e
l'eventuale collegamento all'esecuzione e' una decisione del proprietario da
prendere sui NUMERI di questo registro (stesso percorso del bypass top-down,
`TOP_DOWN_BYPASS`, nato spento). Un tripwire nei test blinda il perimetro.

Regole del modulo:
1. **Fail-safe totale**: nessuna funzione solleva verso il chiamante. Un file
   di stato corrotto non viene sovrascritto e non ferma il giro.
2. **Scrittura atomica** dello stato (tmp + `os.replace`) e append di una riga
   per evento sul log JSONL.
3. **Dedup** per (market_id, esito) entro `BOOK_FLOW_DEDUP_MIN` minuti: il
   giro gira spesso e lo stesso ingresso non deve riempire il registro.
4. Lo stato e' **limitato** (`BOOK_FLOW_MAX_KEYS`): i mercati piu' vecchi
   escono, cosi' il file non cresce senza fine sul volume.

Il modulo NON importa `bot`/`tracker`/`auto_bet` (autonomo e testabile):
l'aggregazione e l'invio Telegram vivono nel chiamante (`bot.py`).
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from config import DATA_DIR

logger = logging.getLogger("book_flow")

_EXEC_DIR = Path(DATA_DIR) / "execution"

#: Stato compatto dell'ultima osservazione per (market_id, esito) e registro
#: degli ingressi. Entrambi sul volume (sopravvivono ai redeploy).
STATE_PATH = Path(os.getenv(
    "BOOK_FLOW_STATE", str(_EXEC_DIR / "book_flow_state.json")))
LOG_PATH = Path(os.getenv(
    "BOOK_FLOW_LOG", str(_EXEC_DIR / "book_flow_events.jsonl")))


def _env_float(name: str, default: float, *, minimum: Optional[float] = None
               ) -> float:
    """Env numerica con fallback DICHIARATO su valore impossibile.

    Un env sbagliato non deve cambiare in silenzio la semantica del
    rilevatore: fuori range si torna al default e si logga.
    """
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        val = float(raw)
    except (TypeError, ValueError):
        logger.warning("book_flow: %s=%r non numerico, uso %s", name, raw,
                       default)
        return default
    if minimum is not None and val < minimum:
        logger.warning("book_flow: %s=%r sotto il minimo %s, uso %s", name,
                       val, minimum, default)
        return default
    return val


def _env_int(name: str, default: int, *, minimum: Optional[int] = None) -> int:
    val = _env_float(name, float(default), minimum=minimum)
    return int(val)


#: Ingresso minimo assoluto in USDC: sotto questa cifra il book si muove
#: normalmente (rimbalzi di market making) e non c'e' niente da seguire.
MIN_INGRESS_USDC = _env_float("BOOK_FLOW_MIN_SIZE_USDC", 50.0, minimum=0.0)
#: ...e in proporzione: su un book da 1000 USDC servono >= 350 USDC (+35%)
#: perche' l'ingresso sia anomalo. La soglia RELATIVA evita che i mercati
#: profondi (dove 50 USDC sono rumore) riempiano il registro.
MIN_JUMP_PCT = _env_float("BOOK_FLOW_MIN_JUMP_PCT", 0.35, minimum=0.0)
#: Un mercato che continua a riempirsi e' lo STESSO ingresso: non si registra
#: di nuovo prima di questo intervallo.
DEDUP_MIN = _env_float("BOOK_FLOW_DEDUP_MIN", 30.0, minimum=0.0)
#: Tetto delle chiavi nello stato (i piu' vecchi escono): il file non cresce
#: senza fine sul volume.
MAX_KEYS = _env_int("BOOK_FLOW_MAX_KEYS", 2000, minimum=10)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _key(market_id: str, selection: Any) -> str:
    return f"{market_id}|{selection}"


def _levels_shape(entry: Optional[Dict[str, Any]]) -> List[Tuple[float, float]]:
    """Livelli `(prezzo, size)` di un esito, ordinati per prezzo decrescente.

    Accetta DUE forme dello stesso dato, perche' il rilevatore le incontra
    entrambe: i dict `{price, size}` di `sx_signals._book` (osservazione
    appena letta) e le coppie `[price, size]` che lo stesso dato assume dopo
    il giro nello stato JSON (un tuple serializzato e' una lista). Senza
    questa doppia lettura la firma "livello nuovo" non scatterebbe MAI su un
    giro successivo al primo.

    Le voci malformate vengono scartate in silenzio: un book sporco non e' un
    errore del rilevatore.
    """
    out: List[Tuple[float, float]] = []
    for lv in (entry or {}).get("levels") or []:
        if isinstance(lv, dict):
            price, size = lv.get("price"), lv.get("size")
        elif isinstance(lv, (list, tuple)) and len(lv) >= 2:
            price, size = lv[0], lv[1]
        else:
            continue
        try:
            price = float(price)
            size = float(size)
        except (TypeError, ValueError):
            continue
        if price > 1.0 and size > 0:
            out.append((round(price, 6), round(size, 4)))
    out.sort(key=lambda item: -item[0])
    return out


def detect_ingress(prev: Optional[Dict[str, Any]],
                   cur: Dict[str, Any], *,
                   min_ingress_usdc: Optional[float] = None,
                   min_jump_pct: Optional[float] = None
                   ) -> Optional[Dict[str, Any]]:
    """Ingresso di liquidita' fra due osservazioni. PURA: nessun I/O.

    `prev`/`cur` sono dello stesso formato dello stato (`depth`, `levels`).
    `prev=None` (prima osservazione) NON e' un ingresso: senza un termine di
    paragone non si puo' dire che il book si stia riempiendo — e dichiarare
    "ingresso" al primo giro riempirebbe il registro di tutto il palinsesto.

    Ritorna None se non c'e' ingresso, altrimenti un dict con `reason`,
    `delta`, `jump_pct`, `prev_depth`, `cur_depth` e (per i livelli nuovi) il
    prezzo/size del livello piu' grande arrivato.
    """
    if not prev:
        return None
    floor = MIN_INGRESS_USDC if min_ingress_usdc is None else float(min_ingress_usdc)
    pct = MIN_JUMP_PCT if min_jump_pct is None else float(min_jump_pct)
    try:
        prev_depth = float(prev.get("depth") or 0.0)
        cur_depth = float(cur.get("depth") or 0.0)
    except (TypeError, ValueError):
        return None
    delta = cur_depth - prev_depth
    if delta <= 0:
        return None
    jump = delta / prev_depth if prev_depth > 0 else float("inf")
    # 1) RIEMPIMENTO: il book cresce di molto in un giro.
    if delta >= floor and jump >= pct:
        return {"reason": "depth_ingress", "delta": round(delta, 4),
                "jump_pct": None if jump == float("inf") else round(jump, 4),
                "prev_depth": round(prev_depth, 4),
                "cur_depth": round(cur_depth, 4)}
    # 2) ORDINE FUORI SCALA: un prezzo che PRIMA non c'era. Un limit order
    #    massiccio su un prezzo nuovo e' informazione anche su un book
    #    profondo, dove il punto 1 non scatterebbe mai.
    prev_prices = {p for p, _ in _levels_shape(prev)}
    fresh = [(p, s) for p, s in _levels_shape(cur)
             if p not in prev_prices and s >= floor]
    if fresh:
        price, size = max(fresh, key=lambda item: item[1])
        return {"reason": "new_level", "delta": round(delta, 4),
                "jump_pct": None if jump == float("inf") else round(jump, 4),
                "prev_depth": round(prev_depth, 4),
                "cur_depth": round(cur_depth, 4),
                "level_price": price, "level_size": round(size, 4)}
    return None


# ---------------------------------------------------------------------------
# 2. STATO (ultima osservazione per mercato/esito) — scrittura atomica
# ---------------------------------------------------------------------------

def load_state() -> Dict[str, Any]:
    """Stato su disco. File corrotto/assente -> stato VUOTO (mai eccezioni).

    Non sovrascrive nulla: se il file e' illeggibile si riparte da zero in
    memoria e la prossima scrittura atomica lo rimpiazza con dati validi.
    """
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            obs = data.get("observations")
            if isinstance(obs, dict):
                return obs
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning("book_flow: stato illeggibile (%s), riparto da zero", e)
    return {}


def save_state(observations: Dict[str, Any]) -> bool:
    """Scrittura ATOMICA dello stato; `False` su errore, mai un'eccezione.

    Il tmp e' nella STESSA cartella (rename atomico sullo stesso filesystem).
    Lo stato viene potato a `MAX_KEYS` chiavi piu' recenti: un file di stato
    che cresce senza limite sul volume e' un problema che si scopre tardi.
    """
    try:
        items = list((observations or {}).items())
        if len(items) > MAX_KEYS:
            items.sort(key=lambda kv: float((kv[1] or {}).get("ts_epoch") or 0),
                       reverse=True)
            items = items[:MAX_KEYS]
        payload = {"updated_at": _now().isoformat(),
                   "observations": dict(items)}
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_suffix(STATE_PATH.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False),
                       encoding="utf-8")
        os.replace(tmp, STATE_PATH)
        return True
    except Exception as e:                                          # pragma: no cover
        logger.warning("book_flow: scrittura stato fallita (%s)", e)
        return False


def record_event(event: Dict[str, Any]) -> Dict[str, Any]:
    """Appende un ingresso al registro JSONL (fail-safe: mai eccezioni)."""
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception as e:                                          # pragma: no cover
        logger.warning("book_flow: scrittura registro fallita (%s)", e)
        event = dict(event)
        event["error"] = str(e)
    return event


# ---------------------------------------------------------------------------
# 3. INGRESSO PUBBLICO: osservazione a LOTTI (una sola lettura/scrittura stato)
# ---------------------------------------------------------------------------

def observe_books(items: Sequence[Tuple[str, Any, Dict[str, Any], Optional[dict]]],
                  *, ts: Optional[datetime] = None,
                  state: Optional[Dict[str, Any]] = None,
                  persist: bool = True) -> List[Dict[str, Any]]:
    """Aggiorna lo stato e registra gli ingressi per un LOTTO di book.

    `items` = [(market_id, selection, entry, context)] dove `entry` e' il
    formato di `sx_signals._book` (`best`/`depth`/`levels`) e `context` e'
    un dict libero (squadre, lega, mercato, linea) copiato nell'evento per
    rendere il registro leggibile senza risalire al ledger.

    **Una sola** lettura e **una sola** scrittura dello stato per l'intero
    lotto: e' il motivo per cui il rilevatore non appesantisce il giro (un
    ingest tocca centinaia di book). Fail-safe: qualunque errore lascia il
    giro intatto e ritorna solo gli eventi gia' prodotti.
    """
    events: List[Dict[str, Any]] = []
    try:
        now = ts or _now()
        epoch = now.timestamp()
        iso = now.isoformat()
        obs = load_state() if state is None else state
        events = []
        for market_id, selection, entry, context in items:
            if not market_id or not isinstance(entry, dict) or "error" in entry:
                continue                      # book assente/rotto: si salta
            key = _key(str(market_id), selection)
            cur = {"ts": iso, "ts_epoch": epoch,
                   "depth": float(entry.get("depth") or 0.0),
                   "levels": _levels_shape(entry)}
            prev = obs.get(key)
            found = detect_ingress(prev, cur)
            # Dedup: lo stesso mercato che si riempie di nuovo non e' un
            # secondo ingresso.
            if found and DEDUP_MIN > 0 and prev:
                last = prev.get("last_event_epoch")
                if last is not None:
                    try:
                        if epoch - float(last) < DEDUP_MIN * 60.0:
                            found = None
                    except (TypeError, ValueError):
                        pass
            if found:
                evt = {"ts": iso, "ts_epoch": epoch,
                       "market_id": str(market_id), "selection": selection,
                       **found}
                for k, v in (context or {}).items():
                    if v is not None and k not in evt:
                        evt[k] = v
                events.append(record_event(evt))
                cur["last_event_epoch"] = epoch
                cur["last_event_reason"] = found.get("reason")
            elif prev and prev.get("last_event_epoch") is not None:
                # Il dedup non deve "dimenticare" l'ultimo evento: si porta
                # avanti, altrimenti il giro successivo lo registrerebbe.
                cur["last_event_epoch"] = prev.get("last_event_epoch")
                cur["last_event_reason"] = prev.get("last_event_reason")
            obs[key] = cur
        if persist:
            save_state(obs)
    except Exception as e:                                          # pragma: no cover
        logger.warning("book_flow: osservazione fallita (%s)", e)
    return events


def observe_books_from_scan(books: Dict[str, Any],
                            contexts: Optional[Dict[str, dict]] = None,
                            **kw: Any) -> List[Dict[str, Any]]:
    """Scorciatoia per i chiamanti: book di `_books_parallel` gia' in mano.

    `books` = {market_hash: {1: {...}, 2: {...}}}, `contexts` opzionale =
    {market_hash: {...}}. Scansiona i soli esiti 1/2 (il formato taker di SX).
    """
    items: List[Tuple[str, Any, Dict[str, Any], Optional[dict]]] = []
    for mid, book in (books or {}).items():
        if not isinstance(book, dict) or "error" in book:
            continue
        ctx = (contexts or {}).get(mid)
        for sel in (1, 2):
            entry = book.get(sel) or book.get(str(sel))
            if isinstance(entry, dict):
                items.append((str(mid), sel, entry, ctx))
    return observe_books(items, **kw)


# ---------------------------------------------------------------------------
# 4. LETTURA: registro, riepilogo, report
# ---------------------------------------------------------------------------

def iter_events(days: Optional[float] = None) -> List[dict]:
    """Eventi dal registro (dal piu' recente al piu' vecchio).

    Righe corrotte ignorate; file assente -> lista vuota. `days=None` = tutto.
    Legge il file vivo **e** le generazioni `.gz` (rotazione automatica dal
    03/10/2026): il registro da 22 MB era il principale indiziato del volume
    all'81%, e comprimerlo non deve togliere storia al riepilogo.
    """
    from telemetry_logs import iter_events as _iter_jsonl
    try:
        return list(_iter_jsonl(LOG_PATH, days=days))
    except Exception as e:                                          # pragma: no cover
        logger.warning("book_flow: lettura registro fallita (%s)", e)
        return []


def summary(days: float = 7.0) -> Dict[str, Any]:
    """Riepilogo del registro: quanti ingressi, dove, di che tipo/firma."""
    evts = iter_events(days=days)
    by_reason: Dict[str, int] = {}
    by_league: Dict[str, int] = {}
    by_market: Dict[str, int] = {}
    total_delta = 0.0
    for e in evts:
        by_reason[str(e.get("reason") or "?")] = \
            by_reason.get(str(e.get("reason") or "?"), 0) + 1
        lg = str(e.get("league") or "n/d")
        by_league[lg] = by_league.get(lg, 0) + 1
        mk = str(e.get("market") or "1X2")
        by_market[mk] = by_market.get(mk, 0) + 1
        try:
            total_delta += float(e.get("delta") or 0.0)
        except (TypeError, ValueError):
            pass
    return {
        "days": days,
        "events": len(evts),
        "total_delta_usdc": round(total_delta, 2),
        "avg_delta_usdc": round(total_delta / len(evts), 2) if evts else 0.0,
        "by_reason": by_reason,
        "by_league": dict(sorted(by_league.items(),
                                 key=lambda kv: -kv[1])[:10]),
        "by_market": by_market,
        "thresholds": {"min_ingress_usdc": MIN_INGRESS_USDC,
                       "min_jump_pct": MIN_JUMP_PCT,
                       "dedup_min": DEDUP_MIN},
        "log_path": str(LOG_PATH),
    }


def format_report(days: float = 7.0) -> Optional[str]:
    """Sezione Telegram-friendly. None se non c'e' niente da dire."""
    s = summary(days)
    if not s["events"]:
        return None
    lines = [f"📈 Flusso book SX — ingressi di liquidita' (ultimi {days:g}gg)",
             f"{s['events']} ingressi · +{s['total_delta_usdc']:.0f} USDC "
             f"totali (media {s['avg_delta_usdc']:.0f})"]
    if s["by_reason"]:
        lines.append("Firme: " + ", ".join(
            f"{k} {v}" for k, v in sorted(s["by_reason"].items(),
                                          key=lambda kv: -kv[1])))
    if s["by_market"]:
        lines.append("Mercati: " + ", ".join(
            f"{k} {v}" for k, v in sorted(s["by_market"].items(),
                                          key=lambda kv: -kv[1])))
    if s["by_league"]:
        lines.append("Leghe: " + ", ".join(
            f"{k} {v}" for k, v in s["by_league"].items()))
    lines.append(f"⬛ TELEMETRIA (nessun ordine): soglie "
                 f"{MIN_INGRESS_USDC:.0f} USDC / +{MIN_JUMP_PCT * 100:.0f}%")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 5. CLI (diagnostica di sola lettura)
# ---------------------------------------------------------------------------

def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse
    p = argparse.ArgumentParser(
        description="Flusso dell'order book SX (telemetria, nessun ordine)")
    p.add_argument("--days", type=float, default=7.0,
                   help="finestra del registro (default 7)")
    p.add_argument("--json", action="store_true", help="output JSON")
    args = p.parse_args(list(argv) if argv is not None else None)
    if args.json:
        print(json.dumps(summary(days=args.days), ensure_ascii=False, indent=2))
        return 0
    report = format_report(days=args.days)
    print(report or f"book_flow: nessun ingresso negli ultimi {args.days:g}gg "
                    f"({LOG_PATH})")
    return 0


if __name__ == "__main__":                                          # pragma: no cover
    raise SystemExit(main())
