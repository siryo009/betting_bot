"""telemetry_logs.py — Rotazione automatica dei JSONL di telemetria.

PERCHE' ESISTE (03/10/2026). Il 30/09/2026 il volume `/app/data` era al **81%**
(342 MB su 434): la causa non era il DB (5 MB) ma i **JSONL di telemetria**
(`book_flow_events` 22 MB, `chief_cycles` 11 MB, `events`, `liquidity_skips`,
`tennis_quant/evaluations`...) che crescono a ogni giro di job — e che
`backup_manager.run_backup` copiava INTERI in ogni snapshot (ogni snapshot
40-47 MB). La manutenzione fu fatta a MANO (gzip + `VACUUM`): una manutenzione
manuale su un sistema che scrive 24/7 e' una manutenzione che va rifatta ogni
volta. Questo modulo la rende AUTOMATICA e la aggancia al job di backup.

Due responsabilita', una sola definizione:
  - `rotate_jsonl_logs()`: comprime i JSONL **inattivi** (nessuna scrittura da
    `LOG_ROTATE_QUIET_MIN`) che sono troppo grandi o troppo vecchi, e pota le
    generazioni oltre `LOG_ROTATE_KEEP`;
  - `iter_events()`: il lettore UNICO per i consumatori (credit_diagnose,
    oracle_skips): legge il file vivo **e** le sue generazioni `.gz`, cosi'
    comprimere non significa perdere la storia.

Perche' il gzip e' SICURO. I writer del progetto aprono/chiudono il file a
OGNI evento (`decision.middleware.JsonlSink`, `liquidity_monitor.record_skip`,
`book_flow`, `chief_shadow_wiring`): rimuovere il file quieto non rompe nessuno,
il writer lo ricrea alla scrittura successiva. La finestra di QUIETE e' la
garanzia: non si tocca un file che qualcuno ha appena scritto.

Fail-safe totale: ogni errore e' contato e loggato, MAI propagato (un problema
di manutenzione log non deve fermare il giro puntate ne' il backup).

CLI: venv/bin/python telemetry_logs.py [--json] [--dry-run]
"""
from __future__ import annotations

import gzip
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from config import DATA_DIR

logger = logging.getLogger("telemetry_logs")

#: Quanto deve essere QUIETO un file prima di poterlo comprimere (minuti).
#: Sotto questa soglia un writer puo' ancora averlo in mano: si aspetta.
DEFAULT_QUIET_MIN = 15.0
#: Comprimi se il file supera questa dimensione (MB)...
DEFAULT_MAX_MB = 5.0
#: ...oppure se non viene toccato da questi giorni.
DEFAULT_AFTER_DAYS = 2.0
#: Quante generazioni `.gz` conservare per ogni file (le piu' recenti).
DEFAULT_KEEP = 3

_ONCE: Dict[str, bool] = {}


def _warn_once(key: str, msg: str, *args) -> None:
    if not _ONCE.get(key):
        _ONCE[key] = True
        logger.warning(msg, *args)


def _num_env(name: str, default: float, *, minimum: float = 0.0) -> float:
    """Legge un env numerico a RUNTIME; valore impossibile -> default."""
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        val = float(raw)
    except (TypeError, ValueError):
        logger.warning("telemetry_logs: %s=%r non numerico, uso %s", name, raw,
                       default)
        return default
    if val < minimum:
        logger.warning("telemetry_logs: %s=%r sotto il minimo %s, uso %s",
                       name, raw, minimum, default)
        return default
    return val


def rotate_enabled() -> bool:
    """Interruttore (default ON): env `TELEMETRY_ROTATE_ENABLED`."""
    return os.getenv("TELEMETRY_ROTATE_ENABLED", "1").strip().lower() \
        in ("1", "true", "yes", "on")


def _is_backup_path(path: Path, root: Path) -> bool:
    """True per i file dentro `backups/`: sono snapshot, non log vivi."""
    try:
        rel = path.relative_to(root)
    except ValueError:
        return False
    return "backups" in rel.parts


def _generations(path: Path) -> List[Path]:
    """Generazioni gzip di un JSONL (nomi con timbro o senza)."""
    try:
        return sorted(path.parent.glob(path.name + "*.gz"))
    except Exception:
        return []


def read_lines(path: Path) -> List[str]:
    """Righe GREZZE del file vivo + delle generazioni `.gz` (fail-safe).

    Unico punto di lettura dei log di telemetria: lo usano `iter_events` e i
    consumatori che devono vedere le righe cosi' come sono scritte (es.
    `chief_shadow_wiring`, che tiene solo la coda del file).
    """
    path = Path(path)
    out: List[str] = []
    for src, kind in ([(path, "txt")] + [(g, "gz") for g in _generations(path)]):
        try:
            if not src.exists():
                continue
            if kind == "gz":
                with gzip.open(src, "rt", encoding="utf-8",
                               errors="replace") as fh:
                    out.extend(fh.read().splitlines())
            else:
                out.extend(src.read_text(encoding="utf-8",
                                         errors="replace").splitlines())
        except Exception as e:                                   # pragma: no cover
            _warn_once(f"read:{src}", "telemetry_logs: lettura %s fallita (%s)",
                       src, e)
    return out


def iter_events(path: Path, days: Optional[float] = None) -> Iterator[dict]:
    """Eventi di un JSONL: file vivo + generazioni `.gz`, dal piu' recente.

    `path` e' il file VIVO (es. `.../credit_calls.jsonl`): le generazioni
    compresse hanno lo stesso prefisso (`credit_calls.jsonl.<stamp>.gz`).
    Righe corrotte o file illeggibili vengono IGNORATI (una telemetria rotta
    non deve rompere il report). `days` limita alla finestra [now-days, now].
    """
    path = Path(path)
    cutoff = None
    if days is not None:
        try:
            cutoff = (datetime.now(timezone.utc)
                      - timedelta(days=float(days))).timestamp()
        except Exception:
            cutoff = None
    out: List[dict] = []
    for line in read_lines(path):
        line = line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except Exception:
            continue
        if not isinstance(evt, dict):
            continue
        if cutoff is not None:
            ts = evt.get("ts_epoch")
            if not isinstance(ts, (int, float)):
                try:
                    ts = datetime.fromisoformat(
                        str(evt.get("ts", "")).replace("Z", "+00:00")
                    ).timestamp()
                except Exception:
                    ts = None
            if ts is None or ts < cutoff:
                continue
        out.append(evt)

    def _epoch(evt: dict) -> float:
        ts = evt.get("ts_epoch")
        if isinstance(ts, (int, float)):
            return float(ts)
        try:
            return datetime.fromisoformat(
                str(evt.get("ts", "")).replace("Z", "+00:00")).timestamp()
        except Exception:
            return 0.0

    out.sort(key=_epoch, reverse=True)
    yield from out


def rotate_jsonl_logs(*, root: Optional[Path] = None,
                      max_mb: Optional[float] = None,
                      after_days: Optional[float] = None,
                      quiet_min: Optional[float] = None,
                      keep: Optional[int] = None,
                      enabled: Optional[bool] = None,
                      now: Optional[float] = None,
                      dry_run: bool = False) -> Dict[str, Any]:
    """Comprimi i JSONL di telemetria inattivi. Ritorna un riepilogo.

    Rotazione = il contenuto viene compreso in
    `<nome>.jsonl.<UTCstamp>.gz` e il file VIVO viene rimosso (i writer lo
    ricreano alla prossima scrittura, comportamento verificato il 03/10).
    Un file entra in rotazione se e' grande (`LOG_ROTATE_MAX_MB`) **o** vecchio
    (`LOG_ROTATE_AFTER_DAYS`) **e** quieto da `LOG_ROTATE_QUIET_MIN` minuti.
    Le generazioni oltre `LOG_ROTATE_KEEP` (piu' recenti conservate) sono
    potate. Mai eccezioni verso il chiamante.
    """
    root = Path(root) if root is not None else Path(DATA_DIR)
    if enabled is None:
        enabled = rotate_enabled()
    res: Dict[str, Any] = {"enabled": bool(enabled), "scanned": 0, "rotated": [],
                           "pruned": 0, "bytes_before": 0, "bytes_after": 0,
                           "errors": 0, "dry_run": bool(dry_run)}
    if not enabled:
        return res
    max_mb = _num_env("LOG_ROTATE_MAX_MB", DEFAULT_MAX_MB) if max_mb is None \
        else float(max_mb)
    after_days = _num_env("LOG_ROTATE_AFTER_DAYS", DEFAULT_AFTER_DAYS) \
        if after_days is None else float(after_days)
    quiet_min = _num_env("LOG_ROTATE_QUIET_MIN", DEFAULT_QUIET_MIN) \
        if quiet_min is None else float(quiet_min)
    if keep is None:
        try:
            keep = max(1, int(_num_env("LOG_ROTATE_KEEP", DEFAULT_KEEP, minimum=1)))
        except Exception:
            keep = int(DEFAULT_KEEP)
    now_ts = time.time() if now is None else float(now)

    try:
        candidates = sorted(root.rglob("*.jsonl"))
    except Exception as e:                                       # pragma: no cover
        logger.warning("telemetry_logs: scansione fallita (%s)", e)
        res["errors"] += 1
        return res

    for path in candidates:
        if _is_backup_path(path, root):
            continue
        try:
            st = path.stat()
        except Exception:
            continue
        res["scanned"] += 1
        if st.st_size <= 0:
            continue
        age_days = (now_ts - st.st_mtime) / 86400.0
        quiet_min_elapsed = (now_ts - st.st_mtime) / 60.0
        big = st.st_size >= max_mb * 1024 * 1024
        old = age_days >= after_days
        if not (big or old):
            continue
        if quiet_min_elapsed < quiet_min:
            # attivamente scritto: si aspetta (mai comprimere sotto i piedi
            # di un writer)
            continue
        if dry_run:
            res["rotated"].append({"path": str(path), "bytes": st.st_size,
                                   "dry_run": True})
            continue
        try:
            raw = path.read_bytes()
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
            dest = path.parent / f"{path.name}.{stamp}.gz"
            with gzip.open(dest, "wb") as fh:
                fh.write(raw)
            path.unlink()
            res["bytes_before"] += len(raw)
            res["bytes_after"] += dest.stat().st_size
            res["rotated"].append({"path": str(path), "gz": str(dest),
                                   "bytes": len(raw)})
        except Exception as e:                                   # pragma: no cover
            logger.warning("telemetry_logs: rotazione %s fallita (%s)", path, e)
            res["errors"] += 1
            continue
        # Potatura delle generazioni (teniamo le `keep` piu' recenti).
        try:
            gens = _generations(path)
            for old_gen in gens[:-keep]:
                old_gen.unlink()
                res["pruned"] += 1
        except Exception as e:                                   # pragma: no cover
            logger.warning("telemetry_logs: potatura %s fallita (%s)", path, e)
            res["errors"] += 1

    if res["rotated"]:
        logger.info("telemetry_logs: ruotati %d JSONL (%.1f MB -> %.1f MB), "
                    "%d generazioni potate", len(res["rotated"]),
                    res["bytes_before"] / 1e6, res["bytes_after"] / 1e6,
                    res["pruned"])
    return res


def format_report(res: Dict[str, Any]) -> str:
    lines = ["🗂️ Rotazione log di telemetria"]
    if not res.get("enabled"):
        lines.append("  disattivata (TELEMETRY_ROTATE_ENABLED=0)")
        return "\n".join(lines)
    lines.append(f"  file scansionati: {res.get('scanned', 0)} | ruotati: "
                 f"{len(res.get('rotated') or [])} | generazioni potate: "
                 f"{res.get('pruned', 0)} | errori: {res.get('errors', 0)}")
    if res.get("bytes_before"):
        lines.append(f"  compresso: {res['bytes_before'] / 1e6:.1f} MB -> "
                     f"{res['bytes_after'] / 1e6:.1f} MB")
    for r in (res.get("rotated") or [])[:10]:
        lines.append(f"  • {Path(r['path']).name} ({r['bytes'] / 1e6:.1f} MB)")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:                # pragma: no cover
    import argparse
    ap = argparse.ArgumentParser(description="Rotazione JSONL di telemetria")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--dry-run", action="store_true",
                    help="mostra cosa verrebbe ruotato, senza toccare nulla")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    res = rotate_jsonl_logs(dry_run=args.dry_run)
    print(json.dumps(res, indent=2, ensure_ascii=False) if args.json
          else format_report(res))
    return 0


if __name__ == "__main__":                                        # pragma: no cover
    raise SystemExit(main())
