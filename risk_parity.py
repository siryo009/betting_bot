"""risk_parity.py — M6 FASE 3: matrice di COVARIANZA + RISK PARITY (15/10/2026).

Direttiva del proprietario: bilanciare l'esposizione del portafoglio in base
alla **volatilita' e alla varianza storica dei singoli mercati/sport**, non solo
al valore atteso (+EV), cosi' che ogni posizione contribuisca in modo **equo e
controllato** al rischio complessivo del bankroll.

PERCHE' QUESTO NON E' UNA SECONDA FORMULA (regola di progetto: "ogni formula
vive in UN solo posto" — i tre bug gravi del progetto sono tutti logica copiata
in due posti):
- la **covarianza esatta** di gambe sulla STESSA partita viene da
  `portfolio_risk.correlation_matrix` (griglia Dixon-Coles, gia' misurata): qui
  non si ricopia, si legge dal ledger la varianza che il ledger PUO' dire;
- la **varianza storica per mercato/sport** viene da `bets` **saldate**
  (`profit`/`stake` = rendimento per unita' di stake): e' l'unica cosa che il
  ledger misura davvero, e non richiede nessuna fonte nuova ne' crediti;
- il de-vig, l'EV e l'edge restano dove sono (`value_filter`,
  `pinnacle_oracle`): questo modulo **non tocca il valore atteso**, agisce solo
  sulla DIMENSIONE.

DIREZIONE UNICA (come `copula_portfolio`): il moltiplicatore e' in **[0, 1]**.
Può solo **ridurre** lo stake di una posizione che pesa troppo sul rischio
totale, o **azzerarlo** quando la riduzione necessaria scende sotto il
pavimento. Mai un fattore > 1, mai una promozione: un modulo che alza lo stake
sarebbe una decisione di strategia, non un effetto di un'integrazione.

IL CRITERIO: **equi-risk-contribution** (ERC). Con pesi `w` e covarianza `S`, il
contributo al rischio della posizione `i` e' `RC_i = w_i (S w)_i / (w' S w)`;
il portafoglio e' "risk parity" quando `RC_i = 1/n` per tutte. Un candidato che
porta piu' del suo share equo (oltre `RISK_PARITY_MAX_SHARE_RATIO`) viene
ridimensionato — su una **griglia** di moltiplicatori (41 punti) e non per
soluzione analitica: la stessa lezione di `copula_portfolio` (l'RC share NON si
assume monotono nello stake).

Confini (tripwire nei test): sola LETTURA del ledger (`mode=ro`), zero rete,
zero crediti, **zero ordini**, nessuna scrittura, nessuna soglia di strategia
nel sorgente, `import risk_parity` non carica `auto_bet`/`bot`/`tracker`.

CLI:
    venv/bin/python risk_parity.py --variance [--days N] [--json]
    venv/bin/python risk_parity.py --portfolio [--bankroll N] [--json]
"""
from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configurazione (env letta a RUNTIME: invertire non richiede un redeploy)
# ---------------------------------------------------------------------------
DEFAULT_MODE = "enforce"
DEFAULT_MAX_SHARE_RATIO = 1.25      # +25% oltre lo share equo 1/n
DEFAULT_MIN_MULTIPLIER = 0.25       # sotto -> 0.0 (blocco, come la copula)
DEFAULT_MIN_SAMPLE = 8              # rendimenti chiusi per bucket
DEFAULT_DAYS = 90                   # finestra dello storico
DEFAULT_MIN_DAYS = 10               # giorni in comune per la covarianza extra-diag
DEFAULT_GRID = 41                   # punti della griglia dei moltiplicatori
DEFAULT_TTL_S = 600                 # memo della lettura (il giro gira ogni 60s)

# Sotto questa varianza il bucket e' praticamente deterministico e non si giudica
MIN_VARIANCE = 1e-6

# Bucket = (mercato, sport). Il mercato viene da `bets.mercato`; lo sport dal
# prefisso del `match_id` (le corsie scrivono identita' diverse per costruzione:
# `sx-tennis-*` per il tennis, `sx-esports-*` per gli eSports, `sx-*` per il
# resto su SX Bet). Un prefisso ignoto NON viene indovinato: finisce in "other".
LANES = (
    ("sx-tennis-", "tennis"),
    ("sx-esports-", "esports"),
    ("sx-", "sxbet"),
)

_MEMO: Dict[str, Any] = {"stamp": None, "data": None}


def _env_float(name: str, default: float, *, minimum: float = 0.0,
               maximum: Optional[float] = None) -> float:
    """Numero da env: valore impossibile -> default con warning (mai silenzioso)."""
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning("risk_parity: %s non numerico (%r): uso il default %s",
                       name, raw, default)
        return default
    if not math.isfinite(value) or value < minimum or (
            maximum is not None and value > maximum):
        logger.warning("risk_parity: %s fuori range (%s): uso il default %s",
                       name, raw, default)
        return default
    return value


def _env_int(name: str, default: int, *, minimum: int = 1,
             maximum: int = 100000) -> int:
    return int(_env_float(name, float(default), minimum=float(minimum),
                          maximum=float(maximum)))


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def config() -> Dict[str, Any]:
    """Configurazione efficace (una sola lettura per decisione)."""
    mode = (os.getenv("RISK_PARITY_MODE") or DEFAULT_MODE).strip().lower()
    if mode not in ("enforce", "shadow", "off"):
        logger.warning("risk_parity: modo sconosciuto %r: uso %s", mode,
                       DEFAULT_MODE)
        mode = DEFAULT_MODE
    enabled = _env_bool("RISK_PARITY_GUARD", True)
    return {
        "enabled": enabled,
        "mode": mode,
        "active": bool(enabled and mode != "off"),
        "max_share_ratio": _env_float("RISK_PARITY_MAX_SHARE_RATIO",
                                      DEFAULT_MAX_SHARE_RATIO, minimum=1.0),
        "min_multiplier": _env_float("RISK_PARITY_MIN_MULTIPLIER",
                                     DEFAULT_MIN_MULTIPLIER, minimum=0.0,
                                     maximum=1.0),
        "min_sample": _env_int("RISK_PARITY_MIN_SAMPLE", DEFAULT_MIN_SAMPLE,
                               minimum=2),
        "days": _env_int("RISK_PARITY_DAYS", DEFAULT_DAYS, minimum=1),
        "min_days": _env_int("RISK_PARITY_MIN_DAYS", DEFAULT_MIN_DAYS, minimum=2),
        "grid": _env_int("RISK_PARITY_GRID", DEFAULT_GRID, minimum=5,
                         maximum=201),
        "ttl_s": _env_float("RISK_PARITY_TTL_S", DEFAULT_TTL_S, minimum=0.0),
    }


def reset_cache() -> None:
    """Azzera la memo della lettura (test e diagnostica)."""
    _MEMO["stamp"] = None
    _MEMO["data"] = None


def _db_path() -> str:
    try:
        import tracker
        return str(tracker.DB_PATH)
    except Exception:                                     # pragma: no cover
        from config import DATA_DIR
        return str(Path(DATA_DIR) / "quotaverace.db")


def _ro_conn(db_path: Optional[str] = None) -> sqlite3.Connection:
    """Connessione in SOLA LETTURA (URI `mode=ro`): il modulo non scrive mai."""
    path = db_path or _db_path()
    conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# 1) Bucket e varianza storica dal ledger
# ---------------------------------------------------------------------------
def lane_of(match_id: Any) -> str:
    """Sport/corsia dal prefisso del `match_id` (ignoto -> "other", mai indovinato)."""
    mid = str(match_id or "").strip().lower()
    for prefix, lane in LANES:
        if mid.startswith(prefix):
            return lane
    return "other"


def bucket_of(market: Any, match_id: Any) -> str:
    """Chiave del bucket: `<MERCATO>|<sport>` (es. `1X2|sxbet`, `TENNIS|tennis`)."""
    return "%s|%s" % (str(market or "").upper().strip() or "?", lane_of(match_id))


def _closed_rows(conn: sqlite3.Connection, days: int) -> List[sqlite3.Row]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=int(days))
              ).strftime("%Y-%m-%dT%H:%M:%S")
    return list(conn.execute(
        "SELECT match_id, mercato, stake, profit, esito_finale, settled_at, mode "
        "FROM bets WHERE esito_finale IS NOT NULL AND profit IS NOT NULL "
        "AND stake > 0 AND datetime(settled_at) >= datetime(?)", (cutoff,)))


def unit_return(profit: Any, stake: Any) -> Optional[float]:
    """Rendimento per unita' di stake (il peso lo da' la colonna `stake`)."""
    try:
        s = float(stake)
        p = float(profit)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(s) or not math.isfinite(p) or s <= 0:
        return None
    return p / s


def bucket_stats(rows: Sequence[sqlite3.Row]) -> Dict[str, Dict[str, Any]]:
    """Varianza e ROI per unita' di stake, per bucket.

    La varianza e' CAMPIONARIA (ddof=1): con n piccolo la varianza di popolazione
    sottostima la dispersione, e qui la dispersione E' il oggetto della misura.
    Un bucket sotto `min_sample` viene dichiarato `insufficient` e non giudicato.
    """
    per_bucket: Dict[str, List[float]] = {}
    per_day: Dict[str, Dict[str, List[float]]] = {}
    for row in rows:
        r = unit_return(row["profit"], row["stake"])
        if r is None:
            continue
        key = bucket_of(row["mercato"], row["match_id"])
        per_bucket.setdefault(key, []).append(r)
        day = str(row["settled_at"] or "")[:10]
        if day:
            per_day.setdefault(day, {}).setdefault(key, []).append(r)
    out: Dict[str, Dict[str, Any]] = {}
    for key, values in per_bucket.items():
        n = len(values)
        arr = np.asarray(values, dtype=float)
        out[key] = {
            "bucket": key, "n": n,
            "mean": float(arr.mean()) if n else None,
            "variance": float(arr.var(ddof=1)) if n > 1 else None,
            "std": float(arr.std(ddof=1)) if n > 1 else None,
            "settled_span": None,
        }
    return out


def daily_matrix(rows: Sequence[sqlite3.Row]) -> Tuple[List[str], List[str],
                                                       np.ndarray]:
    """Matrice `giorni x bucket` dei P/L MEDI per unita' di stake.

    Aggregare per GIORNO e' l'unico modo di stimare la covarianza fra bucket
    diversi con questo ledger: due scommesse su mercati diversi non condividono
    nulla di osservabile al livello della singola bet, ma condividono il giorno
    (giornata di campionato, condizioni di mercato, liquidita').
    """
    per_day: Dict[str, Dict[str, List[float]]] = {}
    for row in rows:
        r = unit_return(row["profit"], row["stake"])
        if r is None:
            continue
        day = str(row["settled_at"] or "")[:10]
        if not day:
            continue
        key = bucket_of(row["mercato"], row["match_id"])
        per_day.setdefault(day, {}).setdefault(key, []).append(r)
    days = sorted(per_day)
    buckets = sorted({k for day in per_day.values() for k in day})
    matrix = np.full((len(days), len(buckets)), np.nan, dtype=float)
    for i, day in enumerate(days):
        for key, values in per_day[day].items():
            matrix[i, buckets.index(key)] = float(np.mean(values))
    return days, buckets, matrix


def covariance_matrix(rows: Sequence[sqlite3.Row], *, cfg: Optional[Dict] = None,
                      ) -> Dict[str, Any]:
    """Matrice di covarianza fra bucket: diagonale dalla varianza, extra-diag dal giorno.

    `pairwise_complete`: ogni coppia usa i giorni in cui ENTRAMBI i bucket hanno
    attivita' (niente imputazione: un giorno senza un bucket non e' uno zero).
    Sotto `min_days` giorni in comune la cella e' `None` e la coppia viene
    dichiarata non stimata — mai uno zero spacciato per indipendenza.
    """
    conf = cfg or config()
    out: Dict[str, Any] = {"buckets": [], "keys": [], "matrix": None,
                           "diag": {}, "pairs": {}, "rows": len(rows),
                           "error": None, "min_days": conf["min_days"]}
    stats = bucket_stats(rows)
    days, buckets, matrix = daily_matrix(rows)
    if not buckets:
        out["error"] = "nessun_bucket"
        return out
    n = len(buckets)
    cov = np.zeros((n, n), dtype=float)
    for i, key in enumerate(buckets):
        info = stats.get(key) or {}
        var = info.get("variance")
        if var is None:
            # Varianza non stimabile (n<=1): si ripiega sulla dispersione dei
            # giorni se c'e' (altrimenti la diagonale resta 0 = "ignoto").
            col = matrix[:, i]
            col = col[~np.isnan(col)]
            var = float(col.var(ddof=1)) if col.size > 1 else 0.0
        cov[i, i] = max(float(var or 0.0), 0.0)
        out["diag"][key] = cov[i, i]
    pairs: Dict[str, Any] = {}
    for i in range(n):
        for j in range(i + 1, n):
            both = matrix[:, [i, j]]
            both = both[~np.isnan(both).any(axis=1)]
            key = "%s~%s" % (buckets[i], buckets[j])
            if both.shape[0] < int(conf["min_days"]) or both.shape[0] < 2:
                pairs[key] = {"n_days": int(both.shape[0]), "cov": None,
                              "reason": "giorni_insufficienti"}
                continue
            c = float(np.cov(both[:, 0], both[:, 1], ddof=1)[0, 1])
            cov[i, j] = cov[j, i] = c
            pairs[key] = {"n_days": int(both.shape[0]), "cov": c, "reason": None}
    out["buckets"] = buckets
    out["keys"] = buckets
    out["matrix"] = cov.tolist()
    out["pairs"] = pairs
    out["stats"] = stats
    out["days"] = len(days)
    return out


def _memo_key() -> Tuple[Any, ...]:
    path = _db_path()
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = None
    return (path, mtime, tuple(sorted(
        (k, v) for k, v in os.environ.items() if k.startswith("RISK_PARITY_"))),
        int(datetime.now(timezone.utc).timestamp()
            // max(1, int(config()["ttl_s"] or 1))))


def variance_snapshot(*, conn: Optional[sqlite3.Connection] = None,
                      db_path: Optional[str] = None,
                      cfg: Optional[Dict] = None,
                      use_cache: bool = True) -> Dict[str, Any]:
    """Istantanea (memoizzata) della varianza/covarianza per bucket."""
    conf = cfg or config()
    key = _memo_key()
    if use_cache and _MEMO["stamp"] == key and _MEMO["data"] is not None:
        return dict(_MEMO["data"])
    own = conn is None
    out: Dict[str, Any] = {"error": None, "buckets": [], "stats": {},
                           "matrix": None, "keys": []}
    try:
        if own:
            conn = _ro_conn(db_path)
        rows = _closed_rows(conn, conf["days"])
        out.update(covariance_matrix(rows, cfg=conf))
    except Exception as e:                                # pragma: no cover
        logger.warning("risk_parity: lettura varianza non riuscita (%s)", e)
        out["error"] = "lettura_fallita"
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    if use_cache:
        _MEMO["stamp"] = key
        _MEMO["data"] = dict(out)
    return out


def variance_for(bucket: str, *, snapshot: Optional[Dict] = None,
                 cfg: Optional[Dict] = None) -> Dict[str, Any]:
    """Varianza di UN bucket, con la motivazione se non e' stimabile.

    Un bucket sotto il campione minimo non produce una varianza "debole": non
    produce una varianza. Chi chiama decide (qui: fail-open, nessuna riduzione
    su un dato che non c'e').
    """
    conf = cfg or config()
    snap = snapshot if snapshot is not None else variance_snapshot(cfg=conf)
    stats = (snap.get("stats") or {}).get(bucket) or {}
    n = int(stats.get("n") or 0)
    var = stats.get("variance")
    if snap.get("error"):
        return {"ok": False, "reason": "lettura_fallita", "n": n}
    if bucket not in (snap.get("buckets") or []):
        return {"ok": False, "reason": "bucket_sconosciuto", "n": n}
    if n < int(conf["min_sample"]):
        return {"ok": False, "reason": "campione_insufficiente", "n": n}
    if var is None or float(var) <= MIN_VARIANCE:
        return {"ok": False, "reason": "varianza_nulla", "n": n}
    return {"ok": True, "reason": "ok", "n": n, "variance": float(var),
            "std": float(stats.get("std") or math.sqrt(float(var))),
            "mean": stats.get("mean")}


# ---------------------------------------------------------------------------
# 2) Risk parity (ERC)
# ---------------------------------------------------------------------------
def risk_contributions(weights: Sequence[float],
                       cov: np.ndarray) -> Dict[str, Any]:
    """Contributi al rischio `RC_i = w_i (S w)_i / (w' S w)` (somma = 1).

    Con varianza totale 0 (nessun rischio misurabile) ritorna share uniformi:
    e' il caso degenere, non un errore — senza rischio non c'e' nulla da
    bilanciare.
    """
    w = np.asarray(weights, dtype=float)
    s = np.asarray(cov, dtype=float)
    if w.ndim != 1 or s.shape != (w.size, w.size):
        return {"ok": False, "reason": "dimensioni_incoerenti", "shares": None}
    if not np.isfinite(w).all() or not np.isfinite(s).all():
        return {"ok": False, "reason": "valori_non_finiti", "shares": None}
    total = float(w @ s @ w)
    if total <= 0 or w.size == 0:
        n = max(w.size, 1)
        return {"ok": True, "total": 0.0, "shares": [1.0 / n] * w.size,
                "abs": [0.0] * w.size, "degenerate": True}
    abs_rc = w * (s @ w)
    return {"ok": True, "total": total, "abs": abs_rc.tolist(),
            "shares": (abs_rc / total).tolist(), "degenerate": False}


def is_positive_definite(cov: np.ndarray) -> Tuple[bool, Optional[float]]:
    """La matrice e' una covarianza valida? (autovalore minimo > 0)."""
    s = np.asarray(cov, dtype=float)
    try:
        eig = float(np.linalg.eigvalsh((s + s.T) / 2.0).min())
    except Exception:
        return False, None
    return (eig > 0.0), eig


def risk_parity_weights(cov: np.ndarray, *, iterations: int = 200,
                        tol: float = 1e-10) -> Dict[str, Any]:
    """Pesi ERC (equi-risk-contribution) via aggiornamento moltiplicativo.

    Algoritmo di Spinu/cyclical: `w_i <- w_i * (target / RC_i)^0.5`, con target
    uniforme. Convergenza dichiarata (`iterations`, `tol`) e matrice
    **fail-closed**: se non e' definita positiva il modulo NON la "aggiusta" in
    silenzio (una covarianza non valida e' un dato da guardare, non da riparare).
    """
    s = np.asarray(cov, dtype=float)
    n = s.shape[0]
    if s.ndim != 2 or s.shape[0] != s.shape[1] or n == 0:
        return {"ok": False, "reason": "matrice_non_quadrata", "weights": None}
    ok_pd, eig = is_positive_definite(s)
    out: Dict[str, Any] = {"ok": False, "weights": None, "iterations": 0,
                           "error": 0.0, "min_eigenvalue": eig, "n": n}
    if not ok_pd:
        out["reason"] = "matrice_non_definita_positiva"
        return out
    w = np.ones(n, dtype=float) / n
    target = 1.0 / n
    for step in range(int(iterations)):
        rc = w * (s @ w)
        total = float(w @ s @ w)
        if total <= 0:
            out["reason"] = "varianza_totale_nulla"
            return out
        shares = rc / total
        if not np.isfinite(shares).all():
            out["reason"] = "shares_non_finite"
            return out
        err = float(np.max(np.abs(shares - target)))
        out["iterations"] = step + 1
        out["error"] = err
        if err <= tol:
            break
        ratio = np.where(shares > 0, target / np.clip(shares, 1e-12, None),
                         1e6)
        w = w * np.sqrt(ratio)
        w = w / w.sum()
    out.update({"ok": True, "weights": w.tolist()})
    rc = risk_contributions(w, s)
    out["shares"] = rc.get("shares")
    out["max_share_error"] = (max(abs(x - target) for x in rc["shares"])
                             if rc.get("shares") else None)
    return out


# ---------------------------------------------------------------------------
# 3) Applicazione ai candidati del percorso ordini (solo riduzione)
# ---------------------------------------------------------------------------
def _stake_of(cand: Dict[str, Any]) -> float:
    try:
        value = float(cand.get("stake") or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return value if math.isfinite(value) and value > 0 else 0.0


def _market_of(cand: Dict[str, Any]) -> str:
    return str(cand.get("mercato") or cand.get("market") or "").upper().strip()


def portfolio_units(candidates: Sequence[Dict[str, Any]],
                    open_positions: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Unita' del portafoglio: le posizioni APERTE + i candidati con stake > 0.

    Le posizioni aperte le legge il chiamante dal ledger (una sola definizione
    di "capitale impegnato" in tutto il progetto: `auto_bet._open_live_snapshot`).
    """
    units: List[Dict[str, Any]] = []
    for pos in open_positions or []:
        stake = _stake_of(pos)
        if stake <= 0:
            continue
        units.append({"stake": stake, "bucket": pos.get("bucket") or
                      bucket_of(pos.get("mercato") or pos.get("market"),
                                pos.get("match_id")),
                      "source": "open", "candidate": None})
    for cand in candidates or []:
        stake = _stake_of(cand)
        if stake <= 0:
            continue
        units.append({"stake": stake,
                      "bucket": bucket_of(_market_of(cand), cand.get("match_id")),
                      "source": "candidate", "candidate": cand})
    return units


def _cov_for(units: Sequence[Dict[str, Any]], variance: Dict[str, float],
             fallback: float) -> np.ndarray:
    """Covarianza diagonale delle unita' (assenza di correlazione = caso peggiore neutro).

    Perche' diagonale: la covarianza EXTRA-diagonale stimata dal ledger e' fra
    BUCKET (giorni condivisi), non fra le singole posizioni, e applicarla a due
    posizioni dello stesso bucket sarebbe un doppio conteggio. La dipendenza fra
    posizioni correlate e' gia' coperta da `copula_portfolio` (griglia esatta
    sulla stessa partita): qui si aggiunge la dimensione che mancava — il PESO
    relativo di mercati con volatilita' diversa.
    """
    n = len(units)
    cov = np.zeros((n, n), dtype=float)
    for i, unit in enumerate(units):
        var = variance.get(unit["bucket"])
        cov[i, i] = max(float(var if var is not None else fallback), MIN_VARIANCE)
    return cov


def _rc_share_of_portfolio(stakes: Sequence[float], cov: np.ndarray,
                           idx: int) -> Optional[float]:
    rc = risk_contributions(stakes, cov)
    if not rc.get("ok") or not rc.get("shares"):
        return None
    return float(rc["shares"][idx])


def _target_share(n: int, ratio: float) -> float:
    return (1.0 / max(n, 1)) * float(ratio)


def apply(candidates: List[Dict[str, Any]], *, bankroll: float,
          open_positions: Optional[Sequence[Dict[str, Any]]] = None,
          cfg: Optional[Dict] = None,
          snapshot: Optional[Dict[str, Any]] = None) -> Tuple[List[Dict], Dict]:
    """Riduce i candidati che pesano troppo sul rischio totale — MAI li alza.

    Ritorna `(candidati, info)`. Un candidato senza varianza stimabile nel suo
    bucket resta **invariato** (fail-open dichiarato: non si giudica un rischio
    che non si e' misurato). Qualunque errore lascia i candidati invariati.
    """
    conf = cfg or config()
    info: Dict[str, Any] = {"enabled": bool(conf["enabled"]),
                            "active": bool(conf["active"]), "mode": conf["mode"],
                            "kept": len(candidates or []), "scaled": 0,
                            "blocked": 0, "clear": 0, "skipped_no_sample": 0,
                            "by_reason": {}, "blocked_detail": [], "error": None,
                            "max_share_ratio": conf["max_share_ratio"],
                            "min_multiplier": conf["min_multiplier"]}
    if not conf["active"]:
        return list(candidates or []), info
    try:
        snap = snapshot if snapshot is not None else variance_snapshot(cfg=conf)
        if snap.get("error"):
            info["error"] = snap["error"]
            return list(candidates or []), info
        stats = snap.get("stats") or {}
        variance: Dict[str, float] = {}
        for key, row in stats.items():
            if int(row.get("n") or 0) >= int(conf["min_sample"]) and \
                    row.get("variance") is not None and \
                    float(row["variance"]) > MIN_VARIANCE:
                variance[key] = float(row["variance"])
        # Fallback per un bucket senza campione: la MEDIANA delle varianze note
        # (dichiarata) — mai 0, che direbbe "rischio nullo" a una posizione di
        # cui semplicemente non sappiamo nulla.
        fallback = float(np.median(list(variance.values()))) if variance else 0.0
        units = portfolio_units(candidates or [], open_positions or [])
        if not units:
            return list(candidates or []), info
        cov = _cov_for(units, variance, fallback)
        n = len(units)
        target = _target_share(n, conf["max_share_ratio"])
        grid = [i / (int(conf["grid"]) - 1) for i in range(int(conf["grid"]))]
        idx = {id(u["candidate"]): i for i, u in enumerate(units)
               if u["candidate"] is not None}
        for cand in candidates or []:
            i = idx.get(id(cand))
            if i is None:
                continue                       # stake 0 o non nel portafoglio
            stake = _stake_of(cand)
            bucket = units[i]["bucket"]
            decision: Dict[str, Any] = {
                "bucket": bucket, "stake_before": stake,
                "variance": variance.get(bucket), "target_share": target,
                "n_units": n, "share_before": None, "stake_after": stake,
                "multiplier": 1.0, "decision": "clear", "detail": ""}
            if bucket not in variance:
                info["skipped_no_sample"] = int(info["skipped_no_sample"]) + 1
                decision["decision"] = "no_sample"
                decision["detail"] = ("varianza non stimabile per %s (n %s < %s "
                                      "o varianza nulla)"
                                      % (bucket, (stats.get(bucket) or {}).get("n"),
                                         conf["min_sample"]))
                cand["risk_parity"] = decision
                continue
            share_before = _rc_share_of_portfolio(
                [u["stake"] for u in units], cov, i)
            decision["share_before"] = share_before
            if share_before is None or share_before <= target:
                info["clear"] = int(info["clear"]) + 1
                cand["risk_parity"] = decision
                continue
            # Griglia: il moltiplicatore piu' ALTO che riporta lo share entro il
            # target. Non si assume la monotonia (lezione di copula_portfolio).
            best = None
            stakes = [u["stake"] for u in units]
            for m in reversed(grid):
                if m <= 0:
                    continue
                probe = list(stakes)
                probe[i] = stake * m
                share = _rc_share_of_portfolio(probe, cov, i)
                if share is not None and share <= target:
                    best = m
                    break
            if best is None:
                best = 0.0
            new_stake = math.floor(stake * best * 100.0) / 100.0
            if 0 < best < float(conf["min_multiplier"]):
                best, new_stake = 0.0, 0.0
            if best <= 0:
                decision.update({"multiplier": 0.0, "stake_after": 0.0,
                                 "decision": "blocked",
                                 "detail": ("nessun ridimensionamento tiene lo "
                                            "share di %s entro %.3f (share "
                                            "attuale %.3f): ordine bloccato"
                                            % (bucket, target, share_before))})
                cand["stake"] = 0.0
                cand["risk_parity"] = decision
                info["blocked"] = int(info["blocked"]) + 1
                info["by_reason"]["blocked_risk_share"] = int(
                    info["by_reason"].get("blocked_risk_share", 0)) + 1
                info["blocked_detail"].append({
                    "match_id": cand.get("match_id"),
                    "esito": cand.get("esito_key") or cand.get("esito"),
                    "reason": "blocked_risk_share", "bucket": bucket,
                    "stake_before": stake, "share_before": share_before,
                    "detail": decision["detail"]})
            else:
                decision.update({"multiplier": best, "stake_after": new_stake,
                                 "decision": "scaled"})
                cand["stake"] = new_stake
                cand["risk_parity"] = decision
                info["scaled"] = int(info["scaled"]) + 1
                info["by_reason"]["scaled_risk_share"] = int(
                    info["by_reason"].get("scaled_risk_share", 0)) + 1
    except Exception as e:                                # pragma: no cover
        logger.warning("risk_parity: gate non applicato (%s): candidati "
                       "invariati", e)
        info["error"] = "errore_valutazione"
        return list(candidates or []), info
    info["kept"] = sum(1 for c in candidates or [] if _stake_of(c) > 0)
    return list(candidates or []), info


# ---------------------------------------------------------------------------
# 4) Report e CLI
# ---------------------------------------------------------------------------
def format_report(snap: Dict[str, Any], *, cfg: Optional[Dict] = None) -> str:
    """Report testuale (Telegram-friendly) della varianza per bucket."""
    conf = cfg or config()
    lines = ["⚖️ RISK PARITY — varianza storica per mercato/sport",
             "  guard %s | modo %s | campione min %d | finestra %dg"
             % ("ON" if conf["enabled"] else "OFF", conf["mode"],
                conf["min_sample"], conf["days"]),
             "  ratio share massimo %.2fx (equo = 1/n) | pavimento %.2f"
             % (conf["max_share_ratio"], conf["min_multiplier"])]
    if snap.get("error"):
        lines.append("  ⚠️ misura non disponibile: %s" % snap["error"])
        return "\n".join(lines)
    stats = snap.get("stats") or {}
    lines.append("  bucket stimati: %d su %d giorni di storia"
                 % (len(snap.get("buckets") or []), snap.get("days") or 0))
    for key in sorted(stats):
        row = stats[key]
        n = int(row.get("n") or 0)
        var = row.get("variance")
        if n < int(conf["min_sample"]) or var is None:
            lines.append("  · %-18s n=%3d  (campione insufficiente)" % (key, n))
            continue
        lines.append("  · %-18s n=%3d  dev.std %.3f  ROI medio %+6.1f%%"
                     % (key, n, math.sqrt(float(var)),
                        100.0 * float(row.get("mean") or 0.0)))
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in args
    use_cache = "--no-cache" not in args
    if "--variance" in args or "--portfolio" in args or not args:
        snap = variance_snapshot(use_cache=use_cache)
        if "--portfolio" in args:
            bankroll = 0.0
            for i, a in enumerate(args):
                if a == "--bankroll" and i + 1 < len(args):
                    bankroll = float(args[i + 1])
            try:
                import tracker
                conn = _ro_conn()
                rows = list(conn.execute(
                    "SELECT match_id, mercato, stake FROM bets WHERE "
                    "esito_finale IS NULL AND mode='live'"))
                conn.close()
                units = [{"match_id": r["match_id"],
                          "mercato": r["mercato"], "stake": float(r["stake"])}
                         for r in rows]
            except Exception:
                units = []
            snap = dict(snap, open_positions=units, bankroll=bankroll)
        print(json.dumps(snap, indent=2, default=str) if as_json
              else format_report(snap))
        return 0
    print("uso: risk_parity.py [--variance] [--portfolio] [--bankroll N] "
          "[--json] [--no-cache]")
    return 2


if __name__ == "__main__":                                # pragma: no cover
    raise SystemExit(main())
