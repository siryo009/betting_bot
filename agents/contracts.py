"""Contratti d'uscita degli agenti (Pydantic, serializzabili per n8n/HTTP).

Ogni `process()` di un agente ritorna uno di questi modelli: il Capo non
consuma mai dict anonimi. Sono INVOLUCRI: il contenuto di valore (`Signal`,
`CommandPlan`, `FeedGateResult`, `DispatchReport`) e' quello di
`decision/models` e `decision/commands` — qui si aggiunge solo l'identita'
dell'agente che ha prodotto il risultato e i contatori del ciclo.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from decision.commands import CommandPlan
from decision.dispatcher import DispatchReport
from decision.feeds import FeedGateResult, FeedSnapshot
from decision.models import Signal

#: Eta' massima di un'osservazione dell'oracolo perche' il segnale sia
#: FRESCO (env `ANALYSIS_MAX_AGE_S`). Una quota letta troppo tempo fa non
#: descrive piu' il mercato: il segnale resta valido come DATO, ma il campo
#: `fresh` dice che non e' giocabile senza una nuova lettura.
DEFAULT_MAX_AGE_S = 180.0
#: Tolleranza sull'orologio: un timestamp nel futuro entro questo margine e'
#: accettato (skew fra container e fonte); oltre e' un dato ROTTO.
FUTURE_TOLERANCE_S = 300.0


def _parse_ts(value: Any) -> Optional[datetime]:
    """Timestamp ISO -> datetime UTC aware (None se non interpretabile).

    Una data NAIVE viene rifiutata: su un mercato un istante senza fuso e'
    ambiguo (lezione del 17/09 sulla refertazione).
    """
    if isinstance(value, datetime):
        ts = value
    elif isinstance(value, str):
        try:
            ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if ts.tzinfo is None:
        return None
    return ts.astimezone(timezone.utc)


class OracleSignal(BaseModel):
    """Uscita dell'Agente ANALISI: il segnale con la sua qualita' di mercato.

    Tre informazioni che il solo prezzo non porta:
    - **Steam Velocity**: il GRADIENTE della quota sharp (`move_pct /
      span_minutes`, %/min) negli ultimi minuti — non la fotografia a T-60;
    - **Juice**: l'overround implicito dello sharp e la sua VARIAZIONE: un
      allargamento improvviso dell'aggio segnala instabilita' o informazioni
      di spogliatoio (`juice_anomaly`);
    - **Freshness**: quanto e' vecchia l'osservazione (`age_s`) e se e' ancora
      utilizzabile (`fresh`).

    Il modello NON decide: descrive. Le soglie di gioco stanno nel Cervello.
    """

    signal_id: str
    match_id: str
    market: str = "1X2"
    esito: str
    price: float = Field(gt=1.0)
    true_prob: Optional[float] = Field(default=None, gt=0.0, le=1.0)
    ev: Optional[float] = None
    #: Edge del modello sul mercato (prob. modello - prob. devigata): la
    #: Finanza lo usa per il k DINAMICO (04/10/2026), non per i gate.
    edge: Optional[float] = None
    league: str = ""
    #: Nomi squadra (dalla riga `matches`, mai da match_id opachi): la corsia
    #: d'ordine li usa per risolvere il mercato exchange.
    home: str = ""
    away: str = ""
    #: Kickoff UTC (aware): la finestra T-60 e' un vincolo di denaro.
    kickoff: Optional[datetime] = None
    # --- steam velocity ---
    steam_move: bool = False
    move_pct: Optional[float] = None          # ΔQ nella finestra (%, negativo = crollo)
    span_minutes: Optional[float] = None      # intervallo REALE di osservazione
    velocity_pct_min: float = 0.0             # ΔQ/Δt normalizzato (%/min)
    steam_reason: str = ""
    # --- juice (overround dello sharp) ---
    juice: Optional[float] = None
    juice_delta: Optional[float] = None       # variazione vs lettura precedente
    juice_anomaly: bool = False
    juice_reason: str = ""
    # --- provnienza / freschezza ---
    observed_at: datetime
    age_s: Optional[float] = None
    fresh: bool = True
    max_age_s: float = DEFAULT_MAX_AGE_S
    sources: list[str] = Field(default_factory=list)
    depth_usdc: Optional[float] = None
    notes: str = ""
    # --- de-vig (direttiva 04/10/2026) ---
    #: Metodo di rimozione dell'aggio usato dall'oracolo per ricavare la
    #: probabilita' REALE ("fair"): `shin` e' il default di progetto (corregge
    #: il favourite-longshot bias), gli altri sono `power`/`multiplicative`.
    #: L'EV non si calcola MAI sulle quote grezze: `fair_odds` e' la quota
    #: equa (senza vig) dello sharp per l'esito di questo segnale.
    devig_method: str = ""
    shin_z: Optional[float] = None
    fair_odds: Optional[float] = None

    @field_validator("observed_at", mode="before")
    @classmethod
    def _ts_aware(cls, value: Any) -> Any:
        ts = _parse_ts(value)
        if ts is None:
            raise ValueError("observed_at mancante, non ISO o senza fuso orario")
        if ts > datetime.now(timezone.utc) + timedelta(seconds=FUTURE_TOLERANCE_S):
            raise ValueError(f"observed_at nel futuro ({ts.isoformat()}): dato rotto")
        return ts

    @field_validator("market")
    @classmethod
    def _market_norm(cls, value: str) -> str:
        norm = str(value or "").strip().upper()
        if not norm:
            raise ValueError("market vuoto")
        return norm

    @field_validator("esito")
    @classmethod
    def _esito_norm(cls, value: str) -> str:
        norm = str(value or "").strip()
        if not norm:
            raise ValueError("esito vuoto")
        return norm

    @model_validator(mode="after")
    def _compute_freshness(self) -> "OracleSignal":
        """Deriva `age_s` e `fresh`: una sola definizione di "fresco"."""
        age = (datetime.now(timezone.utc) - self.observed_at).total_seconds()
        self.age_s = round(max(age, 0.0), 3)
        self.fresh = self.age_s <= float(self.max_age_s)
        return self

    def as_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class ValidatedTrade(BaseModel):
    """Payload Cervello -> Finanza: il segnale VALIDATO e pronto al sizing.

    Contiene la decisione del gate dinamico (`dynamic_ev_min`) e l'esito del
    Portfolio Shield (`shield_action`/`shield_factor`). Lo STAKE non e' ancora
    qui: lo calcola la Finanza col saldo reale letto al momento dell'ordine
    (e lo riscrive in questo stesso contratto, senza perdere un campo).
    """

    signal_id: str
    match_id: str
    market: str = "1X2"
    esito: str
    league: str = ""
    home: str = ""
    away: str = ""
    kickoff: Optional[datetime] = None
    price: float = Field(gt=1.0)
    true_prob: Optional[float] = Field(default=None, gt=0.0, le=1.0)
    ev: Optional[float] = None
    edge: Optional[float] = None
    depth_usdc: Optional[float] = None
    # --- de-vig (propagato dall'Analisi, direttiva 04/10/2026) ---
    devig_method: str = ""
    shin_z: Optional[float] = None
    fair_odds: Optional[float] = None
    # --- gate dinamico ---
    base_ev_min: float = 0.0
    dynamic_ev_min: float = 0.0
    ev_multiplier: float = 1.0
    dynamic_reason: str = ""
    # --- portfolio shield ---
    shield_action: str = "allow"        # allow | scale | block
    shield_factor: float = 1.0
    #: Spazio residuo del blocco correlato (USDC): la Finanza non puo' superarlo.
    #: None = nessun vincolo dal shield (nessuna esposizione aperta misurata).
    shield_max_usdc: Optional[float] = None
    shield_reason: str = ""
    # --- esito (riempito dalla Finanza) ---
    stake: float = 0.0
    kelly_fraction: float = 0.0
    kelly_full: float = 0.0
    raw_stake: float = 0.0
    cap_usdc: float = 0.0
    max_stake_pct: float = 0.0
    min_ticket: float = 0.0
    capped: bool = False
    bankroll: float = 0.0
    executable: bool = False
    reason: str = ""
    # --- provenienza ---
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    analysis: dict[str, Any] = Field(default_factory=dict)

    @field_validator("created_at", mode="before")
    @classmethod
    def _created_aware(cls, value: Any) -> Any:
        ts = _parse_ts(value)
        if ts is None:
            raise ValueError("created_at non interpretabile o senza fuso")
        return ts

    def as_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class AnalysisOutput(BaseModel):
    """Uscita dell'Agente ANALISI: i segnali arricchiti (uno per segnale)."""

    signals: list[OracleSignal] = Field(default_factory=list)
    steam_moves: int = 0
    juice_anomalies: int = 0
    stale: int = 0

    def as_json(self) -> dict[str, Any]:
        return {
            "signals": len(self.signals),
            "steam_moves": self.steam_moves,
            "juice_anomalies": self.juice_anomalies,
            "stale": self.stale,
            "detail": [s.as_json() for s in self.signals],
        }


class BrainOutput(BaseModel):
    """Uscita dell'Agente CERVELLO: i trade validati per la Finanza."""

    trades: list["ValidatedTrade"] = Field(default_factory=list)
    validated: int = 0
    rejected: int = 0
    scaled: int = 0
    blocked: int = 0

    def as_json(self) -> dict[str, Any]:
        return {
            "trades": len(self.trades),
            "validated": self.validated,
            "rejected": self.rejected,
            "scaled": self.scaled,
            "blocked": self.blocked,
        }


class SizingOutput(BaseModel):
    """Uscita del Finance Agent sul percorso ValidatedTrade (Cervello -> ordine).

    Contiene i trade con la size RIEMPITA dal motore Kelly aggressivo
    (`stake`, `executable`, ...). Non e' un `CommandPlan`: la catena agenti
    dimensiona il trade, poi lo passa alla corsia d'ordine di `auto_bet`
    (unico esecutore del denaro).
    """

    trades: list["ValidatedTrade"] = Field(default_factory=list)
    sized: int = 0
    executable: int = 0
    skipped: int = 0

    def as_json(self) -> dict[str, Any]:
        return {
            "trades": len(self.trades),
            "sized": self.sized,
            "executable": self.executable,
            "skipped": self.skipped,
        }


class MarketData(BaseModel):
    """Uscita del Data Agent: istantanea del feed + segnali aperti nel ledger."""

    snapshot: Optional[FeedSnapshot] = None
    gate: FeedGateResult
    signals: list[Signal] = Field(default_factory=list)
    #: Intel live per i segnali in finestra (direttiva 29/09/2026):
    #: statistiche di stagione, ELO, news infortuni, probabili lanciatori.
    #: Vuota (o con `errors > 0`) NON blocca: e' degrado dichiarato.
    intel: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def validated(self) -> bool:
        """Il gate di mercato e' passato (fail-closed: senza feed non si decide)."""
        return bool(self.gate.allowed)

    def as_json(self) -> dict[str, Any]:
        return {
            "gate": self.gate.as_json(),
            "signals": len(self.signals),
            "snapshot": (self.snapshot.model_dump(mode="json")
                         if self.snapshot is not None else None),
            "intel": self.intel,
        }


class StrategyOutput(BaseModel):
    """Uscita dello Strategy Agent: gli stessi Signal, classificati per tier.

    In Fase 1 l'arricchimento (prob model/blend, EV, edge, tier) avviene GIA'
    in `decision.adapters.signal_from_row` quando i segnali vengono estratti
    dal ledger; l'agente li filtra per tier giocabile e dichiara i contatori.
    """

    signals: list[Signal] = Field(default_factory=list)
    playable: int = 0
    rejected: int = 0
    unclassified: int = 0

    def as_json(self) -> dict[str, Any]:
        return {
            "total": len(self.signals),
            "playable": self.playable,
            "rejected": self.rejected,
            "unclassified": self.unclassified,
        }


class FinanceOutput(BaseModel):
    """Uscita del Finance Agent: un piano per segnale (approve/review/reject)."""

    plans: list[CommandPlan] = Field(default_factory=list)
    approved: int = 0
    review: int = 0
    rejected: int = 0

    def as_json(self) -> dict[str, Any]:
        return {
            "plans": len(self.plans),
            "approved": self.approved,
            "review": self.review,
            "rejected": self.rejected,
        }


class ExecutionOutput(BaseModel):
    """Uscita dell'Execution Agent: report del dispatcher (shadow in Fase 1)."""

    reports: list[DispatchReport] = Field(default_factory=list)
    executed: int = 0
    shadow: bool = True
    #: Ordini RESTING (GTC) su SX Bet (direttiva 10/10/2026): l'Execution
    #: Engine e' l'unico che sa cosa e' VIVO sul book. Un ordine parcheggiato
    #: NON e' una puntata (nessuna riga ledger, nessun `placed`), ma impegna
    #: capitale AL RIEMPIMENTO: va riportato nel ciclo, non taciuto.
    resting: dict[str, Any] = Field(default_factory=dict)

    def as_json(self) -> dict[str, Any]:
        return {
            "dispatched": len(self.reports),
            "executed": self.executed,
            "shadow": self.shadow,
            "resting_open": self.resting.get("open"),
            "resting_stake": self.resting.get("open_stake"),
        }


class AdvisorResolution(BaseModel):
    """Risposta dell'AdvisorAgent a un blocco. E' un CONSIGLIO, non un ordine.

    - `resolved=False`: il blocco resta (autorita' superiore, blocco
      strutturale, nessun indizio) — il Capo non fa nulla;
    - `override_approved=True` SOLO per il micro-stake (tutti i gate verdi,
      solo la size bloccava): il piano sostitutivo e' in `modified_plan` e
      segue lo STESSO percorso degli approvati (shadow in Fase 1);
    - `escalate_review=True`: proposta di SALIRE all'umano (coda revisioni
      Telegram), mai esecuzione automatica — e' la via per i falsi positivi
      di contesto e per il market switch.

    `exposure` porta lo stato del recinto di esposizione aperta quando la
    risoluzione lo riguarda (direttiva 28/09/2026): quali ordini sono aperti,
    quanto capitale immobilizzano e quanto ne resta per un nuovo piano.
    """

    resolved: bool = False
    override_approved: bool = False
    override_kind: str = ""          # reduced_stake | market_switch | context_review
    note: str = ""
    reason_no: str = ""             # perche' NON risolto (sempre dichiarato)
    modified_plan: Optional[Any] = None   # CommandPlan (import pigro per evitare cicli)
    escalate_review: bool = False
    context: dict[str, Any] = Field(default_factory=dict)
    original_reason: str = ""
    #: Stato del recinto di esposizione aperta (ordini in corso, tetto 40%).
    exposure: dict[str, Any] = Field(default_factory=dict)

    def as_json(self) -> dict[str, Any]:
        return {
            "resolved": self.resolved,
            "override_approved": self.override_approved,
            "override_kind": self.override_kind,
            "note": self.note,
            "reason_no": self.reason_no,
            "escalate_review": self.escalate_review,
            "original_reason": self.original_reason,
            "has_modified_plan": self.modified_plan is not None,
            "context": self.context,
            "exposure": self.exposure,
        }


class CycleReport(BaseModel):
    """Riepilogo del ciclo del Capo (il dato che finisce in log/HTTP/n8n)."""

    started_at: str = ""
    finished_at: str = ""
    blocked_reason: str = ""
    market: dict[str, Any] = Field(default_factory=dict)
    strategy: dict[str, Any] = Field(default_factory=dict)
    #: Agente ANALISI (steam velocity + juice) — direttiva 04/10/2026.
    analysis: dict[str, Any] = Field(default_factory=dict)
    #: Agente CERVELLO (EV dinamico + Portfolio Shield).
    brain: dict[str, Any] = Field(default_factory=dict)
    #: Sizing della Finanza sul percorso ValidatedTrade (Kelly aggressivo).
    sizing: dict[str, Any] = Field(default_factory=dict)
    finance: dict[str, Any] = Field(default_factory=dict)
    execution: dict[str, Any] = Field(default_factory=dict)
    #: Consigli dell'Advisor sui blocchi (uno per segnale bloccato, se chiamato)
    advisor: list[dict[str, Any]] = Field(default_factory=list)
    #: Recinto di esposizione aperta (direttiva 28/09/2026): ordini in corso,
    #: capitale immobilizzato e tetto del 40% ricalcolato sull'equity corrente.
    exposure: dict[str, Any] = Field(default_factory=dict)
    #: Ordini RESTING (GTC) su SX Bet (10/10/2026): capitale parcheggiato sul
    #: book in attesa di controparte — non e' ancora una puntata, ma si impegna
    #: quando si riempie. Il Capo lo vede a ogni ciclo.
    resting: dict[str, Any] = Field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.blocked_reason

    def as_json(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "blocked_reason": self.blocked_reason,
            "market": self.market,
            "strategy": self.strategy,
            "analysis": self.analysis,
            "brain": self.brain,
            "sizing": self.sizing,
            "finance": self.finance,
            "execution": self.execution,
            "advisor": self.advisor,
            "exposure": self.exposure,
            "resting": self.resting,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }
