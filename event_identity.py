"""event_identity.py — identita' UNICA di un evento tra feed diversi (P3).

Perche' esiste (misurato il 17/10/2026): la stessa partita (Norwich-Portsmouth,
kickoff 14:00Z) era nel sistema con **due `match_id`** — uno `sx-L20471322`
(corsia SX) e uno `7a8f8a07…` (the-odds-api) — e sono partiti **due ordini
reali** sullo stesso `market_id`/`selection_id` (4,98 + 2,79 = 7,77 USDC su un
solo esito). Il dedup per `(match_id, esito)` non poteva vederlo: la chiave era
diversa. Lo stesso doppio id produce due righe in `match_results` per la stessa
partita e due registrazioni nel settlement.

Questo modulo NON modifica lo schema e NON scrive nulla: calcola una chiave
canonica dell'evento (da usare in futuro come colonna) e MISURA quante volte
oggi la stessa partita esiste sotto id diversi. E' il prerequisito dichiarato
della migrazione: prima si vede il danno, poi si tocca lo schema.

Regole (le stesse del resto del progetto):
- **mai fuzzy**: nomi risolti con `rating_engine.resolve_team_name`, confronto
  con `team_names.same_team` (deterministico, con guardia di ambiguita');
- **mai indovinare**: se un nome non e' risolvibile o un kickoff non e'
  interpretabile, la chiave e' `None` (identita' incerta) — e il chiamante deve
  trattarla come "non verificabile", non come "evento nuovo";
- **sola lettura**: `audit_duplicate_events` apre SQLite in `mode=ro`.

CLI: `venv/bin/python event_identity.py [--json] [--db PATH] [--days N]`.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger("event_identity")

#: Granularita' del kickoff nella chiave: **0 = solo la DATA UTC** (default).
#: Due feed possono differire di ORE sull'orario (o pubblicare l'orario in fusi
#: diversi): una chiave che cambia alle 21:00 in punto spaccherebbe lo stesso
#: evento se un feed dice 20:55 e l'altro 21:05. Con la data la chiave resta
#: stabile; le squadre sono gia' nella chiave, quindi due partite diverse nello
#: stesso giorno non collidono. Valori >0 aggiungono un bucket di N ore (utile
#: solo se si vogliono distinguere due match omonimi nello stesso giorno).
BUCKET_HOURS = int(os.getenv("EVENT_BUCKET_HOURS", "0") or 0)

#: Tolleranza di kickoff per considerare DUE RIGHE lo stesso evento nell'audit.
KICKOFF_TOLERANCE_MIN = int(os.getenv("EVENT_MATCH_TOLERANCE_MIN", "90") or 90)


# --- normalizzazione -------------------------------------------------------

def _normalize_name(name: str) -> str:
    """Nome canonico della squadra (delega a `team_names`, nessuna copia)."""
    try:
        from team_names import normalize
        return normalize(name)
    except Exception:                                          # pragma: no cover
        return " ".join(str(name or "").lower().split())


def canonical_team(name: str, known: Optional[Iterable[str]] = None
                   ) -> Optional[str]:
    """Nome RISOLTO della squadra, o None se non determinabile.

    La risoluzione passa da `team_names.resolve_team` (alias + chiave
    normalizzata + contenimento con guardia di ambiguita'), cioe' la stessa
    funzione usata dai rating: la chiave dell'evento e quella dei rating non
    possono divergere. `known` permette di passare un insieme esplicito (test e
    audit sul DB); di default il pool e' `team_ratings`.

    Se il nome non e' nel pool si usa il nome normalizzato (non si inventa
    nulla): e' il motivo per cui due VARIANTI dello stesso club possono dare
    chiavi diverse — e per cui l'audit usa `same_team`, che le unifica.
    """
    raw = str(name or "").strip()
    if not raw:
        return None
    resolved = None
    try:
        from team_names import resolve_team
        resolved = resolve_team(raw, known) if known is not None \
            else resolve_team(raw)
    except Exception:
        resolved = None
    if not resolved:
        try:
            from rating_engine import resolve_team_name
            resolved = resolve_team_name(raw)
        except Exception:
            resolved = None
    norm = _normalize_name(resolved or raw)
    return norm or None


def _parse_utc(value) -> Optional[datetime]:
    """Timestamp ISO -> datetime UTC (None se assente/ambiguo)."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        # Un kickoff senza fuso e' un istante AMBIGUO: non si indovina.
        return None
    return dt.astimezone(timezone.utc)


# --- chiave dell'evento ----------------------------------------------------

def event_key(home: str, away: str, commence, *, bucket_hours: Optional[int] = None,
              known: Optional[Iterable[str]] = None) -> Optional[str]:
    """Chiave canonica dell'evento: `<casa>|<trasferta>|<data UTC>`.

    Forma leggibile a occhio (un hash non direbbe nulla a chi indaga). Con
    `bucket_hours > 0` si aggiunge l'ora arrotondata al bucket; il default e'
    la sola DATA, perche' due feed possono differire di ore sull'orario e una
    chiave che cambia al confine di un'ora spaccherebbe lo stesso evento.

    Ritorna None se un nome manca o il kickoff e' assente/ambiguo (**senza
    fuso**): un'identita' incerta non e' una chiave, e chi la usa per
    deduplicare deve fermarsi (fail-closed), non creare un id nuovo.
    """
    h = canonical_team(home, known)
    a = canonical_team(away, known)
    if not h or not a:
        return None
    dt = _parse_utc(commence)
    if dt is None:
        return None
    base = "%s|%s|%s" % (h, a, dt.date().isoformat())
    bucket = int(bucket_hours if bucket_hours is not None else BUCKET_HOURS)
    if bucket and bucket > 0:
        base += "T%02d" % ((dt.hour // bucket) * bucket)
    return base


def event_key_pair(home: str, away: str, commence, *,
                   bucket_hours: Optional[int] = None,
                   known: Optional[Iterable[str]] = None) -> tuple:
    """Le due chiavi possibili (ordine diretto e invertito).

    I feed non concordano sempre sul ruolo casa/trasferta: per la RICERCA di un
    duplicato servono entrambe le forme. La chiave diretta resta quella
    canonica (`event_key`); questa serve solo al lookup.
    """
    return (event_key(home, away, commence, bucket_hours=bucket_hours, known=known),
            event_key(away, home, commence, bucket_hours=bucket_hours, known=known))


# --- audit (SOLA LETTURA) --------------------------------------------------

def _same_team_safe(a: str, b: str) -> bool:
    try:
        from team_names import same_team
        return bool(same_team(a, b))
    except Exception:                                          # pragma: no cover
        return _normalize_name(a) == _normalize_name(b)


def _same_event(r1: dict, r2: dict, tolerance_min: float) -> bool:
    """Stessa partita? Nomi compatibili (in qualunque ordine) + kickoff vicino."""
    k1, k2 = _parse_utc(r1.get("commence_time")), _parse_utc(r2.get("commence_time"))
    if k1 is not None and k2 is not None:
        if abs((k1 - k2).total_seconds()) > tolerance_min * 60:
            return False
    same_order = (_same_team_safe(r1.get("home_team") or "", r2.get("home_team") or "")
                  and _same_team_safe(r1.get("away_team") or "", r2.get("away_team") or ""))
    swapped = (_same_team_safe(r1.get("home_team") or "", r2.get("away_team") or "")
               and _same_team_safe(r1.get("away_team") or "", r2.get("home_team") or ""))
    return bool(same_order or swapped)


def _connect_ro(db_path: Optional[str] = None) -> sqlite3.Connection:
    path = db_path or os.getenv("EVENT_IDENTITY_DB")
    if not path:
        from tracker import DB_PATH
        path = str(DB_PATH)
    uri = "file:%s?mode=ro" % path
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _tables(conn) -> set:
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def audit_duplicate_events(db_path: Optional[str] = None, *,
                           days: int = 14, limit: int = 800,
                           tolerance_min: Optional[float] = None) -> dict:
    """Quante partite esistono OGGI sotto id diversi (sola lettura).

    Serve a dimensionare il problema prima di toccare lo schema: per ogni
    cluster di `matches` che rappresentano lo stesso evento, riporta gli id, la
    lega, il kickoff e quante puntate/previsioni ricadono su ognuno (il rischio
    vero: due righe di settlement per lo stesso denaro).

    Fail-safe: un DB assente, illeggibile o senza tabelle ritorna un blocco con
    `error` dichiarato e `clusters` vuoti — mai un'eccezione al chiamante.
    """
    tol = float(tolerance_min if tolerance_min is not None else KICKOFF_TOLERANCE_MIN)
    out: dict = {"db": str(db_path or ""), "days": days, "rows": 0,
                 "clusters": [], "duplicates": 0, "events": 0,
                 "tolerance_min": tol, "error": None}
    conn = None
    try:
        conn = _connect_ro(db_path)
        tables = _tables(conn)
        if "matches" not in tables:
            out["error"] = "tabella matches assente"
            return out
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        rows = conn.execute(
            "SELECT id, home_team, away_team, commence_time, league FROM matches "
            "WHERE datetime(commence_time) >= datetime(?) "
            "ORDER BY commence_time DESC LIMIT ?",
            (cutoff, int(limit))).fetchall()
        out["rows"] = len(rows)
        events = [dict(r) for r in rows]
        # Prefiltro per GIORNO: due righe a giorni di distanza non sono lo
        # stesso evento (andata/ritorno), e cosi' il confronto resta piccolo.
        by_day: dict = {}
        for ev in events:
            dt = _parse_utc(ev.get("commence_time"))
            key = dt.date().isoformat() if dt else "?"
            by_day.setdefault(key, []).append(ev)
        seen: set = set()
        for day, group in sorted(by_day.items()):
            for i, ev in enumerate(group):
                if ev["id"] in seen:
                    continue
                cluster = [ev]
                for other in group[i + 1:]:
                    if other["id"] in seen:
                        continue
                    if _same_event(ev, other, tol):
                        cluster.append(other)
                        seen.add(other["id"])
                if len(cluster) > 1:
                    seen.add(ev["id"])
                    out["clusters"].append(_cluster_info(cluster, tables, conn))
        out["duplicates"] = sum(len(c["ids"]) - 1 for c in out["clusters"])
        out["events"] = len(out["clusters"])
        return out
    except sqlite3.Error as e:
        out["error"] = "db_error: %s" % e
        return out
    except Exception as e:                                     # pragma: no cover
        out["error"] = "%s: %s" % (type(e).__name__, e)
        return out
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


def _cluster_info(cluster: Iterable[dict], tables: set, conn) -> dict:
    """Dettaglio di un cluster (id, nomi, kickoff, righe collegate)."""
    items = list(cluster)
    ids = [str(r.get("id")) for r in items]
    bets = 0
    stake = 0.0
    preds = 0
    if "bets" in tables and ids:
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(stake), 0) FROM bets "
            "WHERE match_id IN (%s)" % ",".join("?" * len(ids)), ids).fetchone()
        bets, stake = int(row[0] or 0), float(row[1] or 0.0)
    if "predictions" in tables and ids:
        row = conn.execute(
            "SELECT COUNT(*) FROM predictions WHERE match_id IN (%s)"
            % ",".join("?" * len(ids)), ids).fetchone()
        preds = int(row[0] or 0)
    return {
        "ids": ids,
        "names": [("%s - %s" % (r.get("home_team"), r.get("away_team")))
                  for r in items],
        "league": next((r.get("league") for r in items if r.get("league")), None),
        "kickoff": [r.get("commence_time") for r in items],
        "bets": bets,
        "stake": round(stake, 4),
        "predictions": preds,
        "event_key": event_key(items[0].get("home_team"),
                               items[0].get("away_team"),
                               items[0].get("commence_time")),
    }


def format_report(res: dict) -> str:
    """Report testuale (Telegram-friendly) dell'audit."""
    if res.get("error"):
        return ("🆔 *IDENTITA' EVENTI* — non disponibile: `%s`" % res["error"])
    lines = [
        "🆔 *IDENTITA' EVENTI* — %d partite (%d giorni), %d con id duplicati"
        % (res.get("rows") or 0, res.get("days") or 0, res.get("events") or 0),
        "Righe in eccesso: *%d* · tolleranza kickoff %d'"
        % (res.get("duplicates") or 0, int(res.get("tolerance_min") or 0)),
    ]
    for c in (res.get("clusters") or [])[:8]:
        lines.append("• %s → %d id (puntate %d, %.2f USDC, previsioni %d)"
                     % (" / ".join(c.get("names") or []), len(c.get("ids") or []),
                        c.get("bets") or 0, c.get("stake") or 0.0,
                        c.get("predictions") or 0))
        # Gli ID servono a chi indaga: senza, "2 id" non dice QUALI righe
        # vanno riconciliate (e il caso del 17/10 e' nato proprio su questo).
        ids = [str(i) for i in (c.get("ids") or [])]
        if ids:
            lines.append("  `%s`" % "` · `".join(ids))
    return "\n".join(lines)


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        description="Audit dell'identita' degli eventi (sola lettura)")
    ap.add_argument("--db", type=str, default=None)
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--tolerance", type=float, default=None)
    args = ap.parse_args(argv)
    res = audit_duplicate_events(args.db, days=args.days,
                                 tolerance_min=args.tolerance)
    print(json.dumps(res, indent=2, default=str) if args.json
          else format_report(res))
    return 1 if res.get("error") else 0


if __name__ == "__main__":                    # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
