"""Intersezione fra la griglia di SX Bet e le linee pubblicate dall'oracolo.

PERCHE' ESISTE (07/10/2026). Un pick OU/AH diventa un ordine solo se la SUA
linea esiste anche nel payload di Pinnacle: il gate top-down e' fail-closed
(senza p_true non si ordina). Ma SX quota una GRIGLIA fitta (OU 1.5/2/2.5/3/
3.5/4/4.5..., AH a passi di 0,5) mentre Pinnacle, via the-odds-api, pubblica
tipicamente la SOLA linea main — che puo' essere una quarter line (3.25,
+0.25). Quando le due griglie non si intersecano, OGNI pick di quella partita
e' strutturalmente non ordinabile: misurato il 06-07/10 su MLS
Chicago Fire-Vancouver (SX 3.0/3.5/4.0/4.5 vs Pinnacle 3.25 -> intersezione
VUOTA, 0 pick ordinabili a qualunque budget).

Questo strumento quantifica il fenomeno su dati reali, cosi' la domanda "quanti
pick nascono non ordinabili?" ha una risposta misurata invece che stimata.
Quattro esiti per (fixture, mercato):
  - `unknown`         -> la partita non e' in nessuna cache oracolo fresca:
                         l'oracolo non e' mai stato pagato per quella lega;
  - `oracle_empty`    -> cache presente ma Pinnacle non prezza alcuna linea;
  - `no_intersection` -> Pinnacle prezza, ma nessuna linea combacia (il caso MLS);
  - `priceable`       -> almeno una linea di SX e' prezzata (n linee prezzabili).

SOLA LETTURA e OFFLINE: SQLite in `mode=ro`, nessuna rete, nessun ordine,
nessun credito. Le linee dell'oracolo si leggono dalle cache gia' sul volume
(`pinnacle_oracle.oracle_lines`, UNICA definizione della lettura).

CLI:
    venv/bin/python line_intersection.py [--days 7] [--json] [--db PATH]
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

import tracker

MARKETS_IN_SCOPE = ("OU", "AH")
PLAYABLE_TIERS = ("value", "strong_value", "moderate")

STATUS_UNKNOWN = "unknown"
STATUS_ORACLE_EMPTY = "oracle_empty"
STATUS_NO_INTERSECTION = "no_intersection"
STATUS_PRICEABLE = "priceable"


def window_days() -> float:
    """Finestra di analisi in giorni (`LINE_INTERSECTION_DAYS`, default 7)."""
    raw = os.getenv("LINE_INTERSECTION_DAYS")
    if raw is None or not str(raw).strip():
        return 7.0
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return 7.0
    return val if val > 0 else 7.0


def _ro_conn(db: Optional[str] = None) -> sqlite3.Connection:
    """Connessione in SOLA LETTURA (la misura non deve poter scrivere)."""
    path = db or str(tracker.DB_PATH)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _oracle_lines(home: str, away: str, market_type: str) -> Optional[set]:
    """Linee prezzate da Pinnacle (`None` = oracolo IGNOTO). Mai un'eccezione.

    Delega a `pinnacle_oracle.oracle_lines`: la lettura delle cache, la
    freschezza e l'aggancio per NOMI restano di chi le possiede — qui non si
    ricopia nessuna regola.
    """
    try:
        import pinnacle_oracle as po
        return po.oracle_lines(home or "", away or "", market_type=market_type)
    except Exception:
        return None


def sx_lines(conn: sqlite3.Connection, days: float
             ) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """Linee di SX per `(fixture_id, market_type)` dalle quote ingerite.

    Fuori finestra temporale le righe si ignorano: interessa la griglia che il
    sistema sta davvero valutando, non l'intero storico di `market_quotes`.
    """
    out: Dict[Tuple[str, str], Dict[str, Any]] = {}
    try:
        rows = conn.execute(
            "SELECT fixture_id, market_type, line, home, away, league, kickoff "
            "FROM market_quotes WHERE market_type IN (?,?) AND line IS NOT NULL",
            MARKETS_IN_SCOPE).fetchall()
    except Exception:
        return out
    cutoff_ts = None
    try:
        from datetime import datetime, timedelta, timezone
        cutoff = datetime.now(timezone.utc) - timedelta(days=float(days))
        cutoff_ts = cutoff.timestamp()
    except Exception:
        cutoff_ts = None
    for r in rows:
        ko = r["kickoff"]
        if cutoff_ts is not None and ko:
            try:
                from datetime import datetime, timezone
                txt = str(ko).replace("Z", "+00:00")
                ts = datetime.fromisoformat(txt)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                # Si tiene anche il futuro (i pick nascono prima del kickoff).
                if ts.timestamp() < cutoff_ts - 3 * 86400:
                    continue
            except Exception:
                pass
        key = (str(r["fixture_id"] or ""), str(r["market_type"] or "").upper())
        if not key[0]:
            continue
        slot = out.setdefault(key, {"fixture_id": key[0], "market_type": key[1],
                                    "home": r["home"] or "", "away": r["away"] or "",
                                    "league": r["league"] or "",
                                    "kickoff": ko or "", "lines": set()})
        try:
            slot["lines"].add(round(float(r["line"]), 6))
        except (TypeError, ValueError):
            continue
        if not slot["home"] and r["home"]:
            slot["home"] = r["home"]
    return out


def classify_fixture(slot: Dict[str, Any]) -> Dict[str, Any]:
    """Esito di una (fixture, mercato): prezzabile, vuoto, disallineato o ignoto."""
    known = _oracle_lines(slot.get("home") or "", slot.get("away") or "",
                          slot.get("market_type") or "")
    lines = sorted(slot.get("lines") or [])
    entry = {"fixture_id": slot.get("fixture_id"), "market_type": slot.get("market_type"),
             "league": slot.get("league"), "kickoff": slot.get("kickoff"),
             "sx_lines": lines, "n_sx_lines": len(lines),
             "oracle_lines": sorted(known) if known is not None else None,
             "n_oracle_lines": len(known) if known is not None else 0,
             "priceable_lines": [], "n_priceable": 0}
    if known is None:
        entry["status"] = STATUS_UNKNOWN
        return entry
    matched = [ln for ln in lines
               if any(abs(ln - float(k)) < 1e-6 for k in known)]
    entry["priceable_lines"] = matched
    entry["n_priceable"] = len(matched)
    if not known:
        entry["status"] = STATUS_ORACLE_EMPTY
    elif matched:
        entry["status"] = STATUS_PRICEABLE
    else:
        entry["status"] = STATUS_NO_INTERSECTION
    return entry


def pick_rows(conn: sqlite3.Connection, days: float) -> List[Dict[str, Any]]:
    """Pick OU/AH aperti, con lo stato di prezzabilita' della loro linea.

    E' il KPI della direttiva "i pick non nascono non ordinabili": dice quanti
    segnali marcati giocabili hanno una linea che l'oracolo non prezza (e che
    quindi non potranno mai diventare un ordine).
    """
    out: List[Dict[str, Any]] = []
    try:
        rows = conn.execute(
            "SELECT p.match_id, p.mercato, p.esito, p.status, p.quota, p.league, "
            "m.home_team, m.away_team, m.commence_time "
            "FROM predictions p LEFT JOIN matches m ON m.id = p.match_id "
            "WHERE p.mercato IN (?,?) AND p.esito_finale IS NULL",
            MARKETS_IN_SCOPE).fetchall()
    except Exception:
        return out
    import multi_market
    for r in rows:
        rec = {"match_id": r["match_id"], "mercato": str(r["mercato"]).upper(),
               "esito": r["esito"], "status": r["status"], "league": r["league"],
               "quota": r["quota"], "playable": str(r["status"]) in PLAYABLE_TIERS}
        try:
            target = multi_market.order_target({"mercato": rec["mercato"],
                                                "esito_key": r["esito"]})
        except Exception:
            target = None
        rec["line"] = (target or {}).get("line")
        if rec["line"] is None:
            rec["line_status"] = "no_line"
        else:
            known = _oracle_lines(r["home_team"] or "", r["away_team"] or "",
                                  rec["mercato"])
            if known is None:
                rec["line_status"] = STATUS_UNKNOWN
            elif any(abs(float(rec["line"]) - float(k)) < 1e-6 for k in known):
                rec["line_status"] = STATUS_PRICEABLE
            else:
                rec["line_status"] = STATUS_NO_INTERSECTION
        out.append(rec)
    return out


def measure(*, days: Optional[float] = None, db: Optional[str] = None
            ) -> Dict[str, Any]:
    """Misura completa: fixture OU/AH + pick aperti. Mai un'eccezione."""
    win = window_days() if days is None else float(days)
    rep: Dict[str, Any] = {"days": win, "db": db or str(tracker.DB_PATH),
                           "fixtures": [], "by_status": {}, "by_league": {},
                           "picks": [], "picks_by_status": {}, "error": None}
    try:
        conn = _ro_conn(db)
    except Exception as exc:
        rep["error"] = f"DB non leggibile: {exc}"
        return rep
    try:
        slots = sx_lines(conn, win)
        for slot in slots.values():
            try:
                rep["fixtures"].append(classify_fixture(slot))
            except Exception:
                continue
        rep["picks"] = pick_rows(conn, win)
    except Exception as exc:
        rep["error"] = str(exc)
    finally:
        try:
            conn.close()
        except Exception:
            pass
    rep["by_status"] = _count_by(rep["fixtures"], "status")
    rep["by_league"] = _league_table(rep["fixtures"])
    rep["picks_by_status"] = _count_by(rep["picks"], "line_status")
    rep["totals"] = _totals(rep)
    return rep


def _count_by(rows: List[Dict[str, Any]], key: str) -> Dict[str, int]:
    out: Dict[str, int] = defaultdict(int)
    for r in rows:
        out[str(r.get(key))] += 1
    return dict(sorted(out.items()))


def _league_table(fixtures: List[Dict[str, Any]]) -> Dict[str, Dict[str, int]]:
    out: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for f in fixtures:
        league = str(f.get("league") or "?")
        out[league][str(f.get("status"))] += 1
    return {k: dict(sorted(v.items())) for k, v in sorted(out.items())}


def _totals(rep: Dict[str, Any]) -> Dict[str, Any]:
    fixtures = rep.get("fixtures") or []
    picks = rep.get("picks") or []
    n_sx = sum(int(f.get("n_sx_lines") or 0) for f in fixtures)
    n_priceable = sum(int(f.get("n_priceable") or 0) for f in fixtures)
    playable_picks = [p for p in picks if p.get("playable")]
    return {
        "fixtures": len(fixtures),
        "fixtures_priceable": sum(1 for f in fixtures
                                  if f.get("status") == STATUS_PRICEABLE),
        "fixtures_no_intersection": sum(1 for f in fixtures
                                        if f.get("status") == STATUS_NO_INTERSECTION),
        "sx_lines": n_sx,
        "sx_lines_priceable": n_priceable,
        "line_coverage_pct": (round(100.0 * n_priceable / n_sx, 1) if n_sx else None),
        "picks_open": len(picks),
        "picks_playable": len(playable_picks),
        "picks_playable_unpriceable": sum(
            1 for p in playable_picks
            if p.get("line_status") == STATUS_NO_INTERSECTION),
        "picks_playable_unknown_oracle": sum(
            1 for p in playable_picks
            if p.get("line_status") == STATUS_UNKNOWN),
    }


def format_report(rep: Dict[str, Any]) -> List[str]:
    """Report testuale (lista di righe: Telegram-friendly, testabile)."""
    if rep.get("error") and not rep.get("fixtures"):
        return [f"🎯 intersezione linee: misura non disponibile ({rep['error']})"]
    t = rep.get("totals") or {}
    cov = t.get("line_coverage_pct")
    cov_txt = "" if cov is None else f" ({cov}%)"
    lines = [f"🎯 INTERSEZIONE LINEE SX vs ORACOLO (finestra {rep.get('days')} gg)",
             f"  fixture OU/AH con quote SX: {t.get('fixtures', 0)}",
             f"  esiti per fixture: {rep.get('by_status')}",
             f"  linee SX: {t.get('sx_lines', 0)} | prezzabili dall'oracolo: "
             f"{t.get('sx_lines_priceable', 0)}{cov_txt}",
             f"  pick aperti OU/AH: {t.get('picks_open', 0)} "
             f"(giocabili {t.get('picks_playable', 0)})",
             f"  ⚠️ giocabili con linea NON prezzabile: "
             f"{t.get('picks_playable_unpriceable', 0)} | con oracolo ignoto: "
             f"{t.get('picks_playable_unknown_oracle', 0)}",
             f"  pick per stato linea: {rep.get('picks_by_status')}"]
    for league, bucket in (rep.get("by_league") or {}).items():
        lines.append(f"    · {league}: {bucket}")
    return lines


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=float, default=None,
                    help="finestra in giorni (default LINE_INTERSECTION_DAYS=7)")
    ap.add_argument("--db", default=None, help="path del DB (default tracker.DB_PATH)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    rep = measure(days=args.days, db=args.db)
    if args.json:
        print(json.dumps(rep, indent=2, default=str))
    else:
        for line in format_report(rep):
            print(line)
    return 1 if rep.get("error") and not rep.get("fixtures") else 0


if __name__ == "__main__":                                            # pragma: no cover
    sys.exit(main())
