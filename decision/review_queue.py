"""decision/review_queue.py — Coda delle revisioni umane (persistente sul volume).

Semantica scelta il 14/09/2026: un segnale che passa tutti i gate ma ha
confidenza bassa **non si gioca da solo**: entra in coda e aspetta un ok umano
(Telegram). La coda e' un file JSON sul volume (`DATA_DIR/decision/reviews.json`,
override con `DECISION_REVIEW_QUEUE`), scritto in modo atomico come
`auto_bet_mode.json` cosi' sopravvive ai redeploy.

Regole:

- **Scadenza automatica al kickoff**: una revisione approvata DOPO l'inizio
  della partita sarebbe un ordine su un esito gia' in corso. Chi scade diventa
  `expired` e non e' piu' approvabile (fail-closed).
- **Idempotenza**: due click sullo stesso bottone non producono due decisioni;
  lo stato gia' deciso viene restituito invariato.
- **Fail-safe in lettura**: file corrotto o illeggibile -> coda vuota. La
  direzione e' sicura per costruzione: nessuna voce letta = nessuna
  approvazione = nessuna puntata (e il file non viene sovrascritto).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .models import DecisionRecord, ReasonCode, as_float, utcnow

logger = logging.getLogger("decision.review")

QUEUE_ENV = "DECISION_REVIEW_QUEUE"
STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_EXPIRED = "expired"


def default_path() -> Path:
    """Path della coda (env -> volume -> fallback locale)."""
    override = os.getenv(QUEUE_ENV)
    if override:
        return Path(override)
    try:
        from config import DATA_DIR
        base = Path(DATA_DIR)
    except Exception:
        base = Path("data")
    return base / "decision" / "reviews.json"


def _parse_dt(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class ReviewQueue:
    """Coda minimalista: aggiungi, elenca, approva, rifiuta, scadi."""

    def __init__(self, path: Optional[Path | str] = None) -> None:
        self.path = Path(path) if path else default_path()

    # -- persistenza ------------------------------------------------------
    def load(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("coda revisioni illeggibile (%s): la tratto come vuota", exc)
            return []
        entries = data.get("entries") if isinstance(data, dict) else data
        if not isinstance(entries, list):
            return []
        return [e for e in entries if isinstance(e, dict)]

    def save(self, entries: list[dict]) -> None:
        """Scrittura atomica (tmp nella stessa cartella + replace)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"updated_at": utcnow().isoformat(), "entries": entries}
        fd, tmp_name = tempfile.mkstemp(dir=str(self.path.parent), prefix=".reviews-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self.path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    # -- costruzione di una voce -----------------------------------------
    @staticmethod
    def entry(record: DecisionRecord) -> dict:
        signal = record.signal
        return {
            "record_id": record.record_id,
            "signal_id": signal.signal_id,
            "match_id": signal.match_id,
            "league": signal.league,
            "market": signal.market,
            "outcome": signal.outcome,
            "selection": signal.selection_label or signal.outcome,
            "kickoff": signal.kickoff.isoformat(),
            # La coda e' un JSON su disco: la quota esce NUMERO, non stringa.
            "price": as_float(signal.price),
            "market_prob": signal.market_prob,
            "blended_prob": signal.blended_prob,
            "edge": signal.edge,
            "ev": signal.ev,
            "tier": signal.tier,
            "confidence": signal.confidence,
            "reason": record.risk.reason.value,
            "detail": record.risk.detail,
            "status": STATUS_PENDING,
            "created_at": utcnow().isoformat(),
            "reviewer": None,
            "note": "",
            "reviewed_at": None,
            "record": record.model_dump(mode="json"),
        }

    # -- operazioni -------------------------------------------------------
    def add(self, record: DecisionRecord) -> dict:
        """Accoda una revisione. Idempotente per RECORD e per SEGNALE.

        La deduplicazione sul `signal_id` (15/09/2026) serve al caso reale: la
        catena valuta gli stessi segnali a ogni giro (il job gira ogni 60s),
        quindi il `record_id` — che porta i secondi — cambia ogni volta. Senza
        questo controllo la coda si riempirebbe di centinaia di copie dello
        stesso segnale e Telegram riceverebbe altrettanti prompt.

        La voce gia' decisa (approvata o rifiutata) viene RESTITUITA invariata:
        una revisione e' una per opportunita'. Se un umano ha detto no, quel
        segnale non torna a chiedere.
        """
        entries = self.load()
        for existing in entries:
            if existing.get("record_id") == record.record_id:
                return existing                       # idempotente
            if existing.get("signal_id") and existing.get("signal_id") == record.signal.signal_id:
                return existing                       # stessa opportunita'
        item = self.entry(record)
        entries.append(item)
        self.save(entries)
        return item

    def get(self, record_id: str) -> Optional[dict]:
        for item in self.load():
            if item.get("record_id") == record_id:
                return item
        return None

    def pending(self, now: Optional[datetime] = None, *, expire: bool = True) -> list[dict]:
        """Voci ancora decidibili (le scadute vengono marcate se `expire`)."""
        moment = now or utcnow()
        entries = self.load()
        changed = False
        out: list[dict] = []
        for item in entries:
            if item.get("status") != STATUS_PENDING:
                continue
            kickoff = _parse_dt(item.get("kickoff"))
            if expire and kickoff is not None and kickoff <= moment:
                item["status"] = STATUS_EXPIRED
                item["reviewed_at"] = moment.isoformat()
                changed = True
                continue
            out.append(item)
        if changed:
            self.save(entries)
        return out

    def expire(self, now: Optional[datetime] = None) -> list[str]:
        """Marca scadute le revisioni la cui partita e' iniziata."""
        moment = now or utcnow()
        entries = self.load()
        expired: list[str] = []
        for item in entries:
            if item.get("status") != STATUS_PENDING:
                continue
            kickoff = _parse_dt(item.get("kickoff"))
            if kickoff is not None and kickoff <= moment:
                item["status"] = STATUS_EXPIRED
                item["reviewed_at"] = moment.isoformat()
                expired.append(str(item.get("record_id")))
        if expired:
            self.save(entries)
        return expired

    def _decide(self, record_id: str, status: str, *, reviewer: str,
                note: str = "", now: Optional[datetime] = None) -> dict:
        moment = now or utcnow()
        entries = self.load()
        for item in entries:
            if item.get("record_id") != record_id:
                continue
            if item.get("status") != STATUS_PENDING:
                return item                          # idempotente
            kickoff = _parse_dt(item.get("kickoff"))
            if kickoff is not None and kickoff <= moment:
                item["status"] = STATUS_EXPIRED
                item["reviewed_at"] = moment.isoformat()
                self.save(entries)
                return item                          # mai decidere a partita iniziata
            item.update({
                "status": status,
                "reviewer": reviewer,
                "note": note,
                "reviewed_at": moment.isoformat(),
            })
            self.save(entries)
            return item
        raise KeyError(f"revisione sconosciuta: {record_id}")

    def approve(self, record_id: str, *, reviewer: str, note: str = "",
                now: Optional[datetime] = None) -> dict:
        return self._decide(record_id, STATUS_APPROVED, reviewer=reviewer, note=note, now=now)

    def reject(self, record_id: str, *, reviewer: str, note: str = "",
               now: Optional[datetime] = None) -> dict:
        return self._decide(record_id, STATUS_REJECTED, reviewer=reviewer, note=note, now=now)

    def record_for(self, item: dict) -> DecisionRecord:
        """Ricostruisce il DecisionRecord salvato (round-trip Pydantic)."""
        return DecisionRecord.model_validate(item["record"])

    def summary(self, now: Optional[datetime] = None) -> dict:
        moment = now or utcnow()
        entries = self.load()
        counts: dict[str, int] = {}
        for item in entries:
            status = str(item.get("status") or STATUS_PENDING)
            counts[status] = counts.get(status, 0) + 1
        pending = self.pending(now=moment)
        reasons: dict[str, int] = {}
        for item in pending:
            key = str(item.get("reason") or "?")
            reasons[key] = reasons.get(key, 0) + 1
        return {
            "total": len(entries),
            "by_status": counts,
            "pending": len(pending),
            "pending_reasons": reasons,
            "path": str(self.path),
        }


def format_report(summary: dict, queue: Optional[ReviewQueue] = None,
                  now: Optional[datetime] = None) -> str:
    """Report Telegram-friendly della coda."""
    lines = ["🕓 Revisioni in attesa (nessuno stake senza ok umano)"]
    lines.append(f"  in attesa  : {summary.get('pending', 0)} "
                 f"(totali {summary.get('total', 0)})")
    by_status = summary.get("by_status") or {}
    if by_status:
        lines.append("  stati      : " + ", ".join(f"{k}={v}" for k, v in sorted(by_status.items())))
    if queue is not None and summary.get("pending"):
        for item in queue.pending(now=now):
            lines.append(
                f"    · {item.get('selection')} ({item.get('league')}) @ {item.get('price')} "
                f"| edge {float(item.get('edge') or 0)*100:+.1f}pp "
                f"| conf {float(item.get('confidence') or 0):.2f} "
                f"| {item.get('reason')}")
    return "\n".join(lines)


__all__ = [
    "QUEUE_ENV", "ReviewQueue", "STATUS_APPROVED", "STATUS_EXPIRED", "STATUS_PENDING",
    "STATUS_REJECTED", "default_path", "format_report",
]
