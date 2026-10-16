"""dixon_coles.py — correzione Dixon-Coles con STIMA di rho e TIME-DECAY (15/10/2026).

Direttiva del proprietario: raffinare il modello Poisson del calcio con il
parametro **rho** (correzione dei punteggi bassi: 0-0, 1-1) e dare **peso
maggiore alle partite recenti** rispetto a quelle di inizio stagione.

PERCHE' QUESTO NON E' UNA SECONDA FORMULA (regola di progetto: "ogni formula vive
in UN solo posto" — i tre bug gravi del progetto sono tutti logica copiata in due
posti):
- la **griglia dei punteggi con Dixon-Coles** resta `poisson_engine._probs_grid`
  (vettorizzata, verificata da `test_poisson_vectorized.py`): qui non si
  ricostruisce la matrice, si passa un `rho` diverso per la durata del calcolo
  (`poisson_engine.rho_value`, context manager che RIPRISTINA sempre);
- gli **expected goals** restano `poisson_engine.expected_goals` (rating
  time-decay inclusi): nessuna ri-stima delle forze in attacco/difesa;
- il **time-decay** e' lo stesso dei rating: `rating_engine._weight` con la sua
  mezza vita (`HALF_LIFE_DAYS` = 100 giorni). Due meccanismi di decadimento
  diversi nello stesso progetto sarebbero due idee diverse di "recente".

COSA AGGIUNGE: il parametro rho era una COSTANTE (`poisson_engine.RHO = -0.15`,
scelta a mano). Qui viene **stimato dai dati** con massima verosimiglianza
PESATA nel tempo, sui `match_results` reali — zero crediti, zero rete.

COME si stima (e perche' e' economico): la correzione tocca SOLO le quattro
celle (0,0) (0,1) (1,0) (1,1). Per ogni partita si precalcolano le quattro
probabilita' di Poisson e il totale della griglia troncata; poi, per ogni rho
della griglia, la probabilita' dell'esito osservato si legge da quelle cinque
quantita' — nessuna griglia ricostruita per punto. La verosimiglianza e'
`sum_i w_i * log p_i(rho)` con `w_i` dal time-decay.

DIREZIONE DEL MODULO: il rho stimato viene salvato su volume e riportato;
l'applicazione al modello e' OPT-IN (`DIXON_COLES_APPLY=1`) e comunque
vincolata a una banda di sicurezza: un rho fuori banda e' un dato da guardare,
non un parametro da installare (fail-closed: resta il default). Perche' resta
opt-in anche adesso che il blocco di promozione ESISTE: dal 22/09 la strategia
e' congelata — un cambio di probabilita' del modello cambia gli EV di tutte le
corsie, quindi accendere l'applicazione e' una decisione del proprietario, non
un effetto collaterale del job.

IL BLOCCO CHE LEGGE LA STIMA (16/10/2026): `promote()` e' l'unico punto
d'ingresso del percorso fit -> salvataggio -> applicazione (usato dal job
`bot.dixon_coles_job` e dalla CLI `--promote`). Con l'interruttore spento il
ciclo gira lo stesso e la stima resta MISURA: nessuna corsia cambia.

Confini (tripwire nei test): sola LETTURA del ledger (`mode=ro`), zero rete,
zero crediti, **zero ordini**, nessuna scrittura sul ledger, nessuna soglia di
strategia nel sorgente, `import dixon_coles` non carica `auto_bet`/`bot`.

CLI:
    venv/bin/python dixon_coles.py --fit [--days N] [--json] [--save]
    venv/bin/python dixon_coles.py --promote [--days N] [--json]
    venv/bin/python dixon_coles.py --status
"""
from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configurazione (env letta a RUNTIME)
# ---------------------------------------------------------------------------
DEFAULT_DAYS = 365            # finestra dei match usati per la stima
DEFAULT_MAX_MATCHES = 3000    # tetto di righe (le piu' recenti): il fit e' O(n)
DEFAULT_MAX_GOALS = 10        # troncamento della griglia (come il modello)
DEFAULT_RHO_MIN = -0.30       # griglia di ricerca (il default del modello e' -0.15)
DEFAULT_RHO_MAX = 0.05
DEFAULT_RHO_STEP = 0.01
DEFAULT_REFINE_STEP = 0.002   # secondo passaggio attorno al minimo
# Banda di SICUREZZA per l'applicazione: oltre questi valori il rho stimato non
# viene installato (un parametro di correlazione fuori banda e' un sintomo di
# dati sporchi, non un miglioramento del modello).
DEFAULT_SAFE_MIN = -0.25
DEFAULT_SAFE_MAX = 0.02

MIN_MATCHES_FIT = 30          # sotto questo campione la stima non si fa

RHO_FILE_ENV = "DIXON_COLES_STATE"


def _env_float(name: str, default: float, *, minimum: Optional[float] = None,
               maximum: Optional[float] = None) -> float:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning("dixon_coles: %s non numerico (%r): uso %s", name, raw,
                       default)
        return default
    if not math.isfinite(value) or (minimum is not None and value < minimum) or \
            (maximum is not None and value > maximum):
        logger.warning("dixon_coles: %s fuori range (%s): uso %s", name, raw,
                       default)
        return default
    return value


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    return int(_env_float(name, float(default), minimum=float(minimum)))


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def config() -> Dict[str, Any]:
    """Configurazione efficace (una sola lettura per decisione)."""
    return {
        "days": _env_int("DIXON_COLES_DAYS", DEFAULT_DAYS, minimum=1),
        "max_matches": _env_int("DIXON_COLES_MAX_MATCHES", DEFAULT_MAX_MATCHES,
                                minimum=10),
        "max_goals": _env_int("DIXON_COLES_MAX_GOALS", DEFAULT_MAX_GOALS,
                              minimum=2),
        "rho_min": _env_float("DIXON_COLES_RHO_MIN", DEFAULT_RHO_MIN),
        "rho_max": _env_float("DIXON_COLES_RHO_MAX", DEFAULT_RHO_MAX),
        "rho_step": _env_float("DIXON_COLES_RHO_STEP", DEFAULT_RHO_STEP,
                               minimum=1e-4),
        "refine_step": _env_float("DIXON_COLES_REFINE_STEP", DEFAULT_REFINE_STEP,
                                  minimum=1e-5),
        "safe_min": _env_float("DIXON_COLES_SAFE_MIN", DEFAULT_SAFE_MIN),
        "safe_max": _env_float("DIXON_COLES_SAFE_MAX", DEFAULT_SAFE_MAX),
        "half_life": _env_float("DIXON_COLES_HALF_LIFE_DAYS", 0.0, minimum=0.0),
        "apply": _env_bool("DIXON_COLES_APPLY", False),
    }


def default_rho() -> float:
    """Il default del modello (UNA definizione: quella di `poisson_engine`).

    La stima e il gate di calibrazione confrontano sempre col DEFAULT DI CODICE,
    non col valore installato in questo momento: altrimenti, dopo una
    promozione, un secondo fit misurerebbe il miglioramento contro il parametro
    appena applicato invece che contro il riferimento del modello.
    """
    try:
        import poisson_engine
        return float(getattr(poisson_engine, "DEFAULT_RHO", poisson_engine.RHO))
    except Exception:                                         # pragma: no cover
        return -0.15


def state_path() -> Path:
    """Dove vive il rho stimato (volume, sopravvive ai redeploy)."""
    raw = os.getenv(RHO_FILE_ENV)
    if raw and str(raw).strip():
        return Path(raw)
    try:
        from config import DATA_DIR
        return Path(DATA_DIR) / "decision" / "dixon_coles_rho.json"
    except Exception:                                         # pragma: no cover
        return Path("data/decision/dixon_coles_rho.json")


# ---------------------------------------------------------------------------
# 1) Lettura dei match e pesi temporali (delega: nessun decadimento nuovo)
# ---------------------------------------------------------------------------
def _db_path() -> str:
    try:
        import tracker
        return str(tracker.DB_PATH)
    except Exception:                                         # pragma: no cover
        from config import DATA_DIR
        return str(Path(DATA_DIR) / "quotaverace.db")


def _ro_conn(db_path: Optional[str] = None) -> sqlite3.Connection:
    """Connessione in SOLA LETTURA (URI `mode=ro`): il modulo non scrive mai."""
    conn = sqlite3.connect("file:%s?mode=ro" % (db_path or _db_path()), uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def half_life_days() -> float:
    """Mezza vita del time-decay: quella dei RATING (una sola definizione).

    `DIXON_COLES_HALF_LIFE_DAYS` (default 0 = non impostata) permette di
    sperimentare un decadimento diverso senza toccare il codice; assente o non
    valida vale `rating_engine.HALF_LIFE_DAYS`.
    """
    conf = config()
    if conf["half_life"] and conf["half_life"] > 0:
        return float(conf["half_life"])
    try:
        import rating_engine
        return float(rating_engine.HALF_LIFE_DAYS)
    except Exception:                                         # pragma: no cover
        return 100.0


def time_weight(days_ago: Any, *, half_life: Optional[float] = None) -> float:
    """Peso di una partita in base alla sua eta' (stessa curva dei rating)."""
    try:
        days = max(0.0, float(days_ago))
    except (TypeError, ValueError):
        days = 0.0
    hl = float(half_life or half_life_days())
    if hl <= 0:
        return 1.0
    try:
        import rating_engine
        if abs(hl - float(rating_engine.HALF_LIFE_DAYS)) < 1e-9:
            return float(rating_engine._weight(days))
    except Exception:
        pass
    return math.exp(-math.log(2) * days / hl)


def _parse_ts(ts: Any) -> Optional[datetime]:
    try:
        d = datetime.fromisoformat(str(ts or "").replace("Z", "+00:00"))
    except Exception:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d


def load_matches(*, days: Optional[int] = None, max_matches: Optional[int] = None,
                 conn: Optional[sqlite3.Connection] = None,
                 now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Match conclusi con punteggio, dal piu' recente, con il loro peso.

    Solo partite con entrambi i punteggi: una riga senza risultato non porta
    informazione su rho e non va contata (mai una riga \"riparata\" a zero).
    """
    conf = config()
    days = int(days if days is not None else conf["days"])
    limit = int(max_matches if max_matches is not None else conf["max_matches"])
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    own = conn is None
    out: List[Dict[str, Any]] = []
    try:
        if own:
            conn = _ro_conn()
        rows = list(conn.execute(
            "SELECT league, home_team, away_team, score_home, score_away, "
            "settled_at FROM match_results "
            "WHERE score_home IS NOT NULL AND score_away IS NOT NULL "
            "AND datetime(settled_at) >= datetime(?) "
            "ORDER BY datetime(settled_at) DESC LIMIT ?", (cutoff, limit)))
        hl = half_life_days()
        for row in rows:
            try:
                sh, sa = int(row["score_home"]), int(row["score_away"])
            except (TypeError, ValueError):
                continue
            if sh < 0 or sa < 0 or sh > 20 or sa > 20:
                continue
            ts = _parse_ts(row["settled_at"])
            days_ago = (now - ts).total_seconds() / 86400.0 if ts else 0.0
            out.append({"league": row["league"], "home": row["home_team"],
                        "away": row["away_team"], "score_home": sh,
                        "score_away": sa, "days_ago": days_ago,
                        "weight": time_weight(days_ago, half_life=hl)})
    except Exception as e:
        logger.warning("dixon_coles: lettura match non riuscita (%s)", e)
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return out


# ---------------------------------------------------------------------------
# 2) Verosimiglianza (la griglia resta quella del modello)
# ---------------------------------------------------------------------------
def _poisson_cell_pmf(lam: float, k: int) -> float:
    try:
        return float(math.exp(-lam) * lam ** k / math.factorial(k))
    except Exception:
        return 0.0


def match_terms(matches: Sequence[Dict[str, Any]], *,
                max_goals: int = DEFAULT_MAX_GOALS,
                expected_goals_fn=None) -> Dict[str, Any]:
    """Precalcola, per ogni match, cio' che serve alla verosimiglianza di rho.

    La correzione tocca SOLO le quattro celle (0,0) (0,1) (1,0) (1,1): per ogni
    partita bastano le loro probabilita' di Poisson, il totale della griglia
    troncata e la probabilita' di Poisson dell'esito osservato. Cosi' il costo
    per punto della griglia di rho e' O(n) e non O(n * K^2).
    """
    if expected_goals_fn is None:
        from poisson_engine import expected_goals as expected_goals_fn  # noqa
    n = len(matches)
    p00 = np.zeros(n, dtype=float)
    p01 = np.zeros(n, dtype=float)
    p10 = np.zeros(n, dtype=float)
    p11 = np.zeros(n, dtype=float)
    p_obs = np.zeros(n, dtype=float)
    totals = np.ones(n, dtype=float)
    weights = np.zeros(n, dtype=float)
    lams = np.zeros((n, 2), dtype=float)
    kept: List[int] = []
    cache: Dict[Tuple[str, str], Tuple[float, float]] = {}
    for i, m in enumerate(matches):
        key = (str(m.get("home") or ""), str(m.get("away") or ""))
        if key not in cache:
            try:
                lam_h, lam_a = expected_goals_fn(*key)
            except Exception:
                lam_h = lam_a = float("nan")
            cache[key] = (float(lam_h), float(lam_a))
        lam_h, lam_a = cache[key]
        if not (math.isfinite(lam_h) and math.isfinite(lam_a)) or \
                lam_h <= 0 or lam_a <= 0:
            continue
        ph = [_poisson_cell_pmf(lam_h, k) for k in range(max_goals + 1)]
        pa = [_poisson_cell_pmf(lam_a, k) for k in range(max_goals + 1)]
        sh, sa = int(m["score_home"]), int(m["score_away"])
        if sh > max_goals or sa > max_goals:
            continue                     # fuori dalla griglia troncata
        p00[i] = ph[0] * pa[0]
        p01[i] = ph[0] * pa[1]
        p10[i] = ph[1] * pa[0]
        p11[i] = ph[1] * pa[1]
        p_obs[i] = ph[sh] * pa[sa]
        totals[i] = sum(ph) * sum(pa)
        weights[i] = float(m.get("weight") or 0.0)
        lams[i] = (lam_h, lam_a)
        kept.append(i)
    return {"index": kept, "p00": p00, "p01": p01, "p10": p10, "p11": p11,
            "p_obs": p_obs, "totals": totals, "weights": weights, "lams": lams,
            "scores_h": np.asarray([int(m["score_home"]) for m in matches],
                                    dtype=int),
            "scores_a": np.asarray([int(m["score_away"]) for m in matches],
                                    dtype=int),
            "max_goals": int(max_goals)}


def observed_cell_factor(terms: Dict[str, Any], rho: float,
                         index: Optional[Sequence[int]] = None) -> np.ndarray:
    """Fattore Dixon-Coles della cella OSSERVATA (1.0 se la cella non e' corretta).

    Le quattro celle corrette si distinguono per il PUNTEGGIO osservato, non per
    la probabilita': con `lam_h == lam_a` le pmf di 0-1 e 1-0 coincidono e un
    confronto sulle probabilita' applicherebbe il fattore sbagliato.
    """
    k = np.asarray(list(index) if index is not None else terms["index"],
                   dtype=int)
    sh, sa = terms["scores_h"][k], terms["scores_a"][k]
    lam_h, lam_a = terms["lams"][k, 0], terms["lams"][k, 1]
    factor = np.ones_like(sh, dtype=float)
    factor = np.where((sh == 0) & (sa == 0), 1.0 - lam_h * lam_a * rho, factor)
    factor = np.where((sh == 0) & (sa == 1), 1.0 + lam_h * rho, factor)
    factor = np.where((sh == 1) & (sa == 0), 1.0 + lam_a * rho, factor)
    factor = np.where((sh == 1) & (sa == 1), 1.0 - rho, factor)
    return factor


def log_likelihood(terms: Dict[str, Any], rho: float) -> float:
    """Verosimiglianza LOGARITMICA pesata per un rho (nessuna griglia ricostruita).

    Un rho che rende una cella non valida (fattore <= 0) su una partita esclude
    QUELLA partita dal conteggio e lo dichiara nel valore di ritorno indiretto
    (`-inf` se tutte le partite diventano invalide): non si corregge un fattore
    negativo in silenzio.

    ⚠️ La maschera parziale (alcune partite valide, altre no) e' il caso che la
    stima attraversa DAVVERO sulla griglia di produzione: va coperta dai test,
    non solo il caso estremo "tutte invalide".
    """
    idx = terms["index"]
    if not idx:
        return float("-inf")
    k = np.asarray(idx, dtype=int)
    lam_h = terms["lams"][k, 0]
    lam_a = terms["lams"][k, 1]
    f00 = 1.0 - lam_h * lam_a * rho
    f01 = 1.0 + lam_h * rho
    f10 = 1.0 + lam_a * rho
    f11 = np.full(np.shape(lam_h), 1.0 - rho, dtype=float)
    if np.any(f00 <= 0) or np.any(f01 <= 0) or np.any(f10 <= 0) or \
            np.any(f11 <= 0):
        # Un fattore <= 0 renderebbe la cella non valida: la partita esce dal
        # conteggio (mai un log di un numero non positivo "riparato" a zero).
        valid = (f00 > 0) & (f01 > 0) & (f10 > 0) & (f11 > 0)
        if not np.any(valid):
            return float("-inf")
        k = k[valid]
        f00, f01, f10, f11 = f00[valid], f01[valid], f10[valid], f11[valid]
    p00, p01, p10, p11 = (terms["p00"][k], terms["p01"][k],
                          terms["p10"][k], terms["p11"][k])
    p_obs, totals = terms["p_obs"][k], terms["totals"][k]
    weights = terms["weights"][k]
    # NOTA (16/10/2026): `f11` e' un ARRAY, non uno scalare. Con un solo
    # scalare la maschera qui sopra (`f11[valid]`) sollevava
    # `TypeError: 'float' object is not subscriptable` appena UNA partita
    # andava fuori validita' — e la stima moriva su dati reali (la griglia di
    # produzione parte da rho -0.30, dove un favorito forte la invalida).
    corr = observed_cell_factor(terms, rho, index=k)
    # Probabilita' NON normalizzata della griglia corretta: le quattro celle
    # corrette piu' il resto, che la correzione non tocca.
    rest = totals - (p00 + p01 + p10 + p11)
    denom = p00 * f00 + p01 * f01 + p10 * f10 + p11 * f11 + rest
    with np.errstate(divide="ignore", invalid="ignore"):
        ll = np.log(p_obs * corr / denom)
    ll = np.where(np.isfinite(ll), ll, -700.0)
    return float(np.sum(weights * ll))


def fit_rho(matches: Sequence[Dict[str, Any]], *,
            rho_min: Optional[float] = None, rho_max: Optional[float] = None,
            step: Optional[float] = None, refine_step: Optional[float] = None,
            max_goals: Optional[int] = None,
            expected_goals_fn=None) -> Dict[str, Any]:
    """Stima rho con massima verosimiglianza PESATA nel tempo (griglia + refine).

    Ritorna `{ok, rho, loglik, loglik_default, n_matches, n_used, half_life,
    step, refine_step, safe, reason, elapsed_s}`. La stima si fa su una griglia
    (non su un ottimizzatore): la verosimiglianza di rho ha una forma benigna e
    una griglia e' ispezionabile e riproducibile — e il secondo passaggio
    attorno al minimo recupera la precisione.
    """
    conf = config()
    rho_min = float(conf["rho_min"] if rho_min is None else rho_min)
    rho_max = float(conf["rho_max"] if rho_max is None else rho_max)
    step = float(conf["rho_step"] if step is None else step)
    refine_step = float(conf["refine_step"] if refine_step is None
                        else refine_step)
    max_goals = int(conf["max_goals"] if max_goals is None else max_goals)
    out: Dict[str, Any] = {"ok": False, "rho": None, "loglik": None,
                           "loglik_default": None, "n_matches": len(matches or []),
                           "n_used": 0, "half_life": half_life_days(),
                           "step": step, "refine_step": refine_step,
                           "safe": None, "reason": None, "elapsed_s": None,
                           "improvement": None}
    t0 = time.time()
    if len(matches or []) < MIN_MATCHES_FIT:
        out["reason"] = "campione_insufficiente"
        return out
    if rho_min >= rho_max or step <= 0:
        out["reason"] = "griglia_non_valida"
        return out
    try:
        terms = match_terms(matches, max_goals=max_goals,
                            expected_goals_fn=expected_goals_fn)
        out["n_used"] = len(terms["index"])
        if out["n_used"] < MIN_MATCHES_FIT:
            out["reason"] = "campione_utilizzabile_insufficiente"
            return out
        grid = np.arange(rho_min, rho_max + step / 2.0, step)
        scores = [log_likelihood(terms, float(r)) for r in grid]
        best_i = int(np.argmax(scores))
        best_rho, best_ll = float(grid[best_i]), float(scores[best_i])
        # Secondo passaggio: griglia fine attorno al massimo (stessa funzione)
        fine = np.arange(best_rho - step, best_rho + step + refine_step / 2.0,
                         refine_step)
        fine_scores = [log_likelihood(terms, float(r)) for r in fine]
        j = int(np.argmax(fine_scores))
        if float(fine_scores[j]) >= best_ll:
            best_rho, best_ll = float(fine[j]), float(fine_scores[j])
        out.update({"ok": True, "rho": round(best_rho, 6),
                    "loglik": best_ll, "reason": "ok",
                    "grid_best": round(best_rho, 6),
                    "n_points": int(len(grid) + len(fine))})
        default_ll = log_likelihood(terms, default_rho())
        out["loglik_default"] = default_ll
        out["default_rho"] = default_rho()
        if math.isfinite(default_ll) and math.isfinite(best_ll):
            out["improvement"] = best_ll - default_ll
        out["safe"] = bool(float(conf["safe_min"]) <= best_rho <=
                           float(conf["safe_max"]))
    except Exception as e:                                    # pragma: no cover
        logger.warning("dixon_coles: stima rho non riuscita (%s)", e)
        out["reason"] = "errore_stima"
    finally:
        out["elapsed_s"] = round(time.time() - t0, 3)
    return out


# ---------------------------------------------------------------------------
# 3) Persistenza e applicazione (opt-in, con banda di sicurezza)
# ---------------------------------------------------------------------------
def save_fit(fit: Dict[str, Any], *, path: Optional[Path] = None) -> bool:
    """Salva la stima su volume (scrittura atomica). Fail-safe: mai eccezioni."""
    target = Path(path or state_path())
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {k: v for k, v in (fit or {}).items()}
        payload["saved_at"] = datetime.now(timezone.utc).isoformat()
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, default=str),
                       encoding="utf-8")
        os.replace(tmp, target)
        return True
    except Exception as e:
        logger.warning("dixon_coles: salvataggio stima non riuscito (%s)", e)
        return False


def load_fit(*, path: Optional[Path] = None) -> Dict[str, Any]:
    """Legge la stima salvata. File assente/corrotto -> `{ok: False, reason}`."""
    target = Path(path or state_path())
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"ok": False, "reason": "nessuna_stima"}
    except Exception as e:
        logger.warning("dixon_coles: stima illeggibile (%s)", e)
        return {"ok": False, "reason": "stima_illeggibile"}
    if not isinstance(data, dict) or data.get("rho") is None:
        return {"ok": False, "reason": "stima_malformata"}
    return data


def apply_fitted(*, path: Optional[Path] = None) -> Dict[str, Any]:
    """Installa il rho stimato nel modello — SOLO se `DIXON_COLES_APPLY=1`.

    Tre guardie, tutte fail-closed (il default del modello resta intatto):
    1. l'interruttore deve essere ESPLICITO (`DIXON_COLES_APPLY`, default OFF);
    2. la stima deve esistere ed essere `ok`;
    3. il valore deve stare nella BANDA DI SICUREZZA (`safe_min`/`safe_max`).
    """
    conf = config()
    out: Dict[str, Any] = {"applied": False, "reason": None, "rho": None,
                           "previous": None}
    if not conf["apply"]:
        out["reason"] = "interruttore_off"
        return out
    fit = load_fit(path=path)
    if not fit.get("ok") or fit.get("rho") is None:
        out["reason"] = fit.get("reason") or "stima_non_disponibile"
        return out
    rho = float(fit["rho"])
    if not (float(conf["safe_min"]) <= rho <= float(conf["safe_max"])):
        out["reason"] = "fuori_banda_di_sicurezza"
        out["rho"] = rho
        return out
    try:
        import poisson_engine
        out["previous"] = float(poisson_engine.RHO)
        poisson_engine.RHO = rho
        out.update({"applied": True, "reason": "ok", "rho": rho})
        logger.info("dixon_coles: RHO applicato %.4f (era %.4f)", rho,
                    out["previous"])
    except Exception as e:                                    # pragma: no cover
        logger.warning("dixon_coles: applicazione non riuscita (%s)", e)
        out["reason"] = "errore_applicazione"
    return out


# ---------------------------------------------------------------------------
# 2-bis) GATE DI CALIBRAZIONE: la verosimiglianza NON e' il criterio del denaro
# ---------------------------------------------------------------------------
# MISURA DEL 16/10/2026 sul ledger reale (392 match, container AMS):
#   rho          log-lik      Brier 1X2
#   -0.30        -957.27      0.5506        <- massimo della verosimiglianza
#   -0.20       -1010.05      0.5466
#   -0.15 (default) -1024.79  0.5451
#    0.00       -1052.71      0.5425        <- Brier MIGLIORE
# Osservato: 1 52.6% | X 21.6% | 2 25.9% — il modello al default prevede GIA'
# il 25.3% di pareggi, quindi un rho piu' negativo (che alza il pareggio)
# PEGGIORA la calibrazione. La verosimiglianza dei punteggi esatti premia la
# correzione delle celle basse, ma il denaro sta sui mercati (1X2, OU, AH):
# massimizzare una cosa e migliorare l'altra sono due domande diverse.
#
# Da qui il gate: il rho stimato viene APPLICATO solo se migliora (o almeno non
# peggiora) il Brier 1X2 FUORI CAMPIONE — valutato su una parte recente di
# match che non partecipa alla scelta del valore.
HOLD_OUT_SHARE = 0.30         # quota piu' recente usata per la verifica
MIN_HOLDOUT = 50              # sotto questo campione la verifica non conclude
DEFAULT_CALIB_TOL = 0.0       # guadagno minimo richiesto (0 = "non peggiore")


def brier_1x2(matches: Sequence[Dict[str, Any]], rho: float, *,
              expected_goals_fn=None, max_goals: Optional[int] = None) -> Optional[float]:
    """Brier 1X2 PESATO (time-decay) alle probabilita' del MOTORE per un rho.

    La probabilita' viene da `poisson_engine.prob_1x2` con il `rho` passato e
    RIPRISTINATO dal context manager del modello: qui non si ricostruisce
    nessuna griglia e nessuna formula. Pesa col `weight` (time-decay dei
    rating) e normalizza sulla somma dei pesi, cosi' il valore e' confrontabile
    fra campioni diversi. `None` se non c'e' niente di misurabile.
    """
    import poisson_engine as pe
    if expected_goals_fn is None:
        expected_goals_fn = pe.expected_goals
    max_goals = int(max_goals or config()["max_goals"])
    cache: Dict[Tuple[str, str], Tuple[float, float]] = {}
    num = 0.0
    den = 0.0
    with pe.rho_value(float(rho)):
        for m in matches or []:
            try:
                sh, sa = int(m["score_home"]), int(m["score_away"])
            except (KeyError, TypeError, ValueError):
                continue
            if sh < 0 or sa < 0 or sh > max_goals or sa > max_goals:
                continue
            key = (str(m.get("home") or ""), str(m.get("away") or ""))
            if key not in cache:
                try:
                    lam_h, lam_a = expected_goals_fn(*key)
                except Exception:
                    lam_h = lam_a = float("nan")
                cache[key] = (float(lam_h), float(lam_a))
            lam_h, lam_a = cache[key]
            if not (math.isfinite(lam_h) and math.isfinite(lam_a)) or \
                    lam_h <= 0 or lam_a <= 0:
                continue
            p1, px, p2 = pe.prob_1x2(lam_h, lam_a, max_goals=max_goals)
            w = float(m.get("weight") or 0.0)
            if w <= 0:
                continue
            y1 = 1.0 if sh > sa else 0.0
            yx = 1.0 if sh == sa else 0.0
            y2 = 1.0 if sh < sa else 0.0
            num += w * ((p1 - y1) ** 2 + (px - yx) ** 2 + (p2 - y2) ** 2)
            den += w
    return (num / den) if den > 0 else None


def holdout_verdict(matches: Sequence[Dict[str, Any]], rho: float, *,
                    expected_goals_fn=None) -> Dict[str, Any]:
    """Il rho stimato MIGLIORA la calibrazione 1X2 fuori campione?

    Verifica walk-forward senza look-ahead: la parte PIU' RECENTE dei match
    (`HOLD_OUT_SHARE`) fa da banco di prova; il confronto e' contro il default
    del modello E contro rho = 0 (Poisson puro), cosi' un valore che peggiora
    entrambi viene rifiutato invece di essere installato "perche' piu' probabile".

    Fail-closed: campione di verifica troppo piccolo, Brier non calcolabile o
    guadagno sotto `DIXON_COLES_CALIB_MIN_GAIN` -> `ok = False` (e il modello
    resta al default).
    """
    conf = config()
    share = _env_float("DIXON_COLES_HOLDOUT_SHARE", HOLD_OUT_SHARE,
                       minimum=0.05, maximum=0.95)
    min_holdout = _env_int("DIXON_COLES_MIN_HOLDOUT", MIN_HOLDOUT, minimum=10)
    tol = _env_float("DIXON_COLES_CALIB_MIN_GAIN", DEFAULT_CALIB_TOL)
    ordered = sorted(matches or [],
                     key=lambda m: float(m.get("days_ago") or 0.0))
    n = len(ordered)
    out: Dict[str, Any] = {"ok": False, "reason": None, "n_holdout": 0,
                           "brier_fitted": None, "brier_default": None,
                           "brier_zero": None, "gain_vs_default": None,
                           "share": share, "min_gain": tol}
    if n < 2:
        out["reason"] = "campione_insufficiente"
        return out
    k = max(min_holdout, int(round(n * share)))
    if k >= n:                     # serve almeno una riga fuori dal banco
        k = n - 1
    if k < min_holdout:
        out["reason"] = "holdout_insufficiente"
        return out
    holdout = ordered[:k]          # i piu' recenti
    out["n_holdout"] = k
    b_fit = brier_1x2(holdout, float(rho), expected_goals_fn=expected_goals_fn)
    b_def = brier_1x2(holdout, default_rho(), expected_goals_fn=expected_goals_fn)
    b_zero = brier_1x2(holdout, 0.0, expected_goals_fn=expected_goals_fn)
    out.update({"brier_fitted": b_fit, "brier_default": b_def,
                "brier_zero": b_zero})
    if b_fit is None or b_def is None or b_zero is None:
        out["reason"] = "brier_non_calcolabile"
        return out
    out["gain_vs_default"] = b_def - b_fit
    if b_fit > b_def + tol:
        out["reason"] = "non_migliora_default"
        return out
    if b_fit > b_zero + tol:
        out["reason"] = "non_migliora_rho_zero"
        return out
    out["ok"] = True
    out["reason"] = "ok"
    return out


# ---------------------------------------------------------------------------
# 3-bis) PROMOZIONE: fit -> salvataggio -> applicazione (UN punto d'ingresso)
# ---------------------------------------------------------------------------
def engine_rho() -> Optional[float]:
    """RHO effettivo del motore in questo istante (diagnostica, mai eccezioni).

    Serve al riepilogo della promozione: il log deve dire il rho del MOTORE
    anche quando l'applicazione non avviene, altrimenti "non applicato" resta
    una frase senza il dato che conta.
    """
    try:
        import poisson_engine
        return float(poisson_engine.current_rho())
    except Exception:                                         # pragma: no cover
        return None


def promote(*, path: Optional[Path] = None,
            matches: Optional[Sequence[Dict[str, Any]]] = None,
            now: Optional[datetime] = None,
            expected_goals_fn=None) -> Dict[str, Any]:
    """Ciclo completo nel percorso di produzione: stima, salva, (se autorizzato) applica.

    I tre passi restano separati sotto (`fit_rho`/`save_fit`/`apply_fitted`)
    perche' ognuno ha anche un uso proprio (diagnostica, CLI `--fit`), ma la
    PROMOZIONE li incatena in quest'ordine e nessuno puo' applicare un rho che
    non sia stato stimato su questi dati.

    Fail-closed in quattro punti (ciascuno coperto da un test):
    1. **stima fallita** (campione insufficiente, griglia non valida, errore del
       modello) -> NON si salva: il job gira tutti i giorni e un giorno senza
       dati non deve cancellare la stima buona gia' sul volume;
    2. **salvataggio fallito** -> niente applicazione: si applica cio' che e' sul
       VOLUME (cio' che sopravvive al redeploy), non cio' che era in memoria;
    3. **gate di calibrazione** (`holdout_verdict`, `DIXON_COLES_CALIB_GATE`
       default ON) -> il rho stimato si applica solo se il Brier 1X2 FUORI
       CAMPIONE non peggiora rispetto al default e a rho 0. E' la correzione
       del 16/10/2026: misurato sul ledger reale, il rho che massimizza la
       verosimiglianza dei punteggi (-0.30) PEGGIORA la calibrazione dei
       mercati (0.5506 contro 0.5451) — e il denaro sta sui mercati;
    4. **applicazione** -> le tre guardie di `apply_fitted` (interruttore
       esplicito, stima ok, banda di sicurezza).

    `changed` dice se il giro ha CAMBIATO il rho del motore: e' la condizione
    con cui il job notifica (un valore identico ogni giorno non e' una notizia).
    """
    t0 = time.time()
    conf = config()
    target = Path(path or state_path())
    out: Dict[str, Any] = {
        "ok": False, "reason": None, "rho": None, "previous": None,
        "engine_rho": engine_rho(), "n_matches": 0, "n_used": 0,
        "improvement": None, "safe": None, "half_life": half_life_days(),
        "days": int(conf["days"]), "saved": False, "applied": False,
        "apply_reason": None, "changed": False, "path": str(target),
        "calibration": None, "elapsed_s": None,
    }
    try:
        data = list(matches) if matches is not None else load_matches(now=now)
        out["n_matches"] = len(data)
        out["expected_goals_injected"] = expected_goals_fn is not None
        fit = fit_rho(data, expected_goals_fn=expected_goals_fn)
        out["rho"] = fit.get("rho")
        out["n_used"] = int(fit.get("n_used") or 0)
        out["improvement"] = fit.get("improvement")
        out["safe"] = fit.get("safe")
        if not fit.get("ok"):
            out["reason"] = "stima_non_riuscita:%s" % (fit.get("reason") or "?")
            return out
        out["ok"] = True
        out["reason"] = "ok"
        out["saved"] = bool(save_fit(fit, path=target))
        if not out["saved"]:
            out["apply_reason"] = "salvataggio_non_riuscito"
            return out
        # Interruttore PRIMA di qualunque lavoro: se non si applica, il motivo
        # da riportare e' quello (e la verifica di calibrazione non serve —
        # il job gira ogni giorno su dati che cambiano, non si spreca il fit
        # del Brier quando la risposta e' gia' "non si applica").
        if not conf["apply"]:
            out["apply_reason"] = "interruttore_off"
            return out
        # GATE DI CALIBRAZIONE (16/10/2026): la verosimiglianza dei punteggi
        # esatti NON e' il criterio del denaro. Misurato sul ledger reale, il
        # rho che massimizza la log-lik (-0.30) PEGGIORA il Brier 1X2 (0.5506
        # contro 0.5451 del default e 0.5425 di rho 0): applicarlo sarebbe un
        # peggioramento della calibrazione mascherato da "stima migliore".
        if _env_bool("DIXON_COLES_CALIB_GATE", True):
            verdict = holdout_verdict(data, float(fit["rho"]),
                                      expected_goals_fn=expected_goals_fn)
            out["calibration"] = verdict
            if not verdict.get("ok"):
                out["apply_reason"] = "calibrazione:%s" % (verdict.get("reason"),)
                logger.info("dixon_coles: rho %.4f non applicato — calibrazione "
                            "%s (Brier %.4f vs default %.4f su %s match)",
                            float(fit["rho"]), verdict.get("reason"),
                            verdict.get("brier_fitted") or float("nan"),
                            verdict.get("brier_default") or float("nan"),
                            verdict.get("n_holdout"))
                return out
        applied = apply_fitted(path=target)
        out["applied"] = bool(applied.get("applied"))
        out["apply_reason"] = applied.get("reason")
        out["previous"] = applied.get("previous")
        try:
            out["changed"] = bool(out["applied"] and out["previous"] is not None
                                  and abs(float(out["previous"])
                                          - float(fit["rho"])) > 1e-9)
        except (TypeError, ValueError):
            out["changed"] = False
    except Exception as e:                                    # pragma: no cover
        logger.warning("dixon_coles: promozione non riuscita (%s)", e)
        out["reason"] = "errore_promozione"
    finally:
        out["engine_rho"] = engine_rho()
        out["elapsed_s"] = round(time.time() - t0, 3)
    return out


def format_promotion(out: Dict[str, Any]) -> str:
    """Riga UNICA (log + Telegram) del giro di promozione: dichiara l'esito vero.

    Senza il rho del motore nella riga, "non applicato" e "applicato" sarebbero
    indistinguibili da fuori — ed e' esattamente il dubbio che questo modulo
    deve togliere a chi legge il log.
    """
    data = out or {}
    if not data.get("ok"):
        return ("📐 DIXON-COLES: nessuna promozione (%s) | match %s | motore rho %s"
                % (data.get("reason") or "?", data.get("n_matches"),
                   data.get("engine_rho")))
    text = ("📐 DIXON-COLES: rho stimato %.4f su %s match (%s usati, mezza vita "
            "%.0fgg)"
            % (float(data.get("rho") or 0.0), data.get("n_matches"),
               data.get("n_used"), float(data.get("half_life") or 0.0)))
    if data.get("applied"):
        text += " | APPLICATO (era %s, motore ora %.4f)" % (
            data.get("previous"), float(data.get("engine_rho") or 0.0))
    else:
        text += " | non applicato (%s), motore resta %s" % (
            data.get("apply_reason") or "?", data.get("engine_rho"))
    if data.get("improvement") is not None:
        text += " | log-L %+.1f vs default" % float(data["improvement"])
    cal = data.get("calibration") or {}
    if cal:
        text += " | calibrazione 1X2: Brier %s vs default %s (%s)" % (
            _fmt(cal.get("brier_fitted")), _fmt(cal.get("brier_default")),
            cal.get("reason"))
    return text


def _fmt(value: Any, digits: int = 4) -> str:
    try:
        return ("%." + str(digits) + "f") % float(value)
    except (TypeError, ValueError):
        return str(value)


def refine_probabilities(lam_h: float, lam_a: float, *,
                         rho: Optional[float] = None) -> Dict[str, float]:
    """(p1, pX, p2) con un rho specifico — DELEGA alla griglia del modello.

    Serve al confronto/diagnostica: nessuna formula duplicata, il rho viene
    passato e ripristinato dal context manager di `poisson_engine`.
    """
    import poisson_engine as pe
    use = float(rho) if rho is not None else float(pe.RHO)
    with pe.rho_value(use):
        p1, px, p2 = pe.prob_1x2(lam_h, lam_a)
    return {"p1": p1, "pX": px, "p2": p2, "rho": use}


# ---------------------------------------------------------------------------
# 4) Report e CLI
# ---------------------------------------------------------------------------
def format_report(fit: Optional[Dict[str, Any]] = None, *,
                  cfg: Optional[Dict[str, Any]] = None) -> str:
    """Report testuale (Telegram-friendly) della stima."""
    conf = cfg or config()
    data = fit if fit is not None else load_fit()
    lines = ["📐 DIXON-COLES — rho stimato con time-decay",
             "  finestra %dg | max %d match | mezza vita %.0fgg | griglia "
             "%.3f..%.3f"
             % (conf["days"], conf["max_matches"], half_life_days(),
                conf["rho_min"], conf["rho_max"])]
    if not data or not data.get("ok"):
        lines.append("  ⚠️ nessuna stima disponibile (%s)"
                     % ((data or {}).get("reason") or "?"))
    else:
        lines.append("  rho stimato %.4f (default del modello %s) | "
                     "%d match usati su %d"
                     % (float(data["rho"]), _fmt(default_rho(), 2),
                        int(data.get("n_used") or 0),
                        int(data.get("n_matches") or 0)))
        if data.get("improvement") is not None:
            lines.append("  guadagno di verosimiglianza %+.1f (log-L pesata)"
                         % float(data["improvement"]))
        lines.append("  banda di sicurezza [%.2f, %.2f] -> %s"
                     % (conf["safe_min"], conf["safe_max"],
                        "dentro" if data.get("safe") else "FUORI (non si applica)"))
        if data.get("saved_at"):
            lines.append("  salvato %s" % data["saved_at"])
    lines.append("  applicazione al modello: %s (DIXON_COLES_APPLY)"
                 % ("ON" if conf["apply"] else "OFF"))
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in args
    if "--status" in args:
        data = load_fit()
        print(json.dumps(data, indent=2, default=str) if as_json
              else format_report(data))
        return 0
    if "--promote" in args:
        days = None
        for i, a in enumerate(args):
            if a == "--days" and i + 1 < len(args):
                try:
                    days = int(float(args[i + 1]))
                except (TypeError, ValueError):
                    days = None
        data = load_matches(days=days)
        out = promote(matches=data)
        print(json.dumps(out, indent=2, default=str) if as_json
              else format_promotion(out))
        return 0 if out.get("ok") else 1
    if "--fit" in args or not args:
        days = None
        for i, a in enumerate(args):
            if a == "--days" and i + 1 < len(args):
                try:
                    days = int(float(args[i + 1]))
                except (TypeError, ValueError):
                    days = None
        matches = load_matches(days=days)
        fit = fit_rho(matches)
        if "--save" in args and fit.get("ok"):
            fit["saved"] = save_fit(fit)
        print(json.dumps(fit, indent=2, default=str) if as_json
              else format_report(fit))
        return 0
    print("uso: dixon_coles.py [--fit [--days N] [--save]] [--promote [--days N]]"
          " [--status] [--json]")
    return 2


if __name__ == "__main__":                                    # pragma: no cover
    raise SystemExit(main())
