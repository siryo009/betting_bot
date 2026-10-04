"""Test della coda delle revisioni umane (`decision/review_queue.py`) — OFFLINE.

Semantica: un segnale `review` non si gioca da solo, aspetta un ok umano; e un
ok umano arrivato DOPO il kickoff non vale (fail-closed).
"""

import json
from datetime import timedelta

import pytest

from decision import (
    KillSwitchStatus, ReasonCode, ReviewQueue, RiskLimits, decide, resolve_review,
    utcnow,
)
from decision.review_queue import format_report

BANKROLL = 1000.0


@pytest.fixture
def limits() -> RiskLimits:
    return RiskLimits.from_env()


@pytest.fixture
def queue(tmp_path) -> ReviewQueue:
    return ReviewQueue(tmp_path / "reviews.json")


def review_signal(kickoff_hours: float = 6.0, **kwargs):
    from test_decision_pipeline import make_signal
    signal = make_signal(confidence=0.20, **kwargs)     # sotto soglia -> review
    if kickoff_hours != 6.0:
        signal = signal.model_copy(update={"kickoff": utcnow() + timedelta(hours=kickoff_hours)})
    return signal


def enqueue(queue, limits, **kwargs):
    record = decide(review_signal(**kwargs), kills=KillSwitchStatus(mode="live"),
                    limits=limits, bankroll=BANKROLL, review_queue=queue)
    assert record.risk.verdict == "review"
    return record


class TestAccodamento:
    def test_il_record_entra_in_coda_senza_stake(self, queue, limits):
        record = enqueue(queue, limits)
        assert record.stake is None
        item = queue.get(record.record_id)
        assert item["status"] == "pending"
        assert item["reason"] == ReasonCode.CONFIDENCE_LOW.value
        assert item["selection"]

    def test_persistenza_su_nuova_istanza(self, queue, limits, tmp_path):
        record = enqueue(queue, limits)
        reopened = ReviewQueue(tmp_path / "reviews.json")
        assert reopened.get(record.record_id) is not None
        assert len(reopened.pending()) == 1

    def test_round_trip_del_record(self, queue, limits):
        record = enqueue(queue, limits)
        restored = queue.record_for(queue.get(record.record_id))
        assert restored.signal.signal_id == record.signal.signal_id
        assert restored.risk.reason is ReasonCode.CONFIDENCE_LOW
        assert restored.stake is None

    def test_doppio_add_non_duplica(self, queue, limits):
        record = enqueue(queue, limits)
        queue.add(record)
        queue.add(record)
        assert queue.summary()["total"] == 1

    def test_file_scritto_atomicamente_e_leggibile(self, queue, limits):
        enqueue(queue, limits)
        payload = json.loads(queue.path.read_text(encoding="utf-8"))
        assert isinstance(payload["entries"], list) and payload["updated_at"]

    def test_riepilogo_e_report(self, queue, limits):
        enqueue(queue, limits)
        report = format_report(queue.summary(), queue)
        assert "nessuno stake senza ok umano" in report
        assert "confidence_low" in report


class TestScadenza:
    def test_partita_iniziata_non_e_approvabile(self, queue, limits):
        record = enqueue(queue, limits, kickoff_hours=-1.0)     # kickoff passato
        assert queue.pending() == []                            # niente da revisionare
        assert queue.get(record.record_id)["status"] == "expired"

    def test_approvazione_tardiva_non_produce_stake(self, queue, limits):
        record = enqueue(queue, limits, kickoff_hours=-1.0)
        resolved = resolve_review(queue, record.record_id, approve=True,
                                  reviewer="admin", bankroll=BANKROLL, limits=limits)
        assert resolved.risk.verdict == "reject"
        assert resolved.risk.reason is ReasonCode.REVIEW_EXPIRED
        assert resolved.stake is None

    def test_expire_ritorna_gli_id(self, queue, limits):
        record = enqueue(queue, limits, kickoff_hours=-2.0)
        assert queue.expire() == [record.record_id]

    def test_pending_non_scade_il_futuro(self, queue, limits):
        record = enqueue(queue, limits, kickoff_hours=3.0)
        assert [item["record_id"] for item in queue.pending()] == [record.record_id]


class TestDecisione:
    def test_approvazione_umana_produce_stake(self, queue, limits):
        record = enqueue(queue, limits)
        resolved = resolve_review(queue, record.record_id, approve=True,
                                  reviewer="admin", note="ok manuale",
                                  bankroll=BANKROLL, limits=limits)
        assert resolved.risk.verdict == "approve"
        assert resolved.risk.reason is ReasonCode.REVIEW_APPROVED
        assert resolved.approved_by == "admin"
        assert resolved.review_note == "ok manuale"
        assert resolved.stake is not None and resolved.stake.executable is True
        assert resolved.stake.stake > 0
        assert queue.get(record.record_id)["status"] == "approved"

    def test_approvazione_puo_stringere_il_cap(self, queue, limits):
        record = enqueue(queue, limits)
        resolved = resolve_review(queue, record.record_id, approve=True,
                                  reviewer="admin", bankroll=BANKROLL, limits=limits,
                                  max_stake_pct=0.002)
        assert resolved.risk.tightened == {"max_stake_pct": 0.002}
        assert resolved.stake.stake <= BANKROLL * 0.002
        # CB1 (T-60, 17/09): il cap ASSOLUTO per ordine vale anche su un
        # stake dimensionato da un umano — l'approvazione puo' stringere,
        # mai superare il tetto del circuit breaker. Il cap che morde puo'
        # essere il tightened dell'umano (se piu' stretto del CB1) o CB1.
        from auto_bet import order_ceiling
        assert resolved.stake.stake <= order_ceiling(BANKROLL) + 1e-9
        assert resolved.stake.cap_source in ("risk", "t60_hard_cap")

    def test_rifiuto_umano(self, queue, limits):
        record = enqueue(queue, limits)
        resolved = resolve_review(queue, record.record_id, approve=False,
                                  reviewer="admin", note="partita ambigua",
                                  bankroll=BANKROLL, limits=limits)
        assert resolved.risk.verdict == "reject"
        assert resolved.risk.reason is ReasonCode.REVIEW_REJECTED
        assert resolved.stake is None
        assert queue.get(record.record_id)["status"] == "rejected"

    def test_decisione_idempotente(self, queue, limits):
        record = enqueue(queue, limits)
        first = queue.approve(record.record_id, reviewer="admin")
        second = queue.approve(record.record_id, reviewer="admin")
        assert first["reviewed_at"] == second["reviewed_at"]
        assert queue.summary()["by_status"] == {"approved": 1}

    def test_record_sconosciuto(self, queue, limits):
        with pytest.raises(KeyError):
            resolve_review(queue, "non-esiste", approve=True, reviewer="admin")


class TestFailSafe:
    def test_file_corrotto_non_esplode(self, queue, limits):
        queue.path.parent.mkdir(parents=True, exist_ok=True)
        queue.path.write_text("{questo non e' json", encoding="utf-8")
        assert queue.load() == []
        assert queue.summary()["pending"] == 0
        # e il contenuto corrotto non viene sovrascritto a vuoto
        assert "questo non e' json" in queue.path.read_text(encoding="utf-8")

    def test_coda_vuota(self, queue, limits):
        assert queue.pending() == []
        report = format_report(queue.summary())
        assert "in attesa  : 0" in report

    def test_path_da_env(self, monkeypatch, tmp_path):
        from decision.review_queue import QUEUE_ENV, default_path
        target = tmp_path / "custom.json"
        monkeypatch.setenv(QUEUE_ENV, str(target))
        assert default_path() == target
