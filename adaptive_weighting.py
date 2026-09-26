"""adaptive_weighting.py — Ponderazione dinamica per campionato (CLV 30 giorni).

Direttiva del proprietario (26/09/2026): il rischio si ottimizza col **Closing
Line Value**. Il sistema traccia il CLV per singolo campionato; se un torneo
registra **sistematicamente CLV negativo** negli ultimi 30 giorni, il
moltiplicatore di Kelly per quella lega viene SCALATO (o la lega finisce in una
whitelist restrittiva) — mai alzato.

Principi (coerenti col resto del progetto):
- **Il moltiplicatore va solo VERSO IL BASSO** (`<= 1.0`): non e' un
  acceleratore, e' una protezione. Nessuna lega viene promossa automaticamente.
- **Default OFF** (`ADAPTIVE_WEIGHTING=0`): finche' l'env non e' accesa il
  modulo e' telemetria pura e `league_multiplier()` ritorna sempre 1.0. Il
  congelamento di strategia del 22/09 resta rispettato: si misura prima.
- **Fail-open**: qualunque errore di lettura (DB assente/corrotto, join che non
  torna) vale come "nessun dato" -> moltiplicatore 1.0. Non si riduce lo stake
  per un errore di telemetria.
- **Sola lettura**: qui si legge e si misura, non si scrive mai sul ledger.
- **Env lette a OGNI chiamata** (non all'import): l'interruttore e le soglie
  cambiano senza redeploy — e i test possono accenderle senza ricaricare il
  modulo. E' la stessa scelta fatta per le env di risk management.

Dati: `clv_history` (la fonte del CLV, popolata da `fixture_engine`) unita a
`predictions.league` (colonna dal 22/09) e, in fallback, a `matches.league`.
Il campione minimo (`ADAPTIVE_WEIGHTING_MIN_SAMPLES`, default 8) evita di
reagire al rumore di 2-3 chiusure.

Env:
  ADAPTIVE_WEIGHTING                     (0|1, default 0 = telemetria)
  ADAPTIVE_WEIGHTING_WINDOW_DAYS         (default 30)
  ADAPTIVE_WEIGHTING_MIN_SAMPLES         (default 8)
  ADAPTIVE_WEIGHTING_FLOOR               (default 0.5: il moltiplicatore minimo)
  ADAPTIVE_WEIGHTING_CLV_FLOOR_THRESHOLD (default -0.02: a questo CLV si tocca il floor)
  ADAPTIVE_WEIGHTING_RESTRICT_THRESHOLD  (default -0.04: lega in whitelist stretta)
  ADAPTIVE_WEIGHTING_TTL                 (default 300s, cache in-process)

CLI:
  venv/bin/python adaptive_weighting.py [--json] [--window-days N]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger("adaptive_weighting")


def _flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def _num(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return float(default)


# --- Accessori a runtime: nessun default duplicato, env riletta a ogni uso ---

def enabled() -> bool:
    """Interruttore della ponderazione (default OFF: sola telemetria)."""
    return _flag("ADAPTIVE_WEIGHTING", "0")


def window_days() -> float:
    return _num("ADAPTIVE_WEIGHTING_WINDOW_DAYS", 30)


def min_samples() -> int:
    return max(1, int(_num("ADAPTIVE_WEIGHTING_MIN_SAMPLES", 8)))


def floor() -> float:
    return max(0.0, min(1.0, _num("ADAPTIVE_WEIGHTING_FLOOR", 0.5)))


def clv_floor_threshold() -> float:
    return _num("ADAPTIVE_WEIGHTING_CLV_FLOOR_THRESHOLD", -0.02)


def restrict_threshold() -> float:
    return _num("ADAPTIVE_WEIGHTING_RESTRICT_THRESHOLD", -0.04)


def ttl_seconds() -> float:
    return _num("ADAPTIVE_WEIGHTING_TTL", 300)


_CACHE: Dict[str, Any] = {"ts": 0.0, "table": {}}


def reset_cache() -> None:
    """Svuota la cache in-process (usato dai test e dai job)."""
    _CACHE["ts"] = 0.0
    _CACHE["table"] = {}


def _canonical(league: Optional[str]) -> str:
    """Normalizza il nome lega (stessa fonte del gate: nessun doppio standard)."""
    try:
        from value_filter import canonical_league
        return canonical_league((league or "").strip()) or ""
    except Exception:
        return (league or "").strip()


def _cutoff(now: Optional[datetime] = None,
            days: Optional[float] = None) -> str:
    now = now or datetime.now(timezone.utc)
    d = window_days() if days is None else float(days)
    return (now - timedelta(days=d)).isoformat()


def _clv_by_league(days: Optional[float] = None,
                   now: Optional[datetime] = None) -> Dict[str, Dict[str, Any]]:
    """CLV medio (e n) per campionato nella finestra. Sola lettura, fail-safe.

    Il CLV vig-free non e' ricostruibile dal ledger (serve l'overround della
    closing), quindi si usa il CLV grezzo `signal/closing - 1`: e' la stessa
    metrica grezza che il report mostra come "raw" e basta a decidere se una
    lega batte o no la chiusura. Sotto il campione minimo non si conclude
    nulla (n insufficiente).
    """
    out: Dict[str, Dict[str, Any]] = {}
    try:
        import tracker
        conn = tracker._get_conn()
        c = conn.cursor()
        # datetime(...) avvolge SEMPRE le date: il ledger usa ISO con la 'T',
        # SQLite produce lo spazio, e il confronto fra stringhe mentirebbe
        # (lezione permanente del 17/09/2026).
        rows = c.execute(
            '''SELECT COALESCE(NULLIF(TRIM(p.league), ''),
                              NULLIF(TRIM(m.league), '')) AS league,
                      c.signal_quota, c.closing_quota
                 FROM clv_history c
                 LEFT JOIN predictions p
                        ON p.match_id = c.match_id AND p.esito = c.esito
                 LEFT JOIN matches m ON m.id = c.match_id
                WHERE datetime(c.updated_at) >= datetime(?)''',
            (_cutoff(now, days),)).fetchall()
        conn.close()
    except Exception as e:
        logger.debug("adaptive_weighting: lettura CLV fallita (%s)", e)
        return out
    for league, sig, clos in rows:
        name = _canonical(league)
        if not name:
            continue
        try:
            sig = float(sig)
            clos = float(clos)
        except (TypeError, ValueError):
            continue
        if sig <= 1.0 or clos <= 1.0:
            continue
        bucket = out.setdefault(name, {"n": 0, "clv_sum": 0.0})
        bucket["n"] += 1
        bucket["clv_sum"] += (sig / clos) - 1.0
    for b in out.values():
        b["avg_clv"] = round(b["clv_sum"] / b["n"], 6) if b["n"] else 0.0
        b.pop("clv_sum", None)
    return out


def _multiplier_from_clv(n: int, avg_clv: float) -> float:
    """Moltiplicatore SOLO verso il basso, lineare dal CLV alla soglia floor.

    avg_clv >= 0 -> 1.0;  avg_clv <= CLV_FLOOR_THRESHOLD -> FLOOR.
    """
    if n < min_samples() or avg_clv >= 0:
        return 1.0
    th = clv_floor_threshold()
    th = th if th < 0 else -1e-9
    frac = min(1.0, abs(avg_clv) / abs(th))
    mult = 1.0 - frac * (1.0 - floor())
    return max(floor(), min(1.0, round(mult, 4)))


def table(days: Optional[float] = None,
          now: Optional[datetime] = None, *, use_cache: bool = True
          ) -> Dict[str, Dict[str, Any]]:
    """Tabella per lega: n, CLV medio, moltiplicatore e stato whitelist.

    `use_cache` (default True) evita di rileggere il DB a ogni pick: il giro
    gira ogni 60s e piu' candidati condividono la stessa lega.
    """
    if use_cache and _CACHE["table"] and \
            (time.time() - _CACHE["ts"]) < ttl_seconds():
        return _CACHE["table"]
    data = _clv_by_league(days=days, now=now)
    out: Dict[str, Dict[str, Any]] = {}
    for league, b in data.items():
        n = int(b.get("n") or 0)
        avg = float(b.get("avg_clv") or 0.0)
        out[league] = {
            "n": n,
            "avg_clv": round(avg * 100, 2),          # in punti percentuali
            "enough_sample": n >= min_samples(),
            "multiplier": _multiplier_from_clv(n, avg),
            "restricted": bool(n >= min_samples() and avg <= restrict_threshold()),
        }
    _CACHE["table"] = out
    _CACHE["ts"] = time.time()
    return out


def league_multiplier(league: Optional[str]) -> float:
    """Moltiplicatore di Kelly per la lega (1.0 se OFF/dato insufficiente).

    Fail-open: senza dati o con l'env spenta ritorna 1.0. Non solleva mai.
    """
    if not enabled():
        return 1.0
    name = _canonical(league)
    if not name:
        return 1.0
    try:
        row = table().get(name)
        if not row or not row.get("enough_sample"):
            return 1.0
        return float(row.get("multiplier") or 1.0)
    except Exception as e:
        logger.debug("adaptive_weighting: moltiplicatore non calcolabile (%s)", e)
        return 1.0


def report(days: Optional[float] = None,
           now: Optional[datetime] = None) -> Dict[str, Any]:
    """Riepilogo per CLI/report: tabella + stato + leghe da attenzionare."""
    tab = table(days=days, now=now, use_cache=False)
    restrict = sorted(k for k, v in tab.items() if v.get("restricted"))
    return {
        "enabled": enabled(),
        "window_days": window_days() if days is None else float(days),
        "min_samples": min_samples(),
        "floor": floor(),
        "leagues": dict(sorted(tab.items())),
        "n_leagues": len(tab),
        "restricted": restrict,
    }


def format_report(res: Optional[Dict[str, Any]] = None) -> str:
    """Riga(s) Telegram-friendly sullo stato della ponderazione per lega."""
    res = res or report()
    state = "🟢 ON" if res["enabled"] else "⚪ OFF (telemetria)"
    lines = [f"⚖️ Ponderazione per campionato — {state}",
             f"Finestra {res['window_days']:.0f}g · campione min "
             f"{res['min_samples']} · floor {res['floor']:.2f}"]
    if not res["leagues"]:
        lines.append("Nessun dato CLV per lega nella finestra.")
        return "\n".join(lines)
    for league, row in res["leagues"].items():
        mark = "🚫" if row["restricted"] else ("🔻" if row["multiplier"] < 1 else "✅")
        lines.append(
            f"  {mark} {league}: n={row['n']} CLV {row['avg_clv']:+.2f}% "
            f"→ x{row['multiplier']:.2f}")
    if res["restricted"]:
        lines.append("Whitelist restrittiva: " + ", ".join(res["restricted"]))
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Ponderazione dinamica per campionato (CLV, default OFF)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--window-days", type=float, default=None)
    args = ap.parse_args(argv)
    res = report(days=args.window_days)
    if args.json:
        print(json.dumps(res, indent=2, ensure_ascii=False, default=str))
    else:
        print(format_report(res))
    return 0


if __name__ == "__main__":                                    # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
