"""test_storage_status.py — salute del VOLUME (spazio + backup), TUTTI OFFLINE.

Perche' esiste: il 24/10/2026 il bot si e' fermato perche' il volume era al
100% (`database or disk is full` in ogni job, 231 errori `ENOSPC`). Il capitale
era intatto e nessun guardrail di denaro poteva accorgersene: il guasto e'
SILENZIOSO. Questa suite difende le tre cose che rendono il guasto visibile
PRIMA:

1. le soglie dichiarano `ok` / `warn` / `critical` con il motivo
   machine-readable (mai un semaforo senza spiegazione);
2. il conto che ha causato l'incidente — `snapshot medio x keep` contro il
   volume — deve diventare un WARNING, non un numero da scoprire dopo;
3. la superficie: `/api/dashboard` e `/api/health` espongono lo stato, cosi'
   un monitor esterno vede il disco pieno come vede un errore HTTP.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

import backup_manager as bm
import web_api as wa


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _mk_backup(root, name, status="ok", size_files=1, size_bytes=1024):
    """Crea uno snapshot finto con manifest (o senza) e N file da `size_bytes`."""
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    for i in range(size_files):
        (d / f"f{i}.bin").write_bytes(b"x" * size_bytes)
    if status is not None:
        (d / bm.MANIFEST_NAME).write_text(
            json.dumps({"schema": bm.MANIFEST_SCHEMA, "stamp": name,
                        "status": status}), encoding="utf-8")
    return d


def _stamp(hours_ago: float) -> str:
    when = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return when.strftime("%Y%m%d-%H%M%S-%f")


class _Usage:
    def __init__(self, total_mb, free_mb):
        self.total = int(total_mb * 1024 * 1024)
        self.free = int(free_mb * 1024 * 1024)
        self.used = self.total - self.free


# ---------------------------------------------------------------------------
# Volume
# ---------------------------------------------------------------------------

class TestVolume:
    def test_volume_riporta_mb_e_percentuali(self, tmp_path, monkeypatch):
        monkeypatch.setattr(bm.shutil, "disk_usage",
                            lambda p: _Usage(400, 300))
        out = bm.storage_status(data_dir=tmp_path, backup_root=tmp_path / "b")
        v = out["volume"]
        assert v["total_mb"] == 400.0
        assert v["free_mb"] == 300.0
        assert v["used_mb"] == 100.0
        assert v["used_pct"] == 25.0
        assert v["free_pct"] == 75.0

    def test_spazio_abbondante_e_ok(self, tmp_path, monkeypatch):
        monkeypatch.setattr(bm.shutil, "disk_usage",
                            lambda p: _Usage(400, 300))
        root = tmp_path / "b"
        _mk_backup(root, _stamp(1), "ok")
        out = bm.storage_status(data_dir=tmp_path, backup_root=root)
        assert out["level"] == "ok" and out["ok"] is True
        assert out["warnings"] == []

    def test_disco_quasi_pieno_e_critico(self, tmp_path, monkeypatch):
        # 5% libero: sotto la soglia critica (8%) E sotto i 40 MB minimi.
        monkeypatch.setattr(bm.shutil, "disk_usage",
                            lambda p: _Usage(400, 20))
        root = tmp_path / "b"
        _mk_backup(root, _stamp(1), "ok")
        out = bm.storage_status(data_dir=tmp_path, backup_root=root)
        assert out["level"] == "critical"
        assert "disco_quasi_pieno" in out["warnings"]
        assert out["ok"] is False

    def test_disco_pieno_ma_non_critico_e_warn(self, tmp_path, monkeypatch):
        # 15% libero: sotto la soglia di attenzione (20%), sopra la critica.
        monkeypatch.setattr(bm.shutil, "disk_usage",
                            lambda p: _Usage(400, 60))
        root = tmp_path / "b"
        _mk_backup(root, _stamp(1), "ok")
        out = bm.storage_status(data_dir=tmp_path, backup_root=root)
        assert out["level"] == "warn"
        assert "disco_pieno" in out["warnings"]

    def test_lettura_volume_rotta_non_solleva(self, tmp_path, monkeypatch):
        def boom(p):
            raise OSError("volume sparito")
        monkeypatch.setattr(bm.shutil, "disk_usage", boom)
        out = bm.storage_status(data_dir=tmp_path)
        assert out["level"] == "unknown"
        assert out["ok"] is False
        assert "volume" in (out["error"] or "")


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------

class TestBackup:
    def test_conta_snapshot_keep_e_dimensioni(self, tmp_path, monkeypatch):
        monkeypatch.setattr(bm.shutil, "disk_usage",
                            lambda p: _Usage(400, 300))
        root = tmp_path / "b"
        # File da 200 KB, non da 1 KB: la dimensione totale e' arrotondata a un
        # decimale di MB, e con 10 KB lo snapshot legittimamente vale 0.0 — il
        # test finirebbe per misurare l'arrotondamento invece del conteggio.
        _mk_backup(root, _stamp(2), "ok", size_files=10, size_bytes=200 * 1024)
        _mk_backup(root, _stamp(1), "ok", size_files=10, size_bytes=200 * 1024)
        out = bm.storage_status(data_dir=tmp_path, backup_root=root)
        b = out["backups"]
        assert b["count"] == 2
        assert b["latest"] == max(os.listdir(root))
        assert b["latest_status"] == "ok"
        assert b["latest_age_h"] is not None and b["latest_age_h"] <= 2
        assert b["statuses"] == {"ok": 2}
        assert b["total_mb"] > 0

    def test_nessun_backup_e_warn(self, tmp_path, monkeypatch):
        monkeypatch.setattr(bm.shutil, "disk_usage",
                            lambda p: _Usage(400, 300))
        out = bm.storage_status(data_dir=tmp_path, backup_root=tmp_path / "empty")
        assert out["level"] == "warn"
        assert "nessun_backup" in out["warnings"]
        assert out["backups"]["count"] == 0

    def test_ultimo_backup_parziale_e_warn(self, tmp_path, monkeypatch):
        monkeypatch.setattr(bm.shutil, "disk_usage",
                            lambda p: _Usage(400, 300))
        root = tmp_path / "b"
        _mk_backup(root, _stamp(1), "partial")
        out = bm.storage_status(data_dir=tmp_path, backup_root=root)
        assert out["level"] == "warn"
        assert "ultimo_backup_partial" in out["warnings"]

    def test_ultimo_backup_senza_manifest_e_unknown(self, tmp_path, monkeypatch):
        monkeypatch.setattr(bm.shutil, "disk_usage",
                            lambda p: _Usage(400, 300))
        root = tmp_path / "b"
        _mk_backup(root, _stamp(1), status=None)
        out = bm.storage_status(data_dir=tmp_path, backup_root=root)
        assert out["backups"]["latest_status"] == "unknown"
        assert "ultimo_backup_unknown" in out["warnings"]

    def test_backup_vecchio_e_warn(self, tmp_path, monkeypatch):
        monkeypatch.setattr(bm.shutil, "disk_usage",
                            lambda p: _Usage(400, 300))
        root = tmp_path / "b"
        _mk_backup(root, _stamp(96), "ok")           # 4 giorni
        out = bm.storage_status(data_dir=tmp_path, backup_root=root)
        assert out["backups"]["latest_age_h"] >= 90
        assert "backup_vecchio" in out["warnings"]

    def test_backup_oltre_keep_e_warn(self, tmp_path, monkeypatch):
        monkeypatch.setattr(bm.shutil, "disk_usage",
                            lambda p: _Usage(400, 300))
        monkeypatch.setenv("BACKUP_KEEP", "2")
        root = tmp_path / "b"
        for h in (1, 2, 3):
            _mk_backup(root, _stamp(h), "ok")
        out = bm.storage_status(data_dir=tmp_path, backup_root=root)
        assert out["backups"]["keep"] == 2
        assert out["backups"]["over_keep"] is True
        assert "backup_oltre_keep" in out["warnings"]

    def test_il_conto_che_ha_causato_l_incidente_diventa_un_warning(
            self, tmp_path, monkeypatch):
        """24/10: 5 snapshot da ~70 MB con keep 7 su un volume da 434 MB.

        La dimensione degli snapshot e' INIETTATA (`_size_mb`): il soggetto
        qui e' l'aritmetica `media x keep` contro il volume, non
        l'allocazione di centinaia di MB su disco — un fixture che scrive
        20.000 file per snapshot riempie il volume della macchina di test
        (successo davvero: `ENOSPC` mentre girava questa suite).
        """
        monkeypatch.setattr(bm, "_size_mb", lambda p: 20.0)
        monkeypatch.setattr(bm.shutil, "disk_usage",
                            lambda p: _Usage(434, 200))
        monkeypatch.setenv("BACKUP_KEEP", "7")
        root = tmp_path / "b"
        _mk_backup(root, _stamp(1), "ok")
        out = bm.storage_status(data_dir=tmp_path, backup_root=root)
        # 20 MB x 7 = 140 MB < 90% di 434: qui NON deve allarmare
        assert "backup_troppo_grande_per_il_volume" not in out["warnings"]

        root2 = tmp_path / "b2"
        for i in range(5):
            _mk_backup(root2, _stamp(i + 1), "ok")
        out2 = bm.storage_status(data_dir=tmp_path, backup_root=root2)
        # 5 x 20 MB = 100 MB con keep 7: 140 MB, ancora sotto i 390 MB di soglia
        assert "backup_troppo_grande_per_il_volume" not in out2["warnings"]
        # Volume piccolo (120 MB): la stessa media non ci sta piu'.
        monkeypatch.setattr(bm.shutil, "disk_usage",
                            lambda p: _Usage(120, 40))
        out3 = bm.storage_status(data_dir=tmp_path, backup_root=root2)
        assert "backup_troppo_grande_per_il_volume" in out3["warnings"]

    def test_backup_illeggibili_non_sollevano(self, tmp_path, monkeypatch):
        monkeypatch.setattr(bm.shutil, "disk_usage",
                            lambda p: _Usage(400, 300))
        out = bm.storage_status(data_dir=tmp_path,
                                backup_root=tmp_path / "non_esiste")
        assert out["backups"]["count"] == 0
        assert out["level"] in ("warn", "unknown")


# ---------------------------------------------------------------------------
# Superficie: dashboard e health
# ---------------------------------------------------------------------------

class TestSuperfici:
    def test_dashboard_espone_storage(self, monkeypatch):
        sentinella = {"ok": True, "level": "ok", "volume": {"free_mb": 123.0},
                      "backups": {"count": 2}, "warnings": []}
        monkeypatch.setattr(bm, "storage_status", lambda *a, **k: sentinella)
        out = wa._dashboard_json()
        assert out["storage"] == sentinella

    def test_health_espone_storage_Compatto(self, monkeypatch):
        monkeypatch.setattr(bm, "storage_status", lambda *a, **k: {
            "ok": False, "level": "critical",
            "volume": {"free_mb": 12.0, "free_pct": 3.0},
            "backups": {"count": 5, "latest_status": "partial"},
            "warnings": ["disco_quasi_pieno"]})
        out = wa._health_json()
        st = out["storage"]
        assert st["level"] == "critical"
        assert st["free_mb"] == 12.0
        assert st["backups"] == 5
        assert st["latest_backup"] == "partial"
        assert "disco_quasi_pieno" in st["warnings"]

    def test_dashboard_non_esplode_se_la_lettura_fallisce(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("disco illeggibile")
        monkeypatch.setattr(bm, "storage_status", boom)
        out = wa._dashboard_json()
        assert out["storage"]["ok"] is False
        assert "disco illeggibile" in (out["storage"]["error"] or "")

    def test_health_non_esplode_se_la_lettura_fallisce(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("disco illeggibile")
        monkeypatch.setattr(bm, "storage_status", boom)
        out = wa._health_json()
        assert out["storage"]["level"] == "unknown"
        assert out["status"] == "ok"
