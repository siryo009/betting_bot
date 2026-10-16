"""Test OFFLINE del harness walk-forward ML (`ml_harness.py`, 15/10/2026).

Zero rete, zero crediti, zero ordini: DB SQLite temporaneo con lo schema di
produzione, dati sintetici per la valutazione, date esplicite.

Cosa difendono (le proprieta' che rendono il verdetto utilizzabile):

1. **Nessun look-ahead**: le pieghe sono CRONOLOGICHE e il test e' sempre
   successivo al train; l'imputazione della mediana usa il SOLO train.
2. **Nessuna statistica reinventata**: il test sulle differenze appaiate e il
   campione minimo vengono da `significance`; l'AUC da `match_features`.
3. **Verdetto conservativo**: `promote` solo con miglioramento significativo su
   OGNI baseline e ranking non peggiore; `insufficient` sotto il campione
   minimo; un campione degenere (varianza della differenza nulla) NON promuove.
4. **Degradazione dichiarata**: senza motore di boosting il verdetto lo dice,
   invece di addestrare qualcos'altro in silenzio.
5. **Confini**: sola lettura (`mode=ro`), nessuna rete, nessun ordine, import
   leggero (`import ml_harness` non carica tracker/auto_bet/bot/execution_engine).
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

import match_features as mf
import ml_harness as mlh

SOURCE = Path(mlh.__file__).read_text(encoding="utf-8")
IAC = Path(".railway/railway.ts").read_text(encoding="utf-8")

#: Feature portanti del segnale sintetico (tutte in `HISTORY_FEATURES`).
F_SIGNAL = "pts5_home"
F_RIVAL = "pts5_away"
F_REST = "rest_days_home"


def _names():
    return tuple(mf.HISTORY_FEATURES)


# ---------------------------------------------------------------------------
# Dati sintetici per `evaluate` (nessun DB)
# ---------------------------------------------------------------------------

def _rows(n: int, *, signal_strength: float = 0.0, seed: int = 7):
    """Righe sintetiche con un segnale NON LINEARE controllato.

    La regola vera e': vince la casa se ha piu' punti recenti **E** ha riposato
    (`rest_days_home > 0`) — un'INTERAZIONE fra due feature, che una regressione
    lineare non puo' rappresentare. Serve a distinguere davvero boosting e
    logistica: con un segnale lineare i due modelli coinciderebbero e il
    verdetto sarebbe "flat" per costruzione, non per merito.

    `signal_strength` = frazione di righe generate dalla regola (il resto e'
    testa o croce): 0.0 = rumore puro.
    """
    import random
    rng = random.Random(seed)
    base = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
    out = []
    for i in range(n):
        home_pts, away_pts = rng.uniform(0.0, 3.0), rng.uniform(0.0, 3.0)
        rest = rng.uniform(-1.5, 1.5)
        if rng.random() < signal_strength:
            good = (home_pts > away_pts) and (rest > 0)
            lab = 1 if (good if rng.random() > 0.1 else not good) else 0
        else:
            lab = rng.randint(0, 1)
        row = {name: rng.uniform(-1.0, 1.0) for name in _names()}
        row[F_SIGNAL], row[F_RIVAL], row[F_REST] = home_pts, away_pts, rest
        out.append({"ts": base + i * 3 * 86400.0, "label": lab,
                    "league": "Serie A", "row": row})
    return out


def _ro(path):
    return mf.ro_conn(path)


@pytest.fixture()
def history_db(tmp_path):
    """Storia minima con 4 squadre che si incontrano piu' volte."""
    path = tmp_path / "hist.db"
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE match_results (
            match_id TEXT PRIMARY KEY, league TEXT, home_team TEXT,
            away_team TEXT, score_home INTEGER, score_away INTEGER,
            result TEXT, settled_at TEXT)""")
    base = datetime(2026, 3, 1, 18, 0, tzinfo=timezone.utc)
    teams = ["Inter", "Roma", "Napoli", "Lazio"]
    for i in range(40):
        conn.execute("INSERT INTO match_results VALUES (?,?,?,?,?,?,?,?)",
                     (f"m{i}", "Serie A", teams[i % 4], teams[(i + 1) % 4],
                      i % 3, (i + 1) % 2, "1",
                      (base + timedelta(days=4 * i)).isoformat()))
    conn.commit()
    conn.close()
    return path


# ---------------------------------------------------------------------------
# 1. Popolazione dal ledger
# ---------------------------------------------------------------------------

class TestDataset:

    def test_popola_dal_ledger_senza_lookahead(self, history_db):
        data = mlh.dataset(conn=_ro(history_db))
        assert not data["error"], data["error"]
        assert len(data["rows"]) >= 10, data["stats"]
        for banned in mf.LOOKAHEAD_UNSAFE:
            assert banned not in data["names"], banned

    def test_righe_cronologiche(self, history_db):
        data = mlh.dataset(conn=_ro(history_db))
        ts = [r["ts"] for r in data["rows"]]
        assert ts == sorted(ts), "il walk-forward vive sull'ordine temporale"

    def test_ogni_riga_porta_tempo_e_label(self, history_db):
        data = mlh.dataset(conn=_ro(history_db))
        assert data["rows"]
        for r in data["rows"]:
            assert isinstance(r["ts"], float) and r["label"] in (0, 1)

    def test_target_non_valido_dichiarato(self, history_db):
        data = mlh.dataset(conn=_ro(history_db), target="boh")
        assert data["error"] and data["rows"] == []

    def test_stats_dichiarano_cosa_e_stato_scartato(self, history_db):
        data = mlh.dataset(conn=_ro(history_db))
        assert "usable" in data["stats"] and "dropped" in data["stats"]

    def test_le_feature_senza_lookahead_sono_quelle_del_modulo(self, history_db):
        data = mlh.dataset(conn=_ro(history_db))
        assert tuple(data["names"]) == _names()


# ---------------------------------------------------------------------------
# 2. Pieghe cronologiche
# ---------------------------------------------------------------------------

class TestTemporalFolds:

    def test_il_test_e_sempre_dopo_il_train(self):
        rows = _rows(300)
        splits = mlh.temporal_folds(rows, folds=4, min_train=100)
        assert len(splits) == 4
        for tr, te in splits:
            assert int(tr.max()) < int(te.min())
            assert max(rows[i]["ts"] for i in tr) <= min(rows[i]["ts"] for i in te)

    def test_finestra_espandente(self):
        sizes = [len(tr) for tr, _ in
                 mlh.temporal_folds(_rows(300), folds=3, min_train=100)]
        assert sizes == sorted(sizes) and sizes[0] == 100

    def test_la_coda_del_campione_e_tutta_test(self):
        splits = mlh.temporal_folds(_rows(300), folds=3, min_train=100)
        assert int(splits[-1][1][-1]) == 299

    def test_campione_corto_non_produce_pieghe(self):
        assert mlh.temporal_folds(_rows(50), folds=4, min_train=100) == []

    def test_serie_non_ordinata_rifiutata(self):
        rows = _rows(300)
        rows[10]["ts"], rows[200]["ts"] = rows[200]["ts"], rows[10]["ts"]
        assert mlh.temporal_folds(rows, folds=3, min_train=100) == []

    def test_una_sola_piega_non_e_walk_forward(self):
        assert mlh.temporal_folds(_rows(300), folds=1, min_train=100) == []


# ---------------------------------------------------------------------------
# 3. Stimatori
# ---------------------------------------------------------------------------

class TestStimatori:

    def test_prior_e_la_frequenza_del_train(self):
        m = mlh.PriorModel().fit(np.zeros((4, 1)), np.array([1.0, 1.0, 0.0, 1.0]))
        assert m.predict(np.zeros((2, 1))).tolist() == [0.75, 0.75]

    def test_logistica_imputa_solo_col_train(self):
        """La mediana e la standardizzazione vengono dal TRAIN, mai dal test."""
        x_tr = np.array([[1.0, np.nan], [3.0, 2.0]])
        m = mlh.LogisticModel().fit(x_tr, np.array([1.0, 0.0]))
        assert m.med[0] == pytest.approx(2.0)
        assert m.med[1] == pytest.approx(2.0)
        assert m.mu[0] == pytest.approx(2.0)
        m.predict(np.array([[100.0, 0.0]]))     # il test NON sposta le medie
        assert m.med[0] == pytest.approx(2.0)

    def test_logistica_impara_un_segnale(self):
        rows = _rows(400, signal_strength=0.9)
        x, y = mlh.build_matrix(rows, _names())
        p = mlh.LogisticModel().fit(x[:300], y[:300]).predict(x[300:])
        assert mlh.brier(y[300:], p) < 0.25

    def test_boosting_dichiarato_se_disponibile(self):
        got = mlh.models_available()
        assert "boosting_engine" in got
        assert got["boosting"] == (got["boosting_engine"] is not None)

    def test_nessun_motore_e_dichiarato(self, monkeypatch):
        monkeypatch.setattr(mlh, "_pick_engine", lambda: None)
        res = mlh.evaluate(_rows(300, signal_strength=0.9), names=_names(),
                           folds=3, min_train=100)
        assert res["verdict"]["status"] == "insufficient"
        assert "non e' addestrabile" in res["verdict"]["reason"]

    def test_modello_sconosciuto_non_si_inventa(self):
        with pytest.raises(ValueError):
            mlh.make_model("transformer")

    def test_matrice_preserva_i_buchi(self):
        rows = _rows(5)
        rows[0]["row"].pop(F_REST)
        x, _ = mlh.build_matrix(rows, _names())
        assert np.isnan(x[0][_names().index(F_REST)])

    def test_matrice_ha_forma_y_coerente(self):
        x, y = mlh.build_matrix(_rows(7), _names())
        assert x.shape == (7, len(_names())) and y.shape == (7,)


# ---------------------------------------------------------------------------
# 4. Metriche
# ---------------------------------------------------------------------------

class TestMetriche:

    def test_brier_logloss_e_gap_esatti(self):
        y, p = np.array([1.0, 0.0]), np.array([0.8, 0.2])
        assert mlh.brier(y, p) == pytest.approx(0.04)
        assert mlh.logloss(y, p) == pytest.approx(-np.log(0.8))
        assert mlh.calibration_gap(y, p) == pytest.approx(0.0)

    def test_calibrazione_dichiara_loveryconfidence(self):
        y = np.array([1.0, 0.0, 0.0, 0.0])
        p = np.array([0.9, 0.9, 0.9, 0.9])
        assert mlh.calibration_gap(y, p) > 0

    def test_auc_delega_a_match_features(self):
        y = np.array([1.0, 1.0, 0.0, 0.0])
        p = np.array([0.9, 0.8, 0.2, 0.1])
        rows = [{"label": int(a), "score": float(b)} for a, b in zip(y, p)]
        assert mlh.auc(y, p) == mf.feature_auc(rows, "score")["auc"]
        assert mlh.auc(y, p) == pytest.approx(1.0)

    def test_auc_con_una_sola_classe_e_none(self):
        assert mlh.auc(np.array([1.0, 1.0]), np.array([0.5, 0.6])) is None

    def test_metriche_su_campione_vuoto(self):
        assert mlh.brier(np.array([]), np.array([])) is None
        assert mlh.logloss(np.array([]), np.array([])) is None
        assert mlh.calibration_gap(np.array([]), np.array([])) is None


# ---------------------------------------------------------------------------
# 5. Valutazione e verdetto
# ---------------------------------------------------------------------------

class TestValutazione:

    def test_segnale_forte_promuove(self):
        res = mlh.evaluate(_rows(1200, signal_strength=0.9), names=_names(),
                           folds=4, min_train=300)
        assert not res["error"], res["error"]
        pooled = res["pooled"]
        assert pooled["boosting"]["brier"] < pooled["logistica"]["brier"]
        assert pooled["boosting"]["brier"] < pooled["prior"]["brier"]
        assert res["verdict"]["status"] == "promote", res["verdict"]

    def test_rumore_puro_non_promuove(self):
        res = mlh.evaluate(_rows(800), names=_names(), folds=4, min_train=300)
        assert res["verdict"]["status"] != "promote"
        assert res["verdict"]["reason"]

    def test_campione_corto_e_insufficiente(self):
        res = mlh.evaluate(_rows(40), names=_names(), folds=3, min_train=30)
        assert res["verdict"]["status"] == "insufficient"
        assert res["verdict"]["reason"]

    def test_le_metriche_sono_out_of_fold(self):
        res = mlh.evaluate(_rows(900, signal_strength=0.9), names=_names(),
                           folds=4, min_train=300)
        assert res["n_oof"] == sum(f["n_test"] for f in res["folds"])
        assert res["pooled"]["prior"]["n"] == res["n_oof"]

    def test_confronti_appaiati_con_significativita(self):
        res = mlh.evaluate(_rows(1200, signal_strength=0.9), names=_names(),
                           folds=4, min_train=300)
        c = res["comparisons"]["boosting_vs_prior"]
        assert c["mean_brier_gain"] > 0 and c["p_value"] is not None
        assert c["ci95"][0] <= c["mean_brier_gain"] <= c["ci95"][1]
        assert c["detectable_brier_gain"] is not None
        assert c["verdict_vs_baseline"] == "better"

    def test_il_campione_degenere_non_promuove(self):
        """Varianza nulla: `significance` non inventa un p-value. Non si promuove."""
        base = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
        rows = []
        for i in range(80):
            row = {n: 0.0 for n in _names()}
            row[F_SIGNAL] = 1.0 if i % 2 == 0 else 0.0
            rows.append({"ts": base + i * 86400.0, "label": i % 2,
                         "league": "Serie A", "row": row})
        res = mlh.evaluate(rows, names=_names(), folds=3, min_train=40)
        assert res["verdict"]["status"] != "promote"

    def test_errore_dichiarato_e_mai_un_eccezione(self):
        res = mlh.evaluate([], names=_names())
        assert res["error"] == "nessuna_riga"
        assert res["verdict"]["status"] == "insufficient"

    def test_incumbent_fuori_dal_verdetto(self):
        """Il baseline LEAKY si legge, ma non entra nella decisione."""
        res = mlh.evaluate(_rows(600, signal_strength=0.9), names=_names(),
                           folds=3, min_train=200,
                           incumbent_fn=lambda r: 0.55)
        assert "incumbent" not in res["pooled"]
        assert not any("incumbent" in k for k in res["comparisons"])
        assert all(k.endswith(("_vs_prior", "_vs_logistica"))
                   for k in res["comparisons"])

    def test_ogni_piega_riporta_la_sua_finestra_temporale(self):
        res = mlh.evaluate(_rows(900, signal_strength=0.6), names=_names(),
                           folds=3, min_train=300)
        for f in res["folds"]:
            assert f["ts_from"] <= f["ts_to"]
            assert f["n_train"] > 0 and f["n_test"] > 0


# ---------------------------------------------------------------------------
# 6. Confini: sola lettura, offline, import leggero
# ---------------------------------------------------------------------------

class TestConfini:

    def test_nessun_percorso_di_ordine_o_scrittura(self):
        for banned in ("place_limit_order", "_live_fill", "save_bet(",
                       "execution_engine", "INSERT INTO", "UPDATE ", "DELETE FROM"):
            assert banned not in SOURCE, f"percorso vietato nel sorgente: {banned}"

    def test_nessuna_rete(self):
        for banned in ("requests", "httpx", "aiohttp", "odds_api", "sx_signals",
                       "fetch_scores"):
            assert banned not in SOURCE, f"rete nel sorgente: {banned}"

    def test_nessuna_soglia_di_strategia(self):
        for banned in ("EV_MIN", "ODDS_MIN", "MARKET_EDGE", "KELLY_",
                       "STAKE_CAP", "MIN_SAMPLES ="):
            assert banned not in SOURCE, f"soglia ricopiata: {banned}"

    def test_campione_minimo_e_statistica_delegati(self):
        assert "sig.MIN_SAMPLES" in SOURCE
        assert "sig.roi_test" in SOURCE
        assert "sig.detectable_edge" in SOURCE

    def test_import_leggero(self):
        out = subprocess.run(
            [sys.executable, "-c",
             "import sys, ml_harness; "
             "print(' '.join(m for m in ('tracker', 'auto_bet', 'bot',"
             " 'execution_engine', 'odds_api', 'match_features')"
             " if m in sys.modules))"],
            capture_output=True, text=True, cwd=str(Path(mlh.__file__).parent))
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == "", out.stdout

    def test_connessione_in_sola_lettura(self, history_db):
        conn = _ro(history_db)
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("UPDATE match_results SET score_home = 9")
        conn.close()

    def test_run_su_db_assente_non_solleva(self, tmp_path):
        res = mlh.run(db=str(tmp_path / "nonesiste.db"))
        assert (res.get("verdict") or {}).get("status") == "insufficient"
        assert res.get("error")


# ---------------------------------------------------------------------------
# 7. Env, report, CLI, IaC
# ---------------------------------------------------------------------------

class TestConfigEReport:

    def test_env_impossibile_ricade_sul_default(self, monkeypatch):
        monkeypatch.setenv("MLH_FOLDS", "abc")
        assert mlh.folds_count() == 4
        monkeypatch.setenv("MLH_FOLDS", "1")            # sotto il minimo
        assert mlh.folds_count() == 4
        monkeypatch.setenv("MLH_MIN_TRAIN", "5")
        assert mlh.min_train_rows() == 2000
        monkeypatch.setenv("MLH_SEED", "")
        assert mlh.seed() == 42

    def test_env_valida_leggibile(self, monkeypatch):
        monkeypatch.setenv("MLH_FOLDS", "6")
        assert mlh.folds_count() == 6

    def test_report_leggibile(self):
        res = mlh.evaluate(_rows(600, signal_strength=0.9), names=_names(),
                           folds=3, min_train=200)
        text = mlh.format_report(res)
        assert "ML HARNESS" in text and "Verdetto" in text
        assert "Brier" in text and "boosting" in text

    def test_report_su_valutazione_vuota(self):
        assert "insufficient" in mlh.format_report(
            mlh.evaluate([], names=_names()))

    def test_verdetto_ha_sempre_una_ragione(self):
        for rows in ([], _rows(40), _rows(600, signal_strength=0.6)):
            res = mlh.evaluate(rows, names=_names(), folds=3, min_train=30)
            assert res["verdict"]["reason"]

    def test_cli_esegue_e_restituisce_un_codice(self, tmp_path, capsys):
        code = mlh.main(["--json", "--folds", "2", "--min-train", "20",
                         "--db", str(tmp_path / "vuoto.db")])
        assert code == 1                                 # DB assente: nessun verdetto
        assert json.loads(capsys.readouterr().out)["verdict"]["status"] == \
            "insufficient"

    def test_il_risultato_e_json_serializzabile(self):
        res = mlh.evaluate(_rows(600, signal_strength=0.9), names=_names(),
                           folds=3, min_train=200)
        json.dumps(res, default=str)

    def test_env_dichiarate_nella_iac(self):
        for name in ("MLH_FOLDS", "MLH_MIN_TRAIN", "MLH_SEED"):
            assert f"{name}: preserve()" in IAC, name

    def test_nessuna_env_letta_e_non_dichiarata(self):
        import re
        read = set(re.findall(r"_(?:int_)?env(?:_any)?\(\s*\"([A-Z0-9_]+)\"",
                              SOURCE))
        missing = {n for n in read
                   if f"{n}: preserve()" not in IAC}
        assert not missing, missing
