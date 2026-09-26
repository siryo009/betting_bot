"""decision/models.py — Contratti della catena di decisione.

Tre stadi separati, ognuno con un contratto proprio:

    Signal  ──▶  RiskDecision  ──▶  StakeDecision  ──▶  DecisionRecord
    (cosa e'          (approve/          (quanto, SOLO      (feedback:
     successo)         review/reject)     se approvato)      input+esito)

Regole di ferro (verificate dai tripwire in `test_decision_pipeline.py`):

1. **Il Signal NON contiene lo stake.** Descrive un'opportunita' con la
   probabilita' calibrata: "probabilita' calibrata" non e' un invito a
   scommettere, e chi decide il rischio non e' chi calcola il modello.
2. **Il Risk Engine puo' solo stringere.** `RiskDecision.tightened` puo'
   ridurre un cap, mai alzarlo: nessun motore a valle puo' allargare cio' che
   un motore a monte ha ristretto.
3. **Motivi machine-readable.** Ogni verdetto porta un `ReasonCode`, mai solo
   prosa: il feedback engine aggrega i motivi, non interpreta frasi.
4. **Nessun effetto collaterale.** Questi modelli non toccano DB, rete o
   provider: sono dati.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

Market = Literal["1X2"]
Outcome = Literal["1", "X", "2"]
Tier = Literal["value", "strong_value", "moderate"]
Mode = Literal["off", "sim", "live"]
Verdict = Literal["approve", "review", "reject"]


def utcnow() -> datetime:
    """Ora UTC timezone-aware (mai naive: i kickoff sono UTC)."""
    return datetime.now(timezone.utc)


class ReasonCode(str, Enum):
    """Motivo di una decisione, aggregabile e confrontabile."""

    OK = "ok"
    # --- kill switch (in ordine di precedenza) ---
    KILL_SWITCH_OFF = "kill_switch_off"          # /autobet off: stop totale
    DAILY_STOP_LOSS = "daily_stop_loss"          # -5% in 24h: puntate bloccate
    WEEKLY_STOP_LOSS = "weekly_stop_loss"        # -12% rolling 7g: puntate bloccate
    SETTLEMENT_PAUSED = "settlement_paused"      # non blocca la bet, blocca il referto
    # --- gate di mercato (feed): fail-closed PRIMA del Risk Engine ---
    FEED_MISSING = "feed_missing"                  # nessun refresh in questo giro
    FEED_UNAVAILABLE = "feed_unavailable"          # refresh fallito/non conforme
    FEED_STALE = "feed_stale"                      # quotatura troppo vecchia
    FEED_NOT_VALIDATED = "feed_not_validated"      # serie di refresh conformi incompleta
    # --- Risk Engine ---
    LEAGUE_NOT_ALLOWED = "league_not_allowed"
    ODDS_TOO_LOW = "odds_too_low"
    ODDS_TOO_HIGH = "odds_too_high"
    NOT_FAVOURITE = "not_favourite"
    EV_TOO_LOW = "ev_too_low"
    EV_ANOMALOUS = "ev_anomalous"
    EDGE_TOO_LOW = "edge_too_low"
    DATA_QUALITY_LOW = "data_quality_low"    # modello cieco (senza ratings) -> umano
    CONFIDENCE_LOW = "confidence_low"
    LIQUIDITY_LOW = "liquidity_low"
    MARKET_INCOHERENT = "market_incoherent"
    ALREADY_EXPOSED = "already_exposed"
    # --- Stake Engine ---
    STAKE_BELOW_FLOOR = "stake_below_floor"      # cap severo: fail-closed
    RISK_TIGHTENED = "risk_tightened"
    # --- revisione umana ---
    REVIEW_PENDING = "review_pending"
    REVIEW_APPROVED = "review_approved"
    REVIEW_REJECTED = "review_rejected"
    REVIEW_EXPIRED = "review_expired"
    # --- Shadow Validation (convalida della riga PERSISTITA) ---
    STAKE_NOT_EXECUTABLE = "stake_not_executable"   # stake assente/cappato sotto il floor
    VALIDATION_INCOMPLETE = "validation_incomplete"  # riga incompleta/illeggibile
    # --- Circuit breakers T-60 (17/09/2026, direttiva del proprietario) ---
    T60_WINDOW_OPEN = "t60_window_open"          # dentro la finestra esecutiva T-60..T-50
    T60_WINDOW_NOT_YET = "t60_window_not_yet"    # kickoff oltre la finestra: attendi
    T60_WINDOW_MISSED = "t60_window_missed"      # meno di T60_EXEC_MIN_MIN: non si ordina
    T60_NO_EXECUTABLE_WINDOW = "t60_no_executable_window"  # nessun segnale eseguibile nel giro
    T60_NO_PENDING_FOR_EXECUTION = "t60_no_pending_for_execution"  # riga non trovata per signal_id
    T60_ORDER_ALREADY_PLACED = "t60_order_already_placed"  # lineage: eseguito in un giro precedente
    T60_NOT_VALIDATED_FOR_EXECUTION = "t60_not_validated_for_execution"  # convalida non positiva
    T60_NO_EXECUTION_IN_SIM = "t60_no_execution_in_sim"  # esecuzione reale solo in live
    T60_KILL_SWITCH_WALLET = "t60_kill_switch_wallet"    # CB2: wallet <= kill switch
    T60_FEED_GATE_BLOCKED = "t60_feed_gate_blocked"      # gate di mercato non passato


# Stati del ciclo di vita di una riga del ledger `decisions`:
#
#   pending   -> persistita, NON ancora convalidata (nessun ordine reale)
#   validated -> convalida POSITIVA: l'ordine puo' partire
#   rejected  -> convalida negativa: la riga resta come audit, niente ordine
#
# ⚠️ Duplicati di proposito in `tracker.py`: il ledger non importa questo
# pacchetto (e viceversa). Un tripwire in `test_decision_validation.py`
# confronta le due tabelle di stringhe, cosi' non possono divergere.
DECISION_STATUS_PENDING = "pending"
DECISION_STATUS_VALIDATED = "validated"
DECISION_STATUS_REJECTED = "rejected"
DECISION_STATUSES = (DECISION_STATUS_PENDING, DECISION_STATUS_VALIDATED,
                     DECISION_STATUS_REJECTED)

DecisionStatus = Literal["pending", "validated", "rejected"]


class DataQuality(BaseModel):
    """Qualita' dei dati che sostengono il Signal (telemetria, non prosa).

    Serve al Risk Engine per distinguere "modello che non batte il mercato" da
    "modello cieco": senza ratings reali `expected_goals` usa il profilo neutro
    di lega e una probabilita' bassa e' ignoranza, non valore.
    """

    ratings_home_n: int = 0          # partite osservate per la squadra di casa
    ratings_away_n: int = 0
    model_coverage: float = Field(0.0, ge=0.0, le=1.0)   # 0.0 = entrambe senza rating
    calibrated: bool = False         # calibrazione isotonica attiva
    calibration_samples: int = 0
    market_coherent: bool = True     # inv_sum nella fascia 0.98-1.08
    inv_sum: Optional[float] = None
    depth_usdc: Optional[float] = None       # profondita' al floor della leg giocata
    snapshot_age_minutes: Optional[float] = None
    flags: list[str] = Field(default_factory=list)

    @property
    def is_blind(self) -> bool:
        """True se nessuna delle due squadre ha rating (modello neutro)."""
        return self.model_coverage <= 0.0


class Signal(BaseModel):
    """Un'opportunita' rilevata dal Signal Engine. NON contiene lo stake."""

    signal_id: str = ""
    match_id: str
    league: str = ""
    market: Market = "1X2"
    outcome: Outcome
    selection_label: str = ""
    kickoff: datetime
    price: float = Field(..., gt=1.0, description="quota giocabile")
    price_source: str = ""
    market_prob: float = Field(..., gt=0.0, lt=1.0)
    model_prob: float = Field(..., ge=0.0, le=1.0)
    blended_prob: float = Field(..., gt=0.0, lt=1.0)
    edge: Optional[float] = None       # blended - market (calcolato se assente)
    ev: Optional[float] = None         # blended*price - 1 (calcolato se assente)
    tier: Tier = "moderate"
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    data_quality: DataQuality = Field(default_factory=DataQuality)
    warnings: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _fill_derived(self):
        if self.edge is None:
            self.edge = self.blended_prob - self.market_prob
        if self.ev is None:
            self.ev = self.blended_prob * self.price - 1.0
        if not self.signal_id:
            self.signal_id = make_signal_id(self.match_id, self.market, self.outcome)
        if self.data_quality.is_blind and "ratings_assenti" not in self.warnings:
            self.warnings.append("ratings_assenti")
        return self

    @property
    def is_favourite(self) -> bool:
        return self.market_prob >= 0.50


def make_signal_id(match_id: str, market: str, outcome: str) -> str:
    """Id STABILE di un segnale (stesso match/mercato/esito -> stesso id)."""
    raw = f"{match_id}|{market}|{outcome}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------------

# Precedenza decisa dal proprietario il 14/09/2026:
#   kill switch manuale > stop-loss giornaliero > pausa settlement.
# Il primo blocco in quest'ordine e' il motivo riportato (e cio' che va
# rimosso per ripartire); gli altri restano visibili come avvisi.
KILL_SWITCH_PRECEDENCE = ("manual", "daily_stop", "weekly_stop",
                          "settlement_pause")

_BLOCK_REASONS = {
    "manual": ReasonCode.KILL_SWITCH_OFF,
    "daily_stop": ReasonCode.DAILY_STOP_LOSS,
    "weekly_stop": ReasonCode.WEEKLY_STOP_LOSS,
}


class KillSwitchStatus(BaseModel):
    """Istantanea dei blocchi attivi (letta da file/env, iniettabile nei test)."""

    mode: Mode = "off"
    env_mode: str = ""
    override: Optional[str] = None
    provider_ready: bool = False
    daily_stop_active: bool = False
    daily_stop_detail: str = ""
    weekly_stop_active: bool = False
    weekly_stop_detail: str = ""
    settlement_paused: bool = False

    def betting_blocks(self) -> list[str]:
        """Blocchi che impediscono una puntata, in ordine di precedenza."""
        blocks: list[str] = []
        if self.mode == "off":
            blocks.append("manual")
        if self.daily_stop_active:
            blocks.append("daily_stop")
        if self.weekly_stop_active:
            blocks.append("weekly_stop")
        return [name for name in KILL_SWITCH_PRECEDENCE if name in blocks]

    def advisories(self) -> list[str]:
        """Condizioni non bloccanti per la bet ma da mostrare (in precedenza)."""
        out: list[str] = []
        if self.settlement_paused:
            out.append("settlement_pause")
        return out

    def first_block(self) -> Optional[ReasonCode]:
        """Motivo del primo blocco in ordine di precedenza (None = libero)."""
        blocks = self.betting_blocks()
        return _BLOCK_REASONS.get(blocks[0]) if blocks else None

    @property
    def betting_allowed(self) -> bool:
        return not self.betting_blocks()


# ---------------------------------------------------------------------------
# Risk Engine
# ---------------------------------------------------------------------------

class RiskDecision(BaseModel):
    """Verdetto del Risk Engine: approve / review / reject + motivi."""

    verdict: Verdict
    reason: ReasonCode
    detail: str = ""
    reasons: list[ReasonCode] = Field(default_factory=list)
    advisories: list[ReasonCode] = Field(default_factory=list)
    checked: list[str] = Field(default_factory=list)     # ordine dei controlli
    tightened: dict[str, float] = Field(default_factory=dict)  # cap ridotti

    @property
    def allows_stake(self) -> bool:
        """Solo `approve` autorizza lo Stake Engine."""
        return self.verdict == "approve"


def risk_approve(*, checked: list[str], tightened: Optional[dict] = None,
                 advisories: Optional[list] = None) -> RiskDecision:
    return RiskDecision(verdict="approve", reason=ReasonCode.OK, detail="gate superati",
                        checked=list(checked), tightened=dict(tightened or {}),
                        advisories=list(advisories or []))


def risk_review(reason: ReasonCode, detail: str, *, checked: list[str],
                tightened: Optional[dict] = None) -> RiskDecision:
    return RiskDecision(verdict="review", reason=reason, detail=detail,
                        reasons=[reason], checked=list(checked),
                        tightened=dict(tightened or {}))


def risk_reject(reason: ReasonCode, detail: str, *,
                checked: list[str]) -> RiskDecision:
    return RiskDecision(verdict="reject", reason=reason, detail=detail,
                        reasons=[reason], checked=list(checked))


# ---------------------------------------------------------------------------
# Stake Engine
# ---------------------------------------------------------------------------

class StakeDecision(BaseModel):
    """Quanto puntare. Esiste SOLO a valle di un verdetto che autorizza."""

    bankroll: float
    stake: float = 0.0
    kelly_fraction: float = 0.0
    kelly_stake: float = 0.0          # stake prima dei cap (per l'audit)
    cap_pct: Optional[float] = None   # cap che ha morso (percentuale bankroll)
    cap_source: str = ""              # "tier" | "league" | "risk" | "none"
    floor: float = 0.0
    executable: bool = False
    reason: ReasonCode = ReasonCode.OK
    detail: str = ""
    mode: Mode = "sim"

    @property
    def skipped(self) -> bool:
        return not self.executable


# ---------------------------------------------------------------------------
# Feedback Engine (record unico)
# ---------------------------------------------------------------------------

class DecisionRecord(BaseModel):
    """La riga che lega input, previsione, decisione, stake e (dopo) esito."""

    record_id: str = ""
    signal: Signal
    kill_switch: KillSwitchStatus = Field(default_factory=KillSwitchStatus)
    risk: RiskDecision
    stake: Optional[StakeDecision] = None
    #: Stato della Shadow Validation. Nasce `pending` (la riga va PERSISTITA
    #: prima di qualunque convalida) e viene mosso dal solo gateway di
    #: validazione (`decision/gateways.ValidatingLedgerGateway`): il motore di
    #: decisione non lo tocca, cosi' "decidere" e "convalidare" restano due
    #: atti distinti e verificabili.
    status: DecisionStatus = DECISION_STATUS_PENDING
    mode: Mode = "sim"
    provider: str = ""
    approved_by: Optional[str] = None      # revisione umana (Telegram)
    review_note: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    order: Optional[dict[str, Any]] = None       # riempito dall'esecuzione
    settlement: Optional[dict[str, Any]] = None  # riempito dal referto

    @model_validator(mode="after")
    def _fill_id(self):
        if not self.record_id:
            stamp = self.created_at.strftime("%Y%m%dT%H%M%S")
            self.record_id = f"{self.signal.signal_id}-{stamp}"
        return self

    def as_row(self) -> dict[str, Any]:
        """Riga piatta per il ledger del feedback engine."""
        row = {
            "record_id": self.record_id,
            "signal_id": self.signal.signal_id,
            "match_id": self.signal.match_id,
            "league": self.signal.league,
            "market": self.signal.market,
            "outcome": self.signal.outcome,
            "selection_label": self.signal.selection_label,
            "kickoff": self.signal.kickoff.isoformat(),
            "price": self.signal.price,
            "price_source": self.signal.price_source,
            "market_prob": self.signal.market_prob,
            "model_prob": self.signal.model_prob,
            "blended_prob": self.signal.blended_prob,
            "edge": self.signal.edge,
            "ev": self.signal.ev,
            "tier": self.signal.tier,
            "confidence": self.signal.confidence,
            "model_coverage": self.signal.data_quality.model_coverage,
            "calibrated": self.signal.data_quality.calibrated,
            "verdict": self.risk.verdict,
            "reason": self.risk.reason.value,
            "status": self.status,
            "mode": self.mode,
            "provider": self.provider,
            "approved_by": self.approved_by,
            "review_note": self.review_note,
            "created_at": self.created_at.isoformat(),
        }
        if self.stake is not None:
            row.update({
                "stake": self.stake.stake,
                "stake_executable": self.stake.executable,
                "kelly_fraction": self.stake.kelly_fraction,
                "cap_pct": self.stake.cap_pct,
                "cap_source": self.stake.cap_source,
            })
        if self.order:
            row["order_id"] = self.order.get("bet_id") or self.order.get("order_id")
            row["order_status"] = self.order.get("status")
        if self.settlement:
            row["outcome_final"] = self.settlement.get("esito_finale")
            row["profit"] = self.settlement.get("profit")
        return row


class T60OrderContract(BaseModel):
    """Contratto dell'ordine T-60: identity + verdetto + decisione di rischio.

    CB3 (validazione Pydantic rigida): ogni parametro dell'ordine in uscita
    DEVE passare da questo modello prima del gateway d'esecuzione. Rigore
    (piu' stretto di PlaceOrderPayload, perche' qui passa il denaro):

    - price > 1.0 (una quota <= 1 e' dati malformati, non un mercato);
    - kickoff e created_at con fuso orario OBBLIGATORIO (mai un istante
      ambiguo su un ordine reale);
    - league NON vuota (gate STRATEGY_LEAGUES: senza lega non si sa cosa si
      sta giocando);
    - outcome nei canoni accettati ("1", "X", "2" o il nome della squadra);
    - stake: richiesto (una decisione senza stake non e' eseguibile).

    I circuit breakers sono SopRA il modello: `stake <= T60_MAX_STAKE_USDC`
    e `price <= T60_MAX_ODDS` vengono VERIFICATI dal chiamante (`validate_
    order_payload`) e, se violati, l'ordine e' scartato con `order.rejected`
    — il contratto del decreto e' una REGOLA, non un default silenzioso.
    """

    model_config = {"extra": "forbid"}

    signal_id: str = Field(..., min_length=1)
    record_id: str = Field(..., min_length=1)
    match_id: str = Field(..., min_length=1)
    league: str = Field(..., min_length=1)
    market: Market = "1X2"
    outcome: str = Field(..., min_length=1)
    home: str = ""
    away: str = ""
    price: float = Field(..., gt=1.0)
    stake: float = Field(..., gt=0.0)
    verdict: Verdict = "approve"
    mode: Mode = "live"
    provider: str = ""
    kickoff: datetime
    created_at: datetime
    # Timestamp di convalida del payload: l'ordine parte SOLO se la riga sul
    # ledger e' stata convalidata positiva (Shadow Validation).
    validated_at: Optional[datetime] = None

    @field_validator("kickoff", "created_at", "validated_at")
    @classmethod
    def _require_tz(cls, v: Optional[datetime]) -> Optional[datetime]:
        if v is not None and v.tzinfo is None:
            raise ValueError("timestamp senza fuso orario: usa l'UTC esplicito")
        return v

    @model_validator(mode="after")
    def _coherence(self) -> "T60OrderContract":
        if self.kickoff <= self.created_at:
            raise ValueError("kickoff non successivo a created_at: riga incoerente")
        if self.mode == "live" and not self.provider:
            raise ValueError("ordine live senza provider: rifiutato")
        return self

    def model_dump_json_compact(self) -> str:
        return self.model_dump_json()


def t60_executable(stake: float, price: float) -> bool:
    """Un contratto T-60 rispetta i circuit breakers di decreto?

    CB1 hard cap (0.50 USDC direttiva; su SX il minimo ordine e' 1.00 e la
    scelta del proprietario del 17/09 e' il cap eseguibile 1.00) e tetto
    quota (solo favoriti netti 1.30-1.80). Usato dal validatore d'ordine e
    dai tripwire: una sola fonte per la regola.
    """
    from auto_bet import T60_MAX_ODDS, T60_MAX_STAKE_USDC  # lazy: nessun ciclo
    return (0.0 < float(stake) <= float(T60_MAX_STAKE_USDC) + 1e-9
            and 1.0 < float(price) <= float(T60_MAX_ODDS) + 1e-9)


__all__ = [
    "DECISION_STATUSES", "DECISION_STATUS_PENDING", "DECISION_STATUS_REJECTED",
    "DECISION_STATUS_VALIDATED", "DataQuality", "DecisionRecord", "DecisionStatus",
    "KILL_SWITCH_PRECEDENCE", "KillSwitchStatus", "Market", "Mode", "Outcome",
    "ReasonCode", "RiskDecision", "Signal", "StakeDecision", "T60OrderContract",
    "Tier", "Verdict", "make_signal_id", "risk_approve", "risk_reject",
    "risk_review", "t60_executable", "utcnow",
]
