"""Rischio di PORTAFOGLIO — distribuzione ESATTA del P/L di un blocco correlato.

Perche' esiste (modulo 6 del programma quantitativo, `QUANT_MODULES.md`)
-----------------------------------------------------------------------
`auto_bet.apply_correlation_cap` raggruppa i candidati per partita (o per
lega+finestra) e taglia il blocco al 30% del bankroll. E' una PROTESI: il 30%
e' una costante scelta a mano, non una misura del rischio del blocco.

Quando due o piu' esiti stanno sulla STESSA partita la loro dipendenza non e'
un parametro da stimare: e' una funzione ESATTA del punteggio, e il progetto
modella gia' quella distribuzione (`poisson_engine`, Poisson + Dixon-Coles).
Questo modulo enumera le celle della griglia punteggi e calcola:

  - la distribuzione del P/L del blocco (media, dev. std., p5/p50/p95);
  - P(perdita), P(perdita oltre X% del bankroll) per ogni livello dichiarato;
  - la CORRELAZIONE esatta fra le gambe (covarianza dai punteggi condivisi);
  - il rapporto di diversificazione: std congiunta vs std se le gambe fossero
    indipendenti (quanto il cap sta proteggendo davvero).

Per gli esiti su partite DIVERSE il modello non dice nulla (non c'e' variabile
condivisa) e la correlazione va STIMATA dalla storia: `empirical_block_correlation`
lo fa sui risultati salvati (`match_results`, sola lettura) e dichiara sempre il
PROXY usato e il campione. Mai un numero di correlazione inventato.

Regole di progetto rispettate
-----------------------------
- La PROBABILITA' viene da `poisson_engine` (griglia Dixon-Coles): qui vivono
  solo le REGOLE DI SALDO per cella (1X2/OU/AH/BTTS), le stesse di
  `ou_outcome_probs`/`ah_outcome_probs` — un test di PARITA' le confronta.
- La convenzione degli esiti a linea (linea + lato dal testo di ledger) NON e'
  ricopiata: si delega a `multi_market.order_target`, che e' l'unica
  definizione in casa.
- Sola LETTURA (`mode=ro`), zero rete, zero crediti, zero ordini. Nessuna
  scrittura sul ledger: e' una diagnostica, non un percorso d'ordine.
- Fail-safe: nessuna funzione pubblica solleva al chiamante; un errore diventa
  un campo (`error`/`unsupported`), mai un traceback.
- Nessuna soglia nuova: i cap di produzione si LEGGONO (`auto_bet`), cosi' il
  report confronta sempre il cap vero con il rischio misurato.

CLI
---
  venv/bin/python portfolio_risk.py [--json] [--days N] [--bankroll N]
  venv/bin/python portfolio_risk.py --block "1X2:1:1.65:5" --block "OU:Over 2.5:1.90:5" \
                                    --lambdas 1.6 1.1 [--bankroll 47]
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from itertools import combinations
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# Griglia punteggi: stesso default di `poisson_engine` (nessuna capacita' in
# piu' inventata qui: un modello diverso sarebbe un secondo modello).
MAX_GOALS = 10

# Livelli di drawdown misurati per blocco (frazioni di bankroll). Sono i
# livelli che il bot vive davvero: 5% = stop giornaliero, 10% = drawdown
# protection/stop settimanale 12%, 20% = perdita che il recinto non assorbe.
DD_LEVELS = (0.05, 0.10, 0.20)

# Campione minimo perche' una correlazione empirica sia leggibile.
MIN_PAIRS = 200
# Tetto ai confronti per blocco: un blocco con 10 partite ha 45 coppie, con
# 40 ne ha 780 — il numero che conta e' la copertura, non l'esplosione.
MAX_PAIRS_PER_BLOCK = 200
# Baseline di controllo: coppie fra blocchi diversi (deve risultare ~0; se non
# lo e', il proxy sta misurando un trend comune — giornata, non correlazione).
BASELINE_PAIRS = 2000

# Mercati con griglia punteggi. Tennis/ML sono a 2 esiti senza punteggio: NON
# sono modellabili qui (fail-closed dichiarato, non un valore approssimato).
SCORE_MARKETS = ("1X2", "OU", "AH", "BTTS")
_MARKET_ALIASES = {
    "": "1X2", "H2H": "1X2", "MATCH": "1X2", "MATCH ODDS": "1X2",
    "TOTALS": "OU", "TOTAL": "OU", "OVER/UNDER": "OU", "O/U": "OU",
    "SPREADS": "AH", "ASIAN HANDICAP": "AH", "HANDICAP": "AH",
    "BTTS": "BTTS", "GG/NG": "BTTS", "BOTH TEAMS TO SCORE": "BTTS",
}
_CANON_1X2 = {"1": "1", "HOME": "1", "CASA": "1", "X": "X", "DRAW": "X",
              "TIE": "X", "PAREGGIO": "X", "2": "2", "AWAY": "2",
              "TRASFERTA": "2"}


# ---------------------------------------------------------------------------
# Griglia punteggi e maschere di saldo
# ---------------------------------------------------------------------------

def _grid(lam_h: float, lam_a: float, max_goals: int = MAX_GOALS) -> np.ndarray:
    """Matrice punteggi dal modello di produzione (Dixon-Coles).

    Import pigro di `poisson_engine`: una formula ricopiata qui divergerebbe
    dal modello che scommette, ed e' esattamente cio' che il progetto vieta.
    """
    from poisson_engine import _probs_grid
    return np.asarray(_probs_grid(float(lam_h), float(lam_a), int(max_goals)))


def _masks(max_goals: int = MAX_GOALS):
    """Indici di casa/trasferta, totale e margine (definizione di `poisson_engine`)."""
    from poisson_engine import _margin_grid
    return _margin_grid(int(max_goals))


def _market_of(leg: Dict[str, Any]) -> str:
    raw = str(leg.get("mercato") or leg.get("market") or leg.get("market_type") or "")
    key = raw.strip().upper().replace("_", " ")
    return _MARKET_ALIASES.get(key, key)


def _float(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def parse_leg(leg: Dict[str, Any]) -> Dict[str, Any]:
    """Normalizza una gamba: {market_type, selection, side, line, odds, stake}.

    Solleva `ValueError` con motivo machine-readable quando la gamba non e'
    rappresentabile sulla griglia punteggi: un mercato non modellabile NON
    deve entrare in un conto di rischio con un valore approssimato.
    """
    market = _market_of(leg)
    if market not in SCORE_MARKETS:
        raise ValueError("mercato_senza_griglia_punteggi:%s" % (market or "?"))
    odds = _float(leg.get("quota") if leg.get("quota") is not None else
                  leg.get("odds", leg.get("price")))
    if odds is None or odds <= 1.0:
        raise ValueError("odds_non_valida")
    stake = _float(leg.get("stake"))
    if stake is None or stake <= 0:
        raise ValueError("stake_non_valido")
    esito = str(leg.get("esito_key") or leg.get("esito") or leg.get("selection") or "").strip()
    if not esito:
        raise ValueError("esito_assente")

    selection: Any = esito
    line: Optional[float] = None
    side: Optional[str] = None

    if market == "1X2":
        key = esito.upper()
        if key in _CANON_1X2:
            selection = _CANON_1X2[key]
        else:
            # Nome squadra (il ledger e' misto: `fixture_engine` scrive il nome
            # giocato, `sx_signals` scrive 1/X/2). Stessa canonicalizzazione
            # della catena di decisione: mai indovinare.
            try:
                from decision.adapters import canonical_outcome
                selection = canonical_outcome(esito, leg.get("home") or
                                              leg.get("home_team"),
                                              leg.get("away") or
                                              leg.get("away_team"))
            except Exception:
                selection = None
            if selection not in ("1", "X", "2"):
                raise ValueError("esito_non_canonico")
    elif market == "OU":
        target = _order_target(leg)
        if not target:
            raise ValueError("linea_non_leggibile")
        line, side = float(target["line"]), str(target["side"])
        selection = "%s %.3g" % ("Over" if side == "over" else "Under", line)
    elif market == "AH":
        target = _order_target(leg)
        if not target:
            raise ValueError("linea_non_leggibile")
        line, side = float(target["line"]), str(target["side"])
        # `order_target` restituisce la linea dal punto di vista di teamOne
        # (casa) — e' la convenzione della RISOLUZIONE del mercato su SX. Il
        # motore (`ah_outcome_probs`) vuole invece la linea DEL LATO scelto:
        # per l'ospite il segno si ribalta. Senza questo passaggio due lati
        # complementari (Home -0.75 / Away +0.75) NON sommano a 1: misurato il
        # 14/10, `ah_outcome_probs(-0.75, side='away')` = P(margin < -0.75),
        # cioe' la probabilita' della CASA, non dell'ospite.
        if side == "away":
            line = -line
        selection = "%s %+.2f" % ("Home" if side == "home" else "Away", line)
    else:  # BTTS
        key = esito.strip().lower()
        if key in ("yes", "si", "sì", "gg", "goal", "gol"):
            selection = "Yes"
        elif key in ("no", "ng", "nogoal"):
            selection = "No"
        else:
            raise ValueError("esito_non_canonico")

    return {"market_type": market, "selection": selection, "side": side,
            "line": line, "odds": odds, "stake": stake,
            "raw_selection": esito}


def _order_target(leg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Linea+lato dal testo di ledger: definizione UNICA di `multi_market`.

    Il mercato viene CANONICALIZZATO prima della chiamata (`totals` -> `OU`,
    `spreads` -> `AH`): `order_target` accetta solo i nomi canonici, e lasciare
    il nome grezzo del provider lo farebbe rispondere None — un alias di
    mercato non e' un errore del dato.
    """
    try:
        from multi_market import order_target
        pick = dict(leg)
        pick["mercato"] = _market_of(leg)
        return order_target(pick)
    except Exception as e:  # pragma: no cover - difesa
        logger.debug("portfolio_risk: order_target non disponibile: %s", e)
        return None


def leg_cell_outcomes(leg: Dict[str, Any], max_goals: int = MAX_GOALS):
    """(win_frac, push_frac) per OGNI cella della griglia punteggi.

    `win_frac`/`push_frac` sono in [0,1] e sommano a <= 1: le quarter line
    valgono come due mezze puntate (0.5 ciascuna), esattamente come in
    `poisson_engine.ou_outcome_probs`/`ah_outcome_probs`. La probabilita' di
    vincita di una gamba NON e' calcolata qui: e' `sum(grid * win_frac)`, e un
    test di parita' la confronta con le funzioni del motore.
    """
    spec = parse_leg(leg)
    hg, ag, totals, margins = _masks(max_goals)
    zeros = np.zeros_like(margins, dtype=float)
    market = spec["market_type"]

    if market == "1X2":
        win = {"1": hg > ag, "X": hg == ag, "2": hg < ag}[spec["selection"]]
        return win.astype(float), zeros
    if market == "BTTS":
        both = (hg >= 1) & (ag >= 1)
        win = both if spec["selection"] == "Yes" else ~both
        return win.astype(float), zeros
    if market == "OU":
        halves = _halves(spec["line"])
        win = zeros.copy()
        push = zeros.copy()
        for hline, share in halves:
            win = win + share * (totals > hline)
            push = push + share * (totals == hline)
        if spec["side"] == "under":  # stesso push, lato opposto
            win = zeros.copy()
            for hline, share in halves:
                win = win + share * (totals < hline)
        return win.astype(float), push.astype(float)
    # AH
    halves = _halves(spec["line"])
    win = zeros.copy()
    push = zeros.copy()
    for hline, share in halves:
        net = (margins + hline) if spec["side"] == "home" else (-margins + hline)
        win = win + share * (net > 0)
        push = push + share * (net == 0)
    return win.astype(float), push.astype(float)


def _halves(line: float) -> Tuple[Tuple[float, float], ...]:
    """Linea intera -> se stessa; quarter line (.25/.75) -> due mezze puntate."""
    if abs(float(line) * 2) % 1 != 0:
        lo = math.floor(float(line) * 2) / 2.0
        return ((lo, 0.5), (lo + 0.5, 0.5))
    return ((float(line), 1.0),)


def _leg_pl(leg: Dict[str, Any], win: np.ndarray, push: np.ndarray) -> np.ndarray:
    """P/L per cella punteggio: vincita a quota, push a 0, sconfitta a -stake."""
    spec = parse_leg(leg)
    stake = spec["stake"]
    lose = np.clip(1.0 - win - push, 0.0, 1.0)
    return win * stake * (spec["odds"] - 1.0) - lose * stake


# ---------------------------------------------------------------------------
# Distribuzione del blocco (STESSA partita: matematica esatta)
# ---------------------------------------------------------------------------

def _quantiles(values: np.ndarray, probs: np.ndarray, levels: Sequence[float]):
    order = np.argsort(values, kind="stable")
    v, p = values[order], probs[order]
    cum = np.cumsum(p)
    out = []
    for level in levels:
        idx = int(np.searchsorted(cum, level))
        out.append(float(v[min(idx, len(v) - 1)]))
    return out


def block_distribution(legs: Sequence[Dict[str, Any]], lam_h: float, lam_a: float,
                       *, max_goals: int = MAX_GOALS,
                       bankroll: Optional[float] = None) -> Dict[str, Any]:
    """Distribuzione ESATTA del P/L di un blocco di esiti sulla stessa partita.

    Non c'e' nessuna stima: ogni cella della griglia punteggi assegna un P/L
    determinato a tutte le gambe insieme, quindi la media, la varianza, le
    code e le correlazioni sono proprieta' della distribuzione (nessuna copula
    da fittare, nessun rho da indovinare).
    """
    out: Dict[str, Any] = {"lam_h": _float(lam_h), "lam_a": _float(lam_a),
                           "unsupported": [], "error": None}
    if out["lam_h"] is None or out["lam_a"] is None or out["lam_h"] <= 0 or out["lam_a"] <= 0:
        out["error"] = "lambdas_non_valide"
        return out
    try:
        grid = _grid(out["lam_h"], out["lam_a"], max_goals)
        probs = grid.reshape(-1)
        total_pl = np.zeros_like(probs)
        legs_norm: List[Dict[str, Any]] = []
        marginals: List[Dict[str, Any]] = []
        for leg in legs or []:
            try:
                spec = parse_leg(leg)
                win, push = leg_cell_outcomes(leg, max_goals)
                pl = _leg_pl(leg, win, push)
            except ValueError as e:
                out["unsupported"].append({"leg": _leg_label(leg), "reason": str(e)})
                continue
            total_pl = total_pl + pl.reshape(-1)
            legs_norm.append(spec)
            marginals.append({
                "selection": spec["selection"], "market_type": spec["market_type"],
                "odds": spec["odds"], "stake": spec["stake"],
                "p_win": float((probs * win.reshape(-1)).sum()),
                "p_push": float((probs * push.reshape(-1)).sum()),
                "p_lose": float((probs * (1.0 - win.reshape(-1) - push.reshape(-1))).sum()),
                "payout": spec["stake"] * (spec["odds"] - 1.0),
            })
        if not legs_norm:
            out["error"] = "nessuna_gamba_valida"
            return out

        mean = float((probs * total_pl).sum())
        var = float((probs * (total_pl - mean) ** 2).sum())
        p5, p50, p95 = _quantiles(total_pl, probs, (0.05, 0.50, 0.95))
        stake_total = float(sum(s["stake"] for s in legs_norm))

        # ⚠️ Piena precisione nel PAYLOAD (l'arrotondamento e' solo di stampa:
        # `format_report` formatta, il dato resta confrontabile con quello del
        # motore). Un numero arrotondato a 6 decimali non e' verificabile
        # contro `poisson_engine` e la lezione e' gia' stata pagata il 02/10
        # negli script analitici.
        out.update({
            "n_legs": len(legs_norm),
            "legs": legs_norm,
            "marginals": marginals,
            "stake_total": stake_total,
            "max_loss": float(total_pl.min()),
            "max_win": float(total_pl.max()),
            "mean": mean,
            "std": math.sqrt(var),
            "p5": p5, "p50": p50, "p95": p95,
            "p_loss": float(probs[total_pl < -1e-12].sum()),
            "p_profit": float(probs[total_pl > 1e-12].sum()),
            # Il P/L del blocco e' sempre <= 0 o >= 0 su alcune celle: la
            # probabilita' di "pareggio" non e' zero e va dichiarata, altrimenti
            # le tre probabilita' non sommano a 1 e sembrano un errore.
            "p_flat": float(probs[np.abs(total_pl) <= 1e-12].sum()),
            "worst_cell": _worst_cell(total_pl, probs, grid.shape, max_goals),
        })
        if bankroll and float(bankroll) > 0:
            br = float(bankroll)
            out["bankroll"] = br
            out["drawdown_probs"] = {
                "%.2f" % lvl: float(probs[total_pl <= -lvl * br].sum()) for lvl in DD_LEVELS
            }
            out["stake_pct"] = stake_total / br
        return out
    except Exception as e:  # pragma: no cover - difesa
        logger.debug("portfolio_risk: distribuzione non calcolata: %s", e)
        out["error"] = "errore_calcolo"
        return out


def block_pl_distribution(legs: Sequence[Dict[str, Any]], lam_h: float, lam_a: float,
                          *, max_goals: int = MAX_GOALS) -> Dict[str, Any]:
    """Distribuzione del P/L del blocco come COPPIA (valori, probabilita').

    E' la stessa griglia di `block_distribution` — che ne e' una sintesi — ma
    restituita nella forma che serve a RAGIONARE sulla dipendenza fra blocchi:
    un vettore di esiti possibili con la sua probabilita', su cui si puo'
    applicare una copula (Modulo 6, Fase 2) senza ricostruire la griglia.

    Perche' vive QUI e non nel modulo di copula: le regole di saldo per cella
    (1X2/OU/AH/BTTS) sono UNA definizione di questo modulo — una copia nel
    modulo di portafoglio divergerebbe dal modello che scommette, ed e'
    esattamente cio' che il progetto vieta (regola del 13/09).

    Ritorna {values, probs, n_cells, unsupported, error}. `values` e' il P/L in
    USDC (positivo = vincita) per ogni cella, `probs` la sua probabilita'.
    Fail-safe: un errore diventa un campo, mai un'eccezione al chiamante.
    """
    out: Dict[str, Any] = {"values": None, "probs": None, "n_cells": 0,
                           "unsupported": [], "error": None}
    lam_h, lam_a = _float(lam_h), _float(lam_a)
    if lam_h is None or lam_a is None or lam_h <= 0 or lam_a <= 0:
        out["error"] = "lambdas_non_valide"
        return out
    try:
        grid = _grid(lam_h, lam_a, max_goals)
        probs = grid.reshape(-1)
        total = np.zeros_like(probs)
        n = 0
        for leg in legs or []:
            try:
                win, push = leg_cell_outcomes(leg, max_goals)
                pl = _leg_pl(leg, win, push).reshape(-1)
            except ValueError as e:
                out["unsupported"].append({"leg": _leg_label(leg), "reason": str(e)})
                continue
            total = total + pl
            n += 1
        if not n:
            out["error"] = "nessuna_gamba_valida"
            return out
        out.update({"values": total, "probs": probs, "n_cells": int(probs.size)})
        return out
    except Exception as e:  # pragma: no cover - difesa
        logger.debug("portfolio_risk: distribuzione del blocco non calcolata: %s", e)
        out["error"] = "errore_calcolo"
        return out


def _worst_cell(pl: np.ndarray, probs: np.ndarray, shape, max_goals: int) -> Dict[str, Any]:
    """Cella (punteggio) col P/L peggiore: dice QUALE scenario fa male."""
    idx = int(np.argmin(pl))
    hg, ag = divmod(idx, max_goals + 1)
    return {"score": "%d-%d" % (hg, ag), "pl": float(pl[idx]),
            "p": float(probs[idx])}


def independent_std(legs: Sequence[Dict[str, Any]], lam_h: float, lam_a: float,
                    *, max_goals: int = MAX_GOALS) -> Optional[float]:
    """Dev. std. del blocco SE le gambe fossero indipendenti (baseline)."""
    try:
        grid = _grid(lam_h, lam_a, max_goals)
        probs = grid.reshape(-1)
        var = 0.0
        n = 0
        for leg in legs or []:
            try:
                win, push = leg_cell_outcomes(leg, max_goals)
                pl = _leg_pl(leg, win, push).reshape(-1)
            except ValueError:
                continue
            mean = float((probs * pl).sum())
            var += float((probs * (pl - mean) ** 2).sum())
            n += 1
        return math.sqrt(var) if n else None
    except Exception as e:  # pragma: no cover - difesa
        logger.debug("portfolio_risk: std indipendente non calcolata: %s", e)
        return None


def correlation_matrix(legs: Sequence[Dict[str, Any]], lam_h: float, lam_a: float,
                       *, max_goals: int = MAX_GOALS) -> Dict[str, Any]:
    """Correlazione ESATTA fra le gambe (covarianza dai punteggi condivisi)."""
    out: Dict[str, Any] = {"labels": [], "matrix": [], "pairs": [], "error": None}
    try:
        grid = _grid(lam_h, lam_a, max_goals)
        probs = grid.reshape(-1)
        pls, labels = [], []
        for leg in legs or []:
            try:
                win, push = leg_cell_outcomes(leg, max_goals)
                pl = _leg_pl(leg, win, push).reshape(-1)
            except ValueError:
                continue
            pls.append(pl)
            labels.append(_leg_label(leg))
        out["labels"] = labels
        n = len(pls)
        mat = [[0.0] * n for _ in range(n)]
        for i, pl in enumerate(pls):
            mean_i = float((probs * pl).sum())
            sd_i = math.sqrt(float((probs * (pl - mean_i) ** 2).sum()))
            mat[i][i] = 1.0 if sd_i > 1e-12 else 0.0
            for j in range(i + 1, n):
                mean_j = float((probs * pls[j]).sum())
                sd_j = math.sqrt(float((probs * (pls[j] - mean_j) ** 2).sum()))
                if sd_i <= 1e-12 or sd_j <= 1e-12:
                    rho = 0.0
                else:
                    cov = float((probs * (pl - mean_i) * (pls[j] - mean_j)).sum())
                    rho = cov / (sd_i * sd_j)
                mat[i][j] = mat[j][i] = rho
                out["pairs"].append({"a": labels[i], "b": labels[j], "rho": rho})
        out["matrix"] = mat
        return out
    except Exception as e:  # pragma: no cover - difesa
        logger.debug("portfolio_risk: correlazioni non calcolate: %s", e)
        out["error"] = "errore_calcolo"
        return out


def diversification(legs: Sequence[Dict[str, Any]], lam_h: float, lam_a: float,
                    *, max_goals: int = MAX_GOALS) -> Dict[str, Any]:
    """Quanto il cap sta proteggendo davvero: std congiunta vs indipendente."""
    dist = block_distribution(legs, lam_h, lam_a, max_goals=max_goals)
    if dist.get("error"):
        return {"error": dist["error"]}
    ind = independent_std(legs, lam_h, lam_a, max_goals=max_goals)
    if not ind or ind <= 1e-12:
        return {"std_joint": dist["std"], "std_independent": None,
                "error": "baseline_non_calcolabile"}
    ratio = dist["std"] / ind
    return {"std_joint": dist["std"],
            "std_independent": ind,
            "ratio": ratio,
            "uplift_pct": (ratio - 1.0) * 100.0,
            "note": ("ratio > 1 = le gambe si sommano (lo stesso punteggio le "
                     "muove insieme); ratio ~ 1 = indipendenti di fatto")}


# ---------------------------------------------------------------------------
# Correlazione fra partite DIVERSE (stimata dalla storia, sola lettura)
# ---------------------------------------------------------------------------

def _pearson(pairs: Sequence[Tuple[float, float]]) -> Tuple[Optional[float], int]:
    n = len(pairs)
    if n < 2:
        return None, n
    mx = sum(p[0] for p in pairs) / n
    my = sum(p[1] for p in pairs) / n
    sxy = sum((a - mx) * (b - my) for a, b in pairs)
    sxx = sum((a - mx) ** 2 for a, _ in pairs)
    syy = sum((b - my) ** 2 for _, b in pairs)
    if sxx <= 0 or syy <= 0:
        return None, n
    return sxy / math.sqrt(sxx * syy), n


def _date_key(value: Any) -> str:
    """Data (YYYY-MM-DD) da un timestamp del ledger, formato SQLite o ISO."""
    text = str(value or "").strip()
    return text[:10] if len(text) >= 10 else ""


def empirical_block_correlation(*, conn=None, db_path: Optional[str] = None,
                                days: Optional[int] = None,
                                time_window_hours: int = 6,
                                min_pairs: int = MIN_PAIRS) -> Dict[str, Any]:
    """Correlazione EMPIRICA fra esiti di partite diverse nello stesso blocco.

    Proxy dichiarato: indicatore binario "ha vinto la casa" per partita, coppie
    dentro la STESSA lega e la STESSA giornata di calendario (i kickoff di una
    lega nello stesso giorno condividono il contesto: forma, turni, arbitri).
    Non e' la correlazione del P/L di mercati specifici (per quella servirebbe
    una storia di quote per mercato): e' il numero che dice se il raggruppamento
    per lega+finestra di `auto_bet` ha un fondamento misurabile.

    Il campione e' dichiarato sempre (`n_pairs`) e la BASELINE fra blocchi
    diversi e' misurata a parte: se anche la baseline fosse alta, il proxy
    starebbe leggendo un trend comune (giornata) e non la correlazione interna.
    """
    out: Dict[str, Any] = {"status": "no_data", "n_pairs": 0, "rho": None,
                           "baseline_rho": None, "rows": 0, "min_pairs": min_pairs,
                           "proxy": "home_win_binary", "caveat": None, "error": None}
    own = conn is None
    try:
        if own:
            conn = _ro_conn(db_path)
        where, params = "", []
        if days:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=int(days))
                      ).strftime("%Y-%m-%d %H:%M:%S")
            where = "WHERE datetime(settled_at) >= datetime(?)"
            params.append(cutoff)
        rows = conn.execute(
            "SELECT league, score_home, score_away, settled_at FROM match_results "
            + where, params).fetchall()
        out["rows"] = len(rows)
        blocks: Dict[Tuple[str, str], List[Tuple[str, float]]] = {}
        for league, sh, sa, settled in rows:
            sh_f, sa_f = _float(sh), _float(sa)
            if sh_f is None or sa_f is None:
                continue
            day = _date_key(settled)
            if not day:
                continue
            blocks.setdefault((str(league or ""), day), []).append(
                (str(settled or ""), 1.0 if sh_f > sa_f else 0.0))

        same: List[Tuple[float, float]] = []
        keyed: List[Tuple[Tuple[str, str], float]] = []
        for key in sorted(blocks):
            matches = sorted(blocks[key])
            for a, b in list(combinations(matches, 2))[:MAX_PAIRS_PER_BLOCK]:
                same.append((a[1], b[1]))
            keyed.extend((key, m[1]) for m in matches)

        rho, n = _pearson(same)
        out["n_pairs"] = n
        out["rho"] = rho
        # Baseline deterministica per non allocare una seconda lista enorme:
        # ogni partita viene accoppiata con quella a meta' elenco di distanza
        # (il piu' delle volte un ALTRO blocco = rumore di fondo). La versione
        # "coppie adiacenti" misurava quasi solo accoppiamenti interni allo
        # stesso blocco — la baseline risultava non calcolabile (0 coppie
        # utili) proprio quando serviva, e un None si legge come "nessun
        # problema" invece che "non misurato".
        base: List[Tuple[float, float]] = []
        total = len(keyed)
        half = max(1, total // 2)
        step = max(1, total // max(1, BASELINE_PAIRS))
        for i in range(0, total, step):
            j = (i + half) % total
            if keyed[i][0] != keyed[j][0]:
                base.append((keyed[i][1], keyed[j][1]))
            if len(base) >= BASELINE_PAIRS:
                break
        rho_b, n_b = _pearson(base)
        out["baseline_pairs"] = n_b
        out["baseline_rho"] = rho_b

        if not n:
            out["status"] = "no_data"
            out["caveat"] = "nessun blocco con almeno due partite concluse"
        elif n < min_pairs:
            out["status"] = "insufficient"
            out["caveat"] = ("campione %d coppie < %d: rumore, non una misura"
                             % (n, min_pairs))
        else:
            out["status"] = "ok"
            out["caveat"] = ("proxy: esito binario casa/trasferta, coppie nella "
                             "stessa lega+giornata")
        return out
    except Exception as e:
        logger.warning("portfolio_risk: correlazione empirica non calcolata: %s", e)
        out["error"] = "read_error"
        out["caveat"] = str(e)[:120]
        return out
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Ledger (sola lettura)
# ---------------------------------------------------------------------------

def _ro_conn(db_path: Optional[str] = None) -> sqlite3.Connection:
    """Connessione in SOLA LETTURA: un errore di scrittura deve essere impossibile."""
    path = db_path
    if not path:
        from tracker import DB_PATH  # import pigro: `import portfolio_risk` resta leggero
        path = DB_PATH
    return sqlite3.connect("file:%s?mode=ro" % path, uri=True)


def open_blocks(*, conn=None, db_path: Optional[str] = None,
                mode: str = "live") -> Dict[str, Any]:
    """Blocchi di esiti ANCORA APERTI sulla stessa partita (dal ledger dei soldi).

    Legge gli stessi record che il recinto conta come capitale immobilizzato
    (`bets`, `esito_finale IS NULL AND mode=?`): il rischio che si misura qui
    e' quello delle posizioni vere, non di un portafoglio ipotetico.
    """
    out: Dict[str, Any] = {"blocks": [], "error": None, "rows": 0}
    own = conn is None
    try:
        if own:
            conn = _ro_conn(db_path)
        rows = conn.execute(
            "SELECT b.match_id, b.mercato, b.esito, b.stake, b.price, "
            "       a.lam_h, a.lam_a, m.home_team, m.away_team "
            "FROM bets b "
            "LEFT JOIN match_analysis a ON a.match_id = b.match_id "
            "LEFT JOIN matches m ON m.id = b.match_id "
            "WHERE b.esito_finale IS NULL AND b.mode = ?", (mode,)).fetchall()
        out["rows"] = len(rows)
        grouped: Dict[str, Dict[str, Any]] = {}
        for mid, market, esito, stake, price, lam_h, lam_a, home, away in rows:
            blk = grouped.setdefault(str(mid), {
                "match_id": str(mid), "lam_h": _float(lam_h), "lam_a": _float(lam_a),
                "home": home, "away": away, "legs": [],
            })
            blk["legs"].append({"mercato": market, "esito": esito, "stake": stake,
                                "quota": price, "home": home, "away": away})
        out["blocks"] = [grouped[k] for k in sorted(grouped)]
        return out
    except Exception as e:
        logger.warning("portfolio_risk: lettura blocchi aperti fallita: %s", e)
        out["error"] = "read_error"
        out["caveat"] = str(e)[:120]
        return out
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def production_caps() -> Dict[str, Any]:
    """Cap di produzione LETTI dal modulo che li applica (mai copiati)."""
    out: Dict[str, Any] = {"correlation_cap_pct": None, "open_exposure_cap_pct": None}
    try:
        import auto_bet
        out["correlation_cap_pct"] = float(auto_bet.CORRELATION_CAP_PCT)
        out["open_exposure_cap_pct"] = float(auto_bet.OPEN_EXPOSURE_CAP_PCT)
    except Exception as e:
        logger.debug("portfolio_risk: cap di produzione non leggibili: %s", e)
        out["caveat"] = str(e)[:120]
    return out


def report(*, conn=None, db_path: Optional[str] = None, bankroll: Optional[float] = None,
           days: Optional[int] = None) -> Dict[str, Any]:
    """Istantanea leggibile: rischio ESATTO dei blocchi aperti + correlazione stimata."""
    out: Dict[str, Any] = {"blocks": [], "error": None, "caps": production_caps()}
    opened = open_blocks(conn=conn, db_path=db_path)
    out["error"] = opened.get("error")
    out["open_rows"] = opened.get("rows", 0)
    for blk in opened.get("blocks", []):
        lam_h, lam_a = blk.get("lam_h"), blk.get("lam_a")
        entry: Dict[str, Any] = {"match_id": blk["match_id"],
                                 "home": blk.get("home"), "away": blk.get("away"),
                                 "n_legs": len(blk["legs"]), "lam_h": lam_h, "lam_a": lam_a}
        if not lam_h or not lam_a:
            entry["error"] = "lambdas_assenti"  # senza griglia non si misura nulla
            out["blocks"].append(entry)
            continue
        dist = block_distribution(blk["legs"], lam_h, lam_a, bankroll=bankroll)
        entry["distribution"] = dist
        if not dist.get("error"):
            entry["correlations"] = correlation_matrix(blk["legs"], lam_h, lam_a)
            entry["diversification"] = diversification(blk["legs"], lam_h, lam_a)
            cap = out["caps"].get("correlation_cap_pct")
            if cap and bankroll:
                entry["cap_pct"] = cap
                entry["stake_vs_cap"] = dist["stake_total"] / (cap * float(bankroll))
        out["blocks"].append(entry)
    out["cross_match"] = empirical_block_correlation(conn=conn, db_path=db_path,
                                                    days=days)
    return out


def format_report(data: Dict[str, Any]) -> str:
    """Report testuale (Telegram-friendly), senza parentesi tecniche inutili."""
    lines = ["📊 RISCHIO DI PORTAFOGLIO (misurato, non stimato)"]
    caps = data.get("caps") or {}
    if caps.get("correlation_cap_pct"):
        open_cap = caps.get("open_exposure_cap_pct")
        lines.append("• Cap di produzione: %.0f%% per blocco correlato%s"
                     % (caps["correlation_cap_pct"] * 100,
                        (", %.0f%% esposizione aperta" % (open_cap * 100))
                        if open_cap else ""))
    blocks = data.get("blocks") or []
    if not blocks:
        lines.append("• Nessun blocco APERTO da misurare (0 righe live aperte)")
    for b in blocks:
        # Etichetta del blocco: i nomi se ci sono, altrimenti l'id (nella
        # misura ad hoc da CLI non esiste riga `matches`: un "?" non direbbe
        # nemmeno su quale partita e' stata fatta la misura).
        if b.get("home") or b.get("away"):
            head = "• %s%s" % (b.get("home") or "?",
                               (" vs " + str(b["away"])) if b.get("away") else "")
        else:
            head = "• %s" % (b.get("match_id") or "blocco")
        if b.get("error") == "lambdas_assenti":
            lines.append(head + " — senza lambdas non misurabile (analisi assente)")
            continue
        d = b.get("distribution") or {}
        if d.get("error"):
            lines.append(head + " — %s" % d["error"])
            continue
        lines.append("%s (%d esiti, stake %.2f)" % (head, d.get("n_legs", 0), d.get("stake_total", 0.0)))
        lines.append("   P/L atteso %+.2f | std %.2f | p5 %+.2f / p50 %+.2f / p95 %+.2f"
                     % (d.get("mean", 0.0), d.get("std", 0.0), d.get("p5", 0.0),
                        d.get("p50", 0.0), d.get("p95", 0.0)))
        lines.append("   P(perdita) %.1f%% | scenario peggiore %s (P/L %+.2f, P %.2f%%)"
                     % (d.get("p_loss", 0.0) * 100,
                        (d.get("worst_cell") or {}).get("score", "?"),
                        (d.get("worst_cell") or {}).get("pl", 0.0),
                        (d.get("worst_cell") or {}).get("p", 0.0) * 100))
        dd = d.get("drawdown_probs")
        if dd:
            # Le chiavi sono FRAZIONI di bankroll ("0.05" = 5%): stamparle
            # grezze fa leggere "0.05% bankroll" — un'etichetta che dice una
            # cosa diversa dal numero. La soglia si stampa in punti percentuali
            # e la probabilita' subito dopo.
            lines.append("   P(perdita oltre): " + " | ".join(
                "oltre %.0f%% del bankroll: %.1f%%" % (float(k) * 100, v * 100)
                for k, v in dd.items()))
        for pair in (b.get("correlations") or {}).get("pairs", []):
            lines.append("   ρ(%s, %s) = %+.3f" % (pair["a"], pair["b"], pair["rho"]))
        div = b.get("diversification") or {}
        if div.get("ratio") is not None:
            lines.append("   Somma delle gambe: std congiunta %.2f vs indipendente %.2f (%+.0f%%)"
                         % (div["std_joint"], div["std_independent"], div["uplift_pct"]))
    xm = data.get("cross_match") or {}
    if xm:                      # nella misura ad hoc non c'e' nulla da dire
        lines.append("• Correlazione fra partite DIVERSE (proxy %s): %s"
                     % (xm.get("proxy", "?"),
                        ("%.3f su %d coppie" % (xm["rho"], xm.get("n_pairs", 0)))
                        if xm.get("rho") is not None else str(xm.get("status"))))
        if xm.get("baseline_rho") is not None:
            lines.append("   baseline fra blocchi diversi: %+.3f (se alta, il proxy legge un trend comune)"
                         % xm["baseline_rho"])
        if xm.get("caveat"):
            lines.append("   ⚠️ " + str(xm["caveat"]))
    if data.get("error"):
        lines.append("⚠️ errore di lettura: %s" % data["error"])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _leg_label(leg: Dict[str, Any]) -> str:
    market = _market_of(leg)
    esito = leg.get("esito_key") or leg.get("esito") or leg.get("selection") or "?"
    odds = leg.get("quota") if leg.get("quota") is not None else leg.get("odds", leg.get("price"))
    return "%s %s @%s" % (market, esito, odds)


def _parse_block_arg(text: str) -> Dict[str, Any]:
    """`"1X2:1:1.65:5"` -> gamba. Formato: MERCATO:ESITO:QUOTA:STAKE."""
    parts = str(text).split(":")
    if len(parts) < 4:
        raise ValueError("formato --block: MERCATO:ESITO:QUOTA:STAKE")
    market, esito, odds, stake = parts[0], ":".join(parts[1:-2]), parts[-2], parts[-1]
    return {"mercato": market.strip(), "esito": esito.strip(),
            "quota": float(odds), "stake": float(stake)}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Rischio di portafoglio (sola lettura)")
    parser.add_argument("--json", action="store_true", help="output JSON")
    parser.add_argument("--days", type=int, default=None,
                        help="finestra per la correlazione empirica (default: tutto)")
    parser.add_argument("--bankroll", type=float, default=None)
    parser.add_argument("--db", default=None, help="path del ledger (default tracker.DB_PATH)")
    parser.add_argument("--block", action="append", default=[],
                        help="gamba ad hoc: MERCATO:ESITO:QUOTA:STAKE (ripetibile)")
    parser.add_argument("--lambdas", nargs=2, type=float, default=None,
                        help="lam_h lam_a per la misura ad hoc")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if args.block:
        if not args.lambdas:
            print("serve --lambdas H A per una misura ad hoc")
            return 2
        legs = []
        for text in args.block:
            try:
                legs.append(_parse_block_arg(text))
            except ValueError as e:
                print("gamba non valida (%s): %s" % (text, e))
                return 2
        dist = block_distribution(legs, args.lambdas[0], args.lambdas[1],
                                  bankroll=args.bankroll)
        payload = {"distribution": dist,
                   "correlations": correlation_matrix(legs, *args.lambdas),
                   "diversification": diversification(legs, *args.lambdas),
                   "caps": production_caps()}
        if args.json:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            print(format_report({"blocks": [{"match_id": "ad-hoc",
                                             "n_legs": len(legs),
                                             "distribution": dist,
                                             "correlations": payload["correlations"],
                                             "diversification": payload["diversification"]}],
                                 "caps": payload["caps"], "cross_match": {}}))
        return 0 if not dist.get("error") else 1

    data = report(db_path=args.db, bankroll=args.bankroll, days=args.days)
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False, default=str))
    else:
        print(format_report(data))
    return 0 if not data.get("error") else 1


if __name__ == "__main__":
    raise SystemExit(main())
