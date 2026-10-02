"""line_movement.py — Tracciamento movimenti di linea e RLM detection.

Ogni volta che _analyze_match rileva le quote di un match, registra uno
snapshot nella tabella price_snapshots. Quando lo snapshot successivo
mostra un movimento significativo (> soglia) nella DIREZIONE OPPOSTA
alla prevalenza del pubblico, si tratta di Reverse Line Movement (RLM):
segnale di money sharp.

Steam move = movimento improvviso e grande (> STEAM_THRESHOLD) in pochi
minuti, tipico di piu' sharps che entrano contemporaneamente.

CLI:
  venv/bin/python line_movement.py              # analisi movimenti recenti
  venv/bin/python line_movement.py --json       # output JSON
  venv/bin/python line_movement.py --match ID   # storico un match
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

# --- Soglie ---
RLM_MIN_SAMPLES = 3          # almeno 3 snapshot per rilevare un trend
RLM_MOVE_THRESHOLD = 0.03    # movimento minimo 3% (quote decimali)
STEAM_THRESHOLD = 0.06       # movimento > 6% in < 30 min = steam
STEAM_WINDOW_MINUTES = 30    # finestra temporale per steam move
SIGNIFICANT_MOVE_PCT = 2.0   # movimento significativo in percentuale


def _ensure_table(conn):
    """Crea la tabella price_snapshots se non esiste (idempotente)."""
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS price_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        match_id TEXT NOT NULL,
        esito TEXT NOT NULL,
        price REAL NOT NULL,
        bookmaker TEXT,
        market_prob REAL,
        recorded_at TEXT NOT NULL
    )''')
    # Indice per query veloci
    c.execute('''CREATE INDEX IF NOT EXISTS idx_snapshots_match
                 ON price_snapshots(match_id, esito, recorded_at)''')


def record_snapshot(match_id: str, esito: str, price: float,
                    bookmaker: str = "", market_prob: float = None,
                    conn=None, recorded_at: str = None) -> None:
    """Registra uno snapshot di prezzo per un match+esito.

    Chiamata ad ogni analisi di _analyze_match per costruire lo storico
    dei movimenti di linea. `bookmaker` distingue la FONTE del prezzo (es.
    'pinnacle' per lo sharp, vuoto per il prezzo SX del segnale): lo storico
    di una fonte non deve inquinare il movimento di un'altra.
    `recorded_at` e' iniettabile per i test (default: adesso).
    """
    own_conn = conn is None
    if own_conn:
        from tracker import _get_conn
        conn = _get_conn()
    try:
        _ensure_table(conn)
        now = recorded_at or datetime.now().isoformat()
        conn.execute(
            "INSERT INTO price_snapshots (match_id, esito, price, "
            "bookmaker, market_prob, recorded_at) VALUES (?,?,?,?,?,?)",
            (match_id, esito, price, bookmaker or "", market_prob, now))
        conn.commit()
    finally:
        if own_conn:
            conn.close()


def get_snapshots(match_id: str, esito: str = None,
                  since_minutes: int = None,
                  bookmaker: str = None) -> List[Dict]:
    """Recupera gli snapshot per un match (e opzionalmente un esito).

    Se since_minutes e' specificato, filtra solo gli ultimi N minuti.
    Se bookmaker e' specificato, filtra la FONTE del prezzo (es. 'pinnacle'):
    lo storico di una fonte non deve inquinare il movimento di un'altra.
    """
    from tracker import _get_conn
    conn = _get_conn()
    try:
        _ensure_table(conn)
        _bk = " AND bookmaker=?" if bookmaker is not None else ""
        if esito:
            base = ("SELECT price, bookmaker, market_prob, recorded_at "
                    "FROM price_snapshots WHERE match_id=? AND esito=?" + _bk)
            params: list = [match_id, esito]
            if bookmaker is not None:
                params.append(bookmaker)
            if since_minutes:
                cutoff = (datetime.now() - timedelta(minutes=since_minutes)
                          ).isoformat()
                base += " AND recorded_at >= ?"
                params.append(cutoff)
            rows = conn.execute(base + " ORDER BY recorded_at",
                                tuple(params)).fetchall()
        else:
            base = ("SELECT match_id, esito, price, bookmaker, market_prob, "
                    "recorded_at FROM price_snapshots WHERE match_id=?" + _bk)
            params = [match_id]
            if bookmaker is not None:
                params.append(bookmaker)
            if since_minutes:
                cutoff = (datetime.now() - timedelta(minutes=since_minutes)
                          ).isoformat()
                base += " AND recorded_at >= ?"
                params.append(cutoff)
            rows = conn.execute(base + " ORDER BY recorded_at",
                                tuple(params)).fetchall()
        return [dict(zip(
            ["match_id", "esito", "price", "bookmaker", "market_prob",
             "recorded_at"] if not esito else
            ["price", "bookmaker", "market_prob", "recorded_at"],
            r)) for r in rows]
    finally:
        conn.close()


def detect_rlm(match_id: str, esito: str,
               public_bias: float = None) -> Optional[Dict]:
    """Detect Reverse Line Movement per un match+esito.

    RLM = il prezzo si muove nella direzione OPPOSTA alla prevalenza
    del pubblico. Esempio: pubblico al 70% su Home, ma quota Home SALE
    → i sharps stanno scommettendo contro il pubblico.

    Args:
        match_id: ID del match
        esito: esito da monitorare (es. "1", "X", "2", "Over 2.5")
        public_bias: frazione di scommesse sul lato pubblico (0-1).
                     Se None, usa il movimento di prezzo come proxy.

    Returns:
        Dict con info RLM o None se nessun RLM rilevato.
    """
    snapshots = get_snapshots(match_id, esito)
    if len(snapshots) < RLM_MIN_SAMPLES:
        return None

    first_price = snapshots[0]["price"]
    last_price = snapshots[-1]["price"]
    if first_price <= 0:
        return None

    # Movimento totale: positivo = prezzo sale (mercato si allontana)
    total_move = (last_price / first_price) - 1.0

    # Dati temporali
    first_time = datetime.fromisoformat(snapshots[0]["recorded_at"])
    last_time = datetime.fromisoformat(snapshots[-1]["recorded_at"])
    span_minutes = (last_time - first_time).total_seconds() / 60.0

    # Conta movimenti nella direzione opposta
    reverse_moves = 0
    for i in range(1, len(snapshots)):
        prev = snapshots[i - 1]["price"]
        curr = snapshots[i]["price"]
        if prev <= 0:
            continue
        step = (curr / prev) - 1.0
        # Se il prezzo sale (mercato si allontana dal lato pubblico),
        # è movimento reverse
        if public_bias is not None and public_bias > 0.5:
            # Pubblico favorevole (bias > 50%) ma prezzo sale → reverse
            if step > RLM_MOVE_THRESHOLD:
                reverse_moves += 1
        elif public_bias is not None and public_bias < 0.5:
            # Pubblico sfavorevole (bias < 50%) ma prezzo scende → reverse
            if step < -RLM_MOVE_THRESHOLD:
                reverse_moves += 1
        else:
            # Senza public bias, usa la direzione del prezzo come proxy:
            # movimento grande in entrambe le direzioni è informativo
            if abs(step) > RLM_MOVE_THRESHOLD:
                reverse_moves += 1

    # RLM significativo: movimento totale > soglia E almeno 2 step reversi
    is_rlm = (abs(total_move) > RLM_MOVE_THRESHOLD
              and reverse_moves >= 2
              and span_minutes > 5)

    if not is_rlm:
        return None

    # Intensità: normalizzata rispetto al tempo
    intensity = abs(total_move) / max(span_minutes / 60.0, 0.1)

    return {
        "match_id": match_id,
        "esito": esito,
        "first_price": round(first_price, 3),
        "last_price": round(last_price, 3),
        "total_move_pct": round(total_move * 100, 2),
        "reverse_moves": reverse_moves,
        "span_minutes": round(span_minutes, 1),
        "intensity": round(intensity, 3),
        "public_bias": public_bias,
        "direction": "up" if total_move > 0 else "down",
    }


def detect_steam(match_id: str, esito: str) -> Optional[Dict]:
    """Detect Steam Move: movimento improvviso e grande in pochi minuti.

    Steam = piu' sharps entrano contemporaneamente, spostando il mercato
    rapidamente. Tipico nei 30 minuti prima del kickoff.

    Returns:
        Dict con info steam o None.
    """
    snapshots = get_snapshots(match_id, esito,
                              since_minutes=STEAM_WINDOW_MINUTES)
    if len(snapshots) < 2:
        return None

    first_price = snapshots[0]["price"]
    last_price = snapshots[-1]["price"]
    if first_price <= 0:
        return None

    move = (last_price / first_price) - 1.0
    first_time = datetime.fromisoformat(snapshots[0]["recorded_at"])
    last_time = datetime.fromisoformat(snapshots[-1]["recorded_at"])
    span = (last_time - first_time).total_seconds() / 60.0

    if abs(move) < STEAM_THRESHOLD or span < 1:
        return None

    return {
        "match_id": match_id,
        "esito": esito,
        "first_price": round(first_price, 3),
        "last_price": round(last_price, 3),
        "move_pct": round(move * 100, 2),
        "span_minutes": round(span, 1),
        "direction": "steam_down" if move < 0 else "steam_up",
    }


def analyze_movements(match_id: str,
                      public_biases: Dict[str, float] = None) -> Dict:
    """Analisi completa dei movimenti di linea per un match.

    Returns:
        Dict con rlm_detected, steam_detected, e dettagli per esito.
    """
    from tracker import _get_conn
    conn = _get_conn()
    try:
        _ensure_table(conn)
        rows = conn.execute(
            "SELECT DISTINCT esito FROM price_snapshots WHERE match_id=?",
            (match_id,)).fetchall()
    finally:
        conn.close()

    results = {}
    rlm_list = []
    steam_list = []

    for (esito,) in rows:
        bias = (public_biases or {}).get(esito)
        rlm = detect_rlm(match_id, esito, public_bias=bias)
        steam = detect_steam(match_id, esito)
        results[esito] = {"rlm": rlm, "steam": steam}
        if rlm:
            rlm_list.append(rlm)
        if steam:
            steam_list.append(steam)

    return {
        "match_id": match_id,
        "esiti": results,
        "rlm_detected": len(rlm_list) > 0,
        "steam_detected": len(steam_list) > 0,
        "rlm_count": len(rlm_list),
        "steam_count": len(steam_list),
        "rlm_details": rlm_list,
        "steam_details": steam_list,
    }


def recent_signals_with_movement(since_minutes: int = 120,
                                 min_snapshots: int = 3) -> List[Dict]:
    """Segnali value/strong_value con movimenti di linea rilevanti.

    Utile per il report: mostra quali segnali hanno RLM/steam.
    """
    from tracker import _get_conn
    conn = _get_conn()
    try:
        _ensure_table(conn)
        cutoff = (datetime.now() - timedelta(minutes=since_minutes)
                  ).isoformat()
        # Match con analisi recente
        rows = conn.execute(
            "SELECT a.match_id, a.best_esito, a.best_quota, a.status, "
            "m.home_team, m.away_team, m.league "
            "FROM match_analysis a "
            "JOIN matches m ON m.id = a.match_id "
            "WHERE a.status IN ('value', 'strong_value') "
            "AND a.timestamp >= ? "
            "ORDER BY a.best_ev DESC LIMIT 20",
            (cutoff,)).fetchall()
    finally:
        conn.close()

    signals = []
    for mid, esito, quota, status, home, away, league in rows:
        snaps = get_snapshots(mid, esito, since_minutes=since_minutes)
        if len(snaps) < min_snapshots:
            continue
        first_p = snaps[0]["price"]
        last_p = snaps[-1]["price"]
        move = ((last_p / first_p) - 1.0) * 100 if first_p > 0 else 0
        signals.append({
            "match_id": mid,
            "evento": f"{home} vs {away}",
            "league": league,
            "esito": esito,
            "quota": quota,
            "status": status,
            "price_move_pct": round(move, 2),
            "n_snapshots": len(snaps),
        })
    return signals


# --- CLI ---

def _fmt_move(v: float) -> str:
    if v > 0:
        return f"↗ +{v:.1f}%"
    elif v < 0:
        return f"↙ {v:.1f}%"
    return "→ 0.0%"


def _report_recent(since_minutes: int = 120) -> str:
    sigs = recent_signals_with_movement(since_minutes=since_minutes)
    lines = [f"📊 Movimenti di linea (ultimi {since_minutes} min)"]
    lines.append("━" * 50)
    if not sigs:
        lines.append("Nessun segnale con movimenti rilevanti.")
        return "\n".join(lines)
    for s in sigs:
        move = _fmt_move(s["price_move_pct"])
        lines.append(
            f"  {s['league']} – {s['evento']}\n"
            f"    {s['esito']} @ {s['quota']:.2f} ({s['status']}) "
            f"| {move} ({s['n_snapshots']} snapshots)")
    return "\n".join(lines)


def _report_match(match_id: str) -> str:
    analysis = analyze_movements(match_id)
    lines = [f"📊 Movimenti per match {match_id}"]
    lines.append("━" * 50)
    for esito, data in analysis["esiti"].items():
        rlm = data["rlm"]
        steam = data["steam"]
        if rlm:
            lines.append(
                f"  ⚠️  RLM su {esito}: "
                f"{rlm['first_price']:.2f} → {rlm['last_price']:.2f} "
                f"({_fmt_move(rlm['total_move_pct'])}) "
                f"in {rlm['span_minutes']:.0f} min "
                f"({rlm['reverse_moves']} step reversi)")
        if steam:
            lines.append(
                f"  🔥 STEAM su {esito}: "
                f"{steam['first_price']:.2f} → {steam['last_price']:.2f} "
                f"({_fmt_move(steam['move_pct'])}) "
                f"in {steam['span_minutes']:.0f} min")
        if not rlm and not steam:
            lines.append(f"  {esito}: nessun movimento rilevante")
    if not analysis["rlm_detected"] and not analysis["steam_detected"]:
        lines.append("\n✅ Nessun RLM o steam rilevato.")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Line movement tracking e RLM detection")
    ap.add_argument("--json", action="store_true",
                    help="output JSON")
    ap.add_argument("--match", type=str, default=None,
                    help="analisi di un singolo match")
    ap.add_argument("--minutes", type=int, default=120,
                    help="finestra temporale in minuti (default 120)")
    args = ap.parse_args(argv)

    if args.match:
        res = analyze_movements(args.match)
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            print(_report_match(args.match))
    else:
        sigs = recent_signals_with_movement(since_minutes=args.minutes)
        if args.json:
            print(json.dumps(sigs, ensure_ascii=False, indent=2))
        else:
            print(_report_recent(args.minutes))
    return 0


if __name__ == "__main__":
    sys.exit(main())
