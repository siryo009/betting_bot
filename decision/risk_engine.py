"""decision/risk_engine.py — Il blocco piu' importante: APPROVE / REVIEW / REJECT.

Tre uscite, non due:

    approve  tutto in regola -> lo Stake Engine puo' dimensionare
    review   nessun gate violato ma confidenza/qualita' basse -> coda umana
    reject   un gate violato -> motivo machine-readable, nessuno stake

Il verdetto `review` e' la novita' rispetto a oggi (dove il binario era
value/strong_value vs rejected): un segnale che passa i gate ma non convince
non diventa automaticamente una puntata, va in coda per l'approvazione umana
(semantica scelta il 14/09/2026).

**Parita' con la produzione.** I gate qui sotto sono gli stessi di
`value_filter.is_sane` e nello stesso ordine, con le soglie lette da
`RiskLimits` (che a sua volta le legge dai moduli reali): `test_decision_pipeline`
verifica la parita' su una griglia di casi, cosi' il nuovo percorso non puo'
divergere dal vecchio senza che un test lo segnali. Le uniche differenze, decise
e documentate, sono:

  - l'incoerenza di mercato (`inv_sum` fuori 0.98-1.08) e' un REJECT qui
    (`MARKET_INCOHERENT`): in produzione e' il filtro di `sx_signals.scan`;
  - la liquidita' *relativa allo stake* non si controlla qui (serve lo stake):
    sta nello Stake Engine, dove il numero esiste;
  - la **qualita' dei dati** e' un gate qui: copertura del modello sotto
    `min_model_coverage` -> `review` (`DATA_QUALITY_LOW`). In produzione un
    modello cieco (nessun rating, profilo neutro di lega) puo' arrivare a un
    ordine automatico; nella catena no: decide un umano.
"""

from __future__ import annotations

from typing import Optional

from .limits import RiskLimits
from .models import (
    KillSwitchStatus, ReasonCode, RiskDecision, Signal, as_float, risk_approve,
    risk_reject, risk_review,
)

#: Ordine dei controlli (uguale a `is_sane`, piu' i due aggiunti).
CHECKS = (
    "kill_switch", "league", "market_coherence", "odds_range", "favourite",
    "ev", "edge", "data_quality", "confidence",
)


def evaluate(signal: Signal, *, kills: KillSwitchStatus, limits: RiskLimits,
             already_exposed: bool = False) -> RiskDecision:
    """Applica i gate in ordine e restituisce il verdetto."""
    checked: list[str] = []

    # 1. Kill switch: autorita' superiore a qualunque valore del modello.
    checked.append("kill_switch")
    block = kills.first_block()
    if block is not None:
        detail = ("modalita' 'off' (kill-switch o fail-closed)" if block is ReasonCode.KILL_SWITCH_OFF
                  else f"stop-loss giornaliero attivo: {kills.daily_stop_detail}".strip())
        return risk_reject(block, detail, checked=checked)
    if already_exposed:
        checked.append("exposure")
        return risk_reject(ReasonCode.ALREADY_EXPOSED,
                           "esito gia' in portafoglio per questo match", checked=checked)

    advisories = [ReasonCode.SETTLEMENT_PAUSED] if kills.settlement_paused else []

    # 2. Lega ammessa (strategia "solo campionati vincenti"). Lega vuota =
    #    fallback severo, come in `is_sane`.
    checked.append("league")
    if signal.league:
        import value_filter as vf
        if not vf.league_allowed(signal.league):
            return risk_reject(ReasonCode.LEAGUE_NOT_ALLOWED,
                               f"lega '{signal.league}' esclusa per ROI negativo",
                               checked=checked)

    # 3. Coerenza del mercato (inv_sum fuori banda = book sporco).
    checked.append("market_coherence")
    if not signal.data_quality.market_coherent:
        inv = signal.data_quality.inv_sum
        detail = "mercato incoerente" + (f" (inv_sum {inv:.3f} fuori 0.98-1.08)" if inv else "")
        return risk_reject(ReasonCode.MARKET_INCOHERENT, detail, checked=checked)

    # 4. Fascia quote dei favoriti netti.
    #    La quota e' `Decimal` a riposo (direttiva 29/09) mentre le soglie sono
    #    float: la conversione avviene QUI, una volta. Confrontare il `Decimal`
    #    con la soglia grezza sarebbe SBAGLIATO proprio al confine —
    #    `Decimal("1.3") >= 1.30` e' FALSO, perche' il float 1.30 vale
    #    1.30000000000000004: un segnale esattamente a 1.30 verrebbe rifiutato
    #    per un bit. Tripwire: `test_money_decimal` (confine 1.30).
    checked.append("odds_range")
    price = as_float(signal.price)
    if price < limits.odds_min:
        return risk_reject(ReasonCode.ODDS_TOO_LOW,
                           f"quota {price:.2f} < {limits.odds_min}", checked=checked)
    if price > limits.odds_max:
        return risk_reject(ReasonCode.ODDS_TOO_HIGH,
                           f"quota {price:.2f} > {limits.odds_max}", checked=checked)

    # 5. Deve essere il favorito di mercato (difesa in profondita').
    checked.append("favourite")
    if limits.favourites_only and signal.market_prob < limits.min_favourite_prob:
        return risk_reject(ReasonCode.NOT_FAVOURITE,
                           f"prob. di mercato {signal.market_prob*100:.1f}% < "
                           f"{limits.min_favourite_prob*100:.0f}%", checked=checked)

    # 6. EV (sotto = no edge, sopra la fascia = anomalia/quota sporca).
    checked.append("ev")
    ev = float(signal.ev or 0.0)
    if ev < limits.ev_min:
        return risk_reject(ReasonCode.EV_TOO_LOW,
                           f"EV {ev*100:.1f}% < {limits.ev_min*100:.0f}%", checked=checked)
    if ev > limits.ev_max:
        return risk_reject(ReasonCode.EV_ANOMALOUS,
                           f"EV {ev*100:.1f}% > {limits.ev_max*100:.0f}% (anomalia)",
                           checked=checked)

    # 7. Edge sul mercato devigato, soglia differenziata per lega.
    checked.append("edge")
    edge = float(signal.edge or 0.0)
    min_edge = limits.league_min_edge(signal.league)
    if edge < min_edge:
        return risk_reject(ReasonCode.EDGE_TOO_LOW,
                           f"edge {edge*100:.1f}pp < {min_edge*100:.1f}pp", checked=checked)

    # 8. QUALITA' DEI DATI: un modello senza rating (copertura sotto soglia) usa
    #    il profilo neutro di lega. Non e' un valore da giocare alla cieca: la
    #    direttiva del proprietario mette la qualita' dei dati fra i controlli
    #    del Risk Engine, quindi qui si va in coda umana — NON e' un `reject`.
    checked.append("data_quality")
    coverage = float(signal.data_quality.model_coverage)
    if coverage < limits.min_model_coverage:
        detail = (f"copertura modello {coverage:.2f} < {limits.min_model_coverage:.2f}: "
                  f"nessun gate violato, ma il modello e' cieco su questo match")
        return risk_review(ReasonCode.DATA_QUALITY_LOW, detail, checked=checked)

    # 9. Nessun gate violato: la confidenza decide fra pieno e coda umana.
    checked.append("confidence")
    if limits.review_enabled and signal.confidence < limits.review_confidence_min:
        detail = (f"confidenza {signal.confidence:.2f} < "
                  f"{limits.review_confidence_min:.2f}: nessun gate violato, serve un umano")
        return risk_review(ReasonCode.CONFIDENCE_LOW, detail, checked=checked)

    return risk_approve(checked=checked, advisories=advisories)


def tighten(limits: RiskLimits, tier: str, requested: Optional[float]) -> dict[str, float]:
    """Cap piu' stretto richiesto a monte — MAI piu' largo (regola 2).

    Il Risk Engine puo' solo ridurre: un `requested` sopra il cap del tier viene
    ignorato (documentato in `tightened` con il valore effettivo).
    """
    if requested is None:
        return {}
    try:
        value = float(requested)
    except (TypeError, ValueError):
        return {}
    ceiling = limits.cap_for(tier)
    if value <= 0 or value >= ceiling:
        return {}
    return {"max_stake_pct": value}


__all__ = ["CHECKS", "evaluate", "tighten"]
