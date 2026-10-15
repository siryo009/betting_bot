"""Letture condivise del ledger per gli AGENTI (nessuna dipendenza fra agenti).

Direttiva 15/10/2026: "ogni agente lavora separatamente dagli altri, si passa
solo dal Chief". Prima di questo modulo l'Agente Analisi importava
`agents.data_agent._match_rows` — cioe' una dipendenza diretta fra due agenti
fratelli, che scavalca il Capo e rende l'uno ostaggio dell'altro. Il codice e'
lo stesso, spostato in un modulo NEUTRO che entrambi leggono: la regola resta
"nessun agente parla a un altro agente", e i nomi delle squadre restano
disponibili dove servono (l'Intel del Data Agent e la Steam Velocity
dell'Agente Analisi).

Sola LETTURA, import PIGRO di `tracker` (importare `agents` deve restare
leggero: vedi i tripwire in sottoprocesso), fail-safe: una lettura fallita non
e' un errore, quel match resta senza riga.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

__all__ = ["match_rows"]


def match_rows(conn, match_ids) -> dict[str, dict]:
    """Righe `matches` per i match dati (LETTURA, mai scritture).

    Serve per i NOMI delle squadre (il `Signal` non li porta): l'intel e la
    valutazione della steam velocity sono costruite su nomi reali, mai su
    match_id opachi. Ritorna un dict `match_id -> {match_id, league, home,
    away}`; un match senza riga (o una lettura fallita) NON e' un errore: quel
    match resta semplicemente senza contesto.

    `conn` iniettabile (i test usano un SQLite temporaneo); con `conn=None` si
    apre la connessione di produzione, con import pigro.
    """
    if not match_ids:
        return {}
    if conn is None:
        from tracker import _get_conn  # pigro: `import agents` resta leggero
        conn = _get_conn()
    try:
        ids = tuple(match_ids)
        placeholders = ",".join("?" for _ in ids)
        cursor = conn.execute(
            f"SELECT * FROM matches WHERE id IN ({placeholders})", ids)
        cols = [str(d[0]) for d in (cursor.description or [])]
        out: dict[str, dict] = {}
        for rec in cursor.fetchall():
            row = dict(zip(cols, rec))
            out[row.get("id") or ""] = {
                "match_id": row.get("id") or "",
                "league": row.get("league") or "",
                "home": row.get("home_team") or row.get("home") or "",
                "away": row.get("away_team") or row.get("away") or "",
            }
        return out
    except Exception as exc:
        logger.debug("ledger_read: righe matches non leggibili: %s", exc)
        return {}
