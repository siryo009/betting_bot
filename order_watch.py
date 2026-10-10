"""Verifica degli ordini REALI: stake fisso e recinto di esposizione (28/09/2026).

PERCHE' ESISTE. Dal 28/09/2026 valgono due parametri di risk management:
**stake fisso 1.50 USDC** per ogni singolo ordine reale e **recinto del 40%**
sull'esposizione APERTA (a equity 33.55 il tetto e' 13.42 e gli ordini da 1.50
che ci stanno dentro sono 8). Sono due invarianti sul DENARO: verificarli a
mano una volta non serve a niente, perche' un ordine che li viola puo' arrivare
in qualsiasi momento e nessuno se ne accorgerebbe fino al drawdown.

Questo modulo e' il controllo che si RIPETE: ricostruisce dal ledger ogni
ordine reale e verifica gli invarianti, dichiarando sempre cosa NON ha potuto
verificare (un tetto stimato non e' un tetto verificato).

COSA CONTROLLA
1. **Stake esatto** — ogni riga `mode='live'` creata dalla data della direttiva
   deve avere `stake == order_stake()` (il valore viene da `auto_bet`, mai
   copiato). Le righe PRECEDENTI alla direttiva sono dichiarate
   `stake_predirective`, non giudicate: applicare la regola di oggi al passato
   sarebbe un falso positivo.
2. **Recinto del 40%** — replay cronologico delle aperture/chiusure: in ogni
   istante in cui un ordine si apre, la somma degli stake aperti deve stare
   dentro `equity x OPEN_EXPOSURE_CAP_PCT`. L'equity dell'istante viene dallo
   storico campionato (`bankroll_history.json`); senza campione il tetto e'
   STIMATO con l'equity corrente e la cosa viene dichiarata (`cap_estimated`),
   mai spacciata per verificata.
3. **Ordine senza id** — un `FULLY_FILLED` senza `bet_id` non e' un ordine
   dell'exchange (guardia del 26/09): riga a ledger senza id = violazione.

GARANZIE: sola LETTURA (connessione `mode=ro`), nessuna rete, nessun ordine,
nessun credito. Nessuna soglia duplicata: stake e cap arrivano da `auto_bet`.

CLI:
    venv/bin/python order_watch.py [--json] [--db PATH] [--since YYYY-MM-DD]
    venv/bin/python order_watch.py --wait 15 --interval 60   # sorveglia
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: Data della direttiva: le righe precedenti non vengono giudicate con la
#: regola di oggi (sarebbe un falso positivo su una strategia diversa).
DIRECTIVE_SINCE = "2026-09-28"
#: Tolleranza sui confronti di denaro (evita falsi positivi da arrotondamenti).
TOL = 1e-6

VIOL_STAKE_MISMATCH = "stake_mismatch"
VIOL_STAKE_OVER_MAX = "stake_over_max"
VIOL_STAKE_UNDER_TICKET = "stake_under_ticket"
VIOL_EXPOSURE_OVER_CAP = "exposure_over_cap"
VIOL_MISSING_BET_ID = "missing_bet_id"

VIOLATION_LABEL = {
    VIOL_STAKE_MISMATCH: "stake diverso dal valore fisso",
    VIOL_STAKE_OVER_MAX: "stake oltre il tetto per-ordine",
    VIOL_STAKE_UNDER_TICKET: "stake sotto il ticket minimo del motore Kelly",
    VIOL_EXPOSURE_OVER_CAP: "esposizione aperta oltre il 40%",
    VIOL_MISSING_BET_ID: "ordine riempito senza id dell'exchange",
}


def _ab():
    """Import PIGRO di `auto_bet` (il modulo resta leggero all'import)."""
    import auto_bet
    return auto_bet


def _db_path(db_path: Optional[Any] = None) -> Path:
    if db_path:
        return Path(db_path)
    return Path(_ab().DATA_DIR) / "quotaverace.db"


def _connect_ro(db_path: Optional[Any] = None) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{_db_path(db_path).as_posix()}?mode=ro",
                           uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def expected_stake() -> float:
    """Lo stake atteso per un ordine reale (UNICA fonte: `auto_bet`)."""
    return float(_ab().fixed_order_stake())


def fixed_active() -> bool:
    """Lo stake fisso e' attivo? (con `ORDER_FIXED_STAKE_USDC=0` no)."""
    return bool(_ab().fixed_stake_active())


def order_ceiling_active() -> bool:
    """True se il tetto per-ordine e' ASSOLUTO (`ORDER_MAX_STAKE_USDC` > 0)
    invece che DINAMICO (12% del bankroll, direttiva 04/10/2026)."""
    try:
        return float(_ab().ORDER_MAX_STAKE_USDC) > 0
    except Exception:
        return False


def dynamic_order_cap(equity: Optional[float]) -> Optional[float]:
    """Tetto per-ordine dinamico all'equity data (None = non calcolabile)."""
    if equity is None:
        return None
    try:
        from decision.stake_engine import aggressive_cap_usdc
        cap = float(aggressive_cap_usdc(equity))
        return cap if cap > 0 else None
    except Exception:
        return None


def min_ticket() -> float:
    """Ticket minimo del motore Kelly (UNICA fonte: `auto_bet`/stake_engine)."""
    try:
        return float(_ab().aggressive_min_ticket())
    except Exception:
        return 0.0


def cap_pct() -> float:
    """Percentuale del recinto (UNICA fonte: `auto_bet`)."""
    return float(_ab().OPEN_EXPOSURE_CAP_PCT)


def current_equity() -> Optional[float]:
    """Equity corrente del wallet (None se non leggibile).

    E' l'equity RICONCILIATA, la STESSA base su cui il bot dimensiona gli
    ordini (`auto_bet.sizing_equity`): l'audit e il sizing devono misurare lo
    stesso capitale, altrimenti la verifica del cap per-ordine segnala
    violazioni su ordini che il motore ha dimensionato correttamente (e
    viceversa). La riconciliazione corregge la finestra "payout accreditato /
    settlement non ancora registrato", che gonfiava l'equity del 10/10/2026.
    """
    try:
        snap = _ab()._live_wallet_snapshot() or {}
        eq = snap.get("equity")
        if eq is None:
            return None
        return float(_ab().sizing_equity(eq))
    except Exception:
        return None


def bankroll_history(path: Optional[Any] = None) -> List[Tuple[datetime, float]]:
    """Campioni di equity `(ts, valore)` dallo storico rolling (fail-safe)."""
    try:
        p = Path(path) if path else Path(_ab().BANKROLL_HISTORY_FILE)
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    out: List[Tuple[datetime, float]] = []
    for item in (data.get("samples") if isinstance(data, dict) else None) or []:
        try:
            ts = datetime.fromisoformat(str(item[0]).replace("Z", "+00:00"))
            val = float(item[1])
        except Exception:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if val > 0:
            out.append((ts, val))
    out.sort(key=lambda x: x[0])
    return out


def _parse_ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _equity_at(ts: Optional[datetime], history: List[Tuple[datetime, float]],
               fallback: Optional[float]) -> Tuple[Optional[float], str]:
    """Equity all'istante `ts`: (valore, provenienza).

    Provenienza dichiarata perche' un tetto STIMATO non e' un tetto verificato:
    `sample` = campione reale dello storico, `nearest` = il piu' vicino
    disponibile (il campione nasce 1/ora), `now` = equity corrente,
    `unknown` = niente su cui misurare.
    """
    if ts is not None:
        prior = [v for t, v in history if t <= ts]
        if prior:
            return prior[-1], "sample"
        if history:
            return history[0][1], "nearest"
    if fallback:
        return float(fallback), "now"
    return None, "unknown"


def _rows(db_path: Optional[Any] = None, since: Optional[str] = None
          ) -> List[Dict[str, Any]]:
    """Righe del ledger `bets` (sola lettura). Mai un'eccezione."""
    try:
        conn = _connect_ro(db_path)
    except Exception as exc:
        logger.debug("order_watch: ledger non leggibile: %s", exc)
        return []
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, match_id, mercato, esito, price, stake, mode, status, "
            "bet_id, esito_finale, profit, created_at, settled_at "
            "FROM bets ORDER BY id").fetchall()]
    except Exception as exc:
        logger.debug("order_watch: lettura bets fallita: %s", exc)
        rows = []
    finally:
        conn.close()
    if since:
        rows = [r for r in rows
                if str(r.get("created_at") or "") >= str(since)]
    return rows


def audit(db_path: Optional[Any] = None, *, since: str = DIRECTIVE_SINCE,
          history_path: Optional[Any] = None,
          equity: Optional[float] = None) -> Dict[str, Any]:
    """Verifica gli invarianti degli ordini reali (sola lettura, fail-safe).

    Ritorna: `expected_stake`, `cap_pct`, `orders` (conteggi), `violations`,
    `declared` (cio' che non e' stato possibile verificare), `max_open` (il
    picco di esposizione ricostruito) e `verdict`.
    """
    expected = expected_stake()
    active = fixed_active()
    pct = cap_pct()
    # Il tetto per singolo ordine e' ASSOLUTO se `ORDER_MAX_STAKE_USDC` > 0;
    # altrimenti (default 04/10/2026) e' DINAMICO e si calcola sull'equity
    # dell'istante dell'ordine.
    try:
        max_order = float(_ab().ORDER_MAX_STAKE_USDC)
    except Exception:
        max_order = expected
    hist = bankroll_history(history_path)
    eq_now = equity if equity is not None else current_equity()

    all_rows = _rows(db_path)                      # senza filtro d'era
    live = [r for r in all_rows if str(r.get("mode") or "") == "live"]
    violations: List[Dict[str, Any]] = []
    declared: List[Dict[str, Any]] = []

    def _add(kind: str, row: Dict[str, Any], detail: str) -> None:
        violations.append({"id": row.get("id"), "kind": kind,
                           "label": VIOLATION_LABEL.get(kind, kind),
                           "detail": detail, "match_id": row.get("match_id"),
                           "esito": row.get("esito"),
                           "created_at": row.get("created_at")})

    # --- 1. stake esatto / entro il tetto, e id dell'exchange ------------
    # Il tetto per-ordine dal 04/10/2026 puo' essere DINAMICO (12% del
    # bankroll): senza un tetto assoluto il valore atteso dipende dall'equity
    # dell'ISTANTE dell'ordine, quindi lo si stima dallo storico campionato.
    ticket = min_ticket()
    for row in live:
        try:
            stake = float(row.get("stake") or 0.0)
        except (TypeError, ValueError):
            stake = 0.0
        pre = str(row.get("created_at") or "") < str(since)
        ceiling = float(max_order)
        if max_order <= 0:      # cap dinamico: tetto all'equity dell'ordine
            eq_row, src_row = _equity_at(_parse_ts(row.get("created_at")),
                                         hist, eq_now)
            dyn = dynamic_order_cap(eq_row)
            if dyn is None:
                declared.append({"id": row.get("id"),
                                 "kind": "order_cap_unverifiable",
                                 "detail": "tetto dinamico non calcolabile "
                                           "(equity ignota): non verificato",
                                 "created_at": row.get("created_at")})
                ceiling = None
            else:
                ceiling = dyn
                if src_row != "sample":
                    declared.append({"id": row.get("id"),
                                     "kind": "order_cap_estimated",
                                     "detail": f"tetto dinamico stimato con "
                                               f"equity {src_row}: {dyn:.2f}",
                                     "created_at": row.get("created_at")})
        if ceiling is not None and stake > ceiling + TOL:
            _add(VIOL_STAKE_OVER_MAX, row,
                 f"stake {stake:.4f} > tetto per-ordine {ceiling:.2f}")
        elif active and abs(stake - expected) > TOL:
            if pre:
                declared.append({"id": row.get("id"), "kind": "stake_predirective",
                                 "detail": f"creata prima del {since}: stake "
                                           f"{stake:.4f} (regola non applicabile)",
                                 "created_at": row.get("created_at")})
            else:
                _add(VIOL_STAKE_MISMATCH, row,
                     f"stake {stake:.4f} != valore fisso {expected:.2f}")
        elif (not active and not pre and ticket > 0
              and 0 < stake < ticket - TOL):
            # Motore Kelly: sotto il ticket minimo l'operazione non dovrebbe
            # esistere (sarebbe stata scartata). Un ordine sotto soglia e' un
            # percorso che ha aggirato il motore.
            _add(VIOL_STAKE_UNDER_TICKET, row,
                 f"stake {stake:.4f} < ticket minimo {ticket:.2f}")
        if (str(row.get("status") or "").upper() == "FULLY_FILLED"
                and not (row.get("bet_id") or "").strip()):
            _add(VIOL_MISSING_BET_ID, row, "FULLY_FILLED senza bet_id")

    # --- 2. recinto: replay cronologico dell'esposizione aperta ----------
    events: List[Tuple[datetime, float, Dict[str, Any], str]] = []
    for row in live:
        try:
            stake = float(row.get("stake") or 0.0)
        except (TypeError, ValueError):
            stake = 0.0
        opened = _parse_ts(row.get("created_at"))
        if opened is None:
            continue
        events.append((opened, stake, row, "open"))
        closed = _parse_ts(row.get("settled_at"))
        if closed is not None:
            events.append((closed, -stake, row, "close"))
    # A parita' di istante le chiusure precedono le aperture (altrimenti una
    # sostituzione nello stesso secondo conterebbe come sfondamento).
    events.sort(key=lambda e: (e[0], 0 if e[3] == "close" else 1, e[2].get("id") or 0))

    running = 0.0
    peak = {"stake": 0.0, "count": 0, "cap": None, "at": None, "source": None}
    open_ids: List[Any] = []
    for ts, delta, row, kind in events:
        if kind == "close":
            running = max(0.0, running + delta)
            if row.get("id") in open_ids:
                open_ids.remove(row.get("id"))
            continue
        running += delta
        open_ids.append(row.get("id"))
        eq_i, src = _equity_at(ts, hist, eq_now)
        if eq_i is None:
            declared.append({
                "id": row.get("id"), "kind": "cap_unverifiable",
                "detail": "nessun campione di equity per questo istante: tetto "
                          "non verificabile (non equivale a verificato)",
                "created_at": row.get("created_at")})
            continue
        cap_i = eq_i * pct
        if src != "sample":
            declared.append({
                "id": row.get("id"), "kind": "cap_estimated",
                "detail": f"tetto stimato con equity {src}: {cap_i:.2f}",
                "created_at": row.get("created_at")})
        if running > peak["stake"]:
            peak = {"stake": round(running, 4), "count": len(open_ids),
                    "cap": round(cap_i, 4), "at": ts.isoformat(), "source": src}
        if running > cap_i + TOL:
            _add(VIOL_EXPOSURE_OVER_CAP, row,
                 f"esposizione aperta {running:.4f} > tetto {cap_i:.4f} "
                 f"({pct * 100:.0f}% di {eq_i:.2f})")

    verdict = "ok" if not violations else "violazioni"
    return {
        "verdict": verdict,
        "since": since,
        "fixed_active": active,
        "expected_stake": expected,
        "max_order_stake": max_order,
        "cap_pct": pct,
        "orders": {
            "total": len(all_rows),
            "live": len(live),
            "live_dopo_direttiva": len([r for r in live
                                        if str(r.get("created_at") or "") >= str(since)]),
            "sim": len([r for r in all_rows
                        if str(r.get("mode") or "") == "sim"]),
            "aperte": len([r for r in live if r.get("esito_finale") is None]),
        },
        "esposizione_corrente": round(
            sum(float(r.get("stake") or 0) for r in live
                if r.get("esito_finale") is None), 4),
        "max_open": peak,
        "violations": violations,
        "declared": declared,
        "equity_now": eq_now,
    }


def format_report(data: Optional[Dict[str, Any]] = None) -> str:
    """Report leggibile (CLI/Telegram), mai un'eccezione."""
    d = data if isinstance(data, dict) else audit()
    o = d.get("orders") or {}
    if d.get("fixed_active"):
        stake_rule = f"stake fisso {d.get('expected_stake')} USDC"
    elif float(d.get('max_order_stake') or 0) > 0:
        stake_rule = f"Kelly aggressivo + tetto assoluto {d.get('max_order_stake')} USDC"
    else:
        stake_rule = ("Kelly aggressivo (cap dinamico 12% del bankroll, "
                      "ticket minimo del motore)")
    lines = ["🛡️  VERIFICA ORDINI REALI (stake + recinto 40%)",
             f"Direttiva dal {d.get('since')} | {stake_rule} | recinto "
             f"{(d.get('cap_pct') or 0) * 100:.0f}%"]
    lines.append(f"Ordini: {o.get('live', 0)} live "
                 f"({o.get('live_dopo_direttiva', 0)} dopo la direttiva) | "
                 f"{o.get('sim', 0)} sim | {o.get('aperte', 0)} aperti ora "
                 f"({d.get('esposizione_corrente', 0):.2f} USDC)")
    peak = d.get("max_open") or {}
    if peak.get("at"):
        lines.append(f"Picco esposizione ricostruito: {peak['stake']:.2f} USDC "
                     f"su {peak['count']} ordini aperti | tetto "
                     f"{peak['cap']} USDC (fonte equity: {peak['source']})")
    if d.get("verdict") == "ok":
        lines.append("✅ Nessuna violazione")
    else:
        lines.append(f"❌ {len(d.get('violations') or [])} violazione/i:")
        for v in d.get("violations") or []:
            lines.append(f"   • #{v.get('id')} {v.get('label')}: {v.get('detail')}")
    for item in d.get("declared") or []:
        lines.append(f"   ℹ️  #{item.get('id')} non giudicato ({item.get('kind')}): "
                     f"{item.get('detail')}")
    return "\n".join(lines)


def last_live_id(db_path: Optional[Any] = None) -> int:
    """Id dell'ultima riga `bets` (0 se il ledger non e' leggibile)."""
    try:
        conn = _connect_ro(db_path)
        try:
            row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM bets").fetchone()
            return int(row[0] or 0)
        finally:
            conn.close()
    except Exception:
        return 0


def watch(minutes: float = 15.0, interval: float = 60.0,
          db_path: Optional[Any] = None, *, since: str = DIRECTIVE_SINCE,
          on_new=None, sleep=time.sleep) -> Dict[str, Any]:
    """Sorveglia il ledger e verifica OGNI nuovo ordine reale appena compare.

    `on_new(rows_verificate, report)` viene chiamato ad ogni ordine nuovo (per
    la notifica); `sleep` e' iniettabile cosi' i test girano senza attese.
    Ritorna l'ultimo report piu' il conteggio dei nuovi ordini visti.
    """
    start_id = last_live_id(db_path)
    deadline = time.time() + max(0.0, minutes) * 60.0
    nuovi: List[Dict[str, Any]] = []
    report = audit(db_path, since=since)
    print(f"[order_watch] soglia id {start_id} — sorveglianza "
          f"{minutes:g} min (intervallo {interval:g}s)", flush=True)
    while True:
        # Un ordine nuovo puo' anche essere una riga non-live (un rifiuto T-60):
        # per non perderla si confronta l'id massimo, non il conteggio live.
        now_last = last_live_id(db_path)
        if now_last > start_id:
            fresh = [r for r in _rows(db_path) if (r.get("id") or 0) > start_id]
            start_id = now_last
            report = audit(db_path, since=since)
            nuovi.extend(fresh)
            for r in fresh:
                _verify_one(r, report)
                print(format_report({**report, "orders": {
                    **report.get("orders", {}), "total": len(fresh)}}),
                    flush=True)
            if on_new is not None:
                try:
                    on_new(fresh, report)
                except Exception as exc:              # pragma: no cover
                    logger.debug("order_watch: on_new fallito: %s", exc)
        if time.time() >= deadline:
            break
        sleep(max(1.0, float(interval)))
    return {**report, "new_orders": nuovi, "watched_seconds": (
        max(0.0, minutes) * 60.0)}


def _verify_one(row: Dict[str, Any], report: Dict[str, Any]) -> None:
    """Stampa il verdetto puntuale di una riga (log, mai eccezioni)."""
    try:
        stake = float(row.get("stake") or 0.0)
        vid = [v for v in report.get("violations") or [] if v.get("id") == row.get("id")]
        esito = "❌ " + "; ".join(v["label"] for v in vid) if vid else "✅ conforme"
        logger.info("order_watch: ordine #%s %s %s stake=%.4f mode=%s -> %s",
                    row.get("id"), row.get("match_id"), row.get("esito"),
                    stake, row.get("mode"), esito)
    except Exception:                                  # pragma: no cover
        pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Verifica degli ordini reali: stake fisso 1.50 e recinto 40%")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--db", default=None, help="ledger alternativo (sola lettura)")
    ap.add_argument("--since", default=DIRECTIVE_SINCE)
    ap.add_argument("--equity", type=float, default=None,
                    help="equity da usare se il wallet non e' leggibile")
    ap.add_argument("--wait", type=float, default=0.0,
                    help="sorveglia per N minuti e verifica ogni nuovo ordine")
    ap.add_argument("--interval", type=float, default=60.0)
    args = ap.parse_args(argv)

    if args.wait and args.wait > 0:
        res = watch(args.wait, args.interval, args.db, since=args.since)
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
        else:
            print()
            print(format_report(res))
            print(f"Nuovi ordini visti: {len(res.get('new_orders') or [])}")
        return 1 if (res.get("violations") or []) else 0

    data = audit(args.db, since=args.since, equity=args.equity)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    else:
        print(format_report(data))
    return 1 if (data.get("violations") or []) else 0


if __name__ == "__main__":                            # pragma: no cover
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    raise SystemExit(main())
