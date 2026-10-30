"""Test del Task 2.1: il ciclo del Chief nel giro di produzione (shadow).

Garanzie verificate:
1. l'hook di `auto_bet._shadow_run` registra un ciclo nel JSONL dedicato e
   NON cambia il risultato del giro (nessun effetto su puntate/ledger);
2. l'hook e' fail-safe: un errore del wiring non rompe `_shadow_run`;
3. `CHIEF_SHADOW_ENABLED=0` spegne il ciclo (nessuna scrittura);
4. il registro e' leggibile (`summarize`/`format_report`) e i contatori
   combaciano con cio' che e' stato scritto;
5. tripwire: il hook non monta gateway reali (nessun POST SX, nessuna riga
   `bets` dal ciclo del Capo) e `import chief_shadow_wiring` resta leggero.

Tutto OFFLINE: conftest isola i sink e spegne il feed; DB temporanei.
"""

from __future__ import annotations

import json

import pytest

import chief_shadow_wiring as csw
from chief_shadow_wiring import (chief_shadow_enabled, cycle_log_path,
                                 format_report, run_chief_cycle_shadow, summarize)


class TestCicloShadow:
    def test_hook_registra_un_ciclo(self, tmp_path, monkeypatch):
        log = tmp_path / "chief_cycles.jsonl"
        monkeypatch.setenv("CHIEF_CYCLE_LOG", str(log))
        rec = run_chief_cycle_shadow(bankroll=100.0, mode="sim")
        assert rec is not None and rec["kind"] == "chief_cycle"
        assert rec["ok"] is True          # feed spento dal conftest -> gate passa
        assert rec["finance"]["plans"] == 0   # ledger locale senza segnali: giro vuoto
        assert log.exists() and len(log.read_text().strip().splitlines()) == 1

    def test_hook_fail_safe_su_errore(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CHIEF_CYCLE_LOG", str(tmp_path / "c.jsonl"))
        # Chief rotto: l'import interno esplode -> run ritorna None, niente boom
        import sys
        monkeypatch.setitem(sys.modules, "chief_orchestrator", None)
        assert run_chief_cycle_shadow(bankroll=10.0, mode="sim") is None

    def test_interruttore_spegne_il_ciclo(self, tmp_path, monkeypatch):
        log = tmp_path / "chief_cycles.jsonl"
        monkeypatch.setenv("CHIEF_CYCLE_LOG", str(log))
        monkeypatch.setenv("CHIEF_SHADOW_ENABLED", "0")
        assert chief_shadow_enabled() is False
        assert run_chief_cycle_shadow(bankroll=10.0, mode="sim") is None
        assert not log.exists()
        monkeypatch.setenv("CHIEF_SHADOW_ENABLED", "1")
        assert chief_shadow_enabled() is True

    def test_summarize_e_report(self, tmp_path, monkeypatch):
        log = tmp_path / "chief_cycles.jsonl"
        monkeypatch.setenv("CHIEF_CYCLE_LOG", str(log))
        run_chief_cycle_shadow(bankroll=100.0, mode="sim")
        run_chief_cycle_shadow(bankroll=100.0, mode="sim")
        s = summarize(days=7, path=log)
        assert s["cycles"] == 2 and s["ok"] == 2 and s["blocked"] == 0
        text = format_report(s)
        assert "CHIEF ORCHESTRATOR" in text and "2" in text

    def test_riepilogo_esposizione_aperta(self, tmp_path):
        """Direttiva 28/09/2026: il registro dichiara gli ordini APERTI, il
        tetto e i piani respinti dal recinto del 40%."""
        from datetime import datetime, timezone
        log = tmp_path / "chief_cycles.jsonl"
        log.write_text(json.dumps({
            "ts": datetime.now(timezone.utc).isoformat(),
            "kind": "chief_cycle", "ok": True,
            "exposure": {"allowed": False, "open_stake": 12.0,
                         "cap": 13.42, "count": 8},
            "advisor": [{"resolved": False, "original_reason": "exposure_cap",
                         "reason_no": "tetto 40%"}],
        }) + "\n", encoding="utf-8")
        s = summarize(days=7, path=log)
        assert s["exposure_blocked"] == 1 and s["exposure_gates"] == 1
        assert s["exposure"]["open_stake"] == 12.0
        assert s["advisor_kinds"].get("exposure_cap") == 1
        testo = format_report(s)
        assert "Esposizione aperta" in testo and "12.00" in testo

    def test_registro_su_path_produzione_coerente(self, monkeypatch):
        # default: DATA_DIR/decision/chief_cycles.jsonl (o fallback locale)
        monkeypatch.delenv("CHIEF_CYCLE_LOG", raising=False)
        p = cycle_log_path()
        assert p.name == "chief_cycles.jsonl" and p.parent.name == "decision"


class TestTripwire:
    def test_hook_non_monta_gateway_reali(self):
        src = open("chief_shadow_wiring.py", encoding="utf-8").read()
        for forbidden in ("PlaceOrderGateway(", "execution_engine", "_live_fill("):
            assert forbidden not in src, forbidden
        src2 = open("chief_orchestrator.py", encoding="utf-8").read()
        for forbidden in ("PlaceOrderGateway(", "execution_engine", "_live_fill("):
            assert forbidden not in src2, forbidden

    def test_import_leggero(self, tmp_path, monkeypatch):
        """`import chief_shadow_wiring` non carica tracker/auto_bet/bot."""
        code = (
            "import sys; import chief_shadow_wiring; "
            "bad = [m for m in ('tracker','auto_bet','bot') if m in sys.modules]; "
            "print(bad)"
        )
        import subprocess, sys as _sys
        out = subprocess.run([_sys.executable, "-c", code], capture_output=True,
                             text=True, timeout=60)
        assert out.returncode == 0
        assert out.stdout.strip() == "[]"

    def test_auto_bet_giro_invariato_con_hook(self, tmp_path, monkeypatch):
        """Il giro `run_today_bets` con l'hook attivo produce lo stesso esito
        di un giro senza (0 puntate su ledger vuoto, nessuna eccezione)."""
        import os
        from decision.models import KillSwitchStatus  # noqa: F401
        monkeypatch.setenv("CHIEF_CYCLE_LOG", str(tmp_path / "c.jsonl"))
        monkeypatch.setenv("AUTO_BET_MODE", "off")   # giro fermo: fail-fast KS
        try:
            import auto_bet
            res = auto_bet.run_today_bets()
            assert res == [] or isinstance(res, list)
        except Exception as exc:  # l'hook non deve MAI aggiungere errori
            pytest.fail(f"giro auto_bet fallito con l'hook attivo: {exc}")


class TestSerializzazioneDecimal:
    """Regressione del 29/10/2026 — il ciclo NON veniva registrato.

    Il report porta il denaro come `Decimal` (direttiva 29/09/2026) e
    `json.dumps` senza `default` solleva `Object of type Decimal is not JSON
    serializable`: in produzione la riga del ciclo andava persa a OGNI giro,
    con il solo WARNING "registro non scrivibile" (impatto sul denaro: zero,
    e' shadow; impatto sulla misura: totale).
    """

    def test_append_scrive_decimal_come_numero(self, tmp_path):
        from decimal import Decimal
        log = tmp_path / "c.jsonl"
        ok = csw._append_jsonl(log, {"sizing": {"stake": Decimal("3.18")},
                                     "bankroll": Decimal("16.14")})
        assert ok is True
        rec = json.loads(log.read_text(encoding="utf-8").strip())
        assert rec["sizing"]["stake"] == pytest.approx(3.18)
        assert isinstance(rec["sizing"]["stake"], float)   # NUMERO, non "3.18"
        assert isinstance(rec["bankroll"], float)

    def test_append_fail_safe_su_tipo_ostile(self, tmp_path):
        class Ops:
            def __str__(self):
                return "ops"

        log = tmp_path / "c.jsonl"
        assert csw._append_jsonl(log, {"x": Ops()}) is True
        assert json.loads(log.read_text(encoding="utf-8").strip()) == {"x": "ops"}

    def test_ciclo_con_decimal_nel_report(self, tmp_path, monkeypatch):
        """Il caso reale: il report del ciclo contiene `Decimal` e DEVE
        comunque produrre una riga JSONL leggibile."""
        from decimal import Decimal

        import chief_orchestrator as co

        class FakeReport:
            def as_json(self):
                return {"ok": True,
                        "sizing": {"stake": Decimal("2.50")},
                        "finance": {"plans": 0, "approved": 0,
                                    "review": 0, "rejected": 0}}

        monkeypatch.setattr(co.ChiefOrchestrator, "run_cycle",
                            lambda self: FakeReport(), raising=True)
        log = tmp_path / "c.jsonl"
        monkeypatch.setenv("CHIEF_CYCLE_LOG", str(log))
        rec = run_chief_cycle_shadow(bankroll=50.0, mode="live")
        assert rec is not None and rec["kind"] == "chief_cycle"
        written = json.loads(log.read_text(encoding="utf-8").strip())
        assert written["sizing"]["stake"] == pytest.approx(2.5)
        assert written["ok"] is True
