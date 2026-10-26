#!/usr/bin/env python3
"""flow_measure.py - misura il FLUSSO della corsia scommesse su una finestra.

Direttiva del proprietario (24/09/2026), dopo la diagnosi "flusso scommesse
fermo": serve una misura RIPETIBILE del flusso a 24 ore che risponda a una sola
domanda - **dove si ferma il funnel e per quale motivo**. Gli stadi misurati
sono, nell'ordine in cui il segnale li attraversa:

  1. ANALISI    partite analizzate (`match_analysis`) e leghe coperte;
  2. SEGNALI    righe `predictions` per mercato e per stato (giocabili/scartate);
  3. GATE       attribuzione di OGNI riga scartata al motivo del gate;
  4. ORDINI     righe `bets` per modalita' (live/sim/rejected-t60);
  5. CATENA     righe `decisions` per verdetto e stato (shadow, 16/09);
  6. LIQUIDITA' scarti del monitor SX (edge perso, non silenzioso).

Perche' un modulo e non una query a mano: la diagnosi del 24/09 e' stata fatta
con un'attribuzione manuale, e una misura manuale non si ripete. Qui il motivo
di scarto si ricalcola con il gate di PRODUZIONE (`value_filter.is_sane`), non
con una copia delle soglie - un `is_sane` che cambia non puo' divergere dalla
misura.

Sola LETTURA per costruzione: SQLite in `mode=ro` (un test prova a scrivere e
pretende il rifiuto), nessun ordine, nessuna rete, zero crediti the-odds-api.

Uso:
    venv/bin/python flow_measure.py                  # ultime 24 ore
    venv/bin/python flow_measure.py --hours 72
    venv/bin/python flow_measure.py --json
    venv/bin/python flow_measure.py --db /percorso/quotaverace.db
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from config import DATA_DIR

logger = logging.getLogger("flow_measure")

# Finestra di default: 24 ore ("misurare il flusso a 24 ore").
DEFAULT_HOURS = float(os.getenv("FLOW_WINDOW_HOURS", "24"))

# Stati del ledger che il bot considera giocabili: UNA definizione sola
# (`value_filter.PLAYABLE_TIERS`), importata - copiare la tripla sarebbe il
# modo silenzioso per far divergere due misure.
from value_filter import (PLAYABLE_TIERS, canonical_league, is_sane, league_tier)

#: Stati di lega ammessi dalla strategia (le stesse di `league_allowed`).
ALLOWED_STATES = ("core", "probation")


def _league_state(league: Any) -> str:
    """'core' | 'probation' | 'blocked' | 'unknown' - mai una fusione.

    `unknown` esiste di proposito: `league_tier("")` risponde `blocked`, ma
    "lega ignota" e "lega vietata" sono cose diverse. La lega si perde quando
    la riga `matches` viene potata (`clear_old_matches`) o sulle righe scritte
    prima della colonna `predictions.league` (22/09): in quei casi il gate NON
    e' giudicabile e va dichiarato, non contato come un divieto.
    """
    name = canonical_league(str(league or "").strip())
    if not name:
        return "unknown"
    return league_tier(name)


def _db_path() -> Path:
    return Path(DATA_DIR) / "quotaverace.db"


def _connect(path: Optional[str | Path] = None) -> sqlite3.Connection:
    """Connessione READ-ONLY: questa diagnostica non scrive mai."""
    target = Path(path) if path else _db_path()
    conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _parse_ts(value: Any) -> Optional[datetime]:
    """Istante UTC da una data del ledger, tollerante ai formati reali.

    Le date del ledger sono ISO con la 'T' (a volte con 'Z' o offset) e i
    confronti SQL fra stringhe sono trappole note (lezione 17/09): qui si
    normalizza in Python, cosi' il filtro di finestra non dipende dal formato.
    Un istante senza fuso viene assunto UTC (il ledger scrive UTC); un valore
    illeggibile vale None e non entra in nessuna finestra.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _in_window(value: Any, start: datetime, end: datetime) -> bool:
    ts = _parse_ts(value)
    return ts is not None and start <= ts <= end


def _rows(conn: sqlite3.Connection, sql: str, args: Tuple = ()) -> List[sqlite3.Row]:
    """SELECT difensivo: una tabella mancante non fa fallire la misura."""
    try:
        return conn.execute(sql, args).fetchall()
    except sqlite3.Error as exc:
        logger.warning("flow_measure: query non eseguibile (%s)", exc)
        return []


def _bump(counter: Dict[str, int], key: Any) -> None:
    name = str(key or "?").strip() or "?"
    counter[name] = counter.get(name, 0) + 1


# --- Attribuzione degli scarti ---------------------------------------------
# Ordine = ordine dei controlli in `value_filter.is_sane`. Un motivo nuovo
# (esito diverso) finisce in "altro" col testo, mai fatto sparire.
REASON_CODES: Tuple[Tuple[str, str], ...] = (
    ("lega ", "league_blocked"),
    ("quota troppo bassa", "odds_min"),
    ("quota troppo alta", "odds_max"),
    # Fascia morta 1.50-1.80 (25/10/2026): senza questa voce un gate nuovo
    # finirebbe in "altro" e il suo peso nella diagnosi del flusso sarebbe
    # invisibile proprio quando serve (la lezione del 24/09).
    ("fascia morta", "odds_dead_band"),
    ("non e' il favorito", "not_favourite"),
    ("ANOMALIA", "ev_anomaly"),
    ("EV troppo basso", "ev_low"),
    ("non batte il mercato", "edge_low"),
)


def _reason_code(reason: str) -> str:
    for needle, code in REASON_CODES:
        if needle in (reason or ""):
            return code
    return "altro"


def attribute_reject(row: Dict[str, Any]) -> Tuple[str, str]:
    """Perche' una riga `rejected` e' stata scartata, secondo il gate VERO.

    Ritorna (codice, motivo testuale). Se il gate di produzione OGGI approva
    la riga, il rifiuto e' arrivato da un livello che `is_sane` non copre
    (favourite gate, profondita' del book, feed): codice
    `gate_non_riconciliato` - dichiarato, non attribuito a un motivo inventato.
    """
    market = str(row.get("mercato") or "").upper()
    try:
        sane, reason = is_sane(
            float(row.get("prob") or 0.0),
            float(row.get("quota") or 0.0),
            float(row.get("ev") or 0.0),
            market_prob=(float(row["market_prob"])
                         if row.get("market_prob") is not None else None),
            league=str(row.get("league") or ""),
            # Stessa convenzione di multi_market (il favorito e' scelto a
            # monte sui mercati a 2 esiti).
            favourites_only=(market == "1X2"),
        )
    except (TypeError, ValueError) as exc:
        return "altro", f"riga non interpretabile ({exc})"
    if sane:
        return "gate_non_riconciliato", "il gate odierno approva la riga"
    return _reason_code(reason), reason


# --- Stadi del funnel ------------------------------------------------------

def _analysis_stage(conn: sqlite3.Connection, start, end) -> Dict[str, Any]:
    rows = _rows(conn, "SELECT a.match_id, a.status, a.timestamp, m.league "
                       "FROM match_analysis a LEFT JOIN matches m ON m.id = a.match_id")
    kept = [r for r in rows if _in_window(r["timestamp"], start, end)]
    by_tier: Dict[str, int] = {}
    by_league: Dict[str, int] = {}
    for r in kept:
        _bump(by_tier, _league_state(r["league"]))
        if r["league"]:
            _bump(by_league, r["league"])
    return {
        "rows": len(kept),
        "matches": len({r["match_id"] for r in kept}),
        "by_tier": by_tier,
        "by_league": dict(sorted(by_league.items(), key=lambda kv: -kv[1])),
    }


def _signals_stage(conn: sqlite3.Connection, start, end) -> Dict[str, Any]:
    rows = _rows(conn, "SELECT p.match_id, p.mercato, p.esito, p.quota, p.prob, "
                       "p.ev, p.market_prob, p.market_edge, p.status, p.league, "
                       "p.esito_finale, p.created_at, m.league AS league_m "
                       "FROM predictions p LEFT JOIN matches m ON m.id = p.match_id")
    kept = [dict(r) for r in rows if _in_window(r["created_at"], start, end)]
    by_market: Dict[str, int] = {}
    by_status: Dict[str, int] = {}
    playable: Dict[str, int] = {}
    rejects: List[Dict[str, Any]] = []
    for row in kept:
        # La lega vive sulla riga (22/09) e, per lo storico, sulla JOIN.
        row["league"] = str(row.get("league") or row.get("league_m") or "")
        _bump(by_market, row["mercato"])
        status = str(row.get("status") or "senza_stato").strip().lower()
        _bump(by_status, status)
        if status in PLAYABLE_TIERS:
            _bump(playable, str(row["mercato"] or "?"))
        elif status == "rejected":
            rejects.append(row)
    reasons: Dict[str, int] = {}
    reject_leagues: Dict[str, int] = {}
    reject_tiers: Dict[str, int] = {}
    playable_in_allowed = 0
    playable_unknown = 0
    unknown_rows = 0
    for row in rejects:
        code, _ = attribute_reject(row)
        _bump(reasons, code)
        if row["league"]:
            _bump(reject_leagues, row["league"])
        _bump(reject_tiers, _league_state(row["league"]))
    for row in kept:
        if not row["league"]:
            unknown_rows += 1
        if str(row.get("status") or "").lower() not in PLAYABLE_TIERS:
            continue
        state = _league_state(row["league"])
        if state in ALLOWED_STATES:
            playable_in_allowed += 1
        elif state == "unknown":
            playable_unknown += 1
    return {
        "rows": len(kept),
        "by_market": by_market,
        "by_status": by_status,
        "playable": sum(playable.values()),
        "playable_by_market": playable,
        "playable_in_allowed_leagues": playable_in_allowed,
        # Lega non attribuibile (riga `matches` potata o pre-22/09): il gate
        # non e' giudicabile su queste righe e il report lo dichiara.
        "playable_unknown_league": playable_unknown,
        "unknown_league_rows": unknown_rows,
        "open": len([r for r in kept if r.get("esito_finale") is None]),
        "rejects": {
            "rows": len(rejects),
            "by_reason": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
            "by_league": dict(sorted(reject_leagues.items(), key=lambda kv: -kv[1])[:10]),
            "by_tier": reject_tiers,
        },
    }


def _orders_stage(conn: sqlite3.Connection, start, end) -> Dict[str, Any]:
    rows = _rows(conn, "SELECT id, match_id, mode, status, stake, price, profit, "
                       "esito_finale, created_at FROM bets")
    kept = [r for r in rows if _in_window(r["created_at"], start, end)]
    by_mode: Dict[str, int] = {}
    by_status: Dict[str, int] = {}
    staked = 0.0
    for r in kept:
        _bump(by_mode, r["mode"])
        _bump(by_status, r["status"])
        try:
            staked += float(r["stake"] or 0.0)
        except (TypeError, ValueError):
            pass
    return {
        "rows": len(kept),
        "by_mode": by_mode,
        "by_status": by_status,
        "staked": round(staked, 2),
        "closing_now": len([r for r in kept if r["esito_finale"] is None]),
    }


def _chain_stage(conn: sqlite3.Connection, start, end) -> Dict[str, Any]:
    rows = _rows(conn, "SELECT verdict, status, reason, mode, stake, esito_finale, "
                       "created_at FROM decisions")
    kept = [r for r in rows if _in_window(r["created_at"], start, end)]
    by_verdict: Dict[str, int] = {}
    by_status: Dict[str, int] = {}
    by_reason: Dict[str, int] = {}
    for r in kept:
        _bump(by_verdict, r["verdict"])
        _bump(by_status, r["status"])
        if str(r["verdict"] or "").lower() != "approve":
            _bump(by_reason, r["reason"])
    return {"rows": len(kept), "by_verdict": by_verdict,
            "by_status": by_status, "by_reason": by_reason}


def _quotes_stage(conn: sqlite3.Connection, start, end) -> Dict[str, Any]:
    rows = _rows(conn, "SELECT market_type, line_key, updated_at, observed_at "
                       "FROM market_quotes")
    kept = [r for r in rows
            if _in_window(r["updated_at"] or r["observed_at"], start, end)]
    by_market: Dict[str, int] = {}
    for r in kept:
        _bump(by_market, r["market_type"])
    return {"rows": len(kept), "by_market": by_market}


def _liquidity_stage(hours: float, events: Optional[List[dict]] = None) -> Dict[str, Any]:
    """Scarti per liquidita' dalla finestra (mai un'eccezione)."""
    if events is None:
        try:
            from liquidity_monitor import iter_events
            events = iter_events(days=hours / 24.0)
        except Exception as exc:                      # dipendenza non importabile
            logger.warning("flow_measure: monitor liquidita' non leggibile (%s)", exc)
            events = []
    by_kind: Dict[str, int] = {}
    by_reason: Dict[str, int] = {}
    for e in events or []:
        if not isinstance(e, dict):
            continue
        _bump(by_kind, e.get("kind"))
        _bump(by_reason, e.get("reason"))
    return {"events": len(events or []), "by_kind": by_kind, "by_reason": by_reason}


def _verdict(analysis: Dict, signals: Dict, orders: Dict) -> str:
    """Dove si ferma il funnel, in una riga (il motivo, non un numero)."""
    if orders["rows"] > 0:
        return (f"flusso ATTIVO: {orders['rows']} ordini nella finestra "
                f"(stake {orders['staked']})")
    if signals["rows"] == 0:
        return ("NESSUN SEGNALE generato: la pipeline non ha prodotto candidati "
                f"(analisi nella finestra: {analysis['matches']} partite) - "
                "guardare copertura e rotazione, non i gate")
    if signals["playable"] > 0:
        return (f"{signals['playable']} segnali giocabili ma 0 ordini: il blocco "
                "e' a valle dei gate (T-60, feed di mercato, cap, liquidita')")
    top = next(iter(signals["rejects"]["by_reason"]), "?")
    return (f"segnali generati ({signals['rows']}) ma TUTTI scartati: motivo "
            f"dominante '{top}' ({signals['rejects']['by_reason'].get(top, 0)} righe)")


def measure(hours: Optional[float] = None, *, now: Optional[datetime] = None,
            path: Optional[str | Path] = None,
            conn: Optional[sqlite3.Connection] = None,
            liquidity_events: Optional[List[dict]] = None) -> Dict[str, Any]:
    """Misura il flusso sulla finestra. NON solleva: su errore ritorna `error`."""
    hours = float(hours if hours is not None else DEFAULT_HOURS)
    end = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    start = end - timedelta(hours=hours)
    out: Dict[str, Any] = {
        "readonly": True, "hours": hours,
        "window": {"from": start.isoformat(), "to": end.isoformat()},
        "playable_tiers": list(PLAYABLE_TIERS),
    }
    own = conn is None
    try:
        conn = conn or _connect(path)
        out["analysis"] = _analysis_stage(conn, start, end)
        out["signals"] = _signals_stage(conn, start, end)
        out["orders"] = _orders_stage(conn, start, end)
        out["chain"] = _chain_stage(conn, start, end)
        out["quotes"] = _quotes_stage(conn, start, end)
        out["liquidity"] = _liquidity_stage(hours, liquidity_events)
        out["verdict"] = _verdict(out["analysis"], out["signals"], out["orders"])
        return out
    except Exception as exc:                          # mai un'eccezione al chiamante
        logger.warning("flow_measure: misura fallita (%s)", exc)
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _fmt_counts(counter: Optional[Dict[str, int]]) -> str:
    if not counter:
        return "nessuno"
    return ", ".join(f"{k} {v}" for k, v in counter.items())


def format_report(d: Optional[Dict[str, Any]] = None) -> str:
    """Report leggibile (CLI/Telegram): il funnel riga per riga."""
    data = d if isinstance(d, dict) else measure()
    if data.get("error"):
        return f"⚠️ Misura flusso non disponibile ({data['error']})"
    a, s, o = data["analysis"], data["signals"], data["orders"]
    lines = [
        f"🌊 FLUSSO SCOMMESSE - ultime {data['hours']:g}h "
        f"({data['window']['from'][:16]} → {data['window']['to'][:16]} UTC)",
        f"  1. ANALISI   {a['rows']} righe di analisi, {a['matches']} partite, "
        f"leghe per tier: {_fmt_counts(a['by_tier'])}",
        f"  2. SEGNALI   {s['rows']} righe ({_fmt_counts(s['by_market'])}) | "
        f"giocabili {s['playable']} (leghe ammesse "
        f"{s['playable_in_allowed_leagues']}, lega ignota "
        f"{s['playable_unknown_league']}) | aperte {s['open']}",
        f"               stati: {_fmt_counts(s['by_status'])}",
        f"  3. GATE      {s['rejects']['rows']} scartate | motivi: "
        f"{_fmt_counts(s['rejects']['by_reason'])}",
        f"               leghe scartate (top): {_fmt_counts(s['rejects']['by_league'])}",
        f"               tier degli scarti: {_fmt_counts(s['rejects']['by_tier'])}",
        f"  4. ORDINI    {o['rows']} righe | modalita': {_fmt_counts(o['by_mode'])} | "
        f"stake {o['staked']} | stati: {_fmt_counts(o['by_status'])}",
        f"  5. CATENA    {data['chain']['rows']} decisioni | verdetti: "
        f"{_fmt_counts(data['chain']['by_verdict'])}",
        f"  6. LIQUID.   {data['liquidity']['events']} scarti | tipi: "
        f"{_fmt_counts(data['liquidity']['by_kind'])}",
        "",
        f"  ➜ {data['verdict']}",
    ]
    if s.get("unknown_league_rows"):
        lines.append(
            f"  ⚠️ {s['unknown_league_rows']} righe senza lega attribuibile "
            "(riga `matches` potata o riga scritta prima del 22/09): su queste "
            "il gate leghe NON e' giudicabile - non contarle come un divieto")
    if a["by_league"]:
        top = list(a["by_league"].items())[:8]
        lines.append("  leghe analizzate: "
                     + ", ".join(f"{k} {v}" for k, v in top))
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Misura il flusso della corsia scommesse (sola lettura)")
    parser.add_argument("--hours", type=float, default=DEFAULT_HOURS,
                        help=f"finestra in ore (default {DEFAULT_HOURS:g})")
    parser.add_argument("--db", type=str, default=None,
                        help="percorso del ledger SQLite (default DATA_DIR)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    data = measure(args.hours, path=(Path(args.db) if args.db else None))
    print(json.dumps(data, indent=1, ensure_ascii=False) if args.json
          else format_report(data))
    return 1 if data.get("error") else 0


if __name__ == "__main__":
    raise SystemExit(main())
