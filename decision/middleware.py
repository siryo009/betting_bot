"""decision/middleware.py — Osservabilita': log JSON strutturati con trace e span.

Ogni passo della catena emette eventi JSON con identita' correlabili:

    request_id      una chiamata del chiamante (es. un giro di `auto_bet`)
    trace_id        un percorso della catena (un piano)
    span_id         un passo dentro il percorso (guardie, risk, stake, comando)
    parent_span_id  lo span che ha aperto questo
    config_hash     impronta della CONFIGURAZIONE efficace che ha deciso

Perche' il **config hash** conta in un progetto come questo: le soglie sono
lette da env (`STAKE_CAP_PCT`, `STAKE_CAP_HARD`, `ODDS_MAX`, ...) e cambiano
senza toccare il codice. Senza impronta, due decisioni diverse sembrano uguali
e una regressione di strategia non e' attribuibile. Con l'impronta, "il ROI e'
peggiorato" si puo' legare a "il `config_hash` e' cambiato alle 18:40".

Il sink e' **configurabile** (env `DECISION_LOG_SINK`):

    <vuoto> | default  -> volume: `DATA_DIR/decision/events.jsonl`
    stdout             -> riga per riga su stdout (Railway li raccoglie)
    off | none | null  -> nessun evento (middleware spento)
    /percorso/file     -> quel file (append, JSONL)

Regole:

- **Mai un'eccezione**: un sink non scrivibile logga un warning (una volta) e
  la catena prosegue. L'osservabilita' non deve poter fermare una puntata.
- **Niente credenziali nei dettagli**: `redact()` maschera i campi sensibili
  (token/api key/password) prima della scrittura, coerente con
  `secure_logging.py`.
- **Eventi, non stampe sparse**: `span()` emette `span.start` / `span.end`
  (con `duration_ms` e `outcome`) e `span.error` in caso di eccezione, che
  viene RI-SOLLEVATA (il fail-fast non si perde nel logging).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional, Protocol

logger = logging.getLogger("decision.middleware")

SINK_ENV = "DECISION_LOG_SINK"
ENABLED_ENV = "DECISION_OBSERVABILITY"
#: Dimensione oltre la quale il JSONL del volume viene ruotato (MB). Il sink
#: di default e' sul volume e il job auto_bet gira ogni 60s: senza rotazione
#: il file cresce senza limite (osservato il 15/09 durante i test: 1051 righe
#: in pochi minuti di esecuzione).
MAX_MB_ENV = "DECISION_LOG_MAX_MB"
DEFAULT_MAX_MB = 5.0
#: Generazioni conservate oltre al file corrente (`events.jsonl.1`, `.2`).
GENERATIONS = 2
DEFAULT_COMPONENT = "decision"

#: Campi mai scritti in chiaro nei dettagli (i valori NON vengono loggati).
REDACTED_FIELDS = ("token", "api_key", "apikey", "password", "secret",
                   "private_key", "authorization", "passphrase")

_warned: set[str] = set()


def new_id() -> str:
    """Identificatore breve e casuale (8 hex) per request/trace/span."""
    return os.urandom(4).hex()


def config_hash(limits: Any = None, extra: Optional[Mapping[str, Any]] = None) -> str:
    """Impronta della configurazione efficace (12 hex).

    Hasha i limiti REALI (`RiskLimits.from_env()`: gia' cosi' leggono env e
    moduli di produzione) piu' gli eventuali `extra`. Stabile fra processi:
    due decisioni con la stessa config hanno la stessa impronta, anche dopo un
    redeploy (e' un hash di valori, non di memoria).
    """
    payload: dict[str, Any] = {}
    try:
        if limits is None:
            from .limits import RiskLimits
            limits = RiskLimits.from_env()
        dump = limits.model_dump() if hasattr(limits, "model_dump") else dict(limits)
        payload.update({str(k): v for k, v in sorted(dump.items())})
    except Exception as exc:                     # config illeggibile: impronta ignota
        logger.warning("middleware: limiti non leggibili per il config hash (%s)", exc)
        payload["limits"] = "unavailable"
    if extra:
        payload.update({str(k): v for k, v in sorted(extra.items())})
    raw = json.dumps(payload, sort_keys=True, default=_json_default, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def _json_default(value: Any) -> Any:
    """Fallback di `json.dumps` per i tipi non nativi.

    Il denaro e' `Decimal` (direttiva 29/09): con `default=str` finirebbe nei
    log come STRINGA (`"1.50"`) e un lettore — dashboard, grep, JSONL verso
    n8n — non potrebbe piu' sommare. Qui il `Decimal` esce come numero e ogni
    altro tipo non serializzabile ricade su `str`, come prima.
    """
    if isinstance(value, Decimal):
        return float(value)
    return str(value)


def redact(details: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    """Copia dei dettagli con i campi sensibili mascherati (mai in chiaro)."""
    out: dict[str, Any] = {}
    for key, value in dict(details or {}).items():
        lowered = str(key).lower()
        if any(bad in lowered for bad in REDACTED_FIELDS):
            out[str(key)] = "***"
        elif isinstance(value, Mapping):
            out[str(key)] = redact(value)
        else:
            out[str(key)] = value
    return out


# ---------------------------------------------------------------------------
# Sink
# ---------------------------------------------------------------------------

class EventSink(Protocol):
    """Contratto minimo di un sink: `write(event)` non solleva mai."""

    name: str

    def write(self, event: dict[str, Any]) -> None:  # pragma: no cover - protocollo
        ...


class NullSink:
    """Sink che non scrive nulla (osservabilita' spenta)."""

    name = "null"

    def write(self, event: dict[str, Any]) -> None:
        return None


class ListSink:
    """Sink in memoria: i test leggono gli eventi senza toccare il filesystem."""

    name = "list"

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def write(self, event: dict[str, Any]) -> None:
        self.events.append(event)

    def of(self, name: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e.get("event") == name]


def _max_bytes() -> int:
    try:
        return max(1, int(float(os.getenv(MAX_MB_ENV, str(DEFAULT_MAX_MB))) * 1024 * 1024))
    except (TypeError, ValueError):
        return int(DEFAULT_MAX_MB * 1024 * 1024)


class JsonlSink:
    """Sink JSONL su file o stdout (append, una riga per evento).

    Il file sul volume viene **ruotato** oltre `DECISION_LOG_MAX_MB` (default
    5 MB, `GENERATIONS` versioni conservate): il sink scrive a ogni giro del
    job, quindi la crescita va limitata (e un log di 20 MB sul volume non
    serve a nessuno). La rotazione e' fail-safe come la scrittura.
    """

    def __init__(self, path: Optional[str | Path] = None, *,
                 max_bytes: Optional[int] = None,
                 generations: int = GENERATIONS) -> None:
        self.path = Path(path) if path else None
        self.name = str(self.path) if self.path else "stdout"
        self.max_bytes = max_bytes
        self.generations = max(0, int(generations))

    def max_size(self) -> int:
        return self.max_bytes if self.max_bytes is not None else _max_bytes()

    def rotate(self) -> None:
        """Sposta il file corrente su `.1`, scalando le generazioni."""
        if self.path is None or self.generations <= 0:
            return
        if self.path.exists() and self.path.stat().st_size >= self.max_size():
            for index in range(self.generations, 0, -1):
                source = self.path if index == 1 else Path(f"{self.path}.{index - 1}")
                target = Path(f"{self.path}.{index}")
                if not source.exists():
                    continue
                if target.exists():
                    target.unlink()
                source.rename(target)

    def write(self, event: dict[str, Any]) -> None:
        line = json.dumps(event, ensure_ascii=False, default=_json_default, sort_keys=False)
        if self.path is None:
            print(line, file=sys.stdout, flush=True)
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.rotate()
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def default_sink_path() -> Path:
    """Path del sink di default sul volume (config -> fallback locale)."""
    try:
        from config import DATA_DIR
        base = Path(DATA_DIR)
    except Exception:
        base = Path("data")
    return base / "decision" / "events.jsonl"


def sink_from_env(value: Optional[str] = None) -> EventSink:
    """Sink da env `DECISION_LOG_SINK` (default: JSONL sul volume)."""
    raw = (value if value is not None else os.getenv(SINK_ENV, "")).strip()
    lowered = raw.lower()
    if lowered in ("off", "none", "null", "0", "false"):
        return NullSink()
    if lowered in ("stdout", "-"):
        return JsonlSink(None)
    if raw:
        return JsonlSink(raw)
    return JsonlSink(default_sink_path())


def observability_enabled(value: Optional[str] = None) -> bool:
    raw = value if value is not None else os.getenv(ENABLED_ENV, "")
    if not raw:
        # Senza env: attiva solo se il sink non e' esplicitamente spento.
        return sink_from_env().name != "null"
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


# ---------------------------------------------------------------------------
# Trace / span
# ---------------------------------------------------------------------------

class TraceContext:
    """Identita' del punto corrente: request -> trace -> span."""

    __slots__ = ("request_id", "trace_id", "span_id", "parent_span_id")

    def __init__(self, *, request_id: str = "", trace_id: str = "",
                 span_id: str = "", parent_span_id: str = "") -> None:
        self.request_id = request_id or new_id()
        self.trace_id = trace_id or new_id()
        self.span_id = span_id or new_id()
        self.parent_span_id = parent_span_id or ""

    def child(self) -> "TraceContext":
        """Nuovo span figlio dello span corrente (stesso request e trace)."""
        return TraceContext(request_id=self.request_id, trace_id=self.trace_id,
                            parent_span_id=self.span_id)

    def same_trace(self) -> "TraceContext":
        """Stesso span, per passare il contesto a un altro componente."""
        return TraceContext(request_id=self.request_id, trace_id=self.trace_id,
                            span_id=self.span_id, parent_span_id=self.parent_span_id)

    def as_dict(self) -> dict[str, str]:
        return {"request_id": self.request_id, "trace_id": self.trace_id,
                "span_id": self.span_id, "parent_span_id": self.parent_span_id}

    def __repr__(self) -> str:  # pragma: no cover - diagnostica
        return (f"TraceContext(request={self.request_id}, trace={self.trace_id}, "
                f"span={self.span_id}, parent={self.parent_span_id or '-'})")


class Observability:
    """Il middleware: emette eventi JSON verso un sink configurabile."""

    def __init__(self, *, sink: Optional[EventSink] = None,
                 component: str = DEFAULT_COMPONENT,
                 limits: Any = None, extra: Optional[Mapping[str, Any]] = None,
                 enabled: Optional[bool] = None) -> None:
        self.sink: EventSink = sink or sink_from_env()
        self.component = component
        if enabled is not None:
            self.enabled = bool(enabled)
        elif sink is not None:
            # Un sink INIETTATO (test, tool, pagina) vince sull'env: chi passa
            # un sink vuole vederlo scrivere, anche con `DECISION_LOG_SINK=off`.
            self.enabled = True
        else:
            self.enabled = observability_enabled()
        # Il config hash si calcola UNA volta per istanza: e' la fotografia
        # della configurazione con cui questo processo decide.
        self.config_fingerprint = config_hash(limits, extra=extra)

    # -- eventi ----------------------------------------------------------
    def event(self, name: str, *, ctx: Optional[TraceContext] = None,
              outcome: str = "", stage: str = "", details: Optional[Mapping[str, Any]] = None,
              **fields: Any) -> dict[str, Any]:
        """Emette un evento. Non solleva MAI (fail-safe)."""
        event: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": name,
            "component": self.component,
            "config_hash": self.config_fingerprint,
        }
        if ctx is not None:
            event.update(ctx.as_dict())
        if stage:
            event["stage"] = stage
        if outcome:
            event["outcome"] = outcome
        event.update({k: v for k, v in fields.items() if v not in (None, "", [], {})})
        if details:
            event["details"] = redact(details)
        self._write(event)
        return event

    def _write(self, event: dict[str, Any]) -> None:
        if not self.enabled:
            return
        try:
            self.sink.write(event)
        except Exception as exc:                 # sink rotto: non fermare nulla
            if self.sink.name not in _warned:
                _warned.add(self.sink.name)
                logger.warning("middleware: sink '%s' non scrivibile (%s): "
                               "gli eventi vengono scartati", self.sink.name, exc)

    # -- span ------------------------------------------------------------
    def new_trace(self, *, request_id: str = "", trace_id: str = "") -> TraceContext:
        return TraceContext(request_id=request_id, trace_id=trace_id)

    @contextmanager
    def span(self, name: str, *, ctx: Optional[TraceContext] = None,
             stage: str = "", details: Optional[Mapping[str, Any]] = None,
             **fields: Any) -> Iterator[TraceContext]:
        """Contesto di uno span: start, end (con durata) o error. Ri-solleva."""
        scope = ctx.child() if ctx is not None else self.new_trace()
        started = time.perf_counter()
        # NB: il campo del nome e' `span_name` perche' `name` e' gia' il nome
        # dell'evento (conflitto trovato dai test appena scritti).
        self.event("span.start", ctx=scope, stage=stage, span_name=name,
                   details=details, **fields)
        try:
            yield scope
        except Exception as exc:
            self.event("span.error", ctx=scope, stage=stage, span_name=name,
                       outcome="error",
                       duration_ms=round((time.perf_counter() - started) * 1000, 3),
                       error=f"{type(exc).__name__}: {exc}")
            raise                                    # il fail-fast resta fail-fast
        else:
            self.event("span.end", ctx=scope, stage=stage, span_name=name,
                       outcome="ok",
                       duration_ms=round((time.perf_counter() - started) * 1000, 3))


def default_observability(**kwargs: Any) -> Observability:
    """Middleware con la configurazione di ambiente (usato dai job)."""
    return Observability(**kwargs)


__all__ = [
    "DEFAULT_COMPONENT", "DEFAULT_MAX_MB", "ENABLED_ENV", "EventSink", "GENERATIONS",
    "JsonlSink", "ListSink", "MAX_MB_ENV", "NullSink", "Observability",
    "REDACTED_FIELDS", "SINK_ENV", "TraceContext",
    "config_hash", "default_observability", "default_sink_path", "new_id",
    "observability_enabled", "redact", "sink_from_env",
]
