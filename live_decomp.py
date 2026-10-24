"""Scomposizione del P/L delle puntate LIVE chiuse (sola lettura, zero rete).

    venv/bin/python live_decomp.py [--db PATH] [--json]

Perche' esiste: un ROI aggregato dice "quanto" si perde, non "dove". La
scomposizione per CORSIA (tennis / eSports / calcio 1X2 / OU / AH / football
USA), FASCIA QUOTA, LEGA ed ERA dice quale leva ha perso e quale ha reso, con
lo stesso denominatore (lo stake) in ogni riga: senza questo, "il tennis perde"
e "le quote alte perdono" raccontano la stessa storia due volte.

Regole dichiarate (non nascoste nei numeri):
  * si contano SOLO le puntate `mode='live'` CHIUSE (`esito_finale` noto):
    quelle aperte non hanno ancora un verdetto e un push (P/L 0) e' un esito
    reale, non un dato mancante;
  * `profit` e' il denaro reale del ledger (valuta), quindi `roi` e'
    `sum(profit) / sum(stake)` — niente medie di medie;
  * la CORSIA si ricava dal `match_id` + `mercato` + `league` (le corsie del
    progetto scrivono prefissi diversi), con fallback dichiarato `altro`;
  * le fasce di quota sono quelle della strategia (1,20-1,50 / 1,50-1,80 /
    1,80-2,00 / 2,00-2,30 / 2,30-2,50 / >2,50) piu' un residuo `<1,20`;
  * l'ERA e' la data di apertura della puntata (`created_at`): separa le
    strategie (pre-11/09, pre-14/10 "bot aggressivo", post-21/10 "cap 1 USDC").

Sola lettura (`mode=ro`), offline, fail-safe: un DB illeggibile e' un blocco
con `error`, mai un traceback.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

DB_DEFAULT = Path(__file__).resolve().parent / "data" / "quotaverace.db"

# Fasce di quota della strategia (bordi: [min, max) tranne l'ultima).
ODDS_BANDS = ((0.0, 1.20), (1.20, 1.50), (1.50, 1.80), (1.80, 2.00),
              (2.00, 2.30), (2.30, 2.50), (2.50, float("inf")))

# Soglie d'era (scelte perche' sono CAMBI di strategia misurati, non date a caso).
ERAS = (
    ("< 11/09 quota 1.30-1.80", "2026-09-11T00:00:00"),
    ("11/09-13/10 favoriti netti", "2026-10-14T00:00:00"),
    ("14/10-20/10 bot aggressivo", "2026-10-21T00:00:00"),
    ("dal 21/10 cap 1,00 USDC", "9999-12-31T00:00:00"),
)

GRIDIRON_LEAGUES = ("NFL", "NCAAF", "CFL", "NFL OT", "NCAAF OT")


def _connect(db: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def _rows(conn: sqlite3.Connection) -> list[dict]:
    cur = conn.execute(
        "SELECT id, match_id, mercato, esito, price, stake, profit, "
        "esito_finale, created_at, settled_at, league FROM bets "
        "WHERE mode = 'live' AND esito_finale IS NOT NULL "
        "AND esito_finale <> '' ORDER BY id"
    )
    out = []
    for r in cur.fetchall():
        out.append({
            "id": r[0], "match_id": r[1] or "", "mercato": (r[2] or "").upper(),
            "esito": r[3] or "", "price": float(r[4] or 0.0),
            "stake": float(r[5] or 0.0), "profit": float(r[6] or 0.0),
            "esito_finale": r[7] or "", "created_at": r[8] or "",
            "settled_at": r[9] or "", "league": r[10] or "",
        })
    return out


def lane_of(row: dict) -> str:
    """CORSIA della puntata (dichiarata, non indovinata a meta').

    L'ordine dei controlli conta: il tennis e gli eSports hanno prefissi
    propri, il football USA si riconosce dalla lega (NFL/NCAAF/CFL) e il
    resto e' calcio, distinto per MERCATO (1X2/OU/AH).
    """
    mid = row["match_id"].lower()
    league = (row["league"] or "").strip()
    mercato = row["mercato"]
    if mid.startswith("sx-tennis") or mercato == "TENNIS":
        return "TENNIS"
    if mid.startswith("sx-esports") or mercato == "ML":
        return "ESPORTS"
    if league.upper() in GRIDIRON_LEAGUES:
        return "FOOTBALL USA"
    if mercato == "OU":
        return "CALCIO OU"
    if mercato == "AH":
        return "CALCIO AH"
    if mercato == "1X2":
        return "CALCIO 1X2"
    return "ALTRO"


def band_of(price: float) -> str:
    for lo, hi in ODDS_BANDS:
        if lo <= price < hi:
            if hi == ODDS_BANDS[-1][1]:
                return ">= 2,50"
            if lo == 0.0:
                return "< 1,20"
            return "%.2f-%.2f" % (lo, hi)
    return "n/d"


ERA_MISSING = "(data assente)"


def era_of(created_at: str) -> str:
    """Era di appartenenza dalla data di APERTURA (`created_at`).

    Una data assente o illeggibile NON finisce in un'era: confrontata come
    stringa vuota sarebbe `< 11/09` e una riga senza data verrebbe attribuita
    alla strategia piu' vecchia — cioe' la scomposizione mentirebbe proprio
    sulla riga che non si sa datare. Ha un bucket suo, dichiarato.
    """
    ts = (created_at or "").strip().replace(" ", "T")
    if len(ts) < 10 or ts[:4].isdigit() is False:
        return ERA_MISSING
    for label, until in ERAS:
        if ts < until:
            return label
    return ERAS[-1][0]


def _bucket(rows: list[dict]) -> dict:
    stake = sum(r["stake"] for r in rows)
    profit = sum(r["profit"] for r in rows)
    won = sum(1 for r in rows if r["esito_finale"] in ("won", "vinta"))
    lost = sum(1 for r in rows if r["esito_finale"] in ("lost", "persa"))
    push = sum(1 for r in rows if r["esito_finale"] == "push")
    return {
        "n": len(rows), "won": won, "lost": lost, "push": push,
        "stake": round(stake, 2), "profit": round(profit, 2),
        "roi_pct": round(100.0 * profit / stake, 2) if stake else None,
        "avg_odds": round(sum(r["price"] for r in rows) / len(rows), 3) if rows else None,
        "hit_pct": round(100.0 * won / (won + lost), 1) if (won + lost) else None,
    }


def _group(rows, key) -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        groups[key(r)].append(r)
    out = []
    for name, items in groups.items():
        out.append({"label": name, **_bucket(items)})
    out.sort(key=lambda x: (x["profit"], x["label"]))
    return out


def analyze(db: Path) -> dict:
    if not Path(db).exists():
        return {"ok": False, "error": "db assente: %s" % db}
    try:
        conn = _connect(db)
        rows = _rows(conn)
        conn.close()
    except Exception as e:                                   # pragma: no cover
        return {"ok": False, "error": "lettura fallita: %s" % e}
    for r in rows:
        r["lane"] = lane_of(r)
        r["band"] = band_of(r["price"])
        r["era"] = era_of(r["created_at"])
    total = _bucket(rows)
    return {
        "ok": True,
        "db": str(db),
        "now": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "total": total,
        "by_lane": _group(rows, lambda r: r["lane"]),
        "by_band": sorted(_group(rows, lambda r: r["band"]),
                          key=lambda x: (x["avg_odds"] or 0)),
        "by_league": [b for b in _group(rows, lambda r: r["league"] or "(lega assente)")
                      if b["n"] >= 2],
        "by_era": sorted(_group(rows, lambda r: r["era"]),
                         key=lambda x: x["label"]),
        "cross_lane_band": _cross(rows),
    }


def _cross(rows: list[dict]) -> list[dict]:
    """Corsia x fascia: dove si concentrano le perdite (n >= 3)."""
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rows:
        groups[(r["lane"], r["band"])].append(r)
    out = []
    for (lane, band), items in groups.items():
        if len(items) < 3:
            continue
        out.append({"lane": lane, "band": band, **_bucket(items)})
    out.sort(key=lambda x: x["profit"])
    return out


def format_report(data: dict) -> str:
    if not data.get("ok"):
        return "Scomposizione P/L non disponibile — %s" % data.get("error")
    t = data["total"]
    lines = [
        "📊 PUNTATE LIVE CHIUSE — scomposizione del P/L",
        "campione: %d chiuse (%dV/%dP/%dpush) | stake %.2f | P/L %+.2f | "
        "ROI %+.2f%% | quota media %.2f"
        % (t["n"], t["won"], t["lost"], t["push"], t["stake"], t["profit"],
           t["roi_pct"] or 0.0, t["avg_odds"] or 0.0),
        "",
        "PER CORSIA",
    ]
    for b in data["by_lane"]:
        lines.append(_line(b["label"], b))
    lines += ["", "PER FASCIA QUOTA"]
    for b in data["by_band"]:
        lines.append(_line(b["label"], b))
    lines += ["", "PER LEGA (n >= 2)"]
    for b in data["by_league"]:
        lines.append(_line(b["label"], b))
    lines += ["", "PER ERA (data di apertura)"]
    for b in data["by_era"]:
        lines.append(_line(b["label"], b))
    if data.get("cross_lane_band"):
        lines += ["", "CORSIA x FASCIA (n >= 3)"]
        for b in data["cross_lane_band"]:
            lines.append(_line("%s · %s" % (b["lane"], b["band"]), b))
    return "\n".join(lines)


def _line(label: str, b: dict) -> str:
    return ("· %-28s n=%2d stake %7.2f P/L %+7.2f ROI %+7.2f%% (hit %s)"
            % (label[:28], b["n"], b["stake"], b["profit"],
               b["roi_pct"] if b["roi_pct"] is not None else 0.0,
               ("%.0f%%" % b["hit_pct"]) if b["hit_pct"] is not None else "-"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Scomposizione P/L live (sola lettura)")
    ap.add_argument("--db", default=str(DB_DEFAULT))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    data = analyze(Path(args.db))
    print(json.dumps(data, indent=2, ensure_ascii=False) if args.json
          else format_report(data))
    return 0 if data.get("ok") else 1


if __name__ == "__main__":                               # pragma: no cover
    raise SystemExit(main())
