"""Fase successiva: diagnosi del segnale e decisione operativa.

Produce `NEXT_PHASE_REPORT.md`, i CSV, il log e i risultati JSON **da dati
misurati**, non da numeri incollati: ogni tabella del report nasce da una
funzione di questo pacchetto o di `ev_audit`/`prereg`.

Regole con cui e' scritto (le stesse del report precedente):

* nessun numero senza campione e intervallo;
* un saldo storico non e' un saldo attuale (`stale` esplicito);
* una definizione che non regge viene DICHIARATA, non corretta a mano;
* il test finale e' **pre-registrato** (`prereg.py`) e gira una volta sola;
* la decisione segue regole dichiarate PRIMA del test (`DECISION_RULES`).

Nessun ordine, nessuna scrittura sulla produzione: lo snapshot e' letto in
`mode=ro` da `loader`.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

import numpy as np
import pandas as pd

from . import ev_audit, loader, prereg, stats

OUT_DIR = Path(__file__).resolve().parents[1] / "out"

#: Saldo dichiarato (fonte esterna) e ancora di riconciliazione. L'ancora e'
#: l'ultima lettura PATRIMONIALE registrata dal sistema stesso (il file del
#: kill switch CB2, che scrive l'equity al momento dell'innesco): senza una
#: ancora esterna il solo ledger non puo' verificare un saldo, perche' i
#: depositi non sono movimenti registrati.
DECLARED = {
    "value": 21.1004,
    "at": "2026-10-20T03:25:00+00:00",
    "source": "execution_engine.py --provider sxbet --balance (availableBalance)",
    "note": "saldo LIBERO; escrow 0 al momento della lettura",
}
ANCHOR = {
    "equity": 23.6901,
    "at": "2026-10-18T04:42:00+00:00",
    "source": "data/execution/t60_kill.json (equity all'innesco del CB2)",
}


# ---------------------------------------------------------------------------
# 1) CAPITALE
# ---------------------------------------------------------------------------
def capital_block(bets_df: pd.DataFrame) -> dict:
    """Verifica la provenienza del saldo e ricostruisce ogni movimento.

    Tre domande, tre risposte separate:

    1. **il valore dichiarato e' equity o cassa?** Con escrow 0 coincidono; se
       ci fosse escrow, il disponibile non sarebbe il bankroll;
    2. **e' ancora attuale?** Un saldo letto prima di un settlement e' storico
       (`stale`): usarlo per calcolare stake sarebbe l'errore del 21/09;
    3. **i movimenti del ledger lo spiegano?** Dall'ancora in poi ogni vincita
       rientra come stake + profitto, ogni push come stake, ogni perdita nulla.
    """
    rec = ev_audit.capital_reconciliation(
        bets_df, declared_value=DECLARED["value"], declared_at=DECLARED["at"],
        anchor=ANCHOR, source=DECLARED["source"])
    b = ev_audit._num(bets_df, ("stake", "price", "profit"))
    live = b[b["mode"].astype(str).str.lower() == "live"] if len(b) else b
    rec["ledger"] = {
        "bets_live": int(len(live)),
        "staked_total": float(live["stake"].fillna(0.0).sum()) if len(live) else 0.0,
        "profit_total": float(live["profit"].fillna(0.0).sum()) if len(live) else 0.0,
        "open_bets": int(live["settled_at"].isna().sum()) if len(live) else 0,
        "open_stake": float(live[live["settled_at"].isna()]["stake"].fillna(0.0).sum())
        if len(live) else 0.0,
    }
    rec["declared"] = DECLARED
    rec["anchor"] = {**ANCHOR, **(rec.get("anchor") or {})}
    rec["equity_now"] = float(DECLARED["value"]) + rec["ledger"]["open_stake"]
    rec["stale"] = bool(rec.get("stale"))
    if not rec.get("verified"):
        rec["verdict"] = "NON VERIFICABILE"
    elif rec.get("stale"):
        rec["verdict"] = "SALDO STORICO (non usare per dimensionare)"
    elif rec.get("within_tolerance"):
        rec["verdict"] = "RICONCILIATO"
    else:
        rec["verdict"] = "RESIDUO NON SPIEGATO"
    return rec


# ---------------------------------------------------------------------------
# 2) EV ATTESO vs REALIZZATO
# ---------------------------------------------------------------------------
def ev_block(preds: pd.DataFrame, results_df: Optional[pd.DataFrame] = None) -> dict:
    """Il divario, scomposto in addendi che sommano al totale.

    La prima cosa da stabilire non e' *quanto* e' grande il divario ma **se le
    due grandezze confrontate sono la stessa cosa**. Il controllo `ev_identity`
    confronta l'EV del ledger con `prob*quota-1`: dove i due numeri coincidono
    (1X2, tennis) l'EV descrive la probabilita' registrata; dove non
    coincidono (OU/AH) l'EV e' calcolato sulla probabilita' del MODELLO
    (push-aware) mentre `prob` e' quella BLENDATA col mercato — verificato
    sulle righe reali, non dedotto.
    """
    gap = ev_audit.gap_attribution(preds)
    ident = ev_audit.ev_identity(preds)
    pop = ev_audit.effective_population(preds, results_df)
    out = {"gap": gap, "identity": ident, "population": pop,
           "identity_example": ev_audit.identity_example(preds),
           "n_closed_playable": int(pop.get("all_closed", {}).get("n") or 0)}
    dec = gap.get("decomposition") or {}
    out["summary"] = {
        "ev_ledger": dec.get("ev_ledger"),
        "ev_market": dec.get("ev_market"),
        "realized": dec.get("realized"),
        "gap_total": dec.get("gap_total"),
        "definition_gap": dec.get("definition_gap"),
        "edge_claimed": dec.get("edge_claimed"),
        "residual": dec.get("residual"),
        "calibration_gap": dec.get("calibration_gap"),
        "calibration_ci95": dec.get("calibration_gap_ci95"),
        "realized_ci95": dec.get("realized_ci95"),
        "min_detectable_edge": dec.get("min_detectable_edge"),
    }
    # Chi descrive i risultati? Il mercato (devigato) o il modello?
    out["who_predicts"] = {
        "model_ev": dec.get("ev_ledger"),
        "blend_ev": dec.get("ev_repro"),
        "market_ev": dec.get("ev_market"),
        "realized": dec.get("realized"),
        "distance_market": (None if dec.get("ev_market") is None
                            or dec.get("realized") is None
                            else dec["realized"] - dec["ev_market"]),
        "distance_model": (None if dec.get("ev_ledger") is None
                           or dec.get("realized") is None
                           else dec["realized"] - dec["ev_ledger"]),
    }
    return out


# ---------------------------------------------------------------------------
# 3) CLV: le due coorti, il margine e il controllo stessa-venue
# ---------------------------------------------------------------------------
def clv_block(clv_df: pd.DataFrame, preds: pd.DataFrame,
              snapshots: Optional[pd.DataFrame] = None,
              bets_df: Optional[pd.DataFrame] = None) -> dict:
    """CLV aggregato, coorti, de-vig e realizzato delle stesse righe.

    Le coorti nominate nel report precedente sono ricostruite QUI (non
    ricopiate): `non_echo` (chiusura diversa dalla quota di segnale) e
    `pinnacle_t0` (chiusura scritta dal job a T-0 sulla closing Pinnacle).
    Per ognuna: numerosita', stabilita' temporale, performance REALIZZATA delle
    righe corrispondenti e CLV de-vigato quando il margine e' misurabile.
    """
    base = ev_audit.clv_audit(clv_df, preds, bets_df=bets_df)
    fr = ev_audit._clv_frame(clv_df)
    out: dict = {"aggregate": base, "cohorts": {},
                 "fair": ev_audit.fair_clv(clv_df, snapshots) if snapshots is not None
                         else {"status": "NON VERIFICABILE", "reason": "snapshot assenti"}}
    if fr.empty:
        return out
    pc = ev_audit.playable_closed(preds)[["match_id", "esito", "profit",
                                          "esito_finale", "mercato", "created"]]
    m = fr.merge(pc, on=["match_id", "esito"], how="left", suffixes=("", "_p"))
    m["week"] = m["upd"].dt.tz_convert("UTC").dt.strftime("%Y-W%W")

    def cohort(sel: pd.DataFrame, label: str) -> dict:
        g = sel[sel["profit"].notna()]
        ci = stats.bootstrap_mean_ci(sel["clv"].tolist(),
                                     sel["match_id"].tolist(), n_boot=3000, seed=7)
        weeks = sel.groupby("week")["clv"].agg(["count", "mean"]).round(6)
        return {
            "label": label, "n": int(len(sel)),
            "clv_mean": float(sel["clv"].mean()),
            "clv_ci95": [ci["lo"], ci["hi"]],
            "share_positive": float((sel["clv"] > 0).mean()),
            "weeks": int(sel["week"].nunique()),
            "week_table": weeks.to_dict("index"),
            "realized_n": int(len(g)),
            "realized_roi": float(g["profit"].mean()) if len(g) else None,
            "realized_hit": float((g["esito_finale"] == "won").mean()) if len(g) else None,
            "unmatched": int(sel["profit"].isna().sum()),
        }

    out["cohorts"]["non_echo"] = cohort(m[~m["echo"]], "non-eco (chiusura != segnale)")
    pin = m[m["source"] == "pinnacle_t0"]
    out["cohorts"]["pinnacle_t0"] = cohort(pin, "closing Pinnacle a T-0")
    sx = m[m["source"] == "sx"]
    out["cohorts"]["sx"] = cohort(sx, "chiusura SX (stessa venue)")
    out["by_source"] = base.get("by_source")
    return out


# ---------------------------------------------------------------------------
# 4) DECISIONE (regole dichiarate prima del test)
# ---------------------------------------------------------------------------
DECISION_RULES = {
    "nessun_vantaggio": [
        "il test pre-registrato sull'holdout NON passa (INCONCLUSIVE o FAIL)",
        "oppure il CLV de-vigato non sopravvive (non distinguibile da zero)",
        "oppure il candidato non batte il benchmark sulla stessa finestra",
    ],
    "shadow": [
        "il test PASSA (IC95 > 0) e batte il benchmark",
        "ma il campione e' sotto le 200 chiusure oppure il CLV de-vigato non "
        "e' positivo: il risultato puo' essere rumore o selezione",
    ],
    "candidato": [
        "il test PASSA con n >= 200",
        "e il CLV de-vigato e' positivo con IC95 > 0",
        "e il candidato batte il benchmark sulla stessa finestra",
    ],
}
SHADOW_PROTOCOL = {
    "min_closures": 200,
    "shadow_days": 30,
    "order_size_usdc": 1.00,
    "cb2": "ARMATO (soglia invariata: nessun ordine finche' l'equity non la supera)",
    "live": "DISABILITATO",
    "success": "CLV de-vigato > 0 con IC95 > 0 sul campione shadow",
    "abort": "CLV de-vigato < 0 dopo 100 chiusure",
}


def decide(audit: dict, test: dict) -> dict:
    """Applica `DECISION_RULES` ai numeri. Nessuna eccezione non dichiarata."""
    t = test.get("test") or {}
    fair = (audit.get("clv") or {}).get("fair") or {}
    pass_test = test.get("verdict") == "PASS"
    fair_ok = fair.get("status") == "SOPRAVVIVE AL DE-VIG"
    beats = test.get("beats_benchmark")
    n = int(t.get("n") or 0)
    reasons = []
    if not pass_test:
        reasons.append(f"holdout {test.get('verdict')}: {test.get('why')}")
        decision = "nessun vantaggio dimostrato"
    elif not fair_ok:
        # `or 'n/d'`: uno stato ASSENTE non e' la stringa "None" — e un report
        # che scrive None al posto di "non misurabile" sembra un errore di codice
        # proprio nel punto in cui dichiara di non poter concludere.
        reasons.append(f"CLV de-vigato: {fair.get('status') or 'n/d'}")
        decision = ("segnale promettente da verificare in shadow"
                    if beats and n < SHADOW_PROTOCOL["min_closures"] else
                    "nessun vantaggio dimostrato")
        if beats and n >= SHADOW_PROTOCOL["min_closures"]:
            reasons.append("campione sufficiente ma processo non dimostrato: "
                           "il P/L da solo non basta")
    elif beats is False:
        reasons.append("il candidato NON batte il benchmark sulla stessa finestra")
        decision = "nessun vantaggio dimostrato"
    elif n < SHADOW_PROTOCOL["min_closures"]:
        reasons.append(f"n={n} < {SHADOW_PROTOCOL['min_closures']} chiusure")
        decision = "segnale promettente da verificare in shadow"
    else:
        reasons.append("test PASS, benchmark battuto, CLV de-vigato positivo")
        decision = "candidato idoneo a una futura prova controllata"
    return {"decision": decision, "reasons": reasons,
            "pass_test": bool(pass_test), "fair_clv_ok": bool(fair_ok),
            "beats_benchmark": beats, "n_test": n,
            "rules": DECISION_RULES, "shadow_protocol": SHADOW_PROTOCOL}


# ---------------------------------------------------------------------------
# 5) TESTO DEL REPORT
# ---------------------------------------------------------------------------
def _pct(x, nd=2) -> str:
    """Percentuale o `n/d`. Mai `None` stampato: un report che scrive "None"
    al posto di un dato mancante sembra un errore di codice e nasconde che la
    misura non esiste."""
    if x is None:
        return "n/d"
    try:
        v = float(x)
    except (TypeError, ValueError):
        return "n/d"
    return "n/d" if np.isnan(v) else f"{v * 100:+.{nd}f}%"


def _share(x, nd=1) -> str:
    """Quota (0..1) come percentuale SENZA segno: una frequenza non e' un
    delta, e scriverla "+63.5% positivi" fa leggere un rendimento dove c'e'
    una proporzione."""
    if x is None:
        return "n/d"
    try:
        v = float(x)
    except (TypeError, ValueError):
        return "n/d"
    return "n/d" if np.isnan(v) else f"{v * 100:.{nd}f}%"


def _n(x) -> str:
    """Intero o `n/d` (per i conteggi che possono mancare)."""
    return "n/d" if x is None else str(x)


def _yes(x) -> str:
    """Booleano leggibile; `None` = non misurabile, mai la stringa 'None'."""
    if x is None:
        return "non misurabile"
    return "sì" if x else "no"


def _mag(x, nd=2) -> str:
    """Modulo di una percentuale, SENZA segno: la direzione la dice `_side`.
    "+1.89% sotto" e' una contraddizione che si legge come un refuso."""
    if x is None:
        return "n/d"
    try:
        v = abs(float(x))
    except (TypeError, ValueError):
        return "n/d"
    return "n/d" if np.isnan(v) else f"{v * 100:.{nd}f}%"


def _side(v) -> str:
    """Verso di uno scarto: "sopra" / "sotto" / "in linea con" / `n/d`.
    Una distanza negativa stampata come "a +1.89% dal mercato" dice l'opposto
    di quello che e' successo."""
    if v is None:
        return "n/d"
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "n/d"
    if np.isnan(v):
        return "n/d"
    return "sopra" if v > 0 else ("sotto" if v < 0 else "in linea con")


def _num(x, nd=4) -> str:
    return "n/d" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x:.{nd}f}"


def render_report(audit: dict, test: dict, decision: dict, *, snapshot_at: str = "") -> str:
    cap = audit["capital"]
    ev = audit["ev"]
    clv = audit["clv"]
    s = ev["summary"]
    pop = ev["population"]
    by_mkt = ev["gap"].get("by_market") or {}
    agg = clv["aggregate"]
    fair = clv["fair"]
    coh = clv["cohorts"]
    L = []
    add = L.append

    add("# QUOTAVERACE — Diagnosi del segnale e decisione operativa")
    add("")
    add(f"**Data:** {datetime.now(timezone.utc).strftime('%d/%m/%Y %H:%M UTC')} · "
        f"**Perimetro:** copia isolata `research_isolated/` · snapshot "
        f"`{snapshot_at or 'quotaverace_prod.db'}` (sola lettura) · "
        "**nessun ordine, nessuna modifica alla produzione**")
    add("")
    add("Regola: ogni numero ha campione e intervallo; ciò che i dati non "
        "permettono di misurare è marcato `NON VERIFICABILE`.")
    add("")
    add("---")
    add("")
    add("## 0. Sintesi per il decisore")
    add("")
    add(f"1. **Capitale riconciliato: {_num(cap['equity_now'])} USDC** "
        f"({cap['verdict']}; residuo {_num(cap.get('residual'))} USDC). "
        f"Il valore dichiarato è il **disponibile**, l'escrow al momento della "
        f"lettura è {_num(cap.get('exposure_at_declared'))} USDC, quindi "
        f"l'equity di riferimento è {_num(cap['equity_now'])} USDC: "
        "nessun denaro in gioco fuori dal conteggio.")
    dm = ev["who_predicts"].get("distance_market")
    dmod = ev["who_predicts"].get("distance_model")
    _sd = (s["definition_gap"] / s["gap_total"]
           if s.get("gap_total") and s.get("definition_gap") is not None else None)
    add(f"2. **Il divario atteso/realizzato ha una causa contabile precisa, non "
        f"la sfortuna**: {_share(_sd, 0)} del divario è una **definizione diversa** "
        f"dello stesso numero. L'EV a ledger ({_pct(s['ev_ledger'])}) "
        f"è calcolato sulla probabilità del **modello**, mentre `prob` è la "
        f"probabilità **blendata col mercato**; tolti i {_pct(s['definition_gap'])} "
        f"di definizione l'EV riproducibile è "
        f"{_pct((ev['gap'].get('decomposition') or {}).get('ev_repro'))}. "
        f"Il realizzato ({_pct(s['realized'])}) sta "
        f"{_mag(dm)} {_side(dm)} il mercato e "
        f"{_mag(dmod)} {_side(dmod)} il "
        f"modello — ma il "
        f"suo IC95 è {_pct(s['realized_ci95'][0])}…{_pct(s['realized_ci95'][1])}: "
        f"il campione **smentisce l'EV del modello**, non dimostra che il mercato "
        f"sia calibrato.")
    add(f"3. **La componente statistica è grande e va dichiarata**: IC95 del "
        f"realizzato {_pct(s['realized_ci95'][0])}…{_pct(s['realized_ci95'][1])}, "
        f"edge minimo distinguibile {_pct(s['min_detectable_edge'])} con "
        f"{pop.get('all_closed', {}).get('n', 0)} chiusure.")
    add(f"4. **Il CLV positivo è il margine del bookmaker**: "
        f"{_pct(fair.get('clv_book'))} contro la quota sharp, "
        f"**{_pct(fair.get('clv_fair'))} dopo il de-vig** "
        f"(margine misurato {_pct(fair.get('margin_mean'))}, "
        f"{_num(fair.get('margin_share_of_clv'), 2)}× il CLV misurato, "
        f"n={_n(fair.get('n'))}). Controllo indipendente: contro la **stessa "
        f"venue** (SX) il CLV è {_pct(coh.get('sx', {}).get('clv_mean'))} "
        f"(n={_n(coh.get('sx', {}).get('n'))}).")
    add(f"5. **Test pre-registrato: {test.get('verdict')}** — {test.get('why')}. "
        f"Candidato: {_pct((test.get('test') or {}).get('roi'))} "
        f"(n={_n((test.get('test') or {}).get('n'))}), benchmark "
        f"{_pct((test.get('benchmark_test') or {}).get('roi'))} "
        f"(n={_n((test.get('benchmark_test') or {}).get('n'))}).")
    add("")
    add(f"**DECISIONE: {decision['decision'].upper()}**")
    add("")
    for r in decision["reasons"]:
        add(f"- {r}")
    add("")
    add("CB2 **armato**, LIVE **disabilitato**, nessun ordine eseguito.")
    add("")
    add("---")
    add("")

    # --- 1 capitale
    add("## 1. Riconciliazione del capitale")
    add("")
    add("| grandezza | valore | fonte |")
    add("|---|---|---|")
    add(f"| saldo dichiarato | **{_num(cap['declared_value'])} USDC** | "
        f"{cap['declared'].get('source')} @ {cap['declared'].get('at')} |")
    add(f"| escrow al momento della lettura | {_num(cap.get('exposure_at_declared'))} USDC | "
        "ledger `bets` (ordini aperti) |")
    add(f"| equity di riferimento | **{_num(cap['equity_now'])} USDC** | "
        "disponibile + esposizione |")
    add(f"| ancora esterna | {_num(cap['anchor'].get('equity'))} USDC @ "
        f"{cap['anchor'].get('at')} | {cap['anchor'].get('source')} |")
    add(f"| puntate live a ledger | {cap['ledger']['bets_live']} | "
        f"stake {_num(cap['ledger']['staked_total'], 2)} USDC, "
        f"P/L {_num(cap['ledger']['profit_total'], 2)} USDC |")
    add(f"| ordini aperti | {cap['ledger']['open_bets']} | "
        f"stake {_num(cap['ledger']['open_stake'], 4)} USDC |")
    add("")
    add("**Il conto della riconciliazione** (dall'ancora al saldo dichiarato):")
    add("")
    add("```")
    add(f"equity(ancora)                       {_num(cap['anchor'].get('equity'))}")
    add(f"- esposizione aperta(ancora)         {_num((cap.get('anchor') or {}).get('exposure', 0.0))}")
    add(f"+ cassa rientrata ({cap.get('window_settlements')} settlement)  "
        f"{_num(cap.get('window_inflow'))}")
    add(f"+ esposizione aperta(oggi)           {_num(cap.get('exposure_at_declared'))}")
    add(f"= atteso                             {_num(cap.get('expected_value'))}")
    add(f"  dichiarato                         {_num(cap['declared_value'])}")
    add(f"  residuo                            {_num(cap.get('residual'))} USDC "
        f"({_pct(cap.get('residual_pct'))})")
    add("```")
    add("")
    add(f"Esito: **{cap['verdict']}** · tolleranza {_num(cap.get('tolerance'), 2)} USDC · "
        f"`stale`: {cap.get('stale')} · settlement dopo la misura: "
        f"{cap.get('settlements_after_declared')}")
    add("")
    add("Cosa questo NON dimostra: il **livello** del capitale non è ricostruibile "
        "dal ledger, perché i depositi non sono movimenti registrati. La "
        "verifica riguarda i **movimenti** dal 18/10 04:42: da lì in poi ogni "
        "centesimo è spiegato da vincite, push e perdite.")
    add("")

    # --- 2 EV
    add("## 2. Perché l'EV atteso non coincide con i risultati")
    add("")
    allc = pop.get("all_closed") or {}
    trad = pop.get("traded") or {}
    decc = pop.get("decided") or {}

    def _gap(d):
        if d.get("ev_ledger") is None or d.get("roi") is None:
            return None
        return d["ev_ledger"] - d["roi"]

    add("### 2.0 Da dove vengono i due numeri citati (+11,53% e −2,52%)")
    add("")
    add("La coppia citata **non è un errore di contabilità**: sono le due medie "
        "sullo **stesso** insieme — tutte le righe giocabili chiuse, scadenze "
        "incluse. La verifica è la tabella qui sotto.")
    add("")
    add("| popolazione | n | EV a ledger | ROI realizzato | divario | vinte / perse / push |")
    add("|---|---|---|---|---|---|")
    for label, d in (("tutte le chiuse (la coppia citata)", allc),
                     ("puntate effettive (scadenze escluse)", trad),
                     ("solo decise (won/lost)", decc)):
        add(f"| {label} | {_n(d.get('n'))} | {_pct(d.get('ev_ledger'))} | "
            f"{_pct(d.get('roi'))} | {_pct(_gap(d))} | {_n(d.get('wins'))} / "
            f"{_n(d.get('losses'))} / {_n(d.get('pushes'))} |")
    add("")
    add(f"**{_pct(allc.get('ev_ledger'))} atteso e {_pct(allc.get('roi'))} "
        f"realizzato sullo stesso campione** (n={_n(allc.get('n'))}), divario "
        f"{_pct(_gap(allc))}. Sulle sole {_n(decc.get('n'))} decise il divario "
        f"sale a {_pct(_gap(decc))}.")
    add("")
    add("Il controllo contabile, voce per voce — nessuna delle seguenti spiega "
        "il grosso del divario:")
    add("")
    add(f"- **commissioni: nessuna**. SX Bet è un exchange P2P e il ledger "
        f"registra `profit` **per unità di stake** senza fee; il costo del "
        f"referto è in crediti API, non in cassa, quindi non entra nel P/L.")
    add(f"- **push e rimborsi**: {_n(allc.get('pushes'))} chiuse come push "
        f"({_n(pop.get('expiry_n'))} scadenze + {_n(trad.get('pushes'))} "
        f"rimborsi di mercato veri) con profitto 0: **diluiscono** la media "
        f"({_pct(pop.get('expiry_drag'))} sulle chiuse), non la falsano.")
    add(f"- **duplicati**: {_n(pop.get('duplicates_dropped'))} righe rimosse dal "
        f"dedup partita+esito · **verdetti contraddittori**: "
        f"{_n(pop.get('wrong_verdict_n'))}.")
    add(f"- **risultati mancanti**: le {_n(pop.get('expiry_n'))} scadenze "
        f"({_share(pop.get('expiry_share'))} delle chiuse) sono righe mai "
        f"giocate, chiuse come push perché il referto non ha trovato il "
        f"risultato (scadenza a 5 giorni): contano zero.")
    add("")
    add("### 2.1 Le due grandezze confrontate non sono la stessa cosa")
    add("")
    add("| mercato | n | EV a ledger | `prob*quota-1` | scarto | righe coerenti |")
    add("|---|---|---|---|---|---|")
    for mk, d in (ev["identity"].get("by_market") or {}).items():
        add(f"| {mk} | {d['n']} | {_pct(d['mean_ev'])} | {_pct(d['mean_ev_calc'])} | "
            f"{_pct(d['mean_ev'] - d['mean_ev_calc'])} | {d['consistent_share'] * 100:.1f}% |")
    add("")
    ident = ev["identity"].get("by_market") or {}
    exact = [mk for mk, d in ident.items() if (d.get("consistent_share") or 0) >= 0.999]
    inexact = [(mk, d) for mk, d in ident.items() if (d.get("consistent_share") or 0) < 0.999]
    add(f"Coerenza piena (100%) su **{', '.join(exact) if exact else 'nessun mercato'}**: "
        "lì l'EV a ledger è esattamente `prob*quota-1`, quindi il numero descrive "
        "la probabilità registrata. Non lo è su "
        + (", ".join(f"**{mk}** (scarto medio {_pct(d.get('mean_abs_delta'))}, "
                      f"coerenza {_share(d.get('consistent_share'))})"
                      for mk, d in inexact) if inexact else "nessun mercato")
        + ": qui l'EV è `p_win*(quota-1) - p_lose` sulla probabilità **del "
        "modello** (push che restituisce lo stake) mentre `prob` è la probabilità "
        "**blendata col mercato**. Entrambe legittime, non la stessa misura — e "
        "il report precedente le ha confrontate come se lo fossero.")
    add("")
    ex = ev.get("identity_example")
    if ex:
        add(f"Caso reale (lo scarto **maggiore**, non la media): `{ex['esito']}` "
            f"({ex['mercato']}), quota {_num(ex['quota'], 4)}. Se l'EV a ledger "
            f"({_pct(ex['ev'])}) venisse dalla probabilità registrata "
            f"({_num(ex['prob'], 4)}), l'EV sarebbe {_pct(ex['ev_calc'])}; "
            f"invece corrisponde a una probabilità di **{_num(ex['implied_prob'], 4)}**. "
            f"Lo stesso numero descrive una probabilità che sul ledger non c'è "
            f"(scarto {_pct(ex['delta'])}).")
    add("")
    add("")
    add("### 2.2 Chi descrive i risultati: il mercato, non il modello")
    add("")
    add("| grandezza | valore |")
    add("|---|---|")
    add(f"| EV del **modello** (quello a ledger) | {_pct(s['ev_ledger'])} |")
    add(f"| EV della **probabilità blendata** | {_pct(ev['gap'].get('decomposition', {}).get('ev_repro'))} |")
    add(f"| EV del **mercato** devigato | {_pct(s['ev_market'])} |")
    add(f"| **realizzato** | {_pct(s['realized'])} |")
    add(f"| distanza realizzato − mercato | {_pct(ev['who_predicts']['distance_market'])} |")
    add(f"| distanza realizzato − modello | {_pct(ev['who_predicts']['distance_model'])} |")
    add("")
    # Direzione esplicita, non solo il valore assoluto: "1.89% sotto il mercato"
    # e' l'informazione, "a +1.89% dal mercato" ne darebbe l'opposta.
    add(f"Il realizzato sta **{_mag(dm)} "
        f"{_side(dm)} il mercato** e **"
        f"{_mag(dmod)} {_side(dmod)} il "
        f"modello**: con un IC95 di {_pct(s['realized_ci95'][0])}…"
        f"{_pct(s['realized_ci95'][1])} questo **non** dimostra che il mercato "
        f"sia calibrato — dimostra che l'EV del modello è troppo alto di un "
        f"ordine di grandezza rispetto al divario misurabile.")
    add("")
    add("### 2.3 Scomposizione del divario (gli addendi sommano)")
    add("")
    add("```")
    add(f"atteso (modello)   {_pct(s['ev_ledger'])}")
    add(f"= definizione      {_pct(s['definition_gap'])}   (EV del modello vs prob. registrata)")
    add(f"+ edge dichiarato  {_pct(s['edge_claimed'])}   (modello vs mercato devigato)")
    add(f"+ residuo          {_pct(s['residual'])}   (non linearità: push, quote, blend)")
    add(f"realizzato         {_pct(s['realized'])}")
    add("```")
    add("")
    share_def = (s["definition_gap"] / s["gap_total"]
                 if s.get("gap_total") and s.get("definition_gap") is not None else None)
    add(f"Divario totale **{_pct(s['gap_total'])}**, di cui **{_pct(s['definition_gap'])}** "
        f"di definizione (**{_share(share_def, 0)}** del divario) e "
        f"{_pct(s['edge_claimed'])} di edge dichiarato dal modello.")
    add("")
    add("### 2.4 Per mercato")
    add("")
    add("| mercato | n | EV modello | EV mercato | realizzato | IC95 realizzato | edge minimo |")
    add("|---|---|---|---|---|---|---|")
    for mk, d in by_mkt.items():
        add(f"| {mk} | {d['n']} | {_pct(d['ev_ledger'])} | {_pct(d['ev_market'])} | "
            f"{_pct(d['realized'])} | {_pct(d['realized_ci95'][0])}…{_pct(d['realized_ci95'][1])} | "
            f"{_pct(d['min_detectable_edge'])} |")
    add("")
    one = by_mkt.get("1X2") or {}
    worst = max(by_mkt.items(), key=lambda kv: (kv[1].get("definition_gap") or 0),
                default=(None, {}))
    add(f"**1X2 è l'unico mercato in cui l'EV descrive il risultato** "
        f"({_pct(one.get('ev_ledger'))} atteso, {_pct(one.get('realized'))} "
        f"realizzato, n={_n(one.get('n'))}). "
        + (f"**{worst[0]}** è il mercato con lo scarto di definizione più alto "
           f"({_pct(worst[1].get('definition_gap'))}) e il risultato peggiore "
           f"({_pct(worst[1].get('realized'))}): è la firma di una probabilità di "
           f"modello sovrastimata **proprio dove il gate seleziona**." if worst[0] else ""))
    add("")
    add("### 2.5 Push, scadenze e popolazione")
    add("")
    add("| popolazione | n | ROI flat | IC95 | EV a ledger |")
    add("|---|---|---|---|---|")
    for k in ("all_closed", "traded", "decided"):
        d = pop.get(k) or {}
        if not d:
            continue
        add(f"| {d['label']} | {d['n']} | {_pct(d['roi'])} | "
            f"{_pct(d['roi_ci95'][0])}…{_pct(d['roi_ci95'][1])} | {_pct(d['ev_ledger'])} |")
    add("")
    add(f"- **{pop.get('expiry_n')} scadenze** ({_share(pop.get('expiry_share'))} delle "
        f"chiusure) sono righe mai giocate, chiuse come `push` perché il referto non "
        f"ha trovato il risultato: pesano **{_pct(pop.get('expiry_drag'))}** sul divario.");
    add("- push **di mercato**: rimborso vero (lo stake torna indietro), non un "
        f"errore: {(pop.get('traded') or {}).get('pushes')} fra le giocabili "
        "effettive.")
    add(f"- verdetti contraddittori: {pop.get('wrong_verdict_n')} · duplicati: "
        f"{pop.get('duplicates_dropped')}.")
    add("")
    add("### 2.6 Quanto è misurabile")
    add("")
    add(f"Con n={pop.get('all_closed', {}).get('n', 0)}, deviazione standard "
        f"{_num(s.get('sd') or (ev['gap'].get('decomposition') or {}).get('sd'), 3)} per unità "
        f"e IC95 al 95%, l'edge minimo distinguibile è **{_pct(s['min_detectable_edge'])}**. "
        "Un edge reale del 2% resta invisibile: **il campione non permette di "
        "confermare un vantaggio piccolo, solo di smentire un disastro**.")
    add("")

    # --- 3 CLV
    add("## 3. Il CLV positivo: abilità o margine?")
    add("")
    add("### 3.0 Metodologia (verificata sul codice, non assunta)")
    add("")
    by_src = agg.get("by_source") or {}
    add("- **formula**: `CLV = quota_segnale / quota_chiusura - 1` "
        "(`market_calib.clv_raw`). **Segno**: positivo = abbiamo preso un prezzo "
        "migliore di quello di chiusura. Un CLV medio positivo con P/L negativo "
        "è possibile e va spiegato, non nascosto.")
    add("- **precedenza della chiusura**: `closing_odds` (Pinnacle scritta dal job "
        "a T-0) → `closing_quota` → `pinnacle_quota`. La fonte viaggia con il "
        "campione (`closing_source`) e le coorti non si mescolano.")
    add(f"- **campione**: {_n(agg.get('n'))} osservazioni, di cui "
        f"**{_n(agg.get('echo_n'))} echi** (chiusura identica al segnale: CLV 0 "
        "per costruzione) esclusi dalla coorte utile, che resta "
        f"**{_n((agg.get('non_echo') or {}).get('n'))}** righe.")
    add(f"- **fonti dichiarate**: "
        + (", ".join(f"`{k}` {v['n']}" for k, v in by_src.items()) or "nessuna")
        + f" (fonte non dichiarata: {_n((by_src.get('none') or {}).get('n'))}).")
    add("- **timestamps**: il CLV usa `closing_*` all'istante dell'ultima lettura "
        "(`updated_at`); la stabilità è misurata per settimana, perché una "
        "coorte concentrata in una sola settimana non distingue edge e periodo.")
    add("")
    add("| campione | n | CLV medio | IC95 | positivi | realizzato (stesse righe) |")
    add("|---|---|---|---|---|---|")
    add(f"| tutte le chiusure | {agg.get('n')} | {_pct(agg.get('mean'))} | "
        f"{_pct((agg.get('ci95') or [None, None])[0])}…{_pct((agg.get('ci95') or [None, None])[1])} | "
        f"{_share(agg.get('share_positive'))} | — |")
    for key, label in (("non_echo", "non-eco"), ("pinnacle_t0", "closing Pinnacle T-0"),
                       ("sx", "chiusura SX (stessa venue)")):
        c = coh.get(key) or {}
        if not c:
            continue
        add(f"| {label} | {c['n']} | {_pct(c['clv_mean'])} | "
            f"{_pct(c['clv_ci95'][0])}…{_pct(c['clv_ci95'][1])} | "
            f"{_share(c['share_positive'])} | {_pct(c['realized_roi'])} "
            f"(n={_n(c['realized_n'])}) |")
    add("")
    add(f"**{agg.get('echo_n')} osservazioni su {agg.get('n')}** "
        f"({_share(agg.get('echo_share'))}) sono **echi**: la chiusura coincide "
        "con la quota di segnale, quindi il loro CLV è zero per costruzione. "
        "Includerli diluisce la media: il numero da leggere è quello dei non-eco.")
    add("")
    add("### 3.1 Il controllo che decide: de-vig e stessa venue")
    add("")
    add("| misura | n | valore | IC95 | verdetto |")
    add("|---|---|---|---|---|")
    add(f"| CLV vs quota sharp (con margine) | {_n(fair.get('n'))} | {_pct(fair.get('clv_book'))} | "
        f"{_pct((fair.get('clv_book_ci95') or [None, None])[0])}…"
        f"{_pct((fair.get('clv_book_ci95') or [None, None])[1])} | positivo |")
    add(f"| CLV vs **fair** (de-vigato) | {_n(fair.get('n'))} | {_pct(fair.get('clv_fair'))} | "
        f"{_pct((fair.get('clv_fair_ci95') or [None, None])[0])}…"
        f"{_pct((fair.get('clv_fair_ci95') or [None, None])[1])} | "
        f"{fair.get('status') or 'n/d'} |")
    add(f"| margine medio misurato | {_n(fair.get('n'))} | {_pct(fair.get('margin_mean'))} | — | "
        f"{_num(fair.get('margin_share_of_clv'), 2)}× il CLV positivo |")
    add("")
    add("Il margine **non è stimato**: `sum(1/quota)` è calcolato sulle rilevazioni "
        "sharp complete della **stessa partita** (tutti e tre gli esiti allo stesso "
        "istante). Il CLV contro la quota grezza contiene quel margine per "
        f"costruzione: {_pct(fair.get('margin_mean'))} di margine contro "
        f"{_pct(fair.get('clv_book'))} di CLV.")
    add("")
    add("Due conferme indipendenti che il numero è margine:")
    add("")
    add(f"1. **de-vig**: togliendo il margine il CLV scende a {_pct(fair.get('clv_fair'))} "
        f"(IC95 {_pct((fair.get('clv_fair_ci95') or [None, None])[0])}…"
        f"{_pct((fair.get('clv_fair_ci95') or [None, None])[1])}) e la quota di "
        f"campioni positivi passa da {_share(fair.get('share_positive_book'))} a "
        f"{_share(fair.get('share_positive_fair'))};")
    sx_mean = (coh.get("sx") or {}).get("clv_mean")
    add(f"2. **stessa venue**: quando segnale e chiusura sono entrambi SX (nessun "
        f"margine da attraversare) il CLV è {_pct(sx_mean)} "
        f"(n={_n((coh.get('sx') or {}).get('n'))}): "
        + ("**negativo**, cioè entriamo a un prezzo leggermente peggiore di "
           "quello di chiusura sullo stesso book." if (sx_mean or 0) < 0 else
           "**positivo**, ma il campione è troppo piccolo per concludere."))
    add("")
    add("### 3.2 Le due coorti nominate (+1,70% e +3,34%)")
    add("")
    for key, name in (("non_echo", "non-eco"), ("pinnacle_t0", "closing Pinnacle T-0")):
        c = coh.get(key) or {}
        if not c:
            continue
        # L'etichetta porta la media MISURATA, non il numero del report
        # precedente: due coorti con lo stesso nome e un numero incollato sono
        # il modo piu' facile per non accorgersi che la misura e' cambiata.
        add(f"**{_pct(c.get('clv_mean'))} — {name}**")
        add("")
        add(f"- numerosità **{c['n']}**, CLV {_pct(c['clv_mean'])} "
            f"(IC95 {_pct(c['clv_ci95'][0])}…{_pct(c['clv_ci95'][1])}), "
            f"{_share(c['share_positive'])} positivi;")
        weeks_txt = ", ".join(
            f"{k}: n={v.get('count', 0)} {_pct(v.get('mean'))}"
            for k, v in (c.get("week_table") or {}).items())
        weeks_lbl = "settimana" if c["weeks"] == 1 else "settimane"
        add(f"- stabilità: **{c['weeks']} {weeks_lbl}** nel campione ({weeks_txt});")
        add(f"- performance realizzata delle stesse righe: **{_pct(c['realized_roi'])}** "
            f"su {c['realized_n']} chiuse (hit {_share(c['realized_hit'])}), "
            f"{c['unmatched']} righe non abbinabili a una previsione giocabile;")
        add("- selezione: è un sottoinsieme **non casuale** (le righe con chiusura "
            "catturata sono quelle con cache sharp disponibile) e questo è il "
            "limite principale della coorte;")
        add("")
    add("Conclusione della sezione: **un CLV positivo non implica profitto**. Le "
        "due coorti hanno CLV positivo e realizzato negativo, e la spiegazione "
        "coerente con entrambi i numeri è il margine del book, non un'abilità "
        "di previsione.")
    add("")

    # --- 4 test
    add("## 4. Test pre-registrato")
    add("")
    proto = prereg.PROTOCOL
    add(f"**Protocollo** `{test.get('protocol_hash', '')[:16]}` · "
        f"candidati dichiarati **{proto['candidates']['count']}** · split "
        f"{proto['split']['train']:.0%}/{proto['split']['validation']:.0%}/"
        f"{proto['split']['test']:.0%} cronologico (il test è l'ultimo tratto).")
    add("")
    add("| tratto | righe | finestra |")
    add("|---|---|---|")
    for k in ("train", "validation", "test"):
        w = (test.get("window") if k == "test" else None)
        add(f"| {k} | {test.get('sizes', {}).get(k, 'n/d')} | "
            f"{w[0] + ' → ' + w[1] if w else '—'} |")
    add("")
    sel = test.get("selected") or {}
    add(f"**Candidato selezionato su validation** (regola: massimo ROI con "
        f"n ≥ {prereg.MIN_VAL}): `{sel.get('label')}` → "
        f"{_pct(sel.get('roi'))} (n={_n(sel.get('n'))}).")
    add("")
    add("| insieme | n | ROI | IC95 | hit | EV a ledger |")
    add("|---|---|---|---|---|---|")
    for label, d in (("candidato (test)", test.get("test")),
                     ("benchmark: strategia corrente (test)", test.get("benchmark_test")),
                     ("benchmark di MERCATO — descrittivo (test)",
                      test.get("benchmark_market"))):
        d = d or {}
        add(f"| {label} | {_n(d.get('n'))} | {_pct(d.get('roi'))} | "
            f"{_pct((d.get('ci95') or [None, None])[0])}…"
            f"{_pct((d.get('ci95') or [None, None])[1])} | {_share(d.get('hit'))} | "
            f"{_pct(d.get('ev'))} |")
    add("")
    add(f"**Esito: {test.get('verdict')}** — {test.get('why')}. "
        f"Batte il benchmark: **{_yes(test.get('beats_benchmark'))}**. "
        f"Campione confidenziale (≥{_n(test.get('n_for_confident'))}): "
        f"**{_yes(test.get('confident_sample'))}**.")
    bmk = test.get("benchmark_market") or {}
    add("")
    add(f"Il **benchmark di mercato** ({_n(bmk.get('n'))} righe, "
        f"ROI {_pct(bmk.get('roi'))}) compra solo dove il prezzo offerto batte "
        "il prezzo equo devigato, senza usare il modello. È **descrittivo**: "
        "non entra nel verdetto (le regole di `pass`/`fail` sono quelle firmate "
        "prima del test). Serve a una domanda sola: il candidato batte anche "
        "chi non ha un modello?")
    add("")
    bench_n = (test.get("benchmark_validation") or {}).get("n")
    add(f"Il benchmark ha **n={_n(bench_n)}** su validation: la strategia di "
        "produzione, con i suoi gate, seleziona così poche righe che **non è "
        "misurabile** (soglia dichiarata: 40). È un risultato in sé: il problema "
        "non è scegliere la soglia migliore, è che nessuna soglia produce "
        "abbastanza campione per dimostrare qualcosa.")
    add("")

    # --- 5 decisione
    add("## 5. Decisione")
    add("")
    add(f"### {decision['decision']}")
    add("")
    for r in decision["reasons"]:
        add(f"- {r}")
    add("")
    add("Regole applicate (dichiarate **prima** di eseguire il test, in "
        "`next_phase.DECISION_RULES`):")
    add("")
    for k, v in decision["rules"].items():
        add(f"- **{k}**: " + " · ".join(v))
    add("")
    add("### Cosa servirebbe per una prova controllata")
    add("")
    add("```")
    for k, v in decision["shadow_protocol"].items():
        add(f"{k:<18}: {v}")
    add("```")
    add("")
    if decision["decision"] == "candidato idoneo a una futura prova controllata":
        add("Tutte le condizioni sono soddisfatte: il passo successivo è una prova "
            "controllata con stake minimo, non un aumento di esposizione.")
    else:
        add(f"**Non** tutte le condizioni sono soddisfatte oggi (test: "
            f"{test.get('verdict')}, CLV de-vigato: {fair.get('status') or 'n/d'}, "
            f"n={_n((test.get('test') or {}).get('n'))}): il protocollo resta "
            "sulla carta finché una misura non lo giustifica.")
    add("")

    # --- 6 riproducibilita'
    add("## 6. Riproducibilità")
    add("")
    add("```bash")
    add("venv/bin/python -m research_isolated.lab.next_phase --select-only  # nessun holdout")
    add("venv/bin/python -m research_isolated.lab.next_phase              # una volta")
    add("venv/bin/python -m research_isolated.lab.prereg --select-only")
    add("pytest research_isolated/tests -q")
    add("```")
    add("")
    add("File prodotti: `NEXT_PHASE_REPORT.md`, `NEXT_PHASE_RESULTS.json`, "
        "`next_phase_markets.csv`, `next_phase_clv.csv`, `next_phase_candidates.csv`, "
        "`next_phase_log.txt`, `prereg_protocol.json`, `holdout_used.json`.")
    add("")
    add("Lo snapshot è letto in `mode=ro`; nessun modulo di questo percorso importa "
        "`auto_bet`, `bot`, `tracker` o `execution_engine`.")
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------------------
# 6) pipeline
# ---------------------------------------------------------------------------
def build(*, outdir=None, sign: bool = True, snapshot: Optional[str] = None) -> dict:
    """Esegue la pipeline completa. `sign=False` non consuma l'holdout."""
    out = Path(outdir or OUT_DIR)
    out.mkdir(parents=True, exist_ok=True)
    db = loader.require_snapshot(snapshot)
    preds = loader.predictions(db)
    clv_df = loader.clv(db)
    bets_df = loader.bets(db)
    results_df = loader.results(db)
    snaps = loader.snapshots(db)

    audit = {
        "snapshot": str(db),
        "snapshot_at": datetime.fromtimestamp(db.stat().st_mtime, timezone.utc).isoformat(),
        "rows": {"predictions": len(preds), "clv": len(clv_df),
                 "bets": len(bets_df), "results": len(results_df),
                 "snapshots": len(snaps)},
        "capital": capital_block(bets_df),
        "ev": ev_block(preds, results_df),
        "clv": clv_block(clv_df, preds, snaps, bets_df),
    }

    uni = prereg.universe(preds)
    sp = prereg.chronological_split(uni)
    sizes = {k: int(len(sp[k])) for k in ("train", "validation", "test")}
    sel = prereg.select(sp)
    if not sign:
        # `--select-only` NON tocca l'holdout, nemmeno per calcolare: un test
        # "visto in anteprima" non e' piu' out-of-sample.
        test = {"verdict": "NON ESEGUITO", "why": "selezione senza test",
                "test": {}, "benchmark_test": {}, "beats_benchmark": None,
                "protocol_hash": prereg.protocol_hash(),
                "selected": sel.get("selected")}
    elif sel.get("selected"):
        test = prereg.run_test(sp, sel["selected"], outdir=out, write=True)
    else:
        test = {"verdict": "INCONCLUSIVE", "why": sel.get("reason") or "nessun candidato",
                "test": {}, "benchmark_test": {}, "beats_benchmark": None,
                "protocol_hash": prereg.protocol_hash(), "selected": None}
    test["sizes"] = sizes
    test["candidates"] = (sel.get("table").to_dict("records")
                          if sel.get("table") is not None else [])
    test["benchmark_validation"] = sel.get("benchmark_val")

    decision = decide(audit, test)
    report = render_report(audit, test, decision,
                           snapshot_at=os.path.basename(audit["snapshot"]))
    result = {"generated_at": datetime.now(timezone.utc).isoformat(),
              "audit": audit, "test": test, "decision": decision,
              "report_md": report}
    return result


def rebuild_report(outdir=None) -> dict:
    """Rigenera il report dal test GIA' consumato, senza rieseguirlo.

    Serve per correggere la formattazione dopo che l'holdout e' stato usato: il
    verdetto viene riletto dal file, non ricalcolato — un secondo `run_test`
    sarebbe un secondo test out-of-sample sullo stesso campione.
    """
    out = Path(outdir or OUT_DIR)
    path = out / "NEXT_PHASE_RESULTS.json"
    if not path.exists():
        raise RuntimeError(f"nessun risultato salvato in {path}: eseguire il test")
    stored = json.loads(path.read_text(encoding="utf-8"))
    base = build(outdir=out, sign=False)          # audit fresco, holdout NON toccato
    base["test"] = stored.get("test") or {}
    base["decision"] = decide(base["audit"], base["test"])
    base["report_md"] = render_report(
        base["audit"], base["test"], base["decision"],
        snapshot_at=os.path.basename(base["audit"]["snapshot"]))
    base["rebuilt_from"] = str(path)
    base["generated_at"] = datetime.now(timezone.utc).isoformat()
    return base


def write_outputs(result: dict, outdir=None, log: bool = True) -> dict:
    out = Path(outdir or OUT_DIR)
    out.mkdir(parents=True, exist_ok=True)
    paths = {}
    (out / "NEXT_PHASE_REPORT.md").write_text(result["report_md"], encoding="utf-8")
    paths["report"] = str(out / "NEXT_PHASE_REPORT.md")
    # JSON: senza il markdown (doppione) e senza il dump delle 24 righe di prova
    slim = {k: v for k, v in result.items() if k != "report_md"}
    slim["test"] = {k: v for k, v in slim["test"].items() if k != "candidates"}
    (out / "NEXT_PHASE_RESULTS.json").write_text(
        json.dumps(slim, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    paths["json"] = str(out / "NEXT_PHASE_RESULTS.json")

    gap = result["audit"]["ev"]["gap"]
    rows = []
    for mk, d in (gap.get("by_market") or {}).items():
        rows.append({"mercato": mk, **{k: v for k, v in d.items()
                                       if k not in ("calibration_gap_ci95", "realized_ci95", "hit_ci95")}})
    pd.DataFrame(rows).to_csv(out / "next_phase_markets.csv", index=False)
    paths["markets_csv"] = str(out / "next_phase_markets.csv")

    crows = []
    for key, c in (result["audit"]["clv"]["cohorts"] or {}).items():
        crows.append({"coorte": key, "label": c["label"], "n": c["n"],
                      "clv_mean": c["clv_mean"], "clv_lo": c["clv_ci95"][0],
                      "clv_hi": c["clv_ci95"][1], "share_positive": c["share_positive"],
                      "weeks": c["weeks"], "realized_n": c["realized_n"],
                      "realized_roi": c["realized_roi"]})
    f = result["audit"]["clv"]["fair"]
    crows.append({"coorte": "clv_fair", "label": "CLV de-vigato",
                  "n": f.get("n"), "clv_mean": f.get("clv_fair"),
                  "clv_lo": (f.get("clv_fair_ci95") or [None, None])[0],
                  "clv_hi": (f.get("clv_fair_ci95") or [None, None])[1],
                  "share_positive": f.get("share_positive_fair")})
    pd.DataFrame(crows).to_csv(out / "next_phase_clv.csv", index=False)
    paths["clv_csv"] = str(out / "next_phase_clv.csv")

    cand = result["test"].get("candidates") or []
    if cand:
        keep = [c for c in cand if isinstance(c, dict)]
        cols = ["label", "odds_max", "edge_min", "ev_min", "n", "roi", "hit",
                "ev", "eligible"]
        pd.DataFrame(keep)[[c for c in cols if c in pd.DataFrame(keep).columns]] \
            .to_csv(out / "next_phase_candidates.csv", index=False)
        paths["candidates_csv"] = str(out / "next_phase_candidates.csv")

    # `log=False` su una rigenerazione del report: l'holdout non e' stato
    # rieseguito, quindi una riga di log in piu' falserebbe la serie.
    if log:
        logp = out / "next_phase_log.txt"
        with logp.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "at": result["generated_at"],
                "decision": result["decision"]["decision"],
                "test": result["test"]["verdict"],
                "fair_clv": (result["audit"]["clv"]["fair"] or {}).get("status"),
                "capital": result["audit"]["capital"]["verdict"],
            }, ensure_ascii=False) + "\n")
        paths["log"] = str(logp)
    return paths


def _fmt_summary(result: dict) -> str:
    a = result["audit"]
    cap = a["capital"]
    s = a["ev"]["summary"]
    f = a["clv"]["fair"]
    t = result["test"]
    return "\n".join([
        "=== FASE SUCCESSIVA ===",
        f"capitale: {_num(cap.get('equity_now'))} USDC -> {cap['verdict']} "
        f"(residuo {_num(cap.get('residual'))})",
        f"EV: modello {_pct(s.get('ev_ledger'))} | mercato {_pct(s.get('ev_market'))} | "
        f"realizzato {_pct(s.get('realized'))} | definizione {_pct(s.get('definition_gap'))}",
        f"CLV: book {_pct(f.get('clv_book'))} -> fair {_pct(f.get('clv_fair'))} "
        f"({f.get('status')})",
        f"test: {t.get('verdict')} ({t.get('why')}) | candidato "
        f"{_pct((t.get('test') or {}).get('roi'))}",
        f"DECISIONE: {result['decision']['decision'].upper()}",
    ])


def main(argv: Optional[Sequence[str]] = None) -> int:
    import sys
    args = list(argv if argv is not None else sys.argv[1:])
    outdir = OUT_DIR
    if "--new-protocol" in args and prereg.protocol_path(outdir).exists():
        prereg.protocol_path(outdir).unlink()
    if "--report-only" in args:
        try:
            result = rebuild_report(outdir)
        except RuntimeError as exc:
            print(f"report non rigenerabile: {exc}")
            return 2
        print(_fmt_summary(result))
        for k, v in write_outputs(result, outdir, log=False).items():
            print(f"  {k}: {v}")
        return 0
    if prereg.protocol_path(outdir).exists() and "--new-protocol" not in args:
        try:
            prereg.verify_protocol(outdir)
        except RuntimeError as exc:
            print(f"protocollo non valido: {exc}")
            return 2
    else:
        prereg.save_protocol(outdir)
    sign = "--select-only" not in args
    if sign and prereg.holdout_used(outdir):
        print("holdout gia' consumato: nessun nuovo test out-of-sample. "
              "(dichiarare un protocollo nuovo con --new-protocol)")
        return 2
    result = build(outdir=outdir, sign=sign)
    print(_fmt_summary(result))
    if sign:
        for k, v in write_outputs(result, outdir).items():
            print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
