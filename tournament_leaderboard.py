"""tournament_leaderboard.py — l'andamento della classifica del torneo SX Bet.

PERCHE' ESISTE (direttiva del proprietario, 12/10/2026)
-------------------------------------------------------
"massimizziamo il lavoro per il torneo, tieni aggiornato l'andamento della
classifica sul bot". Il layer di staking (`tournament.py`) decide QUANTO
giocare; questo modulo dice DOVE SIAMO: posizione, punteggio, distanza dalla
zona premi e tempo rimasto. Senza, il torneo e' una scommessa alla cieca.

API USATA — PUBBLICA, ZERO CREDITI the-odds-api, ZERO chiavi, ZERO ordini
----------------------------------------------------------------------------
    GET /tournaments/ongoing            -> i tornei IN CORSO
    GET /tournaments/upcoming           -> i tornei futuri (fallback)
    GET /tournaments/<tournamentId>     -> la CLASSIFICA completa

⚠️ DUE TRAPPOLE MISURATE SUL CAMPO (non ipotesi)
------------------------------------------------
1. **SX Bet blocca le richieste senza `User-Agent`**: con l'header di default
   di `requests`/`urllib` la risposta non arriva. L'UA va SEMPRE inviato (env
   `SX_TOURNAMENT_UA`, default `Mozilla/5.0`).
2. **`/tournaments` e `/user/tournaments` NON servono**: il primo risponde
   `401 BAD_AUTH`, il secondo `400 "address must be a valid address"`.
   La classifica si legge SOLO da `/tournaments/<id>`, e l'id si scopre da
   `/tournaments/ongoing`.

⚠️ COSA SIGNIFICA `totalResult` (formato RETURN)
------------------------------------------------
Il torneo e' *return-based*: il punteggio e' la somma dei RITORNI LORDI delle
puntate **vinte**, una puntata persa **non toglie punti**. Quindi `result 0`
con N trade e' coerente e la leva per scalare la classifica e' **vincere**, non
giocare di piu'. Il report lo dichiara sempre: un lettore che interpreta
`result` come P/L prende una decisione sbagliata.

Fail-safe totale: `snapshot()` non solleva MAI. Un errore di rete diventa un
blocco con `ok=False` ed `error` dichiarato, mai un'eccezione verso il job.

Env (tutte dichiarate `preserve()` in `.railway/railway.ts`):
    TOURNAMENT_BOARD              "1"      (interruttore del lettore)
    SX_TOURNAMENT_API_BASE        "https://api.sx.bet"
    SX_TOURNAMENT_UA              "Mozilla/5.0"  (obbligatorio: vedi sopra)
    TOURNAMENT_USERNAME           "Siryo"  (nome in classifica)
    TOURNAMENT_TOP_RANKS          "30"     (posizioni premiate)
    TOURNAMENT_BOARD_TTL_S        "300"    (memo: `/torneo` non martella l'API)
    TOURNAMENT_BOARD_TIMEOUT_S    "15"
    TOURNAMENT_BOARD_NOTIFY_HOURS "12"     (anti-spam: 2 notifiche/giorno)
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger("tournament_leaderboard")

#: Nome del torneo da seguire (deve combaciare con `tournament.LABEL`).
DEFAULT_TOURNAMENT = "Gridiron Gauntlet"


# ---------------------------------------------------------------------------
# Configurazione (letture a RUNTIME: si cambia senza redeploy)
# ---------------------------------------------------------------------------

def _num_env(name: str, default: float, *, minimum: float = 0.0) -> float:
    """Env numerica con clamp verso il basso (valore impossibile -> default).

    Una guardia di lettura non si spegne con un env sbagliato: un valore non
    numerico torna al default invece di propagare un errore nel job.
    """
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return float(default)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning("tournament_leaderboard: %s='%s' non numerico, uso %s",
                       name, raw, default)
        return float(default)
    if value < minimum:
        logger.warning("tournament_leaderboard: %s=%s sotto il minimo %s, "
                       "uso %s", name, value, minimum, default)
        return float(default)
    return value


def board_enabled() -> bool:
    """Interruttore del lettore (`TOURNAMENT_BOARD`; default ON).

    La lettura e' GRATUITA (API pubblica SX, zero crediti the-odds-api): resta
    accesa di default e la si spegne solo con un valore esplicito.
    """
    return str(os.getenv("TOURNAMENT_BOARD", "1")).strip().lower() \
        not in ("0", "false", "no", "off", "disabled")


def api_base() -> str:
    return (os.getenv("SX_TOURNAMENT_API_BASE") or "https://api.sx.bet").rstrip("/")


def user_agent() -> str:
    """UA esplicito — SX Bet scarta le richieste senza (misurato)."""
    return os.getenv("SX_TOURNAMENT_UA") or "Mozilla/5.0"


def username() -> str:
    """Nome in classifica (env `TOURNAMENT_USERNAME`)."""
    return (os.getenv("TOURNAMENT_USERNAME") or "Siryo").strip()


def top_ranks() -> int:
    """Ultima posizione premiata (default 30: la griglia premi ne ha 30)."""
    return max(1, int(_num_env("TOURNAMENT_TOP_RANKS", 30.0, minimum=1.0)))


def cache_ttl_s() -> float:
    """Memo del `snapshot` (default 300s): il comando non martella l'API."""
    return _num_env("TOURNAMENT_BOARD_TTL_S", 300.0, minimum=0.0)


def request_timeout_s() -> float:
    return _num_env("TOURNAMENT_BOARD_TIMEOUT_S", 15.0, minimum=1.0)


def notify_min_hours() -> float:
    """Anti-spam delle notifiche (default 12h: 2 avvisi al giorno)."""
    return _num_env("TOURNAMENT_BOARD_NOTIFY_HOURS", 12.0, minimum=1.0)


def tournament_name() -> str:
    return os.getenv("TOURNAMENT_LABEL") or DEFAULT_TOURNAMENT


# ---------------------------------------------------------------------------
# HTTP (iniettabile: i test girano interamente OFFLINE)
# ---------------------------------------------------------------------------

def _get(path: str, *, http_get: Any = None,
         timeout: Optional[float] = None) -> Dict[str, Any]:
    """GET fail-safe su `api.sx.bet`. Non solleva MAI.

    Ritorna `{ok, status, payload, error}`: un errore di rete e' un valore,
    non un'eccezione — il job che lo chiama non deve poter morire per una
    lettura di classifica.
    """
    out: Dict[str, Any] = {"ok": False, "status": 0, "payload": None,
                           "error": None}
    try:
        if http_get is None:
            import requests                                  # import pigro
            http_get = requests.get
        url = f"{api_base()}{path}"
        resp = http_get(url,
                        headers={"User-Agent": user_agent(),
                                 "Accept": "application/json"},
                        timeout=timeout if timeout is not None
                        else request_timeout_s())
        status = int(getattr(resp, "status_code", 0) or 0)
        out["status"] = status
        if status != 200:
            out["error"] = f"HTTP {status}"
            return out
        out["payload"] = resp.json()
        out["ok"] = True
    except Exception as exc:                                  # fail-safe
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


# ---------------------------------------------------------------------------
# Normalizzazione (i payload SX cambiano forma fra gli endpoint)
# ---------------------------------------------------------------------------

def _tournaments_of(payload: Any) -> List[Dict[str, Any]]:
    """Estrae la lista tornei da un payload tollerante.

    `/tournaments/ongoing` e `/tournaments/upcoming` rispondono
    `{"data": {"tournaments": [...]}}`; un futuro cambio di forma non deve
    rendere la classifica illeggibile — si prova lista -> `tournaments` ->
    `data` (lista o dict) e si scarta tutto il resto.
    """
    if isinstance(payload, list):
        return [t for t in payload if isinstance(t, dict)]
    if isinstance(payload, dict):
        inner = payload.get("tournaments")
        if isinstance(inner, list):
            return [t for t in inner if isinstance(t, dict)]
        data = payload.get("data")
        if isinstance(data, list):
            return [t for t in data if isinstance(t, dict)]
        if isinstance(data, dict) and isinstance(data.get("tournaments"), list):
            return [t for t in data["tournaments"] if isinstance(t, dict)]
    return []


def _rows_of(payload: Any) -> List[Dict[str, Any]]:
    """Estrae la CLASSIFICA da `/tournaments/<id>`.

    La forma osservata e' `{"status": "success", "data": [ {rank, ...} ]}`:
    una lista di righe. Si accettano anche le chiavi `leaderboard`/`rows`.
    """
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in ("data", "leaderboard", "rows", "entries"):
            value = payload.get(key)
            if isinstance(value, list):
                return [r for r in value if isinstance(r, dict)]
    return []


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def normalize_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """Riga di classifica in forma stabile (mai KeyError sul chiamante).

    I campi reali dell'API sono `username, rank, totalResult,
    totalTradeCount, prize`: qui vengono solo rinominati e tipizzati, mai
    reinterpretati.
    """
    return {
        "username": str(row.get("username") or row.get("name") or "").strip(),
        "rank": _as_int(row.get("rank"), 0),
        "result": round(_as_float(row.get("totalResult")), 6),
        "trades": _as_int(row.get("totalTradeCount"), 0),
        "prize": round(_as_float(row.get("prize")), 2),
    }


def normalize_rows(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Classifica normalizzata e ordinata per posizione (rank crescente)."""
    out = [normalize_row(r) for r in rows if isinstance(r, dict)]
    out.sort(key=lambda r: (r["rank"] if r["rank"] > 0 else 10 ** 9))
    return out


def find_row(rows: Sequence[Dict[str, Any]],
             name: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """La riga dell'utente (confronto per nome, case-insensitive).

    `None` se il nome non compare: il chiamante deve poter distinguere
    "non presente in classifica" da "posizione zero".
    """
    wanted = (name or username()).strip().lower()
    if not wanted:
        return None
    for row in rows:
        if str(row.get("username") or "").strip().lower() == wanted:
            return dict(row)
    return None


def cutoff_row(rows: Sequence[Dict[str, Any]],
               rank: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """L'ultima riga premiata (default rank 30 = ultima posizione con prize).

    E' il numero che conta per il proprietario: con un formato RETURN la
    classifica non si "mantiene" come un P/L, si raggiunge una SOGLIA.
    """
    target = int(rank or top_ranks())
    best: Optional[Dict[str, Any]] = None
    best_rank = 0
    for row in rows:
        r = int(row.get("rank") or 0)
        if r <= 0:
            continue
        if r == target:
            return dict(row)                       # riga esatta del taglio
        if r < target and r > best_rank:           # ultima premiata sotto taglio
            best, best_rank = dict(row), r
    return best                                    # mai una posizione inventata


# ---------------------------------------------------------------------------
# Letture di alto livello
# ---------------------------------------------------------------------------

def fetch_tournaments(kind: str = "ongoing", *,
                      http_get: Any = None) -> List[Dict[str, Any]]:
    """Tornei `ongoing` (default) o `upcoming`. Lista vuota su errore."""
    res = _get(f"/tournaments/{kind}", http_get=http_get)
    if not res["ok"]:
        logger.info("tournament_leaderboard: /tournaments/%s -> %s",
                    kind, res.get("error"))
        return []
    return _tournaments_of(res.get("payload"))


def find_tournament(name: Optional[str] = None, *,
                    http_get: Any = None) -> Optional[Dict[str, Any]]:
    """Trova il torneo per NOME fra i tornei in corso (fallback: futuri).

    Il confronto e' per sottostringa case-insensitive: l'etichetta SX e'
    decorata (`🏈 $7,777 Gridiron Gauntlet 🏈`) e un confronto esatto non
    aggancerebbe mai. Se il nome non compare, si prova `upcoming` — cosi' il
    giorno dopo la chiusura il report dice "nessun torneo in corso" invece di
    sembrare rotto.
    """
    wanted = (name or tournament_name()).strip().lower()
    for kind in ("ongoing", "upcoming"):
        for tor in fetch_tournaments(kind, http_get=http_get):
            label = str(tor.get("name") or "").lower()
            if wanted and wanted in label:
                return dict(tor)
    return None


def fetch_leaderboard(tournament_id: str, *,
                      http_get: Any = None) -> List[Dict[str, Any]]:
    """Classifica normalizzata di un torneo. Lista vuota su errore."""
    if not tournament_id:
        return []
    res = _get(f"/tournaments/{tournament_id}", http_get=http_get)
    if not res["ok"]:
        logger.info("tournament_leaderboard: classifica di %s -> %s",
                    tournament_id, res.get("error"))
        return []
    return normalize_rows(_rows_of(res.get("payload")))


# ---------------------------------------------------------------------------
# Istantanea (con memo: il comando non martella l'API pubblica)
# ---------------------------------------------------------------------------

_CACHE: Dict[str, Any] = {"ts": 0.0, "snap": None}


def reset_cache() -> None:
    """Azzera il memo (isolamento nei test e refresh a richiesta)."""
    _CACHE["ts"] = 0.0
    _CACHE["snap"] = None


def snapshot(*, http_get: Any = None, use_cache: bool = True) -> Dict[str, Any]:
    """Istantanea completa della classifica. **Non solleva mai.**

    Ritorna (tutti i campi sempre presenti):
        ok, ts, cached, error,
        tournament  {id, name, status, type, start_date, end_date,
                     member_count}
        participants, me, cutoff, gap, in_prizes, days_left, top
    """
    out: Dict[str, Any] = {
        "ok": False, "ts": time.time(), "cached": False, "error": None,
        "tournament": None, "participants": 0, "me": None, "cutoff": None,
        "gap": None, "in_prizes": False, "days_left": None, "top": [],
        "username": username(), "top_ranks": top_ranks(),
    }
    try:
        if not board_enabled():
            out["error"] = "lettore spento (TOURNAMENT_BOARD=0)"
            return out
        if use_cache:
            age = time.time() - float(_CACHE.get("ts") or 0.0)
            cached = _CACHE.get("snap")
            if cached is not None and age <= cache_ttl_s():
                snap = dict(cached)
                snap["cached"] = True
                return snap
        tor = find_tournament(http_get=http_get)
        if not tor:
            out["error"] = f"torneo '{tournament_name()}' non trovato " \
                           f"(ongoing/upcoming)"
            return out
        tid = str(tor.get("tournamentId") or tor.get("id") or "")
        out["tournament"] = {
            "id": tid,
            "name": str(tor.get("name") or "").strip(),
            "status": str(tor.get("status") or "").strip(),
            "type": str(tor.get("type") or "").strip(),
            "start_date": tor.get("startDate"),
            "end_date": tor.get("endDate"),
            "member_count": _as_int(tor.get("memberCount"), 0),
        }
        rows = fetch_leaderboard(tid, http_get=http_get)
        out["participants"] = len(rows)
        if not rows:
            out["error"] = "classifica non leggibile (0 righe)"
            return out
        out["top"] = rows[:5]
        me = find_row(rows)
        out["me"] = me
        cut = cutoff_row(rows)
        out["cutoff"] = cut
        if me is not None and cut is not None:
            out["gap"] = round(max(cut["result"] - me["result"], 0.0), 6)
            out["in_prizes"] = (me["rank"] > 0
                                and me["rank"] <= top_ranks())
        out["days_left"] = _days_left(tor.get("endDate"))
        out["ok"] = True
        _CACHE["ts"] = time.time()
        _CACHE["snap"] = dict(out)
        return out
    except Exception as exc:                                  # fail-safe
        logger.warning("tournament_leaderboard: snapshot non disponibile (%s)",
                       exc)
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out


def _days_left(end_date: Any) -> Optional[float]:
    """Giorni residui alla chiusura (`endDate` ISO con fuso obbligatorio).

    Un istante ambiguo non viene indovinato: senza fuso la funzione ritorna
    None (lezione del 15/09 sulle date senza timezone).
    """
    try:
        import datetime as _dt
        raw = str(end_date or "").strip()
        if not raw:
            return None
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = _dt.datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            return None
        now = _dt.datetime.now(_dt.timezone.utc)
        return round(max((dt - now).total_seconds() / 86400.0, 0.0), 2)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Report (Telegram / CLI)
# ---------------------------------------------------------------------------

def format_report(snap: Optional[Dict[str, Any]] = None) -> str:
    """Testo della classifica per Telegram/CLI. Mai eccezioni."""
    try:
        s = dict(snap) if isinstance(snap, dict) else snapshot()
        tor = s.get("tournament") or {}
        name = tor.get("name") or tournament_name()
        lines = [f"🏆 CLASSIFICA — {name}"]
        if not s.get("ok"):
            lines.append(f"⚠️ non disponibile: {s.get('error')}")
            return "\n".join(lines)
        me = s.get("me")
        if not me:
            lines.append(f"⚠️ nessuna riga per '{s.get('username')}' "
                         f"({s.get('participants')} partecipanti in classifica)")
        else:
            rank = me.get("rank") or 0
            total = s.get("participants") or 0
            badge = "✅ in zona premi" if s.get("in_prizes") else "⏳ fuori zona premi"
            lines.append(f"• Posizione: **#{rank}** / {total}  ({badge})")
            lines.append(f"• Punteggio RETURN: **{me.get('result', 0):.2f}**")
            lines.append(f"• Trade: {me.get('trades', 0)}")
            if me.get("prize"):
                lines.append(f"• Premio: {me.get('prize'):.0f} USDC")
        cut = s.get("cutoff")
        if cut is not None:
            lines.append(f"• Taglio top {s.get('top_ranks')}: "
                         f"**{cut.get('result', 0):.2f}** "
                         f"(#{cut.get('rank')} {cut.get('username') or '—'})")
        if s.get("gap") is not None and not s.get("in_prizes"):
            lines.append(f"• Serve per entrare: **+{s.get('gap'):.2f}** "
                         f"di ritorno vincente")
        if s.get("days_left") is not None:
            lines.append(f"• Tempo rimasto: **{s.get('days_left'):.2f} giorni**")
        top = s.get("top") or []
        if top:
            best = top[0]
            lines.append(f"• Leader: {best.get('username') or '—'} "
                         f"{best.get('result', 0):.2f} "
                         f"({best.get('trades', 0)} trade)")
        lines.append("ℹ️ Formato RETURN: contano solo i ritorni delle puntate "
                     "VINTE, le perdite non tolgono punti — per scalare "
                     "servono vittorie, non volume.")
        if s.get("cached"):
            lines.append("(istantanea in memo)")
        return "\n".join(lines)
    except Exception as exc:                                  # fail-safe
        return f"🏆 CLASSIFICA — non disponibile ({exc})"


def main(argv: Optional[List[str]] = None) -> int:              # pragma: no cover
    import argparse
    ap = argparse.ArgumentParser(description="Classifica torneo SX Bet")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args(argv)
    snap = snapshot(use_cache=not args.no_cache)
    print(json.dumps(snap, indent=2, ensure_ascii=False) if args.json
          else format_report(snap))
    return 0 if snap.get("ok") else 1


if __name__ == "__main__":                                      # pragma: no cover
    raise SystemExit(main())
