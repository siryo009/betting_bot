"""steam_move.py — Steam Move sullo sharp (Pinnacle) come priorita' d'esecuzione.

Direttiva del proprietario (02/10/2026): misurare il **ΔQ/Δt** della quota
Pinnacle negli ultimi 15-30 minuti. Un **crollo rapido (> 4%)** della quota
sharp significa che il denaro informato sta entrando su quell'esito: la
probabilita' "vera" sta salendo, quindi il prezzo su SX Bet (che non si e'
ancora riallineato) e' **temporaneamente** un valore ancora piu' grande. Il
segnale va allora eseguito **per primo**, prima che il market maker su SX
riallinei la quota e il ritardo scompaia.

Come si misura (una sola definizione, nessuna formula copiata):
- lo storico dei prezzi sharp vive in `price_snapshots` (la tabella dei
  movimenti di linea di `line_movement`), con `bookmaker='pinnacle'`: lo
  storico di una fonte NON si mescola con quello di un'altra;
- `record_sharp_snapshot` DELEGA a `line_movement.record_snapshot` (stessa
  scrittura, stesso indice) e deduplica i prezzi identici ravvicinati: il giro
  ordini gira ogni 60s e non deve gonfiare la tabella con lo stesso prezzo;
- `delta_q_dt` legge la finestra con `line_movement.get_snapshots` (filtro per
  book) e calcola `(ultimo / primo - 1)` con l'intervallo temporale reale;
- `detect` traduce il movimento in verdetto: crollo >= soglia dentro la
  finestra 15-30' -> **steam move**.

Il modulo NON decide l'ordine e NON parla con l'exchange: espone un MARCATORE
(`steam_move: True` sul candidato) che il giro ordini usa per ORDINARE la coda.
Fail-safe totale: qualunque errore di lettura/scrittura non propaga mai
un'eccezione al chiamante (un problema di telemetria non deve fermare un giro).

Env (default di codice, tutte sovrascrivibili):
  STEAM_MOVE_ENABLED        "1"    interruttore generale
  STEAM_MOVE_PCT            "0.04" crollo (frazione) che accende lo steam
  STEAM_MOVE_WINDOW_MIN     "30"   ampiezza della finestra di osservazione
  STEAM_MOVE_MIN_WINDOW_MIN "15"   span minimo fra i due estremi osservati
                                   (0 = nessun pavimento)
  STEAM_MOVE_BOOK           "pinnacle"  fonte sharp da tracciare
  STEAM_MOVE_DEDUP_MIN      "5"    finestra di dedup degli snapshot identici

CLI:
  venv/bin/python steam_move.py --match ID --esito 1
  venv/bin/python steam_move.py --match ID --esito 1 --json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("steam_move")

BOOK_DEFAULT = "pinnacle"

#: Forma del mercato per la FONTE sharp: 1X2 (calcio) o 2 vie (tennis/eSports).
OUTCOMES_1X2: Tuple[str, ...] = ("1", "X", "2")
OUTCOMES_2WAY: Tuple[str, ...] = ("1", "2")

#: Mercati i cui esiti sono direttamente quelli del mercato sharp.
ONE_X_TWO_MARKETS = frozenset({"1X2"})
TWO_WAY_MARKETS = frozenset({"TENNIS", "ML"})


# ---------------------------------------------------------------------------
# Configurazione (letta a RUNTIME: tarabile e testabile senza redeploy)
# ---------------------------------------------------------------------------

def _num_env(name: str, default: float, *, minimum: Optional[float] = None) -> float:
    """Float da env con fallback DICHIARATO (mai un default silenzioso)."""
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return float(default)
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning("steam_move: %s='%s' non numerico — uso %.4g", name, raw, default)
        return float(default)
    if minimum is not None and value < minimum:
        logger.warning("steam_move: %s=%.4g sotto il minimo %.4g — uso %.4g",
                       name, value, minimum, minimum)
        return float(minimum)
    return value


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return bool(default)
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def enabled() -> bool:
    return _bool_env("STEAM_MOVE_ENABLED", True)


def move_pct() -> float:
    """Soglia di crollo (frazione del prezzo). Direttiva: 0.04 = 4%."""
    return _num_env("STEAM_MOVE_PCT", 0.04, minimum=0.0)


def window_min() -> float:
    return _num_env("STEAM_MOVE_WINDOW_MIN", 30.0, minimum=1.0)


def min_window_min() -> float:
    return _num_env("STEAM_MOVE_MIN_WINDOW_MIN", 15.0, minimum=0.0)


def book_name() -> str:
    return (os.getenv("STEAM_MOVE_BOOK") or BOOK_DEFAULT).strip().lower() or BOOK_DEFAULT


def dedup_min() -> float:
    return _num_env("STEAM_MOVE_DEDUP_MIN", 5.0, minimum=0.0)


def config() -> Dict[str, Any]:
    """Configurazione effettiva (per log e diagnosi)."""
    return {"enabled": enabled(), "move_pct": move_pct(),
            "window_min": window_min(), "min_window_min": min_window_min(),
            "book": book_name(), "dedup_min": dedup_min()}


# ---------------------------------------------------------------------------
# Storico dei prezzi sharp
# ---------------------------------------------------------------------------

def _last_snapshot(match_id: str, esito: str, book: str) -> Optional[Dict]:
    """Ultimo snapshot registrato per (match, esito, book). None se assente."""
    from line_movement import get_snapshots
    snaps = get_snapshots(match_id, esito, bookmaker=book)
    return snaps[-1] if snaps else None


def record_sharp_snapshot(match_id: str, esito: str, price: float, *,
                          book: Optional[str] = None,
                          recorded_at: Optional[str] = None) -> bool:
    """Registra un prezzo sharp. True se ha scritto, False se deduplicato/errore.

    Dedup: se l'ultimo snapshot della stessa fonte ha lo STESSO prezzo ed e'
    piu' recente di `dedup_min` minuti, non si riscrive (il giro gira ogni 60s).
    Un prezzo DIVERSO si registra SEMPRE: e' proprio il movimento che serve a
    misurare il ΔQ/Δt.
    """
    try:
        price = float(price)
    except (TypeError, ValueError):
        return False
    if price <= 1.0 or not match_id or not esito:
        return False
    bk = book or book_name()
    try:
        last = _last_snapshot(match_id, esito, bk)
        if last and abs(float(last["price"]) - price) < 1e-9:
            age = _minutes_since(last.get("recorded_at"), recorded_at)
            if age is not None and 0 <= age < dedup_min():
                return False
        from line_movement import record_snapshot
        record_snapshot(match_id, esito, price, bookmaker=bk,
                        recorded_at=recorded_at)
        return True
    except Exception as exc:
        logger.debug("steam_move: snapshot non registrato su %s/%s (%s)",
                     match_id, esito, exc)
        return False


def _minutes_since(recorded_at: Optional[str], now_iso: Optional[str] = None) -> Optional[float]:
    """Minuti fra `recorded_at` e `now_iso` (now se assente). None se illeggibile."""
    if not recorded_at:
        return None
    try:
        first = datetime.fromisoformat(str(recorded_at))
    except (TypeError, ValueError):
        return None
    try:
        now = datetime.fromisoformat(now_iso) if now_iso else datetime.now()
    except (TypeError, ValueError):
        now = datetime.now()
    return (now - first).total_seconds() / 60.0


def delta_q_dt(match_id: str, esito: str, *, book: Optional[str] = None,
               window_minutes: Optional[float] = None,
               min_window_minutes: Optional[float] = None,
               now: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Variazione % della quota sharp nella finestra: ΔQ/Δt. None se non misurabile.

    `move_pct` e' negativo quando la quota SCENDE (esito che il mercato
    considera piu' probabile). `span_minutes` e' l'intervallo REALE fra il
    primo e l'ultimo snapshot osservati: senza quello un movimento calcolato
    su due letture a distanza di un secondo sarebbe rumore.
    """
    bk = book or book_name()
    win = window_minutes if window_minutes is not None else window_min()
    minspan = min_window_minutes if min_window_minutes is not None else min_window_min()
    try:
        from line_movement import get_snapshots
        snaps = get_snapshots(match_id, esito, since_minutes=int(max(1, round(win))),
                              bookmaker=bk)
    except Exception as exc:
        logger.debug("steam_move: lettura snapshots fallita (%s)", exc)
        return None
    if len(snaps) < 2:
        return None
    first, last = snaps[0], snaps[-1]
    try:
        first_price = float(first["price"])
        last_price = float(last["price"])
    except (TypeError, ValueError, KeyError):
        return None
    if first_price <= 0:
        return None
    span = _minutes_since(first.get("recorded_at"), last.get("recorded_at"))
    if span is None:
        return None
    span = abs(span)
    if minspan > 0 and span < minspan:
        return None
    move = (last_price / first_price) - 1.0
    return {
        "match_id": match_id,
        "esito": esito,
        "book": bk,
        "first_price": round(first_price, 4),
        "last_price": round(last_price, 4),
        "move_pct": round(move * 100.0, 3),
        "span_minutes": round(span, 1),
        "direction": "down" if move < 0 else ("up" if move > 0 else "flat"),
    }


def detect(match_id: str, esito: str, *, book: Optional[str] = None,
           window_minutes: Optional[float] = None,
           min_window_minutes: Optional[float] = None,
           threshold: Optional[float] = None,
           now: Optional[str] = None) -> Dict[str, Any]:
    """Verdetto Steam Move. Ritorna SEMPRE un dict (mai un'eccezione).

    `steam_move=True` solo su un CROLLO (`move_pct <= -soglia`) misurato dentro
    la finestra: un crollo dello sharp = il denaro informato entra su questo
    esito, quindi il prezzo SX (non ancora riallineato) e' valore da eseguire
    SUBITO. Un rialzo NON accende il marcatore (direzione registrata a parte).
    """
    thr = move_pct() if threshold is None else float(threshold)
    out: Dict[str, Any] = {"match_id": match_id, "esito": esito,
                           "steam_move": False, "priority": False,
                           "reason": "no_data", "threshold_pct": round(thr * 100.0, 3)}
    try:
        d = delta_q_dt(match_id, esito, book=book, window_minutes=window_minutes,
                       min_window_minutes=min_window_minutes, now=now)
    except Exception as exc:
        logger.debug("steam_move: delta fallito su %s/%s (%s)", match_id, esito, exc)
        return out
    if not d:
        return out
    out.update(d)
    if d["move_pct"] <= -(thr * 100.0):
        out["steam_move"] = True
        out["priority"] = True
        out["reason"] = "steam_down"
    else:
        out["reason"] = "no_drop"
    return out


# ---------------------------------------------------------------------------
# Ponte con l'oracolo: le quote sharp arrivano dalle cache (0 crediti)
# ---------------------------------------------------------------------------

def outcomes_for_market(mercato: Optional[str]) -> Optional[Tuple[str, ...]]:
    """Forma del mercato sharp per un mercato del candidato (None se non coperto).

    Solo i mercati i cui esiti SONO quelli sharp: 1X2 (calcio) e testa-a-testa
    senza pareggio (tennis/eSports). Per OU/AH l'esito e' una LINEA e la
    corrispondenza non e' diretta: nessun marcatore invece di uno sbagliato.
    """
    m = str(mercato or "").strip().upper()
    if m in ONE_X_TWO_MARKETS:
        return OUTCOMES_1X2
    if m in TWO_WAY_MARKETS:
        return OUTCOMES_2WAY
    return None


def observe(home: str, away: str, match_id: str, esito: str,
            outcomes: Tuple[str, ...], *, cache_dir=None
            ) -> Dict[str, Any]:
    """Registra i prezzi sharp della partita e valuta lo steam sull'esito.

    Lettura a COSTO ZERO (cache della rotazione quote). Il verdetto e' SEMPRE
    calcolato sullo STORICO registrato, anche se la cache in questo momento non
    e' disponibile: un movimento misurato nei giri precedenti resta valido
    (e' proprio il senso dello storico). `sharp_cache` dichiara se in QUESTO
    giro lo sharp era leggibile: senza storia e senza cache il motivo e'
    `no_sharp_cache` (nessuna invenzione).
    """
    try:
        import pinnacle_oracle as po
    except Exception as exc:
        return {"steam_move": False, "reason": f"oracle_unavailable:{type(exc).__name__}"}
    book = book_name()
    read_ok = False
    try:
        got = po.pinnacle_odds_from_cache(home, away, cache_dir=cache_dir,
                                          outcomes=outcomes)
        if got and got.get("odds"):
            read_ok = True
            for key, price in got["odds"].items():
                record_sharp_snapshot(match_id, str(key), price, book=book)
    except Exception as exc:
        return {"steam_move": False, "reason": f"read_error:{type(exc).__name__}"}
    info = detect(match_id, esito, book=book)
    info["sharp_cache"] = read_ok
    if not read_ok and not info.get("steam_move") \
            and info.get("reason") == "no_data":
        info["reason"] = "no_sharp_cache"
    return info


def annotate(candidates: Sequence[Dict[str, Any]], *, cache_dir=None) -> int:
    """Marca i candidati (`steam_move`) e ritorna quanti steam move ci sono.

    Fail-safe: un errore su un candidato non ferma gli altri e non propaga.
    """
    if not enabled():
        return 0
    found = 0
    for cand in candidates or []:
        try:
            outcomes = outcomes_for_market(cand.get("mercato"))
            if outcomes is None:
                cand["steam_move"] = False
                cand["steam_move_info"] = {"steam_move": False,
                                           "reason": "unsupported_market"}
                continue
            info = observe(str(cand.get("home") or ""), str(cand.get("away") or ""),
                           str(cand.get("match_id") or ""),
                           str(cand.get("esito_key") or ""), outcomes,
                           cache_dir=cache_dir)
        except Exception as exc:
            info = {"steam_move": False, "reason": f"error:{type(exc).__name__}"}
        cand["steam_move"] = bool(info.get("steam_move"))
        cand["steam_move_info"] = info
        if cand["steam_move"]:
            found += 1
    return found


def sort_for_execution(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Ordina la coda con gli STEAM MOVE per primi (poi ordine invariato).

    Perche' conta l'ordine: la finestra di valore di uno steam move si chiude
    quando SX riallinea la quota. `sorted` e' STABILE, quindi dentro i due
    gruppi l'ordine EV esistente resta intatto.
    """
    if not candidates:
        return candidates or []
    return sorted(candidates, key=lambda c: 0 if c.get("steam_move") else 1)


# ---------------------------------------------------------------------------
# CLI (diagnostica, sola lettura)
# ---------------------------------------------------------------------------

def format_report(info: Dict[str, Any]) -> str:
    if not info:
        return "steam_move: nessun dato"
    if info.get("steam_move"):
        return (f"🔥 STEAM MOVE {info.get('match_id')} {info.get('esito')} "
                f"({info.get('book')}): {info.get('first_price')} -> "
                f"{info.get('last_price')} = {info.get('move_pct'):+.2f}% in "
                f"{info.get('span_minutes')} min")
    return (f"steam_move: nessuno (motivo {info.get('reason')}"
            + (f", {info.get('move_pct'):+.2f}% in {info.get('span_minutes')} min"
               if info.get("move_pct") is not None else "") + ")")


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Steam move sullo sharp (Pinnacle) — sola lettura")
    ap.add_argument("--match", required=True, help="match_id del candidato")
    ap.add_argument("--esito", required=True, help="esito (1/X/2)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    info = detect(args.match, args.esito)
    if args.json:
        print(json.dumps({"config": config(), "detection": info},
                         indent=2, ensure_ascii=False))
    else:
        print(format_report(info))
    return 0


if __name__ == "__main__":                                    # pragma: no cover
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    sys.exit(main())
