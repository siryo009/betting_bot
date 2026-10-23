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
    # 23/10: uno snapshot completo e' VALIDO e lascia il manifest
    assert s["status"] == "ok" and s["ok"] is True and s["reasons"] == []
    assert (snap / backup_manager.MANIFEST_NAME).exists()
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
    assert s["status"] == "partial" and s["ok"] is False
    snap = Path(s["dest"])
    assert (snap / "a.txt").exists() and (snap / "c.txt").exists()
    assert not (snap / "b.txt").exists()
    assert any("data/b.txt" in e for e in s["errors"])
    report = backup_manager.format_backup_report(s)
    assert "PARZIALE" in report and "2 copiati, 1 non copiati" in report


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


# ---------------------------------------------------------------------------
# Validita' (23/10/2026): un criterio UNICO per DB, completezza ed errori
# ---------------------------------------------------------------------------

class TestValidita:
    """Tre stati distinguibili + `unknown` per cio' che non e' verificabile.

    Prima un backup senza DB o senza meta' delle voci di `data/` poteva
    comunque leggersi come "integrity: ok" (nessun campo stato): il job e
    `/backup` dichiaravano successo su uno snapshot inutilizzabile.
    """

    def test_completo_e_valido_con_manifest(self, temp_db, tmp_path):
        s = backup_manager.run_backup(dest_root=tmp_path / "b", keep=1)
        snap = Path(s["dest"])
        assert s["status"] == "ok" and s["ok"] is True
        m = backup_manager.read_manifest(snap)
        assert m is not None
        assert m["status"] == "ok" and m["ok"] is True
        assert m["schema"] == backup_manager.MANIFEST_SCHEMA
        assert m["created_at"].endswith("Z")
        assert m["db"]["integrity"] == "ok"
        assert m["rotation_error"] is None
        assert backup_manager.snapshot_status(snap) == "ok"
        assert backup_manager.snapshot_is_valid(snap) is True
        report = backup_manager.format_backup_report(s)
        assert "BACKUP COMPLETATO" in report and "FALLITO" not in report

    def test_parziale_non_e_successo(self, temp_db, tmp_path, monkeypatch):
        (tmp_path / "data" / "x.txt").write_text("x")
        import shutil
        real = shutil.copy2

        def _copy2(src, dst, *a, **kw):
            if Path(src).name == "x.txt":
                raise OSError(13, "Permesso negato")
            return real(src, dst, *a, **kw)

        monkeypatch.setattr(shutil, "copy2", _copy2)
        s = backup_manager.run_backup(dest_root=tmp_path / "b", keep=1)
        assert s["status"] == "partial" and s["ok"] is False
        assert any(r.startswith("data_") for r in s["reasons"])
        m = backup_manager.read_manifest(Path(s["dest"]))
        assert m["status"] == "partial" and m["ok"] is False
        assert backup_manager.snapshot_is_valid(Path(s["dest"])) is False
        report = backup_manager.format_backup_report(s)
        assert "PARZIALE" in report and "FALLITO" not in report
        assert "non copiati" in report

    def test_db_assente_e_fallimento(self, monkeypatch, tmp_path):
        import config
        monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "assente.db")
        monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data2")
        (tmp_path / "data2").mkdir()
        s = backup_manager.run_backup(dest_root=tmp_path / "b", keep=1)
        assert s["status"] == "failed" and s["ok"] is False
        assert "db_assente" in s["reasons"]
        assert not (Path(s["dest"]) / "quotaverace.db").exists()
        report = backup_manager.format_backup_report(s)
        assert "BACKUP FALLITO" in report and "COMPLETATO" not in report

    def test_integrita_non_valida_e_fallimento(self, temp_db, tmp_path,
                                               monkeypatch):
        monkeypatch.setattr(
            backup_manager, "backup_integrity_check",
            lambda p: {"integrity": "error: database disk image is malformed",
                       "tables": {}})
        s = backup_manager.run_backup(dest_root=tmp_path / "b", keep=1)
        assert s["status"] == "failed"
        assert any(r.startswith("integrita_db") for r in s["reasons"])
        assert backup_manager.snapshot_status(Path(s["dest"])) == "failed"

    def test_snapshot_senza_manifest_e_sconosciuto(self, tmp_path):
        """I backup pre-manifest NON sono automaticamente validi."""
        snap = tmp_path / "b" / "20260101-000000-000000"
        snap.mkdir(parents=True)
        (snap / "quotaverace.db").write_bytes(b"")
        assert backup_manager.read_manifest(snap) is None
        assert backup_manager.snapshot_status(snap) == "unknown"
        assert backup_manager.snapshot_is_valid(snap) is False

    def test_manifest_illeggibile_e_sconosciuto(self, tmp_path):
        snap = tmp_path / "b" / "20260101-000001-000000"
        snap.mkdir(parents=True)
        (snap / backup_manager.MANIFEST_NAME).write_text("{rotto")
        assert backup_manager.snapshot_status(snap) == "unknown"


# ---------------------------------------------------------------------------
# Rotazione: l'ultimo backup VALIDO non si tocca (23/10/2026)
# ---------------------------------------------------------------------------

class TestRotazioneConservaUltimoValido:

    def test_parziale_piu_recente_non_elimina_l_ultimo_valido(self, temp_db,
                                                              tmp_path,
                                                              monkeypatch):
        dest = tmp_path / "b"
        primo = backup_manager.run_backup(dest_root=dest, keep=2)
        secondo = backup_manager.run_backup(dest_root=dest, keep=2)
        (tmp_path / "data" / "c.txt").write_text("c")
        import shutil
        real = shutil.copy2

        def _copy2(src, dst, *a, **kw):
            if Path(src).name == "c.txt":
                raise OSError(13, "Permesso negato")
            return real(src, dst, *a, **kw)

        monkeypatch.setattr(shutil, "copy2", _copy2)
        terzo = backup_manager.run_backup(dest_root=dest, keep=1)
        assert terzo["status"] == "partial"
        # il valido piu' recente (secondo) resta; il vecchio valido (primo) no
        assert Path(secondo["dest"]).exists()
        assert Path(terzo["dest"]).exists()
        assert not Path(primo["dest"]).exists()
        validi = [p for p in dest.iterdir()
                  if p.is_dir() and backup_manager.snapshot_is_valid(p)]
        assert validi, "la rotazione ha cancellato l'ultimo backup valido"

    def test_legacy_senza_manifest_protetto_ma_mai_valido(self, temp_db,
                                                          tmp_path):
        dest = tmp_path / "b"
        dest.mkdir()
        legacy = dest / "20260101-000000-000000"
        legacy.mkdir()
        (legacy / "quotaverace.db").write_bytes(b"")

        s = backup_manager.run_backup(dest_root=dest, keep=1)

        assert legacy.exists(), "lo snapshot legacy va ispezionabile"
        assert backup_manager.snapshot_status(legacy) == "unknown"
        assert backup_manager.snapshot_status(Path(s["dest"])) == "ok"
        # il legacy NON viene contato fra i validi
        assert [backup_manager.snapshot_status(p) for p in dest.iterdir()
                if p.is_dir()].count("ok") == 1

    def test_keep_non_puo_scendere_sotto_l_ultimo_valido(self, temp_db,
                                                         tmp_path):
        dest = tmp_path / "b"
        s = backup_manager.run_backup(dest_root=dest, keep=1)
        out = backup_manager.prune(dest, 0, current=Path(s["dest"]),
                                    current_status="ok")
        assert out["keep"] == 1
        assert Path(s["dest"]).exists()

    def test_gli_snapshot_validi_restano_entro_keep(self, temp_db, tmp_path):
        dest = tmp_path / "b"
        for _ in range(4):
            backup_manager.run_backup(dest_root=dest, keep=2)
        validi = [p for p in dest.iterdir()
                  if p.is_dir() and backup_manager.snapshot_is_valid(p)]
        assert len(validi) == 2


# ---------------------------------------------------------------------------
# Job schedulato: tracciabilita' AVVIO / COMPLETATO / PARZIALE / FALLITO
# ---------------------------------------------------------------------------

class TestJobSchedulato:
    """Il job delle 03:30 UTC deve lasciare tre righe DISTINGUIBILI.

    Prima l'unica riga era quella di `run_backup`: "job mai partito" e "job
    partito e morto prima di arrivare al backup" si leggevano uguali, e la
    verifica post-backup non poteva attribuire nulla al job.
    """

    @staticmethod
    def _run(monkeypatch, summary=None, raises=None):
        import asyncio
        import bot

        def _fake_backup(*a, **kw):
            if raises is not None:
                raise raises
            return summary

        monkeypatch.setattr(backup_manager, "run_backup", _fake_backup)
        asyncio.run(bot.backup_data_job(object()))

    @staticmethod
    def _summary(**kw):
        base = {"stamp": "20261024-033000-123456", "data_files": 117,
                "data_errors": 0, "data_partial": False,
                "db": {"integrity": "ok", "tables": {}},
                "ml": {"csv": 1721}, "errors": [], "kept": 3,
                "status": "ok", "ok": True, "reasons": [],
                "rotation": None, "rotation_error": None}
        base.update(kw)
        return base

    def test_avvio_e_completamento_con_lo_stesso_id(self, monkeypatch, caplog):
        caplog.set_level("INFO", logger="bot")
        self._run(monkeypatch, summary=self._summary())
        testo = caplog.text

        assert "backup_data_job[backup-" in testo
        assert "AVVIO" in testo and "COMPLETATO" in testo
        # stesso identificativo di run per avvio e completamento
        import re
        ids = set(re.findall(r"backup_data_job\[(backup-\d{8}T\d{6}Z)\]", testo))
        assert len(ids) == 1, ids
        # il completamento dichiara cio' che va verificato a mano
        assert "20261024-033000-123456" in testo
        assert "integrity DB: ok" in testo
        assert "117 file data copiati, 0 non copiati" in testo
        assert "stato: ok" in testo
        assert "FALLITO" not in testo

    def test_fallimento_distinguibile_e_senza_completamento(self, monkeypatch, caplog):
        caplog.set_level("INFO", logger="bot")
        self._run(monkeypatch, raises=RuntimeError("disco pieno"))

        assert "AVVIO" in caplog.text
        assert "FALLITO" in caplog.text and "disco pieno" in caplog.text
        assert "COMPLETATO" not in caplog.text
        record = [r for r in caplog.records if "FALLITO" in r.getMessage()]
        assert record and record[0].levelname == "ERROR"

    def test_snapshot_parziale_non_e_completato(self, monkeypatch, caplog):
        """Un parziale NON e' un "COMPLETATO" (23/10/2026)."""
        caplog.set_level("INFO", logger="bot")
        self._run(monkeypatch, summary=self._summary(
            status="partial", ok=False, data_partial=True, data_errors=3,
            reasons=["data_3_voci_non_copiate"]))
        assert "PARZIALE" in caplog.text
        assert "COMPLETATO" not in caplog.text
        assert "3 non copiati" in caplog.text
        record = [r for r in caplog.records if "PARZIALE" in r.getMessage()]
        assert record and record[0].levelname == "WARNING"

    def test_db_assente_e_fallito(self, monkeypatch, caplog):
        """Criterio unico: DB assente/integrita' rotta -> FALLITO (ERROR)."""
        caplog.set_level("INFO", logger="bot")
        self._run(monkeypatch, summary=self._summary(
            status="failed", ok=False, db=None,
            reasons=["db_assente"]))
        assert "FALLITO" in caplog.text and "db_assente" in caplog.text
        assert "COMPLETATO" not in caplog.text and "PARZIALE" not in caplog.text
        record = [r for r in caplog.records if "FALLITO" in r.getMessage()]
        assert record and record[0].levelname == "ERROR"

    def test_rotazione_rotta_non_impedisce_il_backup(self, monkeypatch, caplog):
        """La rotazione e' dentro `run_backup`: il suo esito si legge dal
        riepilogo e non degrada un backup completo."""
        caplog.set_level("INFO", logger="bot")
        self._run(monkeypatch, summary=self._summary(
            rotation=None, rotation_error="permessi"))
        assert "rotazione JSONL: NON eseguita (permessi)" in caplog.text
        assert "COMPLETATO" in caplog.text

    def test_rotazione_dichiarata_quando_compatta(self, monkeypatch, caplog):
        caplog.set_level("INFO", logger="bot")
        self._run(monkeypatch, summary=self._summary(
            rotation={"rotated": [{"path": "x"}], "bytes_before": 8_388_608,
                      "bytes_after": 209_715, "errors": 0}))
        assert "rotazione JSONL: 1 compattati" in caplog.text
        assert "8.0 MB -> 0.2 MB" in caplog.text

    def test_un_solo_responsabile_della_rotazione(self):
        """Il job NON ruota i JSONL per conto suo (23/10/2026)."""
        import inspect
        import bot
        src = inspect.getsource(bot.backup_data_job)
        assert "rotate_jsonl_logs" not in src
        assert "rotation_text" in src
        # e `run_backup` la chiama davvero
        src_bm = inspect.getsource(backup_manager.run_backup)
        assert "rotate_jsonl_logs" in src_bm


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
