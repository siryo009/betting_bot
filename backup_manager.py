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

La pulizia tiene gli ultimi BACKUP_KEEP snapshot (env BACKUP_KEEP, default 7).

PRIMA della copia esegue `telemetry_logs.rotate_jsonl_logs()` (23/10/2026): la
copia prende TUTTA `data/`, quindi i JSONL di telemetria vanno compattati prima,
altrimenti crescono dentro ogni snapshot. L'esito della copia e' dichiarato:
`data_partial` = True quando una voce non e' stata copiata (spazio esaurito,
permessi) — il log NON dice piu' "integrity: ok" facendo sembrare completo uno
snapshot incompleto.
"""
from __future__ import annotations

import json
import logging
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

DEFAULT_KEEP = 7


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


def run_backup(dest_root: Optional[Path] = None, keep: Optional[int] = None) -> Dict:
    """Esegue uno snapshot completo. Ritorna il riepilogo (dict).

    Mai eccezioni verso il chiamante: ogni errore viene loggato e riportato
    nel riepilogo (fail-soft: un backup parziale e' meglio di niente).
    """
    data_dir = _data_dir()
    backup_root = Path(dest_root) if dest_root else data_dir / "backups"
    # Timestamp con microsecondi: snapshot ravvicinati (es. test o /backup
    # manuale ripetuto) non si sovrascrivono.
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    dest = backup_root / stamp
    dest.mkdir(parents=True, exist_ok=True)

    summary: Dict = {"stamp": stamp, "dest": str(dest), "db": None,
                     "ml": None, "data_files": 0, "data_errors": 0,
                     "data_partial": False, "rotation": None, "errors": []}

    # 0) ROTAZIONE dei JSONL di telemetria PRIMA della copia (23/10/2026).
    # Lo snapshot copia TUTTA `data/`: senza questo, due log di telemetria
    # scritti ogni 60s (`book_flow_events` 79 MB, `chief_cycles` 19 MB) finivano
    # interi in ogni snapshot e il volume da 434 MB andava in
    # `No space left on device` — i backup del 23/10 si sono chiusi parziali.
    # Fail-safe: un problema di rotazione non impedisce il backup.
    try:
        from telemetry_logs import rotate_jsonl_logs
        # `root` esplicito: telemetry_logs tiene `DATA_DIR` all'IMPORT, quindi
        # senza questo argomento la rotazione guarderebbe una cartella diversa
        # da quella che stiamo per copiare (e nei test quella di produzione).
        summary["rotation"] = rotate_jsonl_logs(root=data_dir)
    except Exception as e:
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

    # 4) Pulizia: mantiene gli ultimi `keep` snapshot
    k = keep if keep is not None else _keep()
    try:
        snaps = sorted((p for p in backup_root.iterdir() if p.is_dir()),
                       key=lambda p: p.name)
        for old in snaps[:-k]:
            shutil.rmtree(old, ignore_errors=True)
        summary["kept"] = min(len(snaps), k)
    except Exception as e:
        logger.error("backup: errore pulizia: %s", e)
        summary["errors"].append(f"pulizia: {e}")

    integ = (summary["db"] or {}).get("integrity", "n/d") if summary["db"] else "n/d"
    if summary["data_partial"]:
        # Mai un "integrity: ok" che si legge come successo quando la copia di
        # `data/` e' incompleta: DB e dataset ML ci sono, il resto no.
        logger.warning("backup: snapshot %s → %s PARZIALE — integrity DB: %s, "
                       "%d file data copiati, %d NON copiati (vedi gli errori "
                       "nel report)", stamp, dest, integ,
                       summary["data_files"], summary["data_errors"])
    else:
        logger.info("backup: snapshot %s → %s (integrity: %s, %d file data)",
                    stamp, dest, integ, summary["data_files"])
    return summary


def format_backup_report(s: Dict) -> str:
    """Riepilogo compatto per Telegram."""
    if s.get("errors"):
        head = f"⚠️ *Backup completato con errori* ({s['stamp']})\n"
        if s.get("data_partial"):
            head += (f"DB e dataset ML scritti; copia data/ PARZIALE "
                     f"({s.get('data_files', 0)} file copiati, "
                     f"{s.get('data_errors', 0)} non copiati)\n")
        return head + "\n".join(f"• {e}" for e in s["errors"][:10])
    db = s.get("db") or {}
    tables = db.get("tables", {})
    integ = db.get("integrity", "n/d")
    ml = s.get("ml") or {}
    ml_txt = f"{ml.get('csv', 0)} righe" if ml.get("csv") is not None else "n/d"
    if ml.get("error"):
        ml_txt += " (⚠️ export parziale)"
    return (
        f"💾 *BACKUP COMPLETATO* — {s['stamp']}\n"
        f" Integrity DB: `{integ}`\n"
        f" Ledger: {tables.get('predictions', 'n/d')} previsioni, "
        f"{tables.get('bets', 'n/d')} puntate\n"
        f" CLV tracciati: {tables.get('clv_history', 'n/d')}\n"
        f" Dataset ML: {ml_txt}\n"
        f" File data/: {s.get('data_files', 0)}\n"
        f" Snapshot conservati: {s.get('kept', 'n/d')}"
    )


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
        print(last if last else "Nessun backup presente.")
    else:
        s = run_backup(dest_root=args.dir, keep=args.keep)
        print(format_backup_report(s))
