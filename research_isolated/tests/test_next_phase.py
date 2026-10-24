"""Test della fase successiva (tutti OFFLINE, nessuno snapshot di produzione).

Difendono le regole che rendono il documento difendibile:

* il capitale si riconosce come *movimento* dal ledger, e un saldo letto prima
  di un settlement viene marcato storico;
* la scomposizione del divario somma (definizione + edge + residuo = totale);
* le coorti CLV portano con se' il realizzato e il numero di righe non
  abbinabili (un CLV positivo con P/L negativo deve restare visibile);
* la decisione segue le regole dichiarate, non l'umore del risultato.
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from research_isolated.lab import next_phase as npz


# ---------------------------------------------------------------------------
# dati sintetici
# ---------------------------------------------------------------------------
def bets_frame(*, anchor_after=True):
    """Due puntate live: una chiusa prima dell'ancora, una dopo; una aperta."""
    return pd.DataFrame([
        # chiusa PRIMA dell'ancora (fuori dalla finestra di riconciliazione)
        {"id": 1, "match_id": "m1", "mercato": "1X2", "esito": "1", "price": 2.0,
         "stake": 2.0, "mode": "live", "esito_finale": "won", "profit": 2.0,
         "created_at": "2026-10-17T10:00:00+00:00",
         "settled_at": "2026-10-17T12:00:00+00:00"},
        # chiusa DOPO l'ancora
        {"id": 2, "match_id": "m2", "mercato": "OU", "esito": "Over 2.5",
         "price": 1.5, "stake": 4.0, "mode": "live", "esito_finale": "lost",
         "profit": -4.0, "created_at": "2026-10-18T05:00:00+00:00",
         "settled_at": "2026-10-18T07:00:00+00:00"},
        {"id": 3, "match_id": "m3", "mercato": "1X2", "esito": "2", "price": 1.8,
         "stake": 2.0, "mode": "live", "esito_finale": None, "profit": None,
         "created_at": "2026-10-19T10:00:00+00:00", "settled_at": None},
    ])


def preds_frame(n=120):
    rows = []
    for i in range(n):
        won = i % 3 == 0
        rows.append({
            "id": i, "match_id": f"m{i}", "mercato": "1X2" if i % 2 else "OU",
            "esito": "1" if i % 2 else "Over 2.5", "quota": 1.7, "prob": 0.62,
            # EV coerente con `prob` e `quota` (0.62*1.7-1): la riga del ledger
            # e' riproducibile, ed e' la condizione che il test dell'identita'
            # deve trovare soddisfatta.
            "ev": 0.054, "market_prob": 0.60, "market_edge": 0.02,
            "status": "value", "profit": (0.7 if won else -1.0),
            "esito_finale": ("won" if won else "lost"),
            "league": "Test", "created_at": f"2026-10-{1 + i // 20:02d}T10:00:00+00:00",
            "settled_at": f"2026-10-{1 + i // 20:02d}T12:00:00+00:00",
        })
    return pd.DataFrame(rows)


def clv_frame(n=40, echo_every=3):
    rows = []
    for i in range(n):
        echo = (i % echo_every == 0)
        rows.append({
            "match_id": f"m{i}", "esito": "1", "signal_quota": 1.80,
            "closing_quota": (1.80 if echo else 1.70),
            "pinnacle_quota": None,
            # `closing_odds` (Pinnacle T-0) ha PRECEDENZA su `closing_quota`: se
            # le due colonne si contraddicono la fixture non descrive un caso
            # reale (prima l'eco veniva contata 5 volte invece di 10).
            "closing_odds": ((1.80 if echo else 1.70) if i % 2 else None),
            "closing_source": "pinnacle_t0" if i % 2 else None,
            "betfair_odds": None,
            "updated_at": f"2026-10-{12 + i % 4:02d}T18:00:00+00:00",
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 1) capitale
# ---------------------------------------------------------------------------
class TestCapitale:
    def test_riconcilia_i_movimenti(self):
        # ancora 100 con 4 in gioco; dopo: -4 persa (ritorno 0), +? => atteso
        b = bets_frame()
        rec = npz.ev_audit.capital_reconciliation(
            b, declared_value=100.0, declared_at="2026-10-20T00:00:00+00:00",
            anchor={"equity": 100.0, "at": "2026-10-18T04:42:00+00:00"})
        assert rec["verified"] is True
        assert rec["window_settlements"] == 1
        # L'equity e' la grandezza conservata: 100 - 4 (perdita, nessun rientro).
        assert rec["expected_equity"] == pytest.approx(100.0 - 4.0)
        # Il saldo dichiarato e' il DISPONIBILE: sconta l'escrow aperto (2.0),
        # che non e' cassa spendibile. Confonderli e' l'errore del 21/09.
        assert rec["exposure_at_declared"] == pytest.approx(2.0)
        assert rec["expected_value"] == pytest.approx(100.0 - 4.0 - 2.0)

    def test_saldo_storico_marcato(self):
        rec = npz.capital_block(bets_frame())
        # il declared e' 20/10 03:25, l'ultimo settlement e' 18/10: non stantio
        assert rec["stale"] is False
        b = bets_frame()
        b.loc[2, "esito_finale"] = "won"
        b.loc[2, "profit"] = 1.0
        b.loc[2, "settled_at"] = "2026-10-21T10:00:00+00:00"
        rec2 = npz.capital_block(b)
        assert rec2["stale"] is True
        assert rec2["verdict"].startswith("SALDO STORICO")

    def test_equity_include_l_escrow(self):
        rec = npz.capital_block(bets_frame())
        assert rec["ledger"]["open_bets"] == 1
        assert rec["ledger"]["open_stake"] == pytest.approx(2.0)
        assert rec["equity_now"] == pytest.approx(npz.DECLARED["value"] + 2.0)

    def test_residuo_fuori_tolleranza_non_e_riconciliato(self):
        b = bets_frame()
        rec = npz.ev_audit.capital_reconciliation(
            b, declared_value=50.0, declared_at="2026-10-20T00:00:00+00:00",
            anchor={"equity": 100.0, "at": "2026-10-18T04:42:00+00:00"})
        assert rec["within_tolerance"] is False
        assert any("residuo" in n for n in rec["notes"])

    def test_senza_ancora_non_inventa(self):
        rec = npz.ev_audit.capital_reconciliation(
            bets_frame(), declared_value=21.1, declared_at="2026-10-20T03:25:00+00:00")
        assert rec["verified"] is False
        rec2 = npz.capital_block(bets_frame())  # ancora dichiarata nel modulo
        assert rec2["verified"] is True


# ---------------------------------------------------------------------------
# 2) EV atteso vs realizzato
# ---------------------------------------------------------------------------
class TestEv:
    def test_la_scomposizione_somma(self):
        ev = npz.ev_block(preds_frame())
        s = ev["summary"]
        tot = (s["definition_gap"] + s["edge_claimed"] + s["residual"])
        assert tot == pytest.approx(s["gap_total"], abs=1e-9)
        assert s["ev_ledger"] - s["realized"] == pytest.approx(s["gap_total"], abs=1e-9)

    def test_identita_dell_ev(self):
        """Dove prob ed EV vengono dalla stessa probabilita' la coerenza e'
        piena: e' il caso che rende affidabile la riga di ledger."""
        ev = npz.ev_block(preds_frame())
        ident = ev["identity"]
        assert ident["n"] > 0
        assert ident["consistent_share"] == pytest.approx(1.0)
        assert ident["mean_abs_delta"] < 1e-9

    def test_identita_segnala_la_divergenza(self):
        df = preds_frame()
        df["ev"] = df["ev"] + 0.10          # EV non piu' derivabile da prob
        ev = npz.ev_block(df)
        assert ev["identity"]["consistent_share"] == 0.0
        assert ev["identity"]["mean_abs_delta"] == pytest.approx(0.10, abs=1e-9)

    def test_chi_predice_i_risultati(self):
        df = preds_frame()
        df["market_prob"] = 1.0 / df["quota"]      # mercato fair = nessun edge
        ev = npz.ev_block(df)
        wp = ev["who_predicts"]
        assert wp["distance_market"] is not None
        assert abs(wp["distance_market"]) < abs(wp["distance_model"])

    def test_popolazione_dichiara_le_scadenze(self):
        df = preds_frame()
        # una riga chiusa come push, senza risultato e a 5 giorni => scadenza
        extra = df.iloc[[0]].copy()
        extra["id"] = 999
        extra["match_id"] = "orphan"
        extra["esito_finale"] = "push"
        extra["profit"] = 0.0
        extra["created_at"] = "2026-10-10T10:00:00+00:00"
        extra["settled_at"] = "2026-10-16T10:00:00+00:00"
        pop = npz.ev_block(pd.concat([df, extra], ignore_index=True))["population"]
        assert pop["expiry_n"] == 1
        assert pop["traded"]["n"] == pop["all_closed"]["n"] - 1
        assert pop["expiry_drag"] > 0

    def test_vuoto_non_esplode(self):
        ev = npz.ev_block(pd.DataFrame({"match_id": [], "esito_finale": [],
                                        "profit": [], "quota": [], "ev": [],
                                        "market_edge": [], "created_at": [],
                                        "mercato": [], "id": []}))
        assert ev["n_closed_playable"] == 0


# ---------------------------------------------------------------------------
# 3) CLV
# ---------------------------------------------------------------------------
class TestClv:
    def test_echo_esclusi_dalla_coorte(self):
        blk = npz.clv_block(clv_frame(30, echo_every=3), preds_frame())
        assert blk["aggregate"]["echo_n"] == 10
        assert blk["cohorts"]["non_echo"]["n"] == 20

    def test_clv_negativo_quando_il_segnale_e_peggiore(self):
        c = clv_frame(10, echo_every=99)
        c["signal_quota"] = 1.60          # abbiamo preso peggio della chiusura
        blk = npz.clv_block(c, preds_frame())
        assert blk["cohorts"]["non_echo"]["clv_mean"] < 0

    def test_coorte_porta_il_realizzato(self):
        blk = npz.clv_block(clv_frame(24, echo_every=3), preds_frame())
        coh = blk["cohorts"]["non_echo"]
        assert coh["realized_n"] > 0
        assert -1.0 <= coh["realized_roi"] <= 5.0
        assert coh["unmatched"] >= 0

    def test_righe_non_abbinate_sono_contate(self):
        c = clv_frame(12, echo_every=99)
        c["match_id"] = [f"zz{i}" for i in range(len(c))]
        blk = npz.clv_block(c, preds_frame())
        coh = blk["cohorts"]["non_echo"]
        assert coh["unmatched"] == coh["n"]
        assert coh["realized_roi"] is None

    def test_de_vig_toglie_il_margine(self):
        """Con tre esiti completi il CLV contro la quota grezza e' maggiore di
        quello de-vigato: se non lo fosse il de-vig non starebbe facendo nulla."""
        snap = []
        for i in range(10):
            for esito, price in (("1", 1.80), ("X", 3.60), ("Y", 4.50)):
                snap.append({"match_id": f"m{i}", "esito": esito, "price": price,
                             "bookmaker": "pinnacle",
                             "recorded_at": "2026-10-15T18:00:00+00:00"})
        c = clv_frame(10, echo_every=99)
        fair = npz.ev_audit.fair_clv(c, pd.DataFrame(snap))
        assert fair["n"] > 0
        assert fair["margin_mean"] > 0
        assert fair["clv_fair"] < fair["clv_book"]

    def test_senza_snapshot_dichiara_non_verificabile(self):
        blk = npz.clv_block(clv_frame(5), preds_frame(), None)
        assert blk["fair"]["status"] == "NON VERIFICABILE"


# ---------------------------------------------------------------------------
# 4) decisione (regole dichiarate)
# ---------------------------------------------------------------------------
class TestDecisione:
    def _audit(self, fair_status="SOPRAVVIVE AL DE-VIG"):
        return {"clv": {"fair": {"status": fair_status}}}

    def _test(self, verdict="PASS", n=300, beats=True):
        return {"verdict": verdict, "why": "x", "beats_benchmark": beats,
                "test": {"n": n, "roi": 0.03}}

    def test_holdout_non_passato(self):
        d = npz.decide(self._audit(), self._test(verdict="INCONCLUSIVE"))
        assert d["decision"] == "nessun vantaggio dimostrato"

    def test_passa_ma_clv_non_sopravvive(self):
        d = npz.decide(self._audit("NON DISTINGUIBILE DA ZERO DOPO IL DE-VIG"),
                       self._test(n=120))
        assert d["decision"] == "segnale promettente da verificare in shadow"

    def test_passa_campione_grande_ma_clv_non_sopravvive(self):
        d = npz.decide(self._audit("NON DISTINGUIBILE DA ZERO DOPO IL DE-VIG"),
                       self._test(n=500))
        assert d["decision"] == "nessun vantaggio dimostrato"

    def test_non_batte_il_benchmark(self):
        d = npz.decide(self._audit(), self._test(beats=False))
        assert d["decision"] == "nessun vantaggio dimostrato"

    def test_tutto_a_posto_ma_campione_corto(self):
        d = npz.decide(self._audit(), self._test(n=150))
        assert d["decision"] == "segnale promettente da verificare in shadow"

    def test_tutto_a_posto(self):
        d = npz.decide(self._audit(), self._test(n=300))
        assert d["decision"] == "candidato idoneo a una futura prova controllata"

    def test_le_regole_sono_dichiarate(self):
        d = npz.decide(self._audit(), self._test())
        assert set(d["rules"]) == {"nessun_vantaggio", "shadow", "candidato"}
        assert "cb2" in d["shadow_protocol"]
        assert d["shadow_protocol"]["order_size_usdc"] == 1.00


# ---------------------------------------------------------------------------
# 5) report e artefatti
# ---------------------------------------------------------------------------
def _audit():
    return {"snapshot": "x.db",
            "capital": npz.capital_block(bets_frame()),
            "ev": npz.ev_block(preds_frame()),
            "clv": npz.clv_block(clv_frame(30, echo_every=3), preds_frame()),
            "rows": {"predictions": 120}}


def _test():
    return {"verdict": "INCONCLUSIVE", "why": "IC95 contiene lo zero",
            "protocol_hash": "a" * 64, "selected": {"label": "x", "roi": -0.04, "n": 90},
            "test": {"n": 90, "roi": -0.04, "ci95": [-0.2, 0.1], "hit": 0.4, "ev": 0.05},
            "benchmark_test": {"n": 12, "roi": 0.02, "ci95": [-0.3, 0.4], "hit": 0.5, "ev": 0.03},
            "beats_benchmark": False, "n_for_confident": 200,
            "confident_sample": False, "window": ["2026-10-19", "2026-10-20"],
            "sizes": {"train": 10, "validation": 20, "test": 30},
            "candidates": [{"label": "x", "odds_max": 1.8, "edge_min": 0.01,
                            "ev_min": 0.015, "n": 90, "roi": -0.04, "hit": 0.4,
                            "ev": 0.05, "eligible": True}],
            "benchmark_validation": {"n": 10}}


class TestReport:
    def test_sezioni_presenti(self):
        a, t = _audit(), _test()
        md = npz.render_report(a, t, npz.decide(a, t), snapshot_at="x.db")
        for needle in ("## 1. Riconciliazione del capitale",
                       "## 2. Perché l'EV atteso",
                       "## 3. Il CLV positivo",
                       "## 4. Test pre-registrato",
                       "## 5. Decisione",
                       "## 6. Riproducibilità",
                       "questo NON dimostra",
                       "stessa venue"):
            assert needle in md
        assert "NESSUN VANTAGGIO DIMOSTRATO" in md

    def test_nessuna_riga_di_troppi_spazi(self):
        a, t = _audit(), _test()
        md = npz.render_report(a, t, npz.decide(a, t))
        assert "nan" not in md.lower().replace("finanziaria", "")
        assert "None" not in md

    def test_scrive_gli_artefatti(self, tmp_path):
        a, t = _audit(), _test()
        res = {"generated_at": "2026-10-20T10:00:00+00:00", "audit": a,
               "test": t, "decision": npz.decide(a, t),
               "report_md": npz.render_report(a, t, npz.decide(a, t))}
        paths = npz.write_outputs(res, tmp_path)
        for key in ("report", "json", "markets_csv", "clv_csv", "log"):
            assert key in paths
            assert (tmp_path / paths[key].split("/")[-1]).exists()
        assert (tmp_path / "next_phase_candidates.csv").exists()
        doc = json.loads((tmp_path / "NEXT_PHASE_RESULTS.json").read_text())
        assert doc["decision"]["decision"]
        assert "report_md" not in doc
        assert "candidates" not in doc["test"]
        # il log e' append-only e leggibile
        line = (tmp_path / "next_phase_log.txt").read_text().strip()
        assert json.loads(line)["test"] == "INCONCLUSIVE"

    def test_sintesi_leggibile(self):
        a, t = _audit(), _test()
        out = npz._fmt_summary({"audit": a, "test": t,
                                "decision": {"decision": "nessun vantaggio dimostrato"}})
        assert "capitale" in out and "DECISIONE" in out


# ---------------------------------------------------------------------------
# 6) formattazione: segni, quote e plurali
# ---------------------------------------------------------------------------
class TestFormattazione:
    """Un report che stampa "+1.89% sotto il mercato" o "+63.5% positivi" fa
    leggere un rendimento dove c'e' un verso o una proporzione. Questi test
    fissano la differenza fra un MODULO e un DELTA, e fra una QUOTA e un
    RENDIMENTO."""

    def test_modulo_senza_segno(self):
        assert npz._mag(-0.0189) == "1.89%"
        assert npz._mag(0.0189) == "1.89%"
        assert npz._mag(None) == "n/d"
        assert npz._mag(float("nan")) == "n/d"
        assert npz._mag("abc") == "n/d"

    def test_verso_dichiarato(self):
        assert npz._side(-0.01) == "sotto"
        assert npz._side(0.01) == "sopra"
        assert npz._side(0.0) == "in linea con"
        assert npz._side(None) == "n/d"

    def test_quota_senza_segno(self):
        # una frequenza non e' un rendimento: niente "+" davanti
        assert npz._share(0.635) == "63.5%"
        assert npz._share(None) == "n/d"
        assert not npz._share(0.635).startswith("+")

    def test_report_non_dice_piu_positivi_col_segno(self):
        import re
        a, t = _audit(), _test()
        md = npz.render_report(a, t, npz.decide(a, t))
        # Una FREQUENZA non e' un rendimento: il segno "+" davanti a
        # "N% positivi" / "N% delle chiuse" fa leggere un delta dove c'e' una
        # proporzione. Il "+" resta legittimo sui veri delta (residuo, ROI).
        assert not re.search(r"\+\d+(?:\.\d+)?% (?:positivi|delle chi)", md), \
            [m.group(0) for m in re.finditer(r"\+\d+(?:\.\d+)?% (?:positivi|delle chi)", md)]

    def test_distanza_con_il_verso(self):
        import re
        a, t = _audit(), _test()
        md = npz.render_report(a, t, npz.decide(a, t))
        # "sotto/sopra" dichiarato; mai "a +X% dal mercato" (il "+" davanti a
        # una distanza verso il BASSO dice l'opposto di quel che e' successo).
        assert "sotto il mercato" in md or "sopra il mercato" in md
        assert not re.search(r"a \+\d+\.\d+% dal mercato", md)

    def test_margine_espresso_come_multiplo(self):
        a, t = _audit(), _test()
        a["clv"]["fair"] = {**(a["clv"].get("fair") or {}),
                            "n": 33, "margin_mean": 0.0413,
                            "margin_share_of_clv": 1.5398,
                            "clv_book": 0.0264, "clv_fair": -0.0142,
                            "status": "NON DISTINGUIBILE DA ZERO DOPO IL DE-VIG",
                            "clv_book_ci95": [0.004, 0.048],
                            "clv_fair_ci95": [-0.036, 0.007],
                            "share_positive_book": 0.636,
                            "share_positive_fair": 0.242}
        md = npz.render_report(a, t, npz.decide(a, t))
        assert "1.54×" in md
        assert "+153.99%" not in md and "153.99%" not in md

    def test_nessun_none_nel_report(self):
        a, t = _audit(), _test()
        a["clv"]["fair"] = {}
        md = npz.render_report(a, t, npz.decide(a, t))
        assert "None" not in md
        assert "nan" not in md.lower().replace("finanziaria", "")


# ---------------------------------------------------------------------------
# 7) rigenerazione del report (holdout gia' consumato)
# ---------------------------------------------------------------------------
class TestReportOnly:
    def _seed(self, tmp_path, test=None):
        (tmp_path / "NEXT_PHASE_RESULTS.json").write_text(
            json.dumps({"test": test if test is not None else _test()}), encoding="utf-8")

    def test_rilegge_il_test_senza_rieseguirlo(self, tmp_path, monkeypatch):
        self._seed(tmp_path)
        seen = {}

        def fake_build(*, outdir=None, sign=True, snapshot=None):
            seen["sign"] = sign
            return {"audit": _audit(), "test": {}, "decision": {}, "report_md": "",
                    "generated_at": "x"}

        monkeypatch.setattr(npz, "build", fake_build)
        monkeypatch.setattr(npz.prereg, "run_test",
                            lambda *a, **k: pytest.fail("holdout rieseguito"))
        res = npz.rebuild_report(tmp_path)
        assert seen["sign"] is False          # nessuna firma, nessun test nuovo
        assert res["test"]["verdict"] == "INCONCLUSIVE"
        assert "NESSUN VANTAGGIO DIMOSTRATO" in res["report_md"]

    def test_senza_risultato_salvato_errore_esplicito(self, tmp_path):
        with pytest.raises(RuntimeError):
            npz.rebuild_report(tmp_path)

    def test_la_rigenerazione_non_scrive_una_riga_di_log(self, tmp_path, monkeypatch):
        self._seed(tmp_path)

        def fake_build(*, outdir=None, sign=True, snapshot=None):
            return {"audit": _audit(), "test": {}, "decision": {}, "report_md": "",
                    "generated_at": "x"}

        monkeypatch.setattr(npz, "build", fake_build)
        monkeypatch.setattr(npz, "OUT_DIR", tmp_path)
        assert npz.main(["--report-only"]) == 0
        assert not (tmp_path / "next_phase_log.txt").exists()
        assert (tmp_path / "NEXT_PHASE_REPORT.md").exists()

    def test_la_pipeline_normale_scrive_il_log(self, tmp_path):
        a, t = _audit(), _test()
        res = {"generated_at": "2026-10-20T10:00:00+00:00", "audit": a, "test": t,
               "decision": npz.decide(a, t),
               "report_md": npz.render_report(a, t, npz.decide(a, t))}
        npz.write_outputs(res, tmp_path, log=False)
        assert not (tmp_path / "next_phase_log.txt").exists()
        npz.write_outputs(res, tmp_path)
        assert (tmp_path / "next_phase_log.txt").exists()
