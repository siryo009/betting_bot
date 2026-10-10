"""Execution Agent — esecuzione dei piani approvati (Fase 1: SOLO shadow).

Sub-agenti delegati (esistenti, gia' testati):
- `decision.dispatcher.Dispatcher`: instrada ogni comando al primo gateway
  che lo gestisce, apre span, aggrega `DispatchReport`;
- `decision.gateways.ShadowGateway`: REGISTRA i comandi e non esegue nulla
  (dedup per `dedup_key` sul registro JSONL) — e' il set di default della
  Fase 1;
- `decision.gateways.ValidatingLedgerGateway`: opt-in (`persist=True`) —
  scrive la riga sul ledger `decisions` con stato `pending` e la convalida
  (prima persistere, poi convalidare); l'ordine resta registrato, mai
  eseguito.

**Il denaro resta fuori**: nessun gateway reale (quello che chiamerebbe
`auto_bet._live_fill`) e' montato in Fase 1 — un tripwire lo pretende
(`test_agent_hierarchy.py`). Il cutover e' la Fase 3, decisa sui numeri di
`decision_compare`.

**Stato degli ORDINI APERTI (direttiva 28/09/2026)**: l'Execution Engine e'
l'unico che sa cosa e' stato eseguito, quindi e' anche la fonte dello stato
del capitale immobilizzato. `open_exposure()` legge gli ordini reali ancora
in corso (`mode='live'`, non saldati) e dice se un NUOVO stake entra nel
tetto del 40%: e' la lettura che l'Advisor interroga a ogni ciclo.

**Ordini RESTING (GTC) su SX Bet (direttiva 10/10/2026)**: la stessa ragione
vale per il capitale PARCHEGGIATO sul book. `open_resting()` legge il registro
degli ordini RESTING (delega a `resting_orders.summary`) e lo espone al Capo:
l'ordine parcheggiato non e' ancora una puntata — nessuna riga sul ledger,
nessun `placed` nel giro — ma impegna capitale quando si riempie. Chi sa cosa
c'e' sul book e' l'esecuzione, non la strategia.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from decision.commands import CommandPlan
from decision.dispatcher import Dispatcher
from decision.gateways import ShadowGateway
from decision.middleware import Observability

from .contracts import ExecutionOutput


#: Tipo del lettore iniettabile: (bankroll, nuovo_stake) -> stato del recinto.
ExposureReader = Callable[[float, float], dict]


def default_exposure_reader() -> ExposureReader:
    """Lettore REALE dello stato degli ordini aperti (import pigro).

    Delega a `auto_bet.exposure_allows`: il tetto del 40% e la proiezione
    (aperto + nuovo stake) vivono in UN posto solo — due copie di una soglia
    di denaro divergono, e la prima che diverge e' quella che spende.

    L'import e' dentro la funzione (non a livello di modulo) perche'
    `import agents` deve restare leggero: `auto_bet` trascina il ledger e il
    percorso d'ordine, che il ciclo di valutazione non deve caricare.
    """
    import auto_bet
    return auto_bet.exposure_allows


def _exposure_unavailable(exc: Exception, bankroll: float) -> dict:
    """Stato FAIL-CLOSED: senza lettura non si autorizza nessun ordine."""
    return {
        "allowed": False,
        "open_stake": float("inf"),
        "count": -1,
        "cap": 0.0,
        "bankroll": float(bankroll or 0.0),
        "new_stake": 0.0,
        "projected": float("inf"),
        "blocked": True,
        "reason": ("stato degli ordini aperti non leggibile: nessun nuovo "
                   "ordine (fail-closed) — %s" % exc),
    }


class ExecutionAgent:
    """Dispatch dei piani approvati. In Fase 1 i gateway sono solo shadow."""

    name = "execution"

    def __init__(self, *, shadow_path: Optional[str | Path] = None,
                 persist: bool = False,
                 observability: Optional[Observability] = None,
                 exposure_reader: Optional[ExposureReader] = None) -> None:
        self.shadow_path = shadow_path
        self.persist = bool(persist)
        self.observability = observability or Observability()
        #: Lettore dello stato degli ordini aperti (None = lettura di
        #: produzione, risolta a ogni chiamata: cosi' l'import resta pigro).
        self.exposure_reader = exposure_reader

    def _gateways(self) -> list:
        gateways: list = []
        if self.persist:
            # Opt-in: persistenza + validazione sul ledger `decisions`
            # (l'ordine resta registrato, mai eseguito — vedi shadow.py).
            from decision.gateways import ValidatingLedgerGateway
            gateways.append(ValidatingLedgerGateway())
        gateways.append(ShadowGateway(self.shadow_path))
        return gateways

    # ------------------------------------------------------------------
    # Stato degli ORDINI APERTI (capitale immobilizzato)
    # ------------------------------------------------------------------
    def open_exposure(self, bankroll: float, new_stake: float = 0.0) -> dict:
        """Stato degli ordini aperti + verifica per un NUOVO stake.

        Ritorna {open_stake, count, cap, bankroll, projected, allowed,
        blocked, reason}. `new_stake=0` da' il solo stato corrente.

        Mai un'eccezione verso il chiamante: un lettore rotto o assente
        restituisce lo stato FAIL-CLOSED (`allowed=False`) — meglio respingere
        un piano che autorizzarne uno su un recinto di cui non si sa nulla.
        """
        reader = self.exposure_reader
        if reader is None:
            try:
                reader = default_exposure_reader()
            except Exception as exc:  # auto_bet non importabile
                return _exposure_unavailable(exc, bankroll)
        try:
            return dict(reader(bankroll, new_stake))
        except Exception as exc:  # lettura del ledger esplosa
            return _exposure_unavailable(exc, bankroll)

    # ------------------------------------------------------------------
    # Stato degli ORDINI RESTING (GTC) — capitale parcheggiato sul book
    # ------------------------------------------------------------------
    def open_resting(self, *, reader=None) -> dict:
        """Stato degli ordini RESTING (GTC) su SX Bet (direttiva 10/10/2026).

        Perche' lo legge l'Execution Engine: e' l'unico componente che sa cosa
        e' VIVO sull'exchange. Un ordine parcheggiato NON e' ancora una puntata
        (nessuna riga `bets`, nessun `placed` nel giro) ma e' capitale che si
        impegna AL RIEMPIMENTO: il Capo deve vederlo, altrimenti la differenza
        fra "non ho trovato niente" e "ho messo il prezzo e aspetto" sparisce.

        Delega a `resting_orders.summary()` con import PIGRO (`import agents`
        resta leggero) e non solleva MAI: un registro rotto torna come stato
        vuoto DICHIARATO (`unavailable=True` + motivo), non come un'eccezione
        dentro il ciclo del denaro.
        """
        if reader is None:
            try:
                import resting_orders as ro
                reader = ro.summary
            except Exception as exc:
                return {"unavailable": True,
                        "reason": f"resting_orders non disponibile: {exc}"}
        try:
            state = dict(reader())
        except Exception as exc:
            return {"unavailable": True,
                    "reason": f"lettura registro fallita: {exc}"}
        state["unavailable"] = False
        return state

    def process(self, plans: list[CommandPlan]) -> ExecutionOutput:
        out = ExecutionOutput(shadow=True)
        if not plans:
            return out
        gateways = self._gateways()
        dispatcher = Dispatcher(gateways, observability=self.observability)
        for plan in plans:
            report = dispatcher.dispatch(plan)
            out.reports.append(report)
            out.executed += int(report.executed)
        # `shadow` del dispatcher e' True solo se TUTTI i gateway non eseguono:
        # con il ValidatingLedgerGateway (audit) resta True per costruzione,
        # ma lo ricalcoliamo dal primo report reale.
        if out.reports:
            out.shadow = bool(out.reports[0].shadow)
        return out
