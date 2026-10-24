"""Audit dell'EV: da dove viene il divario fra atteso e realizzato.

Perche' questo modulo esiste separato dal laboratorio: le misure del
`STRATEGY_RESEARCH_REPORT.md` hanno prodotto tre domande che la sola statistica
non risolve — (a) il capitale di partenza e' davvero quello dichiarato?
(b) il "+11,53% atteso" e' l'EV delle probabilita' che il sistema HA USATO?
(c) il CLV positivo misura abilita' o il margine del book?

Tre famiglie di funzioni, tutte PURE (ricevono DataFrame) o di sola lettura
(snapshot `mode=ro`):

* `capital_reconciliation` — ricostruisce il conto di cassa da un'ancora
  dichiarata (equity al trigger del CB2) e dai movimenti del ledger, e dichiara
  il residuo con la sua causa. **Il saldo esterno non viene mai preso per buono
  senza data**: senza ancora e senza timestamp il modulo risponde "non
  verificabile" invece di un numero.
* `ev_identity` / `gap_attribution` — verificano che l'EV scritto sul ledger sia
  l'EV della probabilita' scritta sullo stesso ledger, e scompongono il divario
  in tre addendi che sommano esattamente al totale (definizione, edge
  dichiarato, calibrazione residua).
* `clv_audit` — CLV con la formula del progetto (`market_calib.clv_raw`, mai
  ricopiata), con la fonte della chiusura, le echi (chiusura == segnale, dove
  il CLV e' 0 per costruzione), la stabilita' temporale e la performance
  REALIZZATA delle coorti.

Regola di progetto rispettata: nessuna soglia o formula di strategia vive qui.
Le definizioni (percentuali di quota, formula del CLV) si importano dai moduli
di produzione; qui c'e' solo la CONTABILITA'.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Iterable, Optional, Sequence

import numpy as np
import pandas as pd

from . import stats

# Stati del ledger che rappresentano un segnale giocabile (una sola definizione
# nel progetto: `value_filter.PLAYABLE_TIERS`). Fallback identico se il modulo
# di produzione non e' importabile: l'audit non deve dipendere dal runtime.
try:  # pragma: no cover - dipende dall'ambiente
    from value_filter import PLAYABLE_TIERS as _PLAYABLE
except Exception:  # pragma: no cover
    _PLAYABLE = ("value", "strong_value", "moderate")

PLAYABLE_STATUSES = tuple(sorted(_PLAYABLE))
DECIDED = ("won", "lost")
PUSH = "push"

# Giorni oltre i quali un `push` non puo' essere un push di mercato.
# `expire_stale_sx_rows` chiude come push le righe insaldabili dopo 5 giorni dal
# kickoff (o 5 giorni dalla creazione per le righe orfane): una riga chiusa a
# 4-5 giorni dall'analisi, senza un risultato nel ledger, e' una SCADENZA.
EXPIRY_MIN_DAYS = 3.0
SAME_DAY_MAX_DAYS = 1.0

CLV_ECHO_TOL = 1e-9


# ---------------------------------------------------------------------------
# utilita' di parsing (nessuna assunzione sul fuso: mai indovinare)
# ---------------------------------------------------------------------------
def _dt(series: pd.Series) -> pd.Series:
    """Date ISO del ledger -> UTC. Formati misti ('T', spazio, 'Z', offset)."""
    return pd.to_datetime(series, errors="coerce", utc=True, format="mixed")


def _num(df: pd.DataFrame, cols: Iterable[str]) -> pd.DataFrame:
    out = df.copy()
    for c in cols:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce")
    return out


def playable_closed(preds: pd.DataFrame) -> pd.DataFrame:
    """Righe giocabili chiuse, una per (partita, esito), la PRIMA creata.

    Stessa selezione del report (dedup sull'ordine di `created_at`) perche' una
    rianalisi della stessa partita non e' una seconda scommessa.
    """
    if preds is None or len(preds) == 0:
        return pd.DataFrame()
    d = _num(preds, ("profit", "ev", "prob", "quota", "market_prob", "market_edge"))
    d = d[d["status"].isin(PLAYABLE_STATUSES) & d["profit"].notna()].copy()
    if d.empty:
        return d
    d["created"] = _dt(d["created_at"])
    d = d.sort_values(["match_id", "esito", "created", "id"], na_position="last")
    return d.drop_duplicates(["match_id", "esito"]).copy()


# ---------------------------------------------------------------------------
# 1) RICONCILIAZIONE DEL CAPITALE
# ---------------------------------------------------------------------------
def _cash_inflow(row: pd.Series) -> float:
    """Cassa che rientra quando una puntata si chiude.

    Una vincita restituisce stake + profitto; un push restituisce lo stake; una
    perdita non restituisce nulla (lo stake era gia' uscito al piazzamento).
    Contare solo il `profit` sottostima il rientro di uno stake intero per ogni
    vincita: e' l'errore che rende la riconciliazione impossibile.
    """
    stake = float(row.get("stake") or 0.0)
    price = row.get("price")
    outcome = str(row.get("esito_finale") or "").lower()
    if outcome == "won":
        return stake * (float(price) if price is not None and pd.notna(price) else 1.0)
    if outcome == PUSH:
        return stake
    return 0.0


def exposures_at(bets_df: pd.DataFrame, when: pd.Timestamp) -> float:
    """Stake immobilizzato (ordini aperti) all'istante `when`."""
    if bets_df is None or len(bets_df) == 0:
        return 0.0
    b = _num(bets_df, ("stake",))
    b["created"] = _dt(b["created_at"])
    b["settled"] = _dt(b["settled_at"])
    live = b[b["mode"].astype(str).str.lower() == "live"]
    open_now = live[(live["created"] <= when)
                    & (live["settled"].isna() | (live["settled"] > when))]
    return float(open_now["stake"].fillna(0.0).sum())


def capital_reconciliation(bets_df: pd.DataFrame, *, declared_value: float,
                           declared_at: str, anchor: Optional[dict] = None,
                           source: str = "", snapshot_at: Optional[str] = None,
                           tolerance: float = 0.05,
                           kind: str = "available") -> dict:
    """Il saldo dichiarato e' ricostruibile dai movimenti del ledger?

    `anchor` = stato patrimoniale esterno piu' vecchio e piu' autorevole, con
    `equity` (disponibile + in gioco) e `at`. Il conto:

        equity(declared_at)     = equity(anchor) + profitto realizzato nella finestra
        disponibile(declared_at) = equity(declared_at) - esposizione(declared_at)

    Perche' cosi': `equity` include l'escrow, che non e' cassa disponibile, e
    l'unica grandezza CONSERVATA e' l'equity (piazzare una puntata sposta cassa
    fra disponibile ed escrow; solo il profitto la muove). `kind` dice cosa e'
    il valore dichiarato: `available` (default, quello che restituisce
    `--balance`) o `equity`.
    Senza ancora il modulo NON inventa: dichiara `verified: False` e il motivo.
    """
    out: dict = {
        "declared_value": float(declared_value),
        "declared_at": declared_at,
        "source": source,
        "snapshot_at": snapshot_at,
        "verified": False,
        "notes": [],
    }
    t_decl = pd.to_datetime(declared_at, errors="coerce", utc=True)
    if pd.isna(t_decl):
        out["reason"] = "timestamp del saldo dichiarato non interpretabile"
        return out
    b = _num(bets_df, ("stake", "price", "profit")) if bets_df is not None else pd.DataFrame()
    if b is None or len(b) == 0:
        out["reason"] = "ledger delle puntate assente"
        return out
    b["created"] = _dt(b["created_at"])
    b["settled"] = _dt(b["settled_at"])
    live = b[b["mode"].astype(str).str.lower() == "live"].copy()

    # Movimenti DOPO la misura: se il saldo e' stato letto prima di un
    # settlement, il valore non e' quello attuale.
    after = live[live["settled"] > t_decl]
    out["settlements_after_declared"] = int(len(after))
    out["profit_after_declared"] = float(after["profit"].fillna(0.0).sum())
    out["exposure_at_declared"] = exposures_at(live, t_decl)
    out["last_settlement"] = (live["settled"].max().isoformat()
                              if live["settled"].notna().any() else None)
    out["last_bet_created"] = (live["created"].max().isoformat()
                               if live["created"].notna().any() else None)
    out["open_bets"] = int(live["settled"].isna().sum())
    out["open_stake"] = float(live[live["settled"].isna()]["stake"].fillna(0.0).sum())

    if len(after) > 0:
        out["stale"] = True
        out["notes"].append(
            f"{len(after)} puntate saldate DOPO la misura: il valore dichiarato "
            "e' un saldo storico, non quello attuale")
    else:
        out["stale"] = False

    if not anchor or anchor.get("equity") is None or not anchor.get("at"):
        out["reason"] = ("nessuna ancora esterna: il saldo non e' verificabile "
                         "dal solo ledger (i depositi non sono registrati)")
        return out

    t_anchor = pd.to_datetime(anchor["at"], errors="coerce", utc=True)
    if pd.isna(t_anchor) or t_anchor > t_decl:
        out["reason"] = "ancora piu' recente della misura (ordine temporale non valido)"
        return out

    exp_anchor = exposures_at(live, t_anchor)
    window = live[(live["settled"] > t_anchor) & (live["settled"] <= t_decl)]
    profit_window = float(window["profit"].fillna(0.0).sum()) if len(window) else 0.0
    inflow = float(window.apply(_cash_inflow, axis=1).sum()) if len(window) else 0.0
    # L'EQUITY e' la grandezza conservata: cambia SOLO del profitto realizzato
    # (una puntata piazzata sposta cassa da disponibile a escrow, non cambia
    # l'equity; una persa la riduce del profitto negativo). Il disponibile e'
    # l'equity meno l'escrow dell'istante.
    #
    # La versione precedente ricostruiva il disponibile sommando i rientri e
    # l'esposizione finale, ma NON sottraeva lo stake delle puntate CREATE E
    # SALDATE dentro la finestra: una perdita da 4 USDC piazzata e chiusa fra
    # ancora e misura spariva dal conto (trovato dal test sintetico, non da un
    # caso reale che per fortuna non si e' presentato).
    expected_equity = float(anchor["equity"]) + profit_window
    expected_available = expected_equity - out["exposure_at_declared"]
    kind = str(kind or "available").lower()
    expected = expected_equity if kind == "equity" else expected_available
    residual = float(declared_value) - expected
    out.update({
        "verified": True,
        "declared_kind": kind,
        "anchor": {"equity": float(anchor["equity"]), "at": anchor["at"],
                   "exposure": exp_anchor},
        "window_settlements": int(len(window)),
        "window_inflow": inflow,
        "window_profit": profit_window,
        "expected_equity": expected_equity,
        "expected_available": expected_available,
        "expected_value": expected,
        "residual": residual,
        "residual_pct": (residual / expected) if expected else None,
        "tolerance": tolerance,
        "within_tolerance": abs(residual) <= tolerance,
    })
    if not out["within_tolerance"]:
        out["notes"].append(
            f"residuo {residual:+.4f} oltre la tolleranza {tolerance}: "
            "cassa non spiegata dai movimenti del ledger (deposito/prelievo, "
            "escrow non registrato o fill parziale)")
    if out["exposure_at_declared"] > 0:
        out["notes"].append(
            "esposizione aperta alla misura: il disponibile non e' l'equity")
    return out


# ---------------------------------------------------------------------------
# 2) PUSH E SCADENZE: cosa e' stato DECISO e cosa no
# ---------------------------------------------------------------------------
_LINE_RE = re.compile(r"(-?\d+(?:\.\d+)?)")


def _line_of(esito) -> Optional[float]:
    m = _LINE_RE.search(str(esito))
    return float(m.group(1)) if m else None


def market_verdict(market: str, esito: str, score_home, score_away) -> Optional[str]:
    """Esito del mercato a linea dato il punteggio: 'won' | 'lost' | 'push'.

    Serve a verificare i `push` con un risultato noto: un push di mercato e'
    legittimo solo se la linea coincide ESATTAMENTE con lo scarto (AH) o col
    totale (OU). Il semi-handicap `linea + scarto` e' la forma che sbaglia piu'
    spesso: `Away -1` con 0-1 e' un push, non una vincita.
    """
    if score_home is None or score_away is None:
        return None
    try:
        sh, sa = float(score_home), float(score_away)
    except (TypeError, ValueError):
        return None
    if pd.isna(sh) or pd.isna(sa):
        return None
    line = _line_of(esito)
    if line is None:
        return None
    market = str(market).upper()
    if market == "AH":
        side = (sh - sa) if str(esito).lower().startswith("home") else (sa - sh)
        adv = side + line
        return "push" if abs(adv) < 1e-9 else ("won" if adv > 0 else "lost")
    if market == "OU":
        diff = (sh + sa) - line
        adv = diff if str(esito).lower().startswith("over") else -diff
        return "push" if abs(adv) < 1e-9 else ("won" if adv > 0 else "lost")
    return None


def expiry_audit(preds: pd.DataFrame, results_df: Optional[pd.DataFrame] = None) -> dict:
    """Separa i PUSH DI MERCATO dalle righe CHIUSE PER SCADENZA.

    Le seconde non sono esiti: sono righe che il referto non ha potuto saldare
    (`expire_stale_sx_rows` le chiude come push a costo zero) e che restano nel
    campione della strategia con profitto 0. Contarle come "puntate perse"
    sottostima il ROI; contare il loro EV come raggiunto lo sovrastima: sono
    l'errore di contabilita' piu' insidioso del ledger.
    """
    out: dict = {"total_closed": 0, "push_total": 0, "by_market": {},
                 "genuine": 0, "expiry": 0, "unverifiable_push": 0,
                 "playable": {}}
    if preds is None or len(preds) == 0:
        return out
    d = _num(preds, ("profit", "ev", "quota", "prob"))
    d = d[d["profit"].notna()].copy()
    d["created"] = _dt(d["created_at"])
    d["settled"] = _dt(d["settled_at"])
    d["lag_d"] = (d["settled"] - d["created"]).dt.total_seconds() / 86400.0
    out["total_closed"] = int(len(d))
    pushes = d[d["esito_finale"] == PUSH].copy()
    out["push_total"] = int(len(pushes))
    if pushes.empty:
        out["playable"] = {"push": 0, "expiry": 0}
        return out

    res = {}
    if results_df is not None and len(results_df):
        r = results_df.drop_duplicates("match_id")
        for _, row in r.iterrows():
            res[row["match_id"]] = (row.get("score_home"), row.get("score_away"))

    rows = []
    for _, r in pushes.iterrows():
        lag = r["lag_d"]
        scores = res.get(r["match_id"])
        verdict = market_verdict(r["mercato"], r["esito"], *(scores or (None, None))) \
            if scores else None
        if verdict == PUSH:
            kind = "genuine"
        elif verdict is None and pd.notna(lag) and lag >= EXPIRY_MIN_DAYS:
            kind = "expiry"
        elif verdict is None:
            kind = "unverifiable_push"
        else:
            # Un risultato noto dice che NON era un push: se la riga e' chiusa
            # come push il verdetto del ledger e' sbagliato.
            kind = "wrong_verdict"
        rows.append({"mercato": r["mercato"], "kind": kind,
                     "lag_d": lag, "playable": r["status"] in PLAYABLE_STATUSES,
                     "ev": r["ev"]})
    pr = pd.DataFrame(rows)
    for kind, grp in pr.groupby("kind"):
        out[kind] = int(len(grp))
    out["by_market"] = {
        mk: grp["kind"].value_counts().to_dict()
        for mk, grp in pr.groupby("mercato")
    }
    play = pr[pr["playable"]]
    out["playable"] = {
        "push": int(len(play)),
        "genuine": int((play["kind"] == "genuine").sum()),
        "expiry": int((play["kind"] == "expiry").sum()),
        "expected_ev_sum": float(play["ev"].fillna(0.0).sum()),
        "expected_ev_mean": float(play["ev"].mean()) if len(play) else None,
    }
    out["lag_days"] = {
        "median": float(pr["lag_d"].median()) if pr["lag_d"].notna().any() else None,
        "p90": float(pr["lag_d"].quantile(0.9)) if pr["lag_d"].notna().any() else None,
    }
    return out


def push_classification(preds: pd.DataFrame, results_df: Optional[pd.DataFrame] = None) -> pd.DataFrame:
    """Una riga per OGNI `push` chiuso, con la sua classificazione.

    Serve a separare la popolazione: un `push` di scadenza non e' una puntata
    (nessun esito, profitto 0 per costruzione) e resta nel ledger solo perche'
    il referto non ha trovato il risultato. Tenerlo nel campione della
    strategia diluisce il ROI e gonfia l'EV "atteso".
    """
    out_cols = ["id", "match_id", "esito", "mercato", "status", "ev", "quota",
                "kind", "lag_d", "playable"]
    if preds is None or len(preds) == 0:
        return pd.DataFrame(columns=out_cols)
    d = _num(preds, ("profit", "ev", "quota", "prob"))
    d = d[d["profit"].notna() & (d["esito_finale"] == PUSH)].copy()
    if d.empty:
        return pd.DataFrame(columns=out_cols)
    d["created"] = _dt(d["created_at"])
    d["settled"] = _dt(d["settled_at"])
    d["lag_d"] = (d["settled"] - d["created"]).dt.total_seconds() / 86400.0
    res = {}
    if results_df is not None and len(results_df):
        r = results_df.drop_duplicates("match_id")
        for _, row in r.iterrows():
            res[row["match_id"]] = (row.get("score_home"), row.get("score_away"))
    kinds = []
    for _, r in d.iterrows():
        scores = res.get(r["match_id"])
        verdict = (market_verdict(r["mercato"], r["esito"], *(scores or (None, None)))
                   if scores else None)
        if verdict == PUSH:
            kinds.append("genuine")
        elif verdict is None and pd.notna(r["lag_d"]) and r["lag_d"] >= EXPIRY_MIN_DAYS:
            kinds.append("expiry")
        elif verdict is None:
            kinds.append("unverifiable_push")
        else:
            kinds.append("wrong_verdict")
    d["kind"] = kinds
    d["playable"] = d["status"].isin(PLAYABLE_STATUSES)
    return d[[c for c in out_cols if c in d.columns]].copy()


def effective_population(preds: pd.DataFrame,
                         results_df: Optional[pd.DataFrame] = None,
                         *, n_boot: int = 4000, seed: int = 7) -> dict:
    """Quanto pesano le SCADENZE sul campione della strategia.

    Confronta tre popolazioni, tutte giocabili:

    * `all_closed`  — quello che il report chiama "le N chiuse" (scadenze incluse)
    * `traded`      — decise + push di MERCATO (le scadenze sono escluse: non
      sono puntate, ma il loro stake e' comunque uscito ed e' tornato, quindi
      un push genuino resta nel denominatore con profitto 0)
    * `decided`     — solo won/lost (il ROI piu' rumoroso ma il piu' onesto su
      "quanto rende una puntata che ha un esito")

    Il `expiry_drag` e' la parte di divario che NON e' ne' modello ne' mercato:
    e' contabilita' (EV atteso di righe mai giocate).
    """
    base = playable_closed(preds)
    out: dict = {}
    if base.empty:
        return {"reason": "nessuna riga giocabile chiusa"}
    cls = push_classification(preds, results_df)
    cls = cls[cls["playable"]] if len(cls) else cls
    expiry_ids = set(cls.loc[cls["kind"] == "expiry", "id"].tolist())
    wrong_ids = set(cls.loc[cls["kind"] == "wrong_verdict", "id"].tolist())
    base["is_expiry"] = base["id"].isin(expiry_ids)
    base["is_wrong"] = base["id"].isin(wrong_ids)

    def block(g: pd.DataFrame, label: str) -> dict:
        prof = g["profit"].astype(float).tolist()
        ci = stats.bootstrap_mean_ci(prof, g["match_id"].tolist(),
                                     n_boot=n_boot, seed=seed)
        return {
            "label": label, "n": int(len(g)),
            "roi": float(g["profit"].mean()),
            "roi_ci95": [ci["lo"], ci["hi"]],
            "ev_ledger": float(g["ev"].mean()),
            "hit_rate": float((g["esito_finale"] == "won").mean()),
            "wins": int((g["esito_finale"] == "won").sum()),
            "losses": int((g["esito_finale"] == "lost").sum()),
            "pushes": int((g["esito_finale"] == PUSH).sum()),
        }

    all_c = base
    traded = base[~base["is_expiry"] & ~base["is_wrong"]]
    decided = base[base["esito_finale"].isin(DECIDED)]
    out["all_closed"] = block(all_c, "tutte le chiuse (incluse scadenze)")
    out["traded"] = block(traded, "puntate effettive (scadenze escluse)")
    out["decided"] = block(decided, "solo decise (won/lost)")
    out["expiry_n"] = int(base["is_expiry"].sum())
    out["expiry_share"] = float(base["is_expiry"].mean())
    out["expiry_ev_mean"] = (float(base.loc[base["is_expiry"], "ev"].mean())
                             if base["is_expiry"].any() else None)
    # Quanto del divario atteso-realizzato e' attribuibile alle scadenze: la
    # loro EV e' nel numeratore dell'"atteso" ma il loro P/L e' 0.
    if out["expiry_n"] and out["all_closed"]["n"]:
        out["expiry_drag"] = (float(base.loc[base["is_expiry"], "ev"].sum())
                              / out["all_closed"]["n"])
    out["wrong_verdict_n"] = int(base["is_wrong"].sum())
    out["duplicates_dropped"] = int(max(0, len(preds[preds["profit"].notna()
                                                 & preds["status"].isin(PLAYABLE_STATUSES)])
                                        - len(base)))
    return out


# ---------------------------------------------------------------------------
# 3) L'IDENTITA' DELL'EV E L'ATTRIBUZIONE DEL DIVARIO
# ---------------------------------------------------------------------------
def ev_identity(preds: pd.DataFrame) -> dict:
    """L'EV scritto sul ledger e' l'EV della probabilita' scritta sul ledger?

    `ev == prob * quota - 1` e' l'identita' che rende l'"atteso" riproducibile.
    Se non vale, il numero del report non descrive la decisione: e' la prima
    cosa da escludere prima di attribuire il divario alla calibrazione.
    Test su TUTTE le righe chiuse giocabili (non solo le decise), perche' il
    difetto di definizione riguarda la registrazione, non l'esito.
    """
    out: dict = {"n": 0, "by_market": {}}
    d = playable_closed(preds)
    if d.empty or not {"prob", "quota", "ev"} <= set(d.columns):
        return out
    d = d.dropna(subset=["prob", "quota", "ev"])
    if d.empty:
        return out
    d["ev_calc"] = d["prob"] * d["quota"] - 1.0
    d["delta"] = d["ev"] - d["ev_calc"]
    out["n"] = int(len(d))
    out["mean_abs_delta"] = float(d["delta"].abs().mean())
    out["median_delta"] = float(d["delta"].median())
    out["share_ev_above_calc"] = float((d["delta"] > 1e-6).mean())
    out["consistent_share"] = float((d["delta"].abs() <= 1e-6).mean())
    out["implied_prob"] = float(((d["ev"] + 1.0) / d["quota"]).mean())
    out["stored_prob"] = float(d["prob"].mean())
    out["market_prob"] = float(d["market_prob"].mean()) if d["market_prob"].notna().any() else None
    for mk, g in d.groupby("mercato"):
        out["by_market"][mk] = {
            "n": int(len(g)),
            "mean_ev": float(g["ev"].mean()),
            "mean_ev_calc": float(g["ev_calc"].mean()),
            "mean_abs_delta": float((g["ev"] - g["ev_calc"]).abs().mean()),
            "consistent_share": float(((g["ev"] - g["ev_calc"]).abs() <= 1e-6).mean()),
            "implied_prob": float(((g["ev"] + 1) / g["quota"]).mean()),
            "stored_prob": float(g["prob"].mean()),
            "market_prob": float(g["market_prob"].mean()) if g["market_prob"].notna().any() else None,
        }
    return out


def identity_example(preds: pd.DataFrame) -> Optional[dict]:
    """Una riga REALE in cui l'EV a ledger non e' derivabile da `prob`.

    Serve al report per mostrare il difetto di definizione senza inventare un
    esempio: si prende la riga con lo scarto maggiore, e si dichiara che e'
    un caso, non la media (la media sta in `ev_identity`).
    """
    d = playable_closed(preds)
    need = {"prob", "quota", "ev", "esito", "mercato"}
    if d.empty or not need <= set(d.columns):
        return None
    d = d.dropna(subset=["prob", "quota", "ev"]).copy()
    if d.empty:
        return None
    d["delta"] = (d["ev"] - (d["prob"] * d["quota"] - 1.0)).abs()
    row = d.sort_values("delta", ascending=False).iloc[0]
    return {
        "esito": str(row.get("esito")),
        "mercato": str(row.get("mercato")),
        "quota": float(row["quota"]),
        "prob": float(row["prob"]),
        "ev": float(row["ev"]),
        "ev_calc": float(row["prob"] * row["quota"] - 1.0),
        "implied_prob": float((row["ev"] + 1.0) / row["quota"]),
        "delta": float(row["delta"]),
    }


def gap_attribution(preds: pd.DataFrame, *, n_boot: int = 4000, seed: int = 7) -> dict:
    """Scompone (atteso - realizzato) in tre addendi che sommano al totale.

        ev_ledger      = mean(ev)               (quello che il report chiama atteso)
        ev_repro       = mean(prob*quota - 1)   (la probabilita' che il gate usa)
        ev_market      = mean(market_prob*quota - 1)  (il mercato devigato)
        realized       = mean(profit)

        (1) definizione : ev_ledger - ev_repro
        (2) edge        : ev_repro  - ev_market
        (3) residuo     : ev_market - realized

    La somma dei tre addendi e' ESATTAMENTE ev_ledger - realized (nessun
    "altro" che nasconde la parte scomoda). Solo le righe DECISE entrano nel
    conto: un push non ha ne' vinto ne' perso e la sua esclusione va dichiarata
    (la si vede come differenza fra ROI sul deciso e ROI su tutte le chiuse).
    """
    out: dict = {"n": 0, "by_market": {}, "decomposition": {}}
    d = playable_closed(preds)
    # Una tabella vuota (o costruita senza le colonne del ledger) non e' un
    # campione: senza guardia qui `d["esito_finale"]` solleva KeyError e la
    # pipeline muore su un DB appena creato. Il difetto si vede solo con un
    # frame vuoto, cioe' nel caso in cui la diagnosi deve dire "zero righe".
    need = {"esito_finale", "prob", "quota", "ev", "profit", "match_id"}
    if d.empty or not need <= set(d.columns):
        return out
    d = d[d["esito_finale"].isin(DECIDED)].dropna(subset=["prob", "quota", "ev"])
    if d.empty:
        return out
    d = d.copy()
    d["ev_calc"] = d["prob"] * d["quota"] - 1.0
    has_mkt = d["market_prob"].notna()
    d["ev_market"] = np.where(has_mkt, d["market_prob"] * d["quota"] - 1.0, np.nan)
    d["won"] = (d["esito_finale"] == "won").astype(float)
    # Contributo di calibrazione sul singolo gettone: quota * (esito - prob).
    # La sua media e' esattamente realized - ev_repro.
    d["calib_gap"] = d["quota"] * (d["won"] - d["prob"])

    def block(g: pd.DataFrame) -> dict:
        n = int(len(g))
        realized = float(g["profit"].mean())
        ev_ledger = float(g["ev"].mean())
        ev_repro = float(g["ev_calc"].mean())
        ev_market = float(g["ev_market"].mean()) if g["ev_market"].notna().any() else None
        real_ci = stats.bootstrap_mean_ci(g["profit"].tolist(),
                                          g["match_id"].tolist(),
                                          n_boot=n_boot, seed=seed)
        gap_ci = stats.bootstrap_mean_ci(g["calib_gap"].tolist(),
                                        g["match_id"].tolist(),
                                        n_boot=n_boot, seed=seed)
        res = {
            "n": n,
            "hit_rate": float(g["won"].mean()),
            "mean_prob": float(g["prob"].mean()),
            "mean_quota": float(g["quota"].mean()),
            "ev_ledger": ev_ledger,
            "ev_repro": ev_repro,
            "ev_market": ev_market,
            "realized": realized,
            "gap_total": ev_ledger - realized,
            "calibration_gap": float(g["calib_gap"].mean()),
            "calibration_gap_ci95": [gap_ci["lo"], gap_ci["hi"]],
            "realized_ci95": [real_ci["lo"], real_ci["hi"]],
            "sd": float(g["profit"].std(ddof=1)) if n > 1 else None,
            "min_detectable_edge": stats.min_detectable_effect(
                n, float(g["profit"].std(ddof=1))) if n > 1 else None,
            "hit_ci95": list(stats.wilson_ci(int(g["won"].sum()), n)),
        }
        if ev_market is not None:
            res["definition_gap"] = ev_ledger - ev_repro
            res["edge_claimed"] = ev_repro - ev_market
            res["residual"] = ev_market - realized
            res["closure"] = (res["definition_gap"] + res["edge_claimed"]
                              + res["residual"] - res["gap_total"])
        res["calibrated"] = bool(res["hit_ci95"][0] <= res["mean_prob"] <= res["hit_ci95"][1])
        res["market_calibrated"] = (
            bool(g["market_prob"].notna().any()
                 and stats.wilson_ci(int(g["won"].sum()), n)[0]
                 <= float(g["market_prob"].mean())
                 <= stats.wilson_ci(int(g["won"].sum()), n)[1]))
        return res

    out["n"] = int(len(d))
    out["decomposition"] = block(d)
    out["by_market"] = {mk: block(g) for mk, g in d.groupby("mercato") if len(g) >= 10}
    # Il ROI su TUTTE le chiuse (push inclusi) e' quello del report: la
    # differenza col deciso e' l'effetto dei push nel denominatore.
    allp = playable_closed(preds)
    out["roi_all_closed"] = float(allp["profit"].mean()) if len(allp) else None
    out["roi_decided"] = float(d["profit"].mean())
    out["push_dilution"] = (out["roi_decided"] - out["roi_all_closed"]
                            if out["roi_all_closed"] is not None else None)
    return out


# ---------------------------------------------------------------------------
# 4) CLV: metodo, fonti, coorti, stabilita'
# ---------------------------------------------------------------------------
def _clv_frame(clv_df: pd.DataFrame) -> pd.DataFrame:
    """Chiusura normalizzata + CLV con la formula del PROGETTO."""
    if clv_df is None or len(clv_df) == 0:
        return pd.DataFrame()
    c = _num(clv_df, ("signal_quota", "closing_quota", "pinnacle_quota",
                      "closing_odds", "betfair_odds"))
    c["closing"] = c["closing_odds"].fillna(c["closing_quota"]).fillna(c["pinnacle_quota"])
    c["upd"] = _dt(c["updated_at"])
    c = c.dropna(subset=["closing", "signal_quota"])
    if c.empty:
        return c
    c = c.sort_values("upd").drop_duplicates(["match_id", "esito"], keep="last")
    from market_calib import clv_raw  # formula UNICA del progetto
    c["clv"] = [clv_raw(float(a), float(b))
                for a, b in zip(c["signal_quota"], c["closing"])]
    c = c[c["clv"].notna()].copy()
    c["echo"] = np.isclose(c["closing"], c["signal_quota"], atol=CLV_ECHO_TOL)
    c["source"] = c.get("closing_source").fillna("none").astype(str)
    return c


def clv_audit(clv_df: pd.DataFrame, preds: Optional[pd.DataFrame] = None, *,
              bets_df: Optional[pd.DataFrame] = None,
              n_boot: int = 4000, seed: int = 7) -> dict:
    """Il CLV positivo e' abilita' o margine del book?

    Tre verifiche che il numero aggregato non fa:

    1. **Echi**: se la chiusura coincide col segnale, il CLV e' 0 per
       costruzione (nessun campione). Il loro peso va dichiarato, altrimenti la
       media e' diluita da zeri che non misurano nulla.
    2. **Fonte**: un CLV misurato contro un BOOK (con margine) e uno contro lo
       stesso EXCHANGE (senza margine) non sono la stessa grandezza. Il
       sottoinsieme stessa-venue e' il controllo naturale: se li' il CLV e'
       ~zero, il positivo cross-venue e' in buona parte margine.
    3. **Realizzato**: un CLV positivo con ROI negativo non e' un edge provato.
       Le coorti si misurano sulle righe CHIUSE giocabili corrispondenti.
    """
    out: dict = {"status": "NON VERIFICABILE"}
    c = _clv_frame(clv_df)
    if c.empty:
        out["reason"] = "nessuna chiusura utilizzabile"
        return out
    non_echo = c[~c["echo"]]
    ci_all = stats.bootstrap_mean_ci(c["clv"].tolist(), c["match_id"].tolist(),
                                    n_boot=n_boot, seed=seed)
    ci_ne = (stats.bootstrap_mean_ci(non_echo["clv"].tolist(),
                                     non_echo["match_id"].tolist(),
                                     n_boot=n_boot, seed=seed)
             if len(non_echo) else {"n": 0, "mean": None, "lo": None, "hi": None})
    out.update({
        "n": int(len(c)),
        "echo_n": int(c["echo"].sum()),
        "echo_share": float(c["echo"].mean()),
        "mean": float(c["clv"].mean()),
        "median": float(c["clv"].median()),
        "share_positive": float((c["clv"] > 0).mean()),
        "ci95": [ci_all["lo"], ci_all["hi"]],
        "non_echo": {
            "n": int(len(non_echo)),
            "mean": float(non_echo["clv"].mean()) if len(non_echo) else None,
            "median": float(non_echo["clv"].median()) if len(non_echo) else None,
            "share_positive": float((non_echo["clv"] > 0).mean()) if len(non_echo) else None,
            "ci95": [ci_ne["lo"], ci_ne["hi"]],
        },
    })
    # Fonti: quante righe hanno una provenienza DICHIARATA?
    out["by_source"] = {}
    for src, g in c.groupby("source"):
        sub_ci = stats.bootstrap_mean_ci(g["clv"].tolist(), g["match_id"].tolist(),
                                        n_boot=n_boot, seed=seed)
        out["by_source"][src] = {
            "n": int(len(g)), "mean": float(g["clv"].mean()),
            "median": float(g["clv"].median()),
            "share_positive": float((g["clv"] > 0).mean()),
            "ci95": [sub_ci["lo"], sub_ci["hi"]],
            "non_echo": int((~g["echo"]).sum()),
        }
    out["declared_source_share"] = float((c["source"] != "none").mean())

    # Stabilita' temporale: un effetto che compare e sparisce non e' un edge.
    if c["upd"].notna().any():
        c = c.assign(week=c["upd"].dt.strftime("%Y-W%V"))
        out["by_week"] = {
            w: {"n": int(len(g)),
                "mean": float(g["clv"].mean()),
                "non_echo_n": int((~g["echo"]).sum()),
                "non_echo_mean": float(g.loc[~g["echo"], "clv"].mean())
                if (~g["echo"]).any() else None}
            for w, g in c.groupby("week")
        }
        out["sign_flips"] = int(sum(
            1 for w in out["by_week"].values()
            if (w["non_echo_mean"] or 0) < 0))
        out["weeks"] = int(len(out["by_week"]))

    # Performance REALIZZATA delle coorti (solo righe chiuse giocabili).
    if preds is not None and len(preds):
        base = playable_closed(preds)
        base = base[base["esito_finale"].isin(DECIDED)]
        joined = non_echo.merge(base, on=["match_id", "esito"], how="inner")
        if len(joined):
            jci = stats.bootstrap_mean_ci(joined["profit"].tolist(),
                                          joined["match_id"].tolist(),
                                          n_boot=n_boot, seed=seed)
            out["cohort_realized"] = {
                "n": int(len(joined)),
                "roi": float(joined["profit"].mean()),
                "ci95": [jci["lo"], jci["hi"]],
                "hit_rate": float((joined["esito_finale"] == "won").mean()),
                "mean_clv": float(joined["clv"].mean()),
                "mean_ev_ledger": float(joined["ev"].mean()),
                "mean_ev_repro": float((joined["prob"] * joined["quota"] - 1).mean()),
            }
            per_source = {}
            for src, g in joined.groupby("source"):
                if len(g) < 5:
                    continue
                per_source[src] = {
                    "n": int(len(g)),
                    "roi": float(g["profit"].mean()),
                    "mean_clv": float(g["clv"].mean()),
                    "hit_rate": float((g["esito_finale"] == "won").mean()),
                }
            out["cohort_realized"]["by_source"] = per_source
        # Terzili di CLV sul campione agganciato: il CLV ordina i risultati?
        if len(joined) >= 12:
            try:
                joined = joined.assign(bucket=pd.qcut(joined["clv"], 3,
                                                      labels=["basso", "medio", "alto"],
                                                      duplicates="drop"))
                out["terciles"] = {
                    str(k): {"n": int(len(g)),
                             "mean_clv": float(g["clv"].mean()),
                             "roi": float(g["profit"].mean())}
                    for k, g in joined.groupby("bucket", observed=True)
                }
            except ValueError:  # pragma: no cover - campione degenere
                pass
    # Controllo stessa-venue: un CLV misurato sull'exchange d'ingresso non ha
    # il margine del book, quindi e' il metro della sola abilita'.
    same_venue = out["by_source"].get("sx")
    if same_venue:
        out["same_venue_control"] = {
            "n": same_venue["n"], "mean": same_venue["mean"],
            "note": "chiusura SX: stessa venue dell'ingresso, nessun margine di book",
        }
    out["status"] = _clv_status(out)
    return out


def overround_index(snapshots: pd.DataFrame, *, min_outcomes: int = 3,
                    book: str = "pinnacle") -> pd.Series:
    """Margine (`sum(1/quota)`) per partita, dall'ultima rilevazione completa.

    Un CLV misurato contro la quota di un book contiene il margine del book: per
    separare ABILITA' e MARGINE serve la stessa partita con TUTTI gli esiti,
    perche' il margine si vede solo dall'insieme. `sum(1/quota)` e' l'overround
    (1.0413 = 4.13% di margine) e il fair di un esito e' `quota x overround`
    (devig moltiplicativo: `p_fair_i = (1/o_i) / sum(1/o)`).

    Si usa l'ULTIMA rilevazione completa (la piu' vicina alla chiusura) e si
    richiedono `min_outcomes` esiti distinti nello STESSO secondo di
    rilevazione: sommare prezzi di istanti diversi inventerebbe un margine.
    """
    if snapshots is None or len(snapshots) == 0:
        return pd.Series(dtype=float)
    s = snapshots.copy()
    s["price"] = pd.to_numeric(s.get("price"), errors="coerce")
    s["book"] = s.get("bookmaker", "").astype(str).str.lower()
    s["ts"] = _dt(s["recorded_at"]).dt.floor("s")
    s = s.dropna(subset=["price", "ts"])
    s = s[s["book"] == str(book).lower()]
    if s.empty:
        return pd.Series(dtype=float)
    inv = 1.0 / s["price"].to_numpy(dtype=float)
    s = s.assign(inv=inv)
    g = s.groupby(["match_id", "ts"]).agg(n=("esito", "nunique"), ov=("inv", "sum"))
    g = g[g["n"] >= int(min_outcomes)].reset_index().sort_values("ts")
    if g.empty:
        return pd.Series(dtype=float)
    last = g.groupby("match_id").tail(1)
    return last.set_index("match_id")["ov"].astype(float)


def fair_clv(clv_df: pd.DataFrame, snapshots: pd.DataFrame, *,
             book: str = "pinnacle", n_boot: int = 4000, seed: int = 7) -> dict:
    """CLV contro il book GREZZO e contro lo STESSO book de-vigato.

    Perche' e' la verifica che conta: un CLV positivo contro una quota con
    margine puo' essere solo il margine. Se togliendo il margine il CLV
    sopravvive, c'e' abilita'; se sparisce, il numero misurava il book.

    Benchmark = la quota sharp della stessa partita (`pinnacle_quota`, la piu'
    autorevole; ripiego `closing_odds` quando e' Pinnacle a T-0, poi
    `closing_quota`). Il margine e' quello misurato per QUELLA partita, non una
    media di progetto: un margine medio applicato a un book che ne ha un altro
    sposta il risultato della stessa grandezza che si vuole misurare.
    """
    out: dict = {"book": book, "n": 0, "status": "NON VERIFICABILE"}
    c = _clv_frame(clv_df)
    if c.empty:
        return {"book": book, "n": 0, "status": "NON VERIFICABILE",
                "reason": "nessuna chiusura utilizzabile"}
    margins = overround_index(snapshots, book=book)
    if margins.empty:
        return {"book": book, "n": 0, "status": "NON VERIFICABILE",
                "reason": f"nessun insieme completo di quote per '{book}'"}
    c["bench"] = (c["pinnacle_quota"]
                   .where(c["pinnacle_quota"].notna(), c["closing_odds"])
                   .where(lambda x: x.notna(), c["closing_quota"]))
    c["overround"] = c["match_id"].map(margins)
    d = c[c["overround"].notna() & c["bench"].notna()
          & (c["bench"] > 1.0) & (c["overround"] > 1.0)].copy()
    if d.empty:
        return {"book": book, "n": 0, "status": "NON VERIFICABILE",
                "reason": "nessun CLV agganciabile a un mercato completo"}
    d["clv_book"] = d["signal_quota"] / d["bench"] - 1.0
    d["clv_fair"] = d["signal_quota"] / (d["bench"] * d["overround"]) - 1.0
    d["echo"] = np.isclose(d["bench"], d["signal_quota"], atol=1e-9)
    ci_book = stats.bootstrap_mean_ci(d["clv_book"].tolist(), d["match_id"].tolist(),
                                      n_boot=n_boot, seed=seed)
    ci_fair = stats.bootstrap_mean_ci(d["clv_fair"].tolist(), d["match_id"].tolist(),
                                      n_boot=n_boot, seed=seed)
    out.update({
        "n": int(len(d)), "fixtures": int(d["match_id"].nunique()),
        "margin_mean": float(d["overround"].mean() - 1.0),
        "clv_book": float(d["clv_book"].mean()),
        "clv_book_ci95": [ci_book["lo"], ci_book["hi"]],
        "clv_fair": float(d["clv_fair"].mean()),
        "clv_fair_ci95": [ci_fair["lo"], ci_fair["hi"]],
        "share_positive_book": float((d["clv_book"] > 0).mean()),
        "share_positive_fair": float((d["clv_fair"] > 0).mean()),
        "margin_share_of_clv": (float((d["clv_book"].mean() - d["clv_fair"].mean())
                                      / d["clv_book"].mean())
                                if d["clv_book"].mean() > 0 else None),
    })
    ne = d[~d["echo"]]
    if len(ne):
        out["non_echo"] = {
            "n": int(len(ne)),
            "clv_book": float(ne["clv_book"].mean()),
            "clv_fair": float(ne["clv_fair"].mean()),
            "ci_fair95": list(stats.bootstrap_mean_ci(
                ne["clv_fair"].tolist(), ne["match_id"].tolist(),
                n_boot=n_boot, seed=seed).values())[:2],
        }
    lo, hi = out["clv_fair_ci95"]
    if len(d) < 30:
        out["status"] = "CAMPIONE INSUFFICIENTE"
    elif lo > 0:
        out["status"] = "SOPRAVVIVE AL DE-VIG"
    elif hi < 0:
        out["status"] = "SPARISCE COL DE-VIG (era il margine)"
    else:
        out["status"] = "NON DISTINGUIBILE DA ZERO DOPO IL DE-VIG"
    return out


def _clv_status(out: dict) -> str:
    ne = out.get("non_echo") or {}
    n = int(ne.get("n") or 0)
    lo, hi = (ne.get("ci95") or [None, None])
    if n < 30 or lo is None:
        return "CAMPIONE INSUFFICIENTE"
    if lo > 0:
        return "CLV POSITIVO (da confermare sul realizzato)"
    if hi < 0:
        return "CLV NEGATIVO"
    return "CLV NON DISTINGUIBILE DA ZERO"


# ---------------------------------------------------------------------------
# 5) sintesi per il report
# ---------------------------------------------------------------------------
def summarize(preds: pd.DataFrame, clv_df: pd.DataFrame, bets_df: pd.DataFrame,
              results_df: Optional[pd.DataFrame] = None, *, anchor: Optional[dict] = None,
              declared_value: float = 0.0, declared_at: str = "",
              source: str = "",
              snapshots: Optional[pd.DataFrame] = None) -> dict:
    """Tutti i blocchi dell'audit in un dizionario serializzabile.

    `snapshots` (price_snapshots) e' opzionale ma necessario al controllo che
    conta: senza i prezzi sharp completi il `fair_clv` risponde
    "NON VERIFICABILE" invece di indovinare un margine.
    """
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "capital": capital_reconciliation(
            bets_df, declared_value=declared_value, declared_at=declared_at,
            anchor=anchor, source=source),
        "push": expiry_audit(preds, results_df),
        "ev_identity": ev_identity(preds),
        "gap": gap_attribution(preds),
        "population": effective_population(preds, results_df),
        "clv": clv_audit(clv_df, preds, bets_df=bets_df),
        "fair_clv": fair_clv(clv_df, snapshots) if snapshots is not None else
                    {"status": "NON VERIFICABILE", "reason": "snapshot assenti"},
    }


def format_report(blocks: dict) -> str:
    """Versione testuale breve (log/Telegram), non il report completo."""
    cap = blocks.get("capital") or {}
    gap = (blocks.get("gap") or {}).get("decomposition") or {}
    clv = blocks.get("clv") or {}
    lines = [
        "=== AUDIT EV ===",
        f"capitale: dichiarato {cap.get('declared_value')} @ {cap.get('declared_at')}"
        f" | atteso {round(cap['expected_value'], 4) if cap.get('expected_value') is not None else 'n/d'}"
        f" | residuo {round(cap['residual'], 4) if cap.get('residual') is not None else 'n/d'}",
        f"push: {(blocks.get('push') or {}).get('push_total')} "
        f"(scadenze {(blocks.get('push') or {}).get('expiry')}, "
        f"di mercato {(blocks.get('push') or {}).get('genuine')})",
    ]
    if gap:
        lines.append(
            f"divario: atteso {gap['ev_ledger']:+.2%} vs realizzato {gap['realized']:+.2%}"
            f" = definizione {gap.get('definition_gap', 0):+.2%}"
            f" + edge {gap.get('edge_claimed', 0):+.2%}"
            f" + residuo {gap.get('residual', 0):+.2%}")
    if clv:
        ne = clv.get("non_echo") or {}
        lines.append(f"clv: {clv.get('status')} (n={ne.get('n')}, "
                     f"media {ne.get('mean')})")
    fc = blocks.get("fair_clv") or {}
    if fc.get("n"):
        lines.append(
            f"clv fair: book {fc['clv_book']:+.2%} -> de-vig {fc['clv_fair']:+.2%}"
            f" (margine {fc['margin_mean']:.2%}, n={fc['n']}, {fc['status']})")
    pop = blocks.get("population") or {}
    if pop.get("traded"):
        t = pop["traded"]
        lines.append(f"popolazione: puntate effettive n={t['n']} "
                     f"ROI {t['roi']:+.2%} (scadenze {pop.get('expiry_n')})")
    return "\n".join(lines)
