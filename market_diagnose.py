"""market_diagnose.py — Diagnosi calibrazione per mercato (ledger previsioni).

Quando il campione GIOCABILE raggiunge ~100 previsioni chiuse (STRATEGY.md:
da quel campione il primo segnale e' affidabile), questo script confronta per
ogni mercato:

  - ROI realizzato  vs EV atteso  (gap): il modello sta perdendo dove
    prometteva di vincere?
  - hit rate vs probabilita' media del modello (prob_gap): overconfidence?

I mercati con ROI < 0 e gap sistematicamente negativo (ROI < EV di almeno
`gap_pp` punti percentuali) su un campione sufficiente sono i candidati alla
messa a punto: peso blend, soglia EV, metodo di devig.

⚠️ **La diagnosi gira SOLO sui segnali GIOCABILI** (`value_filter.PLAYABLE_TIERS`:
value / strong_value / moderate), come deciso il 22/09. Le righe `rejected` /
`no_value`/ad altro stato restano nel ledger, ma il loro P/L e' il COSTO (o il
risparmio) dei gate, non la performance di una strategia: sommarle produce un
numero che non corrisponde a nulla e puo' far raccomandare di cambiare blend o
soglie EV per colpa di candidati che non sono mai stati giocati. Restano visibili
nel report come riferimento dichiarato, fuori da ogni calcolo.

CLI:
  venv/bin/python market_diagnose.py                 # analisi sul DB locale
  venv/bin/python market_diagnose.py --json          # output JSON (report)
  venv/bin/python market_diagnose.py --min-total 50  # soglia campione totale
  venv/bin/python market_diagnose.py --all-statuses  # confronto: include gli
                                                     # scartati (NON decisionale)
  venv/bin/python market_diagnose.py --since 2026-09-19 --odds-min 1.30 \
                                     --odds-max 1.80
                                                     # SOLO l'era strategica
                                                     # corrente e la fascia
                                                     # quota corrente

⚠️ **Filtro d'ERA (25/09/2026)**: `--since` + `--odds-min/--odds-max`
restringono il campione ai segnali NATI dopo una data e con una quota in
fascia. Serve perche' lo split per stato da solo non basta: il 25/09 il report
Over/Under mostrava +21.21% su 30 chiusure, ma 22 di quelle righe erano della
pipeline ritirata (quota media ~2.25) mentre le 8 della strategia in
produzione davano -10.9%. Il filtro usa `tracker.filter_predictions` (stessa
definizione per il report shadow multi-mercato): due implementazioni diverse
della stessa "era" produrrebbero due misure che non coincidono.

Exit code: 0 = nessuna azione consigliata (anche campione insufficiente),
           1 = trovati mercati critici da mettere a punto.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Soglie di default, coerenti con STRATEGY.md:
# - MIN_TOTAL: primo segnale affidabile da ~100 previsioni chiuse;
# - MIN_PER_MARKET: sotto 10 chiusi per mercato e' solo rumore;
# - GAP_PP: rumore ammesso tra ROI realizzato ed EV atteso (3pp). E' una
#   soglia di DIAGNOSI, indipendente dal gate di edge di value_filter (che
#   dal 21/09 vale +2pp): non va allineata a quella.
# - PROB_GAP_PP: overconfidence se hit rate < prob media di almeno 5pp.
MIN_TOTAL = 100
MIN_PER_MARKET = 10
GAP_PP = 3.0
PROB_GAP_PP = 5.0

#: Etichetta degli stati fuori dai calcoli. L'insieme effettivo e' TUTTO cio'
#: che non e' in `value_filter.PLAYABLE_TIERS`: si ricava per sottrazione,
#: cosi' uno stato nuovo non puo' sparire dai conti.
_SKIPPED_STATUSES = ("rejected", "no_value")

LABELS = {
    "1X2": "1X2",
    "OU": "Over/Under",
    "AH": "Asian Handicap",
    "BTTS": "BTTS (Gol/Niente Gol)",
}


def _label(key: str) -> str:
    return LABELS.get(key, key)


def _filtro_block(filtro: Optional[Dict]) -> Dict:
    """Normalizza il filtro d'era/fascia per il report (mai dedotto a valle)."""
    f = dict(filtro or {})
    f["applied"] = (bool(f.get("since")) or f.get("odds_min") is not None
                    or f.get("odds_max") is not None)
    return f


def diagnose(by_mkt: Dict[str, Dict], skipped: Optional[Dict[str, Dict]] = None,
             min_total: int = MIN_TOTAL,
             min_per_market: int = MIN_PER_MARKET, gap_pp: float = GAP_PP,
             prob_gap_pp: float = PROB_GAP_PP,
             filtro: Optional[Dict] = None) -> Dict:
    """Analizza predictions_summary() per mercato e ritorna la diagnosi.

    `by_mkt`: {mercato: {n, won, lost, push, hit_rate, roi, avg_ev, gap,
    avg_prob, avg_market_edge}} — l'output di tracker.predictions_summary()
    FILTRATO sui soli stati giocabili.

    `skipped` (facoltativo): le stesse voci per le righe ESCLUSE dai calcoli
    (rejected / no_value / altro). Non entra in nessun giudizio: serve solo a
    dichiarare quanto materiale e' stato lasciato fuori.

    Ritorna un dict con: totals (campione giocabile), markets (per mercato,
    ordinati per volume), excluded (righe fuori dai calcoli), sufficiente
    (campione giocabile >= min_total), critici (mercati da mettere a punto,
    ordinati per ROI crescente), azioni (idem ma SOLO se il campione e'
    sufficiente) e note.
    """
    totals = {"n": 0, "won": 0, "lost": 0, "push": 0, "pnl": 0.0,
              "ev_sum": 0.0, "roi": 0.0, "avg_ev": 0.0, "gap": 0.0}
    markets: List[Dict] = []

    for key, b in by_mkt.items():
        n = int(b.get("n", 0) or 0)
        if n <= 0:
            continue
        won = int(b.get("won", 0) or 0)
        lost = int(b.get("lost", 0) or 0)
        push = int(b.get("push", 0) or 0)
        pnl = float(n * (b.get("roi", 0) or 0) / 100.0)
        roi = float(b.get("roi", 0) or 0)
        avg_ev = float(b.get("avg_ev", 0) or 0)
        gap = float(b.get("gap", 0) or 0)
        hit_rate = float(b.get("hit_rate", 0) or 0)
        avg_prob = float(b.get("avg_prob", 0) or 0)
        prob_gap = hit_rate - avg_prob * 100.0
        edge = b.get("avg_market_edge")
        edge_v = float(edge) if edge is not None else None

        segnali: List[str] = []
        if roi < 0 and gap <= -gap_pp:
            segnali.append(f"ROI {roi:+.1f}% < EV {avg_ev:+.1f}% di "
                           f"{abs(gap):.1f}pp (soglia {gap_pp:g}pp)")
        if prob_gap <= -prob_gap_pp:
            segnali.append(f"hit rate {hit_rate:.1f}% molto sotto la prob."
                           f" media del modello {avg_prob*100:.1f}% "
                           f"(overconfidence, -{abs(prob_gap):.1f}pp)")

        critico = (
            n >= min_per_market
            and roi < 0
            and gap <= -gap_pp
        )

        markets.append({
            "mercato": str(key), "label": _label(str(key)),
            "n": n, "won": won, "lost": lost, "push": push,
            "hit_rate": round(hit_rate, 2), "roi": round(roi, 2),
            "avg_ev": round(avg_ev, 2), "gap": round(gap, 2),
            "prob_gap": round(prob_gap, 2),
            "avg_market_edge": round(edge_v, 2) if edge_v is not None else None,
            "critico": critico, "segnali": segnali,
        })

        totals["n"] += n
        totals["won"] += won
        totals["lost"] += lost
        totals["push"] += push
        totals["pnl"] += pnl
        totals["ev_sum"] += avg_ev * n / 100.0

    if totals["n"]:
        totals["roi"] = round(totals["pnl"] / totals["n"] * 100.0, 2)
        totals["avg_ev"] = round(totals["ev_sum"] / totals["n"] * 100.0, 2)
        totals["gap"] = round(totals["roi"] - totals["avg_ev"], 2)

    markets.sort(key=lambda m: (-m["n"], m["mercato"]))
    critici = [m for m in markets if m["critico"]]
    critici.sort(key=lambda m: (m["roi"], m["mercato"]))

    sufficiente = totals["n"] >= min_total
    azioni = [_azioni_market(m) for m in critici] if sufficiente else []

    # Righe fuori dai calcoli (rejected/no_value/altro): dichiarate, mai usate
    # per giudicare il modello.
    excluded = _excluded_block(skipped or {})

    note = ""
    if not sufficiente:
        note = (f"campione GIOCABILE {totals['n']} < {min_total}: per STRATEGY.md "
                f"il primo segnale affidabile arriva a ~100 previsioni chiuse "
                f"(ROI vs EV pienamente leggibile da 500-1000). Nessuna azione "
                f"consigliata per ora.")
        if excluded["n"]:
            note += (f" Al totale concorrono {excluded['n']} righe NON giocabili "
                     f"(escluse): senza lo split il campione sembrerebbe piu' "
                     f"mature di quanto sia.")
    elif not critici:
        note = "nessun mercato sotto la soglia di intervento."

    return {
        "totals": totals,
        "markets": markets,
        "excluded": excluded,
        "sufficiente": sufficiente,
        "critici": critici,
        "azioni": azioni,
        "note": note,
        "filtro": _filtro_block(filtro),
        "parametri": {"min_total": min_total, "min_per_market": min_per_market,
                      "gap_pp": gap_pp, "prob_gap_pp": prob_gap_pp},
    }


def _excluded_block(skipped: Dict[str, Dict]) -> Dict:
    """Aggrega le righe NON giocabili in un blocco di solo riferimento.

    Non produce segnali, critici o azioni: e' il materiale che il gate ha
    tagliato (o che non ha raggiunto il tier minimo), tenuto visibile perche'
    un escluso non e' un dato che si puo' far sparire. Include il SEGNO del
    P/L: un gate che taglia perdite e' un gate che funziona.
    """
    n = won = lost = push = 0
    pnl = 0.0
    by_market: List[Dict] = []
    for key, b in skipped.items():
        if not isinstance(b, dict):
            continue
        n_i = int(b.get("n", 0) or 0)
        if n_i <= 0:
            continue
        roi_i = float(b.get("roi", 0) or 0)
        won_i = int(b.get("won", 0) or 0)
        lost_i = int(b.get("lost", 0) or 0)
        push_i = int(b.get("push", 0) or 0)
        pnl_i = n_i * roi_i / 100.0
        n += n_i; won += won_i; lost += lost_i; push += push_i
        pnl += pnl_i
        by_market.append({
            "mercato": str(key), "label": _label(str(key)), "n": n_i,
            "won": won_i, "lost": lost_i, "push": push_i,
            "roi": round(roi_i, 2),
        })
    by_market.sort(key=lambda m: (-m["n"], m["mercato"]))
    return {
        "n": n, "won": won, "lost": lost, "push": push,
        "pnl": round(pnl, 2),
        "roi": round(pnl / n * 100.0, 2) if n else 0.0,
        "by_market": by_market,
        "statuses": list(_SKIPPED_STATUSES),
    }


def _azioni_market(m: Dict) -> Dict:
    """Suggerimenti di messa a punto per un mercato critico."""
    azioni = [
        f"ridurre il peso del modello nel blend (market_calib.blend_"
        f"probability) per {m['label']} — EB realizzato sotto l'EV atteso",
        "alzare la soglia EV minima per questo mercato nel value_filter",
    ]
    if m.get("avg_market_edge") is not None and m["avg_market_edge"] < 3.0:
        azioni.append("provare un metodo di devig piu' aggressivo "
                      "(power/shin) o un line shopping piu' restrittivo: "
                      f"edge medio sul mercato +{m['avg_market_edge']:.1f}pp")
    return {
        "mercato": m["mercato"], "label": m["label"], "n": m["n"],
        "roi": m["roi"], "avg_ev": m["avg_ev"], "gap": m["gap"],
        "segnali": m["segnali"], "azioni_da_fare": azioni,
    }


def _subtract(everything: Dict[str, Dict], playable: Dict[str, Dict]) -> Dict[str, Dict]:
    """Residuo per mercato: ledger totale MENO righe giocabili.

    Derivato dalle sole voci pubbliche di `predictions_summary` (n, won/lost/
    push, roi): il blocco esclusi serve a DICHIARARE quanto materiale resta
    fuori, non ad alimentare un giudizio, quindi non gli servono hit rate del
    modello ne' edge medi. Fare la sottrazione qui (invece di contare per
    stato) garantisce che gli esclusi siano ESATTAMENTE il complemento dei
    giocabili: nessuno stato nuovo puo' sparire dai conti.
    """
    out: Dict[str, Dict] = {}
    for key, t in (everything or {}).items():
        if not isinstance(t, dict):
            continue
        p = (playable or {}).get(key) or {}
        n_t = int(t.get("n", 0) or 0)
        n_p = int(p.get("n", 0) or 0)
        n = n_t - n_p
        if n <= 0:
            continue
        pnl = (n_t * float(t.get("roi", 0) or 0) - n_p * float(p.get("roi", 0) or 0)) / 100.0
        out[key] = {
            "n": n, "won": int(t.get("won", 0) or 0) - int(p.get("won", 0) or 0),
            "lost": int(t.get("lost", 0) or 0) - int(p.get("lost", 0) or 0),
            "push": int(t.get("push", 0) or 0) - int(p.get("push", 0) or 0),
            "roi": round(pnl / n * 100.0, 2),
        }
    return out


def _attach_significance(res: Dict, *, statuses, since, odds_min, odds_max,
                         all_statuses: bool = False) -> Dict:
    """Aggiunge il blocco di significativita' statistica alla diagnosi.

    Un ROI su 8 chiusure non e' una misura: qui si aggiunge, per il totale e
    per ogni mercato, se il risultato e' DISTINGUIBILE da zero e l'edge minimo
    rilevabile col campione attuale. E' un di piu' dichiarato: nessun giudizio
    o soglia di questa diagnosi cambia (un campione sotto la soglia resta
    `insufficient` anche se il p-value fosse piccolo).

    Fail-safe: se la lettura o il calcolo falliscono, il campo e' `unavailable`
    con il motivo — mai un'eccezione che toglie la diagnosi.
    """
    try:
        import significance
        sig = significance.from_ledger(statuses=statuses, since=since,
                                       odds_min=odds_min, odds_max=odds_max,
                                       by_market=True,
                                       all_statuses=all_statuses)
    except Exception as exc:
        logger.debug("market_diagnose: significativita' non calcolata: %s", exc)
        sig = {"status": "unavailable", "error": str(exc), "by_market": {}}
    res["significance"] = sig
    by_mkt = sig.get("by_market") or {}
    for m in res.get("markets") or []:
        if isinstance(m, dict):
            m["significance"] = by_mkt.get(str(m.get("mercato")))
    return res


def _significance_lines(block) -> List[str]:
    """Righe di significativita' per il report (vuote se non c'e' nulla)."""
    try:
        import significance
        return significance.format_lines(block, indent="   ")
    except Exception:
        return []


def analyze_db(all_statuses: bool = False, *, since=None, odds_min=None,
               odds_max=None, **kwargs) -> Dict:
    """Diagnosi sul DB reale: legge predictions_summary() da tracker.

    Default (22/09): il giudizio usa SOLO i segnali giocabili
    (`value_filter.PLAYABLE_TIERS`). Le righe degli altri stati finiscono nel
    blocco `excluded`, dichiarato ma mai usato per raccomandare cambi di blend,
    devig o soglie EV.

    `all_statuses=True` ripristina il comportamento pre-22/09 (tutto il
    ledger): e' un CONFRONTO, non una modalita' decisionale.

    `since` + `odds_min`/`odds_max` (25/09/2026): restringono il campione
    all'ERA strategica e alla FASCIA QUOTA correnti. Il filtro viene passato a
    ENTRAMBE le letture (giocabili e totale): se il blocco `excluded` fosse
    calcolato su una popolazione diversa da quella giudicata, il residuo non
    sarebbe piu' il complemento esatto dei giocabili.
    """
    from tracker import predictions_summary
    extra = {"created_since": since, "odds_min": odds_min,
             "odds_max": odds_max}
    filtro = {"since": since, "odds_min": odds_min, "odds_max": odds_max}
    if all_statuses:
        res = diagnose(predictions_summary(**extra), filtro=filtro, **kwargs)
        return _attach_significance(res, statuses=None, since=since,
                                    odds_min=odds_min, odds_max=odds_max,
                                    all_statuses=True)
    from value_filter import PLAYABLE_TIERS
    playable = predictions_summary(statuses=PLAYABLE_TIERS, **extra)
    res = diagnose(playable,
                   skipped=_subtract(predictions_summary(**extra), playable),
                   filtro=filtro, **kwargs)
    return _attach_significance(res, statuses=list(PLAYABLE_TIERS), since=since,
                               odds_min=odds_min, odds_max=odds_max)


def _fmt_pct(v: Optional[float], digits: int = 1) -> str:
    if v is None:
        return "n.d."
    return f"{v:+.{digits}f}"


def _table(markets: List[Dict]) -> str:
    head = (f"{'Mercato':<14} {'n':>5} {'V/P':>7} {'hit%':>6} {'ROI%':>7} "
            f"{'EV%':>7} {'gap%':>7} {'probGap':>8} {'edge%':>7}")
    lines = [head, "-" * len(head)]
    for m in markets:
        vp = f"{m['won']}/{m['lost']}"
        edge = _fmt_pct(m["avg_market_edge"])
        mark = " ⚠️" if m["critico"] else ""
        lines.append(
            f"{m['label']:<14} {m['n']:>5} {vp:>7} "
            f"{m['hit_rate']:>6.1f} {_fmt_pct(m['roi'], 2):>7} "
            f"{_fmt_pct(m['avg_ev'], 2):>7} {_fmt_pct(m['gap'], 2):>7} "
            f"{_fmt_pct(m['prob_gap']):>8} {edge:>7}{mark}")
    return "\n".join(lines)


def _report(res: Dict, all_statuses: bool = False) -> str:
    """Report leggibile. `all_statuses` cambia SOLO le etichette: in modalita'
    confronto il campione include anche cio' che i gate hanno scartato, e
    chiamarlo "giocabile" sarebbe una bugia che l'operatore leggerebbe male."""
    t = res["totals"]
    ex = res.get("excluded") or {}
    if all_statuses:
        out = ["🔬 Diagnosi calibrazione per mercato — TUTTO il ledger "
               "(CONFRONTO, non decisionale)"]
        label = "Campione MESCOLATO"
    else:
        out = ["🔬 Diagnosi calibrazione per mercato — SOLO segnali giocabili"]
        label = "Campione giocabile"
    filtro = res.get("filtro") or {}
    if filtro.get("applied"):
        bits = []
        if filtro.get("since"):
            bits.append(f"era dal {filtro['since']}")
        lo, hi = filtro.get("odds_min"), filtro.get("odds_max")
        if lo is not None or hi is not None:
            bits.append(f"quota {lo if lo is not None else '-'}-"
                        f"{hi if hi is not None else '-'}")
        out.append(f"Filtro: {' | '.join(bits)}")
    else:
        out.append("Filtro: NESSUNO — mescola ere/strategie diverse "
                   "(usare --since, es. --since 2026-09-19)")
    out.append(
        f"{label}: {t['n']} chiusi (V {t['won']} / P {t['lost']} / "
        f"Push {t['push']}) | ROI {_fmt_pct(t['roi'], 2)} | EV atteso "
        f"{_fmt_pct(t['avg_ev'], 2)} | gap {_fmt_pct(t['gap'], 2)}")
    # Significativita' statistica (28/09/2026): il ROI da solo, su questo
    # campione, non dice se il risultato e' distinguibile da zero.
    out.extend(_significance_lines(res.get("significance")))
    if ex.get("n"):
        # Dichiarato, mai sommato: e' cio' che i gate hanno tagliato.
        out.append(
            f"Fuori dai calcoli: {ex['n']} righe non giocabili "
            f"({'/'.join(ex.get('statuses') or [])}) | ROI "
            f"{_fmt_pct(ex['roi'], 2)} → costo dei gate, NON performance "
            f"(il segno del P/L dice se i filtri stanno risparmiando)")
    if not res["markets"]:
        out.append("ℹ️  Nessun segnale GIOCABILE chiuso nel ledger: niente da "
                   "analizzare.")
        return "\n".join(out)
    out.append("")
    out.append(_table(res["markets"]))
    # Dettaglio per mercato: solo dove c'e' un blocco calcolabile (il filtro
    # d'era/fascia, se attivo, e' gia' dichiarato in testa al report).
    if any(m.get("significance") for m in res["markets"]):
        out.append("")
        out.append("🧮 Significativita' del campione (scipy):")
        for m in res["markets"]:
            lines = _significance_lines(m.get("significance"))
            if lines:
                out.append(f"   {m.get('label') or m.get('mercato')}")
                out.extend(lines)
    out.append("")
    if not res["sufficiente"]:
        out.append(f"ℹ️  {res['note']}")
    elif not res["azioni"]:
        out.append("✅ Nessun mercato sotto la soglia di intervento: "
                   "il ROI e' coerente (o migliore) dell'EV atteso.")
    else:
        out.append(f"❌ {len(res['azioni'])} mercato/i da mettere a punto:")
        for a in res["azioni"]:
            out.append(f"   • {a['label']} (n={a['n']}, ROI {a['roi']:+.2f}% "
                       f"vs EV {a['avg_ev']:+.2f}%, gap {a['gap']:+.2f}pp)")
            for s in a["segnali"]:
                out.append(f"     - {s}")
            for x in a["azioni_da_fare"]:
                out.append(f"     → {x}")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Diagnosi calibrazione per mercato (ledger previsioni)")
    ap.add_argument("--json", action="store_true",
                    help="output JSON (per report automatici)")
    ap.add_argument("--min-total", type=int, default=MIN_TOTAL,
                    help=f"soglia campione totale (default {MIN_TOTAL})")
    ap.add_argument("--min-per-market", type=int, default=MIN_PER_MARKET,
                    help=f"soglia chiusi per mercato (default {MIN_PER_MARKET})")
    ap.add_argument("--gap", type=float, default=GAP_PP,
                    help=f"soglia gap ROI-EV in pp (default {GAP_PP:g})")
    ap.add_argument("--prob-gap", type=float, default=PROB_GAP_PP,
                    help=f"soglia overconfidence in pp (default {PROB_GAP_PP:g})")
    ap.add_argument("--all-statuses", action="store_true",
                    help="include TUTTO il ledger (anche rejected/no_value): "
                         "e' un confronto con il comportamento pre-22/09, "
                         "NON una modalita' decisionale")
    ap.add_argument("--since", default=None, metavar="YYYY-MM-DD",
                    help="solo segnali NATI da questa data (era strategica, "
                         "es. 2026-09-19)")
    ap.add_argument("--odds-min", type=float, default=None,
                    help="quota minima (fascia corrente: 1.30)")
    ap.add_argument("--odds-max", type=float, default=None,
                    help="quota massima (fascia corrente: 1.80)")
    args = ap.parse_args(argv)

    res = analyze_db(all_statuses=args.all_statuses,
                     since=args.since, odds_min=args.odds_min,
                     odds_max=args.odds_max,
                     min_total=args.min_total,
                     min_per_market=args.min_per_market,
                     gap_pp=args.gap, prob_gap_pp=args.prob_gap)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        if args.all_statuses:
            print("⚠️  --all-statuses: campione mescolato (confronto, non "
                  "decisionale) — i suggerimenti non vanno applicati.\n")
        print(_report(res, all_statuses=args.all_statuses))
    return 1 if (res["sufficiente"] and res["azioni"]) else 0


if __name__ == "__main__":
    sys.exit(main())