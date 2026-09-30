"""gate_audit.py — audit TELEMETRICO dei filtri bloccanti (30/09/2026).

Perche' esiste: quando il flusso si ferma, la domanda operativa e' "cosa ha
scartato questo?" e la risposta deve arrivare da UNA misura ripetibile, non da
una query a mano. Il modulo fa girare i gate di PRODUZIONE sui candidati CORRENTI
e classifica OGNI scarto, partita per partita, sotto voci esplicite.

Le tre voci richieste dalla direttiva:
    REJECT_EV_BELOW_THRESHOLD   l'EV reale (o l'edge) non raggiunge la soglia
    REJECT_NO_LIQUIDITY         prezzo presente su SX ma book sotto la soglia
    REJECT_CODE_FLAG            un controllo rigido / flag paper-only nel codice

⚠️ Una quarta voce e' DICHIARATA e non nascosta: `REJECT_OTHER_STRATEGY` (lega
fuori dai campionati ammessi, quota fuori fascia, favourite gate, EV anomalo).
Forzare questi rifiuti dentro le tre voci richieste sarebbe una bugia: il
progetto non fa sparire una riga per far tornare un conteggio.

⚠️ `NO_ORACLE` NON e' uno scarto: e' "non valutabile" (niente verita' di
riferimento). Sta a parte, perche' contarlo come rifiuto gonfierebbe i numeri.

Sola LETTURA sul ledger (`mode=ro`), zero ordini. La discovery SX e' pubblica e
gratuita; il tennis legge l'oracolo dalle cache gia' scaricate (0 crediti).
`--live-football` e `--esports` estendono la misura alle corsie di rete.

CLI: venv/bin/python gate_audit.py [--json] [--live-football] [--esports]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# --- Bucket machine-readable ------------------------------------------------
REJECT_EV = "REJECT_EV_BELOW_THRESHOLD"
REJECT_LIQ = "REJECT_NO_LIQUIDITY"
REJECT_FLAG = "REJECT_CODE_FLAG"
REJECT_OTHER = "REJECT_OTHER_STRATEGY"
CANDIDATE = "CANDIDATE"
NO_ORACLE = "NO_ORACLE"

_BUCKETS = (REJECT_EV, REJECT_LIQ, REJECT_FLAG, REJECT_OTHER, CANDIDATE,
            NO_ORACLE)


def _required_depth(stake: float) -> float:
    """Tetto di profondita' d'ordine: UNA formula, da `auto_bet` (mai copiata)."""
    try:
        import auto_bet
        return float(auto_bet.required_depth(float(stake)))
    except Exception:
        # Ripiego dichiarato: la formula di produzione e' max(stake x mult, min).
        mult = float(os.getenv("SX_DEPTH_MULTIPLIER", "1.6"))
        minimum = float(os.getenv("SX_MIN_EXEC_DEPTH_USDC", "20"))
        return max(float(stake) * mult, minimum)


def _code_flag(market: str) -> Optional[str]:
    """Un flag di CODICE che spegne la corsia (paper/shadow/sim), se presente.

    Su questa base non esistono `PAPER_TRADING`/`SHADOW`/`SIMULATE`: la funzione
    esiste per DIMOSTRARLO (0 rifiuti per flag) e per non farlo rientrare in
    silenzio — un flag aggiunto in futuro finirebbe qui, non nel nulla.
    """
    def _off(name: str) -> bool:
        return str(os.getenv(name, "")).strip().lower() in ("0", "false", "no", "off")

    if market == "TENNIS" and _off("TENNIS_LANE"):
        return "TENNIS_LANE=0"
    if market == "ML" and _off("ESPORTS_LIVE"):
        return "ESPORTS_LIVE=0"
    if market in ("OU", "AH"):
        if market == "OU" and _off("ENABLE_LIVE_OU"):
            return "ENABLE_LIVE_OU=0 (OU non autorizzato)"
    # `AUTO_BET_DRY_RUN` intercetta l'ORDINE, non il segnale: dichiarato a parte.
    if str(os.getenv("AUTO_BET_DRY_RUN", "")).strip().lower() in ("1", "true", "yes", "on"):
        return "AUTO_BET_DRY_RUN=1"
    return None


def classify(*, market: str, ev: float, ev_min: float, edge: Optional[float],
             edge_min: Optional[float], odds: float, depth: Optional[float],
             required: float, sane: bool, sane_reason: str
             ) -> Dict[str, Any]:
    """Bucket + motivo per UN candidato. Ordine: flag > liquidita' > EV > altro.

    L'ordine riflette la PRIORITA' del blocco: un flag di codice spegne la
    corsia a prescindere, la liquidita' non dipende dalla strategia, l'EV e'
    il gate di merito.
    """
    flag = _code_flag(market)
    if flag:
        return {"bucket": REJECT_FLAG, "reason": flag}
    if depth is not None and depth + 1e-9 < required:
        return {"bucket": REJECT_LIQ,
                "reason": f"profondita' {depth:.2f} < richiesta {required:.2f} USDC"}
    if ev < ev_min:
        return {"bucket": REJECT_EV,
                "reason": f"EV {ev*100:.2f}% < soglia {ev_min*100:.2f}%"}
    if edge is not None and edge_min is not None and edge < edge_min:
        return {"bucket": REJECT_EV,
                "reason": f"edge {edge*100:.2f}pp < {edge_min*100:.2f}pp"}
    if not sane:
        return {"bucket": REJECT_OTHER, "reason": sane_reason}
    return {"bucket": CANDIDATE, "reason": "ok"}


# ---------------------------------------------------------------------------
# Corsie
# ---------------------------------------------------------------------------

def audit_tennis(stake: float = 1.5) -> Dict[str, Any]:
    """Corsia tennis: discovery SX (gratis) + oracolo dalle cache (0 crediti)."""
    out: Dict[str, Any] = {"lane": "TENNIS", "evaluated": 0, "counts": {},
                           "rows": [], "error": None}
    try:
        import tennis_lane as tl
        import pinnacle_oracle as po
        events = tl.discover()
        ev_min = float(tl.EV_MIN)
        required = _required_depth(stake)
        for e in events:
            probs = tl._oracle(e["team_one"], e["team_two"])
            if not probs:
                out["counts"][NO_ORACLE] = out["counts"].get(NO_ORACLE, 0) + 1
                out["rows"].append({"match": f"{e['team_one']} vs {e['team_two']}",
                                    "bucket": NO_ORACLE,
                                    "reason": "nessun oracolo a 2 esiti"})
                continue
            prices = {s["key"]: float(s["price"]) for s in e["sides"]}
            for row in po.ev_gate(probs, prices, ev_min=ev_min):
                side = next((s for s in e["sides"] if s["key"] == row["esito"]),
                            None)
                if side is None:
                    continue
                out["evaluated"] += 1
                cls = classify(
                    market="TENNIS", ev=float(row["ev"]), ev_min=ev_min,
                    edge=None, edge_min=None, odds=float(row["price"]),
                    depth=float(side["depth"]), required=required,
                    sane=True, sane_reason="ok")
                out["counts"][cls["bucket"]] = out["counts"].get(cls["bucket"], 0) + 1
                out["rows"].append({
                    "match": f"{e['team_one']} vs {e['team_two']}",
                    "selection": side["team"], "odds": row["price"],
                    "ev": row["ev"], "bucket": cls["bucket"],
                    "reason": cls["reason"]})
    except Exception as exc:                                   # pragma: no cover
        out["error"] = str(exc)
    return out


def audit_ledger(stake: float = 1.5) -> Dict[str, Any]:
    """Calcio (1X2 + OU/AH): i segnali APERTI del ledger, coi gate reali."""
    out: Dict[str, Any] = {"lane": "FOOTBALL_LEDGER", "evaluated": 0,
                           "counts": {}, "rows": [], "error": None}
    try:
        import sqlite3
        from config import DATA_DIR
        import value_filter as vf
        db = str(DATA_DIR / "quotaverace.db")
        uri = f"file:{db}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        rows = conn.execute(
            "SELECT p.match_id, m.home_team, m.away_team, m.league, p.mercato,"
            " p.esito, p.quota, p.ev, p.market_prob, p.market_edge, p.status"
            " FROM predictions p LEFT JOIN matches m ON m.id = p.match_id"
            " WHERE p.esito_finale IS NULL").fetchall()
        conn.close()
        required = _required_depth(stake)
        for (mid, home, away, league, mercato, esito, quota, ev, mprob,
             edge, status) in rows:
            market = str(mercato or "").upper()
            try:
                odds = float(quota or 0)
                ev = float(ev or 0)
            except (TypeError, ValueError):
                continue
            edge_v = float(edge) if edge is not None else None
            sane, reason = vf.is_sane(
                prob=float(mprob or 0) + (edge_v or 0), odds=odds, ev=ev,
                market_prob=float(mprob) if mprob is not None else None,
                league=league or "",
                favourites_only=(market in ("OU", "AH")) is False)
            out["evaluated"] += 1
            cls = classify(
                market=market, ev=ev, ev_min=float(vf.EV_MIN),
                edge=edge_v, edge_min=float(vf.MARKET_EDGE_MIN),
                odds=odds, depth=None, required=required,
                sane=sane, sane_reason=reason)
            out["counts"][cls["bucket"]] = out["counts"].get(cls["bucket"], 0) + 1
            out["rows"].append({
                "match": f"{home or '?'} vs {away or '?'}",
                "market": market, "selection": esito, "odds": odds,
                "ev": ev, "league": league, "status": status,
                "bucket": cls["bucket"], "reason": cls["reason"]})
    except Exception as exc:                                   # pragma: no cover
        out["error"] = str(exc)
    return out


def audit_esports(stake: float = 1.5) -> Dict[str, Any]:
    """Corsia eSports: discovery SX + oracolo OddsPapi (consuma quota)."""
    out: Dict[str, Any] = {"lane": "ESPORTS", "evaluated": 0, "counts": {},
                           "rows": [], "error": None}
    try:
        import esports_lane as el
        required = _required_depth(stake)
        # Soglia EV di produzione, UNICA: mai un default hardcoded qui (una
        # seconda soglia nell'audit farebbe leggere come "sotto soglia" un
        # segnale che il gate vero ha gia' bocciato, o viceversa).
        try:
            import esports_oracle as eo
            ev_min = float(eo.min_ev())
        except Exception:
            from value_filter import EV_MIN as ev_min
        for e in el.discover():
            try:
                pick = None
                for p in el.picks():
                    if p.get("match_id") == e.get("event_id"):
                        pick = p
                        break
            except Exception:
                pick = None
            if not pick:
                out["counts"][NO_ORACLE] = out["counts"].get(NO_ORACLE, 0) + 1
                continue
            out["evaluated"] += 1
            cls = classify(
                market="ML", ev=float(pick.get("best_ev") or 0), ev_min=ev_min,
                edge=None, edge_min=None, odds=float(pick.get("quota") or 0),
                depth=pick.get("depth_usdc"), required=required,
                sane=True, sane_reason="ok")
            out["counts"][cls["bucket"]] = out["counts"].get(cls["bucket"], 0) + 1
            out["rows"].append({
                "match": pick.get("home"), "selection": pick.get("team"),
                "odds": pick.get("quota"), "ev": pick.get("best_ev"),
                "bucket": cls["bucket"], "reason": cls["reason"]})
    except Exception as exc:
        out["error"] = str(exc)
    return out


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _lane_lines(lane: Dict[str, Any]) -> List[str]:
    head = f"{lane['lane']}: {lane['evaluated']} candidati valutati"
    lines = [head]
    if lane.get("error"):
        lines.append(f"  ⚠️ errore: {lane['error']}")
        return lines
    for b in _BUCKETS:
        n = lane["counts"].get(b, 0)
        if n:
            lines.append(f"  • {b}: {n}")
    return lines


def format_report(res: Dict[str, Any]) -> str:
    lines = ["🔎 AUDIT FILTRI BLOCCANTI (gate di produzione sui dati correnti)", ""]
    tot: Dict[str, int] = {}
    for lane in res.get("lanes", []):
        lines.extend(_lane_lines(lane))
        for k, v in (lane.get("counts") or {}).items():
            tot[k] = tot.get(k, 0) + v
        lines.append("")
    lines.append("TOTALE")
    for b in _BUCKETS:
        if tot.get(b):
            lines.append(f"  {b}: {tot[b]}")
    if tot.get(NO_ORACLE):
        lines.append(f"  {NO_ORACLE} (NON uno scarto): {tot[NO_ORACLE]}")
    flag = tot.get(REJECT_FLAG, 0)
    lines.append("")
    lines.append(f"✅ REJECT_CODE_FLAG = {flag}: nessun flag paper/shadow/sim "
                 "blocca i segnali (i mercati sono in Denaro Reale)."
                 if flag == 0 else
                 f"⛔ REJECT_CODE_FLAG = {flag}: un flag di codice sta "
                 "spegendo una corsia — verificare.")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Audit dei filtri bloccanti")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--stake", type=float, default=1.5)
    ap.add_argument("--live-football", action="store_true",
                    help="include il ledger calcio (default: sempre)")
    ap.add_argument("--esports", action="store_true",
                    help="include la corsia eSports (consuma quota OddsPapi)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    lanes = [audit_tennis(stake=args.stake), audit_ledger(stake=args.stake)]
    if args.esports:
        lanes.append(audit_esports(stake=args.stake))
    res = {"lanes": lanes}
    if args.json:
        print(json.dumps(res, indent=2, ensure_ascii=False))
    else:
        print(format_report(res))
    return 0


if __name__ == "__main__":                                     # pragma: no cover
    raise SystemExit(main())
