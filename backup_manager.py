"""backup_manager.py — Backup centralizzato del database e dei dati.

Usato da:
  - bot.backup_data_job (giornaliero, 03:30 UTC + snapshot all'avvio)
  - comando Telegram /backup (manuale, solo admin)
  - CLI: venv/bin/python backup_manager.py [--check] [--dir /path]

Contenuto di ogni snapshot (data/backups/<timestamp>/):
  - quotaverace.db        : copia consistente via SQLite backup API
  - training_dataset.csv  : dataset ML rigenerato ADESSO (fresco, deduplicato)
  - ml_export.json        : righe del dataset in JSON (ridondanza di formato)
  - data/...              : cache, modelli, log ordini, saltate.json, ecc.
  - backup.json           : MANIFEST con lo stato di validita' dello snapshot

VALIDITA' (23/10/2026) — un solo criterio, `classify()`:
  - `failed`  : DB assente o `PRAGMA quick_check` diverso da `ok` (un backup
                senza il database non e' un backup);
  - `partial` : DB valido ma dati incompleti/errori (voci di `data/` non
                copiate, export ML fallito, altri errori dichiarati);
  - `ok`      : DB valido e nessun errore;
  - `unknown` : snapshot SENZA manifest (o con manifest illeggibile). Non e'
                mai considerato valido: prima del manifest non c'era modo di
                distinguere un backup completo da uno interrotto, e i vecchi
                snapshot restano classificati cosi' finche' non ne viene
                scritto uno nuovo.
`summary["status"]` / `summary["ok"]` sono quei valori: log, `/backup` e CLI li
leggono invece di dedurre il successo dalla sola presenza del DB.

ROTAZIONE — un solo responsabile: `run_backup`. La pulizia (`prune()`)
conserva i `BACKUP_KEEP` snapshot VALIDI piu' recenti (e comunque sempre
l'ultimo valido), lo snapshot appena creato e il piu' recente snapshot
`sconosciuto` (per poterlo ispezionare). Prima la rotazione poteva cancellare
l'unico backup valido perche' contava solo il numero di cartelle.

PRIMA della copia esegue `telemetry_logs.rotate_jsonl_logs()` (23/10/2026): la
copia prende TUTTA `data/`, quindi i JSONL di telemetria vanno compattati prima,
altrimenti crescono dentro ogni snapshot. L'esito della rotazione e' dichiarato
in `summary["rotation"]`/`rotation_error` e nel manifest; la rotazione e' FATTA
QUI, non anche nel job, cosi' l'esito che si legge e' quello vero.
"""
from __future__ import annotations

import json
import logging
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

DEFAULT_KEEP = 7

MANIFEST_NAME = "backup.json"
MANIFEST_SCHEMA = 1

STATUS_OK = "ok"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"
STATUS_UNKNOWN = "unknown"
STATUSES = (STATUS_OK, STATUS_PARTIAL, STATUS_FAILED, STATUS_UNKNOWN)


def _db_path() -> Path:
    from tracker import DB_PATH
    return DB_PATH


def _data_dir() -> Path:
    from config import DATA_DIR
    return DATA_DIR


def _keep() -> int:
    try:
        return max(1, int(__import__("os").getenv("BACKUP_KEEP", str(DEFAULT_KEEP))))
    except ValueError:
        return DEFAULT_KEEP


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def backup_integrity_check(db_path: Path) -> Dict:
    """Integrity check rapido sulla copia: quick_check + conteggi chiave."""
    out = {"integrity": "unknown", "tables": {}}
    try:
        conn = sqlite3.connect(str(db_path))
        c = conn.cursor()
        res = c.execute("PRAGMA quick_check").fetchone()
        out["integrity"] = res[0] if res else "unknown"
        for t in ("predictions", "bets", "clv_history", "match_results"):
            try:
                out["tables"][t] = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except sqlite3.OperationalError:
                out["tables"][t] = None
        conn.close()
    except Exception as e:
        out["integrity"] = f"error: {e}"
    return out


def export_ml_dataset(dest_dir: Path) -> Dict:
    """Esporta il dataset ML FRESCO (rigenerato ora, gia' deduplicato).

    Il DB contiene le righe sorgente; qui si aggiunge una copia pronta per
    l'addestramento cosi' ogni snapshot e' autosufficiente per il ML.
    """
    out = {"csv": None, "json_rows": 0, "error": None}
    try:
        from ml_dataset import build_training_rows, export_csv
        n_csv = export_csv(dest_dir / "training_dataset.csv")
        rows = build_training_rows()
        with (dest_dir / "ml_export.json").open("w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, default=str)
        out["csv"] = n_csv
        out["json_rows"] = len(rows)
    except Exception as e:
        out["error"] = str(e)
        logger.warning("backup: export ML fallito: %s", e)
    return out


# ---------------------------------------------------------------------------
# Criterio UNICO di validita'
# ---------------------------------------------------------------------------

def classify(summary: Dict) -> Dict:
    """Stato di uno snapshot: `ok` / `partial` / `failed` + i motivi.

    Un solo posto decide se un backup e' valido: log, `/backup`, CLI e pulizia
    leggono questo (`summary["status"]`), quindi non possono divergere. Il
    DB assente o non integro e' un FALLIMENTO (non un "parziale"): senza il
    database non c'e' nulla da ripristinare.
    """
    reasons = []
    db = summary.get("db")
    if not db:
        reasons.append("db_assente")
    else:
        integrita = str(db.get("integrity") or "").strip().lower()
        if integrita != "ok":
            reasons.append(f"integrita_db_{integrita or 'sconosciuta'}")
    data_errors = int(summary.get("data_errors") or 0)
    if summary.get("data_partial") or data_errors:
        reasons.append(f"data_{data_errors}_voci_non_copiate")
    if (summary.get("ml") or {}).get("error"):
        reasons.append("ml_export_fallito")
    altri = [e for e in (summary.get("errors") or [])
             if not str(e).startswith(("db:", "data/"))]
    if altri:
        reasons.append(f"altri_errori_{len(altri)}")
    if any(r == "db_assente" or r.startswith("integrita_db") for r in reasons):
        status = STATUS_FAILED
    elif reasons:
        status = STATUS_PARTIAL
    else:
        status = STATUS_OK
    return {"status": status, "reasons": reasons, "ok": status == STATUS_OK}


def _apply_status(summary: Dict) -> str:
    """Scrive `status`/`ok`/`reasons` nel riepilogo e ritorna lo stato."""
    verdict = classify(summary)
    summary["status"] = verdict["status"]
    summary["ok"] = verdict["ok"]
    summary["reasons"] = verdict["reasons"]
    return verdict["status"]


# ---------------------------------------------------------------------------
# Manifest + classificazione degli snapshot ESISTENTI
# ---------------------------------------------------------------------------

def manifest_path(snapshot) -> Path:
    return Path(snapshot) / MANIFEST_NAME


def read_manifest(snapshot) -> Optional[Dict]:
    """Manifest dello snapshot, o None se assente/illeggibile (fail-safe)."""
    path = manifest_path(snapshot)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("backup: manifest illeggibile in %s: %s", snapshot, e)
        return None
    return data if isinstance(data, dict) else None


def snapshot_status(snapshot) -> str:
    """Stato di uno snapshot esistente: senza manifest e' `unknown`.

    `unknown` NON e' un successo: prima del 23/10/2026 nessuno scriveva un
    manifest, quindi un backup interrotto era indistinguibile da uno completo
    e la pulizia non poteva proteggere quello valido.
    """
    manifest = read_manifest(snapshot)
    if not manifest:
        return STATUS_UNKNOWN
    status = str(manifest.get("status") or "").strip().lower()
    return status if status in STATUSES else STATUS_UNKNOWN


def snapshot_is_valid(snapshot) -> bool:
    return snapshot_status(snapshot) == STATUS_OK


# ---------------------------------------------------------------------------
# Salute dello spazio: un volume PIENO e' un interruttore SILENZIOSO
# ---------------------------------------------------------------------------
# Il 24/10/2026 il bot si e' fermato perche' il volume era al 100%
# (`database or disk is full` in ogni job, 231 errori ENOSPC): `BACKUP_KEEP`
# era a 7 con snapshot da ~70 MB su un volume da 434 MB. Nessun guardrail di
# DENARO poteva accorgersene — il capitale era intatto, il bot semplicemente
# non poteva scrivere. Questa funzione rende lo stato leggibile prima che
# succeda: la legge `/api/dashboard` e la mostra la webapp.

# Soglie (percentuale di spazio LIBERO). Sono soglie di ESERCIZIO, non di
# strategia: non toccano il denaro, dichiarano quando il disco sta per
# diventare un problema operativo.
WARN_FREE_PCT = 20.0
CRITICAL_FREE_PCT = 8.0
MIN_FREE_MB = 40.0
STALE_BACKUP_H = 48.0


def _dir_age_hours(path: Path) -> Optional[float]:
    """Eta' in ore di uno snapshot, dal nome (stamp) o dal mtime."""
    try:
        stamp = path.name
        dt = datetime.strptime(stamp, "%Y%m%d-%H%M%S-%f").replace(
            tzinfo=timezone.utc)
    except Exception:
        try:
            dt = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        except Exception:
            return None
    return round((datetime.now(timezone.utc) - dt).total_seconds() / 3600.0, 2)


def _size_mb(path) -> float:
    try:
        total = 0
        for p in Path(path).rglob("*"):
            try:
                if p.is_file():
                    total += p.stat().st_size
            except OSError:
                continue
        return round(total / (1024 * 1024), 1)
    except Exception:
        return 0.0


def storage_status(data_dir=None, backup_root=None) -> Dict:
    """Spazio del volume + stato dei backup (SOLA LETTURA, fail-safe).

    Ritorna:
        {"ok", "level": ok|warn|critical|unknown,
         "volume": {path, total_mb, used_mb, free_mb, used_pct, free_pct},
         "backups": {count, keep, total_mb, latest, latest_status,
                    latest_age_h, statuses, over_keep},
         "thresholds": {...}, "warnings": [motivi machine-readable],
         "details": [testo leggibile]}

    Non solleva MAI: una dashboard che esplode e' peggio di una dashboard che
    dichiara `ok: False` con il motivo.
    """
    out: Dict = {
        "ok": False, "level": "unknown", "volume": {}, "backups": {},
        "thresholds": {"warn_free_pct": WARN_FREE_PCT,
                        "critical_free_pct": CRITICAL_FREE_PCT,
                        "min_free_mb": MIN_FREE_MB,
                        "stale_backup_h": STALE_BACKUP_H},
        "warnings": [], "details": [], "error": None,
    }
    try:
        data = Path(data_dir) if data_dir is not None else _data_dir()
        root = Path(backup_root) if backup_root is not None else data / "backups"
    except Exception as e:                       # pragma: no cover - difensivo
        out["error"] = str(e)
        return out

    # 1) VOLUME
    try:
        usage = shutil.disk_usage(str(data))
        total_mb = round(usage.total / (1024 * 1024), 1)
        free_mb = round(usage.free / (1024 * 1024), 1)
        used_mb = round((usage.total - usage.free) / (1024 * 1024), 1)
        used_pct = round(100.0 * (usage.total - usage.free) / usage.total, 1) \
            if usage.total else 0.0
        out["volume"] = {"path": str(data), "total_mb": total_mb,
                         "used_mb": used_mb, "free_mb": free_mb,
                         "used_pct": used_pct,
                         "free_pct": round(100.0 - used_pct, 1)}
    except Exception as e:
        out["error"] = f"volume: {e}"
        return out

    # 2) BACKUP
    snaps = []
    try:
        if root.is_dir():
            snaps = sorted((p for p in root.iterdir() if p.is_dir()),
                           key=lambda p: p.name, reverse=True)
    except Exception as e:
        out["error"] = f"backups: {e}"
    statuses: Dict[str, int] = {}
    for s in snaps:
        st = snapshot_status(s)
        statuses[st] = statuses.get(st, 0) + 1
    latest = snaps[0] if snaps else None
    keep = _keep()
    out["backups"] = {
        "count": len(snaps), "keep": keep,
        "total_mb": round(sum(_size_mb(s) for s in snaps), 1),
        "latest": latest.name if latest else None,
        "latest_status": snapshot_status(latest) if latest else None,
        "latest_age_h": _dir_age_hours(latest) if latest else None,
        "statuses": statuses,
        "over_keep": len(snaps) > keep,
    }

    # 3) GIUDIZIO (il primo motivo vince il livello)
    free_pct = out["volume"].get("free_pct") or 0.0
    free_mb = out["volume"].get("free_mb") or 0.0
    if free_pct <= CRITICAL_FREE_PCT or free_mb < MIN_FREE_MB:
        out["warnings"].append("disco_quasi_pieno")
        out["level"] = "critical"
    elif free_pct <= WARN_FREE_PCT:
        out["warnings"].append("disco_pieno")
        out["level"] = "warn"
    else:
        out["level"] = "ok"

    b = out["backups"]
    if not b["count"]:
        out["warnings"].append("nessun_backup")
        if out["level"] == "ok":
            out["level"] = "warn"
    else:
        if b["latest_status"] != STATUS_OK:
            out["warnings"].append(f"ultimo_backup_{b['latest_status']}")
            if out["level"] == "ok":
                out["level"] = "warn"
        if b["latest_age_h"] is not None and b["latest_age_h"] > STALE_BACKUP_H:
            out["warnings"].append("backup_vecchio")
            if out["level"] == "ok":
                out["level"] = "warn"
        if b["over_keep"]:
            out["warnings"].append("backup_oltre_keep")
            if out["level"] == "ok":
                out["level"] = "warn"
        # Il conto che ha causato l'incidente del 24/10: `keep x snapshot`
        # contro il volume. Se non ci starebbero DUE volte, il backup di boot
        # e' un interruttore che spegne il bot.
        avg = (b["total_mb"] / b["count"]) if b["count"] else 0.0
        if avg and (avg * b["keep"]) > (out["volume"]["total_mb"] * 0.9):
            out["warnings"].append("backup_troppo_grande_per_il_volume")
            if out["level"] == "ok":
                out["level"] = "warn"

    out["ok"] = out["level"] == "ok"
    out["details"] = [
        f"volume: {out['volume']['used_mb']:.0f}/{out['volume']['total_mb']:.0f} MB "
        f"({out['volume']['used_pct']:.0f}% usato, {out['volume']['free_mb']:.0f} MB liberi)",
        f"backup: {b['count']} snapshot ({b['total_mb']:.0f} MB), keep {b['keep']}, "
        f"ultimo {b['latest'] or '—'} ({b['latest_status'] or '—'}"
        + (f", {b['latest_age_h']:.1f}h" if b["latest_age_h"] is not None else "")
        + ")",
    ]
    return out


def _write_manifest(dest: Path, summary: Dict) -> Optional[str]:
    """Scrive `backup.json` in modo atomico. Ritorna l'errore (None = ok).

    Un manifest che non si scrive rende lo snapshot NON valido (sarebbe
    `unknown` al giro successivo): e' dichiarato, non silenzioso.
    """
    payload = {
        "schema": MANIFEST_SCHEMA,
        "stamp": summary.get("stamp"),
        "created_at": summary.get("created_at") or _utc_now(),
        "status": summary.get("status"),
        "ok": bool(summary.get("ok")),
        "reasons": summary.get("reasons") or [],
        "db": summary.get("db"),
        "ml": summary.get("ml"),
        "data_files": summary.get("data_files", 0),
        "data_errors": summary.get("data_errors", 0),
        "data_partial": bool(summary.get("data_partial")),
        "rotation": summary.get("rotation"),
        "rotation_error": summary.get("rotation_error"),
        "errors": (summary.get("errors") or [])[:20],
        "kept": summary.get("kept"),
        "pruned": summary.get("pruned") or [],
    }
    tmp = manifest_path(dest).with_name(MANIFEST_NAME + ".tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(manifest_path(dest))
        return None
    except Exception as e:
        logger.error("backup: manifest NON scritto in %s: %s", dest, e)
        summary["errors"].append(f"manifest: {e}")
        summary["reasons"] = list(summary.get("reasons") or []) + ["manifest_non_scritto"]
        summary["ok"] = False
        if summary.get("status") == STATUS_OK:
            summary["status"] = STATUS_PARTIAL
        return str(e)


# ---------------------------------------------------------------------------
# Pulizia: protegge l'ultimo backup VALIDO
# ---------------------------------------------------------------------------

def prune(backup_root: Path, keep: int,
          current: Optional[Path] = None,
          current_status: Optional[str] = None) -> Dict:
    """Cancella gli snapshot NON protetti; ritorna cosa e' stato rimosso.

    Protetti, sempre:
      * i `keep` snapshot validi piu' recenti — quindi **mai l'ultimo valido**,
        anche con `keep=1` e anche se il piu' recente e' parziale;
      * lo snapshot appena creato (`current`);
      * il piu' recente snapshot `unknown` (un backup pre-manifest resta
        ispezionabile; non viene contato fra i validi).
    """
    out: Dict = {"keep": max(int(keep), 1), "removed": [], "kept": 0,
                 "protected": 0, "error": None}
    try:
        snaps = sorted((p for p in Path(backup_root).iterdir() if p.is_dir()),
                       key=lambda p: p.name, reverse=True)      # recente prima
    except Exception as e:
        logger.error("backup: errore pulizia: %s", e)
        out["error"] = str(e)
        return out
    current = Path(current) if current is not None else None
    status = {p: snapshot_status(p) for p in snaps}
    if current is not None:
        status[current] = (current_status or status.get(current) or STATUS_UNKNOWN)
    if not snaps and current is None:
        return out                          # niente da pulire (non e' un errore)
    valid = [p for p in snaps if status.get(p) == STATUS_OK]
    protected = set(valid[:out["keep"]])
    if valid:
        protected.add(valid[0])            # esplicito: l'ultimo valido e' sacro
    if current is not None:
        protected.add(current)
    elif snaps:
        protected.add(snaps[0])            # il piu' recente non si cancella mai
    unknown = [p for p in snaps if status.get(p) == STATUS_UNKNOWN]
    if unknown:
        protected.add(unknown[0])
    for old in sorted(snaps, key=lambda p: p.name):          # dal piu' vecchio
        if old in protected:
            continue
        shutil.rmtree(old, ignore_errors=True)
        out["removed"].append(old.name)
    out["kept"] = len(snaps) - len(out["removed"])
    out["protected"] = len(protected)
    if out["removed"]:
        logger.info("backup: pulizia — rimossi %d snapshot (%s), protetti %d "
                    "(keep %d validi + corrente + legacy)",
                    len(out["removed"]), ", ".join(out["removed"]),
                    out["protected"], out["keep"])
    return out


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------

def run_backup(dest_root: Optional[Path] = None, keep: Optional[int] = None) -> Dict:
    """Esegue uno snapshot completo. Ritorna il riepilogo (dict).

    Mai eccezioni verso il chiamante: ogni errore viene loggato e riportato
    nel riepilogo. Lo stato (`summary["status"]`) e' deciso da `classify()`:
    un backup parziale o senza DB NON si presenta come completato.
    """
    data_dir = _data_dir()
    backup_root = Path(dest_root) if dest_root else data_dir / "backups"
    # Timestamp con microsecondi: snapshot ravvicinati (es. test o /backup
    # manuale ripetuto) non si sovrascrivono.
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    dest = backup_root / stamp
    dest.mkdir(parents=True, exist_ok=True)

    summary: Dict = {"stamp": stamp, "dest": str(dest), "created_at": _utc_now(),
                     "db": None, "ml": None, "data_files": 0, "data_errors": 0,
                     "data_partial": False, "rotation": None,
                     "rotation_error": None, "errors": []}

    # 0) ROTAZIONE dei JSONL di telemetria PRIMA della copia (23/10/2026).
    # Lo snapshot copia TUTTA `data/`: senza questo, due log di telemetria
    # scritti ogni 60s (`book_flow_events` 79 MB, `chief_cycles` 19 MB) finivano
    # interi in ogni snapshot e il volume da 434 MB andava in
    # `No space left on device` — i backup del 23/10 si sono chiusi parziali.
    # Responsabile UNICO: qui (il job non ruota piu' per conto suo, altrimenti
    # l'esito che si leggeva poteva essere di una rotazione diversa).
    # Fail-safe: un problema di rotazione non impedisce il backup.
    try:
        from telemetry_logs import rotate_jsonl_logs
        # `root` esplicito: telemetry_logs tiene `DATA_DIR` all'IMPORT, quindi
        # senza questo argomento la rotazione guarderebbe una cartella diversa
        # da quella che stiamo per copiare (e nei test quella di produzione).
        summary["rotation"] = rotate_jsonl_logs(root=data_dir)
    except Exception as e:
        summary["rotation_error"] = str(e)
        logger.warning("backup: rotazione log non eseguita: %s", e)

    # 1) DB via backup API (consistente con connessioni aperte)
    try:
        src_db = _db_path()
        if src_db.exists():
            out_db = dest / "quotaverace.db"
            dst = sqlite3.connect(str(out_db))
            with sqlite3.connect(str(src_db)) as src:
                src.backup(dst)
            dst.close()
            summary["db"] = backup_integrity_check(out_db)
        else:
            summary["errors"].append(f"db: sorgente assente ({src_db})")
            logger.error("backup: DB sorgente assente in %s", src_db)
    except Exception as e:
        logger.error("backup: errore DB: %s", e)
        summary["errors"].append(f"db: {e}")

    # 2) Dataset ML fresco (csv + json)
    summary["ml"] = export_ml_dataset(dest)

    # 3) Copia data/ (scan, cache, modelli, ordini...) — UNA voce per volta.
    # Prima un solo errore abortiva l'intero ciclo (bastava un file): lo
    # snapshot risultava con 25 voci su ~30 e il log diceva comunque
    # "integrity: ok", cioe' un rapporto che sembrava un successo su un backup
    # PARZIALE (trovato il 23/10 mentre verificavo il volume pieno).
    try:
        items = [i for i in data_dir.iterdir()
                 if i.name != "backups" and i != dest]
    except Exception as e:
        logger.error("backup: data/ illeggibile: %s", e)
        summary["errors"].append(f"data: {e}")
        items = []
    disk_full = False
    for item in items:
        if disk_full:
            # Spazio finito: continuare produce una sfilza di errori identici
            # e allunga il job per niente. Si dichiara e ci si ferma.
            summary["data_errors"] += 1
            continue
        target = dest / item.name
        try:
            if item.is_dir():
                shutil.copytree(item, target, dirs_exist_ok=True)
            else:
                shutil.copy2(item, target)
            summary["data_files"] += 1
        except OSError as e:
            summary["data_errors"] += 1
            summary["errors"].append(f"data/{item.name}: {e}")
            if getattr(e, "errno", None) == 28:        # ENOSPC
                disk_full = True
                logger.error("backup: SPAZIO ESAURITO durante la copia di "
                             "data/%s — snapshot PARZIALE (DB e dataset ML "
                             "sono scritti): liberare spazio o aumentare il "
                             "volume", item.name)
        except Exception as e:
            summary["data_errors"] += 1
            summary["errors"].append(f"data/{item.name}: {e}")
    if summary["data_errors"]:
        summary["data_partial"] = True

    # 4) Stato (criterio unico) + pulizia che non puo' toccare l'ultimo valido.
    status = _apply_status(summary)
    k = keep if keep is not None else _keep()
    pruned = prune(backup_root, k, current=dest, current_status=status)
    summary["kept"] = pruned.get("kept")
    summary["pruned"] = pruned.get("removed") or []
    summary["protected"] = pruned.get("protected")
    if pruned.get("error"):
        summary["errors"].append(f"pulizia: {pruned['error']}")

    # 5) Manifest DOPO la pulizia: contiene stato, rotazione ed esito pulizia.
    #    Se il manifest non si scrive lo snapshot non e' valido (vedi sopra).
    _write_manifest(dest, summary)

    integ = ((summary["db"] or {}).get("integrity") or "n/d") if summary["db"] else "n/d"
    if summary["status"] == STATUS_OK:
        logger.info("backup: snapshot %s → %s VALIDO (integrity: %s, %d file "
                    "data, %s)", stamp, dest, integ, summary["data_files"],
                    rotation_text(summary))
    elif summary["status"] == STATUS_PARTIAL:
        # Mai un "integrity: ok" che si legge come successo quando la copia di
        # `data/` e' incompleta: DB e dataset ML ci sono, il resto no.
        logger.warning("backup: snapshot %s → %s PARZIALE — %s (integrity DB: "
                       "%s, %d file copiati, %d NON copiati)",
                       stamp, dest, ", ".join(summary["reasons"]), integ,
                       summary["data_files"], summary["data_errors"])
    else:
        logger.error("backup: snapshot %s → %s NON VALIDO (%s): %s", stamp, dest,
                     summary["status"], ", ".join(summary["reasons"]) or "n/d")
    return summary


def rotation_text(summary: Dict) -> str:
    """Esito della rotazione JSONL, in una riga (report/log/manifest)."""
    error = summary.get("rotation_error")
    if error:
        return f"rotazione JSONL: NON eseguita ({error})"
    rot = summary.get("rotation")
    if not isinstance(rot, dict):
        return "rotazione JSONL: non eseguita"
    n = len(rot.get("rotated") or [])
    if not n:
        return "rotazione JSONL: nessun file da compattare"
    return ("rotazione JSONL: %d compattati (%.1f MB -> %.1f MB)"
            % (n, (rot.get("bytes_before") or 0) / 1048576,
               (rot.get("bytes_after") or 0) / 1048576))


def format_backup_report(s: Dict) -> str:
    """Riepilogo compatto per Telegram (stato derivato dal criterio unico)."""
    verdict = classify(s) if not s.get("status") else {
        "status": s.get("status"), "reasons": s.get("reasons") or []}
    status = verdict["status"]
    reasons = verdict["reasons"]
    head = {
        STATUS_OK: "💾 *BACKUP COMPLETATO*",
        STATUS_PARTIAL: "⚠️ *BACKUP PARZIALE*",
        STATUS_FAILED: "❌ *BACKUP FALLITO*",
        STATUS_UNKNOWN: "❔ *BACKUP NON CLASSIFICABILE*",
    }.get(status, "❔ *BACKUP NON CLASSIFICABILE*")
    db = s.get("db") or {}
    tables = db.get("tables") or {}
    ml = s.get("ml") or {}
    ml_txt = f"{ml.get('csv', 0)} righe" if ml.get("csv") is not None else "n/d"
    if ml.get("error"):
        ml_txt += " (⚠️ export fallito)"
    lines = [
        f"{head} — {s.get('stamp', 'n/d')}",
        f" Integrity DB: `{db.get('integrity', 'n/d')}`",
        f" Ledger: {tables.get('predictions', 'n/d')} previsioni, "
        f"{tables.get('bets', 'n/d')} puntate",
        f" CLV tracciati: {tables.get('clv_history', 'n/d')}",
        f" Dataset ML: {ml_txt}",
        f" File data/: {s.get('data_files', 0)} copiati, "
        f"{s.get('data_errors', 0)} non copiati",
        f" {rotation_text(s)}",
        f" Snapshot conservati: {s.get('kept', 'n/d')}",
    ]
    if reasons:
        lines.append(f" Motivi: {', '.join(reasons)}")
    errors = s.get("errors") or []
    if errors:
        lines.append(f" errori ({len(errors)}):")
        lines.extend(f"• {e}" for e in errors[:10])
    return "\n".join(lines)


def latest_backup(dest_root: Optional[Path] = None) -> Optional[Path]:
    """Path dello snapshot piu' recente (o None)."""
    root = Path(dest_root) if dest_root else _data_dir() / "backups"
    if not root.is_dir():
        return None
    snaps = sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name)
    return snaps[-1] if snaps else None


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Backup DB + dataset ML")
    ap.add_argument("--dir", type=str, default=None,
                    help="Cartella destinazione (default: data/backups)")
    ap.add_argument("--keep", type=int, default=None)
    ap.add_argument("--check", action="store_true",
                    help="Mostra solo l'ultimo snapshot senza crearne uno")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if args.check:
        last = latest_backup(args.dir)
        if not last:
            print("Nessun backup presente.")
        else:
            status = snapshot_status(last)
            print(f"{last}\nstato: {status}"
                  + (" (manifest assente o illeggibile: NON validabile)"
                     if status == STATUS_UNKNOWN else ""))
    else:
        summary = run_backup(dest_root=args.dir, keep=args.keep)
        print(format_backup_report(summary))
