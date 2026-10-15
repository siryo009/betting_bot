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
                 "DIXON_COLES_APPLY", dc.RHO_FILE_ENV):
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
