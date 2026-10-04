"""Test `telemetry_logs`: rotazione automatica dei JSONL + lettore unico.

Tutti OFFLINE: nessuna rete, nessun job, nessun file di produzione (ogni test
usa una cartella temporanea). Le date sono sempre RELATIVE a `now` (lezione
delle date fisse del 15/09 e del 17/09: un test che scade col calendario
arriva sempre nel momento peggiore).
"""
import gzip
import json
import time
from pathlib import Path

import pytest

import telemetry_logs as tl


@pytest.fixture(autouse=True)
def _rotation_on(monkeypatch):
    """La rotazione e' OFF per tutta la suite (conftest): qui e' il soggetto."""
    monkeypatch.setenv("TELEMETRY_ROTATE_ENABLED", "1")


def _lines(n=1000, *, start=0):
    now = int(time.time())
    return "\n".join(json.dumps({"i": i, "ts_epoch": now})
                     for i in range(start, start + n)) + "\n"


def _write(path: Path, text: str, *, age_days=0.0):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if age_days:
        old = time.time() - age_days * 86400
        import os as _os
        _os.utime(path, (old, old))
    return path


# ---------------------------------------------------------------- rotazione

def test_ruota_file_vecchio_e_grande(tmp_path):
    log = _write(tmp_path / "execution" / "big.jsonl", _lines(50000),
                 age_days=3)
    res = tl.rotate_jsonl_logs(root=tmp_path, max_mb=1, after_days=2,
                               quiet_min=0, keep=3)
    assert len(res["rotated"]) == 1
    assert res["bytes_after"] < res["bytes_before"]
    assert not log.exists()                      # il vivo viene rimosso...
    gens = list((tmp_path / "execution").glob("big.jsonl*.gz"))
    assert len(gens) == 1                        # ...e nasce la generazione
    # contenuto rileggibile
    with gzip.open(gens[0], "rt", encoding="utf-8") as fh:
        assert json.loads(fh.readline())["i"] == 0


def test_file_fresco_e_piccolo_non_si_tocca(tmp_path):
    log = _write(tmp_path / "a.jsonl", _lines(10))
    res = tl.rotate_jsonl_logs(root=tmp_path, max_mb=1, after_days=2,
                               quiet_min=0, keep=3)
    assert res["rotated"] == []
    assert log.exists()


def test_file_vecchio_ma_piccolo_si_ruota(tmp_path):
    """La regola di ETA' esiste per i log che (quasi) non crescono piu'."""
    log = _write(tmp_path / "vecchio.jsonl", _lines(5), age_days=5)
    res = tl.rotate_jsonl_logs(root=tmp_path, max_mb=100, after_days=2,
                               quiet_min=0, keep=3)
    assert len(res["rotated"]) == 1
    assert not log.exists()


def test_file_appena_scritto_non_si_tocca(tmp_path):
    """La finestra di QUIETE protegge un writer: nessuna compressione sotto
    i piedi di chi ha appena scritto."""
    log = _write(tmp_path / "active.jsonl", _lines(50000))   # mtime = adesso
    res = tl.rotate_jsonl_logs(root=tmp_path, max_mb=1, after_days=0.001,
                               quiet_min=15, keep=3)
    assert res["rotated"] == []
    assert log.exists()


def test_potatura_generazioni(tmp_path):
    for i in range(4):
        log = _write(tmp_path / "log.jsonl", _lines(2000), age_days=2 + i)
        tl.rotate_jsonl_logs(root=tmp_path, max_mb=0.001, after_days=1,
                             quiet_min=0, keep=2)
    gens = sorted((tmp_path).glob("log.jsonl*.gz"))
    assert len(gens) == 2, "vanno tenute solo le `keep` generazioni"


def test_registri_backup_mai_toccati(tmp_path):
    snap = tmp_path / "backups" / "20260101-000000-000000"
    inside = _write(snap / "quotaverace.db", "x")     # non jsonl: ignorato
    log = _write(snap / "book_flow_events.jsonl", _lines(50000), age_days=5)
    res = tl.rotate_jsonl_logs(root=tmp_path, max_mb=1, after_days=2,
                               quiet_min=0, keep=3)
    assert res["rotated"] == []
    assert log.exists() and inside.exists()


def test_interruttore_spento(tmp_path):
    log = _write(tmp_path / "x.jsonl", _lines(50000), age_days=5)
    res = tl.rotate_jsonl_logs(root=tmp_path, enabled=False, max_mb=1,
                               after_days=1, quiet_min=0)
    assert res["enabled"] is False and res["rotated"] == []
    assert log.exists()


def test_interruttore_da_env(tmp_path, monkeypatch):
    log = _write(tmp_path / "x.jsonl", _lines(50000), age_days=5)
    monkeypatch.setenv("TELEMETRY_ROTATE_ENABLED", "0")
    res = tl.rotate_jsonl_logs(root=tmp_path, max_mb=1, after_days=1,
                               quiet_min=0)
    assert res["enabled"] is False and log.exists()
    monkeypatch.setenv("TELEMETRY_ROTATE_ENABLED", "1")
    assert tl.rotate_jsonl_logs(root=tmp_path, max_mb=1, after_days=1,
                                quiet_min=0)["enabled"] is True


def test_dry_run_non_tocca_nulla(tmp_path):
    log = _write(tmp_path / "x.jsonl", _lines(50000), age_days=5)
    res = tl.rotate_jsonl_logs(root=tmp_path, max_mb=1, after_days=1,
                               quiet_min=0, dry_run=True)
    assert len(res["rotated"]) == 1 and res["rotated"][0]["dry_run"]
    assert log.exists() and not list(tmp_path.glob("*.gz"))


def test_env_impossibili_ricadono_sul_default(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_ROTATE_MAX_MB", "abc")
    monkeypatch.setenv("LOG_ROTATE_AFTER_DAYS", "-5")
    monkeypatch.setenv("LOG_ROTATE_QUIET_MIN", "")
    monkeypatch.setenv("LOG_ROTATE_KEEP", "0")
    log = _write(tmp_path / "piccolo.jsonl", _lines(3))
    res = tl.rotate_jsonl_logs(root=tmp_path)     # nessuna eccezione
    assert res["scanned"] >= 1 and res["rotated"] == []
    assert log.exists()


def test_cartella_inesistente_non_esplode(tmp_path):
    res = tl.rotate_jsonl_logs(root=tmp_path / "nope", max_mb=1, after_days=1)
    assert res["errors"] == 0 and res["rotated"] == []


# ------------------------------------------------------------------ lettore

def test_iter_events_legge_vivo_e_generazioni(tmp_path):
    log = tmp_path / "ev.jsonl"
    log.write_text(json.dumps({"a": "nuovo", "ts_epoch": time.time()}) + "\n")
    with gzip.open(str(log) + ".20260101T000000000000.gz", "wt") as fh:
        fh.write(json.dumps({"a": "vecchio", "ts_epoch": time.time() - 10}) + "\n")
    ev = list(tl.iter_events(log))
    assert [e["a"] for e in ev] == ["nuovo", "vecchio"]   # recenti prima


def test_iter_events_ignora_righe_corrotte(tmp_path):
    log = tmp_path / "ev.jsonl"
    log.write_text("{rotto\n" + json.dumps({"ok": 1, "ts_epoch": time.time()})
                   + "\n\n")
    ev = list(tl.iter_events(log))
    assert len(ev) == 1 and ev[0]["ok"] == 1


def test_iter_events_finestra_temporale(tmp_path):
    now = time.time()
    log = tmp_path / "ev.jsonl"
    log.write_text(
        json.dumps({"x": 1, "ts_epoch": now}) + "\n"
        + json.dumps({"x": 2, "ts_epoch": now - 10 * 86400}) + "\n")
    assert [e["x"] for e in tl.iter_events(log, days=1)] == [1]


def test_iter_events_file_assente(tmp_path):
    assert list(tl.iter_events(tmp_path / "nope.jsonl")) == []


# -------------------------------------------------------------------- report

def test_consumatori_leggono_le_generazioni_gz(tmp_path, monkeypatch):
    """Tripwire CROSS-MODULO (03/10/2026): chi riepiloga i nostri log deve
    vedere anche le generazioni `.gz`, altrimenti la rotazione gli toglie la
    storia. Tutti delegano a `telemetry_logs.read_lines`/`iter_events`.
    """
    import datetime as _dt
    import gzip as _gz
    import json as _json

    import book_flow as bf
    import chief_shadow_wiring as cw
    import liquidity_monitor as lm
    import smart_hedging as sh

    now = time.time()
    iso = _dt.datetime.now(_dt.timezone.utc).isoformat()

    def _gen(path, payload):
        with _gz.open(str(path) + ".20261003T000000000000.gz", "wt") as fh:
            fh.write(_json.dumps(payload) + "\n")

    monkeypatch.setattr(lm, "SKIP_LOG", tmp_path / "liq.jsonl")
    _gen(tmp_path / "liq.jsonl", {"kind": "order", "reason": "depth",
                                  "ts_epoch": now})
    assert lm.summary(days=1)["events"] == 1

    monkeypatch.setattr(bf, "LOG_PATH", tmp_path / "bf.jsonl")
    _gen(tmp_path / "bf.jsonl", {"reason": "ingress", "ts_epoch": now,
                                 "delta_usdc": 5.0})
    assert bf.summary(days=1)["events"] == 1

    monkeypatch.setenv("HEDGE_LOG", str(tmp_path / "hedge.jsonl"))
    _gen(tmp_path / "hedge.jsonl", {"kind": "opportunity", "ts": iso})
    assert len(sh.iter_events(days=1)) == 1

    log = tmp_path / "chief.jsonl"
    monkeypatch.setenv("CHIEF_CYCLE_LOG", str(log))
    _gen(log, {"ok": True, "ts": iso})
    assert cw.summarize(days=7, path=log)["cycles"] == 1


def test_format_report(tmp_path):
    _write(tmp_path / "x.jsonl", _lines(50000), age_days=5)
    res = tl.rotate_jsonl_logs(root=tmp_path, max_mb=1, after_days=1,
                               quiet_min=0, keep=3)
    txt = tl.format_report(res)
    assert "Rotazione log" in txt and "ruotati" in txt
    assert "disattivata" in tl.format_report({"enabled": False})
