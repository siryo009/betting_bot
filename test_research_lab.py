"""Test del laboratorio di ricerca (tutti OFFLINE, nessun ordine, nessuna rete).

Cosa difendono, in ordine di importanza:

1. **sola lettura**: la connessione allo snapshot di produzione RIFIUTA una
   scrittura (il laboratorio non deve poter toccare il ledger che regola soldi);
2. **anti-leakage**: le regole non guardano l'esito (prova per "sign flip"):
   una regola sporca deve essere RILEVATA, altrimenti la prova non vale nulla;
3. **statistica onesta**: CI a cluster, potenza dichiarata, Bonferroni sulle
   celle. Un "vincitore" su 6 celle senza correzione e' rumore;
4. **niente seconda verita'**: le soglie dei candidati sono quelle di
   produzione, confrontate con `value_filter`, non ricopiate;
5. **riproducibilita'**: il giro completo scrive gli artefatti e il report
   dichiara il capitale misurato.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from research_isolated.lab import backtest, diagnostics, loader, run_lab, stats
from research_isolated.lab.strategies import (STRATEGIES, Strategy, leak_audit,
                                              select)

LAB_DIR = Path(__file__).resolve().parents[0] / "research_isolated" / "lab"


# --------------------------------------------------------------------------- #
# 1. sola lettura e provenienza dei dati
# --------------------------------------------------------------------------- #
class TestSolaLettura:
    def test_connessione_rifiuta_una_scrittura(self):
        conn = loader.connect()
        try:
            with pytest.raises(sqlite3.Error):
                conn.execute("CREATE TABLE _prova(x INTEGER)")
        finally:
            conn.close()

    def test_connessione_rifiuta_un_update(self):
        conn = loader.connect()
        try:
            with pytest.raises(sqlite3.Error):
                conn.execute("UPDATE predictions SET status='x' WHERE 0")
        finally:
            conn.close()

    def test_snapshot_fuori_dalla_produzione(self):
        import tracker
        snap = Path(loader.require_snapshot()).resolve()
        prod_dir = Path(tracker.DB_PATH).resolve().parent
        assert prod_dir not in snap.parents, "lo snapshot non puo' stare nel DATA_DIR di produzione"
        assert snap.name != Path(tracker.DB_PATH).name or snap.parent != prod_dir

    def test_snapshot_integro(self):
        a = loader.audit()
        assert a["quick_check"] == "ok"
        assert a["tables"]["predictions"] > 1000
        assert a["tables"]["bets"] > 0

    def test_nessuna_scrittura_nel_sorgente_del_lab(self):
        forbidden = re.compile(r"\b(INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM)\b", re.I)
        for py in LAB_DIR.glob("*.py"):
            src = py.read_text(encoding="utf-8")
            assert not forbidden.search(src), f"scrittura SQL in {py.name}"
            if "sqlite3.connect(" in src:
                assert "mode=ro" in src, f"connessione non read-only in {py.name}"

    def test_nessuna_rete_nel_sorgente_del_lab(self):
        for py in LAB_DIR.glob("*.py"):
            src = py.read_text(encoding="utf-8")
            for mod in ("requests", "urllib", "httpx", "aiohttp"):
                assert f"import {mod}" not in src, f"{py.name} importa {mod}"


# --------------------------------------------------------------------------- #
# 2. anti-leakage
# --------------------------------------------------------------------------- #
class TestAntiLeakage:
    def test_leak_audit_pulito_sui_candidati(self):
        out = leak_audit(loader.predictions())
        assert out["clean"] is True
        assert all(c["identical"] for c in out["checked"])
        assert len(out["checked"]) == len(STRATEGIES)

    def test_regola_sporca_viene_rilevata(self):
        """Una regola che guarda l'esito DEVE far fallire la prova."""
        dirty = Strategy("dirty", "test", "guarda il futuro",
                         lambda d: d["esito_finale"] == "won")
        out = leak_audit(loader.predictions(), {"dirty": dirty})
        assert out["clean"] is False

    def test_regola_che_guarda_il_profit_viene_rilevata(self):
        dirty = Strategy("dirty2", "test", "guarda il P/L",
                         lambda d: pd.to_numeric(d["profit"], errors="coerce") > 0)
        out = leak_audit(loader.predictions(), {"dirty2": dirty})
        assert out["clean"] is False

    def test_le_selezioni_non_usano_colonne_di_esito(self):
        """Tripwire sul CODICE delle regole, non sulla prosa.

        Il primo tentativo usava `"profit" not in src` e falliva sulla parola
        "profittevoli" in una docstring: un controllo sul testo boccia la
        documentazione invece del difetto. Qui si tolgono stringhe e commenti
        con `tokenize` e si guarda solo l'identificatore.
        """
        import io
        import tokenize

        src = (LAB_DIR / "strategies.py").read_text(encoding="utf-8")
        code = src[src.index("def value_1x2("):src.index("STRATEGIES = {")]
        toks = [t.string for t in tokenize.generate_tokens(io.StringIO(code).readline)
                if t.type not in (tokenize.STRING, tokenize.COMMENT)]
        clean = " ".join(toks)
        for banned in ("esito_finale", "profit", "settled_at"):
            assert banned not in clean, f"la regola usa {banned}"


# --------------------------------------------------------------------------- #
# 3. split cronologico
# --------------------------------------------------------------------------- #
def _synth(n: int = 200, seed: int = 3) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2026-09-01", periods=n, freq="3h", tz="UTC")
    markets = np.where(rng.random(n) < 0.5, "1X2", "OU")
    return pd.DataFrame({
        "id": range(n), "match_id": [f"m{i}" for i in range(n)],
        "mercato": markets,
        "esito": rng.choice(["1", "X", "2"], n),
        "quota": rng.uniform(1.2, 2.6, n),
        "prob": rng.uniform(0.4, 0.8, n),
        "ev": rng.uniform(0.0, 0.2, n),
        "market_prob": rng.uniform(0.4, 0.8, n),
        "market_edge": rng.uniform(-0.05, 0.15, n),
        "status": "value",
        "esito_finale": rng.choice(["won", "lost"], n),
        "profit": rng.choice([0.7, -1.0], n),
        "league": "X", "created_at": dates, "settled_at": dates,
    })


class TestSplit:
    def test_universo_guida_il_taglio(self):
        df = _synth()
        train, test = backtest.chronological_split_for(
            df, lambda d: d["mercato"] == "1X2", 0.7)
        assert train["created_at"].max() < test["created_at"].min()
        # il 70% dell'UNIVERSO (non del dataset) deve stare in train: e' il
        # difetto che rendeva la validation del 1X2 una misura del calendario
        universe_total = int((df["mercato"] == "1X2").sum())
        in_train = int((train["mercato"] == "1X2").sum())
        assert 0.6 < in_train / universe_total < 0.8, in_train / universe_total

    def test_senza_universo_ricade_sul_taglio_globale(self):
        df = _synth()
        a, b = backtest.chronological_split_for(df, None, 0.7)
        assert len(a) == 140 and len(b) == 60

    def test_holdout_mai_prima_della_validation(self):
        for st in STRATEGIES.values():
            tr, te = backtest.chronological_split_for(loader.predictions(), st.universe)
            if tr.empty or te.empty:
                continue
            assert tr["created_at"].max() <= te["created_at"].min()

    def test_universo_troppo_piccolo_non_rompe(self):
        df = _synth(30)
        tr, te = backtest.chronological_split_for(df, lambda d: d["id"] < 2, 0.7)
        assert len(tr) + len(te) == 30


# --------------------------------------------------------------------------- #
# 4. statistica
# --------------------------------------------------------------------------- #
class TestStatistica:
    def test_wilson_regge_agli_estremi(self):
        lo, hi = stats.wilson_ci(0, 10)
        assert 0.0 <= lo <= hi <= 1.0
        lo, hi = stats.wilson_ci(10, 10)
        assert 0.0 <= lo <= hi <= 1.0
        assert lo > 0.6

    def test_bootstrap_cluster_non_piu_stretto(self):
        vals = [1.0, -1.0] * 20
        iid = stats.bootstrap_mean_ci(vals)
        cl = stats.bootstrap_mean_ci(vals, clusters=["a"] * 20)
        assert (cl["hi"] - cl["lo"]) >= (iid["hi"] - iid["lo"]) * 0.5

    def test_potenza_e_inversa_del_campione(self):
        """`required_bets` e' senza potenza, `min_detectable_effect` all'80%:
        a parita' di n il secondo e' piu' grande del fattore (z+0.84)/z."""
        n = stats.required_bets(0.02, 0.8)
        mde = stats.min_detectable_effect(n, 0.8)
        assert mde == pytest.approx(0.02 * (1.959963984540054 + 0.8416212335729143)
                                    / 1.959963984540054, rel=0.02)

    def test_potenza_decresce_col_campione(self):
        assert stats.min_detectable_effect(100, 0.8) > stats.min_detectable_effect(1000, 0.8)

    def test_potenza_su_input_invalidi(self):
        assert stats.min_detectable_effect(0, 0.8) is None
        assert stats.min_detectable_effect(10, 0.0) is None

    @pytest.mark.parametrize("summ,expected", [
        ({"n": 0}, "NON VERIFICABILE"),
        ({"n": 10, "roi": 0.5, "roi_ci95": {"lo": 0.1, "hi": 0.9}}, "DA VERIFICARE"),
        ({"n": 100, "roi": 0.05, "roi_ci95": {"lo": 0.01, "hi": 0.09}}, "PROMETTENTE"),
        ({"n": 100, "roi": -0.05, "roi_ci95": {"lo": -0.09, "hi": -0.01}}, "SCARTATO"),
        ({"n": 100, "roi": 0.0, "roi_ci95": {"lo": -0.05, "hi": 0.05}}, "DA VERIFICARE"),
    ])
    def test_classificazione(self, summ, expected):
        assert stats.classify(summ) == expected

    def test_drawdown(self):
        assert stats.max_drawdown([1.0, -1.0, -1.0, 2.0]) == pytest.approx(2.0)


# --------------------------------------------------------------------------- #
# 5. diagnostica
# --------------------------------------------------------------------------- #
class TestDiagnostica:
    def test_band_table_copre_le_fasce_con_dati(self):
        out = diagnostics.band_table(_synth())
        assert out and all(r["n"] > 0 for rows in out.values() for r in rows)

    def test_cell_scan_su_rumore_non_produce_vincitori(self):
        df = _synth(900, seed=5)
        rng = np.random.default_rng(5)
        df["profit"] = rng.choice([0.7, -1.0], len(df))
        out = diagnostics.cell_scan(df, min_n=25)
        assert out["tests"] > 0
        assert out["survivors"] == 0
        assert "nessuna cella" in out["verdict"]

    def test_cell_scan_rileva_un_edge_forte(self):
        """Con un edge VERO e grande la cella deve sopravvivere a Bonferroni.

        Il segnale ha varianza (85% vinte a quota 1.5 = ROI +27,5%): un P/L
        costante non e' un campione (sd=0) e il test giustamente non conclude.
        """
        rng = np.random.default_rng(21)
        n = 400
        df = pd.DataFrame({
            "id": range(n), "match_id": [f"m{i}" for i in range(n)],
            "mercato": "1X2", "esito": "1", "quota": 1.5,
            "prob": 0.7, "ev": 0.05, "market_prob": 0.65, "market_edge": 0.05,
            "status": "value",
            "esito_finale": rng.choice(["won", "lost"], n, p=[0.85, 0.15]),
            "profit": np.where(rng.random(n) < 0.85, 0.5, -1.0),
            "league": "X",
            "created_at": pd.date_range("2026-09-01", periods=n, freq="2h", tz="UTC"),
            "settled_at": pd.date_range("2026-09-01", periods=n, freq="2h", tz="UTC"),
        })
        out = diagnostics.cell_scan(df, min_n=25)
        assert out["survivors"] >= 1
        assert any(c["market"] == "1X2" for c in out["cells"])
        assert out["alpha_bonferroni"] <= 0.05

    def test_ev_monotonicity_segno(self):
        df = _synth(400, seed=2)
        # EV informativo: profit sale con l'EV
        df["profit"] = np.where(df["ev"] > df["ev"].median(), 1.0, -1.0)
        out = diagnostics.ev_monotonicity(df, n_boot=300)
        assert out["spearman_rho"] > 0.5

    def test_ev_monotonicity_rumore(self):
        df = _synth(400, seed=4)
        out = diagnostics.ev_monotonicity(df, n_boot=300)
        assert abs(out["spearman_rho"]) < 0.35

    def test_ev_calibration_sovrastima(self):
        df = _synth(300, seed=6)
        df["ev"] = 0.15
        df["profit"] = -1.0
        out = diagnostics.ev_calibration(df)
        assert "SOVRASTIMATO" in out["verdict"]
        assert out["realized_roi"] < 0 < out["expected_roi"]

    def test_ev_calibration_calibrata(self):
        df = _synth(300, seed=8)
        df["ev"] = 0.03
        df["profit"] = 0.03
        out = diagnostics.ev_calibration(df)
        assert "CALIBRATO" in out["verdict"]

    def test_stage_returns_su_dati_reali(self):
        out = diagnostics.stage_returns(loader.predictions(), loader.bets())
        assert out["rows_scanned"] > 1000
        assert out["playable_closed"] > 0
        assert out["live_bets"] > 0

    def test_live_vs_ledger_giunzione(self):
        out = diagnostics.live_vs_ledger(loader.predictions(), loader.bets())
        assert out["n_live"] > 0
        assert 0 <= out["join"]["matched_share"] <= 1
        assert out["roi"] is not None

    def test_soft_book_senza_snapshot_e_non_verificabile(self):
        from research_isolated.lab import soft_book
        out = soft_book.run(loader.snapshots(), loader.results())
        assert out["status"] in ("NON VERIFICABILE", "DA VERIFICARE")


class TestFasceDiPrezzoPagato:
    """La tabella del report e' quella del JSON, e il ROI e' PESATO.

    Difetto reale corretto qui: la prima versione usava `_summ`, che tratta il
    profitto come PER UNITA' di stake. Sulle puntate il profitto e' in valuta,
    quindi la tabella stampava +131% dove il ROI vero era +33%.
    """

    @staticmethod
    def _bets() -> pd.DataFrame:
        return pd.DataFrame({
            "match_id": ["m1", "m1", "m1", "m2", "m3", "m4"],
            "mode": ["live", "live", "live", "live", "live", "sim"],
            "price": [1.25, 1.30, 1.35, 2.70, 2.80, 1.50],
            "stake": [10.0, 10.0, 10.0, 5.0, 5.0, 100.0],
            "profit": [2.5, 3.0, 3.5, -5.0, -5.0, 50.0],
            "esito_finale": ["won", "won", "won", "lost", "lost", "won"],
        })

    def test_roi_pesato_sullo_stake(self):
        rows = diagnostics.live_price_bands(self._bets())
        low = [r for r in rows if r["band"] == "1.2-1.4"][0]
        assert low["n"] == 3
        assert low["stake"] == pytest.approx(30.0)
        assert low["roi"] == pytest.approx(9.0 / 30.0)      # non 3.0 (media per gettone)

    def test_esclude_sim_e_stake_zero(self):
        b = self._bets()
        b.loc[0, "stake"] = 0.0
        rows = diagnostics.live_price_bands(b)
        assert sum(r["n"] for r in rows) == 4                # 6 - 1 sim - 1 stake 0
        assert all(r["n"] > 0 for r in rows)

    def test_bootstrap_a_cluster_non_di_gettoni(self):
        rows = diagnostics.live_price_bands(self._bets())
        high = [r for r in rows if r["band"] == "2.5-+"][0]
        assert high["n"] == 2 and high["clusters"] == 2
        # 3 gettoni della stessa partita 1 cluster: l'unita' di indipendenza
        low = [r for r in rows if r["band"] == "1.2-1.4"][0]
        assert low["clusters"] == 1
        assert low["roi"] is not None

    def test_report_json_contiene_le_fasce(self):
        block = run_lab._ledger_block(loader.bets())
        assert block["price_bands"]
        assert {"band", "n", "roi", "ci95", "clusters"} <= set(block["price_bands"][0])


class TestCoortiClv:
    """Il CLV deve ordinare i risultati, altrimenti non e' un predittore."""

    @staticmethod
    def _preds() -> pd.DataFrame:
        return pd.DataFrame({
            "match_id": ["a", "b", "c", "d"],
            "esito": ["1", "1", "1", "1"],
            "status": ["value"] * 4,
            "profit": [1.0, 0.9, -1.0, -1.0],
            "created_at": pd.date_range("2026-10-01", periods=4, freq="h", tz="UTC"),
        })

    @staticmethod
    def _clv() -> pd.DataFrame:
        return pd.DataFrame({
            "match_id": ["a", "b", "c", "d"],
            "esito": ["1"] * 4,
            "signal_quota": [2.0, 2.0, 2.0, 2.0],
            "closing_odds": [None] * 4,
            "closing_quota": [1.8, 1.9, 2.2, 2.0],   # +, +, -, eco
            "pinnacle_quota": [None] * 4,
            "updated_at": pd.date_range("2026-10-02", periods=4, freq="h", tz="UTC"),
        })

    def test_coorti_separano_per_segno(self):
        out = diagnostics.clv_cohorts(self._preds(), self._clv())
        assert out["joined"] == 3                       # l'eco (closing 2.0) e' escluso
        assert out["positive"]["n"] == 2
        assert out["negative"]["n"] == 1
        assert out["positive"]["roi"] > out["negative"]["roi"]
        assert "ordina" in out["verdict"]

    def test_senza_campioni_non_verificabile(self):
        empty = self._clv().iloc[0:0]
        assert diagnostics.clv_cohorts(self._preds(), empty)["status"] == "NON VERIFICABILE"


# --------------------------------------------------------------------------- #
# 6. niente seconda verita' sulle soglie
# --------------------------------------------------------------------------- #
class TestSoglieDiProduzione:
    def test_fascia_quota_allineata(self):
        import value_filter as vf
        p = STRATEGIES["value_1x2"].params
        assert p["odds_min"] == pytest.approx(vf.ODDS_MIN)
        assert p["odds_max"] == pytest.approx(vf.ODDS_MAX)

    def test_ev_min_allineato(self):
        import value_filter as vf
        assert STRATEGIES["value_1x2"].params["min_ev"] == pytest.approx(vf.EV_MIN)

    def test_edge_min_allineato(self):
        import market_calib as mc
        assert STRATEGIES["value_1x2"].params["min_edge"] == pytest.approx(mc.MARKET_EDGE_MIN)

    def test_tennis_fascia_allineata(self):
        import tennis_lane as tl
        p = STRATEGIES["tennis_band"].params
        assert p["odds_min"] == pytest.approx(tl.ODDS_MIN)
        assert p["odds_max"] == pytest.approx(tl.ODDS_MAX)
        assert p["min_ev"] == pytest.approx(tl.EV_MIN)

    def test_ou_ah_usano_la_fascia_della_pipeline(self):
        import value_filter as vf
        for name in ("ou_gate", "ah_gate"):
            p = STRATEGIES[name].params
            assert p["odds_min"] == pytest.approx(vf.ODDS_MIN)
            assert p["odds_max"] == pytest.approx(vf.ODDS_MAX)


# --------------------------------------------------------------------------- #
# 7. capitale dichiarato e giro completo
# --------------------------------------------------------------------------- #
class TestCapitaleEArtefatti:
    def test_capitale_misurato_e_datato(self):
        cap = run_lab.CAPITAL_NOTE
        assert cap["sx_available_usdc"] > 0
        assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}Z$", cap["measured_at"])
        assert "user/balance-v3" in cap["source"] or "execution_engine" in cap["source"]

    def test_giro_completo_scrive_gli_artefatti(self, tmp_path, monkeypatch):
        monkeypatch.setattr(run_lab, "OUT_DIR", tmp_path)
        out = run_lab.run()
        for name in ("STRATEGY_RESULTS.json", "STRATEGY_COMPARISON.csv"):
            assert (tmp_path / name).exists()
        data = json.loads((tmp_path / "STRATEGY_RESULTS.json").read_text())
        assert data["leak_audit"]["clean"] is True
        assert "diagnostics" in data and data["diagnostics"]["cells"]["tests"] >= 0
        assert data["strategies"]["value_1x2"]["full"]["n"] > 0
        csv = (tmp_path / "STRATEGY_COMPARISON.csv").read_text()
        assert "min_detectable_edge_pct" in csv.splitlines()[0]

    def test_ogni_candidato_ha_un_esito_o_e_dichiarato(self, tmp_path, monkeypatch):
        monkeypatch.setattr(run_lab, "OUT_DIR", tmp_path)
        out = run_lab.run()
        for name, block in out["strategies"].items():
            assert block["full"]["status"] in (
                "NON VERIFICABILE", "DA VERIFICARE", "PROMETTENTE", "SCARTATO"), name
            assert block["full"].get("power") is not None
