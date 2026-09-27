"""Data Agent — ingestione mercato e segnali (facciata, zero logica nuova).

Sub-agenti delegati (esistenti, gia' testati):
- `decision.adapters.iter_signals`: ledger -> `Signal` Pydantic (finestra
  mobile 24h, come la corsia `auto_bet`);
- `decision.feeds.MarketFeed` + `verify_feed`: feed SX con refresh forzato e
  gate fail-closed (il presupposto del ciclo: senza mercato validato non si
  decide).

`process()` ritorna `MarketData`: il Capo legge `validated` per decidere se
il ciclo puo' proseguire.
"""

from __future__ import annotations

from typing import Optional

from decision.adapters import iter_signals
from decision.feeds import FeedGateResult, MarketFeed, verify_feed
from decision.models import ReasonCode, Signal

from .contracts import MarketData


def _pass_gate() -> FeedGateResult:
    """Gate "non richiesto" (feed non iniettato e disattivato da env): passa.

    E' la stessa semantica di `verify_feed(required=False)`: il ciclo valuta
    senza dati di mercato — scelta esplicita, tipica di test/diagnostica.
    """
    return FeedGateResult(allowed=True, reason=ReasonCode.OK,
                          detail="feed non attivo (off)")


class DataAgent:
    """Ingestione: feed di mercato + segnali aperti. Nessun effetto collaterale
    oltre al refresh del feed (lettura pubblica SX, zero crediti)."""

    name = "data"

    def __init__(self, feed: Optional[MarketFeed] = None, *, hours: float = 24.0) -> None:
        self.feed = feed
        self.hours = float(hours)

    def process(self, *, conn=None, now=None) -> MarketData:
        if self.feed is not None:
            snapshot = self.feed.refresh()
            gate = verify_feed(snapshot)
        else:
            snapshot = None
            gate = _pass_gate()
        signals: list[Signal] = iter_signals(conn=conn, now=now, hours=self.hours)
        return MarketData(snapshot=snapshot, gate=gate, signals=signals)
