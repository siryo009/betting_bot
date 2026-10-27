"""performance_report.py — Reportistica performance: ROI, CLV, calibrazione.

Modulo di reportistica che traccia:
- ROI realizzato vs EV atteso (calibrazione del modello)
- CLV raw, vig-free e vs Pinnacle
- Drawdown e peak bankroll
- Win rate per mercato, lega, status (value/strong_value)
- Streak (serie vincenti/perdenti)
- Edge moyenne vs mercato

CLI:
  venv/bin/python performance_report.py               # report ultimi 30gg
  venv/bin/python performance_report.py --days 7      # ultimi 7gg
  venv/bin/python performance_report.py --json         # output JSON
  venv/bin/python performance_report.py --period 2026-09-01  # da data
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from tracker import _get_conn
from market_calib import MARKET_EDGE_MODERATE, MARKET_EDGE_STRONG


def _safe_float(v, default=0.0) -> float:
    if v is None:
        return default
    try:
        return float(v)
    except (ValueError, TypeError):
        return default


def get_performance(period_start: str = None, period_end: str = None,
                    days: int = 30) -> Dict:
    """Calcola le performance per un periodo.

    Args:
        period_start: data inizio ISO (default: oggi - days)
        period_end: data fine ISO (default: adesso)
        days: giorni se period_start non specificato

    Returns:
        Dict con tutte le metriche di performance.
    """
    if not period_start:
        period_start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    if not period_end:
        period_end = datetime.now().strftime("%Y-%m-%d")

    conn = _get_conn()
    try:
        # === 1. Previsioni chiuse ===
        by_mkt = _predictions_by_market(conn, period_start, period_end)
        total_n = sum(m["n"] for m in by_mkt.values())
        total_won = sum(m["won"] for m in by_mkt.values())
        total_lost = sum(m["lost"] for m in by_mkt.values())
        total_push = sum(m["push"] for m in by_mkt.values())
        total_pnl = sum(m["pnl"] for m in by_mkt.values())
        total_ev = sum(m.get("avg_ev", 0) * m["n"] / 100.0
                       for m in by_mkt.values())

        # === 2. CLV ===
        clv_data = _clv_stats(conn, period_start, period_end)

        # === 3. Puntate automatiche ===
        bets_data = _bets_stats(conn, period_start, period_end)

        # === 4. Bankroll / Drawdown ===
        bankroll_data = _bankroll_stats(conn)

        # === 5. Win streak ===
        streaks = _calc_streaks(conn, period_start, period_end)

        # === 6. Performance per lega ===
        by_league = _by_league(conn, period_start, period_end)

        # === 7. Edge vs mercato ===
        edge_data = _edge_analysis(conn, period_start, period_end)

        return {
            "period": {"start": period_start, "end": period_end},
            "predictions": {
                "total": total_n,
                "won": total_won,
                "lost": total_lost,
                "push": total_push,
                "hit_rate": (total_won / (total_won + total_lost) * 100
                             if (total_won + total_lost) > 0 else 0),
                "pnl": round(total_pnl, 2),
                "roi": (total_pnl / total_n * 100 if total_n > 0 else 0),
                "avg_ev": (total_ev / total_n * 100 if total_n > 0 else 0),
                "calibration_gap": (round(total_pnl / total_n * 100, 2) -
                                    round(total_ev / total_n * 100, 2)
                                    if total_n > 0 else 0),
                "by_market": by_mkt,
            },
            "clv": clv_data,
            "bets": bets_data,
            "bankroll": bankroll_data,
            "streaks": streaks,
            "by_league": by_league,
            "edge": edge_data,
        }
    finally:
        conn.close()


def _predictions_by_market(conn, start: str, end: str) -> Dict:
    """Previsioni chiuse per mercato nel periodo."""
    c = conn.cursor()
    rows = c.execute(
        "SELECT mercato, "
        "  COUNT(*) as n, "
        "  SUM(CASE WHEN esito_finale='won' THEN 1 ELSE 0 END) as won, "
        "  SUM(CASE WHEN esito_finale='lost' THEN 1 ELSE 0 END) as lost, "
        "  SUM(CASE WHEN esito_finale='push' THEN 1 ELSE 0 END) as push, "
        "  SUM(CASE WHEN esito_finale='won' THEN quota-1 ELSE -1 END) as pnl, "
        "  AVG(ev) as avg_ev, "
        "  AVG(market_edge) as avg_edge "
        "FROM predictions "
        "WHERE settled_at >= ? AND settled_at <= ? "
        "  AND esito_finale IS NOT NULL "
        "GROUP BY mercato",
        (start, end + "T23:59:59")).fetchall()

    out = {}
    for mkt, n, won, lost, push, pnl, avg_ev, avg_edge in rows:
        won = _safe_float(won, 0)
        lost = _safe_float(lost, 0)
        out[mkt] = {
            "n": int(n),
            "won": int(won),
            "lost": int(lost),
            "push": int(push or 0),
            "pnl": round(_safe_float(pnl), 2),
            "roi": round(_safe_float(pnl) / int(n) * 100, 2) if n > 0 else 0,
            "avg_ev": round(_safe_float(avg_ev) * 100, 2),
            "avg_edge": round(_safe_float(avg_edge) * 100, 2),
            "hit_rate": round(won / (won + lost) * 100, 1) if (won + lost) > 0 else 0,
        }
    return out


def _clv_stats(conn, start: str, end: str) -> Dict:
    """Statistiche CLV nel periodo (raw, vig-free, vs Pinnacle).

    - Vig-free USA DAVVERO clv_vig_free() (devig): prima era una copia del
      vs-Pinnacle — due metriche identiche con nomi diversi.
    - Le righe con UN SOLO campione prezzo (closing = eco del segnale, CLV
      finto 0) sono escluse dalle medie: inquinavano la metrica reale
      (es. CLV vig-free -3.85% = esattamente 1/1.04 - 1, l'artefatto del
      fallback overround stimato, non un segnale di mercato).
    """
    c = conn.cursor()
    rows = c.execute(
        "SELECT signal_quota, closing_quota, pinnacle_quota "
        "FROM clv_history WHERE updated_at >= ? AND updated_at <= ?",
        (start, end + "T23:59:59")).fetchall()

    raw_clvs = []
    vf_clvs = []
    pin_clvs = []
    pending = 0  # righe con un solo campione: closing non ancora reale

    try:
        from market_calib import clv_vig_free
    except Exception:
        clv_vig_free = None

    for sig, clos, pin in rows:
        if not (sig and clos and sig > 0 and clos > 0):
            continue
        if abs(clos - sig) < 1e-9:
            # Nessuna lettura di chiusura dopo il segnale: niente CLV ancora.
            pending += 1
            continue
        raw_clvs.append((sig / clos) - 1.0)
        if pin and pin > 0:
            pin_clvs.append((sig / pin) - 1.0)
        if clv_vig_free is not None:
            # Vig-free: Pinnacle come fair se disponibile, altrimenti la
            # closing devigata (come il report giornaliero in bot.py).
            fair = pin if (pin and pin > 0) else clos
            vf = clv_vig_free(sig, fair)
            if vf is not None:
                vf_clvs.append(vf)

    return {
        "n": len(raw_clvs),
        "pending": pending,
        "avg_raw": round(sum(raw_clvs) / len(raw_clvs) * 100, 2) if raw_clvs else 0,
        "avg_vf": round(sum(vf_clvs) / len(vf_clvs) * 100, 2) if vf_clvs else 0,
        "avg_vs_pinnacle": round(sum(pin_clvs) / len(pin_clvs) * 100, 2) if pin_clvs else 0,
        "positive_pct": (round(sum(1 for c in raw_clvs if c > 0) / len(raw_clvs) * 100, 1)
                         if raw_clvs else 0),
    }


#: Fasce di quota del controllo delle puntate reali (27/10/2026). Sono le
#: fasce su cui il progetto ha gia' misurato differenze di rendimento: la
#: tabella serve a leggere il P/L DOVE sta il denaro, non a filtrare nulla.
BET_ODDS_BANDS = ((1.0, 1.30), (1.30, 1.50), (1.50, 1.80),
                  (1.80, 2.00), (2.00, 2.30), (2.30, 1000.0))


def _band_label(price: float) -> str:
    """Etichetta della fascia di quota (estremo inferiore incluso)."""
    try:
        p = float(price)
    except (TypeError, ValueError):
        return "n/d"
    for lo, hi in BET_ODDS_BANDS:
        if lo <= p < hi:
            return (f"{lo:.2f}-{hi:.2f}" if hi < 1000 else f">={lo:.2f}")
    return "n/d"


def _bets_stats(conn, start: str, end: str) -> Dict:
    """Statistiche puntate automatiche nel periodo.

    Oltre al totale (e allo split per `mode`) espone la ripartizione che serve a
    CAPIRCI qualcosa: per mercato, per fascia di quota, per lega, per giorno,
    piu' le puntate ancora APERTE e le ultime chiuse. Il 27/10/2026 il conto
    live era a -8,8% di ROI su 100 puntate e il dashboard mostrava solo il
    totale: senza lo split non si poteva dire QUALE corsia perdeva (il tennis
    pesava zero sul totale e la meta' delle puntate).
    """
    c = conn.cursor()
    rows = c.execute(
        "SELECT price, stake, esito_finale, profit, mode, mercato, league, "
        "       match_id, created_at, settled_at "
        "FROM bets WHERE created_at >= ? AND created_at <= ? "
        "AND esito_finale IS NOT NULL ORDER BY settled_at",
        (start, end + "T23:59:59")).fetchall()

    if not rows:
        return {"n": 0, "pnl": 0, "roi": 0, "modes": {}, "by_market": {},
                "by_band": {}, "by_league": {}, "by_day": {},
                "open": [], "open_n": 0, "open_stake": 0.0, "last": []}

    total_stake = sum(_safe_float(r[1]) for r in rows)
    total_pnl = sum(_safe_float(r[3]) for r in rows)
    won = sum(1 for r in rows if r[2] == "won")

    modes = {}
    for row in rows:
        stake, esito, profit, mode = row[1], row[2], row[3], row[4]
        m = mode or "unknown"
        if m not in modes:
            modes[m] = {"n": 0, "won": 0, "stake": 0, "pnl": 0}
        modes[m]["n"] += 1
        modes[m]["stake"] += _safe_float(stake)
        modes[m]["pnl"] += _safe_float(profit)
        if esito == "won":
            modes[m]["won"] += 1

    def _bucket(buckets, key, stake, esito, profit):
        # Il dizionario e' un PARAMETRO esplicito: chiuso nella closure veniva
        # riusato l'ULTIMO legato dal ciclo (il giorno finiva dentro le leghe).
        d = buckets.setdefault(key, {"n": 0, "won": 0, "lost": 0,
                                     "stake": 0.0, "pnl": 0.0})
        d["n"] += 1
        d["stake"] += stake
        d["pnl"] += profit
        if esito == "won":
            d["won"] += 1
        elif esito == "lost":
            d["lost"] += 1

    by_market: Dict = {}
    by_band: Dict = {}
    by_league: Dict = {}
    by_day: Dict = {}
    for price, stake, esito, profit, mode, mkt, league, _mid, _cr, settled in rows:
        st = _safe_float(stake)
        pl = _safe_float(profit)
        for key, buckets in (
                ((mkt or "1X2").upper(), by_market),
                (_band_label(price), by_band),
                ((league or "n/d"), by_league)):
            _bucket(buckets, key, st, esito, pl)
        day = (settled or _cr or "")[:10] or "n/d"
        _bucket(by_day, day, st, esito, pl)

    def _finalize(buckets):
        out = {}
        for k, v in buckets.items():
            st = v["stake"]
            closed = v["won"] + v["lost"]
            out[k] = {"n": v["n"], "won": v["won"], "lost": v["lost"],
                      "stake": round(st, 2), "pnl": round(v["pnl"], 2),
                      "roi": round(v["pnl"] / st * 100, 2) if st > 0 else 0,
                      "hit_rate": (round(v["won"] / closed * 100, 1)
                                   if closed else 0)}
        return out

    # Ultime chiuse: servono a vedere il P/L RECENTE (l'aggregato lo diluisce).
    last = [{"match_id": r[7], "mercato": (r[5] or "1X2"),
             "esito": r[2], "price": round(_safe_float(r[0]), 4),
             "stake": round(_safe_float(r[1]), 2),
             "pnl": round(_safe_float(r[3]), 2),
             "league": r[6] or "",
             "settled_at": (r[9] or "")[:19]}
            for r in rows[-10:]][::-1]

    # Puntate ANCORA APERTE (indipendenti dalla finestra: sono capitale
    # impegnato ADESSO, e il loro stadio e' il "piazzamento in corso").
    open_rows = c.execute(
        "SELECT match_id, mercato, esito, price, stake, created_at, "
        "       home, away, league, commence "
        "FROM bets WHERE esito_finale IS NULL ORDER BY created_at DESC"
    ).fetchall()
    open_bets = [{"match_id": r[0], "mercato": (r[1] or "1X2"),
                  "esito": r[2], "price": round(_safe_float(r[3]), 4),
                  "stake": round(_safe_float(r[4]), 2),
                  "created_at": (r[5] or "")[:19],
                  "match": (f"{r[6]} - {r[7]}" if r[6] else ""),
                  "league": r[8] or "", "commence": (r[9] or "")[:19]}
                 for r in open_rows]

    return {
        "n": len(rows),
        "won": won,
        "lost": len(rows) - won,
        "total_stake": round(total_stake, 2),
        "pnl": round(total_pnl, 2),
        "roi": round(total_pnl / total_stake * 100, 2) if total_stake > 0 else 0,
        "modes": {k: {kk: round(vv, 2) if isinstance(vv, float) else vv
                       for kk, vv in v.items()}
                  for k, v in modes.items()},
        "by_market": _finalize(by_market),
        "by_band": _finalize(by_band),
        "by_league": _finalize(by_league),
        "by_day": _finalize(by_day),
        "open": open_bets[:10],
        "open_n": len(open_bets),
        "open_stake": round(sum(_safe_float(r[4]) for r in open_rows), 2),
        "last": last,
    }


def _bankroll_stats(conn) -> Dict:
    """Statistiche bankroll e drawdown."""
    c = conn.cursor()
    try:
        # Colonna reale della cassa: importo (prima 'amount' non esisteva →
        # bankroll sempre 0/fallback: bug che nascondeva il bankroll reale).
        row = c.execute("SELECT COALESCE(SUM(importo), 0) FROM cassa").fetchone()
        current = float(row[0]) if row else 0.0

        peak_row = c.execute(
            "SELECT COALESCE(MAX(running_total), 0) FROM ("
            "  SELECT SUM(importo) OVER (ORDER BY id) as running_total FROM cassa"
            ")").fetchone()
        peak = float(peak_row[0]) if peak_row else current

        dd_pct = (1 - current / peak) * 100 if peak > 0 else 0

        return {
            "current": round(current, 2),
            "peak": round(peak, 2),
            "drawdown_pct": round(dd_pct, 1),
            "risk_level": ("🟢 OK" if dd_pct < 5 else
                           "🟡 cautela" if dd_pct < 15 else
                           "🔴 HIGH"),
        }
    except Exception:
        return {"current": 0, "peak": 0, "drawdown_pct": 0, "risk_level": "❓"}


def _calc_streaks(conn, start: str, end: str) -> Dict:
    """Calcola streak vincenti/perdenti."""
    c = conn.cursor()
    rows = c.execute(
        "SELECT esito_finale FROM predictions "
        "WHERE settled_at >= ? AND settled_at <= ? AND esito_finale IS NOT NULL "
        "ORDER BY settled_at",
        (start, end + "T23:59:59")).fetchall()

    if not rows:
        return {"current_streak": 0, "current_type": "none",
                "max_win_streak": 0, "max_loss_streak": 0}

    results = [r[0] for r in rows]

    # Streak corrente
    current_type = results[-1]
    current_streak = 0
    for r in reversed(results):
        if r == current_type:
            current_streak += 1
        else:
            break

    # Max streaks
    max_win = max_loss = 0
    streak = 0
    streak_type = None
    for r in results:
        if r == streak_type:
            streak += 1
        else:
            streak_type = r
            streak = 1
        if r == "won":
            max_win = max(max_win, streak)
        elif r == "lost":
            max_loss = max(max_loss, streak)

    return {
        "current_streak": current_streak,
        "current_type": current_type,
        "max_win_streak": max_win,
        "max_loss_streak": max_loss,
    }


def _by_league(conn, start: str, end: str) -> Dict:
    """Performance per lega."""
    c = conn.cursor()
    rows = c.execute(
        "SELECT m.league, "
        "  COUNT(*) as n, "
        "  SUM(CASE WHEN p.esito_finale='won' THEN 1 ELSE 0 END) as won, "
        "  SUM(CASE WHEN p.esito_finale='won' THEN p.quota-1 ELSE -1 END) as pnl "
        "FROM predictions p "
        "LEFT JOIN matches m ON p.match_id = m.id "
        "WHERE p.settled_at >= ? AND p.settled_at <= ? "
        "  AND p.esito_finale IS NOT NULL "
        "GROUP BY m.league "
        "HAVING n >= 3 "
        "ORDER BY pnl DESC",
        (start, end + "T23:59:59")).fetchall()

    out = {}
    for league, n, won, pnl in rows:
        n = int(n)
        won = _safe_float(won, 0)
        pnl = _safe_float(pnl)
        out[league or "Sconosciuta"] = {
            "n": n,
            "won": int(won),
            "hit_rate": round(won / n * 100, 1) if n > 0 else 0,
            "pnl": round(pnl, 2),
            "roi": round(pnl / n * 100, 2) if n > 0 else 0,
        }
    return out


def _edge_analysis(conn, start: str, end: str) -> Dict:
    """Analisi edge vs mercato."""
    # Le soglie dei bucket sono i valori REALI della strategia (non numeri
    # copiati): se la soglia di edge cambia, il report segue senza restare
    # indietro (lezione del 13/09 sui testi operativi stantii).
    c = conn.cursor()
    rows = c.execute(
        "SELECT "
        "  SUM(CASE WHEN market_edge >= ? THEN 1 ELSE 0 END) as strong, "
        "  SUM(CASE WHEN market_edge >= ? AND market_edge < ? THEN 1 ELSE 0 END) as value, "
        "  SUM(CASE WHEN market_edge < ? THEN 1 ELSE 0 END) as weak, "
        "  AVG(market_edge) as avg_edge "
        "FROM predictions "
        "WHERE settled_at >= ? AND settled_at <= ? "
        "  AND esito_finale IS NOT NULL",
        (MARKET_EDGE_STRONG, MARKET_EDGE_MODERATE, MARKET_EDGE_STRONG,
         MARKET_EDGE_MODERATE, start, end + "T23:59:59")).fetchone()

    strong, value, weak, avg_edge = (rows or (0, 0, 0, 0))
    return {
        "strong_value_n": int(_safe_float(strong)),
        "value_n": int(_safe_float(value)),
        "weak_n": int(_safe_float(weak)),
        "avg_edge_pp": round(_safe_float(avg_edge) * 100, 2),
    }


# --- CLI ---

def _fmt_pct(v: float) -> str:
    return f"{v:+.1f}%" if v != 0 else "0.0%"


def _report(res: Dict) -> str:
    """Genera report testuale formattato."""
    p = res["predictions"]
    clv = res["clv"]
    bets = res["bets"]
    br = res["bankroll"]
    streaks = res["streaks"]
    edge = res["edge"]

    lines = [
        "📊 *REPORT PERFORMANCE*",
        f"📅 Periodo: {res['period']['start']} → {res['period']['end']}",
        "━" * 40,
        "",
        "🎯 *PREVISIONI*",
        f"   Totale: {p['total']} (V {p['won']} / P {p['lost']} / Push {p['push']})",
        f"   Hit rate: {p['hit_rate']:.1f}%",
        f"   ROI: {_fmt_pct(p['roi'])}",
        f"   EV atteso: {_fmt_pct(p['avg_ev'])}",
        f"   Calibration gap: {p['calibration_gap']:+.1f}pp",
        "",
    ]

    # Per mercato
    if p["by_market"]:
        lines.append("📊 *PER MERCATO*")
        for mkt, data in sorted(p["by_market"].items(),
                                 key=lambda x: x[1]["roi"], reverse=True):
            mark = "🔥" if data["roi"] > 0 else "❄️"
            lines.append(
                f"   {mark} {mkt}: n={data['n']} ROI {_fmt_pct(data['roi'])} "
                f"(edge {data['avg_edge']:+.1f}pp)")
        lines.append("")

    # CLV
    if clv["n"] > 0:
        pend = (f" (+{clv['pending']} in attesa di chiusura)"
                if clv.get("pending") else "")
        lines.extend([
            "📈 *CLV*",
            f"   Raw: {_fmt_pct(clv['avg_raw'])} (n {clv['n']}{pend})",
            f"   Vig-free: {_fmt_pct(clv['avg_vf'])}",
            f"   Vs Pinnacle: {_fmt_pct(clv['avg_vs_pinnacle'])}",
            f"   Positivo: {clv['positive_pct']:.0f}%",
            "",
        ])

    # Puntate auto
    if bets["n"] > 0:
        lines.extend([
            "🎯 *PUNTATE AUTOMATICHE*",
            f"   Totale: {bets['n']} (V {bets.get('won', 0)} / P {bets.get('lost', 0)})",
            f"   Stake totale: €{bets['total_stake']:.2f}",
            f"   P/L: €{bets['pnl']:+.2f} (ROI {bets['roi']:+.1f}%)",
            (f"   Aperte: {bets['open_n']} (€{bets['open_stake']:.2f} impegnati)"
             if bets.get("open_n") else "   Aperte: 0"),
            "",
        ])
        # Per mercato: il totale da solo non dice DOVE si perde (27/10/2026).
        for mkt, data in sorted(bets.get("by_market", {}).items(),
                                key=lambda x: x[1]["roi"], reverse=True):
            mark = "🔥" if data["roi"] > 0 else "❄️"
            lines.append(f"   {mark} {mkt}: n={data['n']} "
                         f"ROI {_fmt_pct(data['roi'])}")
        lines.append("")

    # Bankroll
    lines.extend([
        "💰 *BANKROLL*",
        f"   Attuale: €{br['current']:.2f}",
        f"   Peak: €{br['peak']:.2f}",
        f"   Drawdown: {br['drawdown_pct']:.1f}% {br['risk_level']}",
        "",
    ])

    # Streaks
    if streaks["current_streak"] > 0:
        emoji = "✅" if streaks["current_type"] == "won" else "❌"
        lines.extend([
            "🔥 *STREAK*",
            f"   Corrente: {streaks['current_streak']} {emoji} {streaks['current_type']}",
            f"   Max win: {streaks['max_win_streak']} | Max loss: {streaks['max_loss_streak']}",
            "",
        ])

    # Edge
    total_edge = edge["strong_value_n"] + edge["value_n"] + edge["weak_n"]
    if total_edge > 0:
        lines.extend([
            "🎯 *EDGE VS MERCATO*",
            f"   Strong value (+{MARKET_EDGE_STRONG*100:.0f}pp): {edge['strong_value_n']}",
            f"   Value (+{MARKET_EDGE_MODERATE*100:.0f}pp): {edge['value_n']}",
            f"   Weak (<{MARKET_EDGE_MODERATE*100:.0f}pp): {edge['weak_n']}",
            f"   Edge medio: {edge['avg_edge_pp']:+.1f}pp",
        ])

    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Report performance")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--period", type=str, default=None,
                    help="Data inizio YYYY-MM-DD")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    start = args.period or None
    res = get_performance(period_start=start, days=args.days)

    if args.json:
        print(json.dumps(res, indent=2, ensure_ascii=False))
    else:
        print(_report(res))
    return 0


if __name__ == "__main__":
    sys.exit(main())
