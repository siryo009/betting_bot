"""Data Agent — ingestione mercato, segnali e intel live (facciata sottile).

Sub-agenti delegati (esistenti, gia' testati):
- `decision.adapters.iter_signals`: ledger -> `Signal` Pydantic (finestra
  mobile 24h, come la corsia `auto_bet`);
- `decision.feeds.MarketFeed` + `verify_feed`: feed SX con refresh forzato e
  gate fail-closed (il presupposto del ciclo: senza mercato validato non si
  decide);
- `live_intel.assemble_match_intel` (direttiva 29/09/2026): intel a costo
  zero per ogni match in finestra — statistiche di stagione (xG, soccerdata),
  ELO (ClubElo), news infortuni/formazioni (ddgs), probabili lanciatori MLB
  e statistiche NBA (nba_api). L'assembler e' iniettabile e l'intera raccolta
  e' fail-safe: l'intel NON e' un gate — un provider offline degrada e si
  dichiara (`providers[].detail`, `errors`), non blocca (disattivabile con
  `LIVE_INTEL=0`).

`process()` ritorna `MarketData`: il Capo legge `validated` per decidere se
il ciclo puo' proseguire.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from decision.adapters import iter_signals
from decision.feeds import FeedGateResult, MarketFeed, verify_feed
from decision.models import ReasonCode, Signal

from .contracts import MarketData

logger = logging.getLogger(__name__)

__all__ = ["DataAgent", "_pass_gate", "_match_rows"]


def _pass_gate() -> FeedGateResult:
    """Gate "non richiesto" (feed non iniettato e disattivato da env): passa.

    E' la stessa semantica di `verify_feed(required=False)`: il ciclo valuta
    senza dati di mercato — scelta esplicita, tipica di test/diagnostica.
    """
    return FeedGateResult(allowed=True, reason=ReasonCode.OK,
                          detail="feed non attivo (off)")


def _match_rows(conn, match_ids: set[str]) -> dict[str, dict]:
    """Righe `matches` per i match dei segnali (LETTURA, mai scritture).

    Serve per i NOMI delle squadre (il `Signal` non li porta): l'intel e'
    costruita su nomi reali, mai su match_id opachi. Ritorna un dict
    `match_id -> {match_id, league, home, away}`; un match senza riga (o una
    lettura fallita) NON e' un errore: quel match resta senza intel.
    """
    if not match_ids:
        return {}
    if conn is None:
        from tracker import _get_conn  # pigro: `import agents` resta leggero
        conn = _get_conn()
    try:
        placeholders = ",".join("?" for _ in match_ids)
        cursor = conn.execute(
            f"SELECT * FROM matches WHERE id IN ({placeholders})",
            tuple(match_ids))
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
        logger.debug("data: righe matches non leggibili: %s", exc)
        return {}


class DataAgent:
    """Ingestione: feed di mercato + segnali aperti + intel live.

    Nessun effetto collaterale oltre al refresh del feed (lettura pubblica
    SX, zero crediti) e alle letture cache/disco dell'intel (`live_intel`).
    """

    name = "data"

    def __init__(self, feed: Optional[MarketFeed] = None, *, hours: float = 24.0,
                 intel_fn=None) -> None:
        self.feed = feed
        self.hours = float(hours)
        # INTEL LIVE (29/09): assembler iniettabile (test offline senza rete);
        # default `live_intel.assemble_match_intel` caricato PIGRO al primo uso.
        self.intel_fn = intel_fn

    def process(self, *, conn=None, now=None) -> MarketData:
        if self.feed is not None:
            snapshot = self.feed.refresh()
            gate = verify_feed(snapshot)
        else:
            snapshot = None
            gate = _pass_gate()
        signals: list[Signal] = iter_signals(conn=conn, now=now, hours=self.hours)
        intel = self._collect_intel(signals, conn=conn)
        return MarketData(snapshot=snapshot, gate=gate, signals=signals,
                          intel=intel)

    def _collect_intel(self, signals: list[Signal], *, conn=None) -> list[dict]:
        """Intel per i match dei segnali (una voce per match, mai per segnale).

        Fail-safe TOTALE: qualunque errore ritorna [] e logga — l'ingestione
        (feed + segnali) non si rompe mai per l'intel.
        """
        if os.getenv("LIVE_INTEL", "1").strip().lower() in ("0", "false", "no", "off"):
            return []
        if not signals:
            return []
        try:
            if self.intel_fn is not None:
                intel_fn = self.intel_fn
            else:
                from live_intel import assemble_match_intel  # pigro
                intel_fn = assemble_match_intel
            rows = _match_rows(conn, {s.match_id for s in signals})
            out: list[dict] = []
            seen: set[str] = set()
            for s in signals:
                if s.match_id in seen:
                    continue  # piu' segnali sullo stesso match: una intel sola
                seen.add(s.match_id)
                row = rows.get(s.match_id)
                if row is None:
                    logger.debug("data: intel saltata su %s (nessuna riga "
                                 "matches: nomi squadra assenti)", s.match_id)
                    continue
                try:
                    out.append(intel_fn(row).as_json())
                except Exception as exc:
                    logger.debug("data: intel fallita su %s: %s", s.match_id, exc)
            if out:
                errs = sum(int(e.get("errors") or 0) for e in out)
                logger.info("data: intel live su %d match%s", len(out),
                            f" ({errs} provider in errore)" if errs else "")
            return out
        except Exception as exc:
            logger.warning("data: raccolta intel saltata (%s)", exc)
            return []
