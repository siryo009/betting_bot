"""lane_weighting.py — auto-demozione di una CORSIA dal CLV misurato.

Direttiva del proprietario (23/10/2026): la riduzione dello stake per corsia non
deve dipendere da una decisione presa a mano ogni volta. Se una corsia (1X2, OU,
AH, tennis, esports) chiude sistematicamente **sotto** il prezzo di chiusura, il
sistema lo scopre da solo e si ridimensiona.

Perche' il CLV e non il P/L: il P/L di una corsia e' la differenza fra due
numeri rumorosi (a quota media 1,65 servono ~13.100 chiusure per dimostrare un
edge del 2%), mentre il CLV misura direttamente se il PREZZO comprato era
migliore di quello di chiusura — l'unico segnale che si valida in decine di
osservazioni invece che in anni (vedi `significance.detectable_edge`).

Regole, tutte nella direzione prudente:

- **Due soli esiti possibili: 1.0 oppure una RIDUZIONE.** Non esiste un percorso
  che alzi lo stake (una corsia in utile resta a 1.0: la promozione non e'
  automatica).
- **Criterio statistico, non una soglia a occhio:** si interviene solo con
  almeno `LANE_WEIGHTING_MIN_SAMPLES` (30, la stessa soglia del resto del
  progetto) e si guarda l'**intervallo di confidenza al 95%** della media CLV
  (t-test di `significance.roi_test`, nessuna formula ricopiata):
    * IC interamente sotto zero -> la corsia **si ferma** (`floor`, default 0.0);
    * media negativa ma IC che contiene lo zero -> **dimezzata**
      (`LANE_WEIGHTING_SUSPECT_MULT`, default 0.5): sospetto, non prova;
    * tutto il resto -> 1.0.
- **Il dato e' quello dei TRADE** (`bets`, denaro vero) e non dei segnali: una
  corsia non si giudica su cio' che non e' stato comprato. `--signals` esiste per
  la misura, non per decidere.
- **Fail-open dichiarato**: DB assente/corrotto, `scipy` non disponibile, righe
  ostili -> 1.0. Una telemetria rotta non deve cambiare lo stake.
- **Sola lettura**: nessuna scrittura sul ledger, nessun ordine, nessuna rete.
- **Env lette a ogni chiamata** (non all'import): spegnere l'automatismo non
  richiede un redeploy.

Dove NON si applica: il moltiplicatore entra DOPO la dimensione finale
(Kelly/torneo/rush) e PRIMA dei cap di portafoglio — vedi `auto_bet._apply_clv_weighting`.

Env:
  LANE_WEIGHTING                (0|1, default 1 = attiva)
  LANE_WEIGHTING_WINDOW_DAYS    (default 60)
  LANE_WEIGHTING_MIN_SAMPLES    (default 30)
  LANE_WEIGHTING_FLOOR          (default 0.0: corsia misurata negativa -> stop)
  LANE_WEIGHTING_SUSPECT_MULT   (default 0.5: media negativa non conclusiva)
  LANE_WEIGHTING_ALPHA          (default 0.05)
  LANE_WEIGHTING_TTL            (default 300s, cache in-process)

CLI:
  venv/bin/python lane_weighting.py [--json] [--days N] [--signals]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger("lane_weighting")

#: Mappa DETERMINISTICA mercato del ledger -> corsia. Le chiavi sono i valori che
#: `predictions.mercato` contiene davvero (`1X2`, `OU`, `AH`, `TENNIS`, `ML`).
#: Un mercato sconosciuto NON viene raggruppato in un secchio "altro": non e'
#: giudicabile, e inventare un bucket significherebbe demotare cose che nessuno
#: ha misurato.
LANE_MAP: Dict[str, str] = {
    "1X2": "1X2",
    "OU": "OU",
    "OVER/UNDER": "OU",
    "TOTALS": "OU",
    "AH": "AH",
    "SPREADS": "AH",
    "TENNIS": "TENNIS",
    "ML": "ML",
    "ESPORTS": "ML",
}

STATUS_OK = "ok"
STATUS_SUSPECT = "suspect"
STATUS_DEMOTED = "demoted"
STATUS_INSUFFICIENT = "insufficient"
STATUS_UNAVAILABLE = "unavailable"


def _flag(name: str, default: str) -> bool:
    return (os.getenv(name, default) or "").strip().lower() in ("1", "true", "yes", "on")


def _num(name: str, default: float, *, minimum: Optional[float] = None) -> float:
    """Numero da env con fallback DICHIARATO (mai un default silenzioso)."""
    try:
        val = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        logger.warning("lane_weighting: %s non numerico -> default %s", name, default)
        val = float(default)
    if minimum is not None and val < minimum:
        logger.warning("lane_weighting: %s=%s sotto il minimo %s -> %s",
                       name, val, minimum, minimum)
        val = float(minimum)
    return val


def enabled() -> bool:
    """Interruttore dell'auto-demozione (default ON: la direttiva la vuole attiva)."""
    return _flag("LANE_WEIGHTING", "1")


def window_days() -> float:
    return _num("LANE_WEIGHTING_WINDOW_DAYS", 60.0, minimum=1.0)


def min_samples() -> int:
    return int(_num("LANE_WEIGHTING_MIN_SAMPLES", 30, minimum=2))


def floor() -> float:
    return max(0.0, min(1.0, _num("LANE_WEIGHTING_FLOOR", 0.0)))


def suspect_multiplier() -> float:
    return max(0.0, min(1.0, _num("LANE_WEIGHTING_SUSPECT_MULT", 0.5)))


def alpha() -> float:
    return max(0.0001, min(0.5, _num("LANE_WEIGHTING_ALPHA", 0.05)))


def ttl_seconds() -> float:
    return _num("LANE_WEIGHTING_TTL", 300.0, minimum=0.0)


def lane_of(mercato: Any) -> str:
    """Corsia canonica di un mercato del ledger ('' = non giudicabile)."""
    try:
        key = str(mercato or "").strip().upper()
    except Exception:
        return ""
    return LANE_MAP.get(key, "")


_CACHE: Dict[str, Any] = {"ts": 0.0, "table": {}}


def reset_cache() -> None:
    """Svuota la cache in-process (test e job)."""
    _CACHE["ts"] = 0.0
    _CACHE["table"] = {}


def _samples(*, days: Optional[float] = None, include_signals: bool = False,
             now: Optional[datetime] = None) -> Dict[str, Any]:
    """Campioni CLV dal tracker (fonte unica: nessun SQL duplicato qui)."""
    try:
        import clv_tracker
        return clv_tracker.collect(days=days, include_signals=include_signals,
                                   now=now)
    except Exception as exc:
        logger.debug("lane_weighting: campioni non leggibili (%s)", exc)
        return {"samples": [], "error": str(exc)}


def _values_by_lane(samples: Sequence[Dict[str, Any]]
                    ) -> tuple[Dict[str, List[float]], Dict[str, int]]:
    """CLV per corsia. Ritorna anche i campioni non riconosciuti (contati)."""
    out: Dict[str, List[float]] = {}
    unmapped: Dict[str, int] = {}
    for s in samples or []:
        if not isinstance(s, dict):
            continue
        lane = lane_of(s.get("mercato"))
        try:
            clv = float(s.get("clv"))
        except (TypeError, ValueError):
            continue
        if not lane:
            key = str(s.get("mercato") or "?")
            unmapped[key] = unmapped.get(key, 0) + 1
            continue
        out.setdefault(lane, []).append(clv)
    return out, unmapped


def _verdict(values: List[float]) -> Dict[str, Any]:
    """Verdetto di UNA corsia: moltiplicatore solo verso il basso + motivo."""
    n = len(values)
    row: Dict[str, Any] = {
        "n": n, "avg_clv": None, "ci_low": None, "ci_high": None,
        "p_value": None, "multiplier": 1.0, "status": STATUS_INSUFFICIENT,
        "reliable": False, "note": "",
    }
    if n:
        row["avg_clv"] = round(sum(values) / n * 100, 3)     # punti percentuali
    if n < min_samples():
        row["note"] = f"campione {n} < {min_samples()}: nessuna conclusione"
        return row
    try:
        import significance
        if not significance.available():
            row["status"] = STATUS_UNAVAILABLE
            row["note"] = "scipy non disponibile: nessuna demozione automatica"
            return row
        test = significance.roi_test(values, alpha()) or {}
    except Exception as exc:
        row["status"] = STATUS_UNAVAILABLE
        row["note"] = f"test statistico non eseguibile ({exc})"
        return row
    for key in ("mean", "ci_low", "ci_high", "p_value"):
        val = test.get(key)
        if key == "mean":
            continue
        if val is not None:
            row[key] = round(float(val) * 100, 3)
    row["reliable"] = True
    mean = test.get("mean")
    mean = float(mean) if mean is not None else (sum(values) / n)
    ci_high = test.get("ci_high")
    ci_high = float(ci_high) if ci_high is not None else 0.0
    if ci_high < 0.0:
        row["status"] = STATUS_DEMOTED
        row["multiplier"] = floor()
        row["note"] = "IC95 interamente sotto zero: corsia fermata"
    elif mean < 0.0:
        row["status"] = STATUS_SUSPECT
        row["multiplier"] = suspect_multiplier()
        row["note"] = "media negativa non conclusiva: stake dimezzato"
    else:
        row["status"] = STATUS_OK
        row["note"] = "CLV non negativo"
    return row


def table(*, days: Optional[float] = None, include_signals: bool = False,
          now: Optional[datetime] = None, use_cache: bool = True
          ) -> Dict[str, Dict[str, Any]]:
    """Tabella per corsia: n, CLV medio, IC95, moltiplicatore, stato."""
    if use_cache and _CACHE["table"] and \
            (time.time() - _CACHE["ts"]) < ttl_seconds():
        return _CACHE["table"]
    bundle = _samples(days=window_days() if days is None else days,
                      include_signals=include_signals, now=now)
    values, _ = _values_by_lane(bundle.get("samples") or [])
    out = {lane: _verdict(vals) for lane, vals in sorted(values.items())}
    _CACHE["table"] = out
    _CACHE["ts"] = time.time()
    return out


def lane_multiplier(mercato: Any) -> float:
    """Moltiplicatore della corsia: 1.0 salvo demozione misurata. Mai solleva."""
    if not enabled():
        return 1.0
    lane = lane_of(mercato)
    if not lane:
        return 1.0
    try:
        row = table().get(lane) or {}
        mult = float(row.get("multiplier", 1.0))
    except Exception as exc:
        logger.debug("lane_weighting: moltiplicatore non calcolabile (%s)", exc)
        return 1.0
    if mult >= 1.0:
        return 1.0
    if row.get("status") == STATUS_DEMOTED:
        logger.warning("lane_weighting: corsia '%s' FERMATA — CLV medio %s%% su "
                       "n=%s (IC95 %s..%s)", lane, row.get("avg_clv"),
                       row.get("n"), row.get("ci_low"), row.get("ci_high"))
    return max(0.0, min(1.0, round(mult, 4)))


def report(*, days: Optional[float] = None, include_signals: bool = False,
           now: Optional[datetime] = None) -> Dict[str, Any]:
    """Riepilogo per CLI/report."""
    bundle = _samples(days=window_days() if days is None else days,
                      include_signals=include_signals, now=now)
    values, unmapped = _values_by_lane(bundle.get("samples") or [])
    lanes = {lane: _verdict(vals) for lane, vals in sorted(values.items())}
    return {
        "enabled": enabled(),
        "window_days": window_days() if days is None else float(days),
        "min_samples": min_samples(),
        "floor": floor(),
        "suspect_multiplier": suspect_multiplier(),
        "alpha": alpha(),
        "include_signals": bool(include_signals),
        "lanes": lanes,
        "demoted": sorted(k for k, v in lanes.items()
                          if v["status"] == STATUS_DEMOTED),
        "suspect": sorted(k for k, v in lanes.items()
                          if v["status"] == STATUS_SUSPECT),
        "unmapped": unmapped,
        "error": bundle.get("error"),
        "note": ("il verdetto e' per CORSIA sulla chiusura media dei trade; "
                 "sotto il campione minimo non si conclude nulla"),
    }


def format_report(res: Optional[Dict[str, Any]] = None) -> str:
    """Riga(s) Telegram-friendly sullo stato delle corsie."""
    res = res or report()
    state = "🟢 ON" if res["enabled"] else "⚪ OFF (solo telemetria)"
    lines = [f"🎯 Demozione per corsia (CLV) — {state}",
             f"Finestra {res['window_days']:.0f}g · campione min "
             f"{res['min_samples']} · floor {res['floor']:.2f}"]
    if res.get("error"):
        lines.append(f"⚠️ lettura non disponibile: {res['error']}")
    if not res["lanes"]:
        lines.append("Nessun campione CLV per corsia.")
        return "\n".join(lines)
    mark = {STATUS_OK: "✅", STATUS_SUSPECT: "🔻", STATUS_DEMOTED: "🚫",
            STATUS_INSUFFICIENT: "⚪", STATUS_UNAVAILABLE: "⚠️"}
    for lane, row in res["lanes"].items():
        avg = row.get("avg_clv")
        avg_txt = f"{avg:+.2f}%" if avg is not None else "n/d"
        lines.append(f"  {mark.get(row['status'], '·')} {lane}: n={row['n']} "
                     f"CLV {avg_txt} → x{row['multiplier']:.2f} "
                     f"({row['note']})")
    if res["unmapped"]:
        lines.append("Non giudicabili (mercato non mappato): "
                     + ", ".join(f"{k}×{v}" for k, v in res["unmapped"].items()))
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:                  # pragma: no cover
    ap = argparse.ArgumentParser(
        description="Auto-demozione delle corsie dal CLV misurato (sola lettura)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--days", type=float, default=None)
    ap.add_argument("--signals", action="store_true",
                    help="misura anche i segnali (non decide: il denaro e' nei trade)")
    args = ap.parse_args(argv)
    res = report(days=args.days, include_signals=args.signals)
    print(json.dumps(res, indent=2, ensure_ascii=False, default=str)
          if args.json else format_report(res))
    return 0


if __name__ == "__main__":                                         # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
