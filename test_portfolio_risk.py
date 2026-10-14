"""Test di `portfolio_risk` — tutti OFFLINE.

Nessuna rete, nessun credito, nessun ordine, nessun provider: SQLite
temporaneo per le parti che leggono il ledger e matematica pura per il resto.
La classe di PARITA' confronta le probabilita' calcolate qui con quelle di
`poisson_engine`: se le regole di saldo per cella divergessero dal motore, il
rischio di portafoglio misurerebbe un gioco diverso da quello che si scommette.
"""
from __future__ import annotations

import ast
import json
import math
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import portfolio_risk as pr
from poisson_engine import (ah_outcome_probs, ou_outcome_probs, prob_1x2,
                            prob_btts)

LAM_H, LAM_A = 1.6, 1.1


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _leg(market, esito, odds, stake=5.0, **extra):
    leg = {"mercato": market, "esito": esito, "quota": odds, "stake": stake}
    leg.update(extra)
    return leg


def _temp_db(tmp_path) -> str:
    """Ledger temporaneo con lo SCHEMA di produzione (solo le colonne usate)."""
    path = str(tmp_path / "ledger.db")
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE matches (id TEXT PRIMARY KEY, league TEXT, home_team TEXT,
                              away_team TEXT, commence_time TEXT, status TEXT,
                              last_updated TEXT);
        CREATE TABLE match_analysis (id INTEGER PRIMARY KEY, match_id TEXT,
                                     lam_h REAL, lam_a REAL, prob_1 REAL,
                                     prob_X REAL, prob_2 REAL, prob_over REAL,
                                     best_ev REAL, best_esito TEXT, best_quota REAL,
                                     best_bookmaker TEXT, status TEXT,
                                     timestamp TEXT, market_prob REAL,
                                     market_edge REAL);
        CREATE TABLE match_results (match_id TEXT, league TEXT, home_team TEXT,
                                    away_team TEXT, score_home INTEGER,
                                    score_away INTEGER, result TEXT,
                                    settled_at TEXT);
        CREATE TABLE bets (id INTEGER PRIMARY KEY, match_id TEXT, mercato TEXT,
                           esito TEXT, market_id TEXT, selection_id INTEGER,
                           price REAL, stake REAL, mode TEXT, status TEXT,
                           bet_id TEXT, esito_finale TEXT, profit REAL,
                           created_at TEXT, settled_at TEXT);
        """)
    conn.commit()
    conn.close()
    return path


def _write_reco(path, rows):
    conn = sqlite3.connect(path)
    conn.executemany(
        "INSERT INTO match_results (match_id, league, home_team, away_team, "
        "score_home, score_away, result, settled_at) VALUES (?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Parita' col motore: nessuna regola di saldo ricopiata
# ---------------------------------------------------------------------------

class TestParitaColMotore:
    def _pw(self, leg):
        grid = pr._grid(LAM_H, LAM_A)
        win, _push = pr.leg_cell_outcomes(leg)
        return float((grid.reshape(-1) * win.reshape(-1)).sum())

    def _ppush(self, leg):
        grid = pr._grid(LAM_H, LAM_A)
        _win, push = pr.leg_cell_outcomes(leg)
        return float((grid.reshape(-1) * push.reshape(-1)).sum())

    @pytest.mark.parametrize("sel,idx", [("1", 0), ("X", 1), ("2", 2)])
    def test_1x2_uguale_al_motore(self, sel, idx):
        assert self._pw(_leg("1X2", sel, 1.9)) == pytest.approx(
            prob_1x2(LAM_H, LAM_A)[idx], abs=1e-12)
        assert self._ppush(_leg("1X2", sel, 1.9)) == 0.0

    @pytest.mark.parametrize("line", [1.5, 2.5, 3.0, 2.25, 2.75])
    def test_ou_uguale_al_motore(self, line):
        for side in ("over", "under"):
            leg = _leg("OU", "%s %.2f" % (side.capitalize(), line), 1.9)
            expected = ou_outcome_probs(LAM_H, LAM_A, line, side=side)
            assert self._pw(leg) == pytest.approx(expected[0], abs=1e-12)
            assert self._ppush(leg) == pytest.approx(expected[1], abs=1e-12)

    @pytest.mark.parametrize("line", [-0.75, -0.5, 0.0, 0.5, 1.25])
    def test_ah_uguale_al_motore(self, line):
        for side in ("home", "away"):
            leg = _leg("AH", "%s %+.2f" % (side.capitalize(), line), 1.9)
            expected = ah_outcome_probs(LAM_H, LAM_A, line, side=side)
            assert self._pw(leg) == pytest.approx(expected[0], abs=1e-12)
            assert self._ppush(leg) == pytest.approx(expected[1], abs=1e-12)

    @pytest.mark.parametrize("sel", ["Yes", "No"])
    def test_btts_uguale_al_motore(self, sel):
        p_yes = prob_btts(LAM_H, LAM_A)
        assert self._pw(_leg("BTTS", sel, 1.9)) == pytest.approx(
            p_yes if sel == "Yes" else 1.0 - p_yes, abs=1e-12)

    def test_probabilita_sommano_a_uno_per_gamba(self):
        for leg in (_leg("1X2", "1", 1.9), _leg("OU", "Over 2.25", 2.1),
                    _leg("AH", "Home -0.75", 1.8), _leg("BTTS", "Yes", 2.0)):
            grid = pr._grid(LAM_H, LAM_A)
            p = grid.reshape(-1)
            win, push = pr.leg_cell_outcomes(leg)
            lose = 1.0 - win.reshape(-1) - push.reshape(-1)
            assert float((p * (win.reshape(-1) + push.reshape(-1) + lose)).sum()) == \
                pytest.approx(1.0, abs=1e-12)


# ---------------------------------------------------------------------------
# Distribuzione esatta del blocco
# ---------------------------------------------------------------------------

class TestDistribuzioneEsatta:
    def test_gamba_singola_analitica(self):
        leg = _leg("1X2", "1", 2.0, stake=10.0)
        d = pr.block_distribution([leg], LAM_H, LAM_A)
        p1 = prob_1x2(LAM_H, LAM_A)[0]
        payout = 10.0
        assert d["mean"] == pytest.approx(p1 * payout - (1 - p1) * 10.0, abs=1e-9)
        # var = E[X^2] - E[X]^2 su due soli esiti (vincita 10 / sconfitta -10)
        var = p1 * payout ** 2 + (1 - p1) * 100.0 - (p1 * payout - (1 - p1) * 10) ** 2
        assert d["std"] == pytest.approx(math.sqrt(var), abs=1e-9)
        assert d["max_loss"] == pytest.approx(-10.0)
        assert d["max_win"] == pytest.approx(payout)
        assert d["p_loss"] + d["p_profit"] == pytest.approx(1.0, abs=1e-12)
        assert d["stake_total"] == pytest.approx(10.0)

    def test_probabilita_esaustive(self):
        legs = [_leg("1X2", "1", 1.9), _leg("OU", "Over 2.5", 1.95)]
        d = pr.block_distribution(legs, LAM_H, LAM_A)
        assert d["p_loss"] + d["p_profit"] + d["p_flat"] == pytest.approx(1.0, abs=1e-9)

    def test_quantili_ordinati(self):
        legs = [_leg("1X2", "1", 1.9), _leg("OU", "Over 2.5", 2.35)]
        d = pr.block_distribution(legs, LAM_H, LAM_A)
        assert d["p5"] <= d["p50"] <= d["p95"]

    def test_push_esiste_su_linea_intera(self):
        leg = _leg("OU", "Over 3.0", 1.9)
        d = pr.block_distribution([leg], LAM_H, LAM_A)
        # con la linea intera c'e' una massa di celle a P/L 0 (push)
        assert d["p_flat"] > 0.0

    def test_senza_push_su_linea_mezza(self):
        leg = _leg("OU", "Over 2.5", 1.9)
        d = pr.block_distribution([leg], LAM_H, LAM_A)
        assert d["p_flat"] == pytest.approx(0.0, abs=1e-12)

    def test_scenario_peggiore_dichiarato(self):
        leg = _leg("1X2", "1", 1.9, stake=4.0)
        d = pr.block_distribution([leg], LAM_H, LAM_A)
        worst = d["worst_cell"]
        assert worst["pl"] == pytest.approx(-4.0)
        assert worst["p"] > 0
        assert "-" in worst["score"]

    def test_drawdown_probs_col_bankroll(self):
        legs = [_leg("1X2", "1", 5.0, stake=5.0)]
        d = pr.block_distribution(legs, LAM_H, LAM_A, bankroll=50.0)
        assert d["bankroll"] == 50.0
        assert d["stake_pct"] == pytest.approx(0.1)
        # perdere lo stake intero = -10% del bankroll
        assert d["drawdown_probs"]["0.10"] == pytest.approx(d["p_loss"], abs=1e-9)

    def test_blocco_vuoto_e_lambdas_non_valide(self):
        assert pr.block_distribution([], LAM_H, LAM_A)["error"] == "nessuna_gamba_valida"
        assert pr.block_distribution([_leg("1X2", "1", 1.9)], 0.0, 1.0)["error"] == \
            "lambdas_non_valide"
        assert pr.block_distribution([_leg("1X2", "1", 1.9)], None, 1.0)["error"] == \
            "lambdas_non_valide"


# ---------------------------------------------------------------------------
# Correlazioni e diversificazione
# ---------------------------------------------------------------------------

class TestCorrelazioni:
    @staticmethod
    def _pw(leg):
        grid = pr._grid(LAM_H, LAM_A)
        win, _push = pr.leg_cell_outcomes(leg)
        return float((grid.reshape(-1) * win.reshape(-1)).sum())
    def test_esiti_mutuamente_esclusivi_negativi(self):
        legs = [_leg("1X2", "1", 2.0), _leg("1X2", "2", 2.0)]
        corr = pr.correlation_matrix(legs, LAM_H, LAM_A)
        assert corr["pairs"][0]["rho"] < -0.5  # 1 e 2 non possono vincere insieme

    def test_favorito_casa_correlato_con_over(self):
        legs = [_leg("1X2", "1", 1.9), _leg("OU", "Over 2.5", 1.95)]
        corr = pr.correlation_matrix(legs, LAM_H, LAM_A)
        assert corr["pairs"][0]["rho"] > 0  # casa forte -> piu' gol

    def test_pareggio_correlato_con_under(self):
        """Il pareggio e' il classico correlato positivo con l'Under."""
        legs = [_leg("1X2", "X", 3.4), _leg("OU", "Under 2.5", 1.95)]
        corr = pr.correlation_matrix(legs, LAM_H, LAM_A)
        assert corr["pairs"][0]["rho"] > 0

    def test_trasferta_correlata_con_over(self):
        """Trasferta forte = molti gol: segno misurato, non assunto."""
        legs = [_leg("1X2", "2", 3.0), _leg("OU", "Over 2.5", 1.95)]
        corr = pr.correlation_matrix(legs, LAM_H, LAM_A)
        assert corr["pairs"][0]["rho"] > 0.05

    def test_ah_lato_e_complemento_del_suo_contrario(self):
        """Home -0.5 e Away +0.5 sono lo STESSO mercato visto dai due lati.

        Tripwire della convenzione: la linea di `order_target` e' dal punto di
        vista di teamOne e va ribaltata per l'ospite, altrimenti le due
        probabilita' non sommano a 1 e la correlazione fra i due lati non e'
        -1 (difetto trovato e corretto il 14/10/2026).
        """
        home = _leg("AH", "Home -0.50", 1.75)
        away = _leg("AH", "Away +0.50", 2.10)
        assert self._pw(home) + self._pw(away) == pytest.approx(1.0, abs=1e-12)
        corr = pr.correlation_matrix([home, away], LAM_H, LAM_A)
        assert corr["pairs"][0]["rho"] == pytest.approx(-1.0, abs=1e-9)

    def test_ah_lato_ospite_coerente_con_il_suo_1x2(self):
        """'Away +0.5' vince quando la trasferta vince o pareggia: il segno
        della correlazione con l'esito '2' deve essere POSITIVO."""
        corr = pr.correlation_matrix([_leg("1X2", "2", 3.0),
                                      _leg("AH", "Away +0.50", 1.80)], LAM_H, LAM_A)
        assert corr["pairs"][0]["rho"] > 0.3
        assert self._pw(_leg("AH", "Away +0.50", 1.8)) == pytest.approx(
            prob_1x2(LAM_H, LAM_A)[1] + prob_1x2(LAM_H, LAM_A)[2], abs=1e-12)

    def test_diagonale_a_uno(self):
        legs = [_leg("1X2", "1", 1.9), _leg("BTTS", "Yes", 1.9)]
        corr = pr.correlation_matrix(legs, LAM_H, LAM_A)
        assert corr["matrix"][0][0] == 1.0 and corr["matrix"][1][1] == 1.0
        assert corr["matrix"][0][1] == corr["matrix"][1][0]

    def test_diversificazione_su_blocco_correlato(self):
        legs = [_leg("1X2", "1", 1.9), _leg("OU", "Over 2.5", 1.95)]
        div = pr.diversification(legs, LAM_H, LAM_A)
        assert div["ratio"] > 1.0          # le gambe si sommano
        assert div["uplift_pct"] > 0
        assert div["std_joint"] > div["std_independent"]

    def test_diversificazione_gamba_singola_neutra(self):
        div = pr.diversification([_leg("1X2", "1", 1.9)], LAM_H, LAM_A)
        assert div["ratio"] == pytest.approx(1.0, abs=1e-9)

    def test_motore_e_distribuzione_coerenti_sulla_std(self):
        """La std congiunta non puo' essere inferiore alla somma in quadratura
        delle varianze quando tutte le covarianze sono positive."""
        legs = [_leg("1X2", "1", 1.9), _leg("OU", "Over 2.5", 1.95),
                _leg("BTTS", "Yes", 1.85)]
        div = pr.diversification(legs, LAM_H, LAM_A)
        assert div["std_joint"] >= div["std_independent"] - 1e-9


# ---------------------------------------------------------------------------
# Fail-safe e input ostili
# ---------------------------------------------------------------------------

class TestFailSafe:
    def test_mercato_senza_griglia_dichiarato(self):
        d = pr.block_distribution([_leg("TENNIS", "1", 1.8),
                                   _leg("1X2", "1", 1.9)], LAM_H, LAM_A)
        assert d["n_legs"] == 1
        assert d["unsupported"][0]["reason"].startswith("mercato_senza_griglia")
        assert "TENNIS" in d["unsupported"][0]["leg"]

    def test_input_ostili_non_sollevano(self):
        legs = [_leg("1X2", "1", "abc"), _leg("1X2", "1", 1.9, stake="x"),
                _leg("1X2", "", 1.9), {"mercato": None, "esito": None},
                _leg("OU", "Over", 1.9)]
        d = pr.block_distribution(legs, LAM_H, LAM_A)
        assert d["error"] == "nessuna_gamba_valida"
        assert len(d["unsupported"]) == len(legs)

    def test_quota_non_valida(self):
        for odds in (1.0, 0.9, -2.0, None, float("nan")):
            with pytest.raises(ValueError):
                pr.parse_leg(_leg("1X2", "1", odds))

    def test_stake_non_valido(self):
        for stake in (0, -1, None, "abc"):
            with pytest.raises(ValueError):
                pr.parse_leg(_leg("1X2", "1", 1.9, stake=stake))

    def test_alias_di_mercato(self):
        assert pr.parse_leg(_leg("H2H", "1", 1.9))["market_type"] == "1X2"
        assert pr.parse_leg(_leg("totals", "Over 2.5", 1.9))["market_type"] == "OU"
        assert pr.parse_leg(_leg("spreads", "Home -0.5", 1.9))["market_type"] == "AH"

    def test_nome_squadra_canonicalizzato(self):
        leg = _leg("1X2", "Inter", 1.9, home="Inter", away="Cagliari")
        assert pr.parse_leg(leg)["selection"] == "1"

    def test_corr_su_nessuna_gamba(self):
        assert pr.correlation_matrix([], LAM_H, LAM_A)["pairs"] == []


# ---------------------------------------------------------------------------
# Ledger: sola lettura, blocchi aperti, cap di produzione
# ---------------------------------------------------------------------------

class TestLedger:
    def test_blocchi_aperti_raggruppati(self, tmp_path):
        path = _temp_db(tmp_path)
        conn = sqlite3.connect(path)
        conn.executemany(
            "INSERT INTO bets (match_id, mercato, esito, price, stake, mode, "
            "esito_finale) VALUES (?,?,?,?,?,?,?)",
            [("m1", "1X2", "1", 1.75, 3.0, "live", None),
             ("m1", "OU", "Over 2.5", 1.95, 2.0, "live", None),
             ("m2", "1X2", "X", 3.4, 1.5, "live", None),
             ("m3", "1X2", "1", 1.6, 2.0, "sim", None),      # SIM: non e' capitale
             ("m4", "1X2", "1", 1.6, 2.0, "live", "won")])   # chiusa
        conn.execute("INSERT INTO match_analysis (match_id, lam_h, lam_a) "
                     "VALUES ('m1', 1.7, 1.0)")
        conn.execute("INSERT INTO matches (id, home_team, away_team) "
                     "VALUES ('m1', 'Inter', 'Cagliari')")
        conn.commit()
        conn.close()

        result = pr.open_blocks(db_path=path)
        assert result["rows"] == 3
        ids = [b["match_id"] for b in result["blocks"]]
        assert ids == ["m1", "m2"]          # ordinati, solo live aperte
        m1 = result["blocks"][0]
        assert len(m1["legs"]) == 2 and m1["lam_h"] == 1.7

    def test_report_misura_il_blocco_e_dichiara_le_lambdas_mancanti(self, tmp_path):
        path = _temp_db(tmp_path)
        conn = sqlite3.connect(path)
        conn.executemany(
            "INSERT INTO bets (match_id, mercato, esito, price, stake, mode, "
            "esito_finale) VALUES (?,?,?,?,?,?,?)",
            [("m1", "1X2", "1", 1.8, 3.0, "live", None),
             ("m1", "OU", "Over 2.5", 2.0, 2.0, "live", None),
             ("m2", "1X2", "1", 1.7, 2.0, "live", None)])
        conn.execute("INSERT INTO match_analysis (match_id, lam_h, lam_a) "
                     "VALUES ('m1', 1.8, 0.9)")
        conn.commit()
        conn.close()

        data = pr.report(db_path=path, bankroll=50.0)
        assert data["error"] is None
        by_id = {b["match_id"]: b for b in data["blocks"]}
        assert by_id["m1"]["distribution"]["n_legs"] == 2
        assert by_id["m1"]["correlations"]["pairs"]
        assert by_id["m2"]["error"] == "lambdas_assenti"
        # il cap di produzione viaggia nel report (letto, non copiato)
        assert data["caps"]["correlation_cap_pct"] == pytest.approx(
            pr.production_caps()["correlation_cap_pct"])

    def test_db_assente_non_solleva(self, tmp_path):
        missing = str(tmp_path / "nope.db")
        assert pr.open_blocks(db_path=missing)["error"] == "read_error"
        assert pr.report(db_path=missing)["error"] == "read_error"
        assert pr.empirical_block_correlation(db_path=missing)["error"] == "read_error"

    def test_connessione_e_read_only(self, tmp_path):
        """Tripwire: sulla connessione usata dal modulo una scrittura DEVE fallire."""
        path = _temp_db(tmp_path)
        conn = pr._ro_conn(path)
        try:
            with pytest.raises(sqlite3.OperationalError):
                conn.execute("UPDATE bets SET stake = 999")
        finally:
            conn.close()

    def test_report_testuale_leggibile(self, tmp_path):
        path = _temp_db(tmp_path)
        conn = sqlite3.connect(path)
        conn.execute("INSERT INTO bets (match_id, mercato, esito, price, stake, "
                     "mode, esito_finale) VALUES ('m1','1X2','1',1.8,4.0,'live',NULL)")
        conn.execute("INSERT INTO match_analysis (match_id, lam_h, lam_a) "
                     "VALUES ('m1', 1.5, 1.2)")
        conn.execute("INSERT INTO matches (id, home_team, away_team) "
                     "VALUES ('m1','Inter','Cagliari')")
        conn.commit()
        conn.close()
        text = pr.format_report(pr.report(db_path=path, bankroll=50.0))
        assert "RISCHIO DI PORTAFOGLIO" in text
        assert "Inter vs Cagliari" in text
        assert "Cap di produzione" in text


# ---------------------------------------------------------------------------
# Correlazione empirica fra partite diverse
# ---------------------------------------------------------------------------

class TestCorrelazioneEmpirica:
    def test_db_vuoto(self, tmp_path):
        out = pr.empirical_block_correlation(db_path=_temp_db(tmp_path))
        assert out["status"] == "no_data" and out["rho"] is None

    def test_blocchi_con_esiti_concordi_danno_correlazione_positiva(self, tmp_path):
        path = _temp_db(tmp_path)
        rows = []
        # 6 giornate x 8 partite: dentro la giornata TUTTE casa o TUTTE trasferta
        for day in range(6):
            home_day = day % 2 == 0
            for i in range(8):
                sh, sa = ((3, 0) if home_day else (0, 2))
                rows.append(("m%d_%d" % (day, i), "Serie A", "H", "A", sh, sa, "1",
                             "2026-09-%02dT20:00:00" % (day + 1)))
        _write_reco(path, rows)
        out = pr.empirical_block_correlation(db_path=path, min_pairs=10)
        assert out["status"] == "ok"
        assert out["rho"] > 0.9            # concordanza quasi perfetta
        assert out["n_pairs"] == 6 * 28

    def test_baseline_dichiarata(self, tmp_path):
        """Giornate con esiti tutti concordi (rho alta) ma nessun trend fra
        giornate: la baseline fra blocchi diversi deve esistere e restare bassa.

        Giornate generate con un seme fisso: un pattern alternato (giorno pari/
        dispari) NON e' "nessun trend" — e' un trend periodico che l'accoppiamento
        a meta' elenco legge come correlazione perfetta. Servono ABBASTANZA
        giornate: con 6 blocchi la baseline e' stimata su ~15 coppie e vale -0.5
        (rumore), con 20 su 100 coppie vale -0.10 (una stima). Un test che
        asserisce una soglia su un campione da rumore e' un test che cade a
        caso — la stessa lezione delle date fisse del 15/09.
        """
        import random
        rng = random.Random(7)
        path = _temp_db(tmp_path)
        rows = []
        for day in range(20):
            home_day = rng.random() < 0.5
            for i in range(5):
                sh, sa = ((2, 0) if home_day else (0, 1))
                rows.append(("m%d_%d" % (day, i), "Serie A", "H", "A", sh, sa, "1",
                             "2026-09-%02dT18:00:00" % ((day % 28) + 1)))
        _write_reco(path, rows)
        out = pr.empirical_block_correlation(db_path=path, min_pairs=5)
        assert out["rho"] == pytest.approx(1.0, abs=1e-9)      # dentro la giornata: concordi
        assert out["baseline_rho"] is not None
        assert out["baseline_pairs"] >= 50
        assert abs(out["baseline_rho"]) < 0.3

    def test_campione_piccolo_dichiarato(self, tmp_path):
        path = _temp_db(tmp_path)
        _write_reco(path, [("m1", "Serie A", "H", "A", 1, 0, "1", "2026-09-01T20:00:00"),
                           ("m2", "Serie A", "H", "A", 0, 0, "X", "2026-09-01T20:00:00")])
        out = pr.empirical_block_correlation(db_path=path, min_pairs=200)
        assert out["status"] == "insufficient"
        assert out["n_pairs"] == 1
        assert "rumore" in out["caveat"]

    def test_righe_sporche_ignorate(self, tmp_path):
        path = _temp_db(tmp_path)
        _write_reco(path, [("m1", "Serie A", "H", "A", None, 0, None, "2026-09-01T20:00:00"),
                           ("m2", "Serie A", "H", "A", "x", "y", None, "2026-09-01T20:00:00"),
                           ("m3", "Serie A", "H", "A", 1, 0, "1", ""),
                           ("m4", "Serie A", "H", "A", 2, 0, "1", "2026-09-01T20:00:00"),
                           ("m5", "Serie A", "H", "A", 0, 1, "2", "2026-09-01T20:00:00")])
        out = pr.empirical_block_correlation(db_path=path, min_pairs=1)
        assert out["status"] == "ok"
        assert out["n_pairs"] == 1          # solo m4/m5 sono usabili

    def test_filtro_giorni(self, tmp_path):
        path = _temp_db(tmp_path)
        _write_reco(path, [("old1", "Serie A", "H", "A", 1, 0, "1", "2020-01-01T20:00:00"),
                           ("old2", "Serie A", "H", "A", 0, 1, "2", "2020-01-01T20:00:00")])
        out = pr.empirical_block_correlation(db_path=path, days=7, min_pairs=1)
        assert out["status"] == "no_data"
        assert out["rows"] == 0


# ---------------------------------------------------------------------------
# Cap di produzione e modalita' di misura
# ---------------------------------------------------------------------------

class TestCapDiProduzione:
    def test_caps_letti_da_auto_bet(self):
        caps = pr.production_caps()
        import auto_bet
        assert caps["correlation_cap_pct"] == pytest.approx(auto_bet.CORRELATION_CAP_PCT)
        assert caps["open_exposure_cap_pct"] == pytest.approx(auto_bet.OPEN_EXPOSURE_CAP_PCT)

    def test_nessuna_soglia_ricopiata_nel_sorgente(self):
        """Il modulo non deve contenere un cap di rischio scritto a mano."""
        code = _code_only(Path(pr.__file__).read_text())
        assert "CORRELATION_CAP_PCT" in code      # letto da auto_bet
        assert "0.30" not in code and "0.60" not in code and "0.70" not in code


def _code_only(src: str) -> str:
    """Solo il CODICE: commenti, docstring e stringhe fuori.

    Il TESTO nomina le regole (e i moduli vietati) proprio per spiegarle: un
    tripwire che guarda i commenti boccia la documentazione, non il codice.
    `tokenize` distingue i token, quindi una stringa o un commento non possono
    far fallire (ne' passare) una verifica sul codice.
    """
    import io
    import tokenize
    out = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            out.append(tok.string)
    except tokenize.TokenError:            # sorgente troncato: nessuna garanzia
        return src
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Tripwire: nessun percorso d'ordine, nessuna rete, sola lettura
# ---------------------------------------------------------------------------

class TestTripwire:
    def test_nessuna_scrittura_o_ordine_nel_sorgente(self):
        # il docstring e' escluso: nomina le regole proprio per spiegarle
        code_only = _code_only(Path(pr.__file__).read_text())
        for banned in ("INSERT", "UPDATE ", "DELETE FROM",
                       "place_limit_order", "_live_fill", "save_bet",
                       "resolve_market_for", "execution_engine"):
            assert banned not in code_only, banned
        for net in ("requests", "httpx", "aiohttp", "urlopen", "odds_api",
                    "sx_signals"):
            assert net not in code_only, net

    def test_import_leggero_in_subprocess(self):
        out = subprocess.run(
            [sys.executable, "-c",
             "import sys, portfolio_risk; "
             "bad=[m for m in ('auto_bet','bot','tracker','requests','odds_api',"
             "'sx_signals','execution_engine') if m in sys.modules]; "
             "print(','.join(bad))"],
            capture_output=True, text=True, cwd=str(Path(pr.__file__).parent))
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == ""

    def test_numpy_solamente_e_nessuna_dipendenza_pesante(self):
        src = Path(pr.__file__).read_text()
        for heavy in ("import xgboost", "import lightgbm", "import sklearn", "import pydantic"):
            assert heavy not in src


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class TestCli:
    def test_blocco_ad_hoc_json(self, capsys):
        rc = pr.main(["--block", "1X2:1:1.90:5", "--block", "OU:Over 2.5:1.95:5",
                      "--lambdas", "1.6", "1.1", "--json"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["distribution"]["n_legs"] == 2
        assert payload["correlations"]["pairs"][0]["rho"] > 0

    def test_blocco_ad_hoc_testuale(self, capsys):
        rc = pr.main(["--block", "OU:Under 3.0:2.10:10", "--lambdas", "1.2", "1.4",
                      "--bankroll", "50"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "P/L atteso" in out and "bankroll" in out

    def test_gamba_malformata_exit_2(self, capsys):
        assert pr.main(["--block", "nonsense", "--lambdas", "1.6", "1.1"]) == 2
        assert pr.main(["--block", "1X2:1:1.9:5"]) == 2      # manca --lambdas

    def test_report_su_db_temporaneo(self, tmp_path, capsys):
        path = _temp_db(tmp_path)
        rc = pr.main(["--db", path, "--json"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["blocks"] == []
        assert payload["cross_match"]["status"] == "no_data"
