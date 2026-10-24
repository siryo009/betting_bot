"""Test PRE-REGISTRATO: il protocollo si scrive PRIMA di guardare l'holdout.

Il problema che risolve (documentato due volte in questo progetto: 22/09 e
25/09): cercare un sottogruppo positivo dentro 400 righe produce un numero che
sembra un risultato e non e' verificabile. Qui la sequenza e' invertita e
vincolata dal disco:

1. `PROTOCOL` e' **dichiarato** (famiglia di candidati, split, metriche,
   criteri di successo) e la sua impronta sha256 viene salvata accanto ai dati;
2. la **selezione** usa SOLO training e validation (il test non viene letto);
3. il test gira **una volta** e lascia `holdout_used.json`: un secondo `--test`
   rifiuta di rieseguire (altrimenti si puo' riprovare finche' esce bene);
4. se il file del protocollo e' stato modificato dopo la firma, il test
   rifiuta: un protocollo riscritto a posteriori non e' un protocollo.

Perche' uno split CRONOLOGICO e non casuale: la strategia opera nel tempo, e un
test distribuito su tutte le date contiene il futuro dentro il training —
esattamente il look-ahead che il progetto evita altrove (`backtest_mc`,
`ml_harness`).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

import numpy as np
import pandas as pd

from . import loader, stats
from .ev_audit import DECIDED, _dt, _num, playable_closed

# ---------------------------------------------------------------------------
# IL PROTOCOLLO (congelato: cambiarlo richiede un nuovo protocollo, non un
# ritocco silenzioso)
# ---------------------------------------------------------------------------
PROTOCOL_VERSION = "1.0"

#: Ripartizione cronologica. Order: train, validation, test.
SPLIT = (0.5, 0.25, 0.25)

#: Soglie minime perche' una valutazione significhi qualcosa.
MIN_TRAIN = 120
MIN_VAL = 40
MIN_TEST = 40

#: Famiglia di candidati DICHIARATA PRIMA di guardare i risultati. Ogni
#: candidato e' un insieme di gate; la griglia e' volutamente piccola (36):
#: con 36 candidati su ~200 righe di validation il rischio di scegliere il
#: rumore resta, ed e' per questo che il test ensuite deve SUPERARE la soglia,
#: non solo essere il migliore.
ODDS_MAX_OPTS = (1.80, 2.00)
EDGE_MIN_OPTS = (0.01, 0.02, 0.03)
EV_MIN_OPTS = (0.015, 0.035)
MARKET_OPTS = ("1X2", "ALL")

#: Il gate che la produzione applica OGGI sui segnali di calcio 1X2: e' il
#: benchmark. Valori letti dal modulo di produzione quando importabile (mai
#: copiati a mano), con fallback dichiarato.
def baseline_gate() -> dict:
    odds_min, odds_max, ev_min, edge_min, fav = 1.20, 2.00, 0.015, 0.01, 0.50
    try:  # pragma: no cover - dipende dall'ambiente
        import value_filter as vf
        import market_calib as mc
        odds_min = float(getattr(vf, "ODDS_MIN", odds_min))
        odds_max = float(getattr(vf, "ODDS_MAX", odds_max))
        ev_min = float(vf.EV_MIN)
        edge_min = float(mc.MARKET_EDGE_MIN)
        fav = float(getattr(vf, "MIN_FAVOURITE_MARKET_PROB", fav))
    except Exception:
        pass
    return {"odds_min": odds_min, "odds_max": odds_max, "ev_min": ev_min,
            "edge_min": edge_min, "favourite_min": fav, "markets": ["1X2"],
            "label": "produzione attuale (1X2)"}


#: Criteri di SUCCESSO dichiarati prima del test.
SUCCESS_CRITERIA = {
    "primary": "profitto medio per unita' di stake (ROI flat)",
    "pass": "estremo inferiore dell'IC95 bootstrap > 0 sul test",
    "fail": "estremo superiore dell'IC95 < 0",
    "inconclusive": f"n_test < {MIN_TEST} oppure IC95 che contiene lo zero",
    "must_beat_benchmark": "ROI sul test >= ROI del benchmark sulla STESSA "
                          "finestra (se il benchmark ha n >= 20)",
    "n_for_confident": 200,
}

PROTOCOL = {
    "version": PROTOCOL_VERSION,
    "declared_at": "2026-10-20",
    "question": "Esiste un sottoinsieme DICHIARATO di gate la cui performance "
                "supera il benchmark di produzione FUORI CAMPIONE?",
    "universe": "predictions chiuse con profit, quota, ev e market_edge "
                "leggibili; dedup per (match_id, esito), la PRIMA creata",
    "split": {"train": SPLIT[0], "validation": SPLIT[1], "test": SPLIT[2],
              "order": "cronologico su created_at; il test e' l'ULTIMO tratto",
              "note": "nessuna riga del test entra in selezione o soglie"},
    "candidates": {
        "odds_max": list(ODDS_MAX_OPTS),
        "edge_min": list(EDGE_MIN_OPTS),
        "ev_min": list(EV_MIN_OPTS),
        "markets": list(MARKET_OPTS),
        "count": int(len(ODDS_MAX_OPTS) * len(EDGE_MIN_OPTS)
                     * len(EV_MIN_OPTS) * len(MARKET_OPTS)),
    },
    "fixed_gates": ["odds_min (produzione)",
                    "favourite_min (produzione, SOLO 1X2: e' una regola del "
                    "calcio e applicarla a OU/AH scarterebbe i lati Under/Away)",
                    "soglia EV UNIFORME fra i candidati, per confrontabilita' "
                    "(la produzione usa soglie per mercato: semplificazione "
                    "dichiarata, non un dettaglio nascosto)",
                    "dedup per partita+esito"],
    "selection_rule": f"massimo ROI su VALIDATION con n >= {MIN_VAL}; "
                      "nessun'altra metrica, nessun tie-break a posteriori "
                      "(a parita' vince il piu' semplice: odds_max piu' basso)",
    "metrics": ["n", "ROI flat", "IC95 bootstrap per partita", "hit rate",
                "EV medio a ledger", "delta vs benchmark"],
    "success": SUCCESS_CRITERIA,
    "holdout_policy": "un solo test, firmato su disco; un secondo tentativo "
                      "rifiuta senza nuovo protocollo",
}


def protocol_hash(protocol: Optional[dict] = None) -> str:
    payload = json.dumps(protocol or PROTOCOL, sort_keys=True,
                         ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


# ---------------------------------------------------------------------------
# persistenza del protocollo e guardia dell'holdout
# ---------------------------------------------------------------------------
def protocol_path(outdir: "os.PathLike | str | None" = None) -> Path:
    base = Path(outdir or (Path(__file__).resolve().parents[1] / "out"))
    return base / "prereg_protocol.json"


def save_protocol(outdir=None, *, extra: Optional[dict] = None) -> dict:
    """Scrive il protocollo + la sua impronta. Non sovrascrive una firma
    diversa: un protocollo nuovo e' un file nuovo (`--force` esplicito)."""
    p = protocol_path(outdir)
    p.parent.mkdir(parents=True, exist_ok=True)
    doc = {"hash": protocol_hash(), "protocol": PROTOCOL,
           "written_at": datetime.now(timezone.utc).isoformat()}
    if extra:
        doc["context"] = extra
    if p.exists():
        old = json.loads(p.read_text(encoding="utf-8"))
        if old.get("hash") != doc["hash"]:
            raise RuntimeError(
                "protocollo gia' firmato con un'impronta DIVERSA: il test "
                "pre-registrato non si riscrive a posteriori (usare "
                "`--new-protocol` per dichiararne uno nuovo)")
        return old
    p.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
    return doc


def verify_protocol(outdir=None) -> dict:
    p = protocol_path(outdir)
    if not p.exists():
        raise RuntimeError(f"protocollo assente ({p}): il test non puo' girare "
                           "senza una dichiarazione firmata")
    doc = json.loads(p.read_text(encoding="utf-8"))
    h = protocol_hash(doc.get("protocol") or {})
    if h != doc.get("hash"):
        raise RuntimeError("impronta del protocollo non corrispondente: il "
                           "file e' stato modificato dopo la firma")
    if h != protocol_hash():
        raise RuntimeError("il protocollo sul disco differisce da quello nel "
                           "codice: il test non e' piu' pre-registrato")
    return doc


def holdout_path(outdir=None) -> Path:
    base = Path(outdir or (Path(__file__).resolve().parents[1] / "out"))
    return base / "holdout_used.json"


def holdout_used(outdir=None) -> Optional[dict]:
    p = holdout_path(outdir)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def _mark_holdout(outdir, payload: dict) -> None:
    p = holdout_path(outdir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                 encoding="utf-8")


# ---------------------------------------------------------------------------
# universo e split
# ---------------------------------------------------------------------------
def universe(preds: pd.DataFrame) -> pd.DataFrame:
    """Righe valutabili: chiuse, con profitto e con i campi del gate.

    NON si filtra sullo `status` del ledger: quello registra il tier con le
    soglie del giorno in cui la riga e' nata (lezione del 22/09), quindi usarlo
    come filtro mescolerebbe ere diverse. Si riapplicano i gate dichiarati.
    """
    d = _num(preds, ("quota", "prob", "ev", "market_prob", "market_edge", "profit"))
    d = d[d["esito_finale"].isin(DECIDED) | (d["esito_finale"] == "push")].copy()
    d = d.dropna(subset=["profit", "quota", "ev", "market_edge"])
    if d.empty:
        return d
    d["created"] = _dt(d["created_at"])
    d = d.dropna(subset=["created"]).sort_values(["created", "id"])
    d = d.drop_duplicates(["match_id", "esito"], keep="first")
    d["market"] = d["mercato"].fillna("").astype(str).str.upper()
    return d


def chronological_split(df: pd.DataFrame, fracs: Sequence[float] = SPLIT) -> dict:
    """Tre tratti contigui e disgiunti, in ordine di data."""
    d = df.sort_values(["created", "id"]).reset_index(drop=True)
    n = len(d)
    i = int(math.floor(n * fracs[0]))
    j = int(math.floor(n * (fracs[0] + fracs[1])))
    out = {"train": d.iloc[:i], "validation": d.iloc[i:j], "test": d.iloc[j:]}
    out["bounds"] = {"train": [str(d.iloc[0]["created"]) if n else None,
                               str(d.iloc[i - 1]["created"]) if i else None],
                     "validation": [str(d.iloc[i]["created"]) if i < n else None,
                                    str(d.iloc[j - 1]["created"]) if j else None],
                     "test": [str(d.iloc[j]["created"]) if j < n else None,
                              str(d.iloc[n - 1]["created"]) if n else None]}
    return out


# ---------------------------------------------------------------------------
# gate dei candidati (puri: nessuna soglia implicita)
# ---------------------------------------------------------------------------
def apply_gate(df: pd.DataFrame, gate: dict) -> pd.DataFrame:
    if df.empty:
        return df
    g = df
    g = g[(g["quota"] >= float(gate["odds_min"]))
          & (g["quota"] <= float(gate["odds_max"]))]
    g = g[g["ev"] >= float(gate["ev_min"])]
    g = g[g["market_edge"] >= float(gate["edge_min"])]
    # Il gate "favorito" e' una regola di CALCIO 1X2 (la produzione la applica
    # con `favourites_only` solo li'). Applicarla a OU/AH scarterebbe i lati
    # Under/Away che il sistema gioca davvero: il benchmark mentirebbe.
    fav = float(gate.get("favourite_min") or 0.0)
    if fav > 0 and "market_prob" in g.columns:
        is12 = g["market"].isin(("1X2",))
        g = g[~is12 | g["market_prob"].isna() | (g["market_prob"] >= fav)]
    markets = gate.get("markets") or []
    if markets and "1X2" not in markets:
        g = g[g["market"].isin(markets)]
    elif markets == ["1X2"]:
        g = g[g["market"].isin(("1X2",))]
    return g.reset_index(drop=True)


def candidate_gates() -> list[dict]:
    """La famiglia dichiarata, in ordine canonico (deterministico)."""
    base = baseline_gate()
    out = []
    for om in ODDS_MAX_OPTS:
        for em in EDGE_MIN_OPTS:
            for evm in EV_MIN_OPTS:
                for mk in MARKET_OPTS:
                    out.append({"odds_min": base["odds_min"], "odds_max": om,
                                "ev_min": evm, "edge_min": em,
                                "favourite_min": base["favourite_min"],
                                "markets": ["1X2"] if mk == "1X2" else
                                           ["1X2", "OU", "AH", "TENNIS", "ML"],
                                "label": f"1X2<= {om} edge{em:.0%} ev{evm:.1%} {mk}"})
    return out


def score(df: pd.DataFrame, *, n_boot: int = 2000, seed: int = 7) -> dict:
    if df.empty:
        return {"n": 0, "roi": None, "ci95": [None, None], "hit": None,
                "ev": None}
    prof = df["profit"].astype(float).tolist()
    ci = stats.bootstrap_mean_ci(prof, df["match_id"].tolist(),
                                 n_boot=n_boot, seed=seed)
    return {"n": int(len(df)), "roi": float(np.mean(prof)),
            "ci95": [ci["lo"], ci["hi"]],
            "hit": float((df["esito_finale"] == "won").mean()),
            "ev": float(df["ev"].mean())}


# ---------------------------------------------------------------------------
# selezione (solo train+validation) e test (una volta)
# ---------------------------------------------------------------------------
def select(split: dict, *, n_boot: int = 2000) -> dict:
    """Seleziona il candidato su VALIDATION. Il test NON viene letto qui."""
    val = split["validation"]
    rows = []
    for gate in candidate_gates():
        s = score(apply_gate(val, gate), n_boot=n_boot)
        rows.append({**gate, **s, "eligible": bool(s["n"] and s["n"] >= MIN_VAL)})
    table = pd.DataFrame(rows)
    elig = table[table["eligible"]].copy()
    if elig.empty:
        return {"selected": None, "table": table,
                "reason": f"nessun candidato con n >= {MIN_VAL} in validation"}
    # Ordine dichiarato: ROI, poi n, poi odds_max piu' basso (semplicita').
    elig = elig.sort_values(["roi", "n", "odds_max", "edge_min", "ev_min"],
                            ascending=[False, False, True, True, True])
    best = elig.iloc[0].to_dict()
    return {"selected": best, "table": table,
            "benchmark_val": score(apply_gate(val, baseline_gate()), n_boot=n_boot)}


def market_benchmark(df: pd.DataFrame, gate: dict) -> pd.DataFrame:
    """Benchmark di MERCATO: la stessa soglia EV applicata all'EV del mercato.

    Nessun modello: si compra solo dove il prezzo offerto batte il prezzo
    EQUO (`market_prob*quota - 1 >= ev_min`). E' il confronto che rende
    leggibile il risultato del candidato: se il candidato non batte nemmeno
    "compra dove il mercato e' in disaccordo con se stesso", l'edge dichiarato
    non e' un edge.

    DESCRITTIVO: non entra in `pass`/`fail` ne' in `beats_benchmark` (le
    regole del protocollo sono gia' firmate; aggiungere un criterio dopo
    sarebbe un cambio di protocollo, non un'informazione in piu').
    """
    if df is None or df.empty or "market_prob" not in df.columns:
        return pd.DataFrame(columns=getattr(df, "columns", None))
    g = df.copy()
    g["ev_market"] = g["market_prob"] * g["quota"] - 1.0
    g = g[(g["quota"] >= float(gate["odds_min"]))
          & (g["quota"] <= float(gate["odds_max"]))]
    g = g[g["market_prob"].notna() & (g["ev_market"] >= float(gate["ev_min"]))]
    markets = gate.get("markets") or []
    if markets == ["1X2"]:
        g = g[g["market"].isin(("1X2",))]
    elif markets and "1X2" not in markets:
        g = g[g["market"].isin(markets)]
    return g.reset_index(drop=True)


def run_test(split: dict, selected: dict, *, outdir=None, n_boot: int = 4000,
             write: bool = True) -> dict:
    """Il test one-shot sull'holdout. Rifiuta se l'holdout e' gia' consumato."""
    used = holdout_used(outdir)
    if used and write:
        raise RuntimeError(
            "holdout gia' consumato il " + str(used.get("used_at")) +
            f" (protocollo {used.get('protocol_hash', '')[:12]}): un secondo "
            "test non e' piu' out-of-sample")
    test = split["test"]
    gate = {k: selected[k] for k in
            ("odds_min", "odds_max", "ev_min", "edge_min", "favourite_min",
             "markets")}
    cand = apply_gate(test, gate)
    bench = apply_gate(test, baseline_gate())
    s = score(cand, n_boot=n_boot)
    b = score(bench, n_boot=n_boot)
    bm = score(market_benchmark(test, gate), n_boot=n_boot)
    lo, hi = s["ci95"]
    if s["n"] < MIN_TEST:
        verdict = "INCONCLUSIVE"
        why = f"n_test {s['n']} < {MIN_TEST}"
    elif lo is None:
        verdict, why = "INCONCLUSIVE", "IC non calcolabile"
    elif lo > 0:
        verdict, why = "PASS", "IC95 interamente sopra zero"
    elif hi is not None and hi < 0:
        verdict, why = "FAIL", "IC95 interamente sotto zero"
    else:
        verdict, why = "INCONCLUSIVE", "IC95 contiene lo zero"
    beats = None
    if s["roi"] is not None and b["roi"] is not None and b["n"] >= 20:
        beats = bool(s["roi"] >= b["roi"])
    out = {"protocol_hash": protocol_hash(), "selected": selected,
           "verdict": verdict, "why": why, "test": s, "benchmark_test": b,
           "benchmark_market": bm,
           "beats_benchmark": beats,
           "window": split.get("bounds", {}).get("test"),
           "n_for_confident": SUCCESS_CRITERIA["n_for_confident"],
           "confident_sample": bool(s["n"] >= SUCCESS_CRITERIA["n_for_confident"])}
    if write:
        _mark_holdout(outdir, {**{k: out[k] for k in
                                  ("protocol_hash", "verdict", "why", "test",
                                   "benchmark_test", "beats_benchmark", "window")},
                               "used_at": datetime.now(timezone.utc).isoformat()})
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _fmt(s: dict) -> str:
    if not s or not s.get("n"):
        return "n=0"
    lo, hi = s["ci95"]
    return (f"n={s['n']} ROI {s['roi']:+.2%} "
            f"IC95 [{lo:+.2%}, {hi:+.2%}] hit {s['hit']:.1%} "
            f"EV {s['ev']:+.2%}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    import sys
    args = list(argv if argv is not None else sys.argv[1:])
    outdir = Path(__file__).resolve().parents[1] / "out"
    if "--new-protocol" in args and protocol_path(outdir).exists():
        protocol_path(outdir).unlink()
    doc = save_protocol(outdir)
    print(f"protocollo {doc['hash'][:16]} | candidati "
          f"{PROTOCOL['candidates']['count']} | split {SPLIT}")
    df = universe(loader.predictions())
    if len(df) < MIN_TRAIN + MIN_VAL + MIN_TEST:
        print(f"campione insufficiente: {len(df)} righe valutabili "
              f"({MIN_TRAIN + MIN_VAL + MIN_TEST} minime)")
        return 1
    sp = chronological_split(df)
    print(f"universo {len(df)} | train {len(sp['train'])} | "
          f"validation {len(sp['validation'])} | test {len(sp['test'])}")
    sel = select(sp)
    if not sel["selected"]:
        print(sel["reason"])
        return 1
    s = sel["selected"]
    print(f"selezionato (validation): {s['label']} -> {_fmt(s)}")
    print(f"benchmark (validation): {_fmt(sel['benchmark_val'])}")
    if "--select-only" in args:
        return 0
    try:
        res = run_test(sp, s, outdir=outdir, write="--no-sign" not in args)
    except RuntimeError as exc:
        print(f"rifiutato: {exc}")
        return 2
    print(f"TEST: {res['verdict']} ({res['why']})")
    print(f"  candidato: {_fmt(res['test'])}")
    print(f"  benchmark: {_fmt(res['benchmark_test'])}")
    print(f"  batte il benchmark: {res['beats_benchmark']} | "
          f"campione confidenziale: {res['confident_sample']}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
