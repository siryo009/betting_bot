"""Test di `scripts/clv_brier_analytics.py` — tutti OFFLINE, DB temporanei.

Coprono le tre promesse del tool: (1) lettura in SOLA LETTURA (il test tenta
una scrittura e pretende che SQLite la rifiuti), (2) le formule vengono da
`market_calib` e non riscritte qui, (3) il Brier del de-vigging e' fail-closed
sui mercati incompleti (mai un de-vig a 2 su 3).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import _ledger_fixture as fx
import clv_brier_analytics as cba
import market_calib as mc


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "ledger.db"
    conn = fx.make_db(path)
    yield path, conn
    fx.close(conn)


# ---------------------------------------------------------------------------
# 1. Sola lettura
# ---------------------------------------------------------------------------

class TestSolaLettura:
    def test_la_connessione_rifiuta_una_scrittura(self, db):
        path, _ = db
        conn = cba.connect_readonly(path)
        try:
            with pytest.raises(sqlite3.OperationalError):
                conn.execute("UPDATE predictions SET quota=9.99")
        finally:
            conn.close()

    def test_file_inesistente_solleva(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            cba.connect_readonly(tmp_path / "non_esiste.db")

    def test_analyze_non_solleva_su_db_assente(self, tmp_path):
        res = cba.analyze(tmp_path / "vuoto.db")
        assert "error" in res and res["db"].endswith("vuoto.db")

    def test_since_illeggibile_e_un_errore_esplicito(self, db):
        path, _ = db
        with pytest.raises(ValueError):
            cba._since_dt("non-una-data")

    def test_since_accetta_z_e_offset(self):
        assert cba._since_dt("2026-09-19T10:00:00Z") is not None
        assert cba._since_dt("2026-09-19T10:00:00+02:00") is not None
        assert cba._since_dt(None) is None


# ---------------------------------------------------------------------------
# 2. CLV
# ---------------------------------------------------------------------------

class TestCLV:
    def test_clv_da_clv_history_riusa_la_formula_di_market_calib(self, db):
        path, conn = db
        fx.add_clv(conn, "m1", "1", 2.10, 2.00, pinnacle_quota=2.00)
        res = cba.analyze(path)
        st = res["clv"]["overall"]["raw"]
        assert st["n"] == 1
        assert st["mean_pct"] == pytest.approx(mc.clv_raw(2.10, 2.00) * 100, abs=1e-6)

    def test_preferisce_la_quota_pinnacle_alla_chiusura_grezza(self, db):
        path, conn = db
        fx.add_clv(conn, "m1", "1", 2.10, 1.90, pinnacle_quota=2.00)
        samples = cba.build_clv_samples(
            cba.load_predictions(cba.connect_readonly(path)),
            cba.load_clv_history(cba.connect_readonly(path)), {}, {})
        assert samples[0]["closing"] == 2.00
        assert samples[0]["closing_source"] == "pinnacle"

    def test_senza_pinnacle_usa_la_chiusura_del_bookmaker(self, db):
        path, conn = db
        fx.add_clv(conn, "m1", "1", 2.10, 1.90)
        samples = cba.build_clv_samples(
            [], cba.load_clv_history(cba.connect_readonly(path)), {}, {})
        assert samples[0]["closing"] == 1.90
        assert samples[0]["closing_source"] == "bookmaker"

    def test_clv_vig_free_con_mercato_completo(self, db):
        """Con tutti e tre gli esiti la chiusura viene DEVIGATA: il numero
        deve combaciare con `market_calib.clv_vig_free` (nessuna copia)."""
        path, conn = db
        fx.add_clv(conn, "m1", "1", 2.10, 2.00, pinnacle_quota=2.00)
        for esito, price in (("1", 2.00), ("X", 3.40), ("2", 3.60)):
            fx.add_snapshot(conn, "m1", esito, price)
        res = cba.analyze(path)
        expected = mc.clv_vig_free(2.10, 2.00, [2.00, 3.40, 3.60], method="shin")
        assert res["clv"]["overall"]["vig_free_n"] == 1
        assert res["clv"]["overall"]["vig_free"]["mean_pct"] == pytest.approx(
            expected * 100, abs=1e-6)

    def test_chiusura_da_snapshot_quando_manca_clv_history(self, db):
        path, conn = db
        fx.add_prediction(conn, "m1", quota=2.10)
        fx.add_snapshot(conn, "m1", "1", 1.95)
        res = cba.analyze(path, book="pinnacle")
        assert res["coverage"]["clv_samples"] == 1
        st = res["clv"]["overall"]["raw"]
        assert st["mean_pct"] == pytest.approx(
            mc.clv_raw(2.10, 1.95) * 100, abs=1e-6)

    def test_il_prezzo_della_puntata_vince_sulla_quota_del_segnale(self, db):
        """La quota EFFETTIVAMENTE presa e' piu' autorevole del segnale."""
        path, conn = db
        fx.add_prediction(conn, "m1", quota=2.10)
        fx.add_bet(conn, "m1", "1", 2.35)
        fx.add_snapshot(conn, "m1", "1", 2.00)
        res = cba.analyze(path)
        assert res["coverage"]["clv_samples"] == 1
        assert res["clv"]["overall"]["raw"]["mean_pct"] == pytest.approx(
            mc.clv_raw(2.35, 2.00) * 100, abs=1e-6)

    def test_senza_chiusura_nessun_campione(self, db):
        path, conn = db
        fx.add_prediction(conn, "m1", quota=2.10)
        res = cba.analyze(path)
        assert res["coverage"]["clv_samples"] == 0
        assert res["clv"]["overall"]["raw"] is None
        assert any("nessun campione CLV" in w for w in res["warnings"])

    def test_filtro_era_esclude_le_osservazioni_vecchie(self, db):
        """Date RELATIVE: una osservazione a -400 giorni e' fuori dal filtro."""
        path, conn = db
        fx.add_clv(conn, "vecchio", "1", 2.10, 2.00, pinnacle_quota=2.00,
                   updated_at=fx.rel_iso(-400 * 24 * 60))
        fx.add_clv(conn, "nuovo", "1", 2.10, 2.00, pinnacle_quota=2.00,
                   updated_at=fx.rel_iso(-60))
        all_rows = cba.analyze(path)
        filtered = cba.analyze(path, since=fx.rel_iso(-24 * 60))
        assert all_rows["coverage"]["clv_samples"] == 2
        assert filtered["coverage"]["clv_samples"] == 1

    def test_riepilogo_statistiche(self):
        st = cba.summarize([0.1, -0.1, 0.2])
        assert st["n"] == 3
        assert st["positive_pct"] == pytest.approx(66.67, abs=0.01)
        assert st["median_pct"] == pytest.approx(10.0, abs=1e-6)
        assert cba.summarize([]) is None


# ---------------------------------------------------------------------------
# 3. Brier
# ---------------------------------------------------------------------------

class TestBrier:
    def test_valore_noto(self):
        """`mean((p-y)^2)`: 0.8/vinte + 0.3/perse -> (0.04 + 0.09)/2."""
        st = cba.brier_score([(0.8, 1.0), (0.3, 0.0)])
        assert st["brier"] == pytest.approx((0.04 + 0.09) / 2, abs=1e-12)
        assert st["n"] == 2

    def test_serie_vuota_none(self):
        assert cba.brier_score([]) is None

    def test_skill_positivo_se_meglio_del_banale(self):
        st = cba.brier_score([(0.9, 1.0)] * 10 + [(0.1, 0.0)] * 10)
        assert st["skill"] > 0.9

    def test_forecast_costante_non_ha_skill(self):
        """Se p e' sempre la frequenza base, lo skill e' ~0 (non un premio)."""
        st = cba.brier_score([(0.5, 1.0), (0.5, 0.0)])
        assert st["skill"] == pytest.approx(0.0, abs=1e-9)

    def test_skill_none_quando_la_base_e_degenere(self):
        st = cba.brier_score([(0.9, 1.0)])
        assert st["brier_ref"] == 0.0
        assert st["skill"] is None

    def test_modello_usa_prob_ed_esito_finale(self, db):
        path, conn = db
        fx.add_prediction(conn, "m1", prob=0.8, esito_finale="won")
        fx.add_prediction(conn, "m2", prob=0.3, esito_finale="lost")
        res = cba.analyze(path)
        ov = res["brier_model"]["overall"]
        assert ov["n"] == 2
        assert ov["brier"] == pytest.approx((0.04 + 0.09) / 2, abs=1e-12)

    def test_push_escluso_dal_brier(self, db):
        """Un push non ha un esito binario: contarlo falserebbe la calibrazione."""
        path, conn = db
        fx.add_prediction(conn, "m1", prob=0.8, esito_finale="won")
        fx.add_prediction(conn, "m2", prob=0.8, esito_finale="push")
        res = cba.analyze(path)
        assert res["brier_model"]["overall"]["n"] == 1

    def test_righe_aperte_escluse(self, db):
        path, conn = db
        fx.add_prediction(conn, "m1", prob=0.8, esito_finale=None)
        res = cba.analyze(path)
        assert res["brier_model"]["overall"] is None
        assert any("nessuna previsione CHIUSA" in w for w in res["warnings"])

    def test_prob_fuori_range_esclusa(self, db):
        path, conn = db
        fx.add_prediction(conn, "m1", prob=1.6, esito_finale="won")
        fx.add_prediction(conn, "m2", prob=0.5, esito_finale="won")
        res = cba.analyze(path)
        assert res["brier_model"]["overall"]["n"] == 1

    def test_split_per_sport_e_mercato(self, db):
        path, conn = db
        fx.add_prediction(conn, "m1", mercato="1X2", prob=0.8, esito_finale="won")
        fx.add_prediction(conn, "sx-tennis-1", mercato="TENNIS", prob=0.4,
                          esito_finale="lost")
        res = cba.analyze(path)
        assert set(res["brier_model"]["by_sport"]) == {"calcio", "tennis"}
        assert "1X2" in res["brier_model"]["by_market"]
        assert "TENNIS" in res["brier_model"]["by_market"]


class TestBrierSharp:
    def _market(self, conn, mid="m1", result="1"):
        fx.add_result(conn, mid, result, home="Casa", away="Ospite")
        for esito, price in (("1", 2.00), ("X", 3.40), ("2", 3.60)):
            fx.add_snapshot(conn, mid, esito, price)

    def test_brier_per_ogni_metodo_di_devig(self, db):
        path, conn = db
        self._market(conn)
        res = cba.analyze(path)
        for method in cba.DEVIG_METHODS:
            st = res["brier_sharp"]["by_method"][method]["overall"]
            assert st["n"] == 3, method          # tre esiti per partita
        assert res["brier_sharp"]["shin_z_n"] == 1
        assert 0.0 <= res["brier_sharp"]["shin_z_mean"] <= 1.0

    def test_le_probabilita_arrivano_dal_devig_del_progetto(self, db):
        """Il Brier `shin` deve coincidere col de-vig di `market_calib`."""
        path, conn = db
        self._market(conn)
        res = cba.analyze(path)
        fair, _z = mc.devig_with_z([2.00, 3.40, 3.60], method="shin")
        expected = sum((fair[i] - (1.0 if ("1", "X", "2")[i] == "1" else 0.0)) ** 2
                       for i in range(3)) / 3
        assert res["brier_sharp"]["by_method"]["shin"]["overall"]["brier"] \
            == pytest.approx(expected, abs=1e-6)

    def test_mercato_incompleto_e_fail_closed(self, db):
        """Con 2 esiti su 3 il de-vig attribuirebbe il margine in silenzio."""
        path, conn = db
        fx.add_result(conn, "m1", "1")
        fx.add_snapshot(conn, "m1", "1", 2.00)
        fx.add_snapshot(conn, "m1", "X", 3.40)
        res = cba.analyze(path)
        assert res["coverage"]["matches_with_sharp_1x2"] == 0
        assert res["brier_sharp"]["by_method"]["shin"]["overall"] is None
        assert any("nessun mercato 1X2 sharp completo" in w
                   for w in res["warnings"])

    def test_senza_risultato_non_si_calcola(self, db):
        path, conn = db
        for esito, price in (("1", 2.00), ("X", 3.40), ("2", 3.60)):
            fx.add_snapshot(conn, "m1", esito, price)
        res = cba.analyze(path)
        assert res["coverage"]["matches_with_sharp_1x2"] == 1
        assert res["coverage"]["matches_de_vigable"] == 0

    def test_vince_l_ultimo_prezzo_della_serie(self, db):
        """La closing line e' l'ULTIMO snapshot, non il primo."""
        path, conn = db
        fx.add_result(conn, "m1", "1")
        for esito, prices in (("1", (2.10, 2.00)), ("X", (3.30, 3.40)),
                              ("2", (3.50, 3.60))):
            for p in prices:
                fx.add_snapshot(conn, "m1", esito, p)
        res = cba.analyze(path)
        assert res["coverage"]["matches_with_sharp_1x2"] == 1


# ---------------------------------------------------------------------------
# 4. Classificazione sport/mercato
# ---------------------------------------------------------------------------

class TestClassificazione:
    @pytest.mark.parametrize("mercato,mid,league,atteso", [
        ("1X2", "sx-abc", None, "calcio"),
        ("OU", "sx-abc", None, "calcio"),
        ("AH", "sx-abc", None, "calcio"),
        ("TENNIS", "sx-abc", None, "tennis"),
        (None, "sx-tennis-0x1", None, "tennis"),
        (None, "sx-abc", "ATP China Open", "tennis"),
        (None, "sx-abc", "WTA Tokyo", "tennis"),
        ("ML", "sx-abc", None, "esports"),
        (None, "sx-esports-0x1", None, "esports"),
        ("", None, None, "altro"),
        ("BOCCIATA", "sx-abc", None, "altro"),
    ])
    def test_sport(self, mercato, mid, league, atteso):
        assert cba.classify_sport(mercato, mid, league) == atteso

    def test_mercato_normalizzato(self):
        assert cba.classify_market("1x2") == "1X2"
        assert cba.classify_market(None) == "?"


# ---------------------------------------------------------------------------
# 5. Robustezza e CLI
# ---------------------------------------------------------------------------

class TestRobustezza:
    def test_legacy_senza_colonna_league_non_rompe(self, tmp_path):
        path = tmp_path / "legacy.db"
        conn = sqlite3.connect(str(path))
        conn.execute("CREATE TABLE predictions (id INTEGER PRIMARY KEY, "
                     "match_id TEXT, mercato TEXT, esito TEXT, quota REAL, "
                     "prob REAL, status TEXT, esito_finale TEXT, created_at TEXT)")
        conn.execute("INSERT INTO predictions (match_id, mercato, esito, quota,"
                     " prob, esito_finale, created_at) VALUES "
                     "('m1','1X2','1',2.0,0.6,'won',?)", (fx.rel_iso(-60),))
        conn.commit()
        conn.close()
        res = cba.analyze(path)
        assert res["brier_model"]["overall"]["n"] == 1

    def test_db_vuoto_nessun_crash_solo_warning(self, tmp_path):
        path = tmp_path / "vuoto.db"
        fx.close(fx.make_db(path))
        res = cba.analyze(path)
        assert "error" not in res
        assert res["coverage"]["predictions"] == 0
        assert len(res["warnings"]) >= 3

    def test_cli_json_e_exit_code(self, tmp_path, capsys):
        path = tmp_path / "vuoto.db"
        fx.close(fx.make_db(path))
        assert cba.main(["--db", str(path), "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["read_only"] is True

    def test_cli_db_assente_esce_uno(self, tmp_path, capsys):
        assert cba.main(["--db", str(tmp_path / "x.db")]) == 1
        assert "ERRORE" in capsys.readouterr().out

    def test_report_legge_bene_il_vuoto(self, tmp_path):
        path = tmp_path / "vuoto.db"
        fx.close(fx.make_db(path))
        txt = cba.format_report(cba.analyze(path))
        assert "sola lettura" in txt and "Brier" in txt


# ---------------------------------------------------------------------------
# 6. Tripwire
# ---------------------------------------------------------------------------

class TestTripwire:
    def test_source_non_scrive_mai(self):
        src = open(cba.__file__, encoding="utf-8").read()
        for bad in ("INSERT INTO", "UPDATE ", "DELETE FROM", "mode=rw",
                    "mode=rwc", 'open(', "save_prediction", "save_bet"):
            assert bad not in src, bad

    def test_source_non_tocca_la_rete(self):
        src = open(cba.__file__, encoding="utf-8").read()
        for bad in ("requests", "aiohttp", "httpx", "odds_api", "sx_signals",
                    "execution_engine", "_live_fill"):
            assert bad not in src, bad

    def test_usa_il_de_vig_del_progetto(self):
        src = open(cba.__file__, encoding="utf-8").read()
        assert "from market_calib import" in src
        assert "devig_with_z" in src        # il de-vig arriva da market_calib
        assert "(sqrt(" not in src          # nessuna formula di Shin ricopiata
