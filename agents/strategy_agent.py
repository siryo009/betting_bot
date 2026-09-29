"""Strategy Agent — classificazione e arricchimento dei segnali (facciata).

Sub-agenti delegati (esistenti, gia' testati):
- `value_filter.PLAYABLE_TIERS` / `get_signal_tier`: la classificazione
  value/strong_value/moderate (le soglie restano UNA nel modulo originale);
- l'arricchimento numerico (prob modello/blend, edge, EV, confidenza,
  copertura ratings) avviene GIA' in `decision.adapters.signal_from_row`
  quando il Data Agent estrae i segnali: qui NON si ricalcola nulla, perche'
  una formula copiata e' una formula che diverge (lezione 13/09).
- `poisson_engine` (direttiva 29/09/2026): il modello dei mercati NON ancora
  nel ledger. `model_probabilities()` DELEGA — i lambda vengono da
  `expected_goals` (rating time-decay) e le probabilita' dalle funzioni del
  motore, Dixon-Coles incluso. **Qui non si importa numpy ne' scipy e non si
  scrive una sola formula**: se il modello vive in due posti, il giorno che
  divergono non si sa quale dei due ha deciso la puntata.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from decision.models import Signal
from value_filter import PLAYABLE_TIERS

from .contracts import StrategyOutput

#: Mercati che il MODELLO sa prezzare (la chiave e' quella del ledger).
MODEL_MARKETS = ("1X2", "OU", "AH", "BTTS")


class StrategyAgent:
    """Filtra i segnali per tier giocabile, dichiara i contatori e interroga
    il modello per i mercati non ancora nel ledger (delega a `poisson_engine`)."""

    name = "strategy"

    def __init__(self, *, playable_tiers: Optional[Iterable[str]] = None) -> None:
        #: Iniettabile per test; default = la tripla di produzione (UNA fonte).
        self.playable_tiers = tuple(playable_tiers) if playable_tiers else tuple(PLAYABLE_TIERS)

    def process(self, signals: list[Signal]) -> StrategyOutput:
        playable: list[Signal] = []
        rejected = 0
        unclassified = 0
        for signal in signals:
            tier = str(getattr(signal, "tier", "") or "")
            if tier in self.playable_tiers:
                playable.append(signal)
            elif tier == "rejected":
                rejected += 1
            else:
                # Stato ignoto: mai fatto sparire dai conti (lezione 22/09).
                unclassified += 1
        return StrategyOutput(signals=playable, playable=len(playable),
                              rejected=rejected, unclassified=unclassified)

    # ------------------------------------------------------------------
    # MODELLO (delega: nessuna formula qui)
    # ------------------------------------------------------------------
    def expected_goals(self, home: str, away: str) -> tuple[float, float]:
        """Lambda del modello — delegati a `poisson_engine.expected_goals`.

        Import PIGRO: `import agents` resta leggero finche' il modello non
        serve davvero (stesso criterio di `live_intel` nel Data Agent).
        """
        from poisson_engine import expected_goals
        return expected_goals(home, away)

    def model_probabilities(self, market: str, home: str, away: str, *,
                            line: Optional[float] = None,
                            side: str = "over") -> dict[str, Any]:
        """Probabilita' del MODELLO per un mercato (delega, zero formule).

        `1X2` -> `{p1, pX, p2}`; `OU` -> `{p_win, p_push, p_lose}` sulla
        `line` (il push e' esplicito: linee intere e quarter); `AH` -> idem
        sul lato; `BTTS` -> `{p_yes, p_no}`.

        Un mercato senza modello e' un ERRORE DICHIARATO, mai una
        probabilita' inventata (fail-closed): la strategia non puo' nascere
        da un numero che nessuno ha calcolato.

        Nessun import di numpy/scipy qui: la vettorizzazione vive nel motore.
        """
        key = str(market or "").strip().upper()
        if key not in MODEL_MARKETS:
            raise ValueError(
                f"mercato '{market}' senza modello (attesi: "
                f"{', '.join(MODEL_MARKETS)})")
        if key in ("OU", "AH") and line is None:
            raise ValueError(f"mercato {key} senza linea: nessun prezzo")

        from poisson_engine import (ah_outcome_probs, expected_goals,
                                    ou_outcome_probs, prob_1x2, prob_btts)
        lam_h, lam_a = expected_goals(home, away)

        if key == "1X2":
            p1, px, p2 = prob_1x2(lam_h, lam_a)
            return {"p1": p1, "pX": px, "p2": p2}
        if key == "OU":
            p_win, p_push, p_lose = ou_outcome_probs(lam_h, lam_a, float(line),
                                                     side)
            return {"p_win": p_win, "p_push": p_push, "p_lose": p_lose}
        if key == "AH":
            p_win, p_push, p_lose = ah_outcome_probs(lam_h, lam_a, float(line),
                                                     side)
            return {"p_win": p_win, "p_push": p_push, "p_lose": p_lose}
        p_yes = prob_btts(lam_h, lam_a)
        return {"p_yes": p_yes, "p_no": 1.0 - p_yes}
