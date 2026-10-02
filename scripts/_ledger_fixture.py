"""Helper dei test degli script: ledger SQLite temporanei.

Lo schema ricalca quello REALE di produzione (`tracker.py`), altrimenti i test
misurerebbero un DB che non esiste. Le date si costruiscono **relative a `now`**
con `rel_iso()`: un test con una data fissa scade col calendario e fallisce
senza che nulla sia rotto (lezione del 15/09, 17/09 e 21/09 in questo progetto).

Non e' un file di test (`_` iniziale): pytest non lo raccoglie.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Optional

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS predictions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        match_id TEXT, mercato TEXT, esito TEXT, quota REAL, prob REAL, ev REAL,
        market_prob REAL, market_edge REAL, status TEXT, esito_finale TEXT,
        profit REAL, created_at TEXT, settled_at TEXT, league TEXT)""",
    """CREATE TABLE IF NOT EXISTS price_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        match_id TEXT NOT NULL, esito TEXT NOT NULL, price REAL NOT NULL,
        bookmaker TEXT, market_prob REAL, recorded_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS match_results (
        match_id TEXT PRIMARY KEY, league TEXT, home_team TEXT, away_team TEXT,
        score_home INTEGER, score_away INTEGER, result TEXT, settled_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS clv_history (
        match_id TEXT, esito TEXT, signal_quota REAL, closing_quota REAL,
        updated_at TEXT, pinnacle_quota REAL)""",
    """CREATE TABLE IF NOT EXISTS bets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        match_id TEXT, mercato TEXT, esito TEXT, market_id TEXT,
        selection_id TEXT, price REAL, stake REAL, mode TEXT, status TEXT,
        bet_id TEXT, esito_finale TEXT, profit REAL, created_at TEXT,
        settled_at TEXT)""",
)


def rel_iso(minutes: float = 0.0) -> str:
    """ISO UTC relativo ad ADESSO (mai una data fissa)."""
    return (datetime.now() + timedelta(minutes=float(minutes))).isoformat()


def make_db(path) -> sqlite3.Connection:
    """Crea un ledger temporaneo con lo schema di produzione. Ritorna la conn.

    `row_factory = sqlite3.Row` come nel percorso reale: i loader degli script
    leggono le righe per nome (`dict(row)`), quindi una connessione nuda
    farebbe fallire la lettura per un motivo che la produzione non ha.
    """
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    for stmt in SCHEMA:
        conn.execute(stmt)
    conn.commit()
    return conn


def add_prediction(conn, match_id: str, mercato: str = "1X2", esito: str = "1",
                   quota: Optional[float] = 2.0, prob: Optional[float] = 0.55,
                   status: str = "value", esito_finale: Optional[str] = None,
                   league: Optional[str] = None, created_at: Optional[str] = None,
                   settled_at: Optional[str] = None) -> None:
    conn.execute(
        "INSERT INTO predictions (match_id, mercato, esito, quota, prob, status,"
        " esito_finale, created_at, settled_at, league) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (match_id, mercato, esito, quota, prob, status, esito_finale,
         created_at or rel_iso(-120), settled_at, league))
    conn.commit()


def add_snapshot(conn, match_id: str, esito: str, price: float,
                 bookmaker: str = "pinnacle", when: Optional[str] = None) -> None:
    conn.execute(
        "INSERT INTO price_snapshots (match_id, esito, price, bookmaker,"
        " recorded_at) VALUES (?,?,?,?,?)",
        (match_id, esito, float(price), bookmaker, when or rel_iso(-60)))
    conn.commit()


def add_series(conn, match_id: str, esito: str, prices, *, bookmaker: str = "pinnacle",
               start_minutes: float = -120.0, step_minutes: float = 10.0) -> None:
    """Serie cronologica di prezzi (comoda per lo steam move)."""
    for i, price in enumerate(prices):
        add_snapshot(conn, match_id, esito, price, bookmaker=bookmaker,
                     when=rel_iso(start_minutes + i * step_minutes))


def add_result(conn, match_id: str, result: str, *, home: str = "Casa",
               away: str = "Ospite", sh: int = 2, sa: int = 1) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO match_results (match_id, league, home_team,"
        " away_team, score_home, score_away, result, settled_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (match_id, "Test League", home, away, sh, sa, result, rel_iso(-10)))
    conn.commit()


def add_clv(conn, match_id: str, esito: str, signal_quota: float,
            closing_quota: float, *, pinnacle_quota: Optional[float] = None,
            updated_at: Optional[str] = None) -> None:
    conn.execute(
        "INSERT INTO clv_history (match_id, esito, signal_quota, closing_quota,"
        " updated_at, pinnacle_quota) VALUES (?,?,?,?,?,?)",
        (match_id, esito, float(signal_quota), float(closing_quota),
         updated_at or rel_iso(-30), pinnacle_quota))
    conn.commit()


def add_bet(conn, match_id: str, esito: str, price: float, *, mode: str = "live",
            mercato: str = "1X2", created_at: Optional[str] = None) -> None:
    conn.execute(
        "INSERT INTO bets (match_id, mercato, esito, price, stake, mode,"
        " created_at) VALUES (?,?,?,?,?,?,?)",
        (match_id, mercato, esito, float(price), 1.5, mode,
         created_at or rel_iso(-90)))
    conn.commit()


def close(conn: sqlite3.Connection) -> None:
    try:
        conn.commit()
    finally:
        conn.close()
