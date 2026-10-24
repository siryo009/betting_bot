"""Caricamento in sola lettura dello snapshot di produzione.

Perche' uno snapshot e non il DB vivo: il laboratorio deve poter ripetere le
misure identiche domani (riproducibilita') e non deve poter scrivere sul ledger
che regola denaro reale. La copia e' stata presa con `sqlite3.Connection.backup`
dal container (07:55 UTC del 20/10/2026) ed e' la stessa che il report dichiara.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Optional

DEFAULT_DB = Path(__file__).resolve().parents[1] / "snapshot" / "quotaverace_prod.db"
DB_PATH = Path(os.getenv("QV_RESEARCH_DB", str(DEFAULT_DB)))


def require_snapshot(path: Optional[os.PathLike] = None) -> Path:
    p = Path(path or DB_PATH)
    if not p.exists():
        raise FileNotFoundError(
            f"snapshot assente: {p}. Il laboratorio non inventa dati: "
            "riesportare lo snapshot dal volume Railway (vedi README).")
    return p


def connect(path: Optional[os.PathLike] = None) -> sqlite3.Connection:
    """Connessione READ-ONLY (il driver rifiuta ogni scrittura)."""
    p = require_snapshot(path)
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def audit(path: Optional[os.PathLike] = None) -> dict:
    """Integrita' + conteggi: la prima cosa da guardare prima di misurare."""
    conn = connect(path)
    try:
        out = {"path": str(Path(path or DB_PATH))}
        out["quick_check"] = conn.execute("PRAGMA quick_check").fetchone()[0]
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        out["tables"] = {}
        for t in tables:
            try:
                out["tables"][t] = conn.execute(
                    f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except sqlite3.Error as exc:  # pragma: no cover - difensivo
                out["tables"][t] = f"errore: {exc}"
        return out
    finally:
        conn.close()


def fetch_df(sql: str, params: Optional[tuple] = None, path=None):
    """Esegue una SELECT e ritorna un DataFrame (pandas, gia' nel progetto)."""
    import pandas as pd

    conn = connect(path)
    try:
        return pd.read_sql_query(sql, conn, params=params or ())
    finally:
        conn.close()


def predictions(path=None):
    """Ledger previsioni con le colonne che servono alla valutazione."""
    return fetch_df(
        "SELECT id, match_id, mercato, esito, quota, prob, ev, market_prob, "
        "market_edge, status, esito_finale, profit, league, created_at, "
        "settled_at FROM predictions", path=path)


def bets(path=None):
    return fetch_df(
        "SELECT id, match_id, mercato, esito, market_id, selection_id, price, "
        "stake, mode, status, bet_id, esito_finale, profit, created_at, "
        "settled_at, home, away, league, commence FROM bets ORDER BY id",
        path=path)


def clv(path=None):
    return fetch_df(
        "SELECT match_id, esito, signal_quota, closing_quota, pinnacle_quota, "
        "closing_odds, closing_source, betfair_odds, updated_at FROM clv_history",
        path=path)


def results(path=None):
    return fetch_df(
        "SELECT match_id, league, home_team, away_team, score_home, score_away, "
        "result, settled_at FROM match_results", path=path)


def snapshots(path=None):
    return fetch_df(
        "SELECT match_id, esito, price, bookmaker, market_prob, recorded_at "
        "FROM price_snapshots", path=path)
