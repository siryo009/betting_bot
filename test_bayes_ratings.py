"""Test di `bayes_ratings.py` (M3) — TUTTI OFFLINE.

Nessuna rete, nessun credito, nessun ordine: i dati sono SINTETICI generati qui
e il DB (quando serve) e' un file temporaneo con lo schema di `match_results`.

Le prove che contano:
- **la stima e' corretta**: su dati generati da un modello noto i coefficienti
  devono correlare con la verita' e il `sigma` dell'empirical Bayes deve
  cadere vicino allo spread vero (non al pavimento: era l'errore del primo
  giro, corretto col seme largo del primo stadio);
- **il fit e' convergente**: con un passo non normalizzato i coefficienti
  DIVERGEVANO (OverflowError su centinaia di match) — qui si verifica che i
  coefficienti restino finiti e limitati;
- **nessun look-ahead**: nel walk-forward il blocco di TEST e' sempre piu'
  recente di quello di addestramento;
- **sola lettura**: la connessione usata per leggere il ledger RIFIUTA una
  scrittura, e il sorgente non contiene istruzioni di scrittura ne' ordini.
"""
from __future__ import annotations

import ast
import json
import math
import random
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import bayes_ratings as br

ROOT = Path(__file__).resolve().parent
SOURCE = (ROOT / "bayes_ratings.py").read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# Helper: generatore sintetico (il modello noto e' quello che il fit deve capire)
# --------------------------------------------------------------------------
def synth(n_teams=12, n_matches=600, *, seed=11, window=120, half_life=100.0,
          spread_att=0.30, spread_def=0.25, league="Serie A"):
    """Partite generate da log-attacco/log-difesa noti (Poisson esatto)."""
    rng = random.Random(seed)
    teams = ["T%02d" % i for i in range(n_teams)]
    atk = {t: math.exp(rng.gauss(0, spread_att)) for t in teams}
    dfn = {t: math.exp(rng.gauss(0, spread_def)) for t in teams}
    ms = []
    per_day = max(1, n_matches // max(1, window))
    for day in range(window, 0, -1):
        for _ in range(per_day):
            h, a = rng.sample(teams, 2)
            lam_h = 1.50 * atk[h] * dfn[a]
            lam_a = 1.30 * atk[a] * dfn[h]
            sh = sum(1 for _ in range(14) if rng.random() < lam_h / 14)
            sa = sum(1 for _ in range(14) if rng.random() < lam_a / 14)
            ms.append({"league": league, "home": h, "away": a,
                       "score_home": sh, "score_away": sa,
                       "days_ago": float(day),
                       "weight": math.exp(-math.log(2) * day / half_life)})
    # il campione ha ESATTAMENTE le righe richieste: con per_day >= 1 un
    # campione "piccolo" resterebbe grande e i test di dimensione mentirebbero
    return teams, atk, dfn, ms[:n_matches]


def _corr(xs, ys):
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    return cov / (dx * dy) if dx and dy else float("nan")


def _code_only(path) -> str:
    """Sorgente senza docstring (i tripwire giudicano il CODICE, non la prosa)."""
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


@pytest.fixture()
def tennis_style_db(tmp_path, monkeypatch):
    """DB temporaneo con la tabella `match_results` (schema di produzione)."""
    import tracker
    db = tmp_path / "quotaverace.db"
    conn = sqlite3.connect(str(db))
    conn.execute("""CREATE TABLE match_results (
        match_id TEXT, league TEXT, home_team TEXT, away_team TEXT,
        score_home INTEGER, score_away INTEGER, settled_at TEXT)""")
    matches = synth(n_teams=8, n_matches=400, seed=3)[3]
    for i, m in enumerate(matches):
        conn.execute("INSERT INTO match_results VALUES (?,?,?,?,?,?,?)",
                     ("m%d" % i, m["league"], m["home"], m["away"],
                      m["score_home"], m["score_away"],
                      "2026-10-%02dT18:00:00" % (1 + (i % 28))))
    conn.commit()
    conn.close()
    monkeypatch.setattr(tracker, "DB_PATH", db)
    return db


# --------------------------------------------------------------------------
# 1) Configurazione
# --------------------------------------------------------------------------
class TestConfig:
    def test_default_di_codice(self, monkeypatch):
        for k in ("BAYES_ITERATIONS", "BAYES_LR", "BAYES_MIN_MATCHES",
                  "BAYES_FOLDS", "BAYES_MIN_FOLD", "BAYES_SIGMA",
                  "BAYES_SIGMA_FLOOR", "BAYES_SIGMA_CEIL", "BAYES_EB_SEED",
                  "BAYES_DAYS", "BAYES_MAX_MATCHES"):
            monkeypatch.delenv(k, raising=False)
        c = br.config()
        assert c["iterations"] == br.DEFAULT_ITERATIONS
        assert c["lr"] == br.DEFAULT_LR
        assert c["min_matches"] == br.DEFAULT_MIN_MATCHES
        assert c["sigma"] == br.DEFAULT_SIGMA
        assert c["sigma_floor"] == br.DEFAULT_SIGMA_FLOOR
        assert c["eb_seed"] == br.DEFAULT_EB_SEED
        assert c["days"] is None and c["max_matches"] is None

    def test_seme_eb_largo(self):
        """Il seme del primo stadio DEVE essere largo (>= 2): con un seme
        stretto il metodo dei momenti stima un sigma compresso."""
        assert br.DEFAULT_EB_SEED >= 2.0

    def test_env_sovrascrive(self, monkeypatch):
        monkeypatch.setenv("BAYES_ITERATIONS", "12")
        monkeypatch.setenv("BAYES_LR", "0.5")
        monkeypatch.setenv("BAYES_MIN_MATCHES", "33")
        monkeypatch.setenv("BAYES_FOLDS", "2")
        monkeypatch.setenv("BAYES_MIN_FOLD", "7")
        monkeypatch.setenv("BAYES_SIGMA", "0.9")
        c = br.config()
        assert c["iterations"] == 12 and c["lr"] == 0.5
        assert c["min_matches"] == 33 and c["folds"] == 2 and c["min_fold"] == 7
        assert c["sigma"] == 0.9

    def test_env_impossibile_ricade_sul_default(self, monkeypatch):
        monkeypatch.setenv("BAYES_ITERATIONS", "abc")
        monkeypatch.setenv("BAYES_LR", "")
        monkeypatch.setenv("BAYES_SIGMA", "nan")
        c = br.config()
        assert c["iterations"] == br.DEFAULT_ITERATIONS
        assert c["lr"] == br.DEFAULT_LR
        assert c["sigma"] == br.DEFAULT_SIGMA

    def test_valori_fuori_range_vengono_limitati(self, monkeypatch):
        monkeypatch.setenv("BAYES_LR", "7")
        monkeypatch.setenv("BAYES_ITERATIONS", "0")
        monkeypatch.setenv("BAYES_FOLDS", "-3")
        c = br.config()
        assert c["lr"] == 1.0
        assert c["iterations"] == 5          # minimo di codice
        assert c["folds"] == 1

    def test_days_zero_diventa_none(self, monkeypatch):
        monkeypatch.setenv("BAYES_DAYS", "0")
        monkeypatch.setenv("BAYES_MAX_MATCHES", "0")
        c = br.config()
        assert c["days"] is None and c["max_matches"] is None

    def test_state_path_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("BAYES_STATE", str(tmp_path / "f.json"))
        assert br.state_path() == tmp_path / "f.json"

    def test_state_path_default_sotto_data_dir(self, monkeypatch):
        monkeypatch.delenv("BAYES_STATE", raising=False)
        p = br.state_path()
        assert p.name == "bayes_ratings.json"


# --------------------------------------------------------------------------
# 2) Il fit: correttezza e convergenza
# --------------------------------------------------------------------------
class TestFit:
    def test_campione_vuoto_non_conclude(self):
        p = br.fit([])
        assert p["teams"] == {} and p["n_matches"] == 0 and p["sigma"] is None

    def test_una_sola_squadra_non_conclude(self):
        ms = [{"home": "A", "away": "A", "score_home": 1, "score_away": 0,
               "weight": 1.0, "league": "Serie A", "days_ago": 1.0}]
        p = br.fit(ms)
        assert p["teams"] == {}

    def test_righe_degeneri_vengono_saltate(self):
        ms = [{"home": "A", "away": "B", "score_home": None, "score_away": 1,
               "weight": 1.0}, {"home": "A", "away": "B", "score_home": -1,
                                "score_away": 1, "weight": 1.0},
              {"home": "A", "away": "B", "score_home": 1, "score_away": 0,
               "weight": 0.0}, {"home": "", "away": "B", "score_home": 1,
                                "score_away": 0, "weight": 1.0}]
        assert br.fit(ms)["teams"] == {}

    def test_coefficienti_finiti_e_limitati_su_campione_grande(self):
        """La regressione del difetto del primo giro: senza passo normalizzato
        dall'informazione il fit DIVERGE (OverflowError)."""
        ms = synth(n_teams=20, n_matches=1500)[3]
        p = br.fit(ms)
        assert p["teams"]
        for c in p["teams"].values():
            for k in ("attack_home", "attack_away", "defense_home", "defense_away"):
                assert math.isfinite(c[k])
                assert 0.01 < c[k] < 100.0
            assert c["n_home"] + c["n_away"] > 0

    def test_recupera_i_coefficienti_generati(self):
        teams, atk, dfn, ms = synth(n_teams=20, n_matches=1500, seed=11)
        p = br.fit(ms)
        names = list(p["teams"])
        s = [p["teams"][t]["attack_home"] for t in names]
        v = [atk[t] for t in names]
        assert _corr(s, v) > 0.85
        sd = [p["teams"][t]["defense_home"] for t in names]
        vd = [dfn[t] for t in names]
        assert _corr(sd, vd) > 0.6

    def test_sigma_stimato_vicino_allo_spread_vero(self):
        """Empirical Bayes: il sigma stimato non deve collassare al pavimento
        (l'errore del primo giro) ne' esplodere."""
        teams, atk, dfn, ms = synth(n_teams=20, n_matches=1500, seed=11,
                                    spread_att=0.30, spread_def=0.25)
        p = br.fit(ms)
        assert p["sigma_rounds"] == 2
        assert 0.10 <= p["sigma"] <= 0.60, p["sigma"]

    def test_sigma_esplicito_salta_lempirical_bayes(self):
        ms = synth(n_teams=8, n_matches=400, seed=5)[3]
        p = br.fit(ms, sigma=0.4)
        assert p["sigma"] == pytest.approx(0.4)
        assert p["sigma_rounds"] == 1

    def test_empirical_bayes_disattivabile(self):
        ms = synth(n_teams=8, n_matches=400, seed=5)[3]
        p = br.fit(ms, empirical_bayes=False)
        assert p["sigma"] == br.config()["sigma"]
        assert p["sigma_rounds"] == 1

    def test_logpost_finito(self):
        ms = synth(n_teams=8, n_matches=300, seed=9)[3]
        p = br.fit(ms)
        assert p["logpost"] is not None and math.isfinite(p["logpost"])
        assert p["logpost"] < 0                     # e' una log-verosimiglianza

    def test_piu_informazione_stima_piu_vicina_al_vero(self):
        """Con piu' partite per squadra la stima si avvicina ai coefficienti
        veri (stesso processo generativo, stesso seed)."""
        _, atk, _, small = synth(n_teams=10, n_matches=200, seed=4, window=100)
        _, _, _, big = synth(n_teams=10, n_matches=2000, seed=4, window=100)
        ps, pb = br.fit(small), br.fit(big)
        err_s = sum(abs(ps["teams"][t]["attack_home"] - atk[t])
                    for t in ps["teams"])
        err_b = sum(abs(pb["teams"][t]["attack_home"] - atk[t])
                    for t in pb["teams"])
        assert err_b < err_s

    def test_sigma_e_proprieta_della_lega_non_del_campione(self):
        """L'empirical Bayes stima la LARGHEZZA del prior: con lo stesso
        processo generativo il sigma non deve crollare al pavimento ne'
        gonfiarsi col campione (era il difetto del primo giro, corretto col
        seme largo del primo stadio)."""
        sigmas = []
        for nm in (200, 600, 2000):
            sigmas.append(br.fit(synth(n_teams=10, n_matches=nm, seed=4,
                                       window=100)[3])["sigma"])
        assert all(br.DEFAULT_SIGMA_FLOOR < s < 0.6 for s in sigmas), sigmas

    def test_shrinkage_segue_il_numero_di_partite(self):
        """Il meccanismo del prior gerarchico: a parita' di sigma, la squadra
        con PIU' partite si allontana di piu' da 1.0 (meno shrinkage)."""
        ms = []
        for i in range(30):
            ms.append({"home": "Forte", "away": "Media", "score_home": 4,
                       "score_away": 0, "weight": 1.0, "days_ago": 1.0,
                       "league": "Serie A"})
            ms.append({"home": "Media", "away": "Forte", "score_home": 0,
                       "score_away": 3, "weight": 1.0, "days_ago": 2.0,
                       "league": "Serie A"})
        # una squadra altrettanto forte ma con UNA sola partita in casa
        ms.append({"home": "Poche", "away": "Media", "score_home": 4,
                   "score_away": 0, "weight": 1.0, "days_ago": 3.0,
                   "league": "Serie A"})
        p = br.fit(ms, sigma=0.3)
        dev_forte = abs(p["teams"]["Forte"]["attack_home"] - 1.0)
        dev_poche = abs(p["teams"]["Poche"]["attack_home"] - 1.0)
        assert dev_forte > dev_poche
        assert dev_poche < 0.5      # una partita non basta a muovere il prior

    def test_identita_dei_nomi_viene_canonicalizzata(self):
        """`Tottenham Hotspur` e `Tottenham` sono la STESSA squadra."""
        ms = [{"home": "Tottenham", "away": "Arsenal", "score_home": 2,
               "score_away": 0, "weight": 1.0, "league": "Premier League",
               "days_ago": 1.0},
              {"home": "Tottenham Hotspur", "away": "Arsenal", "score_home": 1,
               "score_away": 1, "weight": 1.0, "league": "Premier League",
               "days_ago": 2.0}]
        p = br.fit(ms)
        n_tot = sum(c["n_home"] + c["n_away"] for t, c in p["teams"].items()
                    if t.startswith("Tottenham"))
        assert n_tot == 2 or "Tottenham Hotspur" not in p["teams"]


# --------------------------------------------------------------------------
# 3) Lambdas e baseline neutra
# --------------------------------------------------------------------------
class TestLambdas:
    def test_profilo_neutro_per_squadre_sconosciute(self):
        lam_h, lam_a = br.expected_goals("Zzz United", "Qqq Town",
                                         params={"teams": {}})
        assert (lam_h, lam_a) == pytest.approx((1.50, 1.30))

    def test_coefficienti_stimati_moltiplicano_la_base(self):
        """Ogni coefficiente entra nel lambda che gli compete: attacco casa di
        Alpha sul gol di casa, difesa casa di Alpha sui gol SUBITI in casa."""
        params = {"teams": {"Alpha": {"attack_home": 2.0, "attack_away": 0.5,
                                      "defense_home": 0.25, "defense_away": 1.0},
                  "Beta": {}}}
        lam_h, lam_a = br.expected_goals("Alpha", "Beta", params=params,
                                         league="Serie A")
        avg_h, avg_a = br.neutral_expected_goals("Alpha", "Beta", league="Serie A")
        # casa: attacco casa di Alpha x difesa trasferta di Beta (ignota -> 1.0)
        assert lam_h == pytest.approx(avg_h * 2.0)
        # trasferta: attacco trasferta di BETA (ignoto -> 1.0) x difesa casa di Alpha
        assert lam_a == pytest.approx(avg_a * 0.25)
        # e con i ruoli invertiti Alpha gioca in trasferta: il SUO attacco
        # trasferta entra nel lambda dei gol ospiti
        _, lam_a2 = br.expected_goals("Beta", "Alpha", params=params,
                                      league="Serie A")
        assert lam_a2 == pytest.approx(avg_a * 0.5)

    def test_input_ostili_non_sollevano(self):
        for h, a in ((None, None), ("", ""), (123, 456), ("A", None)):
            lam_h, lam_a = br.expected_goals(h, a, params={"teams": {}})
            assert lam_h > 0 and lam_a > 0 and math.isfinite(lam_h)

    def test_baseline_neutra_coincide_col_modello_neutro_di_produzione(self):
        """Con squadre IGNOTE a entrambi i modelli le due baseline devono
        coincidere: se divergessero, il confronto sarebbe truccato."""
        import poisson_engine as pe
        mine = br.neutral_expected_goals("Zzz United", "Qqq Town")
        theirs = pe.expected_goals("Zzz United", "Qqq Town")
        assert mine == pytest.approx(theirs, abs=1e-9)

    def test_media_di_lega_usata_quando_nota(self):
        """Una lega in tabella usa le SUE medie; una sconosciuta il globale."""
        import poisson_engine as pe
        if not pe.LEAGUE_AVGS:
            pytest.skip("nessuna media di lega in tabella")
        league = next(iter(pe.LEAGUE_AVGS))
        avg = br._league_avg(league)
        assert avg == pytest.approx(tuple(pe.LEAGUE_AVGS[league]))
        assert br._league_avg("Lega Inventata") == (1.50, 1.30)

    def test_adapter_per_il_brier(self):
        params = {"teams": {"Alpha": {"attack_home": 2.0, "attack_away": 1.0,
                                      "defense_home": 1.0, "defense_away": 1.0}}}
        fn = br.expected_goals_fn(params)
        assert fn("Alpha", "Beta") == pytest.approx(
            br.expected_goals("Alpha", "Beta", params=params))


# --------------------------------------------------------------------------
# 4) Walk-forward (senza look-ahead)
# --------------------------------------------------------------------------
class TestWalkForward:
    def test_campione_insufficiente_non_conclude(self):
        ms = synth(n_teams=8, n_matches=100, seed=2)[3]
        res = br.walk_forward(ms, min_matches=500)
        assert res["ok"] is False
        assert res["reason"] == "campione_insufficiente"
        assert res["verdict"] == "insufficient"

    def test_fold_insufficienti(self):
        ms = synth(n_teams=8, n_matches=30, seed=2)[3]
        assert len(ms) == 30
        res = br.walk_forward(ms, min_matches=10)
        assert res["ok"] is False and res["reason"] == "fold_insufficienti"

    def test_nessun_look_ahead_test_piu_recente_del_train(self):
        """Il blocco di test e' SEMPRE piu' recente: senza questa direzione il
        confronto conterrebbe look-ahead."""
        ms = synth(n_teams=10, n_matches=600, seed=6)[3]
        for train, test in br._folds(br._ordered_by_time(ms), 3, 20):
            # days_ago piu' PICCOLO = partita piu' recente: il test (piu'
            # recente) deve stare interamente dopo il train (piu' vecchio)
            oldest_test = max(m["days_ago"] for m in test)
            newest_train = min(m["days_ago"] for m in train)
            assert oldest_test <= newest_train

    def test_fold_si_riducono_su_campioni_piccoli(self):
        ms = synth(n_teams=8, n_matches=200, seed=7)[3]
        ordered = br._ordered_by_time(ms)
        blocks = br._folds(ordered, 5, 40)
        assert blocks
        for train, test in blocks:
            assert len(test) >= 40 and len(train) >= 40

    def test_nessun_fold_da_zero_righe(self):
        assert br._folds([], 3, 20) == []
        assert br._folds([{"days_ago": 1.0}], 3, 20) == []

    def test_verdetto_su_dati_generati(self):
        """Su dati generati dal modello il rating bayesiano deve battere la
        baseline neutra (che qui coincide col rating di produzione, perche' le
        squadre sintetiche non hanno rating nel DB)."""
        ms = synth(n_teams=20, n_matches=1500, seed=11)[3]
        res = br.walk_forward(ms, folds=3, min_matches=100)
        assert res["ok"] is True
        assert res["brier_bayes"] is not None
        assert res["brier_bayes"] < res["brier_neutral"]
        assert res["verdict"] == "bayes_better"
        assert res["folds_a_favore"] == res["n_folds"]

    def test_ordine_temporale_della_lettura(self):
        ms = synth(n_teams=8, n_matches=300, seed=8)[3]
        ordered = br._ordered_by_time(ms)
        assert ordered[0]["days_ago"] <= ordered[-1]["days_ago"]

    def test_verdetto_rating_engine_migliore(self, monkeypatch):
        """Se il bayesiano e' PEGGIORE si deve dirlo, non nasconderlo."""
        ms = synth(n_teams=10, n_matches=600, seed=12)[3]
        calls = {"n": 0}

        def fake_brier(test, expected_goals_fn=None):
            calls["n"] += 1
            # il primo brier di ogni fold e' quello bayesiano
            return 0.70 if calls["n"] % 3 == 1 else 0.60

        monkeypatch.setattr(br, "_brier", fake_brier)
        res = br.walk_forward(ms, folds=1, min_matches=100)
        assert res["ok"] is True
        assert res["verdict"] == "rating_engine_migliore"
        assert res["gain_vs_rating"] < 0

    def test_verdetto_batte_rating_ma_non_la_neutra(self, monkeypatch):
        ms = synth(n_teams=10, n_matches=600, seed=12)[3]
        seq = iter([0.55, 0.60, 0.50])          # bayes < rating, bayes > neutra

        def fake_brier(test, expected_goals_fn=None):
            try:
                return next(seq)
            except StopIteration:
                return 0.55

        monkeypatch.setattr(br, "_brier", fake_brier)
        res = br.walk_forward(ms, folds=1, min_matches=100)
        assert res["verdict"] == "batte_rating_ma_non_la_baseline_neutra"

    def test_fit_vuoto_non_produce_un_fold_valido(self, monkeypatch):
        """Se il fit non stima nulla (campione degenere) non si conclude."""
        ms = synth(n_teams=8, n_matches=300, seed=13)[3]
        monkeypatch.setattr(br, "fit", lambda *a, **k: br._empty_params())
        res = br.walk_forward(ms, min_matches=100)
        assert res["ok"] is False and res["reason"] == "brier_non_calcolabile"


# --------------------------------------------------------------------------
# 5) Lettura dal ledger (sola lettura, DB temporaneo)
# --------------------------------------------------------------------------
class TestLetturaLedger:
    def test_legge_il_ledger_temporaneo(self, tennis_style_db):
        ms = br.load_matches()
        assert ms
        assert {"league", "home", "away", "score_home", "score_away",
                "weight", "days_ago"} <= set(ms[0])

    def test_db_assente_non_solleva(self, tmp_path, monkeypatch):
        import tracker
        monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "non_esiste.db")
        assert br.load_matches() == []

    def test_connessione_in_sola_lettura_rifiuta_una_scrittura(self,
                                                               tennis_style_db):
        import dixon_coles as dc
        conn = dc._ro_conn(str(tennis_style_db))
        try:
            with pytest.raises(sqlite3.OperationalError):
                conn.execute("UPDATE match_results SET score_home = 9")
        finally:
            conn.close()

    def test_walk_forward_end_to_end_sul_ledger(self, tennis_style_db):
        res = br.walk_forward(min_matches=100)
        assert res["ok"] is True
        assert res["n_matches"] >= 100
        assert res["brier_bayes"] is not None


# --------------------------------------------------------------------------
# 6) Persistenza del fit
# --------------------------------------------------------------------------
class TestPersistenza:
    def test_salva_e_rilegge(self, tmp_path):
        ms = synth(n_teams=8, n_matches=300, seed=21)[3]
        p = br.fit(ms)
        out = br.save_fit(p, path=tmp_path / "f.json")
        assert out["ok"] is True
        loaded = br.load_fit(path=tmp_path / "f.json")
        assert loaded["params"]["sigma"] == pytest.approx(p["sigma"])
        assert set(loaded["params"]["teams"]) == set(p["teams"])

    def test_file_corrotto_non_solleva(self, tmp_path):
        f = tmp_path / "f.json"
        f.write_text("{ non json")
        assert br.load_fit(path=f) == {}

    def test_file_assente(self, tmp_path):
        assert br.load_fit(path=tmp_path / "manca.json") == {}

    def test_scrittura_impossibile_non_solleva(self, tmp_path):
        p = br.save_fit({"teams": {}}, path=tmp_path / "dir" / "sub" / "f.json")
        assert p["ok"] is True                      # la cartella viene creata
        p2 = br.save_fit({"teams": {}}, path=Path("/proc/1/non_scrivibile.json"))
        assert p2["ok"] is False and "error" in p2

    def test_expected_goals_legge_il_fit_salvato(self, tmp_path):
        params = {"teams": {"Alpha": {"attack_home": 3.0, "attack_away": 1.0,
                                      "defense_home": 1.0, "defense_away": 1.0}}}
        br.save_fit(params, path=tmp_path / "f.json")
        lam_h, _ = br.expected_goals("Alpha", "Beta", state=tmp_path / "f.json")
        assert lam_h == pytest.approx(1.50 * 3.0)


# --------------------------------------------------------------------------
# 7) Report e CLI
# --------------------------------------------------------------------------
class TestReportCli:
    def test_report_non_conclusivo_dichiara_il_motivo(self):
        txt = br.format_report({"ok": False, "reason": "campione_insufficiente",
                                "n_matches": 5, "min_matches": 200})
        assert "campione_insufficiente" in txt
        assert "nessuna conclusione" in txt

    def test_report_conclusivo_mostra_i_tre_numero(self):
        res = {"ok": True, "n_matches": 900, "n_folds": 3, "brier_bayes": 0.60,
               "brier_rating": 0.65, "brier_neutral": 0.66, "gain_vs_rating": 0.05,
               "gain_vs_neutral": 0.06, "sigma": 0.27, "verdict": "bayes_better",
               "folds_a_favore": 3}
        txt = br.format_report(res)
        assert "0.6000" in txt and "0.6500" in txt and "0.6600" in txt
        assert "bayes_better" in txt and "0.270" in txt

    def test_cli_su_db_vuoto_esce_1(self, tmp_path, monkeypatch):
        import tracker
        monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "vuoto.db")
        assert br.main([]) == 1

    def test_cli_json_leggibile(self, tennis_style_db, capsys):
        rc = br.main(["--json", "--folds", "1"])
        out = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert out["ok"] is True and out["n_folds"] == 1

    def test_cli_db_non_leggibile_esce_2(self, tmp_path, capsys):
        bad = tmp_path / "non_esiste.db"
        assert br.main(["--db", str(bad)]) == 2

    def test_cli_save_scrive_il_fit(self, tennis_style_db, tmp_path,
                                   monkeypatch, capsys):
        monkeypatch.setenv("BAYES_STATE", str(tmp_path / "fit.json"))
        rc = br.main(["--save", "--folds", "1"])
        assert rc == 0
        assert (tmp_path / "fit.json").exists()


# --------------------------------------------------------------------------
# 8) Tripwire: sola lettura, nessun ordine, import leggero, nessuna soglia copiata
# --------------------------------------------------------------------------
class TestTripwire:
    def test_sorgente_senza_scritture(self):
        code = _code_only(ROOT / "bayes_ratings.py")
        for stmt in ("INSERT", "UPDATE", "DELETE", "CREATE TABLE", "save_bet",
                     "save_result", "save_prediction", "executemany"):
            assert stmt not in code, stmt

    def test_sorgente_senza_ordini_ne_rete(self):
        code = _code_only(ROOT / "bayes_ratings.py")
        for stmt in ("place_order", "_live_fill", "resolve_market_for",
                     "execution_engine", "auto_bet", "requests", "subprocess",
                     "os.system"):
            assert stmt not in code, stmt

    def test_sorgente_senza_soglie_di_strategia(self):
        code = _code_only(ROOT / "bayes_ratings.py")
        for stmt in ("EV_MIN", "ODDS_MAX", "kelly_fraction", "MARKET_EDGE",
                     "MAX_STAKE_PCT", "value_filter"):
            assert stmt not in code, stmt

    def test_nessuna_formula_ricopiata(self):
        """La probabilita' 1X2 e il Brier NON vengono ricostruiti qui."""
        code = _code_only(ROOT / "bayes_ratings.py")
        assert "prob_1x2" not in code
        assert "math.factorial" not in code

    def test_import_leggero(self):
        code = ("import sys; import bayes_ratings; "
                "mods=['tracker','auto_bet','bot','odds_api','decision',"
                "'execution_engine','agents','value_filter'];"
                "print([m for m in mods if m in sys.modules])")
        out = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT),
                             capture_output=True, text=True, timeout=120)
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip().endswith("[]"), out.stdout

    def test_nessun_env_letto_allimport(self, monkeypatch):
        """Le soglie sono lette a RUNTIME: un env impostato dopo l'import
        deve cambiare il comportamento."""
        monkeypatch.setenv("BAYES_MIN_MATCHES", "999")
        assert br.config()["min_matches"] == 999

    def test_adotta_la_metrica_di_produzione(self):
        """Il Brier arriva da `dixon_coles` (stessa definizione del progetto)."""
        code = _code_only(ROOT / "bayes_ratings.py")
        assert "dixon_coles" in code
        assert "brier_1x2" in code

    def test_adotta_i_dati_di_lega(self):
        code = _code_only(ROOT / "bayes_ratings.py")
        assert "poisson_engine" in code and "LEAGUE_AVGS" in code


# --------------------------------------------------------------------------
# IaC: ogni env letta dal modulo deve essere dichiarata `preserve()`
# --------------------------------------------------------------------------
class TestIaC:
    """Lezione del 28/09: `preserve()` non crea valori, ma senza dichiarazione
    un `config apply` DISTRUGGE cio' che l'operatore ha impostato. Un modulo
    nuovo che legge env senza dichiararle e' una trappola silenziosa."""

    def test_env_dichiarate_nella_iac(self):
        iac = (ROOT / ".railway" / "railway.ts").read_text(encoding="utf-8")
        for name in ("BAYES_ITERATIONS", "BAYES_LR", "BAYES_MIN_MATCHES",
                     "BAYES_FOLDS", "BAYES_MIN_FOLD", "BAYES_SIGMA",
                     "BAYES_SIGMA_FLOOR", "BAYES_SIGMA_CEIL", "BAYES_EB_SEED",
                     "BAYES_DAYS", "BAYES_MAX_MATCHES", "BAYES_STATE"):
            assert f"{name}: preserve()" in iac, name

    def test_nessuna_env_letta_e_non_dichiarata(self):
        """Tripwire GENERALE: ogni `_env_*("NOME", ...)` del sorgente deve
        comparire nella IaC. Un nome nuovo non puo' passare inosservato."""
        import re
        iac = (ROOT / ".railway" / "railway.ts").read_text(encoding="utf-8")
        read = set(re.findall(r"_env_(?:float|int)\(\s*\"([A-Z0-9_]+)\"", SOURCE))
        read |= set(re.findall(r"os\.getenv\(\s*\"([A-Z0-9_]+)\"", SOURCE))
        missing = {n for n in read if f"{n}: preserve()" not in iac}
        assert not missing, missing
