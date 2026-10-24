"""Candidati strategici come REGOLE PURE, ex ante, sulla riga di ledger.

Ogni regola usa solo cio' che esisteva al momento della decisione:
quota, probabilita' (modello e mercato devigato), EV, edge, mercato, lega,
`created_at`. Nessuna regola guarda l'esito. La closing line NON e' una
regola: e' una metrica di processo calcolata a parte (`attach_clv`).

Le soglie di default sono quelle di produzione del 20/10/2026, importate dove
possibile (`value_filter`) per non creare una seconda verita'.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

import pandas as pd

# Soglie di PRODUZIONE lette dalle fonti, non ricopiate (regola di progetto:
# una sola verita' per ogni soglia). Se un modulo non e' importabile si ricade
# sui valori dichiarati qui, e il test `TestSoglieDiProduzione` confronta i due.
try:
    import value_filter as _vf
    PROD_ODDS_MIN, PROD_ODDS_MAX = float(_vf.ODDS_MIN), float(_vf.ODDS_MAX)
    PROD_EV_MIN = float(_vf.EV_MIN)
except Exception:  # pragma: no cover - difensivo
    PROD_ODDS_MIN, PROD_ODDS_MAX, PROD_EV_MIN = 1.20, 2.00, 0.015
try:
    import market_calib as _mc
    PROD_EDGE_MIN = float(_mc.MARKET_EDGE_MIN)
except Exception:  # pragma: no cover
    PROD_EDGE_MIN = 0.01
try:
    import tennis_lane as _tl
    PROD_TENNIS_ODDS_MIN, PROD_TENNIS_ODDS_MAX = float(_tl.ODDS_MIN), float(_tl.ODDS_MAX)
    PROD_TENNIS_EV_MIN = float(_tl.EV_MIN)
except Exception:  # pragma: no cover
    PROD_TENNIS_ODDS_MIN, PROD_TENNIS_ODDS_MAX, PROD_TENNIS_EV_MIN = 1.30, 2.30, 0.025


@dataclass(frozen=True)
class Strategy:
    name: str
    family: str
    description: str
    select: Callable[[pd.DataFrame], pd.Series]
    params: dict = field(default_factory=dict)
    ex_ante: bool = True       # False = solo misura/contrasto, non giocabile
    notes: str = ""
    # universo di righe su cui la strategia PUO' esprimersi (filtro di mercato).
    # Serve allo split cronologico: tagliare il dataset intero al 70% darebbe
    # una validation da 15 righe (il ledger e' dominato da altri mercati),
    # cioe' una misura del CALENDARIO e non della strategia.
    universe: Optional[Callable[[pd.DataFrame], pd.Series]] = None


def _num(df: pd.DataFrame, col: str) -> pd.Series:
    return pd.to_numeric(df.get(col), errors="coerce")


def _closed(df: pd.DataFrame) -> pd.DataFrame:
    out = df[df["esito_finale"].notna()].copy()
    out["profit"] = pd.to_numeric(out["profit"], errors="coerce")
    return out[out["profit"].notna()]


def value_1x2(df: pd.DataFrame, odds_min: float = PROD_ODDS_MIN,
              odds_max: float = PROD_ODDS_MAX, min_edge: float = PROD_EDGE_MIN,
              min_ev: float = PROD_EV_MIN) -> pd.Series:
    """Value su 1X2 con prob. di mercato favorevole (strategia del progetto).

    Il vincolo `market_prob >= 0.5` e' l'approssimazione dichiarata di
    `FAVOURITES_ONLY` disponibile sul ledger (la riga contiene solo l'esito
    selezionato): non e' ricostruibile il favorito del mercato intero.
    """
    q = _num(df, "quota")
    mp = _num(df, "market_prob")
    ev = _num(df, "ev")
    edge = _num(df, "market_edge")
    return ((df["mercato"] == "1X2") & (q >= odds_min) & (q <= odds_max)
            & (mp >= 0.5) & (edge >= min_edge) & (ev >= min_ev))


def value_capped_ev(df: pd.DataFrame, odds_min: float = PROD_ODDS_MIN,
                    odds_max: float = PROD_ODDS_MAX, min_edge: float = PROD_EDGE_MIN,
                    min_ev: float = PROD_EV_MIN, max_ev: float = 0.10) -> pd.Series:
    """Value 1X2 con TETTO sull'EV, non solo pavimento.

    Nasce dalla calibrazione dell'EV (20/10): le righe con EV atteso 10-20%
    realizzano -4,7% contro +15,7% promesso, mentre quelle a EV 2-4%
    realizzano +2,6% contro +3,0% promesso. Un EV troppo alto su un mercato
    liquido e' piu' spesso un errore di stima che un'opportunita': il tetto e'
    la regola che lo mette per iscritto. Il pavimento resta quello di
    produzione, quindi e' un RESTRINGIMENTO, non un allargamento.
    """
    q = _num(df, "quota")
    mp = _num(df, "market_prob")
    ev = _num(df, "ev")
    edge = _num(df, "market_edge")
    return ((df["mercato"] == "1X2") & (q >= odds_min) & (q <= odds_max)
            & (mp >= 0.5) & (edge >= min_edge)
            & (ev >= min_ev) & (ev <= max_ev))


def value_tight_1x2(df: pd.DataFrame) -> pd.Series:
    q = _num(df, "quota")
    edge = _num(df, "market_edge")
    mp = _num(df, "market_prob")
    return ((df["mercato"] == "1X2") & (q >= 1.60) & (q <= 1.80)
            & (mp >= 0.5) & (edge >= 0.02))


def market_only_favourite(df: pd.DataFrame) -> pd.Series:
    """Baseline: favorito di mercato in fascia 1.30-1.80, SENZA modello."""
    q = _num(df, "quota")
    mp = _num(df, "market_prob")
    return ((df["mercato"] == "1X2") & (q >= 1.30) & (q <= 1.80) & (mp >= 0.55))


def ou_gate(df: pd.DataFrame, odds_min: float = PROD_ODDS_MIN,
            odds_max: float = PROD_ODDS_MAX, min_edge: float = PROD_EDGE_MIN,
            min_ev: float = PROD_EV_MIN) -> pd.Series:
    q = _num(df, "quota")
    ev = _num(df, "ev")
    edge = _num(df, "market_edge")
    return ((df["mercato"] == "OU") & (q >= odds_min) & (q <= odds_max)
            & (edge >= min_edge) & (ev >= min_ev))


def ah_gate(df: pd.DataFrame, odds_min: float = PROD_ODDS_MIN,
            odds_max: float = PROD_ODDS_MAX, min_edge: float = PROD_EDGE_MIN,
            min_ev: float = PROD_EV_MIN) -> pd.Series:
    q = _num(df, "quota")
    ev = _num(df, "ev")
    edge = _num(df, "market_edge")
    return ((df["mercato"] == "AH") & (q >= odds_min) & (q <= odds_max)
            & (edge >= min_edge) & (ev >= min_ev))


def tennis_band(df: pd.DataFrame, odds_min: float = PROD_TENNIS_ODDS_MIN,
                odds_max: float = PROD_TENNIS_ODDS_MAX,
                min_ev: float = PROD_TENNIS_EV_MIN) -> pd.Series:
    q = _num(df, "quota")
    ev = _num(df, "ev")
    return ((df["mercato"] == "TENNIS") & (q >= odds_min) & (q <= odds_max)
            & (ev >= min_ev))


def playable_status(df: pd.DataFrame) -> pd.Series:
    return df["status"].isin(["value", "strong_value", "moderate"])


def contrarian_ou_rejected(df: pd.DataFrame) -> pd.Series:
    """CONTRASTO, non giocabile: gli scartati OU sono davvero profittevoli?

    Se lo fossero, il gate avrebbe il segno sbagliato. Va misurato, non
    raccontato: la regola qui sotto non e' una strategia proposta.
    """
    return (df["mercato"] == "OU") & (df["status"] == "rejected")


def _is(market: str):
    """Universo di una strategia: le righe di un mercato."""
    return lambda d: d["mercato"] == market


STRATEGIES = {
    "value_1x2": Strategy(
        "value_1x2", "value_betting",
        "1X2, favorito di mercato (p>=0.50), quota 1.20-2.00, edge>=1pp, EV>=1.5%",
        value_1x2, {"odds_min": PROD_ODDS_MIN, "odds_max": PROD_ODDS_MAX,
                    "min_edge": PROD_EDGE_MIN, "min_ev": PROD_EV_MIN},
        universe=_is("1X2")),
    "value_tight_1x2": Strategy(
        "value_tight_1x2", "value_betting",
        "1X2, quota 1.60-1.80, edge>=2pp, favorito di mercato",
        value_tight_1x2, universe=_is("1X2")),
    "value_capped_ev": Strategy(
        "value_capped_ev", "value_betting",
        "1X2 come produzione MA con tetto EV 10% (EV alti = stima gonfiata)",
        lambda d, _m=0.10: value_capped_ev(d, max_ev=_m),
        {"odds_min": PROD_ODDS_MIN, "odds_max": PROD_ODDS_MAX,
         "min_edge": PROD_EDGE_MIN, "min_ev": PROD_EV_MIN, "max_ev": 0.10},
        universe=_is("1X2")),
    "value_capped_ev_tight": Strategy(
        "value_capped_ev_tight", "value_betting",
        "1X2 come produzione MA con tetto EV 6% (banda in cui l'EV e' calibrato)",
        lambda d, _m=0.06: value_capped_ev(d, max_ev=_m),
        {"odds_min": PROD_ODDS_MIN, "odds_max": PROD_ODDS_MAX,
         "min_edge": PROD_EDGE_MIN, "min_ev": PROD_EV_MIN, "max_ev": 0.06},
        universe=_is("1X2")),
    "market_only_favourite": Strategy(
        "market_only_favourite", "benchmark",
        "Baseline senza modello: favorito di mercato 1.30-1.80 (p>=0.55)",
        market_only_favourite, notes="serve a misurare l'apporto del modello",
        universe=_is("1X2")),
    "ou_gate": Strategy(
        "ou_gate", "multi_market",
        "OU quota 1.30-2.00, edge>=1pp, EV>=1.5%",
        ou_gate, {"odds_min": PROD_ODDS_MIN, "odds_max": PROD_ODDS_MAX,
                  "min_edge": PROD_EDGE_MIN, "min_ev": PROD_EV_MIN},
        universe=_is("OU")),
    "ah_gate": Strategy(
        "ah_gate", "multi_market",
        "AH quota 1.30-2.00, edge>=1pp, EV>=1.5%",
        ah_gate, {"odds_min": PROD_ODDS_MIN, "odds_max": PROD_ODDS_MAX,
                  "min_edge": PROD_EDGE_MIN, "min_ev": PROD_EV_MIN},
        universe=_is("AH")),
    "tennis_band": Strategy(
        "tennis_band", "tennis",
        "Tennis quota 1.30-2.00, EV>=2.5% (oracolo sharp come prezzo)",
        tennis_band, {"odds_min": PROD_TENNIS_ODDS_MIN,
                      "odds_max": PROD_TENNIS_ODDS_MAX,
                      "min_ev": PROD_TENNIS_EV_MIN},
        universe=_is("TENNIS")),
    "contrarian_ou_rejected": Strategy(
        "contrarian_ou_rejected", "contrast",
        "CONTRASTO: scartati OU, misurati per verificare il segno del gate",
        contrarian_ou_rejected, ex_ante=False,
        notes="non giocabile: esiste solo per falsificare il gate",
        universe=_is("OU")),
}


# colonne che una regola NON puo' guardare (post-decisione)
OUTCOME_COLUMNS = ("esito_finale", "profit", "settled_at")


def leak_audit(df: pd.DataFrame, strategies: Optional[dict] = None) -> dict:
    """Prova anti-leakage: si CAMPIA il segno dell'esito e si verifica che la
    selezione resti identica. Deve uscire `clean=True`.

    Perche' non si permutano gli esiti: permutare CAMBIA la popolazione
    (le righe chiuse), e una regola che legge solo l'input sembrerebbe
    "sporca" per un motivo che col leakage non c'entra — errore che questa
    prova ha commesso alla prima stesura. Il flip won<->lost con `profit`
    negato tiene identiche le righe e cambia solo il bersaglio: se una regola
    guardasse l'esito, la selezione cambierebbe.
    """
    flipped = df.copy()
    esiti = flipped["esito_finale"]
    flipped["esito_finale"] = esiti.map(
        {"won": "lost", "lost": "won"}).fillna(esiti)
    flipped["profit"] = -pd.to_numeric(flipped["profit"], errors="coerce")
    out = {"clean": True, "method": "sign_flip(won<->lost, profit=-profit)",
           "checked": []}
    for name, st in (strategies or STRATEGIES).items():
        a = select(st, df)
        b = select(st, flipped)
        same = (len(a) == len(b)
                and sorted(a["id"].tolist()) == sorted(b["id"].tolist()))
        out["checked"].append({"strategy": name, "identical": bool(same),
                               "n": int(len(a))})
        if not same:
            out["clean"] = False
    return out


def select(strategy: Strategy, df: pd.DataFrame, *, dedup_match: bool = False,
           dedup_key: Optional[list] = None) -> pd.DataFrame:
    """Applica la regola e ritorna le righe CHIUSE (una per scommessa).

    `dedup_match` tiene la prima riga per (match_id, esito) in ordine di
    `created_at`: due righe sullo stesso esito non sono due scommesse (il
    ledger registra anche i ripassi della pipeline).
    """
    closed = _closed(df)
    mask = strategy.select(closed).fillna(False).astype(bool)
    picked = closed[mask].copy()
    picked = picked.sort_values("created_at")
    keys = dedup_key or ["match_id", "esito"]
    if dedup_match and len(picked):
        picked = picked.drop_duplicates(subset=keys, keep="first")
    return picked.reset_index(drop=True)


def attach_clv(picked: pd.DataFrame, clv_df: pd.DataFrame) -> pd.DataFrame:
    """Aggiunge `clv` (quota presa / closing - 1) usando la tabella CLV.

    Regola di purezza: la closing serve SOLO a misurare il processo. Un CLV
    calcolato con la stessa quota del segnale (eco) viene scartato, come fa
    `performance_report` dal 02/09.
    """
    from market_calib import clv_raw

    if picked.empty:
        picked = picked.copy()
        picked["clv"] = None
        return picked
    c = clv_df.copy()
    for col in ("closing_odds", "closing_quota", "pinnacle_quota"):
        c[col] = pd.to_numeric(c.get(col), errors="coerce")
    c["closing"] = c["closing_odds"].fillna(c["closing_quota"]).fillna(c["pinnacle_quota"])
    c = c.dropna(subset=["closing"]).sort_values("updated_at")
    c = c.drop_duplicates(subset=["match_id", "esito"], keep="last")
    merged = picked.merge(c[["match_id", "esito", "closing", "closing_source"]],
                          on=["match_id", "esito"], how="left")
    vals = []
    for _, row in merged.iterrows():
        v = None
        if pd.notna(row.get("closing")) and pd.notna(row.get("quota")):
            v = clv_raw(float(row["quota"]), float(row["closing"]))
            if v is not None and abs(v) < 1e-12:
                v = None  # eco del segnale, non un campione
        vals.append(v)
    merged["clv"] = vals
    return merged
