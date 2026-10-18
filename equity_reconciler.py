"""equity_reconciler.py — riconciliazione FINANZIARIA del conto (P4, 17/10/2026).

Perche' esiste: fra due campioni di equity il conto e' variato di una cifra che
il P/L del ledger NON spiega (-2,26 USDC il 17/10/2026). Un residuo non
spiegato puo' essere: (a) una puntata risolta dall'exchange ma non ancora
saldata a ledger (il payout e' in `available`, lo stake e' ancora contato "in
gioco": doppio conteggio, finestra di poche ore), (b) un deposito/prelievo,
(c) una commissione dell'exchange, (d) una riga mancante nel ledger. Sono
cause con rimedi OPPOSTI: correggere il ledger quando e' colpa di (a) o (d)
sarebbe un falso, ignorare (b) sarebbe un buco. Per questo lo strumento
**misura e classifica**, non corregge: nessuna scrittura, mai.

Regole del progetto rispettate:
- **sola lettura**: SQLite aperto con `file:...?mode=ro`; nessun INSERT/UPDATE;
- **nessuna rete, zero crediti, zero ordini**;
- **nessun numero inventato**: un dato assente e' `None`, non zero;
- **nessuna causa attribuita senza evidenza**: il residuo esce con i
  candidati e le prove raccolte, e la classificazione resta `unknown` quando
  le prove non bastano;
- **mai confronti fra stringhe data**: le date del ledger sono ISO con la 'T',
  i confronti avvengono in Python (`_parse_ts`).

Uso:
    venv/bin/python equity_reconciler.py [--hours 24] [--json] [--db PATH]
    venv/bin/python equity_reconciler.py --day 2026-10-17
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger("equity_reconciler")

try:                                                      # pragma: no cover
    from config import DATA_DIR
except Exception:                                         # pragma: no cover
    DATA_DIR = Path(os.getenv("QUOTAVERACE_DATA_DIR", "data"))

#: Sotto questa soglia un residuo e' arrotondamento (quote/payout a 4 decimali).
TOLERANCE_USDC = float(os.getenv("RECONCILE_TOLERANCE_USDC", "0.01"))


# ---------------------------------------------------------------------------
# letture (tutte fail-safe)
# ---------------------------------------------------------------------------

def _parse_ts(value, assume_utc: bool = False) -> Optional[datetime]:
    """Istante UTC da una data del ledger / di un file di stato.

    Rifiuta le date SENZA fuso: un istante ambiguo non si indovina (lezione del
    15/09/2026). Accetta 'Z', offset, 'T' o spazio, con o senza microsecondi.

    `assume_utc=True` esiste per UNA sola colonna: `bets.created_at`, che
    `tracker.save_bet` scrive con `datetime.now().isoformat()` **senza fuso**.
    Il processo gira in UTC, ma l'assunzione NON e' silenziosa: chi legge la
    conta (`naive_created`) e la dichiara nel report. Rifiutarla del tutto
    renderebbe il tool cieco proprio sulle posizioni aperte (il 100% delle
    righe), che e' ció che deve misurare.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is None:
        if not assume_utc:
            return None
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _db_path(db: Optional[str] = None) -> Path:
    if db:
        return Path(db)
    try:
        from tracker import DB_PATH
        return Path(DB_PATH)
    except Exception:                                     # pragma: no cover
        return DATA_DIR / "quotaverace.db"


def _connect_ro(db: Optional[str] = None):
    """Connessione in SOLA LETTURA (il file non si apre nemmeno in scrittura)."""
    path = _db_path(db)
    if not path.exists():
        raise FileNotFoundError("DB assente: %s" % path)
    conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _columns(conn, table: str) -> set:
    try:
        return {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)}
    except Exception:
        return set()


def _select(conn, table: str, wanted: Iterable[str], where: str = "",
            params: tuple = ()) -> list:
    """SELECT costruita sull'INTERSEZIONE delle colonne esistenti.

    Su un DB di un deploy precedente una colonna puo' mancare: una query che la
    nomina fallisce e il chiamante leggerebbe `[]`, cioe' "zero righe" invece
    di "colonna assente" (difetto trovato il 02/10 sui tool di diagnostica).
    """
    cols = _columns(conn, table)
    if not cols:
        return []
    use = [c for c in wanted if c in cols]
    if not use:
        return []
    sql = "SELECT %s FROM %s %s" % (", ".join(use), table, where)
    try:
        return [dict(r) for r in conn.execute(sql, params)]
    except Exception as e:
        logger.warning("equity_reconciler: lettura %s fallita (%s)", table, e)
        return []


# ---------------------------------------------------------------------------
# campioni di equity
# ---------------------------------------------------------------------------

def load_samples(path=None, now: Optional[datetime] = None,
                 hours: Optional[float] = None) -> list:
    """Campioni di equity (`bankroll_history.json`) ordinati per istante.

    Ritorna `[{ts, value, basis_key}]`. Le righe illeggibili sono SCARTATE e
    contate (`bad`), non silenziate dentro un totale.
    """
    if path is None:
        try:
            from auto_bet import BANKROLL_HISTORY_FILE as path
        except Exception:                                 # pragma: no cover
            path = DATA_DIR / "execution" / "bankroll_history.json"
    path = Path(path)
    out: dict = {"samples": [], "bad": 0, "basis_key": None, "error": None}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("formato inatteso")
    except Exception as e:
        out["error"] = "%s: %s" % (type(e).__name__, e)
        return out
    out["basis_key"] = data.get("basis_key")
    rows = []
    for item in data.get("samples") or []:
        try:
            ts = _parse_ts(item[0])
            value = float(item[1])
        except (TypeError, ValueError, IndexError, KeyError):
            out["bad"] += 1
            continue
        if ts is None or value <= 0:
            out["bad"] += 1
            continue
        rows.append({"ts": ts, "value": value})
    rows.sort(key=lambda r: r["ts"])
    if hours is not None and hours > 0:
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(hours=float(hours))
        rows = [r for r in rows if r["ts"] >= cutoff]
    out["samples"] = rows
    return out


# ---------------------------------------------------------------------------
# movimenti dal ledger
# ---------------------------------------------------------------------------

def ledger_movements(since: datetime, until: datetime,
                     db: Optional[str] = None) -> dict:
    """Movimenti del ledger `bets` nella finestra (sola lettura).

    Distingue cio' che il ledger SA (stake aperti, settlement con profitto) da
    cio' che non sa (commissioni dell'exchange, depositi, prelievi): quelli non
    hanno una riga, quindi restano `None` finche' le prove non li mostrano.
    """
    out = {"settled": [], "opened": [], "open_at_until": [],
           "profit": 0.0, "staked_settled": 0.0, "stake_opened": 0.0,
           "won": 0, "lost": 0, "push": 0, "naive_created": 0,
           "naive_settled": 0, "future_created": 0, "error": None,
           "db": str(_db_path(db))}
    try:
        conn = _connect_ro(db)
    except Exception as e:
        out["error"] = "%s: %s" % (type(e).__name__, e)
        return out
    try:
        rows = _select(conn, "bets",
                       ("rowid", "match_id", "mercato", "esito", "market_id",
                        "selection_id", "price", "stake", "profit",
                        "esito_finale", "created_at", "settled_at", "mode",
                        "status", "bet_id"))
    except Exception as e:                                # pragma: no cover
        out["error"] = "%s: %s" % (type(e).__name__, e)
        conn.close()
        return out
    conn.close()
    for r in rows:
        if str(r.get("mode") or "") != "live":
            continue                        # la cassa simulata non e' denaro
        # ⚠️ MISURATO il 17/10 in produzione: `tracker.save_bet` scrive
        # `created_at` e `settle_*` scrivono `settled_at` con
        # `datetime.now().isoformat()`, cioe' **SENZA fuso**. Un parser
        # rigoroso (che rifiuta le date ambigue, regola del 15/09) rendeva il
        # ledger INVISIBILE: primo giro sul container -> "settled 0, profit
        # 0.00" e un residuo di -14.59 che era solo un artefatto di lettura.
        # Qui l'assunzione "naive = UTC" e' DICHIARATA e contata, non silenziosa.
        created = _parse_ts(r.get("created_at"), assume_utc=True)
        if _is_naive(r.get("created_at")):
            out["naive_created"] += 1
        settled = _parse_ts(r.get("settled_at"), assume_utc=True)
        if _is_naive(r.get("settled_at")):
            out["naive_settled"] += 1
        stake = _num(r.get("stake"))
        profit = _num(r.get("profit"))
        row = {
            "match_id": r.get("match_id"),
            "esito": r.get("esito"),
            "mercato": r.get("mercato"),
            "market_id": r.get("market_id"),
            "selection_id": r.get("selection_id"),
            "price": _num(r.get("price")),
            "stake": stake,
            "profit": profit,
            "esito_finale": r.get("esito_finale"),
            "created_at": created.isoformat() if created else None,
            "settled_at": settled.isoformat() if settled else None,
            "status": r.get("status"),
            "bet_id": r.get("bet_id"),
        }
        if r.get("esito_finale") and settled and since <= settled <= until:
            out["settled"].append(row)
            out["profit"] += profit or 0.0
            out["staked_settled"] += stake or 0.0
            final = str(r.get("esito_finale") or "").lower()
            if final == "won":
                out["won"] += 1
            elif final == "lost":
                out["lost"] += 1
            elif final == "push":
                out["push"] += 1
        if created and since <= created <= until:
            out["opened"].append(row)
            out["stake_opened"] += stake or 0.0
        if not r.get("esito_finale"):
            # Una posizione APERTA esiste adesso a prescindere da quando e'
            # stata creata: il filtro temporale qui non serve (e con
            # `created_at` naive su un host non in UTC taglierebbe proprio le
            # righe che si vogliono misurare). Una data nel FUTURO e' invece
            # un segnale di clock/TZ diverso: si conta e si dichiara.
            out["open_at_until"].append(row)
            if created is not None and created > until:
                out["future_created"] += 1
    out["settled"].sort(key=lambda r: r["settled_at"] or "")
    return out


def _is_naive(value) -> bool:
    """True se la data esiste ma NON porta un fuso orario."""
    if value in (None, "") or isinstance(value, datetime):
        return False
    text = str(value).strip()
    return bool(text) and "+" not in text and not text.endswith("Z")


def _num(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# ricostruzione della variazione
# ---------------------------------------------------------------------------

def rebuild(hours: float = 24.0, now: Optional[datetime] = None,
            db: Optional[str] = None, samples_path=None) -> dict:
    """Ricostruisce la variazione di equity della finestra e il suo residuo.

    Identita' attesa (se il ledger e le letture sono completi):

        equity(fine) - equity(inizio) == SUM(profit dei settlement nella finestra)

    Un residuo NON e' un errore di per se': la classificazione dice quali prove
    esistono a favore di ciascuna causa. `verdict` resta `unknown` finche' non
    c'e' una prova, perche' attribuire una causa senza evidenza e' il modo
    peggiore di "spiegare" un buco.
    """
    now = now or datetime.now(timezone.utc)
    hours = float(hours or 24.0)
    since = now - timedelta(hours=hours)
    hist = load_samples(samples_path)

    out = {
        "window": {"since": since.isoformat(), "until": now.isoformat(),
                   "hours": hours},
        "samples": {"total": len(hist["samples"]), "bad": hist["bad"],
                    "basis_key": hist["basis_key"], "error": hist["error"]},
        "equity": {"first": None, "last": None, "delta": None},
        "ledger": None,
        "residual": None,
        "verdict": "unknown",
        "evidence": {},
        "hypotheses": [],
        "notes": [],
    }
    if hist["error"]:
        out["notes"].append("storico equity illeggibile: %s" % hist["error"])
    samples = [s for s in hist["samples"] if s["ts"] >= since]
    # Un solo campione NON misura una variazione: `last - first` darebbe 0.0,
    # che si legge "nessun cambiamento" invece di "non misurabile".
    if len(samples) >= 2:
        out["equity"].update({
            "first": round(samples[0]["value"], 4),
            "last": round(samples[-1]["value"], 4),
            "delta": round(samples[-1]["value"] - samples[0]["value"], 4),
            "first_ts": samples[0]["ts"].isoformat(),
            "last_ts": samples[-1]["ts"].isoformat(),
            "n": len(samples),
        })

    mov = ledger_movements(since, now, db=db)
    out["ledger"] = {
        "profit": round(mov["profit"], 4),
        "staked_settled": round(mov["staked_settled"], 4),
        "stake_opened": round(mov["stake_opened"], 4),
        "settled": len(mov["settled"]), "opened": len(mov["opened"]),
        "won": mov["won"], "lost": mov["lost"], "push": mov["push"],
        "open_at_until": len(mov["open_at_until"]),
        "created_at_naive": mov["naive_created"],
        "settled_at_naive": mov["naive_settled"],
        "error": mov["error"], "db": mov["db"],
    }
    if mov["naive_settled"]:
        out["notes"].append(
            "%d settlement hanno `settled_at` SENZA fuso (scritto naive dal "
            "settlement): interpretati come UTC — assunzione dichiarata"
            % mov["naive_settled"])
    if mov.get("future_created"):
        out["notes"].append(
            "%d righe hanno `created_at` nel FUTURO rispetto a `now`: il fuso "
            "della scrittura non e' UTC (host non in UTC). Le posizioni sono "
            "contate lo stesso; le date di creazione no."
            % mov["future_created"])
    if mov["naive_created"]:
        out["notes"].append(
            "%d righe hanno `created_at` SENZA fuso orario (scritto naive da "
            "`save_bet`): interpretate come UTC, ed e' un'assunzione "
            "dichiarata, non un dato verificato" % mov["naive_created"])
    if mov["error"]:
        out["notes"].append("ledger illeggibile: %s" % mov["error"])

    if out["equity"]["delta"] is not None and not mov["error"]:
        residual = round(out["equity"]["delta"] - mov["profit"], 4)
        out["residual"] = residual
        if abs(residual) <= TOLERANCE_USDC:
            out["verdict"] = "explained"
        else:
            out["verdict"] = "unexplained"
    elif out["equity"]["delta"] is None:
        out["notes"].append("meno di due campioni nella finestra: variazione "
                            "non misurabile")
    else:
        out["notes"].append("residuo non calcolabile senza il ledger")

    # ------------------------------------------------------------------
    # PROVE (non conclusioni): solo elementi verificabili nel ledger
    # ------------------------------------------------------------------
    first_ts = None
    last_ts = None
    if samples:
        first_ts, last_ts = samples[0]["ts"], samples[-1]["ts"]
    open_rows = mov["open_at_until"]
    # (a) puntate aperte il cui kickoff e' gia' passato: se l'exchange ha
    #     risolto e il job non ha ancora saldato, il payout e' in `available`
    #     mentre lo stake e' ancora "in gioco" -> equity gonfiata (doppio).
    started = [r for r in open_rows if r.get("created_at")
               and (last_ts is None or _parse_ts(r["created_at"]) <= last_ts)]
    out["evidence"] = {
        "open_live_bets": len(open_rows),
        "open_live_stake": round(sum((r["stake"] or 0.0) for r in open_rows), 4),
        "open_live_since_first_sample": len(started),
        "settled_rows": [
            {"match_id": r["match_id"], "esito": r["esito"],
             "stake": r["stake"], "profit": r["profit"],
             "settled_at": r["settled_at"]}
            for r in mov["settled"]
        ],
        "opened_rows": [
            {"match_id": r["match_id"], "esito": r["esito"],
             "stake": r["stake"], "price": r["price"],
             "created_at": r["created_at"], "status": r["status"]}
            for r in mov["opened"]
        ],
    }
    # (b) ordini RESTING vivi: escrow sull'exchange senza riga in `bets`.
    try:
        import resting_orders
        resting = resting_orders.open_orders() or []
        out["evidence"]["resting_open"] = len(resting)
        out["evidence"]["resting_stake"] = round(
            sum(float(r.get("stake") or 0.0) for r in resting), 4)
    except Exception as e:
        out["evidence"]["resting_open"] = None
        out["evidence"]["resting_error"] = "%s: %s" % (type(e).__name__, e)

    if out["verdict"] == "unexplained":
        r = out["residual"]
        if r < 0:
            out["hypotheses"].append({
                "cause": "settlement non ancora registrato / commissione",
                "sign": "equity scesa piu' del P/L",
                "test": "cercare puntate LIVE aperte con partita gia' iniziata "
                        "(il payout e' arrivato, lo stake e' ancora contato)",
                "support": out["evidence"]["open_live_since_first_sample"]})
            out["hypotheses"].append({
                "cause": "prelievo o commissione exchange",
                "sign": "uscita di cassa senza riga nel ledger",
                "test": "confrontare con lo storico movimenti del wallet SX "
                        "(non disponibile via API: /orders-v3 elenca solo gli "
                        "ordini aperti)",
                "support": None})
        else:
            out["hypotheses"].append({
                "cause": "deposito o payout non registrato",
                "sign": "equity salita piu' del P/L",
                "test": "verificare un top-up del wallet o un settlement "
                        "avvenuto senza riga a ledger",
                "support": out["evidence"]["open_live_since_first_sample"]})
        out["notes"].append(
            "residuo %.2f USDC: nessuna ipotesi ha prove sufficienti per "
            "essere dichiarata causa — il ledger NON va modificato per farlo "
            "quadrare" % r)
    return out


# ---------------------------------------------------------------------------
# timeline dello stop giornaliero
# ---------------------------------------------------------------------------

def stop_timeline(day: Optional[str] = None, db: Optional[str] = None,
                  stop_path=None, samples_path=None) -> dict:
    """Perche' lo stop risulta armato PRIMA della perdita che lo giustifica.

    Il giornaliero misura l'EQUITY (disponibile + in gioco) contro il valore
    registrato a inizio giornata: una perdita entra nell'equity quando il
    settlement la scrive, quindi un blocco con orario anteriore al settlement
    NON puo' essere causato da quel settlement (a meno che la lettura
    dell'equity non includa una variazione diversa, es. escrow).

    Ritorna la sequenza: istante dell'armamento, valore misurato, perdita
    dichiarata, e i settlement registrati dopo l'armamento (con il delta che
    ciascuno ha prodotto sull'equity).
    """
    if stop_path is None:
        try:
            from auto_bet import DAILY_STOP_FILE as stop_path
        except Exception:                                 # pragma: no cover
            stop_path = DATA_DIR / "execution" / "daily_stop.json"
    out = {"stop_file": str(stop_path), "state": None, "armed_at": None,
           "start_bankroll": None, "value": None, "loss_pct": None,
           "settlements_after_arm": [], "settlements_before_arm": [],
           "verdict": "unknown", "notes": []}
    try:
        state = json.loads(Path(stop_path).read_text(encoding="utf-8"))
        out["state"] = state if isinstance(state, dict) else None
    except Exception as e:
        out["notes"].append("file di stop illeggibile (%s: %s)"
                            % (type(e).__name__, e))
        state = None
    if isinstance(state, dict):
        out["armed_at"] = state.get("stopped_at") or state.get("stopped_until")
        out["start_bankroll"] = _num(state.get("start_bankroll"))
        out["loss_pct"] = _num(state.get("loss_pct"))
        out["reason"] = state.get("reason")
        # ⚠️ Il file NON ha una chiave `value`: il codice scrive il valore
        # DENTRO `reason` ("... (valore 38.00)"). Senza questo fallback il
        # tool riportava `value: None` e una "perdita misurata" pari all'intero
        # capitale — un numero sbagliato e piu' allarmante del vero.
        value = _num(state.get("value"))
        if value is None:
            import re
            m = re.search(r"valore\s+([0-9]+(?:\.[0-9]+)?)",
                          str(state.get("reason") or ""))
            value = float(m.group(1)) if m else None
        out["value"] = value
        if value is None:
            out["notes"].append("valore misurato non presente nel file di "
                                "stop ne' nel motivo: perdita non ricalcolabile")
    # Il file di stato lo scrive il nostro processo (UTC) con `isoformat()`
    # senza fuso: senza `assume_utc` l'armamento risultava illeggibile e la
    # timeline non era calcolabile affatto (verificato sul file del 17/10:
    # `armed_at` = "2026-10-17T03:11:24.246300", nessun offset).
    armed = _parse_ts(out["armed_at"], assume_utc=True)
    if _is_naive(out["armed_at"]):
        out["armed_at_naive"] = True
        out["notes"].append("`stopped_at` senza fuso nel file di stato: "
                            "interpretato come UTC (assunzione dichiarata)")
    hist = load_samples(samples_path)
    ref_day = day or (armed.date().isoformat() if armed else None)
    if ref_day is None:
        out["notes"].append("nessun giorno di riferimento")
        return out
    try:
        since = datetime.fromisoformat(ref_day + "T00:00:00+00:00")
    except ValueError:
        out["notes"].append("giorno non interpretabile: %s" % ref_day)
        return out
    until = (armed + timedelta(hours=6)) if armed else since + timedelta(days=1)
    mov = ledger_movements(since, until, db=db)
    if mov["error"]:
        out["notes"].append("ledger illeggibile: %s" % mov["error"])
        return out
    for r in mov["settled"]:
        ts = _parse_ts(r["settled_at"])
        row = {"match_id": r["match_id"], "esito": r["esito"],
               "stake": r["stake"], "profit": r["profit"],
               "settled_at": r["settled_at"]}
        if armed and ts and ts > armed:
            out["settlements_after_arm"].append(row)
        else:
            out["settlements_before_arm"].append(row)
    # Campioni intorno all'armamento: la prova che l'equity e' SCESA prima
    # riguarda una variazione che il ledger non registra.
    around = [s for s in hist["samples"] if armed
              and abs((s["ts"] - armed).total_seconds()) <= 6 * 3600]
    out["samples_around"] = [{"ts": s["ts"].isoformat(),
                              "value": round(s["value"], 4)} for s in around]
    if armed and out["start_bankroll"]:
        out["measured_loss"] = round(out["start_bankroll"] - (out["value"] or 0), 4)
    if armed and out["settlements_after_arm"] and not out["settlements_before_arm"]:
        out["verdict"] = "stop_precedes_ledger"
        out["notes"].append(
            "l'armamento (%s) precede TUTTI i settlement della giornata "
            "(%s): la perdita registrata dopo non puo' essere la causa. "
            "L'equity e' scesa per una variazione fuori dal ledger (escrow/"
            "lettura del wallet), non per un esito." %
            (armed.isoformat(), out["settlements_after_arm"][0]["settled_at"]))
    elif armed:
        out["verdict"] = "ledger_before_arm"
        out["notes"].append(
            "esistono settlement (%d) registrati PRIMA dell'armamento: vanno "
            "verificati i loro importi contro la perdita dichiarata (%s%%)"
            % (len(out["settlements_before_arm"]), out["loss_pct"]))
    return out


# ---------------------------------------------------------------------------
# report / CLI
# ---------------------------------------------------------------------------

def format_report(data: dict) -> str:
    """Riepilogo testuale (Telegram/CLI): ogni numero ha la sua fonte."""
    eq = data.get("equity") or {}
    led = data.get("ledger") or {}
    lines = ["💼 RICONCILIAZIONE (ultime %g h)" %
             (data.get("window", {}).get("hours") or 0)]
    if eq.get("first") is None:
        lines.append("• equity: variazione non misurabile (%s)" %
                     "; ".join(data.get("notes") or ["campioni insufficienti"]))
    else:
        lines.append("• equity: %.2f → %.2f (Δ %.2f) su %s campioni" %
                     (eq["first"], eq["last"], eq.get("delta") or 0.0,
                      (data.get("samples") or {}).get("n")))
        lines.append("• ledger: %s settlement, P/L %+.2f (vinti %s / persi %s)"
                     % (led.get("settled"), led.get("profit") or 0.0,
                        led.get("won"), led.get("lost")))
        if data.get("residual") is not None:
            lines.append("• residuo: %+.2f USDC → %s"
                         % (data["residual"], data.get("verdict")))
    ev = data.get("evidence") or {}
    if ev.get("open_live_bets") is not None:
        lines.append("• puntate live aperte: %s (%.2f USDC, %s aperte da "
                     "prima dell'ultimo campione)"
                     % (ev.get("open_live_bets"), ev.get("open_live_stake") or 0,
                        ev.get("open_live_since_first_sample")))
    if ev.get("resting_open") is not None:
        lines.append("• ordini RESTING vivi: %s (%.2f USDC fuori dal ledger)"
                     % (ev["resting_open"], ev.get("resting_stake") or 0))
    for hyp in data.get("hypotheses") or []:
        lines.append("• ipotesi: %s (%s)" % (hyp["cause"], hyp["sign"]))
    for note in data.get("notes") or []:
        lines.append("⚠️ %s" % note)
    return "\n".join(lines)


def main(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(description="Riconciliazione finanziaria (sola "
                                            "lettura)")
    p.add_argument("--hours", type=float, default=24.0)
    p.add_argument("--day", help="timeline dello stop per un giorno YYYY-MM-DD")
    p.add_argument("--json", action="store_true")
    p.add_argument("--db")
    p.add_argument("--samples")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    if args.day:
        data = stop_timeline(day=args.day, db=args.db,
                             samples_path=args.samples)
        if args.json:
            print(json.dumps(data, indent=2, default=str))
            return 0
        print("🛑 STOP GIORNALIERO — %s" % args.day)
        print("• armato: %s (valore %s, perdita dichiarata %s%%)"
              % (data.get("armed_at"), data.get("value"), data.get("loss_pct")))
        print("• settlement prima: %d | dopo: %d"
              % (len(data.get("settlements_before_arm") or []),
                 len(data.get("settlements_after_arm") or [])))
        for row in data.get("settlements_after_arm") or []:
            print("  - dopo: %s %s stake %.2f P/L %+.2f"
                  % (row["settled_at"], row["match_id"], row["stake"] or 0,
                     row["profit"] or 0))
        for note in data.get("notes") or []:
            print("⚠️ %s" % note)
        return 0
    data = rebuild(hours=args.hours, db=args.db, samples_path=args.samples)
    if args.json:
        print(json.dumps(data, indent=2, default=str))
    else:
        print(format_report(data))
    return 0 if data.get("verdict") in ("explained", "unknown") else 1


if __name__ == "__main__":                                # pragma: no cover
    raise SystemExit(main())
