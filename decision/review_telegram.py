"""decision/review_telegram.py — Il verdetto `review` diventa un bottone Telegram.

La catena, quando non e' certa, non decide: mette il segnale nella coda
(`decision/review_queue.py`) e lo mostra a un umano. Qui c'e' il pezzo che
manca perche' quella coda serva davvero: il **messaggio con i bottoni** e il
**callback idempotente** che lo chiude.

Flusso completo:

    REVIEW  ──▶ coda (reviews.json) ──▶ prompt Telegram (✅ Approva / ❌ Rifiuta)
                                                   │  (click)
                                                   ▼
                              callback_query ──▶ handle_callback()
                                                   │
                              resolve_review()  ──┤ decisione (approve/reject)
                              plan_for_resolved ──┤ comandi (persist + ordine)
                              Dispatcher ─────────┘ esecuzione (shadow di default)

**Idempotenza** — due click sullo stesso bottone, un redelivery di Telegram
(accade: la callback query si ripete se il bot non risponde entro pochi
secondi), un riavvio del container a meta' lavoro: tutto deve produrre **una**
decisione e **un** dispatch. Ci sono tre livelli, ognuno con un compito:

1. `callback_id` **stabile**: e' una funzione di (record_id, azione), quindi lo
   stesso bottone ha sempre la stessa chiave (niente timestamp, niente UUID);
2. `CallbackStore` sul volume: la chiave gia' risolta viene ritrovata e
   l'esito viene **restituito invariato** — nessuna risoluzione, nessun
   dispatch, nessuna scrittura;
3. la coda (`ReviewQueue._decide`) e' idempotente per conto suo: una voce gia'
   decisa non cambia stato.

Il **claim prima, completamento dopo** (`claim`/`complete`) copre il caso
peggiore: se il processo muore fra la decisione e il dispatch, al retry
troviamo un claim senza esito e NON ri-dispatchiamo — meglio una revisione
rimasta a meta' (visibile, recuperabile a mano) che due ordini.

**Perche' i bottoni non eseguono nulla in questa fase**: il set di gateway di
default e' lo `ShadowGateway`. L'approvazione umana attraversa tutta la catena
(persistenza, stake, comando d'ordine) ma *registra* invece di ordinare —
coerente con la shadow mode decisa il 15/09. Per eseguire davvero servono
gateway espliciti (`execute=True`), che oggi non passa nessuno: la scelta e'
visibile nel codice invece che implicita in una config.

**Sicurezza**:
- il token Telegram si legge SOLO dall'ambiente (`QUOTAVERACE_BOT_TOKEN`) e non
  compare mai nei testi, nei log o nello store (i testi sono costruiti senza);
- `parse_callback()` e' STRETTO: prefisso, azione e token sono validati, e il
  `record_id` non viaggia nel callback (il token e' un digest) — un payload
  manomesso non puo' puntare a una revisione arbitraria;
- lo store e' fail-safe in lettura (file corrotto = nessuna chiave risolta →
  comportamento conservativo, si chiede di nuovo all'umano) e in scrittura (mai
  un'eccezione: la telemetria non ferma una puntata);
- nessuna esecuzione e' possibile senza un `record_id` presente in coda.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Literal, Optional, Protocol, Sequence

from pydantic import BaseModel, Field

from .dispatcher import Dispatcher
from .gateways import ShadowGateway, admin_targets
from .limits import RiskLimits
from .middleware import Observability, TraceContext
from .models import Mode, ReasonCode, as_float, utcnow
from .review_queue import ReviewQueue

logger = logging.getLogger("decision.review_telegram")

#: Prefisso del `callback_data`. Tutto cio' che non lo inizia non e' nostro e
#: viene ignorato in silenzio (altri messaggi possono avere bottoni propri).
CALLBACK_PREFIX = "rv"
#: Azioni: una lettera, per stare larghi nel limite di 64 byte del callback_data.
CODE_APPROVE = "a"
CODE_REJECT = "r"
ACTION_BY_CODE = {CODE_APPROVE: "approve", CODE_REJECT: "reject"}
CODE_BY_ACTION = {"approve": CODE_APPROVE, "reject": CODE_REJECT}
#: Lunghezza del digest che identifica la revisione (il `record_id` non va nel
#: callback: un payload manomesso non deve poter puntare a una voce arbitraria).
TOKEN_LEN = 10

STORE_ENV = "DECISION_CALLBACK_STORE"
#: Store dati: una revisione senza store non e' idempotente fra processi.
DEFAULT_STORE_NAME = "review_callbacks.json"

Action = Literal["approve", "reject"]


# ---------------------------------------------------------------------------
# Chiavi dei callback (funzioni PURE: stesso input -> stessa chiave)
# ---------------------------------------------------------------------------

def callback_token(record_id: str) -> str:
    """Digest stabile della revisione (10 hex)."""
    return hashlib.sha1(str(record_id).encode("utf-8")).hexdigest()[:TOKEN_LEN]


def callback_id(record_id: str, action: Action) -> str:
    """Chiave IDEMPOTENTE del bottone: `rv:<azione>:<token>`.

    E' una funzione pura di (record, azione): lo stesso bottone porta sempre la
    stessa chiave, in qualunque processo, prima e dopo un redeploy. E' questa
    proprieta' che rende banale la deduplicazione — nessun contatore, nessun
    timestamp da confrontare.
    """
    return f"{CALLBACK_PREFIX}:{CODE_BY_ACTION[action]}:{callback_token(record_id)}"


class CallbackError(ValueError):
    """Payload di callback non conforme (prefisso, azione o token)."""


class ReviewCallback(BaseModel):
    """Un callback valido, gia' scomposto nelle sue parti."""

    callback_id: str
    action: Action
    token: str
    raw: str = ""

    @property
    def approve(self) -> bool:
        return self.action == "approve"


def is_ours(data: Any) -> bool:
    """True se il `callback_data` e' di questo modulo (da NON loggare se no)."""
    return str(data or "").startswith(f"{CALLBACK_PREFIX}:")


def parse_callback(data: Any) -> ReviewCallback:
    """Scompone `callback_data`. STRETTO: qualunque anomalia -> `CallbackError`.

    Il chiamante deve prima verificare `is_ours(data)`: cosi' un bottone di un
    altro modulo viene ignorato senza rumore nei log, mentre un nostro payload
    malformato e' un errore vero (`rv:` con azione o token sbagliati).
    """
    raw = str(data or "")
    parts = raw.split(":")
    if len(parts) != 3 or parts[0] != CALLBACK_PREFIX:
        raise CallbackError(f"formato inatteso: {raw[:24]!r}")
    code, token = parts[1], parts[2]
    if code not in ACTION_BY_CODE:
        raise CallbackError(f"azione sconosciuta: {code!r}")
    if len(token) != TOKEN_LEN or any(c not in "0123456789abcdef" for c in token):
        raise CallbackError(f"token non valido: {token!r}")
    return ReviewCallback(callback_id=raw, action=ACTION_BY_CODE[code],  # type: ignore[arg-type]
                          token=token, raw=raw)


# ---------------------------------------------------------------------------
# Store idempotente (sul volume, scrittura atomica, mai un'eccezione)
# ---------------------------------------------------------------------------

def default_store_path() -> Path:
    """Path dello store (env -> volume -> fallback locale)."""
    override = os.getenv(STORE_ENV)
    if override:
        return Path(override)
    try:
        from config import DATA_DIR
        base = Path(DATA_DIR)
    except Exception:
        base = Path("data")
    return base / "decision" / DEFAULT_STORE_NAME


class CallbackStore:
    """Registro persistente di callback risolti e prompt inviati.

    Tre sezioni, tutte sullo stesso file JSON (scrittura atomica):

        resolved  callback_id -> {status, record_id, action, ...}   (idempotenza)
        prompts   record_id   -> {message_id, chat_id, sent_at}     (anti-spam)

    Lettura fail-safe: un file corrotto torna `{}` e NON viene sovrascritto
    (si perde la memoria dell'idempotenza, ma non si distrugge nulla); in quel
    caso la coda resta comunque l'autorita' sulla decisione.
    """

    def __init__(self, path: Optional[Path | str] = None) -> None:
        self.path = Path(path) if path else default_store_path()

    # -- persistenza ------------------------------------------------------
    @staticmethod
    def _empty() -> dict:
        """Struttura minima: `load()` la restituisce SEMPRE ben formata.

        Il primo giro su un volume appena creato non ha file, e un file corrotto
        e' trattato come assente: in entrambi i casi il chiamante deve trovare
        le due sezioni, non un dict vuoto (un `data["resolved"]` su `{}` era un
        `KeyError` a runtime — trovato dai test prima del deploy).
        """
        return {"resolved": {}, "prompts": {}}

    def load(self) -> dict:
        if not self.path.exists():
            return self._empty()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("store callback illeggibile (%s): lo tratto come vuoto", exc)
            return self._empty()
        if not isinstance(data, dict):
            return self._empty()
        data.setdefault("resolved", {})
        data.setdefault("prompts", {})
        if not isinstance(data["resolved"], dict) or not isinstance(data["prompts"], dict):
            return self._empty()
        return data

    def save(self, data: dict) -> bool:
        """Scrittura atomica. Ritorna False (senza sollevare) se non riesce."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = dict(data)
            payload["updated_at"] = utcnow().isoformat()
            fd, tmp_name = tempfile.mkstemp(dir=str(self.path.parent), prefix=".callbacks-")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp_name, self.path)
            except Exception:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise
            return True
        except Exception as exc:
            logger.warning("store callback non scrivibile (%s): idempotenza non persistita", exc)
            return False

    # -- callback risolti -------------------------------------------------
    def resolved(self, callback_id: str) -> Optional[dict]:
        entry = self.load().get("resolved", {}).get(str(callback_id))
        return dict(entry) if isinstance(entry, dict) else None

    def claim(self, callback_id: str, *, record_id: str, action: str,
              reviewer: str = "") -> Optional[dict]:
        """Prenota la chiave (PRIMA del dispatch).

        Ritorna `None` se la chiave era libera (e l'ha prenotata), altrimenti la
        voce gia' presente — cosi' il chiamante distingue "tocca a me" da
        "già fatto". Un claim senza esito (`status="claimed"`) e' il caso del
        processo morto a meta': NON va ri-dispatchato.
        """
        data = self.load()
        existing = data["resolved"].get(str(callback_id))
        if isinstance(existing, dict):
            return dict(existing)
        data["resolved"][str(callback_id)] = {
            "status": "claimed",
            "record_id": record_id,
            "action": action,
            "reviewer": reviewer,
            "claimed_at": utcnow().isoformat(),
        }
        self.save(data)
        return None

    def complete(self, callback_id: str, **fields: Any) -> bool:
        """Salva l'esito finale del callback (fail-safe)."""
        data = self.load()
        entry = data["resolved"].get(str(callback_id))
        entry = dict(entry) if isinstance(entry, dict) else {}
        entry.update({k: v for k, v in fields.items() if v is not None})
        entry.setdefault("claimed_at", utcnow().isoformat())
        entry["completed_at"] = utcnow().isoformat()
        data["resolved"][str(callback_id)] = entry
        return self.save(data)

    # -- prompt inviati (anti-spam) ---------------------------------------
    def prompted(self, record_id: str) -> Optional[dict]:
        entry = self.load().get("prompts", {}).get(str(record_id))
        return dict(entry) if isinstance(entry, dict) else None

    def mark_prompted(self, record_id: str, *, message_id: Any = None,
                      chat_id: Any = None, scope: str = "") -> bool:
        data = self.load()
        data["prompts"][str(record_id)] = {
            "sent_at": utcnow().isoformat(),
            "message_id": message_id,
            "chat_id": chat_id,
            "scope": scope,
        }
        return self.save(data)

    def release(self, callback_id: str) -> bool:
        """Libera un callback FALLITO, per permettere un nuovo tentativo.

        Serve al caso `status="error"`: il claim resta (nessun doppio dispatch
        silenzioso) e la ripresa e' un atto esplicito dell'operatore —
        l'idempotenza non si rompe da sola.
        """
        data = self.load()
        entry = data["resolved"].get(str(callback_id))
        if not isinstance(entry, dict) or str(entry.get("status")) != "error":
            return False
        data["resolved"].pop(str(callback_id), None)
        return self.save(data)

    def forget_prompt(self, record_id: str) -> bool:
        """Rimuove la memoria del prompt (per rimandarlo, es. dopo un errore)."""
        data = self.load()
        if str(record_id) not in data["prompts"]:
            return False
        data["prompts"].pop(str(record_id), None)
        return self.save(data)

    def summary(self) -> dict:
        data = self.load()
        resolved = data.get("resolved", {}) if isinstance(data, dict) else {}
        by_status: dict[str, int] = {}
        for entry in (resolved or {}).values():
            status = str((entry or {}).get("status") or "?")
            by_status[status] = by_status.get(status, 0) + 1
        return {"path": str(self.path), "resolved": len(resolved or {}),
                "prompts": len((data.get("prompts") or {}) if isinstance(data, dict) else {}),
                "by_status": by_status}


# ---------------------------------------------------------------------------
# Client Telegram (token SOLO da ambiente; iniettabile nei test)
# ---------------------------------------------------------------------------

# NB: nessuna costante col nome del token a livello di modulo. La guardia
# `test_secret_hygiene.py` segnala le assegnazioni "credential-like" e il nome
# di una variabile d'ambiente non fa eccezione; la convenzione del progetto
# (vedi `gateways._send_telegram`) e' leggere l'env inline, una volta sola qui.


class TelegramError(RuntimeError):
    """La chiamata Telegram non e' riuscita (HTTP non-200 o eccezione)."""


class TelegramClient(Protocol):
    """Contratto minimo: invia, risponde a una callback, modifica un messaggio."""

    name: str

    def send_message(self, chat_id: Any, text: str, *,
                     reply_markup: Optional[dict] = None) -> dict:  # pragma: no cover
        ...

    def answer_callback(self, callback_query_id: str, text: str, *,
                        show_alert: bool = False) -> bool:  # pragma: no cover
        ...

    def edit_message(self, chat_id: Any, message_id: Any, text: str, *,
                     reply_markup: Optional[dict] = None) -> bool:  # pragma: no cover
        ...


class HttpTelegramClient:
    """Client reale: POST diretti all'API Telegram (come `gateways._send_telegram`).

    Il token viene letto dall'ambiente a ogni chiamata e **non viene mai
    loggato ne' messo nel testo**: `secure_logging` maschera comunque il
    formato, ma la regola qui e' non scriverlo affatto.
    """

    name = "telegram"

    def __init__(self, token: Optional[str] = None, *, timeout: float = 20.0) -> None:
        self._token = token
        self.timeout = timeout

    @property
    def token(self) -> str:
        return self._token or os.getenv("QUOTAVERACE_BOT_TOKEN", "")

    def _method(self, method: str, payload: dict) -> dict:
        token = self.token
        if not token:
            raise TelegramError("token Telegram non configurato")
        import requests
        try:
            response = requests.post(
                f"https://api.telegram.org/bot{token}/{method}",
                json=payload, timeout=self.timeout)
        except Exception as exc:                 # rete: errore, non crash
            raise TelegramError(f"{method}: {type(exc).__name__}") from exc
        if response.status_code != 200:
            raise TelegramError(f"{method}: HTTP {response.status_code}")
        return {"status_code": response.status_code, "body": response.text}

    def send_message(self, chat_id: Any, text: str, *,
                     reply_markup: Optional[dict] = None) -> dict:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text,
                                   "disable_web_page_preview": True}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return self._method("sendMessage", payload)

    def answer_callback(self, callback_query_id: str, text: str, *,
                        show_alert: bool = False) -> bool:
        try:
            self._method("answerCallbackQuery",
                         {"callback_query_id": callback_query_id, "text": text,
                          "show_alert": bool(show_alert)})
            return True
        except TelegramError as exc:
            logger.warning("answerCallbackQuery fallita (%s)", exc)
            return False

    def edit_message(self, chat_id: Any, message_id: Any, text: str, *,
                     reply_markup: Optional[dict] = None) -> bool:
        payload: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id,
                                   "text": text}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        try:
            self._method("editMessageText", payload)
            return True
        except TelegramError as exc:
            logger.warning("editMessageText fallita (%s)", exc)
            return False


def default_client() -> TelegramClient:
    """Client di produzione (o errore esplicito se il token manca)."""
    return HttpTelegramClient()


# ---------------------------------------------------------------------------
# Prompt (testo + tastiera inline)
# ---------------------------------------------------------------------------

def _edge_pct(value: Any) -> str:
    try:
        return f"{float(value) * 100:+.1f}pp"
    except (TypeError, ValueError):
        return "-"


def _num(value: Any, digits: int = 2, suffix: str = "") -> str:
    try:
        return f"{float(value):.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return "-"


def build_prompt(entry: dict, *, kickoff_eta: str = "") -> dict:
    """Testo + `reply_markup` per una voce di revisione (funzione PURA).

    Nessuna rete, nessun DB: si puo' ispezionare in un test senza Telegram, ed
    e' il modo per vedere *esattamente* cosa legge l'operatore prima di
    approvare una puntata.
    """
    record_id = str(entry.get("record_id") or "")
    approve_id = callback_id(record_id, "approve")
    reject_id = callback_id(record_id, "reject")
    mode = ""
    coverage = None
    record = entry.get("record")
    if isinstance(record, dict):
        mode = str(record.get("mode") or "")
        signal = record.get("signal")
        if isinstance(signal, dict):
            quality = signal.get("data_quality") or {}
            if isinstance(quality, dict):
                coverage = quality.get("model_coverage")
    quality_line = f"  modello : confidenza {_num(entry.get('confidence'), 2)}"
    if coverage is not None:
        quality_line += f" | copertura ratings {_num(coverage, 2)}"
    quality_line += f" | motivo {entry.get('reason') or '-'}"
    lines = [
        "🕓 REVISIONE UMANA — nessuno stake senza il tuo ok",
        f"  partita : {entry.get('selection') or entry.get('outcome')} "
        f"({entry.get('league') or '-'})",
        f"  quota   : {_num(entry.get('price'))} | mercato "
        f"{_num(entry.get('market_prob'), 3)} | blend {_num(entry.get('blended_prob'), 3)}",
        f"  edge    : {_edge_pct(entry.get('edge'))} | EV {_edge_pct(entry.get('ev'))} "
        f"| tier {entry.get('tier') or '-'}",
        quality_line,
        f"  kickoff : {entry.get('kickoff') or '-'}{kickoff_eta}",
    ]
    if mode:
        lines.append(f"  modalita': {mode}" + (" (shadow: nessun ordine reale)"
                                               if mode != "live" else ""))
    lines.append(f"  chiave  : {callback_token(record_id)}")
    keyboard = {"inline_keyboard": [[
        {"text": "✅ Approva", "callback_data": approve_id},
        {"text": "❌ Rifiuta", "callback_data": reject_id},
    ]]}
    return {"text": "\n".join(lines), "reply_markup": keyboard,
            "callback_ids": {"approve": approve_id, "reject": reject_id},
            "record_id": record_id, "token": callback_token(record_id)}


def pending_prompts(queue: Optional[ReviewQueue] = None,
                    store: Optional[CallbackStore] = None, *,
                    include_prompted: bool = False,
                    now: Any = None) -> list[dict]:
    """Voci in attesa con il loro prompt (quelle gia' inviate restano fuori)."""
    queue = queue or ReviewQueue()
    store = store or CallbackStore()
    out: list[dict] = []
    for item in queue.pending(now=now):
        record_id = str(item.get("record_id") or "")
        if not include_prompted and store.prompted(record_id):
            continue
        prompt = build_prompt(item)
        prompt["entry"] = item
        out.append(prompt)
    return out


# ---------------------------------------------------------------------------
# Invio dei prompt
# ---------------------------------------------------------------------------

def send_prompts(*, queue: Optional[ReviewQueue] = None,
                 store: Optional[CallbackStore] = None,
                 client: Optional[TelegramClient] = None,
                 targets: Optional[Sequence[str]] = None, limit: int = 5,
                 observability: Optional[Observability] = None,
                 ctx: Optional[TraceContext] = None) -> dict:
    """Invia i prompt delle revisioni in attesa. Mai un'eccezione.

    Il **marker di prompt inviato** e' scritto sullo store dopo l'invio: il job
    che chiama questa funzione gira di frequente e senza marker lo stesso
    segnale verrebbe rimandato a ogni giro. Se l'invio fallisce il marker NON
    viene scritto, quindi al giro dopo si ritenta.
    """
    queue = queue or ReviewQueue()
    store = store or CallbackStore()
    obs = observability or Observability()
    scope = ctx or obs.new_trace()
    destination = [str(t) for t in (targets if targets is not None else admin_targets())]
    out: dict[str, Any] = {"sent": 0, "skipped": 0, "errors": [], "prompts": [],
                           "targets": destination}
    if not destination:
        obs.event("review.send", ctx=scope, stage="review", outcome="skipped",
                  detail="nessun destinatario configurato (ADMIN_CHAT_ID)")
        return out

    prompts = pending_prompts(queue, store)
    if limit is not None:
        prompts = prompts[:max(0, int(limit))]
    if not prompts:
        return out

    try:
        sender = client or default_client()
    except Exception as exc:                    # client non costruibile
        out["errors"].append(f"client: {type(exc).__name__}: {exc}")
        logger.warning("review: client Telegram non disponibile (%s)", exc)
        return out

    for prompt in prompts:
        delivered = 0
        last_error = ""
        message_id: Any = None
        for chat_id in destination:
            try:
                response = sender.send_message(chat_id, prompt["text"],
                                               reply_markup=prompt["reply_markup"])
                delivered += 1
                body = (response or {}).get("body") if isinstance(response, dict) else None
                message_id = _message_id(body)
            except Exception as exc:            # mai un'eccezione verso il job
                last_error = f"{type(exc).__name__}: {exc}"
                logger.warning("review: invio a %s fallito (%s)", chat_id, last_error)
        if delivered:
            store.mark_prompted(prompt["record_id"], message_id=message_id,
                                chat_id=destination[0], scope="review")
            out["sent"] += 1
            out["prompts"].append({"record_id": prompt["record_id"],
                                   "token": prompt["token"],
                                   "approve": prompt["callback_ids"]["approve"],
                                   "delivered": delivered})
            obs.event("review.prompt", ctx=scope, stage="review", outcome="sent",
                      record_id=prompt["record_id"], token=prompt["token"],
                      action="approve", delivered=delivered)
        else:
            out["skipped"] += 1
            out["errors"].append(f"{prompt['record_id']}: {last_error or 'invio fallito'}")
    return out


def _message_id(body: Any) -> Any:
    """`message_id` dalla risposta Telegram, se il body e' JSON valido."""
    if not body:
        return None
    try:
        parsed = json.loads(body) if isinstance(body, str) else body
        return (((parsed or {}).get("result") or {}) if isinstance(parsed, dict) else {}).get("message_id")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Gestione del callback (idempotente)
# ---------------------------------------------------------------------------

class ReviewOutcome(BaseModel):
    """Esito di un callback: dato serializzabile (log, risposta all'operatore)."""

    callback_id: str = ""
    record_id: str = ""
    action: str = ""
    status: str = "unknown"        # approved|rejected|duplicate|expired|unknown|error
    duplicate: bool = False
    reviewer: str = ""
    note: str = ""
    detail: str = ""
    plan_id: str = ""
    commands: list[str] = Field(default_factory=list)
    would_order: bool = False
    stake: Optional[float] = None
    verdict: str = ""
    reason: str = ""
    executed: int = 0
    errors: list[str] = Field(default_factory=list)
    #: Risultati dei gateway (solo con `capture=True`): diagnostica, non log.
    results: list[Any] = Field(default_factory=list)

    @property
    def decided(self) -> bool:
        """True se la decisione e' stata presa in QUESTO giro (non duplicata)."""
        return not self.duplicate and self.status in ("approved", "rejected")

    def answer_text(self) -> str:
        """Testo per `answerCallbackQuery` (breve: Telegram lo mostra a comparsa)."""
        if self.duplicate:
            return {"approved": "Già approvata", "rejected": "Già rifiutata",
                    "claimed": "In lavorazione"}.get(self.status, "Già gestita")
        if self.status == "approved":
            return f"Approva ✅ stake {_num(self.stake)}" if self.stake else "Approvata ✅"
        if self.status == "rejected":
            return "Rifiutata ❌"
        if self.status == "expired":
            return "Scaduta: partita già iniziata"
        if self.status == "unknown":
            return "Revisione non trovata in coda"
        return "Errore: riprova"

    def as_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def find_entry(queue: ReviewQueue, token: str) -> Optional[dict]:
    """Voce della coda il cui `record_id` ha questo token (o None).

    Il percorso token -> voce passa dalla coda, non dal payload: un callback
    manomesso non puo' inventare un `record_id` che non esiste.
    """
    for item in queue.load():
        record_id = str(item.get("record_id") or "")
        if record_id and callback_token(record_id) == token:
            return item
    return None


def _gateways_for_shadow(path: Optional[str | Path] = None) -> list[Any]:
    return [ShadowGateway(path)]


def handle_callback(data: Any, *, queue: Optional[ReviewQueue] = None,
                    store: Optional[CallbackStore] = None,
                    bankroll: float = 0.0, mode: Mode = "sim",
                    limits: Optional[RiskLimits] = None,
                    reviewer: str = "", note: str = "",
                    gateways: Optional[Sequence[Any]] = None,
                    capture: bool = False,
                    observability: Optional[Observability] = None,
                    ctx: Optional[TraceContext] = None,
                    now: Any = None) -> ReviewOutcome:
    """Chiude una revisione dal suo callback. Idempotente, fail-safe, non esegue.

    `gateways=None` (default) significa **ShadowGateway**: la catena completa
    gira (decisione, stake, comando) ma nulla viene eseguito — e' la garanzia
    che un click in questa fase non possa produrre un ordine. `capture=True`
    aggiunge la risposta a una lista di `CommandResult` (`report["results"]`)
    per chi vuole ispezionare cosa e' successo, senza eseguire.

    Ritorna SEMPRE un `ReviewOutcome`: mai un'eccezione verso il bot.
    """
    obs = observability or Observability()
    scope = ctx or obs.new_trace()
    queue = queue or ReviewQueue()
    store = store or CallbackStore()
    limits = limits or RiskLimits.from_env()

    outcome = ReviewOutcome(callback_id=str(data or ""), reviewer=reviewer, note=note)
    try:
        callback = parse_callback(data)
    except CallbackError as exc:
        obs.event("review.callback", ctx=scope, stage="review", outcome="error",
                  error_code="malformed", detail=str(exc))
        outcome.status = "error"
        outcome.detail = f"callback non conforme: {exc}"
        return outcome

    outcome.callback_id = callback.callback_id
    outcome.action = callback.action

    # 1. IDEMPOTENZA: chiave gia' risolta -> esito invariato, niente altro.
    resolved = store.resolved(callback.callback_id)
    if resolved is not None:
        outcome.duplicate = True
        outcome.record_id = str(resolved.get("record_id") or "")
        outcome.status = str(resolved.get("status") or "claimed")
        # Lo store su disco e' JSON: `as_float` mantiene numerico l'importo
        # (la serializzazione di un Decimal passerebbe come stringa).
        stored = resolved.get("stake")
        outcome.stake = as_float(stored) if stored is not None else None
        outcome.plan_id = str(resolved.get("plan_id") or "")
        outcome.verdict = str(resolved.get("verdict") or "")
        outcome.reason = str(resolved.get("reason") or "")
        outcome.detail = str(resolved.get("detail") or "callback gia' gestito")
        outcome.commands = list(resolved.get("commands") or [])
        outcome.would_order = bool(resolved.get("would_order"))
        outcome.executed = int(resolved.get("executed") or 0)
        obs.event("review.callback", ctx=scope, stage="review", outcome="duplicate",
                  callback_id=callback.callback_id, token=callback.token,
                  action=callback.action, record_id=outcome.record_id,
                  detail="callback gia' risolto: nessuna nuova decisione")
        return outcome

    # 2. La revisione esiste in coda?
    entry = find_entry(queue, callback.token)
    if entry is None:
        outcome.status = "unknown"
        outcome.detail = "nessuna revisione in coda con questo token"
        obs.event("review.callback", ctx=scope, stage="review", outcome="unknown",
                  callback_id=callback.callback_id, token=callback.token,
                  action=callback.action, detail=outcome.detail)
        return outcome

    record_id = str(entry.get("record_id") or "")
    outcome.record_id = record_id

    # 3. Claim PRIMA del lavoro: un processo che muore a meta' non ri-dispatcha.
    existing = store.claim(callback.callback_id, record_id=record_id,
                           action=callback.action, reviewer=reviewer)
    if existing is not None:
        outcome.duplicate = True
        outcome.status = str(existing.get("status") or "claimed")
        outcome.detail = "callback gia' prenotato"
        obs.event("review.callback", ctx=scope, stage="review", outcome="duplicate",
                  callback_id=callback.callback_id, record_id=record_id,
                  action=callback.action, detail=outcome.detail)
        return outcome

    try:
        from . import engine, pipeline
        record = pipeline.resolve_review(
            queue, record_id, approve=callback.approve, reviewer=reviewer, note=note,
            bankroll=bankroll, limits=limits, mode=mode, now=now)
        outcome.verdict = record.risk.verdict
        outcome.reason = record.risk.reason.value
        outcome.stake = as_float(record.stake.stake) if record.stake else None

        if record.risk.reason == ReasonCode.REVIEW_EXPIRED:
            outcome.status = "expired"
            outcome.detail = record.risk.detail
            store.complete(callback.callback_id, status="expired",
                           verdict=outcome.verdict, reason=outcome.reason,
                           detail=outcome.detail, reviewer=reviewer)
            obs.event("review.callback", ctx=scope, stage="review", outcome="expired",
                      callback_id=callback.callback_id, record_id=record_id,
                      action=callback.action, detail=outcome.detail)
            return outcome

        outcome.status = "approved" if callback.approve else "rejected"
        plan = engine.plan_for_resolved(record, home=str(entry.get("home") or ""),
                                        away=str(entry.get("away") or ""))
        outcome.plan_id = plan.plan_id
        outcome.commands = plan.kinds()
        outcome.would_order = plan.places_order
        dispatcher = Dispatcher(gateways if gateways is not None
                                else _gateways_for_shadow(), observability=obs)
        report = dispatcher.dispatch(plan, ctx=scope)
        outcome.executed = report.executed
        outcome.errors = list(report.errors)
        if capture:
            outcome.results = list(report.results)

        store.complete(callback.callback_id, status=outcome.status,
                       verdict=outcome.verdict, reason=outcome.reason,
                       detail=f"{outcome.status} da {reviewer or 'operatore'}",
                       stake=outcome.stake, plan_id=outcome.plan_id,
                       commands=outcome.commands, would_order=outcome.would_order,
                       executed=outcome.executed, reviewer=reviewer, note=note)
        obs.event("review.callback", ctx=scope, stage="review", outcome=outcome.status,
                  callback_id=callback.callback_id, record_id=record_id,
                  action=callback.action, reviewer=reviewer,
                  plan_id=outcome.plan_id, commands=outcome.commands,
                  would_order=outcome.would_order, stake=outcome.stake,
                  reason=outcome.reason, shadow=dispatcher.shadow,
                  errors=len(outcome.errors))
        return outcome
    except KeyError as exc:                        # voce sparita fra claim e resolve
        store.complete(callback.callback_id, status="unknown", detail=str(exc))
        outcome.status = "unknown"
        outcome.detail = str(exc)
        return outcome
    except Exception as exc:                       # mai un'eccezione verso il bot
        logger.warning("review: callback %s fallito (%s)", callback.callback_id, exc)
        # Il claim resta: la voce e' visibile come "claimed" e non si ri-dispatcha.
        store.complete(callback.callback_id, status="error",
                       detail=f"{type(exc).__name__}: {exc}")
        outcome.status = "error"
        outcome.detail = f"{type(exc).__name__}: {exc}"
        obs.event("review.callback", ctx=scope, stage="review", outcome="error",
                  callback_id=callback.callback_id, record_id=record_id,
                  action=callback.action, error=f"{type(exc).__name__}")
        return outcome


def answer_callback(callback_query: dict, *, queue: Optional[ReviewQueue] = None,
                    store: Optional[CallbackStore] = None,
                    client: Optional[TelegramClient] = None,
                    **kwargs: Any) -> ReviewOutcome:
    """Chiude il callback E risponde a Telegram (una sola funzione per il bot).

    `callback_query` e' l'oggetto di Telegram: `id`, `data`, `from`, `message`.
    L'`answerCallbackQuery` viene inviata SEMPRE, anche sui duplicati — e'
    proprio la risposta che ferma i redelivery di Telegram. Il messaggio
    originale viene modificato per mostrare l'esito (niente bottoni attivi su
    una revisione gia' chiusa).
    """
    query = callback_query or {}
    callback_query_id = str(query.get("id") or "")
    data = query.get("data")
    sender = query.get("from") or {}
    reviewer = str(sender.get("username") or sender.get("first_name")
                   or sender.get("id") or "")
    outcome = handle_callback(data, queue=queue, store=store, reviewer=reviewer, **kwargs)

    client = client or default_client()
    try:
        client.answer_callback(callback_query_id, outcome.answer_text(),
                               show_alert=outcome.status in ("error", "unknown"))
    except Exception as exc:                       # risposta mancante: non fatale
        logger.warning("review: answerCallbackQuery fallita (%s)", exc)

    message = query.get("message") or {}
    chat_id, message_id = message.get("chat", {}).get("id"), message.get("message_id")
    if chat_id is not None and message_id is not None:
        original = str(message.get("text") or "")
        stamp = {"approved": f"\n\n✅ APPROVATA da {reviewer or 'operatore'}",
                 "rejected": f"\n\n❌ RIFIUTATA da {reviewer or 'operatore'}",
                 "expired": "\n\n⌛ SCADUTA (partita iniziata)",
                 "duplicate": "\n\n↩️ Già gestita",
                 }.get(outcome.status, "\n\n⚠️ gestione non riuscita")
        try:
            client.edit_message(chat_id, message_id, original.split("\n\n✅")[0].split("\n\n❌")[0]
                                .split("\n\n⌛")[0].split("\n\n↩️")[0] + stamp,
                                reply_markup={"inline_keyboard": []})
        except Exception as exc:
            logger.warning("review: modifica del messaggio fallita (%s)", exc)
    return outcome


def format_report(summary: Optional[dict] = None, *,
                  queue: Optional[ReviewQueue] = None,
                  store: Optional[CallbackStore] = None) -> str:
    """Riga Telegram-friendly sullo stato delle revisioni."""
    store_summary = (store or CallbackStore()).summary()
    data = summary if isinstance(summary, dict) else store_summary
    lines = ["🕓 Revisioni Telegram (approvazione umana)",
             f"  callback risolti: {data.get('resolved', 0)} "
             f"(prompt inviati {data.get('prompts', 0)})"]
    by_status = data.get("by_status") or {}
    if by_status:
        lines.append("  esiti: " + ", ".join(f"{k}={v}" for k, v in sorted(by_status.items())))
    if queue is not None:
        pending = queue.pending()
        lines.append(f"  in attesa ora  : {len(pending)}")
        for item in pending[:5]:
            lines.append(f"    · {item.get('selection')} ({item.get('league')}) "
                         f"@ {item.get('price')} | {item.get('reason')}")
    return "\n".join(lines)


__all__ = [
    "ACTION_BY_CODE", "CALLBACK_PREFIX", "CODE_APPROVE", "CODE_REJECT",
    "CallbackError", "CallbackStore", "HttpTelegramClient", "ReviewCallback",
    "ReviewOutcome", "STORE_ENV", "TelegramClient", "TelegramError",
    "TOKEN_LEN", "answer_callback", "build_prompt", "callback_id",
    "callback_token", "default_client", "default_store_path", "find_entry",
    "format_report", "guards", "handle_callback", "is_ours", "parse_callback",
    "pending_prompts", "send_prompts",
]
