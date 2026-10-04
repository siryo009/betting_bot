"""Test `oracle_skips` + hook del gate top-down in `auto_bet` (03/10/2026).

Tutti OFFLINE: nessuna rete, nessun ordine, nessuna scrittura nel ledger. Il
log va nella tmp (conftest isola `ORACLE_SKIP_LOG` per tutta la suite).
"""
import json
import time
from pathlib import Path

import pytest

import oracle_skips as osk


@pytest.fixture(autouse=True)
def _clean():
    osk.reset_dedup()
    yield
    osk.reset_dedup()


def _pick(**kw):
    base = {"match_id": "sx-L1", "esito_key": "Over 2.5", "mercato": "OU",
            "league": "Serie A", "quota": 1.55}
    base.update(kw)
    return base


# ------------------------------------------------------------------ scrittura

def test_record_skip_scrive(tmp_path, monkeypatch):
    p = tmp_path / "skips.jsonl"
    monkeypatch.setenv("ORACLE_SKIP_LOG", str(p))
    evt = osk.record_skip(_pick(), "linea", detail="serve fetch")
    assert evt and evt["reason"] == "linea" and evt["mercato"] == "OU"
    row = json.loads(p.read_text().splitlines()[0])
    assert row["league"] == "Serie A" and row["detail"] == "serve fetch"


def test_dedup_per_giorno_pick_e_motivo(tmp_path, monkeypatch):
    monkeypatch.setenv("ORACLE_SKIP_LOG", str(tmp_path / "s.jsonl"))
    assert osk.record_skip(_pick(), "linea") is not None
    assert osk.record_skip(_pick(), "linea") is None        # duplicato
    assert osk.record_skip(_pick(esito_key="Home +1.5"), "linea") is not None
    assert osk.record_skip(_pick(), "no_oracle") is not None  # motivo diverso


def test_reset_dedup_riabilita(tmp_path, monkeypatch):
    monkeypatch.setenv("ORACLE_SKIP_LOG", str(tmp_path / "s.jsonl"))
    osk.record_skip(_pick(), "linea")
    osk.reset_dedup()
    assert osk.record_skip(_pick(), "linea") is not None


def test_scrittura_impossibile_non_propaga(tmp_path, monkeypatch):
    # Il path punta a una DIRECTORY: `open("a")` solleva e la telemetria deve
    # degradare a None senza propagare (una scrittura rotta non ferma il giro).
    monkeypatch.setenv("ORACLE_SKIP_LOG", str(tmp_path))
    assert osk.record_skip(_pick(), "no_oracle") is None


def test_path_letto_a_runtime(tmp_path, monkeypatch):
    """Il path NON e' una costante di import (bug reale di `tennis_quant`)."""
    a, b = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    monkeypatch.setenv("ORACLE_SKIP_LOG", str(a))
    osk.record_skip(_pick(), "linea")
    osk.reset_dedup()
    monkeypatch.setenv("ORACLE_SKIP_LOG", str(b))
    osk.record_skip(_pick(match_id="sx-L2"), "linea")
    assert a.exists() and b.exists()
    assert json.loads(b.read_text().splitlines()[0])["match_id"] == "sx-L2"


# --------------------------------------------------------------------- report

def test_summary_raggruppa(tmp_path, monkeypatch):
    monkeypatch.setenv("ORACLE_SKIP_LOG", str(tmp_path / "s.jsonl"))
    osk.record_skip(_pick(), "linea")
    osk.record_skip(_pick(esito_key="Under 2.5"), "no_oracle")
    osk.record_skip(_pick(match_id="sx-L9", mercato="AH", esito_key="Home +1",
                          league="Liga MX"), "no_oracle")
    s = osk.summary(days=1)
    assert s["events"] == 3
    assert s["by_reason"] == {"linea": 1, "no_oracle": 2}
    assert s["by_market"] == {"OU": 2, "AH": 1}
    assert s["by_league"]["Liga MX"] == 1


def test_summary_vuoto(tmp_path, monkeypatch):
    monkeypatch.setenv("ORACLE_SKIP_LOG", str(tmp_path / "s.jsonl"))
    s = osk.summary(days=1)
    assert s["events"] == 0 and s["by_reason"] == {}


def test_finestra_temporale(tmp_path, monkeypatch):
    p = tmp_path / "s.jsonl"
    monkeypatch.setenv("ORACLE_SKIP_LOG", str(p))
    p.write_text(json.dumps({"reason": "linea", "ts_epoch": time.time()}) + "\n"
                 + json.dumps({"reason": "linea",
                               "ts_epoch": time.time() - 10 * 86400}) + "\n")
    assert osk.summary(days=1)["events"] == 1


def test_format_report(tmp_path, monkeypatch):
    monkeypatch.setenv("ORACLE_SKIP_LOG", str(tmp_path / "s.jsonl"))
    assert "nessuno scarto" in osk.format_report(days=1)
    osk.record_skip(_pick(), "linea")
    txt = osk.format_report(days=1)
    assert "linea" in txt and "OU" in txt


# ------------------------------------------------- hook nel gate top-down

def test_note_top_down_skip_scrive(tmp_path, monkeypatch):
    monkeypatch.setenv("ORACLE_SKIP_LOG", str(tmp_path / "s.jsonl"))
    import auto_bet
    auto_bet._note_top_down_skip(_pick(), "no_oracle", detail="d")
    assert osk.summary(days=1)["by_reason"] == {"no_oracle": 1}


def test_note_top_down_skip_fail_safe(monkeypatch):
    """Un guasto della telemetria non deve MAI fermare un giro puntate."""
    import auto_bet
    monkeypatch.setattr(osk, "record_skip",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    auto_bet._note_top_down_skip(_pick(), "linea")      # nessuna eccezione


def test_hook_presente_nei_rami_di_scarto():
    """Tripwire: i due rami di scarto del gate devono registrare la misura."""
    src = (Path(__file__).parent / "auto_bet.py").read_text()
    assert src.count("_note_top_down_skip(") >= 3     # def + 2 chiamate
    seg = src.split("if not verdict.get(\"ok\"):")[1][:600]
    assert "_note_top_down_skip(" in seg
    seg2 = src.split("verdict[\"ev_min\"] * 100.0,")[1][:400]
    assert "_note_top_down_skip(" in seg2


def test_job_credit_watchdog_usa_la_diagnosi():
    src = (Path(__file__).parent / "bot.py").read_text()
    assert "credit_diagnose" in src and "attribuzione" in src


def test_rotazione_agganciata_al_backup():
    src = (Path(__file__).parent / "bot.py").read_text()
    assert "rotate_jsonl_logs" in src


def test_env_dichiarate_in_iac():
    iac = (Path(__file__).parent / ".railway" / "railway.ts").read_text()
    assert "ORACLE_SKIP_LOG" in iac and "TELEMETRY_ROTATE_ENABLED" in iac
