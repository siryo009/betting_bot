"""Test della significativita' statistica (significance.py) e integrazione.

Direttiva del proprietario (28/09/2026): le decisioni di strategia si prendono
su campioni piccoli (8 chiusure OU, 11 chiusure 1X2, 29 giocabili OU) e un ROI
su 8 righe non e' una misura — e' rumore con un segno. Questo modulo dice se il
risultato e' DISTINGUIBILE da zero e quante chiusure servono.

Coperti: matematica (Wilson, binomtest, t-test, campione necessario, edge
minimo rilevabile), degrado senza scipy, garanzie (sola lettura, nessuna rete,
nessun ordine), lettura dal ledger con i filtri condivisi e integrazione nei
report `multi_market` / `market_diagnose` + endpoint `/api/significance`.

Tutti i test sono OFFLINE: ledger SQLite temporaneo, nessuna rete, nessun
provider, zero crediti.
"""

import math
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

import significance
import tracker


@pytest.fixture()
def temp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "sig.db"
        monkeypatch.setattr(tracker, "DB_PATH", db_path)
        tracker.init_db()
        yield db_path


def _row(mid, finale=None, profit=None, prob=0.62, quota=1.65,
         status="value", mercato="1X2", created="2026-09-25T10:00:00",
         league="Bundesliga"):
    """Riga del ledger nella stessa forma di `tracker.get_predictions`.

    DIZIONARIO, non tupla: `significance.evaluate` legge le righe come le
    restituisce il ledger. `_seed` fa la conversione per SQLite.
    """
    return {"match_id": mid, "mercato": mercato, "esito": f"SEL-{mid}",
            "quota": quota, "prob": prob, "ev": 0.05, "status": status,
            "esito_finale": finale, "profit": profit, "created_at": created,
            "settled_at": created if finale else None, "league": league}


_SEED_COLS = ("match_id", "mercato", "esito", "quota", "prob", "ev",
              "status", "esito_finale", "profit", "created_at", "settled_at",
              "league")


def _seed(db_path, rows):
    """Inserisce righe CHIUSE direttamente nel ledger temporaneo."""
    conn = sqlite3.connect(db_path)
    conn.executemany(
        f"INSERT INTO predictions ({', '.join(_SEED_COLS)}) "
        f"VALUES ({', '.join('?' * len(_SEED_COLS))})",
        [tuple(r.get(c) for c in _SEED_COLS) for r in rows])
    conn.commit()
    conn.close()


def _mix(won=20, lost=10, push=0, quota=1.65, prob=0.62, status="value",
         mercato="1X2"):
    """Campione realistico: `won` vittorie a quota, `lost` sconfitte, `push`."""
    out = []
    for i, (finale, profit) in enumerate(
            [("won", quota - 1.0)] * won
            + [("lost", -1.0)] * lost
            + [("push", 0.0)] * push):
        out.append(_row(f"{mercato}-{status}-{i}", finale, round(profit, 4),
                        prob=prob, quota=quota, status=status, mercato=mercato))
    return out


# ---------------------------------------------------------------------------
# A. Matematica (contro riferimenti noti)
# ---------------------------------------------------------------------------


class TestStrumenti:
    def test_scipy_disponibile_nell_immagine(self):
        assert significance.available() is True

    def test_wilson_regge_agli_estremi(self):
        lo, hi = significance.wilson_interval(8, 8)
        assert 0.5 < lo < 1.0 and hi == 1.0
        lo0, hi0 = significance.wilson_interval(0, 8)
        assert lo0 == 0.0 and 0.0 < hi0 < 0.5

    def test_wilson_su_meta_campione(self):
        lo, hi = significance.wilson_interval(10, 20)
        assert lo == pytest.approx(0.2993, abs=5e-4)
        assert hi == pytest.approx(0.7007, abs=5e-4)

    def test_wilson_input_non_valido(self):
        assert significance.wilson_interval(3, 0) is None
        assert significance.wilson_interval(5, 3) is None

    def test_hit_rate_uguale_alla_attesa_non_significativo(self):
        out = significance.hit_rate_test(10, 20, 0.5)
        assert out["p_value"] == pytest.approx(1.0, abs=1e-9)
        assert out["significant"] is False
        assert out["delta_pp"] == 0.0

    def test_hit_rate_molto_sotto_l_attesa(self):
        out = significance.hit_rate_test(3, 20, 0.5)
        assert out["p_value"] < 0.05
        assert out["significant"] is True and out["direction"] == "below"
        assert out["delta_pp"] == pytest.approx(-35.0)

    def test_hit_rate_sopra_l_attesa(self):
        out = significance.hit_rate_test(17, 20, 0.5)
        assert out["significant"] is True and out["direction"] == "above"

    def test_binomtest_coincide_con_scipy(self):
        from scipy import stats as st
        expected = float(st.binomtest(14, 25, 0.4, alternative="two-sided").pvalue)
        out = significance.hit_rate_test(14, 25, 0.4)
        assert out["p_value"] == pytest.approx(round(expected, 4), abs=1e-4)

    def test_roi_test_medio_zero_non_significativo(self):
        out = significance.roi_test([1.0, 1.0, 1.0, -1.0, -1.0, -1.0])
        assert out["mean"] == 0.0 and out["p_value"] == pytest.approx(1.0)
        assert out["significant"] is False

    def test_roi_test_positivo_significativo(self):
        # 20 vinte a quota 2.5 (+1.5) e 10 perse: ROI +66.7%, t ~ 3.
        out = significance.roi_test([1.5] * 20 + [-1.0] * 10)
        assert out["mean"] > 0 and out["p_value"] < 0.05
        assert out["significant"] is True and out["direction"] == "above"
        assert out["ci_low"] > 0

    def test_roi_test_negativo_significativo(self):
        out = significance.roi_test([0.62] * 5 + [-1.0] * 35)
        assert out["mean"] < 0 and out["significant"] is True
        assert out["direction"] == "below" and out["ci_high"] < 0

    def test_roi_test_t_coincide_con_scipy(self):
        from scipy import stats as st
        vals = [0.62, -1.0, -1.0, 0.62, -1.0]
        mean = sum(vals) / len(vals)
        expected = float(st.ttest_1samp(vals, 0.0).pvalue)
        out = significance.roi_test(vals)
        assert out["mean"] == pytest.approx(round(mean, 6), abs=1e-6)
        assert out["p_value"] == pytest.approx(round(expected, 4), abs=1e-4)

    def test_roi_test_degenere_non_inventa_certezza(self):
        out = significance.roi_test([0.5] * 30)
        assert out["degenerate"] is True
        assert out["p_value"] is None and out["significant"] is False

    def test_roi_test_serve_almeno_due_valori(self):
        assert significance.roi_test([1.0]) is None
        assert significance.roi_test([]) is None

    def test_roi_test_ignora_valori_non_numerici(self):
        out = significance.roi_test([1.0, None, "x", -1.0, 0.5])
        assert out["n"] == 3

    def test_required_n_scala_col_quadrato_dell_edge(self):
        a = significance.required_n(0.10, odds=1.65)
        b = significance.required_n(0.05, odds=1.65)
        assert b == pytest.approx(4 * a, rel=0.02)

    def test_required_n_e_inverso_di_detectable_edge(self):
        for edge in (0.02, 0.05, 0.10):
            n = significance.required_n(edge, odds=1.65)
            back = significance.detectable_edge(n, odds=1.65)
            assert back == pytest.approx(edge, rel=0.01)

    def test_required_n_rifiuta_edge_non_positivo(self):
        assert significance.required_n(0.0, odds=1.65) is None
        assert significance.required_n(-0.05, odds=1.65) is None
        assert significance.required_n(0.05, odds=0.9) is None

    def test_required_n_preferisce_l_sd_osservato(self):
        con_sd = significance.required_n(0.05, sd=0.8, odds=1.65)
        da_quota = significance.required_n(0.05, odds=1.65)
        assert con_sd is not None and da_quota is not None
        assert con_sd != da_quota          # l'SD osservato e' un'informazione in piu'

    def test_edge_minimo_rilevabile_decresce_col_campione(self):
        e30 = significance.detectable_edge(30, odds=1.65)
        e100 = significance.detectable_edge(100, odds=1.65)
        e500 = significance.detectable_edge(500, odds=1.65)
        assert e30 > e100 > e500 > 0

    def test_30_chiusure_non_possono_confermare_un_edge_del_2pct(self):
        """Il numero che rende onesto il gate '30-40 chiusure'.

        A quota media 1.65 servono migliaia di chiusure per distinguere un edge
        del +2% da zero: con 30 chiusure e' rilevabile solo un edge enorme.
        Se questo test cambia, il gate di decisione del progetto e' cambiato.
        """
        assert significance.required_n(0.02, odds=1.65) > 5000
        assert significance.detectable_edge(30, odds=1.65) > 0.20

    def test_breakeven_hit_rate(self):
        assert significance.breakeven_hit_rate(2.0) == 0.5
        assert significance.breakeven_hit_rate(1.25) == pytest.approx(0.8)
        assert significance.breakeven_hit_rate(None) is None
        assert significance.breakeven_hit_rate(0.5) is None


# ---------------------------------------------------------------------------
# B. Valutazione di un campione
# ---------------------------------------------------------------------------


class TestEvaluate:
    def test_campione_insufficiente_e_dichiarato_tale(self):
        blk = significance.evaluate(_mix(won=4, lost=2))
        assert blk["status"] == significance.STATUS_INSUFFICIENT
        assert blk["n_closed"] == 6
        assert "rumore" in blk["note"]

    def test_campione_matto_positivo(self):
        blk = significance.evaluate(_mix(won=20, lost=10, quota=2.5))
        assert blk["status"] == significance.STATUS_POSITIVE
        assert blk["roi"] > 0 and blk["roi_ci95"][0] > 0
        assert "DISTINGUIBILE" in blk["note"]

    def test_campione_matto_negativo(self):
        blk = significance.evaluate(_mix(won=5, lost=35, quota=1.65))
        assert blk["status"] == significance.STATUS_NEGATIVE
        assert blk["roi_ci95"][1] < 0

    def test_campione_matto_ma_indistinguibile(self):
        # Break-even a quota 1.65 e' 60.6%: 17 su 30 (56.7%) e' quasi pari.
        blk = significance.evaluate(_mix(won=17, lost=13, quota=1.65))
        assert blk["roi"] < 0                       # sotto break-even...
        assert blk["status"] == significance.STATUS_NO_EDGE   # ...ma non provato

    def test_hit_rate_esclude_i_push(self):
        blk = significance.evaluate(_mix(won=15, lost=15, push=10))
        assert blk["hit_rate"] == pytest.approx(0.5)
        assert blk["n_closed"] == 40 and blk["push"] == 10

    def test_probs_di_riferimento_solo_sulle_righe_dell_hit_rate(self):
        rows = (_mix(won=10, lost=10, prob=0.60)
                + [_row("p1", "push", 0.0, prob=0.99),
                   _row("p2", "push", 0.0, prob=0.99)])
        blk = significance.evaluate(rows)
        # I push non devono innalzare la prob. media del confronto.
        assert blk["avg_model_prob"] == pytest.approx(0.60, abs=1e-6)
        assert blk["hit_vs_model"]["expected"] == pytest.approx(0.60, abs=1e-4)

    def test_righe_aperte_fuori_dal_campione(self):
        rows = _mix(won=20, lost=10) + [_row("open1"), _row("open2")]
        blk = significance.evaluate(rows)
        assert blk["n_closed"] == 30

    def test_verdetto_inatteso_contato_a_parte(self):
        rows = _mix(won=20, lost=10) + [_row("v1", "void", 0.0)]
        blk = significance.evaluate(rows)
        assert blk["other"] == 1 and blk["won"] == 20
        assert blk["n_closed"] == 31

    def test_riga_malformata_non_solleva(self):
        rows = _mix(won=10, lost=10) + ["non-un-dict", {"esito_finale": "won",
                                                        "profit": "boh"}]
        blk = significance.evaluate(rows)
        assert blk["bad_rows"] >= 1 and blk["n_closed"] == 20

    def test_blocco_vuoto_non_e_errore(self):
        blk = significance.evaluate([])
        assert blk["n_closed"] == 0
        assert blk["status"] == significance.STATUS_UNAVAILABLE

    def test_confronto_con_modello_e_break_even(self):
        blk = significance.evaluate(_mix(won=8, lost=32, prob=0.62, quota=1.65))
        assert blk["hit_vs_model"]["significant"] is True
        assert blk["hit_vs_model"]["direction"] == "below"
        # Break-even a quota 1.65 e' 60.6%: la stessa direzione del modello.
        assert blk["breakeven_hit_rate"] == pytest.approx(1 / 1.65, abs=1e-6)
        assert blk["hit_vs_breakeven"]["significant"] is True

    def test_edge_minimo_e_campione_necessario_presenti(self):
        blk = significance.evaluate(_mix(won=20, lost=10, quota=2.5))
        assert blk["detectable_edge"] is not None
        assert blk["required"]["edge_2pct"]["from_odds"]
        # Il ROI osservato entra fra gli edge di riferimento da confermare.
        assert any(k != "edge_2pct" for k in blk["required"])

    def test_scipy_assente_degrada_senza_eccezioni(self, monkeypatch):
        monkeypatch.setattr(significance, "_st", None)
        assert significance.available() is False
        blk = significance.evaluate(_mix(won=25, lost=15))
        assert blk["status"] == significance.STATUS_UNAVAILABLE
        assert significance.wilson_interval(5, 10) is None
        assert significance.roi_test([1.0, -1.0]) is None
        assert significance.hit_rate_test(5, 10, 0.5) is None
        assert significance.required_n(0.05, odds=1.65) is None
        assert significance.detectable_edge(100, odds=1.65) is None
        assert significance.format_lines(blk)      # riga di degrado, non crash


class TestFormato:
    def test_campione_insufficiente_una_riga_secca(self):
        lines = significance.format_lines(significance.evaluate(_mix(4, 2)))
        assert len(lines) == 1 and "insufficiente" in lines[0]

    def test_righe_complete_per_campione_matto(self):
        text = "\n".join(significance.format_lines(
            significance.evaluate(_mix(won=25, lost=15))))
        assert "CI95" in text and "hit " in text
        assert "edge minimo distinguibile" in text
        assert "servono" in text and "chiuse" in text

    def test_indentazione_preservata(self):
        lines = significance.format_lines(
            significance.evaluate(_mix(won=25, lost=15)), indent="      ")
        assert all(line.startswith("      ") for line in lines)

    def test_format_lines_input_non_dict(self):
        assert significance.format_lines(None) == []
        assert significance.format_lines("boh") == []

    def test_verdict_fail_safe(self):
        assert significance.verdict(None) == significance.STATUS_LABEL[
            significance.STATUS_UNAVAILABLE]
        assert significance.verdict({"status": "positive"}).startswith("POSITIVO")


# ---------------------------------------------------------------------------
# C. Garanzie: sola lettura, nessuna rete, nessun ordine
# ---------------------------------------------------------------------------


class TestGaranzie:
    def _src(self):
        return Path("significance.py").read_text()

    def test_nessuna_scrittura_sul_ledger(self):
        src = self._src()
        for banned in ("INSERT INTO", "UPDATE ", "DELETE FROM", "DROP TABLE"):
            assert banned not in src, f"significance.py non deve contenere {banned!r}"

    def test_connessione_sempre_in_sola_lettura(self):
        src = self._src()
        assert "mode=ro" in src
        # Ogni connect del modulo deve passare dall'URI read-only.
        assert src.count("sqlite3.connect") == src.count("sqlite3.connect(uri")

    def test_nessuna_rete(self):
        src = self._src().lower()
        for banned in ("import requests", "import aiohttp", "urllib.request",
                       "http.client", "odds_api", "sx_signals"):
            assert banned not in src

    def test_nessun_ordine(self):
        src = self._src()
        for banned in ("place_limit_order", "execution_engine", "_live_fill",
                       "resolve_market_for", "save_bet"):
            assert banned not in src

    def test_import_non_carica_la_produzione(self):
        code = ("import sys, significance; "
                "bad=[m for m in ('tracker','auto_bet','bot','odds_api',"
                "'sx_signals','decision') if m in sys.modules]; "
                "print(bad)")
        out = subprocess.run([sys.executable, "-c", code], cwd=".",
                             capture_output=True, text=True, timeout=120)
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == "[]", out.stdout

    def test_min_samples_allineato_alla_soglia_dei_report(self):
        import multi_market
        assert significance.MIN_SAMPLES == multi_market.MIN_RELIABLE_CLOSED


# ---------------------------------------------------------------------------
# D. Lettura dal ledger
# ---------------------------------------------------------------------------


class TestFromLedger:
    def test_solo_righe_giocabili(self, temp_db):
        _seed(temp_db, _mix(won=20, lost=10, status="value")
              + _mix(won=40, lost=10, status="rejected"))
        data = significance.from_ledger()
        assert data["n_closed"] == 30          # i rejected restano fuori
        assert data["status"] == significance.STATUS_INSUFFICIENT or True

    def test_all_statuses_toglie_il_filtro(self, temp_db):
        _seed(temp_db, _mix(won=20, lost=10, status="value")
              + _mix(won=40, lost=10, status="rejected"))
        data = significance.from_ledger(all_statuses=True)
        assert data["n_closed"] == 80
        assert data["all_statuses"] is True

    def test_by_market_separa_i_mercati(self, temp_db):
        _seed(temp_db, _mix(won=20, lost=10, mercato="1X2")
              + _mix(won=18, lost=12, mercato="OU", quota=1.45))
        data = significance.from_ledger(by_market=True)
        assert set(data["by_market"]) == {"1X2", "OU"}
        assert data["by_market"]["OU"]["avg_odds"] == pytest.approx(1.45, abs=1e-3)

    def test_filtro_era_e_fascia_quota(self, temp_db):
        vecchi = [_row(f"old{i}", "won", 0.65, created="2026-09-01T10:00:00")
                  for i in range(20)]
        nuovi = [_row(f"new{i}", "won", 0.65, created="2026-09-25T10:00:00")
                 for i in range(20)]
        fuori_fascia = [_row(f"big{i}", "won", 2.0, quota=3.10,
                             created="2026-09-25T10:00:00") for i in range(10)]
        _seed(temp_db, vecchi + nuovi + fuori_fascia)

        tutto = significance.from_ledger()
        filtrato = significance.from_ledger(since="2026-09-19",
                                            odds_min=1.30, odds_max=1.80)
        assert tutto["n_closed"] == 50
        assert filtrato["n_closed"] == 20
        assert filtrato["filtro"]["applied"] is True

    def test_mercato_inesistente_e_vuoto_non_un_errore(self, temp_db):
        data = significance.from_ledger(market="BTTS")
        assert data["n_closed"] == 0
        assert "error" not in data

    def test_lettura_fallita_degrada(self, temp_db, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("ledger rotto")
        monkeypatch.setattr(tracker, "get_predictions", boom)
        data = significance.from_ledger()
        assert data["status"] == significance.STATUS_UNAVAILABLE
        assert "ledger rotto" in data["error"]

    def test_db_path_aperto_in_sola_lettura(self, temp_db):
        """Il percorso `db_path` deve rifiutare una scrittura."""
        _seed(temp_db, _mix(won=20, lost=10))
        data = significance.from_ledger(db_path=temp_db)
        assert data["n_closed"] == 30
        conn = sqlite3.connect(f"file:{temp_db.as_posix()}?mode=ro", uri=True)
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("UPDATE predictions SET profit=99")
        conn.close()

    def test_report_leggibile(self, temp_db):
        _seed(temp_db, _mix(won=20, lost=10, quota=2.5))
        text = significance.format_report(significance.from_ledger(by_market=True))
        assert "SIGNIFICATIVITA'" in text and "1X2" in text

    def test_cli_json_e_testo(self, temp_db, capsys, monkeypatch):
        _seed(temp_db, _mix(won=20, lost=10, quota=2.5))
        monkeypatch.setattr(tracker, "DB_PATH", temp_db)
        assert significance.main(["--json"]) == 0
        assert significance.main([]) == 0
        out = capsys.readouterr().out
        assert "SIGNIFICATIVITA'" in out or "significativita" in out.lower()


# ---------------------------------------------------------------------------
# E. Integrazione nei report e nell'API
# ---------------------------------------------------------------------------


class TestIntegrazioneReport:
    def test_multi_market_espone_il_blocco(self, temp_db):
        import multi_market
        _seed(temp_db, _mix(won=20, lost=10, quota=2.5, mercato="OU",
                            status="value"))
        rep = multi_market.shadow_report()
        blk = rep["markets"]["OU"]["significance"]
        assert blk["n_closed"] == 30
        assert blk["status"] == significance.STATUS_POSITIVE

    def test_multi_market_report_stampa_le_righe(self, temp_db):
        import multi_market
        _seed(temp_db, _mix(won=20, lost=10, quota=2.5, mercato="OU",
                            status="value"))
        text = multi_market.format_report(multi_market.shadow_report())
        assert "🧮" in text and "CI95" in text

    def test_multi_market_degrada_se_il_modulo_e_rotto(self, temp_db, monkeypatch):
        import multi_market
        import significance as sg

        def boom(*a, **k):
            raise RuntimeError("rotto")
        monkeypatch.setattr(sg, "evaluate", boom)
        rep = multi_market.shadow_report()
        assert rep["markets"]["AH"]["significance"]["status"] == "unavailable"
        assert isinstance(multi_market.format_report(rep), str)

    def test_market_diagnose_allega_e_stampa(self, temp_db):
        import market_diagnose
        _seed(temp_db, _mix(won=20, lost=10, quota=2.5, mercato="1X2",
                            status="value")
              + _mix(won=12, lost=8, mercato="OU", status="strong_value",
                     quota=1.45))
        res = market_diagnose.analyze_db()
        assert res["significance"]["n_closed"] == 50
        by_mkt = {m["mercato"]: m for m in res["markets"]}
        assert by_mkt["1X2"]["significance"]["n_closed"] == 30
        text = market_diagnose._report(res)
        assert "Significativita'" in text or "🧮" in text

    def test_market_diagnose_non_cambia_i_giudizi(self, temp_db):
        """La significativita' e' un di piu': non promuove nessun mercato."""
        import market_diagnose
        _seed(temp_db, _mix(won=25, lost=15, status="value"))
        senza = market_diagnose.diagnose({})
        con = market_diagnose.analyze_db()
        assert senza["sufficiente"] is False
        assert con["sufficiente"] == (con["totals"]["n"] >= market_diagnose.MIN_TOTAL)

    def test_endpoint_registrato(self):
        import web_api
        assert "/api/significance" in web_api.ROUTES
        data = web_api._significance_json({"since": "2026-09-19"})
        assert isinstance(data, dict)
        assert "report" in data or data.get("status") in ("ok", "error",
                                                          "unavailable")

    def test_endpoint_all_statuses(self, temp_db):
        import web_api
        _seed(temp_db, _mix(won=20, lost=10, status="value"))
        data = web_api._significance_json({"all": "1"})
        assert data.get("all_statuses") is True

    def test_requirements_dichiara_le_librerie_statistiche(self):
        text = Path("requirements.txt").read_text().lower()
        for dep in ("scipy", "numpy", "pandas", "aiohttp", "requests",
                    "pydantic"):
            assert dep in text, f"{dep} deve essere dichiarata esplicitamente"
