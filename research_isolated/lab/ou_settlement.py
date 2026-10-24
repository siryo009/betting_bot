"""P/L di riferimento di un Over/Under **con la quarter line vera**.

Perche' esiste (audit LIVE del 20/10/2026): il settlement di produzione
(`tracker.ou_won`) ricava la linea dal testo dell'esito e la confronta col
totale gol come **soglia secca**. Su una linea ``.25``/``.75`` questo e'
sbagliato: la quota e' divisa su due mezze linee e, quando il totale cade
esattamente sulla mezza INTERA, una meta' e' push e l'altra vince/perde.

Esempio reale (Over 2.75, 3 gol a quota 2.00):

    mezza a 2.5 -> vinta   (+0.5 x (2.00-1) = +0.50)
    mezza a 3.0 -> push    (0.00)
    totale P/L            = +0.50

La produzione confronta ``3 > 2.75`` e paga ``+1.00``: **il doppio**.
Lo stesso errore, simmetrico, sull'Under (``-1.00`` invece di ``-0.50``).

Questo modulo e' la **specifica eseguibile** della regola corretta; i test la
usano come riferimento contro il comportamento di produzione (che oggi e'
marcato ``xfail`` e resta aperto, perche' l'audit non modifica la produzione).

Copia la logica di `tracker.ah_pnl_units` (split-bet AH) — e' l'unico posto
del progetto dove la regola delle quarter line e' gia' implementata bene.
"""

from __future__ import annotations

import math
from typing import Tuple


def halves_of(line: float) -> Tuple[Tuple[float, ...], float]:
    """Mezze linee e quota di ciascuna (1 per linee intere/mezze, 0.5 per quarter)."""
    line = float(line)
    if abs(line * 2) % 1 != 0:              # .25 / .75 -> due mezze puntate
        lo = math.floor(line * 2) / 2.0
        return (lo, lo + 0.5), 0.5
    return (line,), 1.0


def ou_quarter_pnl(side: str, line: float, total: float, quota: float) -> float:
    """P/L (stake 1) di un Over/Under, con il push pesato su ogni mezza linea.

    ``side`` = ``"over"``/``"under"`` (case-insensitive), ``total`` = gol
    della partita, ``quota`` > 1.
    """
    s = str(side).strip().lower()
    if s not in ("over", "under"):
        raise ValueError(f"side non valido: {side!r}")
    halves, share = halves_of(line)
    pnl = 0.0
    for h in halves:
        if total > h:
            pnl += share * (quota - 1.0) if s == "over" else -share
        elif total < h:
            pnl += -share if s == "over" else share * (quota - 1.0)
        # totale == h: push, P/L 0 su quella mezza
    return pnl


def ou_outcome(side: str, line: float, total: float) -> str:
    """Verdetto coerente col P/L: won / lost / push (push solo se P/L == 0)."""
    pnl = ou_quarter_pnl(side, line, total, 2.0)
    if pnl > 0:
        return "won"
    if pnl < 0:
        return "lost"
    return "push"
