"""ml_harness.py — Harness WALK-FORWARD per il modello ML (15/10/2026).

Direttiva del proprietario: *"valuta l'integrazione di un modello di boosting
leggero per affinare le stime di valore"*. Un modello non si integra perche'
sembra promettente: si integra perche' una misura dice che batte quello che
c'e' **adesso**. Questo modulo e' quella misura, e nient'altro.

Cosa fa
-------
Costruisce la popolazione STORICA dalle partite reali
(`match_features.history_feature_rows`, a sua volta dalle `match_results` della
sync API-Football: ~17k partite, 2 stagioni, **zero crediti, zero rete**),
divide il tempo in PIEGHE CRONOLOGICHE (finestra espandente) e confronta, sulle
STESSE pieghe, tre stimatori addestrati **solo sul passato di ogni piega**:

| nome | cos'e' |
|---|---|
| `prior` | la frequenza di classe del train — il pavimento onesto |
| `logistica` | regressione logistica numpy sulle feature nuove (l'ML leggero che il progetto ha gia') |
| `boosting` | gradient boosting LEGGERO (LightGBM se presente, altrimenti XGBoost, altrimenti HistGradientBoosting di sklearn) |

Metriche: Brier, LogLoss, AUC, gap di calibrazione — per piega e aggregate
sulle predizioni OUT-OF-FOLD (ogni riga e' predetta da un modello che non l'ha
mai vista, quindi il numero non e' in-sample).

Il verdetto
-----------
`promote` / `reject` / `insufficient`, con la DIFFERENZA APPAIATA per riga
(Brier del baseline meno Brier dello sfidante), l'intervallo di confidenza e il
p-value presi da `significance.roi_test` — nessuna formula riscritta — e
`detectable`, cioe' l'effetto minimo che questo campione potrebbe distinguere.
Senza quello il verdetto sarebbe un'opinione: 17k righe distinguono differenze
di Brier minuscole, 300 righe no.

Perche' i baseline sono questi (e non "il modello attuale" in produzione)
-----------------------------------------------------------------------
Il modello attuale in produzione e' un **ensemble addestrato sul ledger LIVE**
(`ml_ensemble`): le sue righe di training esistono solo per le partite
analizzate dal bot (poche centinaia, e solo dell'era recente), quindi NON e'
riproducibile sulle 17k partite storiche. Confrontarlo su un campione diverso
darebbe un numero non comparabile. La scelta e' quindi: (a) confronto
riproducibile e onesto su tutta la storia (prior / logistica / boosting), e
(b) un baseline OPZIONALE del motore statistico del progetto
(`--incumbent`), iniettato e DICHIARATO **leaky** (i rating in `team_ratings`
sono quelli di OGGI, quindi un match del 2023 verrebbe valutato con
informazione futura): resta leggibile come riferimento, ed e' ESCLUSO dal
verdetto.

Garanzie
--------
- Sola LETTURA del ledger (`match_features.ro_conn`, `mode=ro`) e zero rete:
  nessuna chiave, nessun credito, nessun ordine.
- Nessuna soglia di strategia copiata: il campione minimo e' `significance`
  (`MIN_SAMPLES`) e la statistica e' `significance.roi_test`.
- Fail-safe: un errore diventa un bloccon `error` dichiarato, mai un'eccezione
  al chiamante; un target sconosciuto e' rifiutato, non indovinato.
- Niente risultato in-sample: le pieghe sono cronologiche e il test e' SEMPRE
  dopo il train (tripwire dedicato).

CLI
---
    venv/bin/python ml_harness.py [--json] [--folds N] [--min-train N]
        [--target home_win|home_or_draw|away_win|over25] [--limit N] [--db PATH]
        [--incumbent] [--models prior,logistica,boosting]
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_MODELS", "MODEL_BOOSTING", "MODEL_INCUMBENT", "MODEL_LOGISTIC",
    "MODEL_PRIOR", "build_matrix", "dataset", "evaluate", "format_report",
    "main", "models_available", "temporal_folds", "verdict_of",
]


# ---------------------------------------------------------------------------
# Parametri (env lette a RUNTIME: la taratura non richiede un redeploy)
# ---------------------------------------------------------------------------

def _int_env(name: str, default: int, *, minimum: int = 2) -> int:
    """Intero da env, con pavimento e fallback dichiarato (mai un valore assurdo)."""
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        got = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        logger.warning("ml_harness: %s='%s' non numerico, uso %s", name, raw, default)
        return default
    if got < minimum:
        logger.warning("ml_harness: %s=%s sotto il minimo %s, uso %s",
                       name, got, minimum, default)
        return default
    return got


def folds_count() -> int:
    """Numero di pieghe walk-forward (env `MLH_FOLDS`, default 4)."""
    return _int_env("MLH_FOLDS", 4)


def min_train_rows() -> int:
    """Righe minime nel train della prima piega (env `MLH_MIN_TRAIN`, 2000)."""
    return _int_env("MLH_MIN_TRAIN", 2000, minimum=100)


def seed() -> int:
    """Seme fisso: due run sugli stessi dati devono dare lo stesso numero."""
    return _int_env_any("MLH_SEED", 42)


def _int_env_any(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(float(str(raw).strip()))
    except (TypeError, ValueError):
        logger.warning("ml_harness: %s='%s' non numerico, uso %s", name, raw, default)
        return default


MODEL_PRIOR = "prior"
MODEL_LOGISTIC = "logistica"
MODEL_BOOSTING = "boosting"
MODEL_INCUMBENT = "incumbent"          # opzionale, LEAKY, fuori dal verdetto

#: Modelli confrontati di default (l'incumbent va chiesto con `--incumbent`).
DEFAULT_MODELS: Tuple[str, ...] = (MODEL_PRIOR, MODEL_LOGISTIC, MODEL_BOOSTING)

#: Sfidante su cui si pronuncia il verdetto e baseline di riferimento.
CHALLENGER = MODEL_BOOSTING
_BASELINES: Tuple[str, ...] = (MODEL_PRIOR, MODEL_LOGISTIC)

#: Tolleranza sull'AUC: il boosting non deve PEGGIORARE il ranking.
AUC_EPS = 0.002


# ---------------------------------------------------------------------------
# 1. POPOLAZIONE (storica, senza look-ahead) e MATRICE
# ---------------------------------------------------------------------------

def dataset(*, conn: Any = None, target: str = "home_win",
            limit: Optional[int] = None) -> Dict[str, Any]:
    """Righe storiche ordinate nel TEMPO, con le sole feature senza look-ahead.

    Delega a `match_features.history_feature_rows` (una sola definizione della
    popolazione: `ml_audit`, la scrematura e questo harness guardano gli stessi
    dati). Le feature di `LOOKAHEAD_UNSAFE` (xG di stagione) sono ESCLUSE per
    costruzione, non filtrate a valle.
    """
    import match_features as mf
    out: Dict[str, Any] = {"rows": [], "names": [], "target": target,
                           "error": None, "stats": {}}
    got = mf.history_feature_rows(conn=conn, limit=limit, target=target)
    if got.get("error"):
        out["error"] = got["error"]
        out["stats"] = got.get("stats") or {}
        return out
    names = tuple(mf.HISTORY_FEATURES)
    rows: List[Dict[str, Any]] = []
    dropped = {"no_ts": 0, "no_label": 0, "all_nan": 0}
    for r in got.get("rows") or []:
        order = r.get("ts")
        if order is None:
            dropped["no_ts"] += 1
            continue                       # senza tempo non c'e' walk-forward
        if r.get("label") is None:
            dropped["no_label"] += 1
            continue
        vec = mf.feature_vector(r, names)
        if all(v is None for v in vec):
            dropped["all_nan"] += 1
            continue                       # nessuna feature: riga non addestrabile
        rows.append({"ts": float(order), "label": int(r["label"]),
                     "league": r.get("league") or "", "row": r})
    rows.sort(key=lambda x: x["ts"])       # ORDINE CRONOLOGICO: e' il walk-forward
    out["rows"] = rows
    out["names"] = list(names)
    out["stats"] = {**(got.get("stats") or {}), "usable": len(rows),
                    "dropped": dropped}
    return out


def build_matrix(rows: Sequence[Dict[str, Any]], names: Sequence[str]
                 ) -> Tuple[np.ndarray, np.ndarray]:
    """(X, y) con i BUCHI come `NaN` — nessuna imputazione fatta qui.

    Imputare prima di sapere dove finisce il train sarebbe un leak silenzioso
    (la mediana calcolata sul futuro). Chi ha bisogno di un valore (la
    logistica) imputa con la mediana del PROPRIO train; chi non ne ha bisogno
    (il boosting) riceve i `NaN` e li gestisce nativamente.
    """
    import match_features as mf
    x = np.full((len(rows), len(names)), np.nan, dtype=float)
    y = np.zeros(len(rows), dtype=float)
    for i, item in enumerate(rows):
        vec = mf.feature_vector(item["row"], names)
        for j, v in enumerate(vec):
            if v is not None and math.isfinite(v):
                x[i, j] = float(v)
        y[i] = float(item["label"])
    return x, y


def temporal_folds(rows: Sequence[Dict[str, Any]], *, folds: Optional[int] = None,
                   min_train: Optional[int] = None
                   ) -> List[Tuple[np.ndarray, np.ndarray]]:
    """Pieghe cronologiche a finestra ESPANDENTE: (indici train, indici test).

    Regola (il difetto che falsa qualunque backtest): il test e' SEMPRE
    successivo al train. Le righe sono ordinate per tempo da `dataset`, quindi
    il rispetto dell'ordine e' verificato qui con un assert, non dato per
    scontato. Ritorna `[]` se il campione non basta per `folds` pieghe.
    """
    k = folds if folds is not None else folds_count()
    need = min_train if min_train is not None else min_train_rows()
    n = len(rows)
    if k < 2 or n < need + k:
        return []
    ts = [float(r["ts"]) for r in rows]
    for i in range(1, n):
        if ts[i] < ts[i - 1]:                                  # pragma: no cover
            logger.warning("ml_harness: serie non ordinata nel tempo, nessuna piega")
            return []
    block = (n - need) // k
    if block < 1:
        return []
    out: List[Tuple[np.ndarray, np.ndarray]] = []
    for i in range(k):
        start = need + i * block
        stop = start + block if i < k - 1 else n
        if stop <= start:
            continue
        train = np.arange(0, start, dtype=int)
        test = np.arange(start, stop, dtype=int)
        out.append((train, test))
    return out


# ---------------------------------------------------------------------------
# 2. STIMATORI (interfaccia minima: fit(X, y) / predict(X))
# ---------------------------------------------------------------------------

class PriorModel:
    """Frequenza di classe del train: il pavimento che ogni modello deve battere."""

    name = MODEL_PRIOR

    def __init__(self) -> None:
        self.p = 0.5

    def fit(self, x: np.ndarray, y: np.ndarray) -> "PriorModel":
        self.p = float(np.mean(y)) if len(y) else 0.5
        return self

    def predict(self, x: np.ndarray) -> np.ndarray:
        return np.full(len(x), min(max(self.p, 1e-6), 1.0 - 1e-6), dtype=float)


class LogisticModel:
    """Logistica numpy (L2, discesa a gradiente): l'ML leggero gia' in casa.

    Imputa i `NaN` con la mediana del SOLO train e standardizza sulle statistiche
    del train (media/deviazione del train, mai dell'intero dataset).
    """

    name = MODEL_LOGISTIC

    def __init__(self, *, epochs: int = 400, lr: float = 0.5,
                 l2: float = 1e-3) -> None:
        self.epochs = int(epochs)
        self.lr = float(lr)
        self.l2 = float(l2)
        self.med: Optional[np.ndarray] = None
        self.mu: Optional[np.ndarray] = None
        self.sd: Optional[np.ndarray] = None
        self.w: Optional[np.ndarray] = None
        self.b: float = 0.0

    def _prep(self, x: np.ndarray, *, fit: bool) -> np.ndarray:
        z = np.array(x, dtype=float, copy=True)
        if fit:
            with np.errstate(invalid="ignore"):
                self.med = np.nanmedian(z, axis=0)
            self.med = np.where(np.isfinite(self.med), self.med, 0.0)
        assert self.med is not None
        bad = ~np.isfinite(z)
        if bad.any():
            z[bad] = np.take(self.med, np.where(bad)[1])
        if fit:
            self.mu = z.mean(axis=0)
            sd = z.std(axis=0)
            sd[sd < 1e-9] = 1.0
            self.sd = sd
        assert self.mu is not None and self.sd is not None
        return (z - self.mu) / self.sd

    def fit(self, x: np.ndarray, y: np.ndarray) -> "LogisticModel":
        z = self._prep(x, fit=True)
        n, d = z.shape
        self.w = np.zeros(d, dtype=float)
        self.b = 0.0
        for _ in range(self.epochs):
            p = _sigmoid(z @ self.w + self.b)
            err = p - y
            self.w -= self.lr * (z.T @ err / n + self.l2 * self.w)
            self.b -= self.lr * float(err.mean())
        return self

    def predict(self, x: np.ndarray) -> np.ndarray:
        z = self._prep(x, fit=False)
        assert self.w is not None
        return _sigmoid(z @ self.w + self.b)


def _sigmoid(v: np.ndarray) -> np.ndarray:
    """Sigmoide numericamente stabile (evita overflow su |v| grande)."""
    out = np.empty_like(v, dtype=float)
    pos = v >= 0
    out[pos] = 1.0 / (1.0 + np.exp(-v[pos]))
    e = np.exp(v[~pos])
    out[~pos] = e / (1.0 + e)
    return out


class BoostingModel:
    """Gradient boosting LEGGERO con degradazione dichiarata.

    Ordine di preferenza: LightGBM -> XGBoost -> HistGradientBoosting (sklearn).
    `which()` dice quale motore si e' usato: se nessuno e' installato il modello
    NON esiste e il verdetto lo dichiara (mai un nome senza sostanza). I
    parametri sono volutamente PICCOLI (`max_depth` 3, 300 alberi, lr 0.05):
    "leggero" vuol dire un modello che il container puo' addestrare senza
    costare memoria, non un tuning da competizione.

    I `NaN` sono gestiti NATIVAMENTE dai tre motori: nessuna imputazione, quindi
    nessun rischio di imputare con informazione del test.
    """

    name = MODEL_BOOSTING

    def __init__(self, *, seed_value: Optional[int] = None) -> None:
        self.engine: Optional[str] = None
        self.model: Any = None
        self.seed = seed() if seed_value is None else int(seed_value)

    def fit(self, x: np.ndarray, y: np.ndarray) -> "BoostingModel":
        engine = self.engine or _pick_engine()
        if engine is None:
            raise RuntimeError("nessun motore di boosting disponibile")
        self.engine = engine
        if engine == "lightgbm":
            import lightgbm as lgb
            self.model = lgb.LGBMClassifier(
                n_estimators=300, max_depth=3, learning_rate=0.05,
                subsample=0.8, random_state=self.seed, verbose=-1)
        elif engine == "xgboost":
            from xgboost import XGBClassifier
            self.model = XGBClassifier(
                n_estimators=300, max_depth=3, learning_rate=0.05,
                subsample=0.8, colsample_bytree=0.8, random_state=self.seed,
                eval_metric="logloss", n_jobs=1)
        else:
            from sklearn.ensemble import HistGradientBoostingClassifier
            self.model = HistGradientBoostingClassifier(
                max_iter=300, max_depth=3, learning_rate=0.05,
                random_state=self.seed)
        self.model.fit(x, y)
        return self

    def predict(self, x: np.ndarray) -> np.ndarray:
        proba = self.model.predict_proba(x)
        return np.asarray(proba)[:, 1].astype(float)


def _pick_engine() -> Optional[str]:
    """Primo motore di boosting disponibile, o None (degradazione dichiarata)."""
    for engine, module in (("lightgbm", "lightgbm"), ("xgboost", "xgboost"),
                           ("sklearn", "sklearn.ensemble")):
        try:
            __import__(module)
            return engine
        except Exception:
            continue
    return None


def models_available() -> Dict[str, Any]:
    """Cosa si puo' addestrare in QUESTO ambiente (dichiarato nel report)."""
    engine = _pick_engine()
    return {"boosting_engine": engine,
            "boosting": engine is not None,
            "numpy": True, "logistica": True}


class IncumbentModel:                                  # pragma: no cover (opz.)
    """Baseline INIETTATO del motore in produzione. Il chiamante DICHIARA il leak.

    Interfaccia volutamente diversa dagli altri (`callable(row) -> p`): sta fuori
    dal verdetto e serve solo a leggere il confronto con cio' che gira oggi.
    """

    name = MODEL_INCUMBENT

    def __init__(self, fn: Callable[[Dict[str, Any]], Optional[float]],
                 *, leaky: bool = True) -> None:
        self.fn = fn
        self.leaky = bool(leaky)
        self.cache: Dict[int, Optional[float]] = {}

    def fit(self, x: np.ndarray, y: np.ndarray) -> "IncumbentModel":
        return self

    def predict_rows(self, rows: Sequence[Dict[str, Any]]) -> np.ndarray:
        out = np.full(len(rows), np.nan, dtype=float)
        for i, r in enumerate(rows):
            try:
                got = self.fn(r)
            except Exception:
                got = None
            if got is not None and math.isfinite(float(got)):
                out[i] = min(max(float(got), 1e-6), 1.0 - 1e-6)
        return out


def make_model(name: str, **kw: Any) -> Any:
    """Fabbrica degli stimatori (nome sconosciuto -> errore dichiarato)."""
    if name == MODEL_PRIOR:
        return PriorModel()
    if name == MODEL_LOGISTIC:
        return LogisticModel(**kw)
    if name == MODEL_BOOSTING:
        return BoostingModel(**kw)
    if name == MODEL_INCUMBENT:
        raise ValueError("incumbent: costruirlo con IncumbentModel(...)")
    raise ValueError(f"modello sconosciuto: {name}")


# ---------------------------------------------------------------------------
# 3. METRICHE (nessuna formula copiata: l'AUC viene da `match_features`)
# ---------------------------------------------------------------------------

def _clip(p: np.ndarray) -> np.ndarray:
    return np.clip(np.asarray(p, dtype=float), 1e-6, 1.0 - 1e-6)


def brier(y: np.ndarray, p: np.ndarray) -> Optional[float]:
    if len(y) == 0:
        return None
    return float(np.mean((_clip(p) - y) ** 2))


def logloss(y: np.ndarray, p: np.ndarray) -> Optional[float]:
    if len(y) == 0:
        return None
    q = _clip(p)
    return float(-np.mean(y * np.log(q) + (1.0 - y) * np.log(1.0 - q)))


def calibration_gap(y: np.ndarray, p: np.ndarray) -> Optional[float]:
    """Media(predetto) - media(osservato): il segno dice l'overconfidence."""
    if len(y) == 0:
        return None
    return float(np.mean(_clip(p)) - np.mean(y))


def auc(y: np.ndarray, p: np.ndarray) -> Optional[float]:
    """AUC di ranking — formula UNICA del progetto (`match_features.feature_auc`)."""
    if len(y) == 0 or len(set(y.tolist())) < 2:
        return None
    import match_features as mf
    rows = [{"label": int(yi), "score": float(pi)} for yi, pi in zip(y, p)]
    got = mf.feature_auc(rows, "score")
    return got.get("auc")


def _metrics(y: np.ndarray, p: np.ndarray) -> Dict[str, Any]:
    return {"n": int(len(y)), "brier": brier(y, p), "logloss": logloss(y, p),
            "auc": auc(y, p), "calib_gap": calibration_gap(y, p)}


# ---------------------------------------------------------------------------
# 4. VALUTAZIONE WALK-FORWARD
# ---------------------------------------------------------------------------

def evaluate(rows: Sequence[Dict[str, Any]], *, names: Sequence[str],
             models: Sequence[str] = DEFAULT_MODELS,
             folds: Optional[int] = None, min_train: Optional[int] = None,
             incumbent_fn: Optional[Callable[[Dict[str, Any]], Optional[float]]] = None,
             alpha: float = 0.05) -> Dict[str, Any]:
    """Confronto walk-forward: metriche per piega, aggregate OOF e verdetto.

    Fail-safe: qualunque errore diventa un blocco con `error` dichiarato.
    """
    out: Dict[str, Any] = {"target": None, "n_rows": len(rows),
                           "models": list(models), "folds": [],
                           "pooled": {}, "comparisons": {}, "verdict": {},
                           "available": models_available(), "error": None,
                           "caveats": []}
    try:
        if not rows:
            out["error"] = "nessuna_riga"
            out["verdict"] = verdict_of(out, alpha=alpha)
            return out
        x, y = build_matrix(rows, names)
        splits = temporal_folds(rows, folds=folds, min_train=min_train)
        if not splits:
            out["error"] = "campione_insufficiente_per_le_pieghe"
            out["caveats"].append(
                f"servono almeno {min_train_rows()} righe di train + 2 pieghe: "
                f"disponibili {len(rows)}")
            out["verdict"] = verdict_of(out, alpha=alpha)
            return out
        wanted = list(models)
        if incumbent_fn is not None and MODEL_INCUMBENT not in wanted:
            wanted.append(MODEL_INCUMBENT)
        pooled: Dict[str, List[float]] = {m: [] for m in wanted}
        pooled_y: List[float] = []
        per_fold: List[Dict[str, Any]] = []
        for fi, (tr, te) in enumerate(splits):
            x_tr, y_tr = x[tr], y[tr]
            x_te, y_te = x[te], y[te]
            fold: Dict[str, Any] = {"fold": fi, "n_train": int(len(tr)),
                                    "n_test": int(len(te)),
                                    "ts_from": float(rows[int(te[0])]["ts"]),
                                    "ts_to": float(rows[int(te[-1])]["ts"]),
                                    "models": {}}
            for name in wanted:
                try:
                    if name == MODEL_INCUMBENT:
                        model = IncumbentModel(incumbent_fn)
                        pred = model.predict_rows([rows[i] for i in te])
                        keep = np.isfinite(pred)
                        fold["models"][name] = _metrics(y_te[keep], pred[keep])
                        fold["models"][name]["leaky"] = True
                    else:
                        model = make_model(name)
                        model.fit(x_tr, y_tr)
                        pred = np.asarray(model.predict(x_te), dtype=float)
                        fold["models"][name] = _metrics(y_te, pred)
                except Exception as exc:
                    fold["models"][name] = {"n": 0, "error": type(exc).__name__,
                                            "detail": str(exc)[:200]}
                    continue
                if name == MODEL_INCUMBENT:
                    continue
                pooled[name].extend(float(v) for v in pred)
            pooled_y.extend(float(v) for v in y_te)
            per_fold.append(fold)
        out["folds"] = per_fold
        yy = np.asarray(pooled_y, dtype=float)
        for name in wanted:
            pred = pooled.get(name) or []
            if name == MODEL_INCUMBENT:
                continue
            if len(pred) != len(yy):
                continue
            out["pooled"][name] = _metrics(yy, np.asarray(pred, dtype=float))
        out["n_oof"] = int(len(yy))
        out["comparisons"] = _comparisons(yy, pooled, alpha=alpha)
        out["verdict"] = verdict_of(out, alpha=alpha)
        return out
    except Exception as exc:                               # fail-safe totale
        logger.debug("ml_harness: valutazione non completata (%s)", exc)
        out["error"] = f"{type(exc).__name__}:{exc}"
        out["verdict"] = verdict_of(out, alpha=alpha)
        return out


def _comparisons(y: np.ndarray, pooled: Dict[str, List[float]], *,
                 alpha: float) -> Dict[str, Any]:
    """Differenze APPAIATE per riga (baseline - sfidante) con test e CI95.

    Differenza POSITIVA = lo sfidante ha Brier piu' BASSO = meglio. La
    statistica e' `significance.roi_test` (t-test su una serie di valori per
    riga): non e' un test nuovo, e' lo stesso strumento con cui il progetto
    giudica ogni altro campione.
    """
    import significance as sig
    out: Dict[str, Any] = {}
    if CHALLENGER not in pooled or not pooled[CHALLENGER]:
        return out
    chal = np.asarray(pooled[CHALLENGER], dtype=float)
    for base in _BASELINES:
        if base not in pooled or len(pooled[base]) != len(chal):
            continue
        base_p = np.asarray(pooled[base], dtype=float)
        diffs = ((base_p - y) ** 2 - (chal - y) ** 2)     # Brier_base - Brier_chal
        got = sig.roi_test([float(v) for v in diffs], alpha=alpha)
        entry: Dict[str, Any] = {"mean_brier_gain": (float(np.mean(diffs))
                                                     if len(diffs) else None)}
        if got:
            entry.update({"n": got.get("n"), "p_value": got.get("p_value"),
                          "ci95": ([got["ci_low"], got["ci_high"]]
                                   if got.get("ci_low") is not None else None),
                          "significant": got.get("significant"),
                          "direction": got.get("direction"),
                          "degenerate": got.get("degenerate")})
            if got.get("sd"):
                entry["detectable_brier_gain"] = sig.detectable_edge(
                    got["n"], sd=got["sd"], alpha=alpha)
        entry["verdict_vs_baseline"] = (
            "better" if (got or {}).get("direction") == "above"
            and (got or {}).get("significant")
            else ("worse" if (got or {}).get("direction") == "below"
                  and (got or {}).get("significant") else "flat"))
        out[f"{CHALLENGER}_vs_{base}"] = entry
    return out


def verdict_of(result: Dict[str, Any], *, alpha: float = 0.05) -> Dict[str, Any]:
    """`promote` / `reject` / `insufficient` con il motivo SEMPRE dichiarato.

    Regola (niente magia):
      - `insufficient` se le righe out-of-fold sono sotto `significance.MIN_SAMPLES`
        o se non c'e' un verdetto calcolabile;
      - `promote` se il boosting batte OGNI baseline in modo significativo
        (Bonferroni sul numero di confronti) e non peggiora l'AUC;
      - `reject` altrimenti, col motivo e con la differenza misurata.

    Un modello NON si promuove per un Brier piu' basso di 0.0001 su un campione
    che non puo' distinguerlo dal rumore: e' il `detectable` a dirlo.
    """
    import significance as sig
    out: Dict[str, Any] = {"status": "insufficient", "reason": "", "alpha": alpha}
    if result.get("error"):
        out["reason"] = f"valutazione non disponibile: {result['error']}"
        return out
    pooled = result.get("pooled") or {}
    n = int(result.get("n_oof") or 0)
    out["n_oof"] = n
    if CHALLENGER not in pooled:
        out["reason"] = (f"lo sfidante '{CHALLENGER}' non e' addestrabile in "
                         "questo ambiente (nessun motore di boosting)")
        return out
    if n < sig.MIN_SAMPLES:
        out["reason"] = (f"campione out-of-fold insufficiente ({n}/"
                         f"{sig.MIN_SAMPLES}): nessun verdetto")
        return out
    comps = result.get("comparisons") or {}
    if not comps:
        out["reason"] = "nessun confronto disponibile sulle pieghe"
        return out
    # Bonferroni: il numero di confronti e' quello dei baseline EFFETTIVI.
    thr = alpha / max(1, len(comps))
    out["bonferroni_alpha"] = round(thr, 6)
    worse = [k for k, v in comps.items()
             if v.get("verdict_vs_baseline") == "worse"]
    not_better = [k for k, v in comps.items()
                  if v.get("verdict_vs_baseline") != "better"]
    gains = {k: v.get("mean_brier_gain") for k, v in comps.items()}
    out["brier_gains"] = gains
    sig_ok = all((comps[k].get("p_value") is not None
                  and comps[k]["p_value"] < thr) for k in comps)
    chal_auc = (pooled.get(CHALLENGER) or {}).get("auc")
    other_aucs = [(pooled.get(b) or {}).get("auc") for b in comps.keys()
                  if (pooled.get(b) or {}).get("auc") is not None]
    auc_ok = (chal_auc is None or not other_aucs
              or all(chal_auc >= a - AUC_EPS for a in other_aucs))
    out["significant_all"] = bool(sig_ok)
    out["auc_ok"] = bool(auc_ok)
    if worse:
        out["status"] = "reject"
        out["reason"] = "lo sfidante e' SIGNIFICATIVAMENTE peggiore su: " + \
                        ", ".join(sorted(worse))
        return out
    if not_better:
        out["status"] = "reject"
        out["reason"] = ("nessun miglioramento significativo su: "
                         + ", ".join(sorted(not_better)))
        return out
    if not auc_ok:
        out["status"] = "reject"
        out["reason"] = "il Brier migliora ma il RANKING (AUC) peggiora"
        return out
    out["status"] = "promote"
    out["reason"] = (f"il boosting batte {len(comps)} baseline sia sul Brier "
                     f"(p < {thr:g}) sia sull'AUC, su {n} righe out-of-fold")
    return out


# ---------------------------------------------------------------------------
# 5. REPORT e CLI
# ---------------------------------------------------------------------------

def _fmt(value: Optional[float], digits: int = 4) -> str:
    return "—" if value is None else f"{float(value):.{digits}f}"


def format_report(res: Dict[str, Any]) -> str:
    """Report leggibile (CLI/Telegram). Non decide: descrive."""
    v = res.get("verdict") or {}
    lines = ["🧪 *ML HARNESS* — walk-forward (solo misura, nessun ordine)"]
    lines.append(f"Righe: {res.get('n_rows', 0)} | out-of-fold: "
                 f"{res.get('n_oof', 0)} | pieghe: {len(res.get('folds') or [])}")
    av = res.get("available") or {}
    lines.append(f"Motore di boosting: {av.get('boosting_engine') or 'NESSUNO'}")
    pooled = res.get("pooled") or {}
    for name, m in pooled.items():
        lines.append(f"  • {name}: Brier {_fmt(m.get('brier'))} | LogLoss "
                     f"{_fmt(m.get('logloss'))} | AUC {_fmt(m.get('auc'))} | "
                     f"gap {_fmt(m.get('calib_gap'))} (n={m.get('n')})")
    for key, c in (res.get("comparisons") or {}).items():
        ci = c.get("ci95")
        ci_txt = (f" CI95 {ci[0]:+.5f}..{ci[1]:+.5f}" if ci else "")
        lines.append(f"  ↳ {key}: guadagno Brier {_fmt(c.get('mean_brier_gain'), 5)}"
                     f" | p={c.get('p_value')} | {c.get('verdict_vs_baseline')}"
                     f"{ci_txt}")
        if c.get("detectable_brier_gain") is not None:
            lines.append(f"     effetto minimo distinguibile: "
                         f"{c['detectable_brier_gain']:.5f}")
    lines.append(f"Verdetto: *{v.get('status')}* — {v.get('reason')}")
    for caveat in res.get("caveats") or []:
        lines.append(f"⚠️ {caveat}")
    if res.get("error"):
        lines.append(f"⚠️ {res['error']}")
    return "\n".join(lines)


def run(*, db: Optional[str] = None, target: str = "home_win",
        folds: Optional[int] = None, min_train: Optional[int] = None,
        limit: Optional[int] = None, models: Sequence[str] = DEFAULT_MODELS,
        incumbent_fn: Optional[Callable[[Dict[str, Any]], Optional[float]]] = None,
        alpha: float = 0.05) -> Dict[str, Any]:
    """Dataset + valutazione in una chiamata. Mai un'eccezione."""
    out: Dict[str, Any] = {}
    try:
        import match_features as mf
        conn = mf.ro_conn(db) if db else None
        try:
            data = dataset(conn=conn, target=target, limit=limit) if conn \
                else dataset(target=target, limit=limit)
        finally:
            if conn is not None:
                conn.close()
        if data.get("error"):
            return {"error": data["error"], "verdict": {"status": "insufficient",
                                                        "reason": data["error"]}}
        res = evaluate(data["rows"], names=data["names"], models=models,
                       folds=folds, min_train=min_train,
                       incumbent_fn=incumbent_fn, alpha=alpha)
        res["stats"] = data.get("stats") or {}
        res["target"] = target
        return res
    except Exception as exc:                               # fail-safe totale
        logger.debug("ml_harness: run non completata (%s)", exc)
        return {"error": f"{type(exc).__name__}:{exc}",
                "verdict": {"status": "insufficient", "reason": str(exc)}}


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Harness walk-forward del modello ML")
    p.add_argument("--json", action="store_true", help="output JSON")
    p.add_argument("--folds", type=int, default=None)
    p.add_argument("--min-train", type=int, default=None)
    p.add_argument("--target", default="home_win")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--db", default=None, help="ledger alternativo (sola lettura)")
    p.add_argument("--models", default=",".join(DEFAULT_MODELS))
    p.add_argument("--incumbent", action="store_true",
                   help="aggiunge il baseline del motore di produzione (LEAKY)")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    models = tuple(m.strip() for m in str(args.models).split(",") if m.strip())
    res = run(db=args.db, target=args.target, folds=args.folds,
              min_train=args.min_train, limit=args.limit, models=models)
    if args.json:
        print(json.dumps(res, indent=2, default=str))
    else:
        print(format_report(res))
    status = (res.get("verdict") or {}).get("status")
    return 0 if status in ("promote", "reject") else 1


if __name__ == "__main__":                                 # pragma: no cover
    sys.exit(main())
