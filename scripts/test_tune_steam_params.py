"""Test di `scripts/tune_steam_params.py` — tutti OFFLINE, DB temporanei.

Coprono le promesse che rendono il tuning USABILE e non pericoloso:

1. i parametri di partenza si LEGGONO da `steam_move` (mai copiati) e i nomi
   di env stampati sono quelli VERI (`STEAM_MOVE_PCT`, non il
   `STEAM_MOVE_MIN_DROP_PCT` citato dalla direttiva);
2. nessun look-ahead: il segnale e' il PRIMO trigger della serie, e il
   trigger sull'ULTIMO snapshot e' scartato e contato a parte (li' il CLV e' 0
   per costruzione);
3. il CLV arriva da `market_calib.clv_raw` (nessuna formula riscritta qui);
4. anti-overfitting: fold TEMPORALI, media dei fold meno penalita' di
   stabilita', pavimento sui trade dichiarato;
5. sola LETTURA (il test tenta una scrittura e pretende che SQLite la
   rifiuti) e nessun effetto sull'ambiente.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import _ledger_fixture as fx
import market_calib as mc
import tune_steam_params as tsp

#: Base dei tempi costruiti a mano: relativa fra loro, mai confrontata con
#: "adesso" (queste funzioni non guardano l'orologio).
BASE = datetime(2026, 1, 1, 12, 0, 0)


def series(mid: str, esito: str, minutes, prices, offset_min: float = 0.0) -> dict:
    """Serie con `times` a partire da BASE (+ offset): nessuna data fissa."""
    start = BASE + timedelta(minutes=offset_min)
    return {
        "match_id": mid, "esito": esito,
        "times": [start + timedelta(minutes=m) for m in minutes],
        "prices": [float(p) for p in prices],
    }


def drop_series(n: int, *, signal_base: float = 1.90, step_min: float = 60.0) -> list:
    """`n` serie che innescano lo steam, con CLV DIVERSO fra loro.

    Il crollo e' il primo punto (`index 1`), quindi c'e' sempre una chiusura
    successiva: senza varieta' il CLV sarebbe costante e lo Sharpe (per
    costruzione media/std) varrebbe 0 su ogni fold.
    """
    out = []
    for i in range(n):
        signal = signal_base - 0.005 * (i % 10)
        closing = 1.75 + 0.002 * (i % 5)
        out.append(series(f"m{i}", "1", (0.0, 20.0, 40.0), (2.00, signal, closing),
                          offset_min=i * step_min))
    return out


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "ledger.db"
    conn = fx.make_db(path)
    yield path, conn
    fx.close(conn)


# ---------------------------------------------------------------------------
# 1. Produzione: i parametri si leggono, i nomi di env sono quelli veri
# ---------------------------------------------------------------------------

class TestProduzione:
    def test_config_letta_da_steam_move(self):
        import steam_move
        prod = tsp.production_config()
        assert prod["source"] == "steam_move"
        assert prod["move_pct"] == pytest.approx(steam_move.move_pct())
        assert prod["window_min"] == pytest.approx(steam_move.window_min())
        assert prod["min_window_min"] == pytest.approx(steam_move.min_window_min())

    def test_fallback_dichiarato_se_steam_move_non_importabile(self, monkeypatch):
        """`source` dichiara la provenienza: 'default' non e' 'produzione'."""
        monkeypatch.setitem(sys.modules, "steam_move", None)
        prod = tsp.production_config()
        assert prod["source"] == "default"
        assert prod["move_pct"] == tsp.DEFAULT_MOVE_PCT

    def test_nomi_di_env_veri_non_quelli_della_direttiva(self):
        """Il nome sbagliato puo' stare nella PROSA, mai fra gli env da impostare."""
        assert tsp.ENV_NAMES["move_pct"] == "STEAM_MOVE_PCT"
        assert "STEAM_MOVE_MIN_DROP_PCT" not in tsp.ENV_NAMES.values()
        assert "STEAM_MOVE_MIN_DROP_PCT" not in tsp.UNSEARCHABLE

    def test_dedup_escluso_con_il_motivo(self):
        """Ottimizzare un parametro che la storia non vede = valore casuale."""
        assert "dedup_min" in tsp.UNSEARCHABLE
        env_name, why = tsp.UNSEARCHABLE["dedup_min"]
        assert env_name == "STEAM_MOVE_DEDUP_MIN"
        assert "scritture" in why.lower() or "storia" in why.lower()
        assert "dedup_min" not in [n for n, _, _ in tsp.PARAM_SPACE]

    def test_spazio_di_ricerca_esattamente_tre_parametri(self):
        assert [n for n, _, _ in tsp.PARAM_SPACE] == [
            "move_pct", "window_min", "min_window_min"]
        for name, low, high in tsp.PARAM_SPACE:
            assert low < high, name


# ---------------------------------------------------------------------------
# 2. Trigger: primo punto, nessun look-ahead, finestra e span
# ---------------------------------------------------------------------------

class TestFirstTrigger:
    PARAMS = dict(move_pct=0.04, window_min=30.0, min_window_min=15.0)

    def test_e_il_primo_trigger_non_il_minimo_futuro(self):
        """Il segnale è il PRIMO punto che scatta, non il minimo della serie."""
        s = series("m1", "1", (0, 20, 40), (2.00, 1.70, 1.60))
        trig = tsp.first_trigger(s["times"], s["prices"], **self.PARAMS)
        assert trig is not None
        assert trig["index"] == 1
        assert trig["signal_price"] == pytest.approx(1.70)
        assert trig["move_pct"] == pytest.approx(-15.0)

    def test_senza_crollo_nessun_trigger(self):
        s = series("m1", "1", (0, 20, 40), (2.00, 1.99, 2.01))
        assert tsp.first_trigger(s["times"], s["prices"], **self.PARAMS) is None

    def test_span_minimo_blocca_il_segnale(self):
        """Due letture a 10 minuti di distanza non misurano un movimento."""
        s = series("m1", "1", (0, 10), (2.00, 1.50))
        assert tsp.first_trigger(s["times"], s["prices"], move_pct=0.04,
                                 window_min=30.0, min_window_min=15.0) is None
        # Controprova: la stessa serie scatta se il pavimento di span scende.
        trig = tsp.first_trigger(s["times"], s["prices"], move_pct=0.04,
                                 window_min=30.0, min_window_min=0.0)
        assert trig is not None and trig["index"] == 1

    def test_la_finestra_limita_il_prezzo_di_partenza(self):
        """Il prezzo di confronto sta DENTRO la finestra, non a inizio serie."""
        s = series("m1", "1", (0, 10, 50), (2.00, 1.95, 1.80))
        trig = tsp.first_trigger(s["times"], s["prices"], move_pct=0.04,
                                 window_min=20.0, min_window_min=15.0)
        assert trig is not None
        assert trig["first_price"] == pytest.approx(1.95)
        assert trig["span_minutes"] == pytest.approx(40.0)

    def test_due_letture_contemporanee_non_misurano_nulla(self):
        s = series("m1", "1", (0, 0), (2.00, 1.50))
        assert tsp.first_trigger(s["times"], s["prices"], **self.PARAMS) is None

    def test_prezzo_non_positivo_scartato(self):
        s = series("m1", "1", (0, 20), (0.0, 1.00))
        assert tsp.first_trigger(s["times"], s["prices"], **self.PARAMS) is None

    def test_una_sola_lettura_non_basta(self):
        s = series("m1", "1", (0,), (2.00,))
        assert tsp.first_trigger(s["times"], s["prices"], **self.PARAMS) is None


# ---------------------------------------------------------------------------
# 3. Valutazione dei parametri: CLV, ROI, eventi non misurabili
# ---------------------------------------------------------------------------

class TestEvaluateParams:
    PARAMS = dict(move_pct=0.04, window_min=30.0, min_window_min=15.0)

    def test_trigger_sull_ultimo_snapshot_scartato_e_contato(self, db):
        """Li' la chiusura coincide con l'ingresso: CLV 0 per costruzione."""
        path, conn = db
        fx.add_series(conn, "m1", "1", [2.00, 1.80], start_minutes=-120.0,
                      step_minutes=20.0)
        res = tsp.evaluate_params(tsp.load_series(conn, "pinnacle"), {}, {},
                                  self.PARAMS)
        assert res["events"] == []
        assert res["skipped_no_closing"] == 1

    def test_clv_dalla_formula_di_market_calib(self, db):
        path, conn = db
        fx.add_series(conn, "m1", "1", [2.00, 1.80, 1.70], start_minutes=-120.0,
                      step_minutes=20.0)
        res = tsp.evaluate_params(tsp.load_series(conn, "pinnacle"), {}, {},
                                  self.PARAMS)
        assert len(res["events"]) == 1
        assert res["events"][0]["clv"] == pytest.approx(
            mc.clv_raw(1.80, 1.70), abs=1e-12)

    def test_roi_dalla_quota_presa_e_dall_esito(self, db):
        path, conn = db
        fx.add_series(conn, "m1", "1", [2.00, 1.80, 1.70], start_minutes=-120.0,
                      step_minutes=20.0)
        fx.add_bet(conn, "m1", "1", 1.90)
        series_list = tsp.load_series(conn, "pinnacle")
        taken = tsp.load_taken_prices(conn)
        assert taken[("m1", "1")] == pytest.approx(1.90)
        for verdict, atteso in (("won", 0.90), ("lost", -1.0)):
            winners = {("m1", "1"): verdict == "won"}
            res = tsp.evaluate_params(series_list, winners, taken, self.PARAMS)
            assert res["events"][0]["roi"] == pytest.approx(atteso)

    def test_senza_quota_presa_si_misura_solo_il_clv(self, db):
        """Nessun prezzo nel ledger: l'evento resta, il ROI no."""
        path, conn = db
        fx.add_series(conn, "m1", "1", [2.00, 1.80, 1.70], start_minutes=-120.0,
                      step_minutes=20.0)
        res = tsp.evaluate_params(tsp.load_series(conn, "pinnacle"),
                                  {}, {}, self.PARAMS)
        assert res["events"][0]["clv"] is not None
        assert res["events"][0]["roi"] is None

    def test_esito_dal_risultato_reale_via_canonical_outcome(self, db):
        """Senza verdicto in `predictions` si usa `match_results`."""
        path, conn = db
        fx.add_series(conn, "m1", "1", [2.00, 1.80, 1.70], start_minutes=-120.0,
                      step_minutes=20.0)
        fx.add_bet(conn, "m1", "1", 1.90)
        fx.add_result(conn, "m1", "1", home="Casa", away="Ospite")
        series_list = tsp.load_series(conn, "pinnacle")
        winners = tsp.load_winners(conn)
        taken = tsp.load_taken_prices(conn)
        res = tsp.evaluate_params(series_list, winners, taken, self.PARAMS)
        ev = res["events"][0]
        assert ev["won"] is True
        assert ev["roi"] == pytest.approx(0.90)

    def test_push_escluso_dagli_esiti(self, db):
        path, conn = db
        fx.add_series(conn, "m1", "1", [2.00, 1.80, 1.70], start_minutes=-120.0,
                      step_minutes=20.0)
        fx.add_prediction(conn, "m1", esito="1", quota=1.90, esito_finale="push")
        winners = tsp.load_winners(conn)
        res = tsp.evaluate_params(tsp.load_series(conn, "pinnacle"), winners,
                                  tsp.load_taken_prices(conn), self.PARAMS)
        assert res["events"][0]["won"] is None
        assert res["events"][0]["roi"] is None

    def test_min_window_mai_sopra_la_finestra(self):
        """`min_window_min` > `window_min` e' clampato: finestra impossibile."""
        s = [series("m1", "1", (0, 45, 90), (2.00, 1.70, 1.60))]
        params = dict(move_pct=0.04, window_min=30.0, min_window_min=60.0)
        # Clampato a 30 il trigger (span 45') esiste...
        assert len(tsp.evaluate_params(s, {}, {}, params)["events"]) == 1
        # ...e senza clamp (pavimento 60') non esisterebbe: il clamp conta.
        assert tsp.first_trigger(s[0]["times"], s[0]["prices"], move_pct=0.04,
                                 window_min=30.0, min_window_min=60.0) is None


class TestCaricamento:
    def test_serie_con_una_sola_lettura_esclusa(self, db):
        path, conn = db
        fx.add_snapshot(conn, "m1", "1", 2.00)
        assert tsp.load_series(conn, "pinnacle") == []

    def test_il_book_e_un_filtro(self, db):
        path, conn = db
        fx.add_series(conn, "m1", "1", [2.00, 1.80], bookmaker="BookA")
        assert tsp.load_series(conn, "pinnacle") == []
        assert len(tsp.load_series(conn, "BookA")) == 1

    def test_recorded_at_illeggibile_scartato(self, db):
        path, conn = db
        fx.add_series(conn, "m1", "1", [2.00, 1.80], start_minutes=-120.0)
        fx.add_snapshot(conn, "m1", "1", 1.70, when="non-una-data")
        got = tsp.load_series(conn, "pinnacle")
        assert len(got) == 1 and len(got[0]["prices"]) == 2

    def test_bets_vincono_sulle_predictions(self, db):
        path, conn = db
        fx.add_prediction(conn, "m1", esito="1", quota=2.10)
        fx.add_bet(conn, "m1", "1", 2.35)
        assert tsp.load_taken_prices(conn)[("m1", "1")] == pytest.approx(2.35)

    def test_serie_ordinate_cronologicamente(self, db):
        path, conn = db
        fx.add_series(conn, "m1", "1", [2.00, 1.80, 1.70], start_minutes=-120.0,
                      step_minutes=20.0)
        got = tsp.load_series(conn, "pinnacle")[0]
        assert got["times"] == sorted(got["times"])


# ---------------------------------------------------------------------------
# 4. Punteggio: Sharpe, CLV degenere, pesi
# ---------------------------------------------------------------------------

class TestScore:
    def test_sharpe_con_std_degenere_vale_zero(self):
        """Un CLV costante non ha un rapporto segnale/rumore: 0, non 1e15."""
        assert tsp.sharpe_of([0.01] * 6) == 0.0
        assert tsp.sharpe_of([0.0, 0.0]) == 0.0

    def test_sharpe_e_media_su_devstd(self):
        assert tsp.sharpe_of([0.03, 0.01, 0.02]) == pytest.approx(2.0)

    def test_meno_di_due_valori_zero(self):
        assert tsp.sharpe_of([0.05]) == 0.0
        assert tsp.sharpe_of([]) == 0.0

    def test_score_pesa_sharpe_e_roi(self):
        events = [{"clv": 0.03, "roi": 1.5, "won": True},
                  {"clv": 0.01, "roi": -1.0, "won": False}]
        both = tsp.score_events(events)
        assert both["score"] == pytest.approx(both["sharpe"] + both["roi"],
                                             abs=1e-9)
        only_sharpe = tsp.score_events(events, w_roi=0.0)
        assert only_sharpe["score"] == pytest.approx(only_sharpe["sharpe"])
        only_roi = tsp.score_events(events, w_sharpe=0.0)
        assert only_roi["score"] == pytest.approx(only_roi["roi"])

    def test_score_vuoto_none(self):
        assert tsp.score_events([]) is None

    def test_roi_ignoto_non_azzera_lo_sharpe(self):
        """Gli eventi senza quota presa contano per il CLV, non per il ROI."""
        events = [{"clv": 0.03, "roi": None, "won": None},
                  {"clv": 0.01, "roi": None, "won": None}]
        st = tsp.score_events(events)
        assert st["trades"] == 2 and st["trades_with_price"] == 0
        assert st["roi"] is None


# ---------------------------------------------------------------------------
# 5. Anti-overfitting: fold temporali, pavimento, penalita'
# ---------------------------------------------------------------------------

class TestAntiOverfitting:
    def test_fold_sono_blocchi_temporali_contigui(self):
        events = [{"time": BASE + timedelta(hours=i), "clv": 0.01 * i,
                   "roi": None, "won": None} for i in range(9)]
        folds = tsp.fold_events(events, 3)
        assert [len(f) for f in folds] == [3, 3, 3]
        for a, b in zip(folds, folds[1:]):
            assert max(e["time"] for e in a) <= min(e["time"] for e in b)

    def test_fold_non_casuali_anche_con_eventi_disordinati(self):
        events = [{"time": BASE + timedelta(hours=i), "clv": 0.01, "roi": None,
                   "won": None} for i in (5, 2, 9, 0)]
        folds = tsp.fold_events(events, 2)
        flat = [e for f in folds for e in f]
        assert [e["time"] for e in flat] == sorted(e["time"] for e in flat)

    def test_sotto_il_pavimento_di_trade_penalita_dichiarata(self):
        """Un parametro che scatta 3 volte non e' un risultato: PENALITA_SCORE."""
        s = drop_series(3)
        res = tsp.objective_for(s, {}, {}, {"move_pct": 0.04, "window_min": 30.0,
                                           "min_window_min": 15.0},
                               min_trades=50)
        assert res["score"] == tsp.PENALTY_SCORE
        assert res["reason"] == "insufficient_trades"
        assert res["events"] == 3
        assert res["folds"] == []

    def test_score_e_media_dei_fold_meno_penalita(self):
        s = drop_series(30)
        params = {"move_pct": 0.04, "window_min": 30.0, "min_window_min": 15.0}
        res = tsp.objective_for(s, {}, {}, params, folds=3, min_trades=3,
                                stability_penalty=0.5)
        assert res["reason"] == "ok"
        assert res["score"] == pytest.approx(
            res["fold_mean"] - 0.5 * res["fold_std"], abs=1e-4)
        assert len(res["folds"]) == 3

    def test_penalita_alta_non_migliora_mai_il_punteggio(self):
        s = drop_series(30)
        params = {"move_pct": 0.04, "window_min": 30.0, "min_window_min": 15.0}
        soft = tsp.objective_for(s, {}, {}, params, min_trades=3,
                                 stability_penalty=0.0)
        hard = tsp.objective_for(s, {}, {}, params, min_trades=3,
                                 stability_penalty=5.0)
        assert hard["score"] <= soft["score"]

    def test_il_fold_piu_recente_resta_per_la_verifica_oos(self):
        s = drop_series(30)
        res = tsp.objective_for(s, {}, {}, {"move_pct": 0.04, "window_min": 30.0,
                                            "min_window_min": 15.0},
                                folds=3, min_trades=3)
        assert res["last_fold"] == res["folds"][-1]

    def test_nessun_evento_dichiara_zero_trade(self):
        res = tsp.objective_for([], {}, {}, {"move_pct": 0.04,
                                             "window_min": 30.0,
                                             "min_window_min": 15.0},
                                min_trades=1)
        assert res["events"] == 0 and res["score"] == tsp.PENALTY_SCORE


# ---------------------------------------------------------------------------
# 6. Ricerca: Optuna se c'e', altrimenti random dichiarato; determinismo
# ---------------------------------------------------------------------------

class TestRicerca:
    KW = dict(trials=40, folds=3, min_trades=2, stability_penalty=0.5,
              w_sharpe=1.0, w_roi=1.0)

    def _search(self, seed=7, use_optuna=False, trials=40):
        s = drop_series(24)
        return tsp.search(s, {}, {}, seed=seed, use_optuna=use_optuna,
                          trials=trials, folds=3, min_trades=2,
                          stability_penalty=0.5, w_sharpe=1.0, w_roi=1.0)

    def test_stesso_seed_stessi_parametri(self):
        a, b = self._search(seed=7), self._search(seed=7)
        assert a["best"]["params"] == b["best"]["params"]
        assert a["best"]["score"] == pytest.approx(b["best"]["score"])
        assert a["engine"] == b["engine"]

    def test_seed_diverso_cambia_il_campione(self):
        a, b = self._search(seed=1), self._search(seed=2)
        first = [tuple(sorted(r["params"].items())) for r in a["results"][:5]]
        second = [tuple(sorted(r["params"].items())) for r in b["results"][:5]]
        assert first != second

    def test_i_parametri_stanno_nello_spazio_dichiarato(self):
        res = self._search(trials=30)
        bounds = {n: (lo, hi) for n, lo, hi in tsp.PARAM_SPACE}
        for r in res["results"]:
            assert set(r["params"]) == set(bounds)
            for name, value in r["params"].items():
                lo, hi = bounds[name]
                assert lo <= value <= hi, (name, value)
            assert r["params"]["min_window_min"] <= r["params"]["window_min"]

    def test_numero_di_prove_rispettato(self):
        assert self._search(trials=17)["trials"] == 17

    def test_fallback_random_dichiarato_senza_optuna(self, monkeypatch):
        monkeypatch.setattr(tsp, "optuna_available", lambda: False)
        assert self._search()["engine"] == "random"

    def test_con_optuna_il_motore_e_dichiarato(self, monkeypatch):
        monkeypatch.setattr(tsp, "optuna_available", lambda: True)
        try:
            import optuna  # noqa: F401
        except Exception:
            pytest.skip("Optuna non installata (requirements-scripts.txt)")
        assert self._search(use_optuna=True)["engine"] == "optuna_tpe"


# ---------------------------------------------------------------------------
# 7. Pipeline completa e report
# ---------------------------------------------------------------------------

def _seed_running_db(conn, n: int = 8) -> None:
    """Ledger che fa PARTIRE la ricerca: serie sharp + quota presa + esito.

    Quattro prezzi (non tre): col trigger sul PENULTIMO punto c'e' sempre una
    chiusura successiva, altrimenti ogni evento cadrebbe in
    `skipped_no_closing` e non ci sarebbe nulla da misurare.
    """
    for i in range(n):
        fx.add_series(conn, f"m{i}", "1", [2.00, 1.90, 1.80, 1.75],
                      start_minutes=-120.0)
        fx.add_prediction(conn, f"m{i}", esito="1", quota=1.90,
                          esito_finale="won" if i % 2 else "lost")


def _run_kwargs(**over) -> dict:
    kw = dict(book="pinnacle", trials=8, folds=2, min_trades=2, min_events=5,
              stability_penalty=0.5, w_sharpe=1.0, w_roi=1.0, seed=7,
              use_optuna=False)
    kw.update(over)
    return kw


class TestPipeline:
    def test_db_vuoto_non_esegue_e_dichiara_il_motivo(self, db):
        path, _ = db
        res = tsp.run(path, **_run_kwargs())
        assert "error" not in res
        assert res["ran"] is False
        assert res["coverage"]["serie_snapshot"] == 0
        assert "almeno" in res["reason"]

    def test_db_assente_e_un_errore_non_un_crash(self, tmp_path):
        res = tsp.run(tmp_path / "non_esiste.db", **_run_kwargs())
        assert "error" in res and res["ran"] is False

    def test_serie_insufficienti_non_avviano_la_ricerca(self, db):
        path, conn = db
        fx.add_series(conn, "m1", "1", [2.00, 1.80, 1.75], start_minutes=-120.0)
        res = tsp.run(path, **_run_kwargs(min_events=30))
        assert res["ran"] is False
        assert "30" in res["reason"]

    def test_senza_esiti_reali_la_ricerca_non_parte(self, db):
        path, conn = db
        for i in range(6):
            fx.add_series(conn, f"m{i}", "1", [2.00, 1.80, 1.75],
                          start_minutes=-120.0)
        res = tsp.run(path, **_run_kwargs())
        assert res["ran"] is False
        assert "esito" in res["reason"]

    def test_pipeline_completa_su_ledger_seminato(self, db):
        path, conn = db
        _seed_running_db(conn)
        res = tsp.run(path, **_run_kwargs())
        assert res["ran"] is True
        assert res["coverage"]["serie_snapshot"] == 8
        assert res["coverage"]["con_esito_noto"] == 8
        assert res["coverage"]["quote_prese_nel_ledger"] == 8
        assert res["engine"] == "random"
        assert res["best"] is not None
        assert res["current"]["events"] >= 1
        assert res["production"]["source"] == "steam_move"

    def test_report_dichiara_motore_e_comandi_non_eseguiti(self, db):
        path, conn = db
        _seed_running_db(conn)
        res = tsp.run(path, **_run_kwargs())
        txt = tsp.format_report(res, tsp.production_config(), res["coverage"])
        assert "Motore" in txt and "random" in txt
        assert "MIGLIORE" in txt
        assert "railway variables --service betting_bot --set" in txt
        assert "NON imposta nulla" in txt
        assert "NON ottimizzabili" in txt

    def test_report_senza_ricerca_spiega_perche(self, db):
        path, _ = db
        res = tsp.run(path, **_run_kwargs())
        txt = tsp.format_report(res, tsp.production_config(), res["coverage"])
        assert "ricerca NON eseguita" in txt

    def test_cli_json_non_trascina_tutta_la_tabella(self, db, capsys):
        path, conn = db
        _seed_running_db(conn)
        code = tsp.main(["--db", str(path), "--json", "--no-optuna",
                         "--trials", "5", "--min-events", "5",
                         "--min-trades", "2"])
        assert code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["read_only"] is True
        assert "results" not in payload and isinstance(payload["top"], list)

    def test_cli_db_assente_esce_uno(self, tmp_path, capsys):
        assert tsp.main(["--db", str(tmp_path / "x.db")]) == 1
        assert "ERRORE" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 8. Sola lettura + tripwire (nessuna scrittura, nessuna rete, nessun env)
# ---------------------------------------------------------------------------

class TestSolaLettura:
    def test_la_connessione_rifiuta_una_scrittura(self, db):
        path, _ = db
        conn = tsp.connect_readonly(path)
        try:
            with pytest.raises(sqlite3.OperationalError):
                conn.execute("UPDATE price_snapshots SET price=9.99")
        finally:
            conn.close()

    def test_file_inesistente_solleva(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            tsp.connect_readonly(tmp_path / "non_esiste.db")

    def test_la_uri_e_in_sola_lettura(self, db, monkeypatch):
        """La modalita' e' dichiarata nell'URI, non implicita."""
        path, _ = db
        seen = {}
        real = sqlite3.connect

        def spy(database, *a, **kw):
            seen["uri"] = database
            return real(database, *a, **kw)

        monkeypatch.setattr(tsp.sqlite3, "connect", spy)
        tsp.connect_readonly(path).close()
        assert "mode=ro" in seen["uri"]


class TestTripwire:
    def test_source_non_scrive_mai(self):
        src = Path(tsp.__file__).read_text(encoding="utf-8")
        for bad in ("INSERT INTO", "UPDATE ", "DELETE FROM", "mode=rw",
                    "mode=rwc", 'open(', "save_bet", "save_prediction",
                    "save_clv"):
            assert bad not in src, bad

    def test_source_non_tocca_la_rete_ne_gli_ordini(self):
        src = Path(tsp.__file__).read_text(encoding="utf-8")
        for bad in ("requests", "aiohttp", "httpx", "odds_api", "sx_signals",
                    "execution_engine", "_live_fill", "place_order",
                    "run_all"):
            assert bad not in src, bad

    def test_source_non_imposta_variabili_d_ambiente(self):
        """Il tool CONSIGLIA, non applica: la decisione resta umana."""
        src = Path(tsp.__file__).read_text(encoding="utf-8")
        for bad in ("os.environ[", "putenv", "load_dotenv", "subprocess",
                    "os.system", "setenv"):
            assert bad not in src, bad

    def test_il_clv_arriva_da_market_calib(self):
        src = Path(tsp.__file__).read_text(encoding="utf-8")
        assert "from market_calib import clv_raw" in src
        assert "clv_raw(" in src

    def test_import_leggero(self):
        """Importare lo script non tira dentro il loop di produzione."""
        import os as _os
        code = ("import sys; sys.path.insert(0, %r); sys.path.insert(0, %r); "
                "import tune_steam_params as t; "
                "print(any(m in sys.modules for m in "
                "('tracker', 'bot', 'auto_bet', 'steam_move', 'decision')))")
        here = _os.path.dirname(_os.path.abspath(tsp.__file__))
        out = subprocess.run([sys.executable, "-c", code % (here, str(tsp._ROOT))],
                             capture_output=True, text=True, timeout=90)
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == "False", out.stdout

    def test_env_consigliate_dichiarate_nella_iac(self):
        """`config apply` distrugge cio' che non e' in `preserve()` (28/09)."""
        iac = (tsp._ROOT / ".railway" / "railway.ts").read_text(encoding="utf-8")
        names = list(tsp.ENV_NAMES.values()) + [
            env_name for env_name, _ in tsp.UNSEARCHABLE.values()]
        for name in names:
            assert name in iac, name
