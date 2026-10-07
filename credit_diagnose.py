"""credit_diagnose.py — Dove vanno i crediti di the-odds-api (03/10/2026).

PERCHE' ESISTE. Il 01/10/2026 la telemetria diceva `remaining 388`,
`estimated_daily_consumption 44,8/giorno` contro 13,9 sostenibili: esaurimento
previsto ~11-12/10, PRIMA del reset (01/11). `odds_api.credit_budget_status`
dice QUANTO si consuma e QUANDO finira' — non DA DOVE arriva il costo. Questo
strumento attribuisce il consumo per SORGENTE leggendo la telemetria delle
chiamate che `odds_api` scrive a ogni richiesta HTTP:

  - **rotation**   — rotazione di ricerca hai/h2h (1 credito/chiamata);
  - **oracle**     — oracolo a linea OU/AH `h2h,totals,spreads` (3 crediti);
  - **settlement** — risultati `/scores` (1 credito).

Aggiunge l'INVENTARIO delle cache (quante leghe sono state interrogate per
categoria: e' il costo TEORICO del piano, indipendente dalla telemetria) e la
proiezione di esaurimento. Diagnostica pura: **sola lettura**, zero rete, zero
crediti, zero ordini. Ogni errore e' dichiarato, mai propagato.

CLI: venv/bin/python credit_diagnose.py [--days N] [--json]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import DATA_DIR
from telemetry_logs import iter_events

logger = logging.getLogger("credit_diagnose")

#: Sorgenti note della telemetria (le altre sono riportate con la loro chiave).
SOURCES = ("rotation", "oracle", "settlement")
#: Prefissi delle cache per categoria (`_classify_cache`).
CACHE_PREFIXES = (("toao_", "oracle"), ("toa_scores_", "settlement"),
                  ("toa_", "rotation"))


def calls_log_path() -> Path:
    """Path della telemetria: stessa env e stesso DEFAULT di `odds_api`.

    Il default non viene ricopiato: si delega a `odds_api` (import pigro) per
    non avere due path di default che possono divergere.
    """
    env = os.getenv("CREDIT_CALLS_LOG")
    if env and str(env).strip():
        return Path(env)
    try:
        import odds_api as oa
        return Path(oa.DEFAULT_CREDIT_CALLS_LOG)
    except Exception:                                            # pragma: no cover
        return Path(DATA_DIR) / "execution" / "credit_calls.jsonl"


def cache_dir() -> Path:
    return Path(DATA_DIR)


def _classify_cache(name: str) -> str:
    for prefix, cat in CACHE_PREFIXES:
        if name.startswith(prefix):
            return cat
    return "other"


def inventory(*, directory: Optional[Path] = None,
              now: Optional[float] = None) -> Dict[str, Any]:
    """Inventario delle cache `toa_*` per categoria (costo teorico del piano).

    Ritorna per categoria: numero di leghe interrogate, eta' minima/media/
    massima in ore (dal `ts` della cache). Un errore di lettura non propaga.
    """
    directory = Path(directory) if directory is not None else cache_dir()
    now_ts = time.time() if now is None else float(now)
    out: Dict[str, Any] = {c: {"files": 0, "min_age_h": None, "max_age_h": None,
                               "avg_age_h": None} for c in SOURCES}
    out["other"] = {"files": 0, "min_age_h": None, "max_age_h": None,
                    "avg_age_h": None}
    try:
        # ⚠️ `toa*.json`, NON `toa_*.json`: la cache oracolo e' `toao_*.json` e
        # non inizia con `toa_` (dopo 'toa' c'e' una 'o'): con il glob stretto
        # l'inventario dell'oracolo restava a zero (detto dal test).
        files = list(directory.glob("toa*.json"))
    except Exception as e:                                       # pragma: no cover
        out["error"] = str(e)
        return out
    ages: Dict[str, List[float]] = defaultdict(list)
    for f in files:
        cat = _classify_cache(f.name)
        out[cat]["files"] += 1
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            ts = data.get("remaining_ts", data.get("ts"))
            if isinstance(ts, (int, float)):
                ages[cat].append((now_ts - float(ts)) / 3600.0)
        except Exception:
            continue
    for cat, vals in ages.items():
        if vals:
            out[cat]["min_age_h"] = round(min(vals), 1)
            out[cat]["max_age_h"] = round(max(vals), 1)
            out[cat]["avg_age_h"] = round(sum(vals) / len(vals), 1)
    return out


def breakdown(*, days: float = 7.0, path: Optional[Path] = None) -> Dict[str, Any]:
    """Chiamate e crediti per SORGENTE nella finestra (dalla telemetria).

    Ritorna `by_source` {sorgente: {calls, credits, errors, last_ts,
    by_market}} + totali. Fail-safe: telemetria assente -> zeri dichiarati.
    """
    path = Path(path) if path is not None else calls_log_path()
    by_source: Dict[str, Dict[str, Any]] = {}
    by_market: Dict[str, int] = defaultdict(int)
    total_calls = 0
    total_credits = 0.0
    errors = 0
    last_ts: Optional[float] = None
    try:
        events: List[dict] = list(iter_events(path, days=days))
    except Exception as e:                                       # pragma: no cover
        events = []
        errors += 1
        logger.warning("credit_diagnose: lettura telemetria fallita (%s)", e)
    for evt in events:
        src = str(evt.get("source") or "unknown")
        row = by_source.setdefault(
            src, {"calls": 0, "credits": 0.0, "errors": 0, "last_ts": None,
                  "by_market": defaultdict(int)})
        row["calls"] += 1
        try:
            row["credits"] += float(evt.get("credits") or 0.0)
        except (TypeError, ValueError):
            pass
        if evt.get("error"):
            row["errors"] += 1
        try:
            if int(evt.get("status") or 200) >= 400:
                row["errors"] += 1
        except (TypeError, ValueError):
            pass
        mkt = str(evt.get("markets") or "?")
        row["by_market"][mkt] += 1
        by_market[mkt] += 1
        ts = evt.get("ts_epoch")
        if isinstance(ts, (int, float)):
            row["last_ts"] = max(row["last_ts"] or 0.0, float(ts))
            last_ts = max(last_ts or 0.0, float(ts))
        total_calls += 1
        total_credits += float(evt.get("credits") or 0.0)
    # `defaultdict` non e' JSON-serializzabile: si appiattisce.
    for row in by_source.values():
        row["by_market"] = dict(row["by_market"])
    return {"days": days, "path": str(path), "total_calls": total_calls,
            "total_credits": round(total_credits, 1), "errors": errors,
            "last_ts": last_ts, "by_source": by_source,
            "by_market": dict(by_market)}


def oracle_budget(*, days: float = 7.0) -> Dict[str, Any]:
    """Stato del budget dell'oracolo a linea: tetto, leghe, RIFIUTI per causa.

    PERCHE' E' QUI (06/10/2026). L'esaurimento del budget dell'oracolo era
    invisibile: nel report dei crediti compariva solo la generica sorgente
    `oracle` (dalla telemetria delle chiamate), mai il TETTO saturato. Il
    06/10 la diagnosi diceva "oracle 2 chiamate" mentre in produzione le due
    unita' erano finite sulla stessa lega e le altre leghe Core erano rimaste
    senza prezzo: un numero senza il tetto non racconta quella storia.

    Dichiara: usato/tetto, leghe distinte pagate, tetto per lega, crediti
    stimati del giorno e i RIFIUTI per causa classe (da `oracle_skips`, che li
    registra dal 06/10 con la causa nella chiave di dedup). Fail-safe: ogni
    sonda che non risponde diventa un campo d'errore, mai un'eccezione.
    """
    out: Dict[str, Any] = {}
    try:
        import odds_api as oa
        out["budget"] = oa.oracle_budget_status()
    except Exception as e:                                       # pragma: no cover
        out["budget"] = {"error": str(e)}
    try:
        import oracle_skips
        s = oracle_skips.summary(days=days)
        out["refusals_by_class"] = dict(s.get("by_refusal_class") or {})
        out["refusals"] = dict(s.get("by_refusal") or {})
        out["skips_in_window"] = int(s.get("orders_blocked") or 0)
    except Exception as e:                                       # pragma: no cover
        out["refusals_by_class"] = {}
        out["refusals_error"] = str(e)
    return out


def diagnose(*, days: float = 7.0, path: Optional[Path] = None) -> Dict[str, Any]:
    """Diagnosi completa: budget + attribuzione per sorgente + inventario.

    Fail-safe: qualunque errore delle parti diventa un campo dichiarato, cosi'
    un report parziale e' leggibile invece di far fallire tutto.
    """
    out: Dict[str, Any] = {"days": days}
    try:
        import odds_api as oa
        out["budget"] = oa.credit_budget_status()
    except Exception as e:                                       # pragma: no cover
        out["budget"] = {"error": str(e)}
    out["calls"] = breakdown(days=days, path=path)
    out["inventory"] = inventory()
    out["oracle"] = oracle_budget(days=days)
    # Costo GIORNALIERO attribuito (crediti/giorno) per sorgente: la metrica
    # azionabile per decidere quale tagliare.
    per_day: Dict[str, float] = {}
    dd = max(0.5, float(days))
    for src, row in (out["calls"].get("by_source") or {}).items():
        per_day[src] = round(float(row.get("credits") or 0.0) / dd, 1)
    out["credits_per_day_by_source"] = per_day
    out["telemetry_empty"] = (out["calls"].get("total_calls") == 0)
    return out


def format_report(d: Dict[str, Any]) -> str:
    lines = ["💳 DIAGNOSI CREDITI the-odds-api"]
    b = d.get("budget") or {}
    if b.get("error"):
        lines.append(f"  ⚠️ budget non leggibile: {b['error']}")
    else:
        lines.append(f"  residui: {b.get('remaining')} | reset fra "
                     f"{b.get('days_to_reset')} gg | sostenibile "
                     f"{b.get('sustainable_per_day')}/giorno")
        lines.append(f"  ritmo misurato: {b.get('rate_per_day')}/giorno "
                     f"(finestra {b.get('window_hours')}h) | esaurimento "
                     f"{b.get('exhaustion_date')}"
                     + ("  ⚠️ PRIMA DEL RESET" if b.get("alert") else ""))
    calls = d.get("calls") or {}
    if d.get("telemetry_empty"):
        lines.append("  📊 attribuzione: NESSUNA telemetria ancora (le chiamate "
                     "da ora vengono registrate)")
    else:
        lines.append(f"  📊 attribuzione (finestra {d.get('days')} gg, "
                     f"{calls.get('total_calls')} chiamate, "
                     f"{calls.get('total_credits')} crediti):")
        per_day = d.get("credits_per_day_by_source") or {}
        for src, row in sorted((calls.get("by_source") or {}).items(),
                               key=lambda kv: -(kv[1].get("credits") or 0)):
            lines.append(f"    • {src:<11} {row['calls']:>5} chiamate | "
                         f"{row['credits']:>6.1f} cr | "
                         f"{per_day.get(src, 0.0):>5.1f} cr/giorno"
                         + (f" | errori {row['errors']}" if row.get("errors")
                            else ""))
    # Tetto dell'oracolo a linea: la riga che dice se il gate resta senza
    # prezzo per BUDGET (e non per assenza di pick).
    orc = d.get("oracle") or {}
    ob = orc.get("budget") or {}
    if ob.get("error"):
        lines.append(f"  ⚠️ budget oracolo non leggibile: {ob['error']}")
    else:
        lines.append(f"  🎯 oracolo a linea: {ob.get('used')}/{ob.get('cap')} "
                     f"chiamate usate oggi ({ob.get('left')} residue) | "
                     f"{ob.get('leagues')} leghe distinte | tetto per lega "
                     f"{ob.get('max_calls_per_league')} | "
                     f"~{ob.get('credits_used_today')} crediti"
                     + ("  ⚠️ TETTO ESAURITO" if ob.get("exhausted") else ""))
        bl = ob.get("by_league") or {}
        if bl:
            lines.append("    per lega: " + ", ".join(
                f"{k.replace('soccer_', '')} {v}" for k, v in bl.items()))
        ref = orc.get("refusals_by_class") or {}
        if ref:
            lines.append("    rifiuti del fetch (finestra "
                         f"{d.get('days')} gg): " + ", ".join(
                             f"{k} {v}" for k, v in list(ref.items())[:4]))
    inv = d.get("inventory") or {}
    lines.append("  🗃️ cache per categoria:")
    for cat in SOURCES + ("other",):
        row = inv.get(cat) or {}
        if row.get("files"):
            lines.append(f"    • {cat:<11} {row['files']:>3} file | eta' media "
                         f"{row.get('avg_age_h')}h (max {row.get('max_age_h')}h)")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:                # pragma: no cover
    ap = argparse.ArgumentParser(description="Dove vanno i crediti the-odds-api")
    ap.add_argument("--days", type=float, default=7.0)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    d = diagnose(days=args.days)
    print(json.dumps(d, indent=2, ensure_ascii=False, default=str)
          if args.json else format_report(d))
    return 0


if __name__ == "__main__":                                        # pragma: no cover
    raise SystemExit(main())
