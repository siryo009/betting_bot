"""decision/commands.py — Il motore emette COMANDI, non effetti.

Perche' esiste (rifattorizzazione del 15/09/2026): la catena di decisione
sapeva *cosa* andava fatto, ma lo faceva da sola (o lo lasciava fare al
chiamante, in ordine sparso). Qui il motore produce **comandi leggeri** —
dati serializzabili che descrivono l'effetto desiderato — e il compito di
eseguirli passa a gateway dedicati (`decision/gateways.py`), instradati dal
`Dispatcher` (`decision/dispatcher.py`).

Vantaggi concreti, non teorici:

- **Testabilita'**: un piano si ispeziona senza DB, senza rete, senza ordini
  (`test_decision_commands.py` verifica esattamente questo).
- **Shadow mode**: lo stesso piano si puo' eseguire con gateway che NON
  eseguono (solo registrano) — e' il confronto misurato voluto per auto_bet.
- **Idempotenza**: ogni comando porta una `dedup_key` stabile (stesso
  effetto -> stessa chiave), cosi' un job che gira ogni 60s non produce
  duplicati quando non c'e' nulla di nuovo.
- **Audit**: il piano e' JSON, quindi finisce nei log strutturati senza
  conversioni (nessun `repr()` di oggetti vivi).

Regole del modulo:

1. **Purezza assoluta**: nessun import di `tracker`, `auto_bet`, provider o
   rete. Solo pydantic. Un comando non esegue, descrive.
2. **Leggerezza**: nel payload c'e' SOLO cio' che serve a eseguire. Il
   `DecisionRecord` completo viaggia dentro `PersistDecision` (e' un dato,
   non una dipendenza).
3. **Un comando, un effetto**: `PlaceOrder` non notifica, `NotifyOperators`
   non scrive sul ledger. L'ordine degli effetti e' responsabilita' del
   `Dispatcher`, che li esegue nell'ordine in cui sono emessi.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator

from .models import DecisionRecord, Mode, Money, Outcome, Signal, utcnow


class CommandKind(str, Enum):
    """Tipi di effetto che il motore puo' chiedere."""

    PERSIST_DECISION = "persist_decision"    # scrive la riga sul ledger
    PLACE_ORDER = "place_order"              # ordine sull'exchange (SOLO live)
    NOTIFY_OPERATORS = "notify_operators"    # Telegram a admin/iscritti
    #: Quote multi-mercato sul ledger (`market_quotes`): dati di ingresso,
    #: non un effetto sul mondo — vanno scritti PRIMA della decisione che
    #: su quei prezzi e' stata presa.
    SAVE_MARKET_QUOTES = "save_market_quotes"
    #: Campione CLV sul ledger (`clv_history`): MISURA a cose fatte, in coda.
    WRITE_CLV = "write_clv"


#: Ordine di esecuzione dei comandi quando il motore ne emette piu' di uno.
#: La regola e' "prima l'EVIDENZA, poi l'effetto": lo snapshot di mercato
#: (le quote su cui si e' deciso) apre, la riga di decisione segue, l'ordine
#: viene DOPO l'audit; notifiche e misure chiudono. Un fallimento a valle non
#: cancella cio' che e' stato scritto prima.
COMMAND_ORDER = (CommandKind.SAVE_MARKET_QUOTES,
                 CommandKind.PERSIST_DECISION, CommandKind.PLACE_ORDER,
                 CommandKind.NOTIFY_OPERATORS, CommandKind.WRITE_CLV)


def _digest(*parts: Any) -> str:
    """Digest stabile a 12 hex per le chiavi di deduplicazione."""
    raw = "|".join("" if p is None else str(p) for p in parts).encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Payload tipizzati (validati all'EMISSIONE: un comando malformato non nasce)
# ---------------------------------------------------------------------------

class PlaceOrderPayload(BaseModel):
    """Cio' che serve a un gateway d'esecuzione. Niente logica, solo dati."""

    match_id: str
    league: str = ""
    home: str = ""
    away: str = ""
    market: str = "1X2"
    outcome: Outcome
    selection_label: str = ""
    kickoff: datetime
    #: Quota del segnale: e' il BOUND dell'ordine (mai peggio di cosi').
    #: Denaro (direttiva 29/09): nel payload serializzato esce come NUMERO
    #: (`Money` -> float in JSON), quindi i gateway lo leggono come prima.
    price: Money = Field(..., gt=1.0)
    stake: Money = Field(..., gt=0.0)
    mode: Mode = "live"
    provider: str = ""
    time_in_force: str = "IOC"


class PersistDecisionPayload(BaseModel):
    """La riga piatta del ledger (`DecisionRecord.as_row()`)."""

    row: dict[str, Any]
    record_id: str = ""


class NotifyPayload(BaseModel):
    """Messaggio agli operatori, con chiave anti-spam."""

    kind: Literal["blocked", "review_pending", "order_placed", "info"] = "info"
    text: str
    dedup_key: str = ""
    targets: list[str] = Field(default_factory=list)


class WriteCLVPayload(BaseModel):
    """Campione CLV per il ledger `clv_history` (niente logica, solo dati).

    I campi ricalcano la firma di `tracker.save_clv` — l'esecutore reale —
    piu' `source` (provenienza del campione) e `signal_odds`/`closing_odds`
    che portano la MISURA (la differenza di quota non va ricalcolata a valle).
    """

    match_id: str = Field(..., min_length=1)
    outcome: str = Field(..., min_length=1)
    signal_odds: float = Field(..., gt=1.0)
    closing_odds: float = Field(..., gt=1.0)
    timestamp: datetime
    source: str = ""


class SaveQuotesPayload(BaseModel):
    """Righe di quote multi-mercato per il ledger `market_quotes`.

    `rows` sono righe GIA' serializzate da `MarketQuote.as_row()` (o da
    `FixtureQuotes.as_rows()`): il contratto resta l'unico posto in cui una
    quota viene VALIDATA, il comando si limita a trasportarla. Almeno una riga:
    un upsert senza righe non e' un effetto e non deve nemmeno nascere.
    """

    rows: list[dict[str, Any]] = Field(..., min_length=1)
    fixture_id: str = ""
    gateway_id: str = ""
    source: str = ""


# ---------------------------------------------------------------------------
# Comando
# ---------------------------------------------------------------------------

class Command(BaseModel):
    """Un effetto richiesto dal motore, in forma di dato."""

    kind: CommandKind
    payload: dict[str, Any] = Field(default_factory=dict)
    command_id: str = ""
    #: Chiave STABILE dell'effetto (stesso effetto -> stessa chiave): i
    #: gateway la usano per non ripetere cio' che e' gia' stato fatto.
    dedup_key: str = ""
    signal_id: str = ""
    record_id: str = ""
    mode: Mode = "sim"
    created_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _fill_ids(self):
        if not self.command_id:
            self.command_id = _digest(self.kind.value, self.record_id or self.signal_id,
                                      self.created_at.strftime("%Y%m%dT%H%M%S"))
        return self

    @property
    def order(self) -> int:
        """Posizione del comando nell'ordine di esecuzione dichiarato."""
        try:
            return COMMAND_ORDER.index(self.kind)
        except ValueError:                          # kind sconosciuto: in coda
            return len(COMMAND_ORDER)

    def as_json(self) -> dict[str, Any]:
        """Rappresentazione JSON-safe (per log strutturati e shadow ledger)."""
        return self.model_dump(mode="json")


# ---------------------------------------------------------------------------
# Fabbriche: costruiscono E validano il payload all'emissione
# ---------------------------------------------------------------------------

def persist_decision_command(record: DecisionRecord) -> Command:
    """Comando che scrive la decisione sul ledger (`decisions`)."""
    row = record.as_row()
    return Command(
        kind=CommandKind.PERSIST_DECISION,
        payload=PersistDecisionPayload(row=row, record_id=record.record_id).model_dump(mode="json"),
        dedup_key=_digest("persist", record.record_id),
        signal_id=record.signal.signal_id,
        record_id=record.record_id,
        mode=record.mode,
    )


def place_order_command(record: DecisionRecord, *, provider: str = "",
                        home: str = "", away: str = "",
                        stake: Optional[Any] = None) -> Command:
    """Comando d'ordine per un segnale APPROVATO ed ESEGUIBILE.

    La quota nel payload e' quella del segnale: il gateway non puo' riempire a
    un prezzo peggiore (floor EV, come in `auto_bet._live_fill`). La
    `dedup_key` rende l'ordine idempotente per (match, esito).
    """
    # Denaro: `Decimal` o float, normalizzato dal campo `Money` del payload
    # (unico punto di conversione). Il dict del comando esce col NUMERO.
    stake_value = stake if stake is not None else (record.stake.stake if record.stake else 0.0)
    payload = PlaceOrderPayload(
        match_id=record.signal.match_id,
        league=record.signal.league,
        home=home, away=away,
        market=record.signal.market,
        outcome=record.signal.outcome,
        selection_label=record.signal.selection_label,
        kickoff=record.signal.kickoff,
        price=record.signal.price,
        stake=stake_value,
        mode=record.mode,
        provider=provider or record.provider,
    )
    return Command(
        kind=CommandKind.PLACE_ORDER,
        payload=payload.model_dump(mode="json"),
        dedup_key=_digest("order", record.signal.match_id, record.signal.outcome),
        signal_id=record.signal.signal_id,
        record_id=record.record_id,
        mode=record.mode,
    )


def write_clv_command(*, match_id: str, outcome: str, signal_odds: float,
                      closing_odds: float, timestamp: Optional[datetime] = None,
                      source: str = "", signal_id: str = "",
                      mode: Mode = "sim") -> Command:
    """Comando che registra un campione CLV sul ledger (`clv_history`).

    Emissione tipica: il valutatore CLV (`decision/clv.py`) calcola la
    differenza di quota in modo PURO e restituisce questo comando
    all'orchestratore, che lo instrada a un gateway di scrittura. Il valutatore
    non tocca il DB.
    """
    payload = WriteCLVPayload(
        match_id=match_id, outcome=outcome,
        signal_odds=signal_odds, closing_odds=closing_odds,
        timestamp=timestamp or utcnow(), source=source)
    return Command(
        kind=CommandKind.WRITE_CLV,
        payload=payload.model_dump(mode="json"),
        dedup_key=_digest("clv", match_id, outcome, signal_odds, closing_odds),
        signal_id=signal_id,
        mode=mode,
    )


def save_quotes_command(rows, *, fixture_id: str = "", gateway_id: str = "",
                        source: str = "", mode: Mode = "sim") -> Command:
    """Comando che persiste le quote multi-mercato sul ledger.

    La `dedup_key` cambia quando cambia il PREZZO: lo stesso palinsesto letto
    due volte non produce due comandi (il registro shadow non si riempie di
    ripetizioni), ma un movimento di quota in finestra T-60 si'. L'upsert a
    valle e' comunque idempotente per chiave, quindi ri-eseguire non duplica.
    """
    payload = SaveQuotesPayload(
        rows=[dict(row) for row in rows], fixture_id=fixture_id,
        gateway_id=gateway_id, source=source)
    keys = []
    for row in payload.rows:
        identity = row.get("identity_key")
        if not identity:
            identity = "|".join(str(row.get(key) or "") for key in
                                ("fixture_id", "market_type", "line_key", "selection"))
        keys.append(f"{identity}:{row.get('odds')}")
    return Command(
        kind=CommandKind.SAVE_MARKET_QUOTES,
        payload=payload.model_dump(mode="json"),
        dedup_key=_digest("quotes", fixture_id, *sorted(keys)),
        mode=mode,
    )


def notify_command(record: DecisionRecord, *, kind: str, text: str,
                   targets: Optional[list[str]] = None,
                   scope: str = "") -> Command:
    """Comando di notifica, con chiave anti-spam.

    `scope` decide la granularita' della deduplicazione: vuoto = per decisione
    (una notifica per segnale, caso `review_pending`); valorizzato in modo
    stabile nel tempo (es. il giorno) = UNA notifica per quel tipo, come
    l'anti-spam del kill switch che avvisava 1440 volte al giorno col job ogni
    60s (chiave `KS_OFF` del 09/09).
    """
    day = record.created_at.strftime("%Y%m%d")
    key = _digest("notify", kind, scope) if scope else _digest("notify", kind,
                                                               record.record_id, day)
    return Command(
        kind=CommandKind.NOTIFY_OPERATORS,
        payload=NotifyPayload(kind=kind, text=text,           # type: ignore[arg-type]
                              dedup_key=key,
                              targets=list(targets or [])).model_dump(mode="json"),
        dedup_key=key,
        signal_id=record.signal.signal_id,
        record_id=record.record_id,
        mode=record.mode,
    )


# ---------------------------------------------------------------------------
# Piano
# ---------------------------------------------------------------------------

class WriteCLVCommand(BaseModel):
    """Contratto IMMUTABILE del comando CLV (alias tipizzato di `Command`).

    Il valutatore CLV (`decision/clv.py`) restituisce all'orchestratore
    l'istanza con i soli campi del contratto: `signal_id`, `market_id`,
    `signal_odds`, `closing_odds`, `timestamp`, `source`. E' un record
    Pydantic congelato: niente logica, nessun side effect, la scrittura su DB
    spetta al gateway (`decision/gateways.ClvGateway`).

    `market_id` e' l'id del match sul ledger (`matches.match_id`, es.
    `sx-L…`): e' la chiave che `tracker.save_clv` si aspetta in `match_id`.
    La differenza di quota NON e' un campo: e' una MISURA calcolata dal
    valutatore (`clv_diff`), non parte del comando che la richiede.
    """

    model_config = {"frozen": True}

    signal_id: str = ""
    market_id: str = Field(..., min_length=1)
    signal_odds: float = Field(..., gt=1.0)
    closing_odds: float = Field(..., gt=1.0)
    timestamp: datetime
    source: str = ""


class CommandPlan(BaseModel):
    """L'uscita del motore: un record + i comandi che lo traducono in effetti.

    Il piano NON esegue niente. `dispatcher.dispatch(plan)` lo fa, con i
    gateway scelti (reali, shadow, finti nei test).
    """

    plan_id: str = ""
    record: DecisionRecord
    commands: list[Command] = Field(default_factory=list)
    #: Motivo del fail-fast, se un blocco di sicurezza ha fermato la catena
    #: PRIMA di qualunque calcolo (`decision/guards.py`).
    blocked: Optional[dict[str, Any]] = None
    #: Identita' del feed DI MERCATO che ha preceduto il Risk Engine
    #: (`decision/feeds.py`): request_id, trace_id, gateway_id, schema_version,
    #: config_hash, source, refreshed_at. E' cio' che lega una decisione alla
    #: quotatura su cui e' stata presa — senza, "perche' questo ordine?" non ha
    #: risposta verificabile.
    market: Optional[dict[str, Any]] = None
    created_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _fill_plan(self):
        if not self.plan_id:
            self.plan_id = _digest("plan", self.record.record_id)
        self.commands.sort(key=lambda c: c.order)
        return self

    def kinds(self) -> list[str]:
        return [c.kind.value for c in self.commands]

    def of_kind(self, kind: CommandKind) -> list[Command]:
        return [c for c in self.commands if c.kind == kind]

    @property
    def places_order(self) -> bool:
        return bool(self.of_kind(CommandKind.PLACE_ORDER))

    def as_json(self) -> dict[str, Any]:
        """Piano serializzato: e' cio' che finisce nei log e nello shadow ledger."""
        return {
            "plan_id": self.plan_id,
            "record_id": self.record.record_id,
            "signal_id": self.record.signal.signal_id,
            "verdict": self.record.risk.verdict,
            "reason": self.record.risk.reason.value,
            "mode": self.record.mode,
            "commands": [c.as_json() for c in self.commands],
            "blocked": self.blocked,
            "market": self.market,
            "created_at": self.created_at.isoformat(),
        }


def plan_for_record(record: DecisionRecord, commands: list[Command], *,
                    blocked: Optional[dict] = None,
                    market: Optional[dict] = None) -> CommandPlan:
    """Piano da record + comandi (i comandi restano ordinati per `order`)."""
    return CommandPlan(record=record, commands=list(commands), blocked=blocked,
                       market=market)


__all__ = [
    "COMMAND_ORDER", "Command", "CommandKind", "CommandPlan", "NotifyPayload",
    "PersistDecisionPayload", "PlaceOrderPayload", "SaveQuotesPayload",
    "WriteCLVCommand", "WriteCLVPayload", "notify_command",
    "persist_decision_command", "place_order_command", "plan_for_record",
    "save_quotes_command", "write_clv_command",
]
