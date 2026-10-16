"""Test di `dixon_coles.py` — TUTTI OFFLINE.

Nessuna rete, nessun credito, nessun ordine: ledger SQLite temporaneo (schema
`match_results` di produzione) e `expected_goals` iniettato dove serve.

Due prove che contano piu' di tutte:
1. **parita' con la griglia del modello**: `log_likelihood` deve coincidere, al
   livello dei float, con la verosimiglianza calcolata ricostruendo la griglia
   vera di `poisson_engine` con `rho_value(...)` — se divergesse, la stima
   sarebbe quella di un modello diverso da quello in produzione;
2. **recupero di un rho noto**: su dati simulati DAL modello con un rho scelto,
   la stima deve ritrovarlo (altrimenti il massimo non e' il massimo).
"""
from __future__ import annotations

import ast
import asyncio
import json
import math
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

import dixon_coles as dc
import poisson_engine as pe

ROOT = Path(__file__).resolve().parent


def _code_only(path) -> str:
    """Solo il CODICE del modulo (la prosa nomina i confini per spiegarli)."""
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and \
                    isinstance(body[0].value, ast.Constant) and \
                    isinstance(body[0].value.value, str):
                node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


# ---------------------------------------------------------------------------
# Helper: match sintetici e ledger temporaneo
# ---------------------------------------------------------------------------
def _matches(n=200, *, seed=7, weight=1.0, lam=(1.6, 1.1)):
    rng = np.random.default_rng(seed)
    return [{"home": "H%d" % i, "away": "A%d" % i,
             "score_home": int(rng.integers(0, 4)),
             "score_away": int(rng.integers(0, 4)),
             "weight": weight} for i in range(n)]


def _eg_const(lam=(1.6, 1.1)):
    return lambda home, away: lam


def _eg_mixed(lam_big=(4.0, 3.5), lam_small=(1.4, 1.3)):
    """`expected_goals` per un campione MISTO: favoriti forti + partite normali.

    Serve a esercitare la maschera PARZIALE di `log_likelihood`: sulla griglia
    di produzione (che parte da rho -0.30) un favorito con lam 4.0 rende la
    cella 0-1 non valida (`1 + lam*rho = -0.2`) mentre le partite normali
    restano valide — esattamente il caso che su dati reali ha fatto fallire la
    stima (bug del 16/10/2026: `f11` era uno scalare e la maschera lo
    indicizzava).
    """
    return lambda home, away: lam_big if str(home).startswith("BIG") else lam_small


def _mixed_matches(n=20):
    """20 partite con lam 4.0 (che a rho -0.30 escono) + 20 con lam 1.4."""
    rows = [{"home": "BIG%d" % i, "away": "A", "score_home": 1,
             "score_away": 0, "weight": 1.0} for i in range(n)]
    rows += [{"home": "SM%d" % i, "away": "A", "score_home": 1,
              "score_away": 0, "weight": 1.0} for i in range(n)]
    return rows


def _simulate(true_rho, n=4000, lam=(1.6, 1.2), seed=11):
    """Punteggi campionati DAL modello con `true_rho` (nessun modello nuovo)."""
    rng = np.random.default_rng(seed)
    with pe.rho_value(true_rho):
        grid = pe._probs_grid(*lam)
    flat = np.cumsum(grid.flatten())
    idx = np.searchsorted(flat, rng.random(n))
    return [{"home": "H", "away": "A",
             "score_home": int(i) // grid.shape[1],
             "score_away": int(i) % grid.shape[1], "weight": 1.0}
            for i in idx]


def _temp_db(tmp_path, rows, *, days_ago=1):
    path = str(tmp_path / "dc.db")
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE match_results (id INTEGER PRIMARY KEY, league TEXT,"
        " home_team TEXT, away_team TEXT, score_home INTEGER,"
        " score_away INTEGER, settled_at TEXT);")
    for i, row in enumerate(rows):
        ts = (datetime.now(timezone.utc) - timedelta(
            days=float(row.get("days_ago", days_ago)))).isoformat()
        conn.execute("INSERT INTO match_results (league, home_team, away_team,"
                     " score_home, score_away, settled_at) VALUES (?,?,?,?,?,?)",
                     (row.get("league", "Serie A"), row["home"], row["away"],
                      row.get("score_home"), row.get("score_away"), ts))
    conn.commit()
    conn.close()
    return path


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    for name in ("DIXON_COLES_DAYS", "DIXON_COLES_MAX_MATCHES",
                 "DIXON_COLES_MAX_GOALS", "DIXON_COLES_RHO_MIN",
                 "DIXON_COLES_RHO_MAX", "DIXON_COLES_RHO_STEP",
                 "DIXON_COLES_REFINE_STEP", "DIXON_COLES_SAFE_MIN",
                 "DIXON_COLES_SAFE_MAX", "DIXON_COLES_HALF_LIFE_DAYS",
                 "DIXON_COLES_APPLY", "DIXON_COLES_ENABLED", dc.RHO_FILE_ENV):
        monkeypatch.delenv(name, raising=False)
    # Lo stato su volume va nella tmp passando dall'ENV (come in produzione):
    # cosi' il percorso reale di `state_path()` resta quello esercitato.
    monkeypatch.setenv(dc.RHO_FILE_ENV,
                       str(tmp_path / "dixon_coles_rho.json"))
    monkeypatch.setattr(dc, "_db_path", lambda: str(tmp_path / "mai.db"))
    yield
    pe.RHO = -0.15      # il modello torna SEMPRE al default


# ---------------------------------------------------------------------------
# Configurazione
# ---------------------------------------------------------------------------
class TestConfig:
    def test_default_di_codice(self):
        conf = dc.config()
        assert conf["apply"] is False          # l'applicazione e' OPT-IN
        assert conf["days"] == 365
        assert conf["rho_min"] < -0.15 < conf["rho_max"]
        assert conf["safe_min"] <= -0.15 <= conf["safe_max"]

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("DIXON_COLES_DAYS", "90")
        monkeypatch.setenv("DIXON_COLES_APPLY", "1")
        monkeypatch.setenv("DIXON_COLES_RHO_STEP", "0.02")
        conf = dc.config()
        assert conf["days"] == 90 and conf["apply"] is True
        assert conf["rho_step"] == 0.02

    def test_valori_impossibili_ricadono_sul_default(self, monkeypatch):
        monkeypatch.setenv("DIXON_COLES_DAYS", "-3")
        monkeypatch.setenv("DIXON_COLES_RHO_STEP", "abc")
        monkeypatch.setenv("DIXON_COLES_MAX_GOALS", "1")
        conf = dc.config()
        assert conf["days"] == 365 and conf["rho_step"] == dc.DEFAULT_RHO_STEP
        assert conf["max_goals"] == dc.DEFAULT_MAX_GOALS

    def test_state_path_da_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv(dc.RHO_FILE_ENV, str(tmp_path / "x.json"))
        assert dc.state_path() == tmp_path / "x.json"


# ---------------------------------------------------------------------------
# Time-decay (delega ai rating)
# ---------------------------------------------------------------------------
class TestTimeDecay:
    def test_usa_la_mezza_vita_dei_rating(self):
        import rating_engine
        assert dc.half_life_days() == pytest.approx(rating_engine.HALF_LIFE_DAYS)

    def test_peso_identico_ai_rating(self):
        import rating_engine
        for days in (0, 7, 30, 100, 365):
            assert dc.time_weight(days) == pytest.approx(
                rating_engine._weight(days), rel=1e-12)

    def test_peso_decrescente_e_meta_a_una_mezza_vita(self):
        hl = dc.half_life_days()
        assert dc.time_weight(0) == pytest.approx(1.0)
        assert dc.time_weight(hl) == pytest.approx(0.5, rel=1e-9)
        assert dc.time_weight(10) > dc.time_weight(200)

    def test_peso_illeggibile_non_solleva(self):
        assert dc.time_weight(None) == pytest.approx(1.0)
        assert dc.time_weight("abc") == pytest.approx(1.0)

    def test_env_cambia_la_mezza_vita(self, monkeypatch):
        monkeypatch.setenv("DIXON_COLES_HALF_LIFE_DAYS", "50")
        assert dc.half_life_days() == pytest.approx(50.0)
        assert dc.time_weight(50) == pytest.approx(0.5, rel=1e-9)


# ---------------------------------------------------------------------------
# Parita' con la griglia del modello
# ---------------------------------------------------------------------------
class TestParita:
    def test_log_likelihood_uguale_alla_griglia_vera(self):
        matches = _matches(120)
        terms = dc.match_terms(matches, expected_goals_fn=_eg_const())
        for rho in (-0.20, -0.15, -0.05, 0.0, 0.03):
            got = dc.log_likelihood(terms, rho)
            expected = 0.0
            with pe.rho_value(rho):
                for m in matches:
                    grid = pe._probs_grid(1.6, 1.1)
                    expected += math.log(float(grid[m["score_home"],
                                                  m["score_away"]]))
            assert got == pytest.approx(expected, abs=1e-9), rho

    def test_pesi_moltiplicano_la_verosimiglianza(self):
        matches = _matches(60)
        terms = dc.match_terms(matches, expected_goals_fn=_eg_const())
        weighted = [dict(m, weight=2.0) for m in matches]
        terms_w = dc.match_terms(weighted, expected_goals_fn=_eg_const())
        assert dc.log_likelihood(terms_w, -0.10) == pytest.approx(
            2.0 * dc.log_likelihood(terms, -0.10), rel=1e-9)

    def test_il_fattore_osservato_usa_il_punteggio_non_la_probabilita(self):
        """Con `lam_h == lam_a` le pmf di 0-1 e 1-0 coincidono: scambiarle
        applicherebbe il fattore sbagliato. Il fattore segue il PUNTEGGIO."""
        lam = (1.5, 1.5)
        matches = [{"home": "H", "away": "A", "score_home": 0, "score_away": 1,
                    "weight": 1.0},
                   {"home": "H", "away": "A", "score_home": 1, "score_away": 0,
                    "weight": 1.0}]
        terms = dc.match_terms(matches, expected_goals_fn=lambda h, a: lam)
        factors = dc.observed_cell_factor(terms, -0.10)
        assert factors[0] == pytest.approx(1.0 + lam[0] * -0.10)   # 0-1
        assert factors[1] == pytest.approx(1.0 + lam[1] * -0.10)   # 1-0
        # La probabilita' osservata e' la STESSA (lam_h == lam_a): il confronto
        # sulle pmf non li distinguerebbe.
        assert terms["p_obs"][0] == pytest.approx(terms["p_obs"][1])

    def test_rho_che_invalida_una_cella_non_viene_riparato(self):
        matches = [{"home": "H", "away": "A", "score_home": 0, "score_away": 0,
                    "weight": 1.0} for _ in range(40)]
        terms = dc.match_terms(matches, expected_goals_fn=lambda h, a: (3.0, 3.0))
        # 1 - lam_h*lam_a*rho <= 0 con rho alto: la partita esce dal conteggio
        assert dc.log_likelihood(terms, 0.5) == float("-inf")

    def test_nessun_match_utilizzabile(self):
        terms = dc.match_terms([], expected_goals_fn=_eg_const())
        assert dc.log_likelihood(terms, -0.15) == float("-inf")

    def test_maschera_parziale_esclude_solo_le_invalide(self):
        """Meta' campione non valido a rho -0.30: le valide restano nel conto.

        Regressione del bug del 16/10/2026 (`f11` scalare -> TypeError sulla
        maschera parziale): senza questo caso il difetto resta invisibile
        perche' la suite copriva solo "tutte invalide".
        """
        mixed = _mixed_matches()
        terms = dc.match_terms(mixed, expected_goals_fn=_eg_mixed())
        got = dc.log_likelihood(terms, -0.30)
        assert isinstance(got, float) and math.isfinite(got)
        # Le partite con lam 4.0 sono davvero fuori validita' a rho -0.30...
        assert 1.0 + 4.0 * -0.30 <= 0
        # ...quindi il valore e' ESATTAMENTE quello delle sole partite valide
        # (mai un fattore negativo "riparato" a zero: sarebbero rimaste dentro
        # con un contributo finto).
        only_small = [m for m in mixed if m["home"].startswith("SM")]
        ref = dc.log_likelihood(
            dc.match_terms(only_small, expected_goals_fn=_eg_mixed()), -0.30)
        assert got == pytest.approx(ref, rel=1e-12)

    def test_ogni_punto_della_griglia_non_solleva(self):
        """La griglia di PRODUZIONE (-0.30..0.05) attraversata per intero."""
        terms = dc.match_terms(_mixed_matches(), expected_goals_fn=_eg_mixed())
        for r in np.arange(-0.30, 0.055, 0.01):
            assert isinstance(dc.log_likelihood(terms, float(r)), float), r

    def test_partita_fuori_griglia_scartata(self):
        matches = [{"home": "H", "away": "A", "score_home": 15, "score_away": 0,
                    "weight": 1.0}]
        terms = dc.match_terms(matches, expected_goals_fn=_eg_const(),
                               max_goals=10)
        assert terms["index"] == []


# ---------------------------------------------------------------------------
# Stima
# ---------------------------------------------------------------------------
class TestStima:
    def test_recupera_un_rho_noto(self):
        for true_rho in (-0.15, -0.05, 0.02):
            fit = dc.fit_rho(_simulate(true_rho), expected_goals_fn=_eg_const(
                (1.6, 1.2)))
            assert fit["ok"] is True
            assert fit["rho"] == pytest.approx(true_rho, abs=0.05)

    def test_campione_insufficiente(self):
        fit = dc.fit_rho(_matches(5), expected_goals_fn=_eg_const())
        assert fit["ok"] is False
        assert fit["reason"] == "campione_insufficiente"

    def test_nessun_match_utilizzabile(self):
        matches = _matches(50)
        fit = dc.fit_rho(matches, expected_goals_fn=lambda h, a: (0.0, 0.0))
        assert fit["ok"] is False
        assert fit["reason"] in ("campione_utilizzabile_insufficiente",
                                 "campione_insufficiente")

    def test_griglia_non_valida(self):
        fit = dc.fit_rho(_matches(50), rho_min=0.1, rho_max=-0.1,
                         expected_goals_fn=_eg_const())
        assert fit["ok"] is False and fit["reason"] == "griglia_non_valida"

    def test_confronto_col_default_e_dichiarato(self):
        fit = dc.fit_rho(_simulate(-0.05), expected_goals_fn=_eg_const((1.6, 1.2)))
        assert fit["loglik"] is not None
        assert fit["loglik_default"] is not None
        # Il massimo non puo' essere PEGGIORE del default: e' un massimo
        assert fit["loglik"] >= fit["loglik_default"] - 1e-9
        assert fit["improvement"] >= -1e-9

    def test_rho_resta_nel_range_della_griglia(self):
        fit = dc.fit_rho(_matches(200), rho_min=-0.20, rho_max=-0.10,
                         expected_goals_fn=_eg_const())
        assert -0.20 - 0.002 <= fit["rho"] <= -0.10 + 0.002

    def test_errore_del_modello_non_propaga(self):
        def _boom(home, away):
            raise RuntimeError("rating rotto")
        fit = dc.fit_rho(_matches(60), expected_goals_fn=_boom)
        # Il modello non produce lam: nessuna partita utilizzabile (mai un
        # traceback al chiamante)
        assert fit["ok"] is False
        assert fit["reason"] in ("campione_utilizzabile_insufficiente",
                                 "campione_insufficiente", "errore_stima")

    def test_tempo_di_stima_contenuto(self):
        fit = dc.fit_rho(_matches(1000), expected_goals_fn=_eg_const())
        assert fit["ok"] is True
        assert fit["elapsed_s"] < 30.0

    def test_stima_su_campione_con_favoriti_forti(self):
        """Regressione di PRODUZIONE (16/10/2026): la stima moriva a rho -0.30.

        Con un favorito forte nel campione la griglia tocca punti in cui una
        cella non e' valida: `fit_rho` deve restituire comunque una stima (non
        `errore_stima`, che era il risultato su 17.621 match reali).
        """
        fit = dc.fit_rho(_mixed_matches(), expected_goals_fn=_eg_mixed())
        assert fit["ok"] is True and fit["reason"] == "ok"
        assert -0.30 <= fit["rho"] <= 0.05
        assert fit["n_used"] >= 20          # le partite valide sono state usate

    def test_il_modello_non_viene_lasciato_modificato(self):
        before = pe.RHO
        dc.fit_rho(_matches(60), expected_goals_fn=_eg_const())
        assert pe.RHO == before


# ---------------------------------------------------------------------------
# Lettura dal ledger
# ---------------------------------------------------------------------------
class TestLedger:
    def test_legge_i_match_con_peso(self, tmp_path, monkeypatch):
        rows = [{"home": "Inter", "away": "Milan", "score_home": 2,
                 "score_away": 1, "days_ago": 10},
                {"home": "Roma", "away": "Lazio", "score_home": 0,
                 "score_away": 0, "days_ago": 200}]
        monkeypatch.setattr(dc, "_db_path", lambda: _temp_db(tmp_path, rows))
        got = dc.load_matches(days=400)
        assert len(got) == 2
        older = [m for m in got if m["home"] == "Roma"][0]
        recent = [m for m in got if m["home"] == "Inter"][0]
        assert recent["weight"] > older["weight"]        # time-decay attivo

    def test_righe_senza_punteggio_escluse(self, tmp_path, monkeypatch):
        path = _temp_db(tmp_path, [{"home": "A", "away": "B",
                                    "score_home": None, "score_away": None}])
        monkeypatch.setattr(dc, "_db_path", lambda: path)
        assert dc.load_matches() == []

    def test_finestra_temporale(self, tmp_path, monkeypatch):
        rows = [{"home": "A", "away": "B", "score_home": 1, "score_away": 0,
                 "days_ago": 5},
                {"home": "C", "away": "D", "score_home": 1, "score_away": 0,
                 "days_ago": 400}]
        monkeypatch.setattr(dc, "_db_path", lambda: _temp_db(tmp_path, rows))
        assert len(dc.load_matches(days=30)) == 1

    def test_ledger_assente_non_solleva(self, tmp_path, monkeypatch):
        monkeypatch.setattr(dc, "_db_path",
                            lambda: str(tmp_path / "inesistente.db"))
        assert dc.load_matches() == []

    def test_connessione_sola_lettura(self, tmp_path, monkeypatch):
        path = _temp_db(tmp_path, [{"home": "A", "away": "B",
                                    "score_home": 1, "score_away": 0}])
        conn = dc._ro_conn(path)
        try:
            with pytest.raises(sqlite3.OperationalError):
                conn.execute("UPDATE match_results SET score_home = 9")
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Persistenza e applicazione (opt-in)
# ---------------------------------------------------------------------------
class TestApplicazione:
    def test_salva_e_rilegge(self, tmp_path, monkeypatch):
        path = tmp_path / "rho.json"
        fit = {"ok": True, "rho": -0.12, "n_used": 100, "safe": True}
        assert dc.save_fit(fit, path=path) is True
        back = dc.load_fit(path=path)
        assert back["rho"] == -0.12 and back["saved_at"]

    def test_stima_illeggibile(self, tmp_path):
        path = tmp_path / "rho.json"
        path.write_text("{non json", encoding="utf-8")
        got = dc.load_fit(path=path)
        assert got["ok"] is False and got["reason"] == "stima_illeggibile"

    def test_nessuna_stima(self, tmp_path):
        got = dc.load_fit(path=tmp_path / "assente.json")
        assert got["ok"] is False and got["reason"] == "nessuna_stima"

    def test_salvataggio_impossibile_non_solleva(self, tmp_path):
        # Un path il cui genitore e' un FILE: mkdir fallisce -> False, mai eccezione
        blocker = tmp_path / "file"
        blocker.write_text("x", encoding="utf-8")
        assert dc.save_fit({"ok": True, "rho": -0.1},
                           path=blocker / "sub" / "rho.json") is False

    def test_applicazione_off_di_default(self, tmp_path, monkeypatch):
        dc.save_fit({"ok": True, "rho": -0.10}, path=tmp_path / "rho.json")
        monkeypatch.setenv(dc.RHO_FILE_ENV, str(tmp_path / "rho.json"))
        got = dc.apply_fitted()
        assert got["applied"] is False and got["reason"] == "interruttore_off"
        assert pe.RHO == -0.15

    def test_applicazione_con_interruttore(self, tmp_path, monkeypatch):
        dc.save_fit({"ok": True, "rho": -0.10}, path=tmp_path / "rho.json")
        monkeypatch.setenv(dc.RHO_FILE_ENV, str(tmp_path / "rho.json"))
        monkeypatch.setenv("DIXON_COLES_APPLY", "1")
        got = dc.apply_fitted()
        assert got["applied"] is True and got["rho"] == pytest.approx(-0.10)
        assert pe.RHO == pytest.approx(-0.10)
        assert got["previous"] == pytest.approx(-0.15)

    def test_fuori_banda_di_sicurezza_non_si_applica(self, tmp_path, monkeypatch):
        dc.save_fit({"ok": True, "rho": -0.60}, path=tmp_path / "rho.json")
        monkeypatch.setenv(dc.RHO_FILE_ENV, str(tmp_path / "rho.json"))
        monkeypatch.setenv("DIXON_COLES_APPLY", "1")
        got = dc.apply_fitted()
        assert got["applied"] is False
        assert got["reason"] == "fuori_banda_di_sicurezza"
        assert pe.RHO == -0.15

    def test_senza_stima_non_si_applica(self, tmp_path, monkeypatch):
        monkeypatch.setenv(dc.RHO_FILE_ENV, str(tmp_path / "assente.json"))
        monkeypatch.setenv("DIXON_COLES_APPLY", "1")
        got = dc.apply_fitted()
        assert got["applied"] is False and got["reason"] == "nessuna_stima"

    def test_rho_value_ripristina_anche_su_errore(self):
        assert pe.RHO == -0.15
        with pytest.raises(RuntimeError):
            with pe.rho_value(-0.05):
                assert pe.RHO == -0.05
                raise RuntimeError("dentro il blocco")
        assert pe.RHO == -0.15

    def test_rho_value_ripristina_dopo_uso_normale(self):
        with pe.rho_value(0.01):
            assert pe.RHO == pytest.approx(0.01)
        assert pe.RHO == -0.15


# ---------------------------------------------------------------------------
# Probabilita' raffinate
# ---------------------------------------------------------------------------
class TestCalibrazione:
    """Il gate che allinea la stima al criterio del DENARO (Brier 1X2).

    La misura del 16/10/2026 sul ledger reale: il rho che massimizza la
    verosimiglianza dei punteggi esatti (-0.30) PEGGIORA il Brier 1X2 (0.5506
    contro 0.5451 del default, 0.5425 di rho 0). Questi test difendono sia il
    calcolo sia la regola (fail-closed).
    """

    def _draws(self, n=120, score=(1, 1), lam=(1.3, 1.3)):
        return [{"home": "H", "away": "A", "score_home": score[0],
                 "score_away": score[1], "weight": 1.0, "days_ago": i * 0.5}
                for i in range(n)]

    def _wins(self, n=120, lam=(1.0, 1.0)):
        return [{"home": "H", "away": "A", "score_home": 3,
                 "score_away": 1, "weight": 1.0, "days_ago": i * 0.5}
                for i in range(n)]

    def test_brier_coincide_col_modello(self):
        matches = self._draws(40, score=(1, 0))
        got = dc.brier_1x2(matches, -0.10, expected_goals_fn=_eg_const((1.6, 1.2)))
        manual = 0.0
        with pe.rho_value(-0.10):
            p1, px, p2 = pe.prob_1x2(1.6, 1.2)
        manual = ((p1 - 1.0) ** 2 + px ** 2 + p2 ** 2)
        assert got == pytest.approx(manual)

    def test_brier_normalizzato_sui_pesi(self):
        a = dc.brier_1x2(self._draws(60), -0.15, expected_goals_fn=_eg_const())
        weighted = [dict(m, weight=3.0) for m in self._draws(60)]
        b = dc.brier_1x2(weighted, -0.15, expected_goals_fn=_eg_const())
        assert a == pytest.approx(b)                 # media pesata, non somma

    def test_brier_senza_campione_e_none(self):
        assert dc.brier_1x2([], -0.15, expected_goals_fn=_eg_const()) is None
        bad = [{"home": "H", "away": "A", "score_home": None,
                "score_away": None, "weight": 1.0}]
        assert dc.brier_1x2(bad, -0.15, expected_goals_fn=_eg_const()) is None

    def test_verdetto_accetta_chi_migliora(self):
        # Un campione di PAREGGI: un rho piu' negativo alza la probabilita' del
        # pareggio e quindi migliora il Brier (e' il caso reale al contrario).
        v = dc.holdout_verdict(self._draws(), -0.30,
                               expected_goals_fn=_eg_const((1.3, 1.3)))
        assert v["ok"] is True and v["reason"] == "ok"
        assert v["gain_vs_default"] > 0
        # 30% di 120 = 36, ma il minimo (`MIN_HOLDOUT` 50) prevale
        assert v["n_holdout"] == 50

    def test_verdetto_rifiuta_chi_peggiora(self):
        v = dc.holdout_verdict(self._draws(), 0.05,
                              expected_goals_fn=_eg_const((1.3, 1.3)))
        assert v["ok"] is False
        assert v["reason"] in ("non_migliora_default", "non_migliora_rho_zero")

    def test_verdetto_fail_closed_su_campione_piccolo(self):
        v = dc.holdout_verdict(self._draws(20), -0.20,
                               expected_goals_fn=_eg_const((1.3, 1.3)))
        assert v["ok"] is False and v["reason"] == "holdout_insufficiente"
        v0 = dc.holdout_verdict([], -0.20, expected_goals_fn=_eg_const())
        assert v0["ok"] is False and v0["reason"] == "campione_insufficiente"

    def test_verdetto_senza_lam_utilizzabili(self):
        v = dc.holdout_verdict(self._draws(120), -0.20,
                               expected_goals_fn=lambda h, a: (float("nan"), 1.0))
        assert v["ok"] is False and v["reason"] == "brier_non_calcolabile"

    def test_verifica_ignora_l_ordine_del_campione(self):
        """Il banco di prova sono i match PIU' RECENTI, non i primi della lista."""
        recenti_negativi = [{"home": "H", "away": "A", "score_home": 1,
                             "score_away": 1, "weight": 1.0, "days_ago": 0.1}
                            for _ in range(80)]
        vecchi_positivi = [{"home": "H", "away": "A", "score_home": 3,
                            "score_away": 1, "weight": 1.0, "days_ago": 300.0}
                           for _ in range(80)]
        a = dc.holdout_verdict(recenti_negativi + vecchi_positivi, -0.30,
                               expected_goals_fn=_eg_const((1.3, 1.3)))
        b = dc.holdout_verdict(vecchi_positivi + recenti_negativi, -0.30,
                               expected_goals_fn=_eg_const((1.3, 1.3)))
        assert a["brier_fitted"] == pytest.approx(b["brier_fitted"])
        assert a["ok"] == b["ok"] is True

    def test_gate_chiamato_col_rho_stimato_e_blocca(self, monkeypatch):
        """Promozione COMPLETA col gate che rifiuta: si salva, non si applica.

        E' il comportamento misurato il 16/10 sul ledger di produzione (stima
        al bordo della griglia, calibrazione 1X2 che peggiora): modello al
        default, misura salvata. Il verdetto e' INIETTATO per fissare la
        POLITICA del percorso (chiamato col rho stimato, blocca, non applica);
        la direzione matematica del verdetto ha i suoi test sopra.
        """
        monkeypatch.setenv("DIXON_COLES_APPLY", "1")
        seen = {}

        def _fake_verdict(matches, rho, **kw):
            seen["rho"] = rho
            seen["n"] = len(matches)
            return {"ok": False, "reason": "non_migliora_default",
                    "n_holdout": 120, "brier_fitted": 0.5506,
                    "brier_default": 0.5451, "brier_zero": 0.5425,
                    "gain_vs_default": -0.0055}

        monkeypatch.setattr(dc, "holdout_verdict", _fake_verdict)
        data = _simulate(-0.08, n=4000)
        out = dc.promote(matches=data)
        assert out["ok"] is True and out["saved"] is True
        assert out["applied"] is False
        assert out["apply_reason"] == "calibrazione:non_migliora_default"
        assert out["calibration"]["brier_fitted"] == pytest.approx(0.5506)
        assert seen["rho"] == pytest.approx(out["rho"])
        assert seen["n"] == len(data)          # verifica sullo stesso campione
        assert pe.RHO == -0.15                  # modello intatto
        assert out["engine_rho"] == pytest.approx(-0.15)
        assert dc.load_fit()["ok"] is True      # la misura resta

    def test_gate_lascia_passare_chi_migliora_davvero(self, monkeypatch):
        """Campione generato dal modello con rho -0.20: il Brier migliora -> si applica.

        Il campione dei pareggi NON serve qui: la stima su un campione
        degenerato cade al bordo della griglia (`-0.30`) e la BANDA di
        sicurezza la fermerebbe prima della calibrazione — il percorso reale
        passa da entrambe le guardie, quindi il test usa un caso in banda.
        """
        monkeypatch.setenv("DIXON_COLES_APPLY", "1")
        out = dc.promote(matches=_simulate(-0.20, n=4000, lam=(1.4, 1.2)),
                         expected_goals_fn=_eg_const((1.4, 1.2)))
        cal = out["calibration"]
        assert cal is not None and cal["ok"] is True
        assert cal["brier_fitted"] < cal["brier_default"]
        assert out["safe"] is True                    # in banda: applicabile
        assert out["applied"] is True and out["changed"] is True
        assert pe.RHO == pytest.approx(out["rho"])

    def test_gate_spegnevole_da_env(self, monkeypatch):
        monkeypatch.setenv("DIXON_COLES_APPLY", "1")
        monkeypatch.setenv("DIXON_COLES_CALIB_GATE", "0")
        out = dc.promote(matches=_simulate(-0.08, n=4000))
        assert out["calibration"] is None             # gate non interpellato
        assert out["applied"] is True                 # resta la banda di sicurezza


class TestDefaultUnico:
    """Il default del modello vive in UN solo posto (`poisson_engine`).

    `fit_rho` (log-L di riferimento) e `holdout_verdict` (Brier di riferimento)
    confrontavano il rho stimato con un `-0.15` SCRITTO A MANO: due definizioni
    dello stesso default. Il giorno in cui il default cambia, confronti scritti
    a mano misurerebbero un miglioramento contro un parametro che il modello non
    usa piu' — e il gate potrebbe accettare un rho che peggiora la calibrazione
    VERA. Qui si difende la derivazione.
    """

    def test_il_default_viene_dal_modello(self):
        assert dc.default_rho() == pytest.approx(pe.DEFAULT_RHO)
        assert pe.default_rho() == pytest.approx(pe.DEFAULT_RHO)
        # Il parametro EFFETTIVO nasce dal default (nessuna seconda verita')
        # e senza promozioni applicate coincide con esso.
        if pe.current_rho() == pytest.approx(pe.DEFAULT_RHO):
            assert pe.current_rho() == pytest.approx(dc.default_rho())

    def test_il_confronto_segue_il_default_del_modello(self, monkeypatch):
        """Se il default del MODELLO cambia, il riferimento della stima cambia con lui."""
        matches = _matches(120)
        base = dc.fit_rho(matches, expected_goals_fn=_eg_const())
        monkeypatch.setattr(pe, "DEFAULT_RHO", -0.02)
        moved = dc.fit_rho(matches, expected_goals_fn=_eg_const())
        assert base["default_rho"] == pytest.approx(-0.15)
        assert moved["default_rho"] == pytest.approx(-0.02)
        assert moved["loglik_default"] != pytest.approx(base["loglik_default"])
        # Il verdetto di calibrazione usa lo STESSO riferimento.
        terms = dc.match_terms(matches, expected_goals_fn=_eg_const())
        assert moved["loglik_default"] == pytest.approx(
            dc.log_likelihood(terms, -0.02))

    def test_la_promozione_non_sposta_il_riferimento(self, monkeypatch):
        """Applicare un rho NON cambia il default di CODICE del modello.

        Dopo una promozione `poisson_engine.RHO` e' il valore installato: se il
        riferimento della stima lo seguisse, un secondo fit misurerebbe il
        miglioramento contro il parametro appena applicato (circolo vizioso: il
        gate non potrebbe piu' accorgersi di un peggioramento).
        """
        monkeypatch.setenv("DIXON_COLES_APPLY", "1")
        out = dc.promote(matches=_simulate(-0.20, n=4000),
                         expected_goals_fn=_eg_const((1.4, 1.2)))
        assert out["applied"] is True
        assert pe.RHO == pytest.approx(out["rho"])
        assert pe.DEFAULT_RHO == pytest.approx(-0.15)
        assert dc.default_rho() == pytest.approx(-0.15)


class TestPromozione:
    """`promote()`: fit -> salvataggio -> applicazione, in quest'ordine.

    E' il percorso che il job di produzione usa ogni giorno: qui si verifica che
    ogni passo possa solo FERMARE il successivo (fail-closed) e che
    l'applicazione cambi davvero le probabilita' del MOTORE (non un oggetto
    in memoria).
    """

    def test_promuove_e_cambia_il_motore(self, monkeypatch):
        # Campione GENERATO DAL MODELLO con un rho lontano dal default: la
        # stima lo ritrova e la calibrazione 1X2 (fuori campione) migliora
        # davvero, quindi il gate di calibrazione lascia passare.
        lam = (1.6, 1.2)
        before = pe.prob_1x2(*lam)
        monkeypatch.setenv("DIXON_COLES_APPLY", "1")
        out = dc.promote(matches=_simulate(-0.20, n=4000, lam=lam))
        assert out["ok"] is True and out["reason"] == "ok"
        assert out["saved"] is True and out["applied"] is True
        assert out["changed"] is True                 # era -0.15
        assert out["previous"] == pytest.approx(-0.15)
        # Il MOTORE ha davvero il nuovo parametro...
        assert pe.RHO == pytest.approx(out["rho"])
        assert dc.engine_rho() == pytest.approx(out["rho"])
        assert out["engine_rho"] == pytest.approx(out["rho"])
        # ...e le probabilita' sono cambiate (l'applicazione non e' un no-op).
        after = pe.prob_1x2(*lam)
        assert after != before
        assert sum(after) == pytest.approx(1.0)
        # La stima e' finita sul VOLUME (cio' che sopravvive al redeploy).
        assert dc.load_fit()["rho"] == pytest.approx(out["rho"])

    def test_interruttore_spento_salva_ma_non_applica(self):
        out = dc.promote(matches=_simulate(-0.08, n=4000))
        assert out["ok"] is True and out["saved"] is True
        assert out["applied"] is False
        assert out["apply_reason"] == "interruttore_off"
        assert out["changed"] is False
        assert pe.RHO == -0.15                        # modello intatto
        assert out["engine_rho"] == pytest.approx(-0.15)
        assert dc.load_fit()["ok"] is True            # la misura resta

    def test_fuori_banda_salva_e_non_applica(self, monkeypatch):
        monkeypatch.setenv("DIXON_COLES_APPLY", "1")
        # Gate di calibrazione spento: qui si sta esercitando la BANDA, e senza
        # questo il motivo riportato sarebbe quello della calibrazione (che nel
        # percorso viene prima dell'applicazione).
        monkeypatch.setenv("DIXON_COLES_CALIB_GATE", "0")
        monkeypatch.setenv("DIXON_COLES_SAFE_MIN", "-0.02")
        monkeypatch.setenv("DIXON_COLES_SAFE_MAX", "-0.01")
        out = dc.promote(matches=_simulate(-0.08, n=4000))
        assert out["ok"] is True and out["saved"] is True
        assert out["applied"] is False
        assert out["apply_reason"] == "fuori_banda_di_sicurezza"
        assert out["safe"] is False
        assert pe.RHO == -0.15

    def test_stima_fallita_non_cancella_quella_buona(self, monkeypatch):
        good = dc.promote(matches=_simulate(-0.08, n=4000))
        assert good["ok"] is True
        before = dc.load_fit()
        bad = dc.promote(matches=_matches(4))          # campione insufficiente
        assert bad["ok"] is False
        assert bad["reason"].startswith("stima_non_riuscita")
        assert bad["saved"] is False
        # La stima buona e' ancora li': un giorno senza dati non deve
        # cancellare il parametro in uso.
        assert dc.load_fit()["rho"] == pytest.approx(before["rho"])
        assert pe.RHO == -0.15                         # e il motore e' intatto

    def test_salvataggio_impossibile_non_applica(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DIXON_COLES_APPLY", "1")
        blocker = tmp_path / "file"
        blocker.write_text("x", encoding="utf-8")
        out = dc.promote(matches=_simulate(-0.08, n=4000),
                         path=blocker / "sub" / "rho.json")
        assert out["ok"] is True and out["saved"] is False
        assert out["applied"] is False
        assert out["apply_reason"] == "salvataggio_non_riuscito"
        assert pe.RHO == -0.15

    def test_secondo_giro_stesso_dato_non_e_un_cambio(self, monkeypatch):
        monkeypatch.setenv("DIXON_COLES_APPLY", "1")
        matches = _simulate(-0.20, n=4000)
        first = dc.promote(matches=matches)
        assert first["changed"] is True
        second = dc.promote(matches=matches)
        assert second["applied"] is True and second["changed"] is False
        assert pe.RHO == pytest.approx(first["rho"])

    def test_ledger_vuoto_non_solleva(self, tmp_path, monkeypatch):
        monkeypatch.setattr(dc, "_db_path", lambda: str(tmp_path / "mai.db"))
        monkeypatch.setenv("DIXON_COLES_APPLY", "1")
        out = dc.promote()
        assert out["ok"] is False and out["saved"] is False
        assert out["n_matches"] == 0
        assert pe.RHO == -0.15

    def test_engine_rho_mai_un_eccezione(self):
        assert isinstance(dc.engine_rho(), float)

    def test_report_promozione_dichiara_motore_e_esito(self):
        applied = dc.format_promotion(
            {"ok": True, "rho": -0.11, "n_matches": 900, "n_used": 880,
             "half_life": 100.0, "applied": True, "previous": -0.15,
             "engine_rho": -0.11, "improvement": 12.0})
        assert "-0.1100" in applied and "APPLICATO" in applied
        assert "motore ora -0.1100" in applied
        skipped = dc.format_promotion(
            {"ok": True, "rho": -0.11, "n_matches": 900, "n_used": 880,
             "half_life": 100.0, "applied": False,
             "apply_reason": "interruttore_off", "engine_rho": -0.15})
        assert "non applicato" in skipped and "-0.15" in skipped
        failed = dc.format_promotion({"ok": False, "reason": "stima_non_riuscita:x",
                                      "n_matches": 3, "engine_rho": -0.15})
        assert "nessuna promozione" in failed

    def test_cli_promote_su_ledger(self, tmp_path, monkeypatch, capsys):
        rows = [{"home": "H%d" % i, "away": "A%d" % i,
                 "score_home": i % 3, "score_away": (i + 1) % 3}
                for i in range(120)]
        monkeypatch.setattr(dc, "_db_path", lambda: _temp_db(tmp_path, rows))
        assert dc.main(["--promote", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is True and payload["saved"] is True

    def test_cli_promote_senza_dati_esce_1(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(dc, "_db_path", lambda: str(tmp_path / "mai.db"))
        assert dc.main(["--promote"]) == 1
        assert "nessuna promozione" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Cablaggio nel bot (job) e nella IaC
# ---------------------------------------------------------------------------
class _FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))


class _FakeContext:
    def __init__(self):
        self.bot = _FakeBot()


def _job_out(**over):
    out = {"ok": True, "rho": -0.11, "n_matches": 900, "n_used": 880,
           "half_life": 100.0, "applied": True, "previous": -0.15,
           "engine_rho": -0.11, "apply_reason": "ok", "changed": True,
           "improvement": 12.0}
    out.update(over)
    return out


class TestCablaggioBot:
    """Il blocco di promozione e' COLLEGATO: senza job sarebbe codice morto."""

    @pytest.fixture()
    def bot_src(self):
        return Path("bot.py").read_text(encoding="utf-8")

    def test_job_registrato_e_giornaliero(self, bot_src):
        assert "async def dixon_coles_job" in bot_src
        assert "run_daily(dixon_coles_job" in bot_src
        # Anche al BOOT: il rho applicato e' un globale di processo e un
        # redeploy lo azzererebbe fino alle 05:20 (deriva silenziosa).
        assert "run_once(dixon_coles_job" in bot_src

    def test_job_solo_admin_e_anti_spam_per_valore(self, bot_src):
        body = bot_src.split("async def dixon_coles_job")[1].split(
            "async def market_shadow_job")[0]
        assert "_admin_chat_ids()" in body            # mai gli iscritti
        assert "is_notified(\"DIXON_COLES\"" in body
        assert '"rho:%s"' in body                    # anti-spam per VALORE
        assert 'if not out.get("changed")' in body    # notifica solo sul cambio

    def test_job_logga_sempre(self, monkeypatch):
        import bot as bot_mod
        monkeypatch.setattr(dc, "promote", lambda **kw: _job_out(changed=False,
                                                                 applied=False))
        monkeypatch.setattr(bot_mod, "_admin_chat_ids", lambda: [1])
        ctx = _FakeContext()
        asyncio.run(bot_mod.dixon_coles_job(ctx))
        assert ctx.bot.sent == []                     # nessun cambio -> niente spam

    def test_job_notifica_sul_cambio(self, monkeypatch):
        import bot as bot_mod
        import tracker
        monkeypatch.setattr(dc, "promote", lambda **kw: _job_out())
        monkeypatch.setattr(bot_mod, "_admin_chat_ids", lambda: [111, 222])
        marked = []
        monkeypatch.setattr(tracker, "is_notified", lambda *a: False)
        monkeypatch.setattr(tracker, "mark_notified",
                            lambda *a: marked.append(a))
        ctx = _FakeContext()
        asyncio.run(bot_mod.dixon_coles_job(ctx))
        assert len(ctx.bot.sent) == 2                 # tutti gli admin
        assert "Dixon-Coles" in ctx.bot.sent[0][1]
        assert marked and marked[0][1] == "rho:-0.11"

    def test_job_spento_da_env(self, monkeypatch):
        import bot as bot_mod
        monkeypatch.setenv("DIXON_COLES_ENABLED", "0")
        calls = []
        monkeypatch.setattr(dc, "promote", lambda **kw: calls.append(1) or {})
        asyncio.run(bot_mod.dixon_coles_job(_FakeContext()))
        assert calls == []

    def test_job_errore_non_crasha(self, monkeypatch):
        import bot as bot_mod
        def boom(**kw):
            raise RuntimeError("stima rotta")
        monkeypatch.setattr(dc, "promote", boom)
        asyncio.run(bot_mod.dixon_coles_job(_FakeContext()))  # non solleva


class TestIaC:
    """Le env devono stare in `preserve()`: un `config apply` non le distrugge."""

    def test_env_dichiarate(self):
        iac = Path(".railway/railway.ts").read_text(encoding="utf-8")
        for name in ("DIXON_COLES_ENABLED", "DIXON_COLES_APPLY",
                     "DIXON_COLES_STATE", "DIXON_COLES_DAYS",
                     "DIXON_COLES_MAX_MATCHES", "DIXON_COLES_MAX_GOALS",
                     "DIXON_COLES_RHO_MIN", "DIXON_COLES_RHO_MAX",
                     "DIXON_COLES_RHO_STEP", "DIXON_COLES_REFINE_STEP",
                     "DIXON_COLES_SAFE_MIN", "DIXON_COLES_SAFE_MAX",
                     "DIXON_COLES_HALF_LIFE_DAYS"):
            assert f"{name}: preserve()" in iac, name

    def test_ogni_env_letta_e_dichiarata(self):
        import re
        iac = Path(".railway/railway.ts").read_text(encoding="utf-8")
        src = _code_only(dc.__file__)
        read = set(re.findall(
            r"_env_(?:float|int|bool)\(\s*['\"]([A-Z0-9_]+)['\"]", src))
        read |= set(re.findall(
            r"os\.getenv\(\s*['\"]([A-Z0-9_]+)['\"]", src))
        read |= {dc.RHO_FILE_ENV}
        wanted = {n for n in read
                  if n.startswith("DIXON_") or n == dc.RHO_FILE_ENV}
        missing = {n for n in wanted if f"{n}: preserve()" not in iac}
        assert not missing, missing


class TestRefine:
    def test_delega_alla_griglia_del_modello(self):
        got = dc.refine_probabilities(1.6, 1.1, rho=-0.05)
        with pe.rho_value(-0.05):
            p1, px, p2 = pe.prob_1x2(1.6, 1.1)
        assert got["p1"] == pytest.approx(p1)
        assert got["pX"] == pytest.approx(px)
        assert got["p2"] == pytest.approx(p2)
        assert sum([got["p1"], got["pX"], got["p2"]]) == pytest.approx(1.0)

    def test_rho_negativo_alza_il_pareggio(self):
        rho_neg = dc.refine_probabilities(1.6, 1.1, rho=-0.15)
        rho_zero = dc.refine_probabilities(1.6, 1.1, rho=0.0)
        assert rho_neg["pX"] > rho_zero["pX"]

    def test_non_lascia_il_modello_modificato(self):
        dc.refine_probabilities(1.6, 1.1, rho=0.02)
        assert pe.RHO == -0.15


# ---------------------------------------------------------------------------
# Report e CLI
# ---------------------------------------------------------------------------
class TestReport:
    def test_report_dichiara_stima_e_banda(self):
        text = dc.format_report({"ok": True, "rho": -0.11, "n_used": 900,
                                 "n_matches": 950, "improvement": 12.5,
                                 "safe": True, "saved_at": "2026-10-15"})
        assert "DIXON-COLES" in text
        assert "-0.1100" in text and "dentro" in text
        assert "DIXON_COLES_APPLY" in text

    def test_report_senza_stima_non_solleva(self):
        text = dc.format_report({"ok": False, "reason": "nessuna_stima"})
        assert "nessuna stima" in text

    def test_cli_status_json(self, tmp_path, monkeypatch, capsys):
        dc.save_fit({"ok": True, "rho": -0.11}, path=tmp_path / "rho.json")
        monkeypatch.setenv(dc.RHO_FILE_ENV, str(tmp_path / "rho.json"))
        assert dc.main(["--status", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["rho"] == -0.11

    def test_cli_fit_su_ledger(self, tmp_path, monkeypatch, capsys):
        rows = [{"home": "H%d" % i, "away": "A%d" % i,
                 "score_home": i % 3, "score_away": (i + 1) % 3}
                for i in range(80)]
        monkeypatch.setattr(dc, "_db_path", lambda: _temp_db(tmp_path, rows))
        assert dc.main(["--fit", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert "ok" in payload and "n_matches" in payload

    def test_cli_uso(self, capsys):
        assert dc.main(["--boh"]) == 2


# ---------------------------------------------------------------------------
# Tripwire
# ---------------------------------------------------------------------------
class TestTripwire:
    def _src(self) -> str:
        return _code_only(dc.__file__)

    def test_sorgente_senza_scritture_sul_ledger(self):
        src = self._src()
        for token in ("INSERT", "UPDATE ", "DELETE FROM", "save_bet(",
                      "save_prediction(", "save_clv("):
            assert token not in src, token

    def test_sorgente_senza_rete_ne_ordini(self):
        src = self._src()
        for token in ("import requests", "httpx", "odds_api", "sx_signals",
                      "_live_fill", "place_limit_order", "resolve_market_for",
                      "execution_engine", "value_filter", "auto_bet"):
            assert token not in src, token

    def test_non_ricopia_la_griglia(self):
        """La griglia dei punteggi vive in `poisson_engine`: qui non si copia."""
        src = self._src()
        for token in ("_probs_grid =", "def _probs_grid", "1.0 - RHO",
                      "draw correlation"):
            assert token not in src, token

    def test_usa_il_gancio_del_modello(self):
        src = self._src()
        assert "rho_value" in src or "poisson_engine" in src

    def test_import_leggero(self):
        code = ("import sys, dixon_coles;"
                "print(any(m in sys.modules for m in "
                "('tracker','auto_bet','bot','odds_api')))")
        out = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT),
                             capture_output=True, text=True, timeout=120)
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == "False"
