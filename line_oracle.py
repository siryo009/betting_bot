"""line_oracle.py — Oracolo a LINEA per OU/AH, follow-the-money (30/09/2026).

PERCHE' ESISTE. I pick OU/AH morivano tutti con `no_oracle`: il gate top-down
(`auto_bet._top_down_eval`) legge `probs.get(pick["esito_key"])` dove l'esito
e' 'Over 2.5' / 'Home -0.75' — chiavi che un oracolo 1X2 NON ha mai. La
p_true a linea richiede i mercati `totals,spreads` di Pinnacle, e the-odds-api
addebita `markets x regions` per chiamata: metterli sulla rotazione di ricerca
triplicherebbe OGNI chiamata (~938 crediti/mese, fuori dal tetto 460 anche
tagliando la rotazione).

Il design scelto (direttiva "tagliare la rotazione quote" interpretata come
budget: si taglia il costo pagando SOLO dove c'e' denaro in gioco) e'
FOLLOW-THE-MONEY:
- la rotazione di ricerca resta `markets="h2h"` (1 credito) — INVARIATA;
- una seconda chiamata `markets="h2h,totals,spreads"` (3 crediti) viene fatta
  SOLO per le leghe con pick OU/AH aperti in finestra d'ordine;
- cache separata `toao_<sport>.json` (TTL 24h), budget giornaliero dedicato
  (`ORACLE_BUDGET_DAY`, default 12 leghe) e hard-stop crediti rispettati.

Costo atteso misurato: ~118 crediti/mese (2 crediti extra x ~59 righe
OU/AH giocabili chiuse/giorno, storico 25/09). La DIAGONALE `line_true_probs`
(e' in `pinnacle_oracle`) resta sempre a costo ZERO: legge solo le cache.

GARANZIE (tripwire in `test_line_oracle.py`):
- sola orchestrazione: nessuna scrittura sul ledger, nessun ordine, nessuna
  formula di de-vig (la matematica sta in `pinnacle_oracle`/`market_calib`);
- fail-safe: qualunque errore per lega e' contato in `errors`, mai propagato
  (un problema dell'oracolo non ferma il bot);
- `HTTP` passa solo da `odds_api.fetch_line_odds` (che porta budget, hard-stop
  e should_query_sport): nessuna chiamata diretta a the-odds-api da qui.

CLI: venv/bin/python line_oracle.py [--json] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

#: Finestra dei pick considerati "in gioco" (ore dal kickoff): allineata alla
#: finestra esecutiva T-120..T-15 (prod) con margine: un pick oltre T-120 non
#: genera ordini oggi, non serve pagare la sua linea.
PICK_WINDOW_H = float(os.getenv("ORACLE_PICK_WINDOW_H", "6"))

# Quante leghe fetchare per giro (doppio tetto col budget giornaliero di
# `odds_api.ORACLE_BUDGET_DAY`, default 3 leghe/giorno = 9 crediti): i pick
# piu' vicini al kickoff prima.
LEAGUES_PER_PASS = int(os.getenv("ORACLE_LEAGUES_PER_PASS", "3"))


def _pick_window_h() -> float:
    raw = os.getenv("ORACLE_PICK_WINDOW_H")
    if raw is None:
        return PICK_WINDOW_H
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return PICK_WINDOW_H
    return v if v > 0 else PICK_WINDOW_H


def budget_credits_per_day() -> float:
    """Costo MAX/dell'oracolo a linea in crediti (tetto, non target).

    = leghe/giorno ammesse dal budget `odds_api.ORACLE_BUDGET_DAY` x 3
    crediti a chiamata (`h2h,totals,spreads`). Il test di budget usa questo
    numero: il consumo REALE e' piu' basso (segue i pick in gioco).
    """
    try:
        import odds_api as oa
        budget = int(oa.ORACLE_BUDGET_DAY)
        credits = 1 + int(getattr(oa, "ORACLE_EXTRA_CREDITS", 2))
    except Exception:                                        # pragma: no cover
        budget, credits = 12, 3
    return float(max(0, budget) * credits)


def _parse_iso(ts: Any) -> Optional[datetime]:
    """Kickoff del ledger ('T'/'Z'/spazio) -> datetime UTC. None se ignoto."""
    try:
        s = str(ts or "").strip().replace("Z", "+00:00").replace(" ", "T")
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def line_picks() -> List[Dict[str, Any]]:
    """Pick OU/AH aperti in finestra, con lega risolta in sport key.

    Riusa ESATTAMENTE la selezione della corsia d'ordine
    (`multi_market.live_picks`: lega ammessa, fascia quota, lato favorito,
    esito riconoscibile) cosi' il budget segue il DENARO, non la telemetria.
    La lega viene convertita con `sx_signals.league_to_sport` (l'UNICA mappa
    SX/the-odds-api del progetto). Ordinati per kickoff crescente (prima le
    partite piu' vicine: sono quelle che entreranno in finestra T-60).
    """
    try:
        import multi_market
        picks = multi_market.live_picks(hours=_pick_window_h() * 4)
    except Exception as exc:                                     # pragma: no cover
        logger.warning("line_oracle: corsia multi-mercato non disponibile (%s)",
                       exc)
        return []
    try:
        from sx_signals import league_to_sport
    except Exception as exc:                                     # pragma: no cover
        logger.warning("line_oracle: league_to_sport non disponibile (%s)", exc)
        return []
    now = datetime.now(timezone.utc)
    deadline = now + timedelta(hours=_pick_window_h() * 4)
    out: List[Dict[str, Any]] = []
    seen: set = set()
    for p in picks:
        k = (p.get("match_id"), p.get("esito_key"))
        if k in seen:
            continue
        seen.add(k)
        ko = _parse_iso(p.get("commence"))
        if ko is None or ko > deadline:
            continue
        sport = league_to_sport(p.get("league") or "")
        if not sport:
            continue
        out.append({"match_id": p.get("match_id"), "esito_key": p.get("esito_key"),
                    "league": p.get("league"), "sport_key": sport,
                    "kickoff": ko.isoformat(),
                    "kickoff_ts": ko.timestamp()})
    out.sort(key=lambda x: x["kickoff_ts"])
    return out


def leagues_needing_fetch(now: Optional[float] = None) -> List[Dict[str, Any]]:
    """Sport key DISTINTI con pick a linea in gioco e cache oracolo stantia.

    La cache `toao_<sport>.json` copre TUTTA la lega: se e' fresca (TTL 24h)
    la lega non serve (gia' pagata). `expired` e' letto a runtime dal file di
    cache, come fa `odds_api._get_odds`.
    """
    try:
        import odds_api as oa
        from config import DATA_DIR
        from pathlib import Path
    except Exception as exc:                                     # pragma: no cover
        logger.warning("line_oracle: odds_api non disponibile (%s)", exc)
        return []
    ts_now = time.time() if now is None else float(now)
    # Finestra "in gioco": un pick oltre l'orizzonte non genera ordini oggi
    # (la sua linea si paga quando si avvicina il kickoff).
    deadline = ts_now + _pick_window_h() * 4 * 3600.0
    per_sport: Dict[str, Dict[str, Any]] = {}
    for p in line_picks():
        if float(p.get("kickoff_ts") or 0) > deadline:
            continue
        sp = p["sport_key"]
        if sp not in per_sport:
            per_sport[sp] = {"sport_key": sp, "league": p.get("league"),
                             "picks": 0, "min_kickoff": p["kickoff_ts"]}
        per_sport[sp]["picks"] += 1
        per_sport[sp]["min_kickoff"] = min(per_sport[sp]["min_kickoff"],
                                           p["kickoff_ts"])
    out: List[Dict[str, Any]] = []
    for sp, info in per_sport.items():
        cache = Path(DATA_DIR) / f"{oa.ORACLE_CACHE_PREFIX}{sp}.json"
        age = None
        try:
            if cache.exists():
                data = json.loads(cache.read_text())
                age = (ts_now - float(data.get("ts") or 0))
                if age < oa.ORACLE_CACHE_TTL_S:
                    continue        # cache fresca: la lega e' gia' coperta
        except Exception:
            age = None              # cache corrotta = da rifare
        info["cache_age_h"] = round(age / 3600.0, 1) if age is not None else None
        out.append(info)
    out.sort(key=lambda x: x["min_kickoff"])
    return out


def ensure_oracle_payloads(max_leagues: Optional[int] = None
                           ) -> Dict[str, Any]:
    """Fetch `h2h,totals,spreads` per le leghe che ne hanno bisogno.

    Costo: 3 crediti per lega fetchata (via `odds_api.fetch_line_odds`, che
    porta budget giornaliero e hard-stop). Ritorna un riepilogo DICHIARATO:
    `leagues` (richieste), `fetched`/`skipped`/`errors`, `requests_today`.
    """
    try:
        import odds_api as oa
    except Exception as exc:                                     # pragma: no cover
        return {"leagues": [], "fetched": 0, "skipped": 0, "errors": 1,
                "error": str(exc), "requests_today": 0}
    cap = LEAGUES_PER_PASS if max_leagues is None else int(max_leagues)
    pending = leagues_needing_fetch()[:max(0, cap)]
    res: Dict[str, Any] = {"leagues": [x["sport_key"] for x in pending],
                           "fetched": 0, "skipped": 0, "errors": 0,
                           "rows": [], "requests_today":
                           getattr(oa, "_oracle_req_day", {}).get("n", 0)}
    now = datetime.now(timezone.utc)
    frm = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    to = (now + timedelta(hours=_pick_window_h() * 4)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    for info in pending:
        sp = info["sport_key"]
        try:
            payload, remaining = oa.fetch_line_odds(sp, frm, to)
        except Exception as exc:
            res["errors"] += 1
            res["rows"].append({"sport": sp, "error": str(exc)})
            continue
        if payload:
            res["fetched"] += 1
            res["rows"].append({"sport": sp, "matches": len(payload),
                                "remaining": remaining})
        else:
            res["skipped"] += 1
            res["rows"].append({"sport": sp, "skipped": True,
                                "remaining": remaining})
    res["requests_today"] = getattr(oa, "_oracle_req_day", {}).get("n", 0)
    return res


def format_report(res: Dict[str, Any]) -> str:
    lines = ["🎯 Oracolo a linea OU/AH (follow-the-money)"]
    leagues = res.get("leagues") or []
    if not leagues:
        lines.append("  nessuna lega da fetchare (nessun pick a linea in "
                     "gioco o cache fresche)")
        return "\n".join(lines)
    lines.append(f"  leghe richieste: {len(leagues)} | fetch {res.get('fetched', 0)}"
                 f" | skip {res.get('skipped', 0)} | errori {res.get('errors', 0)}"
                 f" | richieste oggi {res.get('requests_today', 0)}")
    for r in res.get("rows") or []:
        if r.get("error"):
            lines.append(f"  ⚠️ {r.get('sport')}: errore {r.get('error')}")
        elif r.get("skipped"):
            lines.append(f"  • {r.get('sport')}: skip (remaining "
                         f"{r.get('remaining')})")
        else:
            lines.append(f"  ✅ {r.get('sport')}: {r.get('matches')} match "
                         f"(crediti {r.get('remaining')})")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:               # pragma: no cover
    ap = argparse.ArgumentParser(description="Oracolo a linea OU/AH")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--dry-run", action="store_true",
                    help="mostra le leghe che verrebbero fetchate (no HTTP)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    if args.dry_run:
        pending = leagues_needing_fetch()
        print(json.dumps(pending, indent=2, ensure_ascii=False))
        return 0
    res = ensure_oracle_payloads()
    if args.json:
        print(json.dumps(res, indent=2, ensure_ascii=False))
    else:
        print(format_report(res))
    return 0


if __name__ == "__main__":                                       # pragma: no cover
    raise SystemExit(main())
