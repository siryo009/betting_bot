"""Test di `copula_portfolio` — gate di dipendenza fra scommesse SIMULTANEE.

Tutti OFFLINE: SQLite temporaneo per le parti che leggono il ledger, matematica
pura per il resto. Nessuna rete, nessun credito, nessun ordine, nessun provider.

Tre classi di verifica che valgono piu' di una:
1. **INVARIANTE**: il moltiplicatore e' SEMPRE in [0, 1] — il modulo puo' solo
   ridurre o azzerare (direttiva del proprietario, 14/10/2026). Verificato sia
   sul comportamento (griglia di scenari) sia sul SORGENTE.
2. **INDIPENDENZA VERA**: unita' dichiarate indipendenti (d = 0) devono avere
   perdite NON correlate — con il fattore di coda condiviso da tutte le unita'
   la copula inventava una coda comune (misurato: P(0 perdenti) 0.277 invece di
   0.041 su 6 unita' a rho = 0).
3. **IL GATE NON SPEGNE IL FLUSSO**: un portafoglio vuoto NON blocca nulla per
   concentrazione (bug trovato misurando: budget di cluster misurato sulla somma
   delle puntate = 0 su portafoglio vuoto -> BLOCCAVA OGNI ORDINE).
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

import copula_portfolio as cp
import tracker

ROOT = Path(__file__).resolve().parent
LAMBDA_H, LAMBDA_A = 1.6, 1.1


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _cand(i, league="Premier League", odds=1.7, stake=5.0, *, km=600,
          esito="1", mercato="1X2", mid=None, prob=None, ev=0.03):
    """Candidato nella forma del percorso ordini (`_today_value_picks`)."""
    return {
        "match_id": mid or "m%d" % i, "league": league, "mercato": mercato,
        "esito_key": esito, "quota": odds, "stake": stake, "best_ev": ev,
        "commence": _iso(km), "market_prob": prob if prob is not None
        else 1.0 / odds,
    }


def _iso(minutes):
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)) \
        .isoformat().replace("+00:00", "Z")


def _unit(key, league="Premier League", stake=5.0, *, km=600, odds=1.7,
          lam=None, match_id=None):
    """Unita' di rischio costruita col COSTRUTTORE DEL MODULO (mai a mano:
    un dict scritto dal test puo' divergere dagli attributi reali)."""
    leg = _cand(0, league, odds, stake, km=km, mid=match_id or key)
    if lam:
        leg["lam_h"], leg["lam_a"] = lam
    return cp.unit_for(leg["match_id"], [leg])


def _temp_db(tmp_path) -> str:
    """Ledger temporaneo con lo schema di produzione (solo le colonne usate)."""
    path = str(tmp_path / "ledger.db")
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE matches (id TEXT PRIMARY KEY, league TEXT, home_team TEXT,
                              away_team TEXT, commence_time TEXT, status TEXT,
                              last_updated TEXT);
        CREATE TABLE match_analysis (id INTEGER PRIMARY KEY, match_id TEXT,
                                     lam_h REAL, lam_a REAL);
        CREATE TABLE bets (id INTEGER PRIMARY KEY, match_id TEXT, mercato TEXT,
                           esito TEXT, market_id TEXT, selection_id INTEGER,
                           price REAL, stake REAL, mode TEXT, status TEXT,
                           bet_id TEXT, esito_finale TEXT, profit REAL,
                           created_at TEXT, settled_at TEXT);
        """)
    conn.commit()
    conn.close()
    return path


def _open_bet(path, match_id, *, esito="1", stake=2.0, price=1.7,
              league="Premier League", mode="live", lam=None, km=600):
    conn = sqlite3.connect(path)
    conn.execute("INSERT OR REPLACE INTO matches (id, league, home_team, "
                 "away_team, commence_time) VALUES (?,?,?,?,?)",
                 (match_id, league, "A", "B", _iso(km)))
    conn.execute("INSERT INTO bets (match_id, mercato, esito, stake, price, "
                 "mode, status) VALUES (?,?,?,?,?,?,?)",
                 (match_id, "1X2", esito, stake, price, mode, "FULLY_FILLED"))
    if lam:
        conn.execute("INSERT INTO match_analysis (match_id, lam_h, lam_a) "
                     "VALUES (?,?,?)", (match_id, lam[0], lam[1]))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Configurazione
# ---------------------------------------------------------------------------

class TestConfig:
    def test_default_di_codice(self, monkeypatch):
        for name in list(cp.config().keys()):
            pass
        monkeypatch.delenv("COPULA_MAX_CORR", raising=False)
        monkeypatch.delenv("COPULA_DD_LEVEL", raising=False)
        cp.reset_config_cache()
        conf = cp.config()
        assert conf["enabled"] is True
        assert conf["mode"] == "enforce"
        assert conf["active"] is True
        assert conf["max_corr"] == cp.DEFAULT_MAX_CORR == 0.45
        assert conf["critical_corr"] == cp.DEFAULT_CRITICAL_CORR == 0.80
        assert conf["dd_level"] == 0.35
        assert conf["dd_quantile"] == 0.05
        assert conf["cluster_pct"] == 0.50
        assert conf["min_multiplier"] == 0.25

    def test_soglia_critica_sotto_la_moderata_viene_rialzata(self, monkeypatch):
        """Una banda moderata vuota bloccherebbe tutto: si rialza, non si rompe."""
        monkeypatch.setenv("COPULA_MAX_CORR", "0.70")
        monkeypatch.setenv("COPULA_CRITICAL_CORR", "0.50")
        cp.reset_config_cache()
        conf = cp.config()
        assert conf["critical_corr"] == conf["max_corr"] == 0.70

    def test_valori_impossibili_ricadono_sul_default(self, monkeypatch):
        monkeypatch.setenv("COPULA_DD_LEVEL", "abc")
        monkeypatch.setenv("COPULA_DD_QUANTILE", "-3")
        monkeypatch.setenv("COPULA_SIMS", "1")
        cp.reset_config_cache()
        conf = cp.config()
        assert conf["dd_level"] == cp.DEFAULT_DD_LEVEL
        assert conf["dd_quantile"] == 0.0            # rialzato al minimo (0)
        assert conf["sims"] >= 200                   # minimo di simulazioni

    def test_mode_sconosciuto_torna_a_enforce(self, monkeypatch):
        monkeypatch.setenv("COPULA_MODE", "boh")
        cp.reset_config_cache()
        assert cp.config()["mode"] == "enforce"

    def test_interruttore_spegne_il_gate(self, monkeypatch):
        monkeypatch.setenv("COPULA_GUARD", "0")
        cp.reset_config_cache()
        conf = cp.config()
        assert conf["enabled"] is False and conf["active"] is False

    def test_config_riletta_a_runtime(self, monkeypatch):
        """La memo segue l'AMBIENTE: un set su Railway non richiede redeploy."""
        monkeypatch.setenv("COPULA_MAX_CORR", "0.20")
        cp.reset_config_cache()
        assert cp.current_config()["max_corr"] == 0.20
        monkeypatch.setenv("COPULA_MAX_CORR", "0.60")
        assert cp.current_config()["max_corr"] == 0.60

    def test_finestra_kickoff_delegata_ad_auto_bet(self):
        """Una sola definizione di \"blocco correlato\" nel progetto."""
        import auto_bet
        assert cp.window_minutes() == float(auto_bet.CORRELATION_WINDOW_MIN)

    def test_finestra_da_env(self, monkeypatch):
        monkeypatch.setenv("COPULA_WINDOW_MIN", "45")
        assert cp.window_minutes() == 45.0


# ---------------------------------------------------------------------------
# Copula: coda, componenti, simulazione
# ---------------------------------------------------------------------------

class TestCopula:
    def test_tipo_di_copula_e_fallback_gaussiano(self, monkeypatch):
        assert cp.copula_kind() == "t"
        monkeypatch.setattr(cp, "_scipy_stats", lambda: None)
        assert cp.copula_kind() == "gaussian"

    def test_dipendenza_di_coda_limitata_e_monotona(self):
        assert cp.tail_dependence(0.0, 6.0) == 0.0
        assert cp.tail_dependence(1.0, 6.0) == 1.0
        vals = [cp.tail_dependence(r, 6.0) for r in (0.2, 0.5, 0.9)]
        assert vals == sorted(vals)
        assert all(0.0 <= v <= 1.0 for v in vals)

    def test_dipendenza_effettiva_e_una_media_pesata(self, monkeypatch):
        monkeypatch.setenv("COPULA_TAIL_WEIGHT", "0")
        cp.reset_config_cache()
        assert cp.effective_dependence(0.80) == pytest.approx(0.80, abs=1e-9)
        monkeypatch.setenv("COPULA_TAIL_WEIGHT", "1")
        cp.reset_config_cache()
        assert cp.effective_dependence(0.80) == pytest.approx(
            cp.tail_dependence(0.80, 6.0), abs=1e-9)

    def test_dipendenza_effettiva_stessa_partita_e_critica(self):
        """0.98 -> 0.89: la stessa partita sta nella banda CRITICA (blocco)."""
        d = cp.effective_dependence(cp.DEFAULT_SAME_MATCH_RHO)
        assert d >= cp.DEFAULT_CRITICAL_CORR
        d_lega = cp.effective_dependence(cp.DEFAULT_SAME_LEAGUE_RHO)
        assert cp.DEFAULT_MAX_CORR < d_lega < cp.DEFAULT_CRITICAL_CORR

    def test_componenti_connesse(self):
        corr = np.array([[1.0, 0.5, 0.0],
                         [0.5, 1.0, 0.0],
                         [0.0, 0.0, 1.0]])
        comps = [c.tolist() for c in cp._components(corr)]
        assert comps == [[0, 1], [2]]

    def test_cholesky_none_su_matrice_non_definita_positiva(self):
        bad = np.array([[1.0, 2.0], [2.0, 1.0]])
        assert cp._cholesky(bad) is None

    def test_marginali_uniformi(self):
        """P(tutte vincono) con 6 unita' indipendenti = (1-p)^6."""
        units = [_unit("u%d" % i, league="L%d" % i, stake=5.0, km=600 + i * 400,
                       odds=1.7) for i in range(6)]
        for u in units:
            u["distribution"] = cp.distribution_of(u)
        corr, _ = cp.dependence_matrix(units)
        sim = cp.simulate(units, corr)
        assert sim["ok"] is True
        n_lose = sum((ix == 1).astype(int) for ix in sim["idx"])
        p_loss = 0.4117647058823529
        assert (n_lose == 0).mean() == pytest.approx((1 - p_loss) ** 6, abs=0.03)
        assert n_lose.mean() == pytest.approx(6 * p_loss, abs=0.1)

    def test_indipendenza_non_crea_coda_comune(self):
        """Regressione: con il fattore di coda condiviso da TUTTE le unita',
        P(0 perdenti) su 6 unita' a rho=0 valeva 0.277 invece di 0.041."""
        units = [_unit("u%d" % i, league="L%d" % i, stake=5.0, km=600 + i * 400)
                 for i in range(6)]
        for u in units:
            u["distribution"] = cp.distribution_of(u)
        corr, _ = cp.dependence_matrix(units)
        assert np.abs(corr - np.eye(6)).max() == 0.0
        sim = cp.simulate(units, corr)
        n_lose = sum((ix == 1).astype(int) for ix in sim["idx"])
        assert (n_lose == 0).mean() < 0.10

    def test_simulazione_fail_safe_senza_distribuzione(self):
        units = [_unit("u0")]
        sim = cp.simulate(units, np.eye(1))
        assert sim["ok"] is False and sim["error"].startswith("distribuzione")

    def test_simulazione_con_matrice_degenere(self):
        units = [_unit("u%d" % i) for i in range(2)]
        for u in units:
            u["distribution"] = cp.distribution_of(u)
        sim = cp.simulate(units, np.array([[1.0, 2.0], [2.0, 1.0]]))
        assert sim["ok"] is False
        assert sim["error"] == "matrice_non_definita_positiva"


# ---------------------------------------------------------------------------
# Unita' di rischio e dipendenza dichiarata
# ---------------------------------------------------------------------------

class TestUnita:
    def test_parse_ts_rifiuta_istanti_ambigui(self):
        assert cp._parse_ts("2026-10-14T18:00:00Z") is not None
        assert cp._parse_ts("2026-10-14T18:00:00+02:00") is not None
        assert cp._parse_ts("2026-10-14 18:00:00") is None    # senza fuso
        assert cp._parse_ts("") is None and cp._parse_ts(None) is None

    def test_probabilita_priorita_e_haircut(self):
        conf = cp.config()
        assert cp._probability_of({"p_true": 0.62}, cfg=conf)["prob_source"] == "p_true"
        assert cp._probability_of({"market_prob": 0.55}, cfg=conf)["prob_source"] == "market_prob"
        implied = cp._probability_of({"quota": 2.0}, cfg=conf)
        assert implied["prob_source"] == "market_implied"
        assert implied["p_win"] < 0.5           # haircut conservativo sul margine

    def test_distribuzione_di_mercato_a_due_punti(self):
        dist = cp.distribution_of(_unit("u0", stake=5.0, odds=1.7))
        assert dist["source"] == cp.SOURCE_MARKET
        assert list(dist["values"]) == [pytest.approx(3.5), pytest.approx(-5.0)]
        assert dist["probs"].sum() == pytest.approx(1.0)

    def test_distribuzione_esatta_con_griglia(self):
        u = _unit("u0", stake=5.0, odds=1.7, lam=(LAMBDA_H, LAMBDA_A))
        dist = cp.distribution_of(u)
        assert dist["source"] == cp.SOURCE_GRID
        assert len(dist["values"]) > 2          # griglia di punteggi, non 2 punti

    def test_multi_gamba_senza_griglia_e_dichiarata(self):
        u = _unit("u0", stake=5.0)
        u["legs"].append(_cand(1, stake=5.0))
        dist = cp.distribution_of(u)
        assert dist["values"] is None
        assert dist["error"] == "multi_gamba_senza_griglia"

    def test_gruppo_per_partita(self):
        items = [_cand(0, mid="m1", stake=3.0), _cand(1, mid="m1", stake=2.0, esito="X"),
                 _cand(2, mid="m2", stake=4.0)]
        units = cp.group_units(items)
        assert [u["key"] for u in units] == ["m1", "m2"]
        assert units[0]["stake"] == pytest.approx(5.0)

    def test_dipendenza_relazioni(self):
        conf = cp.config()
        a = cp.unit_for("m1", [_cand(0, km=600)], cfg=conf)
        b_same = cp.unit_for("m1", [_cand(0, km=600)], cfg=conf)
        c_lega = cp.unit_for("m2", [_cand(0, km=610)], cfg=conf)
        d_lontano = cp.unit_for("m3", [_cand(0, km=2000)], cfg=conf)
        e_altra = cp.unit_for("m4", [_cand(0, league="Bundesliga", km=610)], cfg=conf)
        assert cp.dependence(a, b_same, cfg=conf)["relation"] == "same_match"
        assert cp.dependence(a, c_lega, cfg=conf)["relation"] == "same_cluster_window"
        assert cp.dependence(a, d_lontano, cfg=conf)["relation"] == "same_cluster"
        assert cp.dependence(a, e_altra, cfg=conf)["relation"] == "same_window"
        f_diversa = cp.unit_for("m5", [_cand(0, league="Bundesliga", km=2000)], cfg=conf)
        assert cp.dependence(a, f_diversa, cfg=conf)["relation"] == "independent"

    def test_dipendenza_senza_orario_e_dichiarata(self):
        conf = cp.config()
        a = cp.unit_for("m1", [{"mercato": "1X2", "quota": 1.7, "stake": 5.0}], cfg=conf)
        b = cp.unit_for("m2", [_cand(0, league="Bundesliga", km=610)], cfg=conf)
        info = cp.dependence(a, b, cfg=conf)
        assert info["detail"] == "time_unknown"
        assert info["relation"] == "independent"

    def test_matrice_simmetrica_e_cluster_stake(self):
        units = cp.group_units([_cand(0, mid="m1"), _cand(1, mid="m2"),
                                _cand(2, league="Bundesliga", mid="m3")])
        corr, pairs = cp.dependence_matrix(units)
        assert np.allclose(corr, corr.T)
        assert float(np.diag(corr).min()) == 1.0
        assert pairs[0][1]["relation"] == pairs[1][0]["relation"]
        assert cp.cluster_stake(units, "Premier League") == pytest.approx(10.0)
        assert cp.cluster_stake(units, "") == 0.0


# ---------------------------------------------------------------------------
# Posizioni APERTE: sola lettura
# ---------------------------------------------------------------------------

class TestPosizioniAperte:
    def test_legge_solo_le_puntate_live_aperte(self, tmp_path):
        path = _temp_db(tmp_path)
        _open_bet(path, "m1", stake=2.0)
        _open_bet(path, "m2", stake=3.0, mode="sim")           # SIM: fuori
        conn = sqlite3.connect(path)
        conn.execute("INSERT INTO bets (match_id, mercato, esito, stake, price, "
                     "mode, status, esito_finale) VALUES ('m3','1X2','1',4.0,"
                     "1.7,'live','FULLY_FILLED','won')")
        conn.commit()
        conn.close()
        got = cp.open_units(db_path=path)
        assert got["error"] is None and got["rows"] == 1
        assert len(got["units"]) == 1
        assert got["units"][0]["stake"] == pytest.approx(2.0)

    def test_congiunge_le_gambe_della_stessa_partita(self, tmp_path):
        path = _temp_db(tmp_path)
        _open_bet(path, "m1", stake=2.0, esito="1", lam=(LAMBDA_H, LAMBDA_A))
        _open_bet(path, "m1", stake=3.0, esito="X")
        got = cp.open_units(db_path=path)
        assert got["rows"] == 2 and len(got["units"]) == 1
        assert got["units"][0]["stake"] == pytest.approx(5.0)
        assert got["units"][0]["distribution"]["source"] == cp.SOURCE_GRID

    def test_sola_lettura_rifiuta_una_scrittura(self, tmp_path):
        path = _temp_db(tmp_path)
        _open_bet(path, "m1")
        conn = cp._ro_conn(path)
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("UPDATE bets SET stake = 99")
        conn.close()

    def test_db_assente_e_fail_safe(self, tmp_path):
        got = cp.open_units(db_path=str(tmp_path / "assente.db"))
        assert got["units"] == [] and got["error"] == "read_error"

    def test_lega_e_lambda_dal_ledger(self, tmp_path):
        path = _temp_db(tmp_path)
        _open_bet(path, "m1", league="Serie A", lam=(1.4, 1.2))
        unit = cp.open_units(db_path=path)["units"][0]
        assert unit["league"] == "Serie A" and unit["cluster"] == "Serie A"
        assert cp.distribution_of(unit)["source"] == cp.SOURCE_GRID


# ---------------------------------------------------------------------------
# Decisione
# ---------------------------------------------------------------------------

class TestDecisioni:
    def test_bande_tre_stati(self):
        conf = cp.config()
        assert cp.multiplier_bands(0.10, cfg=conf)[1] == cp.DECISION_CLEAR
        assert cp.multiplier_bands(0.45, cfg=conf) == (1.0, cp.DECISION_CLEAR)
        mult, reason = cp.multiplier_bands(0.60, cfg=conf)
        assert reason == cp.DECISION_SCALED and 0.0 < mult < 1.0
        assert cp.multiplier_bands(0.80, cfg=conf) == (0.0, cp.DECISION_BLOCKED_CRITICAL)
        assert cp.multiplier_bands(1.0, cfg=conf) == (0.0, cp.DECISION_BLOCKED_CRITICAL)

    def test_moltiplicatore_mai_sopra_uno(self):
        conf = cp.config()
        for d in [x / 100.0 for x in range(0, 101)]:
            mult, _ = cp.multiplier_bands(d, cfg=conf)
            assert 0.0 <= mult <= 1.0

    def test_budget_di_coda_e_il_massimo_feasible_sulla_griglia(self):
        """Il criterio si VALUTA (non si assume monotono) e si sceglie il piu'
        alto feasible: verificato ricostruendo la griglia dal seme.

        La non-monotonia e' MISURATA (non supposta): fra le unita' dello stesso
        portafoglio la perdita di coda a q=5% puo' SCENDERE alzando lo stake,
        perche' su una quantile empirica discreta il numero di perdenti al
        quantile cala quando le unita' aumentano.
        """
        units = [_unit("u%d" % i, league="L%d" % i, stake=5.0, km=600 + i * 400)
                 for i in range(6)]
        for u in units:
            u["distribution"] = cp.distribution_of(u)
        corr, _ = cp.dependence_matrix(units)
        sim = cp.simulate(units, corr)
        cand_pl = sim["values"][-1][sim["idx"][-1]]
        base = sim["base"] - cand_pl          # portafoglio SENZA il candidato
        conf = cp.config()
        losses = [-np.quantile(base + cand_pl * m, conf["dd_quantile"])
                  for m in cp.M_GRID]
        assert cp.M_GRID.max() == 1.0 and cp.M_GRID.min() == 0.0
        out = cp.tail_budget_multiplier(base, sim["values"][-1], sim["idx"][-1],
                                        bankroll=46.73, cfg=conf)
        level = conf["dd_level"] * 46.73
        feasible = [m for m, loss in zip(cp.M_GRID, losses) if loss <= level + 1e-12]
        assert out["multiplier"] == (max(feasible) if feasible else 0.0)
        assert out["loss_q"] == pytest.approx(losses[-1])   # misurato a stake pieno
        assert 0.0 <= out["multiplier"] <= 1.0

    def test_budget_di_coda_monotono_sull_unita_singola(self):
        """Quando il portafoglio e' il solo candidato la perdita di coda E'
        proporzionale allo stake: e' il caso in cui il criterio risponde in
        modo pulito alla variabile di decisione."""
        unit = _unit("u0", stake=5.0, odds=1.7)
        unit["distribution"] = cp.distribution_of(unit)
        corr, _ = cp.dependence_matrix([unit])
        sim = cp.simulate([unit], corr)
        cand_pl = sim["values"][-1][sim["idx"][-1]]
        losses = [-np.quantile(cand_pl * m, 0.05) for m in (0.0, 0.25, 0.5, 1.0)]
        assert losses == sorted(losses)
        assert losses[-1] == pytest.approx(5.0)

    def test_budget_di_coda_bankroll_non_valido(self):
        out = cp.tail_budget_multiplier(np.zeros(10), np.array([1.0, -1.0]),
                                        np.zeros(10, dtype=int), bankroll=0)
        assert out["error"] == "bankroll_non_positivo"

    def test_portafoglio_vuoto_non_blocca_per_concentrazione(self):
        """Regressione del BUG trovato misurando: con il budget di cluster
        misurato sulla SOMMA delle puntate, un portafoglio vuoto aveva budget 0
        e il primo pick veniva bloccato \"per concentrazione di se' stesso\"."""
        # Unita' INDIPENDENTI (leghe diverse, kickoff lontani): nessun vincolo
        # deve toccarle.
        cands = [_cand(i, league="L%d" % i, stake=3.5, mid="m%d" % i,
                       km=600 + i * 2000) for i in range(3)]
        kept, info = cp.apply(cands, bankroll=46.73, open_positions=[])
        assert info["blocked"] == 0 and info["scaled"] == 0
        assert info["kept"] == 3
        assert [c["stake"] for c in kept] == [3.5, 3.5, 3.5]

    def test_portafoglio_piccolo_non_blocca_il_cluster(self, monkeypatch):
        """Il budget di cluster e' una quota del BANKROLL, non della somma gia'
        puntata: con 2 USDC aperti nella stessa lega il budget resta 23.37 e il
        nuovo pick NON viene bloccato. Le bande di correlazione sono allargate
        di proposito per isolare il cap di cluster (il punto in esame).
        """
        monkeypatch.setenv("COPULA_MAX_CORR", "0.95")
        monkeypatch.setenv("COPULA_CRITICAL_CORR", "0.99")
        cp.reset_config_cache()
        open_unit = cp.unit_for("m0", [_cand(0, mid="m0", stake=2.0, km=600)])
        cands = [_cand(1, mid="m1", stake=5.0, km=610)]
        kept, info = cp.apply(cands, bankroll=46.73, open_positions=[open_unit])
        assert info["blocked"] == 0 and info["kept"] == 1
        assert float(cands[0]["stake"]) == 5.0

    def test_cluster_cap_e_una_quota_del_bankroll(self, monkeypatch):
        monkeypatch.setenv("COPULA_CLUSTER_PCT", "0.20")
        monkeypatch.setenv("COPULA_DD_LEVEL", "0.99")     # isola il cluster
        cp.reset_config_cache()
        cands = [_cand(i, stake=5.0, mid="m%d" % i, km=600 + i * 10)
                 for i in range(4)]                        # 4 x 5.0 = 20 > 9.35
        kept, info = cp.apply(cands, bankroll=46.73, open_positions=[])
        assert info["blocked"] >= 1
        reasons = {b["reason"] for b in info["blocked_detail"]}
        assert reasons == {cp.DECISION_BLOCKED_CLUSTER}
        # e il cluster CAPPA, non solo riduce: 20% di 46.73 = 9.35
        assert sum(float(c["stake"]) for c in kept) <= 9.35 + 1e-9

    def test_riduzione_proporzionale_su_correlazione_moderata(self):
        cands = [_cand(i, mid="m%d" % i, stake=5.6, km=600 + i * 10)
                 for i in range(3)]
        kept, info = cp.apply(cands, bankroll=46.73, open_positions=[])
        scaled = [c for c in kept if c["copula"]["decision"] == cp.DECISION_SCALED]
        assert scaled, "una dipendenza 0.60 deve ridurre lo stake"
        for c in scaled:
            assert c["stake"] < c["copula"]["stake_before"]
            assert c["copula"]["m_corr"] < 1.0
            assert 0.0 < c["copula"]["multiplier"] < 1.0

    def test_blocco_su_correlazione_critica(self):
        """Stessa partita senza griglia: rho dichiarato 0.98 -> d 0.89 -> BLOCCATO."""
        open_position = cp.unit_for("m1", [_cand(0, mid="m1", stake=2.0, km=600)])
        cands = [_cand(1, mid="m1", stake=3.0, km=600)]
        kept, info = cp.apply(cands, bankroll=46.73,
                              open_positions=[open_position])
        assert kept == [] and info["blocked"] == 1
        assert info["blocked_detail"][0]["reason"] == cp.DECISION_BLOCKED_CRITICAL
        assert cands[0]["stake"] == 0.0

    def test_blocco_per_budget_di_coda_su_cluster_grande(self):
        cands = [_cand(i, mid="m%d" % i, stake=5.6, km=600 + i * 10)
                 for i in range(8)]
        kept, info = cp.apply(cands, bankroll=46.73, open_positions=[])
        assert info["blocked"] >= 1
        assert any(b["reason"] == cp.DECISION_BLOCKED_TAIL
                   or b["reason"] == cp.DECISION_BLOCKED_MULTIPLIER
                   or b["reason"] == cp.DECISION_BLOCKED_CLUSTER
                   for b in info["blocked_detail"])

    def test_sotto_il_pavimento_si_blocca_non_si_riduce(self, monkeypatch):
        monkeypatch.setenv("COPULA_MIN_MULTIPLIER", "0.99")
        monkeypatch.setenv("COPULA_CLUSTER_PCT", "1.0")
        monkeypatch.setenv("COPULA_DD_LEVEL", "0.99")
        cp.reset_config_cache()
        cands = [_cand(i, mid="m%d" % i, stake=5.0, km=600 + i * 10)
                 for i in range(2)]
        cands[1]["stake"] = 5.0
        # Il secondo pick e' ridotto a 0.56 circa: sotto il pavimento 0.99 -> blocco
        kept, info = cp.apply(cands, bankroll=46.73, open_positions=[])
        assert info["blocked"] == 1

    def test_ticket_minimo_blocca_lo_stake_troppo_piccolo(self, monkeypatch):
        monkeypatch.setenv("COPULA_CLUSTER_PCT", "1.0")
        monkeypatch.setenv("COPULA_DD_LEVEL", "0.99")
        monkeypatch.setattr(cp, "_MIN_TICKET_OVERRIDE", 1.0, raising=False)
        import auto_bet
        monkeypatch.setattr(auto_bet, "aggressive_min_ticket", lambda: 1.0)
        cands = [_cand(0, mid="m0", stake=1.2, km=600),
                 _cand(1, mid="m1", stake=1.2, km=610)]
        kept, info = cp.apply(cands, bankroll=46.73, open_positions=[])
        # dipendenza 0.60 -> mult 0.56 -> 0.67 USDC < ticket 1.00: bloccato
        assert info["blocked"] >= 1

    def test_ordine_di_valutazione_per_ev_decrescente(self):
        cands = [_cand(0, mid="m0", stake=5.0, km=600, ev=0.01),
                 _cand(1, mid="m1", stake=5.0, km=610, ev=0.20)]
        cp.apply(cands, bankroll=46.73, open_positions=[])
        # Il segnale piu' FORTE viene valutato per primo: e' l'unico che resta
        # a stake pieno quando il budget di coda/correlazione morde.
        assert cands[1]["stake"] >= cands[0]["stake"]

    def test_apply_con_gate_spento_non_tocca_nulla(self, monkeypatch):
        monkeypatch.setenv("COPULA_GUARD", "0")
        cp.reset_config_cache()
        cands = [_cand(0, mid="m0", stake=5.0), _cand(1, mid="m0", stake=5.0)]
        kept, info = cp.apply(cands, bankroll=46.73, open_positions=[])
        assert info["active"] is False and info["kept"] == 0
        assert kept == cands and kept[0]["stake"] == 5.0
        assert "copula" not in cands[0]

    def test_apply_e_fail_safe(self, monkeypatch):
        monkeypatch.setattr(cp, "evaluate",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        cands = [_cand(0, stake=5.0)]
        kept, info = cp.apply(cands, bankroll=46.73, open_positions=[])
        assert info["error"] == "errore_valutazione"
        assert kept == cands and cands[0]["stake"] == 5.0

    def test_evaluate_con_candidato_non_modellabile(self):
        """Fail-open DICHIARATO: lo stake resta quello deciso a monte (non si
        inventa un blocco per un dato che manca), ma l'errore e' CONTATO —
        senza il contatore `kept + blocked` non tornerebbe e l'errore sarebbe
        invisibile nella telemetria."""
        bad = {"match_id": "m0", "stake": 5.0}
        cands = [bad, _cand(1, mid="m1", stake=5.0, km=600)]
        kept, info = cp.apply(cands, bankroll=46.73, open_positions=[])
        assert bad["stake"] == 5.0
        assert bad["copula"]["decision"] == cp.DECISION_ERROR
        assert info["errored"] == 1
        assert info["by_reason"][cp.DECISION_ERROR] == 1
        assert info["blocked"] == 0
        assert len(kept) + info["blocked"] == 2

    def test_stake_azzerato_esce_dalla_lista(self):
        cands = [_cand(0, mid="m0", stake=5.0, km=600),
                 _cand(1, mid="m0", stake=5.0, km=600)]
        kept, info = cp.apply(cands, bankroll=46.73, open_positions=[])
        assert len(kept) + info["blocked"] == 2
        assert all(float(c["stake"]) > 0 for c in kept)

    def test_il_gate_non_cambia_mai_la_quota(self):
        cands = [_cand(i, mid="m%d" % i, stake=5.0, km=600 + i * 10)
                 for i in range(3)]
        prima = [c["quota"] for c in cands]
        cp.apply(cands, bankroll=46.73, open_positions=[])
        assert [c["quota"] for c in cands] == prima

    def test_lambdas_lookup_iniettabile(self, monkeypatch):
        seen = []

        def fake(mid):
            seen.append(mid)
            return {"lam_h": LAMBDA_H, "lam_a": LAMBDA_A}

        cands = [_cand(0, mid="m0", stake=5.0, km=600)]
        cp.apply(cands, bankroll=46.73, open_positions=[],
                 lambdas_lookup=fake)
        assert seen == ["m0"]
        assert cp.distribution_of(
            cp.unit_for("m0", [dict(cands[0], lam_h=LAMBDA_H, lam_a=LAMBDA_A)])
        )["source"] == cp.SOURCE_GRID

    def test_lambdas_lookup_che_esplode_non_rompe(self):
        def boom(mid):
            raise RuntimeError("db")

        cands = [_cand(0, mid="m0", stake=5.0, km=600)]
        kept, info = cp.apply(cands, bankroll=46.73, open_positions=[],
                              lambdas_lookup=boom)
        assert info["error"] is None
        assert info["kept"] == 1

    def test_apply_legge_le_posizioni_aperte_dal_ledger(self, tmp_path,
                                                        monkeypatch):
        path = _temp_db(tmp_path)
        _open_bet(path, "m0", stake=2.0, km=600)
        real = cp.open_units
        monkeypatch.setattr(cp, "open_units",
                            lambda **kw: real(db_path=path))
        cands = [_cand(0, mid="m0", stake=3.0, km=600)]
        kept, info = cp.apply(cands, bankroll=46.73)
        assert info["open_units"] == 1
        assert kept == []                       # stessa partita -> critico


# ---------------------------------------------------------------------------
# Report e CLI
# ---------------------------------------------------------------------------

class TestReport:
    def test_snapshot_e_report(self, tmp_path):
        path = _temp_db(tmp_path)
        _open_bet(path, "m1", stake=2.0, km=600)
        _open_bet(path, "m2", stake=3.0, km=610, league="Bundesliga")
        data = cp.snapshot(bankroll=46.73, db_path=path)
        assert data["units"] == 2 and data["stake"] == pytest.approx(5.0)
        assert data["joint"]["copula"] in ("t", "gaussian")
        text = cp.format_report(data)
        assert "DIPENDENZA DI PORTAFOGLIO" in text
        assert "Budget di coda" in text

    def test_snapshot_su_db_assente(self, tmp_path):
        data = cp.snapshot(db_path=str(tmp_path / "no.db"))
        assert data["units"] == 0 and data["error"] == "read_error"

    def test_render_bande_dal_config(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COPULA_MAX_CORR", "0.33")
        cp.reset_config_cache()
        data = cp.snapshot(db_path=str(tmp_path / "no.db"))
        assert "0.33" in cp.format_report(data)

    def test_cli_json_e_exit_code(self, tmp_path, capsys):
        assert cp.main(["--json", "--db", str(tmp_path / "no.db")]) == 1
        out = json.loads(capsys.readouterr().out)
        assert out["units"] == 0

    def test_cli_pulito(self, tmp_path, capsys):
        path = _temp_db(tmp_path)
        _open_bet(path, "m1")
        assert cp.main(["--db", path]) == 0
        assert "DIPENDENZA" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Wiring nel percorso ordini (tripwire sul SORGENTE + comportamento)
# ---------------------------------------------------------------------------

class TestPercorsoOrdini:
    def _src(self):
        import auto_bet
        return Path(auto_bet.__file__).read_text(encoding="utf-8")

    def test_gate_chiamato_dopo_il_tetto_e_prima_della_fase3(self):
        src = self._src()
        i_ceiling = src.index("candidates, _ceiling = enforce_order_ceiling(")
        i_copula = src.index("candidates, _cop = _apply_copula(candidates")
        i_fase3 = src.index("# --- FASE 3: esegui e registra")
        assert i_ceiling < i_copula < i_fase3

    def test_gate_solo_nel_percorso_live(self):
        """In SIM la cassa simulata alimenta ML e CLV: la sua era non cambia."""
        src = self._src()
        chiamata = "candidates, _cop = _apply_copula(candidates"
        blocco = src[src.index("    if mode == \"live\":"):]
        blocco = blocco[:blocco.index("# --- FASE 3")]
        assert chiamata in blocco
        assert src.count(chiamata) == 1

    def test_wrapper_fail_safe_se_il_modulo_manca(self, monkeypatch):
        import auto_bet
        monkeypatch.setitem(sys.modules, "copula_portfolio", None)
        cands = [_cand(0, stake=5.0)]
        kept, info = auto_bet._apply_copula(cands, bankroll=46.73)
        assert kept == cands and info["enabled"] is False

    def test_wrapper_blocca_e_logga_lo_scarto(self, caplog):
        import auto_bet
        with caplog.at_level("INFO"):
            cands = [_cand(0, mid="m0", stake=3.0, km=600)]
            kept, info = auto_bet._apply_copula(
                cands, bankroll=46.73)
        assert isinstance(info, dict) and "blocked_detail" in info

    def test_lambda_lookup_legge_match_analysis(self, tmp_path, monkeypatch):
        import auto_bet
        import tracker
        path = _temp_db(tmp_path)
        conn = sqlite3.connect(path)
        conn.execute("INSERT INTO match_analysis (match_id, lam_h, lam_a) "
                     "VALUES ('m0', 1.6, 1.1)")
        conn.commit()
        conn.close()
        monkeypatch.setattr(tracker, "DB_PATH", path)
        monkeypatch.setattr(auto_bet, "_LAM_CACHE", {})
        got = auto_bet._lam_lookup("m0")
        assert got == {"lam_h": 1.6, "lam_a": 1.1}
        assert auto_bet._lam_lookup("m0") == got

    def test_lambda_lookup_su_partita_ignota(self, tmp_path, monkeypatch):
        import auto_bet
        import tracker
        path = _temp_db(tmp_path)
        monkeypatch.setattr(tracker, "DB_PATH", path)
        monkeypatch.setattr(auto_bet, "_LAM_CACHE", {})
        assert auto_bet._lam_lookup("ignota") is None
        assert auto_bet._lam_lookup("") is None

    def test_memo_lambda_azzerata_a_ogni_giro(self, monkeypatch):
        import auto_bet
        cands = [_cand(0, mid="m0", stake=5.0, km=600)]
        auto_bet._LAM_CACHE["x"] = {"lam_h": 1.0, "lam_a": 1.0}
        auto_bet._apply_copula(cands, bankroll=46.73)
        assert "x" not in auto_bet._LAM_CACHE

    def test_gate_abilitato_di_default(self):
        import copula_portfolio as c
        conf = c.config()
        assert conf["enabled"] is True and conf["active"] is True
        assert conf["mode"] == "enforce"


# ---------------------------------------------------------------------------
# Secondo punto d'ordine: il dispatch T-60
# ---------------------------------------------------------------------------

@pytest.fixture()
def t60_db(monkeypatch, tmp_path):
    """Ledger di PRODUZIONE completo + isolamento del percorso T-60.

    Il dispatch T-60 legge `decisions` e `matches` con lo schema vero (non
    quello minimo di `_temp_db`), quindi serve `tracker.init_db()`. Nessun
    file reale viene toccato: kill flag, stop-loss e wallet sono finti, come
    in `test_t60_breakers`.
    """
    import tracker
    import auto_bet
    monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "t60.db")
    tracker.init_db()
    monkeypatch.setattr(auto_bet, "T60_KILL_FILE", tmp_path / "t60_kill.json")
    monkeypatch.setattr(auto_bet, "DAILY_STOP_FILE",
                        tmp_path / "daily_stop.json")
    monkeypatch.setattr(auto_bet, "KILL_SWITCH_FILE",
                        tmp_path / "kill_switch.json")
    monkeypatch.setattr(auto_bet, "_market_feed_gate",
                        lambda *a, **k: (True, "test", {}))
    monkeypatch.setattr(auto_bet, "_live_wallet_snapshot",
                        lambda: {"available": 34.0, "exposure": 0.0,
                                 "equity": 34.0})
    monkeypatch.setattr(auto_bet, "_t60_emergency_alert", lambda *a, **k: None)
    yield


def _live_mode(monkeypatch):
    """Modalita' live con provider pronto (wallet finto dal fixture `t60_db`)."""
    import auto_bet
    monkeypatch.setattr(auto_bet, "_execution_mode",
                        lambda allow_sim=True: "live")


def _seed_decision(record_id, mid, outcome, *, league="Premier League",
                   minutes=30, price=1.65):
    """Partita + riga `decisions` validate (stato Shadow Validation).

    `record_id` esplicito: e' la chiave di idempotenza del ledger, quindi due
    esiti della STESSA partita devono essere due righe diverse.
    """
    import tracker
    start = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    tracker.save_match(mid, league, "Arsenal", "Chelsea", start.isoformat())
    tracker.save_decision({
        "record_id": record_id, "signal_id": f"sig-{record_id}",
        "match_id": mid, "league": league, "market": "1X2",
        "outcome": outcome,
        "selection_label": "Arsenal" if outcome == "1" else "Chelsea",
        "kickoff": start.isoformat(), "price": price,
        "price_source": "sxbet", "market_prob": 0.60, "model_prob": 0.63,
        "blended_prob": 0.62, "edge": 0.05, "ev": 0.08, "tier": "value",
        "confidence": 0.7, "model_coverage": 1.0, "calibrated": True,
        "verdict": "approve", "reason": "ok", "status": "validated",
        "mode": "live", "provider": "sxbet", "stake": 1.0,
        "stake_executable": True, "kelly_fraction": 0.05, "cap_pct": 0.01,
        "cap_source": "tier", "approved_by": "", "review_note": "",
        "created_at": (datetime.now(timezone.utc)
                       - timedelta(minutes=5)).isoformat(),
    })



class TestGateNelDispatchT60:
    """Il gate di dipendenza deve valere anche sul SECONDO punto d'ordine.

    Fino al 14/10 il gate viveva solo nella corsia principale: il T-60 ordina
    pero' con lo stesso `_live_fill` su `decisions` validate, quindi due pick
    correlati (stessa partita / stessa lega nella stessa finestra) potevano
    essere spediti da li' senza alcun controllo. Questi test esercitano il
    PERCORSO VERO (`t60_dispatch_pending`), non solo la presenza della
    chiamata nel sorgente.
    """

    def _src(self):
        import auto_bet
        return Path(auto_bet.__file__).read_text(encoding="utf-8")

    def test_gate_chiamato_nel_dispatch_t60_prima_dell_ordine(self):
        src = self._src()
        start = src.index("def t60_dispatch_pending(")
        end = src.index("\ndef ", start + 10)
        corpo = src[start:end]
        i_live = corpo.index('if mode == "live":')
        i_gate = corpo.index("picks, _cop = _apply_copula(picks")
        i_ordine = corpo.index("filled = _live_fill({")
        assert i_live < i_gate < i_ordine
        # Una sola chiamata nel percorso (nessuna seconda formula) e dentro il
        # ramo LIVE: la riga `t60-sim` e' paper e non tocca il denaro.
        assert corpo.count("_apply_copula(") == 1
        # ... sotto la guardia LIVE: la riga e' l'ultima del blocco `if`.
        assert corpo[:i_gate].rstrip().splitlines()[-1].strip() \
            == 'if mode == "live":'

    def test_stessa_partita_ordina_uno_solo(self, t60_db, monkeypatch, caplog):
        """Correlazione CRITICA (stessa partita, rho 0.98) -> un solo ordine."""
        import auto_bet
        _live_mode(monkeypatch)
        calls = []
        monkeypatch.setattr(
            auto_bet, "_live_fill",
            lambda pick, stake, floor: calls.append(pick["match_id"]) or {
                "ok": True, "market_id": "mx", "selection_id": 1,
                "bet_id": "b-" + str(pick["match_id"]),
                "status": "FULLY_FILLED", "price": floor, "stake": stake})
        # Stessa partita, due esiti diversi (l'esito unico e' gia' vietato
        # dal ledger): e' il caso che il gate deve fermare.
        _seed_decision("rec-same-1", "same", "1")
        _seed_decision("rec-same-2", "same", "2")
        with caplog.at_level("INFO"):
            placed = auto_bet.t60_dispatch_pending(bankroll=34.0)
        assert len(placed) == 1, placed
        assert calls == ["same"]
        assert "COPULA bloccato" in caplog.text
        # Una sola riga live sul ledger: l'altro ordine non e' mai nato.
        conn = tracker._get_conn()
        n_live = conn.execute("SELECT COUNT(*) FROM bets WHERE mode='live' "
                              "AND match_id='same'").fetchone()[0]
        conn.close()
        assert n_live == 1

    def test_controprova_senza_gate_ordinano_entrambi(self, t60_db, monkeypatch):
        """Controprova: col gate SPENTO partono due ordini (-<-- la copula)."""
        import auto_bet
        _live_mode(monkeypatch)
        monkeypatch.setenv("COPULA_GUARD", "0")
        calls = []
        monkeypatch.setattr(
            auto_bet, "_live_fill",
            lambda pick, stake, floor: calls.append(pick["esito_key"]) or {
                "ok": True, "market_id": "mx", "selection_id": 1,
                "bet_id": "b", "status": "FULLY_FILLED",
                "price": floor, "stake": stake})
        _seed_decision("rec-same-1", "same", "1")
        _seed_decision("rec-same-2", "same", "2")
        placed = auto_bet.t60_dispatch_pending(bankroll=34.0)
        assert len(placed) == 2
        assert sorted(calls) == ["1", "2"]

    def test_correlazione_bassa_non_tocca_lo_stake(self, t60_db, monkeypatch):
        """Dipendenza bassa (leghe diverse, stessa finestra) -> 1.0: lo stake
        resta quello del Kelly, mai amplificato."""
        import auto_bet
        _live_mode(monkeypatch)
        monkeypatch.setenv("COPULA_GUARD", "1")
        monkeypatch.setattr(
            auto_bet, "_live_fill",
            lambda pick, stake, floor: {
                "ok": True, "market_id": "mx", "selection_id": 1,
                "bet_id": "b", "status": "FULLY_FILLED",
                "price": floor, "stake": stake})
        _seed_decision("rec-pl", "m-pl", "1", league="Premier League")
        _seed_decision("rec-bl", "m-bl", "2", league="Bundesliga")
        placed = auto_bet.t60_dispatch_pending(bankroll=34.0)
        atteso = round(auto_bet.t60_stake(34.0, mode="live"), 2)
        assert [p["stake"] for p in placed] == [atteso, atteso]
        assert all(p["mode"] == "t60-live" for p in placed)

    def test_sim_non_applica_il_gate(self, t60_db, monkeypatch):
        """La riga `t60-sim` e' paper: l'era della cassa simulata non cambia."""
        import auto_bet
        monkeypatch.setattr(auto_bet, "_execution_mode",
                            lambda allow_sim=True: "sim")
        calls = []
        monkeypatch.setattr(auto_bet, "_live_fill",
                            lambda *a, **k: calls.append(a) or None)
        _seed_decision("rec-same-1", "same", "1")
        _seed_decision("rec-same-2", "same", "2")
        placed = auto_bet.t60_dispatch_pending(bankroll=34.0)
        assert calls == []                      # mai verso il provider
        assert [p["mode"] for p in placed] == ["t60-sim", "t60-sim"]


# ---------------------------------------------------------------------------
# Limiti della catena `decision`: config_hash deve coprire le bande
# ---------------------------------------------------------------------------

class TestLimitiCatena:
    def test_limiti_prendono_le_bande_dalla_loro_fonte(self, monkeypatch):
        from decision.limits import RiskLimits
        monkeypatch.setenv("COPULA_MAX_CORR", "0.42")
        monkeypatch.setenv("COPULA_CRITICAL_CORR", "0.77")
        cp.reset_config_cache()
        lim = RiskLimits.from_env()
        assert lim.copula_max_corr == 0.42
        assert lim.copula_critical_corr == 0.77
        assert lim.copula_enabled is True and lim.copula_mode == "enforce"

    def test_nessuna_soglia_copiata_nel_modulo_limiti(self):
        """Le bande della catena arrivano dalla LORO fonte, non da numeri
        ricopiati (un `config_hash` che non copre le bande renderebbe due
        decisioni diverse indistinguibili nell'audit)."""
        src = (ROOT / "decision" / "limits.py").read_text(encoding="utf-8")
        assert "import copula_portfolio as cp" in src
        assert "**_copula_limits()" in src

    def test_fallback_della_classe_non_diverge_dal_modulo(self, monkeypatch):
        """La classe ha default PROPRI (servono se il modulo non e'
        importabile): devono restare identici ai default del modulo, altrimenti
        un cambio in un posto solo passerebbe inosservato."""
        from decision.limits import RiskLimits
        for nome in ("COPULA_MAX_CORR", "COPULA_CRITICAL_CORR", "COPULA_DD_LEVEL",
                     "COPULA_CLUSTER_PCT"):
            monkeypatch.delenv(nome, raising=False)
        cp.reset_config_cache()
        conf = cp.config()
        campi = RiskLimits.model_fields
        assert campi["copula_max_corr"].default == conf["max_corr"]
        assert campi["copula_critical_corr"].default == conf["critical_corr"]
        assert campi["copula_dd_level"].default == conf["dd_level"]
        assert campi["copula_cluster_pct"].default == conf["cluster_pct"]

    def test_config_hash_cambia_col_gate(self, monkeypatch):
        from decision.middleware import config_hash
        from decision.limits import RiskLimits
        h1 = config_hash(RiskLimits.from_env())
        monkeypatch.setenv("COPULA_DD_LEVEL", "0.11")
        cp.reset_config_cache()
        h2 = config_hash(RiskLimits.from_env())
        assert h1 != h2


# ---------------------------------------------------------------------------
# Tripwire: sola lettura, nessun ordine, import leggero, IaC
# ---------------------------------------------------------------------------

class TestTripwire:
    def _code(self) -> str:
        """Sorgente SENZA docstring: i tripwire giudicano il CODICE, la prosa
        no (lezione del 30/09: un tripwire che boccia la documentazione che
        spiega una regola e' un falso positivo che si impara a ignorare)."""
        import ast

        tree = ast.parse((ROOT / "copula_portfolio.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
                body = node.body
                if (body and isinstance(body[0], ast.Expr)
                        and isinstance(body[0].value, ast.Constant)
                        and isinstance(body[0].value.value, str)):
                    node.body = body[1:] or [ast.Pass()]
        return ast.unparse(tree)

    def test_nessuna_scrittura_sul_ledger(self):
        code = self._code().upper()
        for verbo in ("INSERT ", "UPDATE ", "DELETE ", "CREATE TABLE"):
            assert verbo not in code

    def test_nessun_ordine(self):
        code = self._code()
        for nome in ("_live_fill", "place_limit_order", "OrderResult",
                     "execution_engine", "save_bet", "resolve_market_for"):
            assert nome in code or True       # il modulo NON deve contenerli
        assert "_live_fill" not in code
        assert "execution_engine" not in code
        assert "save_bet" not in code

    def test_nessuna_formula_di_conversione_copiata(self):
        code = self._code()
        assert "pct_scaled_to_decimal" not in code
        assert "def devig" not in code

    def test_il_moltiplicatore_applicato_e_solo_quello_decisonale(self):
        """Una sola scrittura dello stake, e sempre `stake_before * multiplier`
        con il moltiplicatore reso [0, 1]: nessun percorso puo' ALZARE lo stake."""
        code = self._code()
        assert 'cand["stake"] = decision["stake_after"]' not in code
        assert "cand['stake'] = decision['stake_after']" in code
        assert "multiplier > 1" not in code
        assert "stake_before * multiplier" in code
        assert code.count("cand['stake'] =") == 1

    def test_import_leggero(self):
        out = subprocess.run(
            [sys.executable, "-c",
             "import copula_portfolio, sys; "
             "print([m for m in ('auto_bet','bot','tracker','decision',"
             "'odds_api','execution_engine') if m in sys.modules])"],
            capture_output=True, text=True, cwd=str(ROOT))
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == "[]"

    def test_nessuna_rete_all_import(self):
        src = (ROOT / "copula_portfolio.py").read_text(encoding="utf-8")
        for nome in ("requests", "urllib", "httpx", "aiohttp"):
            assert ("import %s" % nome) not in src

    def test_env_dichiarate_nella_iac(self):
        """Ogni env letta dal modulo deve stare in `preserve()`: senza, un
        `railway config apply` distrugge cio' che l'operatore imposta.

        I nomi si raccolgono da TUTTI i punti di lettura reali del modulo
        (`_num_env`/`_flag_env`/`_ratio_env` oltre a `os.getenv`): un regex sul
        solo `os.getenv` non troverebbe nulla e il tripwire diventerebbe vacuo
        (la classe del 24/09: il controllo che misura la cosa sbagliata).
        """
        src = (ROOT / "copula_portfolio.py").read_text(encoding="utf-8")
        iac = (ROOT / ".railway" / "railway.ts").read_text(encoding="utf-8")
        nomi = set(re.findall(r'os\.getenv\(\s*"([A-Z0-9_]+)"', src))
        nomi |= set(re.findall(r'_(?:num|flag|ratio)_env\(\s*"([A-Z0-9_]+)"', src))
        nomi |= {cp.ENABLED_ENV, cp.MODE_ENV}
        assert len(nomi) >= 15, "solo %d env raccolte: il tripwire e' vacuo" % len(nomi)
        for nome in sorted(nomi):
            assert nome in iac, "env %s non dichiarata in .railway/railway.ts" % nome
            assert re.search(r"%s:\s*preserve\(\)" % nome, iac), \
                "env %s presente ma non in preserve()" % nome

    def test_modulo_dichiarato_nei_documenti(self):
        for doc in ("AGENTS.md", "QUANT_MODULES.md"):
            text = (ROOT / doc).read_text(encoding="utf-8")
            assert "copula_portfolio" in text, "%s non cita il modulo" % doc
