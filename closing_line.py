"""closing_line.py — Quota FINALE di Pinnacle a T-0 (direttiva 04/10/2026, punto 5).

Perche' esiste: il CLV dice se un ordine ha battuto il mercato, ma per
calcolarlo serve la **closing line** — il prezzo con cui lo sharp CHIUDE
l'evento, cioe' la stima piu' completa che il mercato produce (a T-0 arrivano
le formazioni ufficiali e i volumi dei sindacati). `fixture_engine` aggiorna
`clv_history` a ogni analisi, ma nessuno registrava SPECIFICAMENTE l'ultima
quota Pinnacle prima del fischio.

Questo modulo fa quella sola cosa, e la fa in sola LETTURA dalla cache quote
(`pinnacle_oracle`, **zero crediti**, nessuna rete, nessun ordine):

1. trova le righe APERTE con kickoff ormai imminente (finestra T-10..T+5);
2. legge la quota GREZZA di Pinnacle dall'ultima cache disponibile;
3. la scrive in `clv_history.closing_odds` (`tracker.save_clv`), senza
   sovrascrivere un campione gia' catturato;
4. `beat_pct` misura la percentuale di beat: `(quota_ordine / closing) - 1`,
   delegando a `market_calib.clv_raw` (la formula vive li', mai ricopiata).

Confini: non decide, non ordina, non tocca `predictions`/`bets`. Fail-safe
totale (un errore su una riga non ferma le altre) e fail-closed sui dati: senza
cache sharp fresca, senza esito canonico o su un mercato non testa-a-testa la
riga viene SALTATA con motivo machine-readable — mai un numero inventato.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "beat_pct", "candidates", "capture_closing_lines", "format_report",
    "post_minutes", "pre_minutes", "report", "sharp_closing",
]

#: Motivi machine-readable di un salto (mai prosa, mai un silenzio):
#: `no_sharp_cache` (sharp assente/stantio), `unsupported_market` (mercato a
#: linea: il prezzo non e' nell'h2h), `already_captured` (campione gia' preso),
#: `read_error`/`write_error` (lettura o scrittura fallita).
SKIP_REASONS = ("no_sharp_cache", "unsupported_market", "already_captured",
                "read_error", "write_error")

#: Finestra di cattura rispetto al kickoff (minuti): si parte `PRE` minuti
#: prima e si tollera fino a `POST` minuti dopo (la routine gira ogni pochi
#: minuti, quindi il fischio puo' cadere fra due giri). Env per taratura.
PRE_MIN_ENV = "CLOSING_LINE_PRE_MIN"
POST_MIN_ENV = "CLOSING_LINE_POST_MIN"
MAX_ROWS_ENV = "CLOSING_LINE_MAX_ROWS"
DEFAULT_PRE_MIN = 10.0
DEFAULT_POST_MIN = 5.0
DEFAULT_MAX_ROWS = 60

#: Mercati testa-a-testa: la cache sharp ha i 3 esiti 1X2 (calcio) o i 2 esiti
#: del tennis/eSports. Per OU/AH il prezzo a linea non e' nell'h2h: la riga
#: viene saltata con `unsupported_market` invece di scrivere la quota di un
#: altro mercato.
_H2H_MARKETS = {"1X2"}
_TWO_WAY_MARKETS = {"TENNIS", "ML"}


def _num_env(name: str, default: float, *, minimum: float = 0.0) -> float:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return max(float(raw), minimum)
    except (TypeError, ValueError):
        logger.warning("closing_line: %s='%s' non numerico, uso %.2f",
                       name, raw, default)
        return default


def pre_minutes() -> float:
    return _num_env(PRE_MIN_ENV, DEFAULT_PRE_MIN)


def post_minutes() -> float:
    return _num_env(POST_MIN_ENV, DEFAULT_POST_MIN)


def _max_rows() -> int:
    return int(_num_env(MAX_ROWS_ENV, DEFAULT_MAX_ROWS, minimum=1.0))


def _parse_ts(value: Any) -> Optional[datetime]:
    """Timestamp ISO del ledger -> datetime UTC aware (None se non valido).

    Le date del ledger sono ISO con la 'T' e a volte 'Z': il confronto si fa
    in PYTHON, mai in SQL (lezione del 17/09 sui formati di data).
    """
    if isinstance(value, datetime):
        ts = value
    else:
        try:
            ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# 1. CANDIDATI: righe APERTE con kickoff nella finestra T-0
# ---------------------------------------------------------------------------

_OPEN_PRED = (
    "SELECT p.match_id, p.mercato, p.esito, p.quota, "
    "       m.home_team, m.away_team, m.commence_time "
    "FROM predictions p JOIN matches m ON m.id = p.match_id "
    "WHERE p.esito_finale IS NULL "
    "  AND p.status IN ('value', 'strong_value', 'moderate')"
)
_OPEN_BETS = (
    "SELECT b.match_id, b.mercato, b.esito, b.price, "
    "       m.home_team, m.away_team, m.commence_time "
    "FROM bets b JOIN matches m ON m.id = b.match_id "
    "WHERE b.mode = 'live' "
    "  AND (b.esito_finale IS NULL OR TRIM(b.esito_finale) = '')"
)


def candidates(conn=None, *, now: Optional[datetime] = None) -> list[dict]:
    """Righe aperte con kickoff nella finestra di cattura (dedup per riga)."""
    now = now or datetime.now(timezone.utc)
    lo = now - timedelta(minutes=post_minutes())
    hi = now + timedelta(minutes=pre_minutes())
    out: dict[tuple[str, str], dict] = {}
    try:
        if conn is None:
            from tracker import _get_conn
            conn = _get_conn()
        for sql in (_OPEN_PRED, _OPEN_BETS):
            try:
                rows = conn.execute(sql).fetchall()
            except Exception as exc:
                logger.debug("closing_line: query non leggibile (%s)", exc)
                continue
            for row in rows:
                try:
                    match_id = str(row[0] or "")
                    market = str(row[1] or "").strip().upper()
                    esito = str(row[2] or "").strip()
                    price = float(row[3] or 0.0)
                    home = str(row[4] or "")
                    away = str(row[5] or "")
                    kickoff = _parse_ts(row[6])
                except (IndexError, TypeError, ValueError):
                    continue
                if not match_id or not esito or price <= 1.0 or kickoff is None:
                    continue
                if not (lo <= kickoff <= hi):
                    continue
                key = (match_id, esito)
                if key in out:
                    continue
                out[key] = {"match_id": match_id, "mercato": market,
                            "esito": esito, "price": price,
                            "home": home, "away": away, "kickoff": kickoff}
    except Exception as exc:
        logger.debug("closing_line: candidati non leggibili (%s)", exc)
        return []
    got = list(out.values())
    got.sort(key=lambda r: r["kickoff"])
    return got[:_max_rows()]


# ---------------------------------------------------------------------------
# 2. QUOTA SHARP DI CHIUSURA
# ---------------------------------------------------------------------------

def _outcomes_for(market: str) -> Optional[tuple]:
    if market in _H2H_MARKETS:
        return ("1", "X", "2")
    if market in _TWO_WAY_MARKETS:
        return ("1", "2")
    return None


def _canonical(home: str, away: str, market: str, esito: str) -> Optional[str]:
    """Esito del ledger -> chiave sharp ('1'/'X'/'2'), mai indovinato."""
    if market in _TWO_WAY_MARKETS:
        return esito if esito in ("1", "2") else None
    if esito in ("1", "X", "2"):
        return esito
    try:
        from decision.adapters import canonical_outcome
        # Firma reale: (esito, home, away) — l'ordine sbagliato faceva
        # combaciare il nome della squadra con l'esito e restituiva il prezzo
        # di un ALTRO esito (bug trovato dal test del 04/10).
        return canonical_outcome(esito, home, away)
    except Exception:
        return None


def sharp_closing(home: str, away: str, market: str, esito: str, *,
                  now: Optional[datetime] = None) -> Optional[float]:
    """Quota Pinnacle di chiusura per l'esito, dalle cache. None se assente."""
    outcomes = _outcomes_for(market)
    if outcomes is None:
        return None
    key = _canonical(home, away, market, esito)
    if key not in outcomes:
        return None
    try:
        import pinnacle_oracle as po
        got = po.pinnacle_odds_from_cache(home, away, outcomes=outcomes,
                                          now=(now.timestamp() if now else None))
    except Exception as exc:
        logger.debug("closing_line: sharp non leggibile per %s-%s (%s)",
                     home, away, exc)
        return None
    if not got:
        return None
    try:
        price = float((got.get("odds") or {}).get(key))
    except (TypeError, ValueError):
        return None
    return price if price > 1.0 else None


# ---------------------------------------------------------------------------
# 3. SCRITTURA SUL LEDGER + BEAT %
# ---------------------------------------------------------------------------

def beat_pct(order_odds: float, closing_odds: float) -> Optional[float]:
    """% di beat sul mercato: `(quota_ordine / closing) - 1` (formula unica)."""
    try:
        from market_calib import clv_raw
        return clv_raw(float(order_odds), float(closing_odds))
    except Exception:
        return None


def _existing(conn, match_id: str, esito: str) -> tuple[bool, Optional[float]]:
    """(riga presente?, closing_odds gia' catturata) — None se illeggibile.

       Serve a rendere la routine IDEMPOTENTE: gira ogni pochi minuti e la
       closing line deve restare quella del PRIMO T-0 catturato, non l'ultima
       lettura di cache (che potrebbe essere di ore prima).
       """
    try:
        got = conn.execute(
            "SELECT closing_odds FROM clv_history WHERE match_id=? AND esito=?",
            (match_id, esito)).fetchone()
    except Exception:
        return False, None
    if got is None:
        return False, None
    try:
        value = float(got[0]) if got[0] is not None else None
    except (TypeError, ValueError):
        value = None
    return True, (value if value and value > 1.0 else None)


def capture_closing_lines(*, conn=None, now: Optional[datetime] = None) -> dict:
    """Cattura la closing line di Pinnacle a T-0 per le righe aperte.

    Ritorna `{checked, captured, skipped, by_reason, rows}`. Fail-safe: un
    errore su una riga non ferma le altre e non solleva mai al chiamante.
    """
    out: dict[str, Any] = {"checked": 0, "captured": 0, "skipped": {},
                           "rows": []}
    now = now or datetime.now(timezone.utc)
    try:
        if conn is None:
            from tracker import _get_conn
            conn = _get_conn()
        rows = candidates(conn=conn, now=now)
    except Exception as exc:
        logger.debug("closing_line: candidati non disponibili (%s)", exc)
        return out
    out["checked"] = len(rows)
    for row in rows:
        reason = ""
        # FAIL-SAFE per riga: una cache che esplode non deve fermare le altre
        # (la routine gira dentro il job del bot).
        try:
            closing = sharp_closing(row["home"], row["away"], row["mercato"],
                                    row["esito"], now=now)
        except Exception as exc:
            logger.debug("closing_line: lettura sharp fallita per %s (%s)",
                         row["match_id"], exc)
            closing, reason = None, "read_error"
        if not reason and closing is None:
            reason = ("unsupported_market"
                      if _outcomes_for(row["mercato"]) is None
                      else "no_sharp_cache")
        if not reason:
            try:
                from tracker import save_clv
                exists, prev = _existing(conn, row["match_id"], row["esito"])
                if prev is not None:
                    # Campione GIA' preso a T-0: non si riscrive (idempotenza).
                    reason = "already_captured"
                elif not exists:
                    # Seme del campione: la quota del segnale e' quella presa
                    # dall'ordine (non c'e' un'analisi a monte da cui leggerla).
                    save_clv(row["match_id"], row["esito"], row["price"],
                             signal_started=True, pinnacle_quota=closing,
                             closing_odds=closing)
                else:
                    save_clv(row["match_id"], row["esito"], row["price"],
                             pinnacle_quota=closing, closing_odds=closing)
            except Exception as exc:
                logger.debug("closing_line: scrittura fallita (%s)", exc)
                reason = "write_error"
        if reason:
            out["skipped"][reason] = out["skipped"].get(reason, 0) + 1
            continue
        out["captured"] += 1
        out["rows"].append({**{k: v for k, v in row.items() if k != "kickoff"},
                            "kickoff": row["kickoff"].isoformat(),
                            "closing_odds": round(closing, 6),
                            "beat_pct": beat_pct(row["price"], closing)})
    return out


def report(*, conn=None, limit: int = 200) -> dict:
    """Riepilogo dei campioni con `closing_odds`: quanti, e che beat %."""
    out: dict[str, Any] = {"closed_n": 0, "with_closing": 0, "avg_beat": None,
                           "beat_positive": 0, "rows": []}
    try:
        if conn is None:
            from tracker import _get_conn
            conn = _get_conn()
        rows = conn.execute(
            "SELECT match_id, esito, signal_quota, closing_quota, closing_odds "
            "FROM clv_history ORDER BY updated_at DESC LIMIT ?",
            (int(limit),)).fetchall()
    except Exception as exc:
        logger.debug("closing_line: report non leggibile (%s)", exc)
        return out
    out["closed_n"] = len(rows)
    beats: list[float] = []
    for row in rows:
        try:
            closing = float(row[4]) if row[4] is not None else None
            signal = float(row[2]) if row[2] is not None else None
        except (TypeError, ValueError):
            continue
        if closing is None or signal is None or closing <= 1.0:
            continue
        out["with_closing"] += 1
        beat = beat_pct(signal, closing)
        if beat is None:
            continue
        beats.append(beat)
        if beat > 0:
            out["beat_positive"] += 1
        out["rows"].append({"match_id": row[0], "esito": row[1],
                            "signal_quota": round(signal, 4),
                            "closing_odds": round(closing, 4),
                            "beat_pct": round(beat, 6)})
    if beats:
        out["avg_beat"] = round(sum(beats) / len(beats), 6)
    return out


def format_report(rep: Optional[dict] = None) -> str:
    """Report Telegram-friendly del beat % sulle closing line."""
    rep = rep if rep is not None else report()
    if not rep.get("with_closing"):
        return ("📉 Closing line (T-0)\n"
                "Nessun campione `closing_odds` catturato finora.")
    lines = [
        "📉 Closing line Pinnacle (T-0)",
        f"Campioni con closing: {rep['with_closing']}/{rep['closed_n']}",
        f"Beat positivo: {rep['beat_positive']}/{rep['with_closing']}",
    ]
    if rep.get("avg_beat") is not None:
        lines.append(f"Beat medio: {rep['avg_beat']*100:+.2f}%")
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover - diagnostica
    import argparse
    parser = argparse.ArgumentParser(description="Closing line Pinnacle a T-0")
    parser.add_argument("--capture", action="store_true",
                        help="cattura le closing line in finestra")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    data = capture_closing_lines() if args.capture else report()
    print(json.dumps(data, indent=2, default=str) if args.json
          else (format_report(data) if not args.capture
                else f"catturate: {data['captured']} / {data['checked']}"))
