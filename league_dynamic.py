"""league_dynamic.py — Gestione DINAMICA del gate di lega (misurata, non statica).

PERCHE' ESISTE
Il gate di lega e' una tabella STATICA (`value_filter.STRATEGY_LEAGUES` +
`PROBATION_LEAGUES`), decisa il 12/09 e congelata dal 22/09. Fino al 22/09 la
strategia per campionato era **non misurabile** (75,9% delle righe senza lega:
la riga `matches` viene potata e la colonna `predictions.league` non esisteva),
quindi non c'era modo di sapere se una lega stesse davvero battendo il mercato.
Da quando `predictions.league` esiste, quel dato e' nel ledger — e non deve
restare sepolto in una query a mano: sia il 22/09 sia il 25/09 due letture
sbagliate erano nate proprio da query manuali senza filtro d'era (il `-21,64%`
del 1X2 era per 74/85 righe una strategia RITIRATA; il `+21,21%` dell'OU era
portato per intero da 22 righe pre-19/09). Qui quella lettura diventa
ripetibile, filtrata e **dichiarata**.

COSA FA
Legge il ledger (SOLA LETTURA, mai una scrittura) e calcola per lega il
rendimento delle SOLE righe GIOCABILI (`value_filter.PLAYABLE_TIERS`) dell'ERA
corrente e della FASCIA QUOTA corrente, usando i filtri CONDIVISI
(`tracker.filter_predictions` via `get_predictions`: era + fascia). Ne ricava
uno STATO per lega: `promote` / `hold` / `demote` / `insufficient`.

AUTORITA' — default prudente, lo stesso schema di `adaptive_weighting`
 - `LEAGUE_DYNAMIC_ENABLED=0` (**DEFAULT**): **ADVISORY**. Calcola e riporta:
   non cambia NULLA nel percorso ordini.
 - `LEAGUE_DYNAMIC_ENABLED=1`: puo' solo **RESTRINGERE**. Una lega che il
   ledger misura in perdita oltre soglia viene esclusa dai candidati d'ordine.
   **NON promuove MAI** una lega non ammessa: aprire una lega a denaro reale
   resta una decisione dell'operatore (le tabelle statiche). La ragione e'
   statistica, non stilistica: `significance.py` misura che con 30 chiusure a
   quota media 1.65 l'edge minimo distinguibile e' **~41%**, quindi un
   campione di gate a taglia minima NON e' una prova di profitto — al massimo
   smentisce un disastro. Il modulo agisce solo nella direzione in cui
   l'errore costa meno (chiudere una lega in perdita, non aprirne una).

PRINCIPI
 - **Sola lettura**: nessuna scrittura sul ledger, nessun ordine, nessuna rete.
 - **Fail-open sulla lettura**: qualunque errore (DB assente/corrotto, join che
   non torna) vale "nessun dato" -> nessuna restrizione. Una telemetria rotta
   non deve cambiare il gate.
 - **Fail-closed sul campione**: sotto `LEAGUE_DYNAMIC_MIN_SAMPLES` chiusure
   non si conclude nulla (`insufficient`), nemmeno con un ROI molto negativo.
 - **Era e fascia DICHIARATE**: il report stampa sempre la finestra usata e
   quante righe sono state escluse, cosi' un report filtrato e uno completo non
   sono indistinguibili (lezione del 25/09).
 - **Env lette a OGNI chiamata** (non all'import): soglie e interruttore
   cambiano senza redeploy e i test possono accenderli senza ricaricare.
 - **Nessun import di produzione a livello modulo**: `tracker` e `value_filter`
   entrano pigri dentro le funzioni (tripwire nei test).

Env:
  LEAGUE_DYNAMIC_ENABLED      (0|1, default 0 = sola telemetria)
  LEAGUE_DYNAMIC_SINCE        (default "2026-09-11": inizio dell'era corrente,
                               cioe' fascia favoriti + FAVOURITES_ONLY)
  LEAGUE_DYNAMIC_MIN_SAMPLES  (default 30: la stessa soglia di "campione
                               affidabile" di significance.py / multi_market)
  LEAGUE_DYNAMIC_DEMOTE_ROI   (default -0.20: perdita catastrofica -> restrizione)
  LEAGUE_DYNAMIC_PROMOTE_ROI  (default +0.20: solo CANDIDATA, mai applicata)
  LEAGUE_DYNAMIC_TTL          (default 300s, cache in-process)

CLI:
  venv/bin/python league_dynamic.py [--json] [--since D] [--odds-min X]
                                    [--odds-max Y] [--all]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from datetime import datetime
from typing import Any, Dict, Optional

logger = logging.getLogger("league_dynamic")


def _flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def _num(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return float(default)


def _int(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return int(default)


# --- Accessori a runtime: nessun default duplicato, env riletta a ogni uso ---

def enabled() -> bool:
    """Interruttore del gate dinamico (default OFF: sola telemetria)."""
    return _flag("LEAGUE_DYNAMIC_ENABLED", "0")


def era_since() -> str:
    """Data di NASCITA del segnale da cui parte la misura (era corrente)."""
    return (os.getenv("LEAGUE_DYNAMIC_SINCE", "2026-09-11") or "").strip()


def min_samples() -> int:
    return max(1, _int("LEAGUE_DYNAMIC_MIN_SAMPLES", 30))


def demote_roi() -> float:
    return _num("LEAGUE_DYNAMIC_DEMOTE_ROI", -0.20)


def promote_roi() -> float:
    return _num("LEAGUE_DYNAMIC_PROMOTE_ROI", 0.20)


def ttl_seconds() -> float:
    return max(0.0, _num("LEAGUE_DYNAMIC_TTL", 300))


_CACHE: Dict[str, Any] = {"ts": 0.0, "table": {}}


def reset_cache() -> None:
    """Svuota la cache in-process (usato dai test e dai job)."""
    _CACHE["ts"] = 0.0
    _CACHE["table"] = {}


# --- Ponte verso la strategia: import pigro, mai una copia delle soglie ---

def _canonical(league: Optional[str]) -> str:
    """Nome canonico della lega (stessa fonte del gate: nessun doppio standard)."""
    try:
        from value_filter import canonical_league
        return canonical_league((league or "").strip()) or ""
    except Exception:
        return (league or "").strip()


def _allowed(league: Optional[str]) -> bool:
    """Il gate STATICO ammette la lega? (fail-open: un errore non restringe)."""
    try:
        from value_filter import league_allowed
        return bool(league_allowed(league or ""))
    except Exception:
        return True


def _tier(league: Optional[str]) -> str:
    try:
        from value_filter import league_tier
        return league_tier(league or "")
    except Exception:
        return ""


# NB: i valori letterali qui sotto sono la Rete di SICUREZZA di un import
# impossibile (value_filter dipende solo da market_calib), NON una seconda
# definizione della strategia: le soglie si leggono sempre da `value_filter`.
# Un fallback che sbagliasse in direzione prudente (nessuna riga giocabile ->
# nessuna restrizione) non puo' aprire nulla: e' fail-open, come il resto del
# modulo, e il gate STATICO resta comunque l'autorita'.

def _playable_tiers() -> set:
    try:
        from value_filter import PLAYABLE_TIERS
        return set(PLAYABLE_TIERS)
    except Exception:
        return {"value", "strong_value", "moderate"}


def _band() -> tuple:
    """Fascia quota corrente (`ODDS_MIN`/`ODDS_MAX`): mai copiata a mano."""
    try:
        import value_filter as vf
        return (float(vf.ODDS_MIN), float(vf.ODDS_MAX))
    except Exception:
        return (1.30, 1.80)


def _rows(*, since: Optional[str] = None,
          odds_min: Optional[float] = None,
          odds_max: Optional[float] = None,
          now: Optional[datetime] = None) -> tuple:
    """Righe CHIUSE e GIOCABILI dell'era/fascia richiesta. Sola lettura.

    Ritorna `(rows, meta)`: `meta` dichiara finestra, righe totali e righe
    escluse, cosi' il report puo' dire su COSA sta giudicando.
    """
    lo, hi = _band()
    win = since if since is not None else era_since()
    o_lo = lo if odds_min is None else float(odds_min)
    o_hi = hi if odds_max is None else float(odds_max)
    meta = {"since": win or "", "odds_min": o_lo, "odds_max": o_hi,
            "rows_unfiltered": 0, "rows_filtered_out": 0,
            "rows_total": 0, "rows_kept": 0, "rows_excluded": 0, "error": ""}
    try:
        import tracker
        # Due letture: la PRIMA senza filtri, per poter DICHIARARE quante righe
        # l'era/fascia ha escluso. Senza quel numero un report filtrato
        # sembrerebbe semplicemente "un ledger piu' piccolo", ed e' il modo in
        # cui il 22/09 (74/85 righe di una strategia ritirata) e il 25/09
        # (+21% OU tutto pre-19/09) sono stati letti come misure della
        # strategia corrente.
        rows_unfiltered = tracker.get_predictions(closed=True, limit=100000)
        rows = tracker.get_predictions(closed=True, limit=100000,
                                       created_since=win or None,
                                       odds_min=o_lo, odds_max=o_hi)
    except Exception as e:
        logger.debug("league_dynamic: lettura ledger fallita (%s)", e)
        meta["error"] = str(e)
        return ([], meta)
    meta["rows_unfiltered"] = len(rows_unfiltered)
    meta["rows_filtered_out"] = max(0, len(rows_unfiltered) - len(rows))
    meta["rows_total"] = len(rows)
    playable = _playable_tiers()
    kept = [r for r in rows if (r.get("status") or "") in playable]
    meta["rows_kept"] = len(kept)
    meta["rows_excluded"] = meta["rows_total"] - len(kept)
    return (kept, meta)


def _bucket() -> Dict[str, Any]:
    return {"n": 0, "won": 0, "lost": 0, "push": 0, "profit": 0.0,
            "odds_sum": 0.0, "ev_sum": 0.0}


def league_stats(*, since: Optional[str] = None,
                 odds_min: Optional[float] = None,
                 odds_max: Optional[float] = None,
                 now: Optional[datetime] = None) -> Dict[str, Any]:
    """Statistiche per lega sulle righe giocabili chiuse. Sola lettura, fail-safe.

    Ritorna `{"meta": ..., "leagues": {lega: {...}}, "senza_lega": {...}}`.
    Le righe SENZA lega non si buttano: sono dichiarate in `senza_lega`
    (storicamente la maggioranza — la riga `matches` viene potata), perche' un
    campione che sparisce in silenzio e' il modo piu' rapido di leggere un
    numero e crederci.
    """
    rows, meta = _rows(since=since, odds_min=odds_min, odds_max=odds_max, now=now)
    leagues: Dict[str, Dict[str, Any]] = {}
    no_league = _bucket()
    for r in rows:
        name = _canonical(r.get("league"))
        b = leagues.setdefault(name, _bucket()) if name else no_league
        outcome = (r.get("esito_finale") or "").strip().lower()
        if outcome == "won":
            b["won"] += 1
        elif outcome == "lost":
            b["lost"] += 1
        elif outcome == "push":
            b["push"] += 1
        try:
            profit = float(r.get("profit") or 0.0)
        except (TypeError, ValueError):
            profit = 0.0
        try:
            odds = float(r.get("quota") or 0.0)
        except (TypeError, ValueError):
            odds = 0.0
        try:
            ev = float(r.get("ev") or 0.0)
        except (TypeError, ValueError):
            ev = 0.0
        b["n"] += 1
        b["profit"] += profit
        b["odds_sum"] += odds
        b["ev_sum"] += ev

    out: Dict[str, Dict[str, Any]] = {}
    for name, b in leagues.items():
        n = int(b["n"])
        # ROI = P/L per unita' di stake, media su TUTTE le chiuse (push a 0):
        # la stessa convenzione di `significance.py` e dei report.
        roi = (b["profit"] / n) if n else 0.0
        status = _status(n, roi)
        out[name] = {
            "league": name,
            "tier": _tier(name),
            "allowed": _allowed(name),
            "n": n,
            "won": b["won"], "lost": b["lost"], "push": b["push"],
            "hit_rate": round(b["won"] / n, 4) if n else 0.0,
            "avg_odds": round(b["odds_sum"] / n, 4) if n else 0.0,
            "profit": round(b["profit"], 4),
            "roi": round(roi, 4),
            "avg_ev": round(b["ev_sum"] / n, 6) if n else 0.0,
            "status": status,
            "enough_sample": n >= min_samples(),
        }
    return {"meta": meta, "leagues": out, "senza_lega": _finalize(no_league)}


def _finalize(b: Dict[str, Any]) -> Dict[str, Any]:
    n = int(b["n"])
    return {"n": n, "won": b["won"], "lost": b["lost"], "push": b["push"],
            "profit": round(b["profit"], 4),
            "roi": round((b["profit"] / n) if n else 0.0, 4)}


def _status(n: int, roi: float) -> str:
    """Stato della lega sul campione misurato (mai oltre l'evidenza)."""
    if n < min_samples():
        return "insufficient"
    if roi <= demote_roi():
        return "demote"
    if roi >= promote_roi():
        return "promote"
    return "hold"


def table(*, use_cache: bool = True, **kw) -> Dict[str, Any]:
    """Tabella per lega (con cache in-process: il giro ordini gira ogni 60s)."""
    if use_cache and _CACHE["table"] and \
            (time.time() - _CACHE["ts"]) < ttl_seconds():
        return _CACHE["table"]
    data = league_stats(**kw)
    _CACHE["table"] = data
    _CACHE["ts"] = time.time()
    return data


def restricted(league: Optional[str]) -> bool:
    """True se il gate DINAMICO esclude la lega (solo con l'env accesa).

    Fail-open su tutto: spento, dato assente, campione insufficiente o errore
    di lettura -> False (nessuna restrizione).
    """
    if not enabled():
        return False
    name = _canonical(league)
    if not name:
        return False
    try:
        row = (table().get("leagues") or {}).get(name)
        if not row or not row.get("enough_sample"):
            return False
        return row.get("status") == "demote"
    except Exception as e:
        logger.debug("league_dynamic: restrizione non calcolabile (%s)", e)
        return False


def effective_allowed(league: Optional[str]) -> bool:
    """Gate STATICO **e** dinamico: il punto d'ingresso per i candidati.

    Delega a `value_filter.league_allowed` (definizione unica del gate) e vi
    applica, solo quando accesa, la restrizione misurata dal ledger.
    """
    if not _allowed(league):
        return False
    return not restricted(league)


def report(*, since: Optional[str] = None, odds_min: Optional[float] = None,
           odds_max: Optional[float] = None,
           now: Optional[datetime] = None) -> Dict[str, Any]:
    """Riepilogo per CLI/report: tabella + stato + cosa farebbe il gate."""
    data = league_stats(since=since, odds_min=odds_min, odds_max=odds_max, now=now)
    leagues = data["leagues"]
    demote = sorted(k for k, v in leagues.items()
                    if v["status"] == "demote" and v["allowed"])
    promote = sorted(k for k, v in leagues.items()
                     if v["status"] == "promote" and v["allowed"])
    return {
        "enabled": enabled(),
        "min_samples": min_samples(),
        "demote_roi": demote_roi(),
        "promote_roi": promote_roi(),
        "filter": data["meta"],
        "n_leagues": len(leagues),
        "restricted": demote,
        "promote_candidates": promote,
        "senza_lega": data["senza_lega"],
        "leagues": dict(sorted(leagues.items())),
    }


def format_report(res: Optional[Dict[str, Any]] = None) -> str:
    """Riga(s) Telegram-friendly sullo stato del gate dinamico."""
    res = res or report()
    f = res["filter"]
    state = "🟢 ON" if res["enabled"] else "⚪ OFF (sola telemetria)"
    era = f["since"] or "SEMPRE"
    lines = [
        "🧭 Gate di lega dinamico — " + state,
        f"Filtro: era dal {era} | quota {f['odds_min']:.2f}-{f['odds_max']:.2f}"
        f" | campione min {res['min_samples']}",
        f"Righe: {f['rows_kept']} giocabili su {f['rows_total']} "
        f"({f['rows_excluded']} non giocabili) — {f.get('rows_filtered_out', 0)} "
        f"fuori da era/fascia su {f.get('rows_unfiltered', 0)} chiuse",
    ]
    if f.get("error"):
        lines.append(f"⚠️ Lettura ledger non disponibile: {f['error']}")
    if not res["leagues"]:
        lines.append("Nessuna lega giocabile chiusa nella finestra.")
    for league, row in res["leagues"].items():
        mark = {"demote": "🚫", "promote": "⬆️", "hold": "✅"}.get(row["status"], "⏳")
        lines.append(
            f"  {mark} {league} [{row['tier'] or 'n/d'}]: n={row['n']} "
            f"ROI {row['roi'] * 100:+.2f}% · hit {row['hit_rate'] * 100:.1f}% "
            f"· quota media {row['avg_odds']:.2f}")
    sl = res["senza_lega"]
    if sl["n"]:
        lines.append(f"  ❔ senza lega attribuibile: n={sl['n']} "
                     f"ROI {sl['roi'] * 100:+.2f}% (non giudicabile per lega)")
    if res["restricted"]:
        lines.append("🚫 Restringerebbe: " + ", ".join(res["restricted"]))
    if res["promote_candidates"]:
        lines.append("⬆️ Candidata alla promozione (DECISIONE UMANA, mai "
                     "automatica): " + ", ".join(res["promote_candidates"]))
    if not res["enabled"]:
        lines.append("Nessuna restrizione applicata (LEAGUE_DYNAMIC_ENABLED=0).")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Gate di lega dinamico misurato dal ledger (default OFF)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--since", default=None,
                    help="data ISO di nascita del segnale (default: era corrente)")
    ap.add_argument("--odds-min", type=float, default=None)
    ap.add_argument("--odds-max", type=float, default=None)
    ap.add_argument("--all", action="store_true",
                    help="nessun filtro di era/quota (CONFRONTO, non decisionale)")
    args = ap.parse_args(argv)
    if args.all:
        res = report(since="", odds_min=1.0, odds_max=1e9)
        if not args.json:
            print("⚠️ --all: nessun filtro — CONFRONTO, non decisionale.\n")
    else:
        res = report(since=args.since, odds_min=args.odds_min,
                     odds_max=args.odds_max)
    if args.json:
        print(json.dumps(res, indent=2, ensure_ascii=False, default=str))
    else:
        print(format_report(res))
    return 0


if __name__ == "__main__":                                    # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
