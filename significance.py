"""Significativita' statistica del campione del ledger (28/09/2026).

PERCHE' ESISTE (direttiva del proprietario, 28/09/2026). Le decisioni di
strategia di questo progetto si prendono su campioni PICCOLI — 8 chiusure
Over/Under dell'era nuova, 11 chiusure 1X2, 29 chiusure "giocabili" OU — e un
ROI su 8 righe non e' una misura: e' rumore con un segno. Il 22/09 un
`-21,64%` sul 1X2 e' stato letto per un giorno intero come una prova contro la
strategia quando 74 delle 85 righe appartenevano a una pipeline RITIRATA; il
25/09 il `+21,21%` dell'OU era portato per intero da 22 righe pre-19/09.

Questo modulo non decide niente e non cambia nessun gate: dice, per ogni
campione, se il risultato osservato e' **distinguibile da zero** e **quante
chiusure servono** per dichiarare un edge. E' il numero che rende leggibili le
soglie di decisione ("30-40 chiusure dell'era nuova", "20 chiusure OU").

SCIPY, NON FORMULE REINVENTATE. Il progetto e' nato numpy-only, ma `scipy`
e' gia' nell'immagine Docker (arriva con `scikit-learn`, che serve all'ensemble
ML): dal 28/09/2026 e' dichiarata esplicitamente in `requirements.txt` invece
di dipendere da un arrivo transitivo. Da li' prendiamo `binomtest` per l'hit
rate e `t`/`norm` per intervalli e test sul ROI: nessuna formula riscritta a
mano, cosi' i numeri sono confrontabili con qualunque riferimento esterno.
Se `scipy` non fosse disponibile il modulo DEGRADA (`available()` -> False,
tutti i blocchi `unavailable`) invece di far cadere i report.

GARANZIE (tripwire in `test_significance.py`): sola LETTURA del ledger
(`mode=ro` quando legge il DB da solo), nessuna rete, nessun ordine, nessuna
scrittura. Zero crediti API.
"""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

try:                                             # pragma: no cover - ambiente
    from scipy import stats as _st
except Exception:                                # pragma: no cover
    _st = None


# ---------------------------------------------------------------------------
# Parametri (default di codice; i tre report li leggono da qui, mai copiati)
# ---------------------------------------------------------------------------

#: Confidenza degli intervalli e livello di significativita' dei test.
ALPHA: float = 0.05
#: Potenza statistica usata per il calcolo del campione necessario (80%).
POWER: float = 0.80
#: Sotto questo numero di chiusure il campione non e' giudicabile: e' la STESSA
#: soglia di `multi_market.MIN_RELIABLE_CLOSED` e di `league_gate_impact`
#: (un progetto, una sola idea di "campione affidabile").
MIN_SAMPLES: int = 30

STATUS_INSUFFICIENT = "insufficient"
STATUS_NO_EDGE = "no_edge"
STATUS_POSITIVE = "positive"
STATUS_NEGATIVE = "negative"
STATUS_UNAVAILABLE = "unavailable"

#: Etichette leggibili dei verdetti (CLI/Telegram).
STATUS_LABEL = {
    STATUS_INSUFFICIENT: "campione insufficiente",
    STATUS_NO_EDGE: "non distinguibile da zero",
    STATUS_POSITIVE: "POSITIVO significativo",
    STATUS_NEGATIVE: "NEGATIVO significativo",
    STATUS_UNAVAILABLE: "non calcolabile",
}


def available() -> bool:
    """`scipy` e' utilizzabile in questo processo?"""
    return _st is not None


# ---------------------------------------------------------------------------
# Strumenti statistici elementari (tutti fail-safe: mai un'eccezione)
# ---------------------------------------------------------------------------


def _as_float(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(out) or math.isinf(out):
        return None
    return out


def wilson_interval(won: int, closed: int, alpha: float = ALPHA
                    ) -> Optional[Tuple[float, float]]:
    """Intervallo di confidenza di Wilson su una proporzione.

    Scelto al posto dell'intervallo normale perche' REGGE sui campioni piccoli
    e agli estremi (0 su 8, 8 su 8), dove l'approssimazione normale produce
    limiti fuori da [0, 1] o ampiezza nulla. Ritorna frazioni (0..1).
    """
    if not available():
        return None
    try:
        n = int(closed)
        k = int(won)
    except (TypeError, ValueError):
        return None
    if n <= 0 or k < 0 or k > n:
        return None
    z = float(_st.norm.ppf(1.0 - alpha / 2.0))
    p = k / n
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2.0 * n)) / denom
    half = (z / denom) * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n))
    return (max(0.0, centre - half), min(1.0, centre + half))


def hit_rate_test(won: int, closed: int, expected: float,
                  alpha: float = ALPHA) -> Optional[Dict[str, Any]]:
    """Test binomiale esatto dell'hit rate contro una probabilita' attesa.

    L'hit rate e' `won / closed` con `closed = won + lost` (i PUSH sono
    esclusi: non sono ne' vittorie ne' sconfitte, e includerli abbasserebbe
    artificialmente la frequenza). `expected` e' la probabilita' di riferimento
    — la prob. media del MODELLO (overconfidence) oppure quella di
    break-even implicita dalla quota media.
    """
    if not available():
        return None
    try:
        n = int(closed)
        k = int(won)
        p0 = float(expected)
    except (TypeError, ValueError):
        return None
    if n <= 0 or k < 0 or k > n or not (0.0 < p0 < 1.0):
        return None
    try:
        res = _st.binomtest(k, n, p0, alternative="two-sided")
        p_value = float(res.pvalue)
    except Exception:
        return None
    observed = k / n
    direction = "above" if observed > p0 else ("below" if observed < p0 else "flat")
    return {
        "won": k, "closed": n,
        "observed": round(observed, 4), "expected": round(p0, 4),
        "delta_pp": round((observed - p0) * 100.0, 2),
        "p_value": round(p_value, 4),
        "significant": p_value < alpha,
        "direction": direction,
    }


def roi_test(profits: Sequence[float], alpha: float = ALPHA
             ) -> Optional[Dict[str, Any]]:
    """Test t (una coda per direzione) sul P/L medio per unita' di stake.

    Il campione sono i profitti REALIZZATI per riga chiusa (i PUSH valgono 0):
    la media coincide quindi con il ROI del report (`pnl / n` con n = tutte le
    chiuse), senza introdurre una seconda definizione di ROI.
    """
    if not available():
        return None
    vals: List[float] = []
    for p in profits or []:
        v = _as_float(p)
        if v is not None:
            vals.append(v)
    n = len(vals)
    if n < 2:
        return None
    mean = sum(vals) / n
    var = sum((v - mean) ** 2 for v in vals) / (n - 1)
    sd = math.sqrt(var)
    se = sd / math.sqrt(n)
    if se <= 0.0:
        # Nessuna variabilita' osservata: se la media e' 0 il campione non dice
        # nulla; altrimenti il segno e' deterministico DENTRO il campione, ma
        # un p-value a 0 sarebbe una certezza inventata -> si dichiara None.
        return {"n": n, "mean": round(mean, 6), "sd": round(sd, 6),
                "se": 0.0, "t": None, "p_value": None,
                "ci_low": round(mean, 6), "ci_high": round(mean, 6),
                "significant": False, "direction": "flat",
                "degenerate": True}
    t = mean / se
    try:
        p_value = 2.0 * (1.0 - float(_st.t.cdf(abs(t), n - 1)))
        t_crit = float(_st.t.ppf(1.0 - alpha / 2.0, n - 1))
    except Exception:
        return None
    return {
        "n": n, "mean": round(mean, 6), "sd": round(sd, 6), "se": round(se, 6),
        "t": round(t, 3), "p_value": round(p_value, 4),
        "ci_low": round(mean - t_crit * se, 6),
        "ci_high": round(mean + t_crit * se, 6),
        "significant": p_value < alpha,
        "direction": "above" if mean > 0 else ("below" if mean < 0 else "flat"),
        "degenerate": False,
    }


def required_n(edge: float, *, sd: Optional[float] = None,
               odds: Optional[float] = None, prob: Optional[float] = None,
               alpha: float = ALPHA, power: float = POWER) -> Optional[int]:
    """Chiusure necessarie per dichiarare un edge a potenza `power`.

        n = ((z_(1-a/2) + z_power) * sd / edge)^2

    `sd` e' la deviazione standard del P/L per unita' di stake. Se non si ha
    un campione osservato si stima dalla quota:
    `sd = quota * sqrt(p (1-p))` con p = probabilita' attesa (default
    break-even `1/quota`). E' una stima, non una misura: serve a dare l'ORDINE
    DI GRANDEZZA del campione richiesto, che nella pratica resta saldamente a
    due-tre cifre anche per edge generosi a quote alte.
    """
    if not available():
        return None
    e = _as_float(edge)
    if e is None or e <= 0.0:
        return None
    s = _as_float(sd)
    if s is None:
        o = _as_float(odds)
        if o is None or o <= 1.0:
            return None
        p = _as_float(prob)
        if p is None or not (0.0 < p < 1.0):
            p = 1.0 / o
        s = o * math.sqrt(p * (1.0 - p))
    if s <= 0.0:
        return None
    try:
        z_a = float(_st.norm.ppf(1.0 - alpha / 2.0))
        z_b = float(_st.norm.ppf(power))
    except Exception:
        return None
    return int(math.ceil(((z_a + z_b) * s / e) ** 2))


def breakeven_hit_rate(odds: Optional[float]) -> Optional[float]:
    """Hit rate di break-even implicita da una quota (1/quota)."""
    o = _as_float(odds)
    if o is None or o <= 1.0:
        return None
    return 1.0 / o


def detectable_edge(n: int, *, sd: Optional[float] = None,
                    odds: Optional[float] = None, prob: Optional[float] = None,
                    alpha: float = ALPHA, power: float = POWER) -> Optional[float]:
    """L'INVERSO di `required_n`: il piu' piccolo edge distinguibile con `n`.

        edge_min = (z_(1-a/2) + z_power) * sd / sqrt(n)

    E' il numero che rende onesto un gate di decisione. Se a quota media 1.65
    l'edge minimo distinguibile con 30 chiusure e' ~41%, allora la soglia
    "30-40 chiusure" NON puo' confermare un edge del +2%: puo' solo smentire un
    disastro (un ROI fortemente negativo) o confermare un edge enorme. Dirlo
    prima evita di leggere un campione piccolo come se fosse una misura.
    """
    if not available():
        return None
    try:
        count = int(n)
    except (TypeError, ValueError):
        return None
    if count < 2:
        return None
    s = _as_float(sd)
    if s is None:
        o = _as_float(odds)
        if o is None or o <= 1.0:
            return None
        p = _as_float(prob)
        if p is None or not (0.0 < p < 1.0):
            p = 1.0 / o
        s = o * math.sqrt(p * (1.0 - p))
    if s <= 0.0:
        return None
    try:
        z_a = float(_st.norm.ppf(1.0 - alpha / 2.0))
        z_b = float(_st.norm.ppf(power))
    except Exception:
        return None
    return (z_a + z_b) * s / math.sqrt(count)


# ---------------------------------------------------------------------------
# Valutazione di un campione di righe del ledger
# ---------------------------------------------------------------------------


def evaluate(rows: Iterable[Dict[str, Any]], *, alpha: float = ALPHA,
             reference_edges: Sequence[float] = (0.02,)) -> Dict[str, Any]:
    """Blocco di significativita' da un insieme di righe del ledger.

    Accetta le righe di `tracker.get_predictions` (o qualunque dict con
    `esito_finale`, `profit`, `prob`, `quota`). Non solleva MAI: ogni campo
    malformato viene contato come `bad_rows`, mai fatto sparire.

    Il blocco contiene:
      * `n_closed` / `won` / `lost` / `push` / `other` — il campione;
      * `roi` + `roi_ci95` + `roi_p` — il ROI con il suo intervallo e il test;
      * `hit_rate` + `hit_ci95` + `hit_vs_model` + `hit_vs_breakeven`;
      * `status` — il VERDETTO machine-readable (vedi STATUS_*);
      * `required` — chiusure necessarie per alcuni edge di riferimento;
      * `note` — la frase da mostrare accanto al ROI.
    """
    won = lost = push = other = 0
    profits: List[float] = []
    probs_closed: List[float] = []
    odds_closed: List[float] = []
    bad_rows = 0

    src = rows if isinstance(rows, (list, tuple)) else list(rows or [])
    for row in src:
        if not isinstance(row, dict):
            bad_rows += 1
            continue
        try:
            verdict = row.get("esito_finale")
            if verdict is None or str(verdict).strip() == "":
                continue                      # aperta: non entra nel campione
            label = str(verdict).strip().lower()
            profit = _as_float(row.get("profit"))
            if profit is not None:
                profits.append(profit)
            else:
                bad_rows += 1
            prob = _as_float(row.get("prob"))
            odds = _as_float(row.get("quota"))
            if label == "won":
                won += 1
            elif label == "lost":
                lost += 1
            elif label == "push":
                push += 1
            else:
                other += 1                    # verdetto inatteso: contato a parte
            if label in ("won", "lost"):
                # La prob. di riferimento si prende SOLO sulle righe che
                # concorrono all'hit rate: includere i push confronterebbe una
                # frequenza (won/(won+lost)) con una media calcolata su un
                # insieme diverso.
                if prob is not None:
                    probs_closed.append(prob)
                if odds is not None:
                    odds_closed.append(odds)
        except Exception:
            bad_rows += 1

    closed = won + lost
    block: Dict[str, Any] = {
        "n_closed": len(profits), "won": won, "lost": lost, "push": push,
        "other": other, "bad_rows": bad_rows,
        "alpha": alpha, "min_samples": MIN_SAMPLES,
        "scipy": available(),
    }
    if not available():
        block["status"] = STATUS_UNAVAILABLE
        block["note"] = "scipy non disponibile: significativita' non calcolata"
        return block

    block["roi"] = round(sum(profits) / len(profits), 6) if profits else None
    roi = roi_test(profits, alpha=alpha)
    if roi:
        block["roi_ci95"] = [roi["ci_low"], roi["ci_high"]]
        block["roi_p"] = roi["p_value"]
        block["roi_t"] = roi["t"]
        block["roi_sd"] = roi["sd"]
        block["roi_degenerate"] = bool(roi.get("degenerate"))
    else:
        block["roi_ci95"] = None
        block["roi_p"] = None

    hit_rate = (won / closed) if closed else None
    block["hit_rate"] = round(hit_rate, 6) if hit_rate is not None else None
    ci = wilson_interval(won, closed, alpha=alpha) if closed else None
    block["hit_ci95"] = [round(ci[0], 6), round(ci[1], 6)] if ci else None

    avg_prob = (sum(probs_closed) / len(probs_closed)) if probs_closed else None
    avg_odds = (sum(odds_closed) / len(odds_closed)) if odds_closed else None
    block["avg_model_prob"] = round(avg_prob, 6) if avg_prob is not None else None
    block["avg_odds"] = round(avg_odds, 4) if avg_odds is not None else None
    block["hit_vs_model"] = (
        hit_rate_test(won, closed, avg_prob, alpha=alpha)
        if (closed and avg_prob is not None) else None)
    be = breakeven_hit_rate(avg_odds)
    block["breakeven_hit_rate"] = round(be, 6) if be is not None else None
    block["hit_vs_breakeven"] = (
        hit_rate_test(won, closed, be, alpha=alpha)
        if (closed and be is not None) else None)

    # Verdetto: prima il campione, poi il segno e la sua significativita'.
    if not profits:
        block["status"] = STATUS_UNAVAILABLE
    elif len(profits) < MIN_SAMPLES:
        block["status"] = STATUS_INSUFFICIENT
    elif block.get("roi_p") is not None and block["roi_p"] < alpha:
        block["status"] = (STATUS_POSITIVE if (block["roi"] or 0) > 0
                           else STATUS_NEGATIVE)
    else:
        block["status"] = STATUS_NO_EDGE

    # Edge minimo distinguibile con QUESTO campione: la lettura onesta di un
    # gate di decisione (un ROI dentro l'intervallo e' indistinguibile da zero).
    block["detectable_edge"] = detectable_edge(
        len(profits), sd=block.get("roi_sd"), odds=avg_odds, prob=avg_prob,
        alpha=alpha)

    # Campione necessario: con l'SD osservato (se c'e') e con la stima da quota.
    needed: Dict[str, Any] = {}
    obs_sd = block.get("roi_sd")
    edges = list(reference_edges or [])
    if block.get("roi") and block["roi"] > 0:
        edges.append(block["roi"])            # "quante chiusure per confermare QUESTO ROI"
    for edge in edges:
        key = f"edge_{edge * 100:g}pct"
        if key in needed:
            continue
        needed[key] = {
            "edge": edge,
            "observed_sd": required_n(edge, sd=obs_sd) if obs_sd else None,
            "from_odds": required_n(edge, odds=avg_odds, prob=avg_prob),
        }
    block["required"] = needed

    block["note"] = _verdict_note(block)
    return block


def _verdict_note(block: Dict[str, Any]) -> str:
    """Frase leggibile accanto al ROI (mai una conclusione oltre i numeri)."""
    status = block.get("status")
    n = int(block.get("n_closed") or 0)
    if status == STATUS_INSUFFICIENT:
        return (f"{n} chiusure < {MIN_SAMPLES}: il ROI e' rumore, non una misura "
                f"(nessuna conclusione ammessa)")
    if status == STATUS_UNAVAILABLE:
        return "campione non calcolabile"
    roi_txt = _pct(block.get("roi"))
    if status == STATUS_POSITIVE:
        return f"ROI {roi_txt} DISTINGUIBILE da zero (p={block.get('roi_p')})"
    if status == STATUS_NEGATIVE:
        return f"ROI {roi_txt} NEGATIVO in modo significativo (p={block.get('roi_p')})"
    p = block.get("roi_p")
    extra = f" (p={p})" if p is not None else ""
    return f"ROI {roi_txt} NON distinguibile da zero{extra}"


def _pct(value: Any, digits: int = 2) -> str:
    v = _as_float(value)
    return "n.d." if v is None else f"{v * 100:+.{digits}f}%"


def verdict(block: Optional[Dict[str, Any]]) -> str:
    """Etichetta leggibile del verdetto di un blocco (fail-safe)."""
    if not isinstance(block, dict):
        return STATUS_LABEL[STATUS_UNAVAILABLE]
    return STATUS_LABEL.get(str(block.get("status")), STATUS_LABEL[STATUS_UNAVAILABLE])


def format_lines(block: Optional[Dict[str, Any]], label: str = "",
                 indent: str = "   ") -> List[str]:
    """Righe pronte per un report (Telegram/CLI). Lista vuota se non c'e' nulla.

    Il campione insufficiente stampa UNA riga secca invece del blocco intero:
    un intervallo di confidenza su 4 righe suggerirebbe una precisione che non
    esiste.
    """
    if not isinstance(block, dict):
        return []
    try:
        n = int(block.get("n_closed") or 0)
    except (TypeError, ValueError):
        n = 0
    # L'indentazione va PRESERVATA: `rstrip()` su una testa senza etichetta la
    # cancellerebbe e le righe di significativita' finirebbero a filo margine,
    # fuori posto rispetto al gruppo a cui appartengono.
    head = f"{indent}{label} " if label else indent
    status = block.get("status")
    if status in (STATUS_INSUFFICIENT, STATUS_UNAVAILABLE) or not n:
        return [f"{head}🧮 {verdict(block)} ({n} chiusure"
                + (f", soglia {MIN_SAMPLES}" if status == STATUS_INSUFFICIENT else "")
                + ")"]
    out = [f"{head}🧮 {verdict(block)} — {n} chiuse | ROI {_pct(block.get('roi'))}"
           f" CI95 [{_pct(_ci(block, 0))}, {_pct(_ci(block, 1))}]"
           f" | p={block.get('roi_p')}"]
    hr = _as_float(block.get("hit_rate"))
    if hr is not None:
        line = f"{indent}   hit {hr * 100:.1f}%"
        ci = block.get("hit_ci95") or []
        if len(ci) == 2:
            line += f" CI95 [{ci[0] * 100:.1f}%, {ci[1] * 100:.1f}%]"
        for key, name in (("hit_vs_model", "modello"),
                          ("hit_vs_breakeven", "break-even")):
            test = block.get(key)
            if isinstance(test, dict):
                flag = "❌ significativo" if test.get("significant") else "n.s."
                line += (f" | vs {name} {test['expected'] * 100:.1f}% "
                         f"(Δ {test['delta_pp']:+.1f}pp, {flag})")
        out.append(line)
    det = _as_float(block.get("detectable_edge"))
    if det is not None:
        out.append(f"{indent}   edge minimo distinguibile con {n} chiuse: "
                   f"{det * 100:.1f}% (potenza {POWER * 100:.0f}%)")
    req = block.get("required") or {}
    for key, value in req.items():
        v = value or {}
        n_obs = v.get("observed_sd")
        n_odds = v.get("from_odds")
        edge = _as_float(v.get("edge"))
        if edge is None:
            continue
        bits = []
        if n_obs:
            bits.append(f"{n_obs:,} (SD osservato)")
        if n_odds and n_odds != n_obs:
            bits.append(f"{n_odds:,} (stima da quota)")
        if bits:
            out.append(f"{indent}   per un edge di {edge * 100:+.1f}% servono "
                       + " / ".join(bits) + " chiuse")
    return out


def _ci(block: Dict[str, Any], idx: int) -> Optional[float]:
    ci = block.get("roi_ci95")
    if not isinstance(ci, (list, tuple)) or len(ci) != 2:
        return None
    return _as_float(ci[idx])


# ---------------------------------------------------------------------------
# Lettura dal ledger (sola lettura; il modulo diagnostico non scrive mai)
# ---------------------------------------------------------------------------


def _ledger_rows(*, market: Optional[str] = None, statuses: Any = None,
                 since: Any = None, odds_min: Optional[float] = None,
                 odds_max: Optional[float] = None,
                 db_path: Optional[Any] = None) -> List[Dict[str, Any]]:
    """Righe CHIUSE del ledger (read-only), con i filtri condivisi del progetto.

    Import PIGRO di `tracker` + connessione in sola lettura quando il modulo
    legge il DB per conto suo (`db_path`): la diagnostica non deve poter
    scrivere sul ledger nemmeno per sbaglio.
    """
    import sqlite3
    if db_path is None:
        from tracker import DB_PATH as _db, get_predictions
        rows = get_predictions(mercato=market, closed=True, limit=100000,
                               created_since=since, odds_min=odds_min,
                               odds_max=odds_max)
    else:
        uri = f"file:{Path(db_path).as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            q = ("SELECT match_id, mercato, esito, quota, prob, ev, market_prob, "
                 "market_edge, status, esito_finale, profit, created_at, "
                 "settled_at, league FROM predictions "
                 "WHERE esito_finale IS NOT NULL")
            args: List[Any] = []
            if market:
                q += " AND mercato=?"
                args.append(market)
            q += " ORDER BY id DESC LIMIT 100000"
            rows = [dict(r) for r in cur.execute(q, args).fetchall()]
        finally:
            conn.close()
        from tracker import filter_predictions
        rows = filter_predictions(rows, created_since=since, odds_min=odds_min,
                                  odds_max=odds_max)
    if statuses is not None:
        wanted = {str(s).strip().lower() for s in statuses}
        rows = [r for r in rows
                if str(r.get("status") or "").strip().lower() in wanted]
    return rows


def from_ledger(*, market: Optional[str] = None, statuses: Any = None,
                since: Any = None, odds_min: Optional[float] = None,
                odds_max: Optional[float] = None, db_path: Optional[Any] = None,
                by_market: bool = False,
                all_statuses: bool = False) -> Dict[str, Any]:
    """Blocco di significativita' letto dal ledger locale (zero crediti).

    Default: SOLO le righe giocabili (`value_filter.PLAYABLE_TIERS`), perche'
    un ROI che somma cio' che i gate hanno scartato non descrive nessuna
    strategia. `all_statuses=True` toglie il filtro: e' la modalita'
    CONFRONTO (stessa semantica di `market_diagnose --all-statuses`), non una
    modalita' decisionale. `by_market=True` aggiunge il dettaglio per mercato.

    Fail-safe: qualunque errore di lettura ritorna un blocco `unavailable` con
    il motivo, mai un'eccezione (la diagnostica non deve poter rompere un
    report).
    """
    if all_statuses:
        statuses = None
    elif statuses is None:
        try:
            from value_filter import PLAYABLE_TIERS
            statuses = PLAYABLE_TIERS
        except Exception:
            statuses = None
    out: Dict[str, Any] = {"by_market": {}, "statuses": list(statuses or []),
                           "all_statuses": bool(all_statuses)}
    try:
        rows = _ledger_rows(market=market, statuses=statuses, since=since,
                            odds_min=odds_min, odds_max=odds_max,
                            db_path=db_path)
    except Exception as exc:
        logger.debug("significance: lettura ledger fallita: %s", exc)
        return {"status": STATUS_UNAVAILABLE, "error": str(exc),
                "by_market": {}, "statuses": list(statuses or [])}
    out.update(evaluate(rows))
    out["rows"] = len(rows)
    if by_market:
        buckets: Dict[str, List[Dict[str, Any]]] = {}
        for row in rows:
            buckets.setdefault(str(row.get("mercato") or "?"), []).append(row)
        out["by_market"] = {k: evaluate(v) for k, v in sorted(buckets.items())}
    out["filtro"] = {"since": since, "odds_min": odds_min, "odds_max": odds_max,
                     "applied": bool(since) or odds_min is not None
                     or odds_max is not None}
    return out


def format_report(data: Optional[Dict[str, Any]] = None) -> str:
    """Report leggibile (CLI/Telegram), mai un'eccezione."""
    rep = data if isinstance(data, dict) else from_ledger(by_market=True)
    lines = ["🧮 SIGNIFICATIVITA' STATISTICA (ledger, sola lettura)",
             f"scipy: {'disponibile' if available() else 'ASSENTE (degradato)'} "
             f"| alpha {rep.get('alpha', ALPHA)} | potenza {POWER} "
             f"| soglia campione {MIN_SAMPLES} chiusure"]
    filtro = rep.get("filtro") or {}
    if filtro.get("applied"):
        bits = []
        if filtro.get("since"):
            bits.append(f"era dal {filtro['since']}")
        lo, hi = filtro.get("odds_min"), filtro.get("odds_max")
        if lo is not None or hi is not None:
            bits.append(f"quota {lo if lo is not None else '-'}-"
                        f"{hi if hi is not None else '-'}")
        lines.append("Filtro: " + " | ".join(bits))
    else:
        lines.append("Filtro: NESSUNO — mescola ere/strategie diverse "
                     "(usare --since, es. --since 2026-09-19)")
    if rep.get("error"):
        lines.append(f"⚠️  lettura non riuscita: {rep['error']}")
    statuses = rep.get("statuses") or []
    if statuses:
        lines.append("Popolazione: stati giocabili " + "/".join(statuses))
    lines.extend(format_lines(rep, label="TOTALE", indent=""))
    by_market = rep.get("by_market") or {}
    for market, block in by_market.items():
        lines.extend(format_lines(block, label=market))
    if rep.get("all_statuses"):
        lines.append("⚠️  TUTTO il ledger (CONFRONTO, non decisionale): il "
                     "campione include cio' che i gate hanno scartato.")
    if not by_market and not rep.get("n_closed"):
        lines.append("ℹ️  Nessuna riga giocabile chiusa: niente da misurare.")
    return "\n".join(lines)


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        description="Significativita' statistica del ledger (sola lettura, "
                    "zero crediti)")
    ap.add_argument("--json", action="store_true", help="output JSON")
    ap.add_argument("--market", default=None,
                    help="solo un mercato (1X2/OE/AH/BTTS)")
    ap.add_argument("--all-statuses", action="store_true",
                    help="include anche le righe non giocabili (CONFRONTO, "
                         "non decisionale)")
    ap.add_argument("--since", default=None, metavar="YYYY-MM-DD",
                    help="solo segnali NATI da questa data")
    ap.add_argument("--odds-min", type=float, default=None)
    ap.add_argument("--odds-max", type=float, default=None)
    ap.add_argument("--db", default=None, help="DB alternativo (aperto in sola lettura)")
    args = ap.parse_args(argv)

    # Default: SOLO giocabili. `--all-statuses` toglie il filtro (e' un
    # CONFRONTO, non una modalita' decisionale: stessa semantica di
    # `market_diagnose --all-statuses`).
    data = from_ledger(market=args.market, since=args.since,
                       odds_min=args.odds_min, odds_max=args.odds_max,
                       db_path=args.db, by_market=True,
                       all_statuses=args.all_statuses)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    else:
        print(format_report(data))
    return 0


if __name__ == "__main__":                        # pragma: no cover
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    raise SystemExit(main())
