"""Test backup_manager: snapshot completo, integrity, rotazione, report."""
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import tracker
import backup_manager


@pytest.fixture()
def temp_db(monkeypatch, tmp_path):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(tracker, "DB_PATH", db_path)
    tracker.init_db()
    # config.DATA_DIR usato da backup_manager: punta su tmp
    import config
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    (tmp_path / "data").mkdir(exist_ok=True)
    yield db_path


def _seed_signal(mid="m1", settle=True):
    base = datetime.now(timezone.utc) - timedelta(hours=3)
    tracker.save_match(mid, "Serie A", "Inter", "Empoli",
                       base.isoformat().replace("+00:00", "Z"))
    tracker.save_analysis(mid, 1.7, 1.1, 0.52, 0.27, 0.21, 0.58, 0.08,
                          "Inter", 1.90, "Pinnacle", "value",
                          market_prob=0.45, market_edge=0.07)
    tracker.save_prediction(mid, "1X2", "Inter", 1.90, 0.55, 0.10)
    if settle:
        tracker.save_result(mid, "Serie A", "Inter", "Empoli", 2, 1,
                            datetime.now().isoformat())
        tracker.settle_predictions()


def test_run_backup_completo(temp_db, tmp_path):
    _seed_signal()
    dest = tmp_path / "backups"
    s = backup_manager.run_backup(dest_root=dest, keep=3)

    snap = Path(s["dest"])
    assert (snap / "quotaverace.db").exists()
    assert (snap / "training_dataset.csv").exists()
    assert (snap / "ml_export.json").exists()
    # integrity ok e conteggi coerenti
    assert s["db"]["integrity"] == "ok"
    assert s["db"]["tables"]["predictions"] == 1
    # dataset ML esportato: 1 riga chiusa
    assert s["ml"]["csv"] == 1
    assert s["ml"]["json_rows"] == 1
    assert s["errors"] == []


def test_backup_db_contenuto_corretto(temp_db, tmp_path):
    """Il DB copiato contiene i dati (non un file vuoto)."""
    _seed_signal("mX")
    s = backup_manager.run_backup(dest_root=tmp_path / "b", keep=1)
    conn = sqlite3.connect(Path(s["dest"]) / "quotaverace.db")
    n = conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
    conn.close()
    assert n == 1


def test_rotazione_backup(temp_db, tmp_path):
    """Oltre `keep` snapshot: i piu' vecchi vengono rimossi."""
    dest = tmp_path / "b"
    for _ in range(5):
        backup_manager.run_backup(dest_root=dest, keep=2)
    snaps = sorted(p for p in dest.iterdir() if p.is_dir())
    assert len(snaps) == 2


def test_backup_dataset_vuoto_non_esplode(temp_db, tmp_path):
    s = backup_manager.run_backup(dest_root=tmp_path / "b", keep=1)
    assert s["errors"] == [] or all("db" not in e for e in s["errors"])
    assert s["ml"]["csv"] == 0


def test_format_backup_report(temp_db, tmp_path):
    _seed_signal()
    s = backup_manager.run_backup(dest_root=tmp_path / "b", keep=1)
    text = backup_manager.format_backup_report(s)
    assert "BACKUP COMPLETATO" in text
    assert "Dataset ML" in text
    # con errori: messaggio diverso
    s2 = {**s, "errors": ["db: test error"]}
    assert "errori" in backup_manager.format_backup_report(s2)


# ------------------------------------------ rotazione + copia onesta (23/10)

def test_rotazione_log_prima_della_copia(temp_db, tmp_path, monkeypatch):
    """I JSONL di telemetria vengono compattati PRIMA che lo snapshot copi
    `data/`: senza, i due log scritti ogni 60s finivano interi in ogni backup
    e il volume andava in `No space left on device` (23/10)."""
    import json as _json
    import telemetry_logs as tl
    monkeypatch.setenv("TELEMETRY_ROTATE_ENABLED", "1")
    monkeypatch.setenv("LOG_ROTATE_ACTIVE_MAX_MB", "0.001")
    monkeypatch.setenv("LOG_ROTATE_QUIET_MIN", "15")
    log = tmp_path / "data" / "execution" / "book_flow_events.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("\n".join(_json.dumps({"i": i}) for i in range(3000)) + "\n")
    big = log.stat().st_size

    s = backup_manager.run_backup(dest_root=tmp_path / "b", keep=1)

    assert s["rotation"] is not None and s["rotation"]["enabled"] is True
    assert log.exists() and log.stat().st_size == 0        # troncato in place
    gens = list(log.parent.glob("book_flow_events.jsonl*.gz"))
    assert len(gens) == 1                                  # storia conservata
    # lo snapshot NON contiene i MB del log: prende il vivo troncato
    snap = Path(s["dest"])
    assert (snap / "execution" / "book_flow_events.jsonl").exists()
    assert (snap / "execution" / "book_flow_events.jsonl").stat().st_size == 0
    assert big > 0


def test_rotazione_fallita_non_blocca_il_backup(temp_db, tmp_path):
    """Fail-safe: un problema di rotazione non deve impedire il backup."""
    import telemetry_logs as tl

    def _boom(**kw):
        raise RuntimeError("rotazione rotta")

    orig = tl.rotate_jsonl_logs
    tl.rotate_jsonl_logs = _boom
    try:
        s = backup_manager.run_backup(dest_root=tmp_path / "b", keep=1)
    finally:
        tl.rotate_jsonl_logs = orig
    assert s["rotation"] is None
    assert s["db"]["integrity"] == "ok"
    assert (Path(s["dest"]) / "quotaverace.db").exists()


def test_copia_parziale_viene_dichiarata(temp_db, tmp_path, monkeypatch):
    """Una voce che non si copia non deve fermare le altre e lo snapshot deve
    dire di essere PARZIALE (prima: 25 voci su ~30 e log "integrity: ok")."""
    (tmp_path / "data" / "a.txt").write_text("a")
    (tmp_path / "data" / "b.txt").write_text("b")
    (tmp_path / "data" / "c.txt").write_text("c")
    import shutil
    real = shutil.copy2

    def _copy2(src, dst, *a, **kw):
        if Path(src).name == "b.txt":
            raise OSError(13, "Permesso negato")
        return real(src, dst, *a, **kw)

    monkeypatch.setattr(shutil, "copy2", _copy2)
    s = backup_manager.run_backup(dest_root=tmp_path / "b", keep=1)

    assert s["data_partial"] is True
    assert s["data_errors"] == 1
    snap = Path(s["dest"])
    assert (snap / "a.txt").exists() and (snap / "c.txt").exists()
    assert not (snap / "b.txt").exists()
    assert any("data/b.txt" in e for e in s["errors"])
    report = backup_manager.format_backup_report(s)
    assert "PARZIALE" in report and "2 file copiati" in report


def test_spazio_esaurito_ferma_la_copia(temp_db, tmp_path, monkeypatch):
    """ENOSPC: si dichiara e ci si ferma (niente sfilza di errori identici)."""
    for name in ("a", "b", "c", "d"):
        (tmp_path / "data" / f"{name}.txt").write_text(name)
    import errno
    import shutil
    calls = {"n": 0}

    def _copy2(src, dst, *a, **kw):
        calls["n"] += 1
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(shutil, "copy2", _copy2)
    s = backup_manager.run_backup(dest_root=tmp_path / "b", keep=1)

    assert calls["n"] == 1, "dopo ENOSPC non si tenta piu' la copia"
    assert s["data_partial"] is True
    assert s["data_errors"] == 4          # 1 tentata + 3 dichiarate
    assert s["db"]["integrity"] == "ok"   # DB e dataset ML restano scritti


def test_latest_backup(temp_db, tmp_path):
    assert backup_manager.latest_backup(tmp_path / "b") is None
    backup_manager.run_backup(dest_root=tmp_path / "b", keep=1)
    last = backup_manager.latest_backup(tmp_path / "b")
    assert last is not None and last.is_dir()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
