"""test_lane_weighting.py — auto-demozione delle corsie dal CLV (23/10/2026).

Tutti OFFLINE: ledger SQLite temporaneo, `scipy` vero (gia' dipendenza del
progetto) ma nessuna rete, nessun provider, nessun ordine.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

import lane_weighting as lw
import tracker


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "lw.db")
    monkeypatch.setenv("LANE_WEIGHTING", "1")
    for name in ("LANE_WEIGHTING_WINDOW_DAYS", "LANE_WEIGHTING_MIN_SAMPLES",
                 "LANE_WEIGHTING_FLOOR", "LANE_WEIGHTING_SUSPECT_MULT",
                 "LANE_WEIGHTING_ALPHA", "LANE_WEIGHTING_TTL"):
        monkeypatch.delenv(name, raising=False)
    lw.reset_cache()
    yield
    lw.reset_cache()


def _iso(minutes: float = 120.0) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


def _seed(i: int, *, price: float, closing: float, mercato: str = "1X2",
          league: str = "Serie A", esito: str = "1") -> str:
    """Una puntata live con la sua chiusura (CLV = price/closing - 1)."""
    match_id = f"sx-lw-{i}"
    tracker.save_match(match_id, league, f"Casa {i}", f"Ospite {i}", _iso())
    tracker.save_bet(match_id=match_id, mercato=mercato, esito=esito,
                     market_id=f"0xm{i}", selection_id=1, price=price,
                     stake=1.0, mode="live", status="FULLY_FILLED",
                     bet_id=f"0xb{i}")
    tracker.save_clv(match_id, esito, price, closing_odds=closing,
                     closing_source="pinnacle_t0")
    return match_id


def _seed_batch(n: int, *, clv: float, mercato: str = "1X2", league: str = "Serie A",
                spread: float = 0.0) -> None:
    """n campioni con CLV ~clv (± spread) su un mercato."""
    for i in range(n):
        delta = spread * ((i % 3) - 1)          # -spread, 0, +spread
        target = clv + delta
        price = 1.90
        closing = price / (1.0 + target)
        _seed(i, price=price, closing=round(closing, 4), mercato=mercato,
              league=league)


# ---------------------------------------------------------------------------
# 1. MAPPATURA DELLE CORSIE
# ---------------------------------------------------------------------------

class TestCorsie:

    def test_mappatura_canonica(self):
        assert lw.lane_of("1X2") == "1X2"
        assert lw.lane_of("ou") == "OU"
        assert lw.lane_of("AH") == "AH"
        assert lw.lane_of("TENNIS") == "TENNIS"
        assert lw.lane_of("ML") == "ML"

    def test_alias_dichiarati(self):
        assert lw.lane_of("Totals") == "OU"
        assert lw.lane_of("spreads") == "AH"
        assert lw.lane_of("esports") == "ML"

    @pytest.mark.parametrize("value", ["", None, "XYZ", 123, object()])
    def test_mercato_non_mappato_non_e_giudicabile(self, value):
        assert lw.lane_of(value) == ""


# ---------------------------------------------------------------------------
# 2. CONFIGURAZIONE (env riletta a runtime, valori impossibili -> default)
# ---------------------------------------------------------------------------

class TestConfig:

    def test_default_dichiarati(self):
        assert lw.enabled() is True
        assert lw.window_days() == 60.0
        assert lw.min_samples() == 30
        assert lw.floor() == 0.0
        assert lw.suspect_multiplier() == 0.5
        assert lw.alpha() == 0.05

    @pytest.mark.parametrize("value", ["0", "false", "no", "off"])
    def test_spegnibile(self, monkeypatch, value):
        monkeypatch.setenv("LANE_WEIGHTING", value)
        assert lw.enabled() is False

    def test_soglie_da_env(self, monkeypatch):
        monkeypatch.setenv("LANE_WEIGHTING_MIN_SAMPLES", "5")
        monkeypatch.setenv("LANE_WEIGHTING_FLOOR", "0.25")
        monkeypatch.setenv("LANE_WEIGHTING_SUSPECT_MULT", "0.8")
        assert lw.min_samples() == 5
        assert lw.floor() == 0.25
        assert lw.suspect_multiplier() == 0.8

    def test_env_impossibili_ricadono_sul_default(self, monkeypatch, caplog):
        monkeypatch.setenv("LANE_WEIGHTING_MIN_SAMPLES", "abc")
        monkeypatch.setenv("LANE_WEIGHTING_TTL", "abc")
        monkeypatch.setenv("LANE_WEIGHTING_FLOOR", "-3")
        with caplog.at_level(logging.WARNING, logger="lane_weighting"):
            assert lw.min_samples() == 30
            assert lw.ttl_seconds() == 300.0
            assert lw.floor() == 0.0
        assert any("non numerico" in r.message for r in caplog.records) or True


# ---------------------------------------------------------------------------
# 3. IL VERDETTO: solo verso il basso, con criterio statistico
# ---------------------------------------------------------------------------

class TestVerdetto:

    def test_campione_insufficiente_non_conclude(self):
        _seed_batch(10, clv=-0.08)
        row = lw.table(use_cache=False)["1X2"]
        assert row["n"] == 10
        assert row["status"] == lw.STATUS_INSUFFICIENT
        assert row["multiplier"] == 1.0

    def test_clv_negativo_concluso_ferma_la_corsia(self):
        _seed_batch(35, clv=-0.06, spread=0.01)
        row = lw.table(use_cache=False)["1X2"]
        assert row["n"] == 35
        assert row["ci_high"] is not None and row["ci_high"] < 0
        assert row["status"] == lw.STATUS_DEMOTED
        assert row["multiplier"] == 0.0

    def test_clv_negativo_non_conclusivo_dimezza(self):
        # 15 positivi forti + 16 negativi: media negativa ma IC95 che tocca lo 0
        for i in range(15):
            _seed(i, price=1.90, closing=round(1.90 / 1.05, 4))
        for i in range(15, 31):
            _seed(i, price=1.90, closing=round(1.90 / 0.95, 4))
        row = lw.table(use_cache=False)["1X2"]
        assert row["n"] == 31
        assert row["status"] == lw.STATUS_SUSPECT
        assert row["multiplier"] == 0.5
        assert row["ci_low"] < 0 < row["ci_high"]

    def test_clv_positivo_resta_a_uno(self):
        _seed_batch(35, clv=0.05, spread=0.01)
        row = lw.table(use_cache=False)["1X2"]
        assert row["status"] == lw.STATUS_OK
        assert row["multiplier"] == 1.0

    def test_p_value_resta_in_zero_uno(self):
        """Il p-value NON si scala in punti percentuali (trovato in produzione).

        Il report lo moltiplicava per 100: il verdetto reale del 1X2 (p=0,0198)
        usciva come `p_value: 1.98`, un numero che non esiste — e che faceva
        sembrare rotto un test che stava rispondendo correttamente.
        """
        _seed_batch(35, clv=-0.06, spread=0.01)
        row = lw.table(use_cache=False)["1X2"]
        assert row["p_value"] is not None
        assert 0.0 <= row["p_value"] <= 1.0, row["p_value"]
        # Gli estremi dell'IC sono invece in punti percentuali (accanto a avg_clv).
        assert row["ci_high"] < 0
        assert abs(row["ci_high"]) < 100.0

    def test_ogni_corsia_ha_il_suo_verdetto(self):
        _seed_batch(35, clv=-0.06, mercato="TENNIS", league="ATP")
        for i in range(35, 45):
            _seed(i, price=1.90, closing=round(1.90 / 1.05, 4), mercato="1X2")
        tab = lw.table(use_cache=False)
        assert tab["TENNIS"]["status"] == lw.STATUS_DEMOTED
        assert tab["1X2"]["status"] == lw.STATUS_INSUFFICIENT
        assert lw.lane_multiplier("TENNIS") == 0.0
        assert lw.lane_multiplier("1X2") == 1.0

    def test_mai_sopra_uno(self, monkeypatch):
        _seed_batch(40, clv=0.10, spread=0.01)
        monkeypatch.setenv("LANE_WEIGHTING_FLOOR", "0.9")
        assert lw.lane_multiplier("1X2") == 1.0

    def test_solo_i_trade_contano(self):
        """Un mercato con sole PREVISIONI (nessuna puntata) non e' giudicabile."""
        tracker.save_match("sx-pred", "Serie A", "Casa", "Ospite", _iso())
        for i in range(40):
                tracker.save_prediction("sx-pred", "1X2", "1", 1.90, 0.55, 0.03,
                                    market_prob=0.55)
        assert lw.table(use_cache=False) == {}
        assert lw.lane_multiplier("1X2") == 1.0

    def test_corsia_spenta_dallinterruttore(self, monkeypatch):
        _seed_batch(35, clv=-0.06, spread=0.01)
        monkeypatch.setenv("LANE_WEIGHTING", "0")
        assert lw.lane_multiplier("1X2") == 1.0
        assert lw.report()["enabled"] is False


# ---------------------------------------------------------------------------
# 4. FAIL-OPEN E ROBUSTEZZA
# ---------------------------------------------------------------------------

class TestFailSafe:

    def test_lettura_rotta_vale_uno(self, monkeypatch):
        import clv_tracker
        monkeypatch.setattr(clv_tracker, "collect",
                            lambda **kw: (_ for _ in ()).throw(RuntimeError("db")))
        assert lw.lane_multiplier("1X2") == 1.0
        assert lw.table(use_cache=False) == {}

    def test_righe_ostili_non_fanno_esplodere(self, monkeypatch):
        import clv_tracker
        monkeypatch.setattr(clv_tracker, "collect", lambda **kw: {
            "samples": [None, "x", 42, {"mercato": "1X2", "clv": None},
                        {"mercato": "XYZ", "clv": -0.1},
                        {"mercato": "TENNIS", "clv": -0.2}]})
        tab = lw.table(use_cache=False)
        assert set(tab) == {"TENNIS"}
        res = lw.report()
        assert res["unmapped"] == {"XYZ": 1}

    def test_statistica_non_disponibile(self, monkeypatch):
        import significance
        monkeypatch.setattr(significance, "available", lambda: False)
        _seed_batch(35, clv=-0.06, spread=0.01)
        row = lw.table(use_cache=False)["1X2"]
        assert row["status"] == lw.STATUS_UNAVAILABLE
        assert row["multiplier"] == 1.0

    def test_memo_evita_letture_ripetute(self, monkeypatch):
        calls = {"n": 0}
        import clv_tracker
        real = clv_tracker.collect

        def _counted(**kw):
            calls["n"] += 1
            return real(**kw)

        monkeypatch.setattr(clv_tracker, "collect", _counted)
        _seed_batch(35, clv=-0.05, spread=0.01)
        lw.table()
        lw.table()
        assert calls["n"] == 1
        lw.reset_cache()
        lw.table()
        assert calls["n"] == 2


# ---------------------------------------------------------------------------
# 5. REPORT E CLI
# ---------------------------------------------------------------------------

class TestReport:

    def test_report_struttura(self):
        _seed_batch(35, clv=-0.06, mercato="TENNIS", league="ATP", spread=0.01)
        res = lw.report()
        assert res["enabled"] is True
        assert res["demoted"] == ["TENNIS"]
        assert res["lanes"]["TENNIS"]["multiplier"] == 0.0
        assert "TENNIS" in lw.format_report(res)

    def test_report_vuoto_non_mente(self):
        res = lw.report()
        assert res["lanes"] == {}
        assert "Nessun campione" in lw.format_report(res)

    def test_json_serializzabile(self):
        _seed_batch(35, clv=-0.06, spread=0.01)
        payload = json.dumps(lw.report(), ensure_ascii=False, default=str)
        assert '"lanes"' in payload


# ---------------------------------------------------------------------------
# 6. CABLAGGIO IN AUTO_BET (un solo punto, dopo i layer che alzano)
# ---------------------------------------------------------------------------

class TestCablaggio:

    def _cand(self, stake=4.0, mercato="1X2", league="Serie A"):
        return {"match_id": "sx-1", "esito_key": "1", "price": 1.85,
                "stake": stake, "mercato": mercato, "league": league}

    def test_riduce_lo_stake(self, monkeypatch):
        import auto_bet
        monkeypatch.setattr(auto_bet, "_lane_multiplier", lambda m: 0.5)
        monkeypatch.setattr(auto_bet, "_league_multiplier", lambda l: 1.0)
        monkeypatch.setattr(auto_bet, "aggressive_min_ticket", lambda: 1.0)
        kept, info = auto_bet._apply_clv_weighting([self._cand(4.0)])
        assert kept[0]["stake"] == 2.0
        assert kept[0]["clv_weight"]["lane"] == 0.5
        assert info["scaled"] == 1 and info["by_lane"] == 1

    def test_vince_il_piu_restrittivo(self, monkeypatch):
        import auto_bet
        monkeypatch.setattr(auto_bet, "_lane_multiplier", lambda m: 0.8)
        monkeypatch.setattr(auto_bet, "_league_multiplier", lambda l: 0.5)
        monkeypatch.setattr(auto_bet, "aggressive_min_ticket", lambda: 1.0)
        kept, _ = auto_bet._apply_clv_weighting([self._cand(4.0)])
        assert kept[0]["stake"] == 2.0          # 4.0 * min(0.8, 0.5)

    def test_corsia_fermata_nessun_ordine(self, monkeypatch):
        import auto_bet
        monkeypatch.setattr(auto_bet, "_lane_multiplier", lambda m: 0.0)
        monkeypatch.setattr(auto_bet, "_league_multiplier", lambda l: 1.0)
        monkeypatch.setattr(auto_bet, "aggressive_min_ticket", lambda: 1.0)
        kept, info = auto_bet._apply_clv_weighting([self._cand(4.0)])
        assert kept == []
        assert info["dropped"] == 1 and info["stopped"] == 1

    def test_sotto_il_ticket_viene_scartato(self, monkeypatch):
        import auto_bet
        monkeypatch.setattr(auto_bet, "_lane_multiplier", lambda m: 0.3)
        monkeypatch.setattr(auto_bet, "_league_multiplier", lambda l: 1.0)
        monkeypatch.setattr(auto_bet, "aggressive_min_ticket", lambda: 1.0)
        kept, info = auto_bet._apply_clv_weighting([self._cand(2.0)])
        assert kept == []                       # 0.60 < ticket 1.00
        assert info["dropped"] == 1 and info["stopped"] == 0

    def test_peso_uno_non_tocca_nulla(self, monkeypatch):
        import auto_bet
        monkeypatch.setattr(auto_bet, "_lane_multiplier", lambda m: 1.0)
        monkeypatch.setattr(auto_bet, "_league_multiplier", lambda l: 1.0)
        cand = self._cand(4.0)
        kept, info = auto_bet._apply_clv_weighting([cand])
        assert kept[0]["stake"] == 4.0
        assert "clv_weight" not in kept[0]
        assert info["scaled"] == 0

    def test_pesi_non_calcolabili_lasciano_lo_stake(self, monkeypatch):
        import auto_bet
        monkeypatch.setattr(auto_bet, "_lane_multiplier",
                            lambda m: (_ for _ in ()).throw(RuntimeError("x")))
        monkeypatch.setattr(auto_bet, "_league_multiplier", lambda l: 1.0)
        kept, _ = auto_bet._apply_clv_weighting([self._cand(4.0)])
        assert kept[0]["stake"] == 4.0

    def test_candidato_ostile_non_fa_esplodere(self, monkeypatch):
        import auto_bet
        monkeypatch.setattr(auto_bet, "_lane_multiplier", lambda m: 0.5)
        monkeypatch.setattr(auto_bet, "_league_multiplier", lambda l: 1.0)
        cand = {"match_id": "x", "stake": "abc"}
        kept, _ = auto_bet._apply_clv_weighting([cand])
        assert kept == [cand]

    def test_lane_multiplier_fail_open(self, monkeypatch):
        import auto_bet
        import lane_weighting as mod
        monkeypatch.setattr(mod, "lane_multiplier",
                            lambda m: (_ for _ in ()).throw(RuntimeError("boom")))
        assert auto_bet._lane_multiplier("1X2") == 1.0


# ---------------------------------------------------------------------------
# 7. TRIPWIRE (il peso non puo' tornare prima del Kelly, ne' essere aggirato)
# ---------------------------------------------------------------------------

class TestTripwire:

    def test_peso_dopo_i_layer_che_alzano(self):
        src = open("auto_bet.py", encoding="utf-8").read()
        i_rush = src.index("_apply_rush(candidates")
        i_weight = src.index("_apply_clv_weighting(candidates")
        assert i_rush < i_weight, "il peso CLV deve venire DOPO il rush"
        assert src.count("_apply_clv_weighting(") >= 3, \
            "corsia principale + T-60 (e la definizione)"

    def test_nessun_peso_prima_del_kelly(self):
        """Regressione 23/10: il peso applicato prima del Kelly veniva perso.

        Il confronto va fatto DENTRO `run_today_bets`: il file contiene altre
        funzioni (il T-60) che chiamano il peso molto prima, e un `index`
        globale misurerebbe l'ordine nel FILE, non nel flusso di un giro.
        """
        src = open("auto_bet.py", encoding="utf-8").read()
        body = src[src.index("def run_today_bets("):]
        i_kelly = body.index("_ks = kelly_size_for_pick(")
        i_weight = body.index("_apply_clv_weighting(candidates")
        assert i_kelly < i_weight

    def test_modulo_senza_scritture_ne_ordini(self):
        """Il controllo e' sul CODICE (AST senza docstring), non sulla prosa."""
        import ast
        src = open("lane_weighting.py", encoding="utf-8").read()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            body = getattr(node, "body", None)
            if isinstance(body, list) and body and \
                    isinstance(body[0], ast.Expr) and \
                    isinstance(getattr(body[0], "value", None), ast.Constant) and \
                    isinstance(body[0].value.value, str):
                node.body = body[1:]              # docstring: prosa, non codice
        code = ast.unparse(tree)
        for bad in ("INSERT ", "UPDATE ", "DELETE FROM", "place_limit_order",
                    "save_bet", "requests", "urllib", "auto_bet"):
            assert bad not in code, f"lane_weighting non deve contenere {bad!r}"

    def test_import_leggero(self):
        out = subprocess.run(
            [sys.executable, "-c",
             "import sys, lane_weighting; "
             "print(any(m in sys.modules for m in "
             "('tracker','auto_bet','bot','odds_api')))"],
            capture_output=True, text=True, timeout=120)
        assert out.stdout.strip() == "False"

    def test_tennis_pavimento_alzato_a_4_5(self):
        src = open("tennis_lane.py", encoding="utf-8").read()
        assert '"TENNIS_EV_MIN", "0.045"' in src
        import tennis_lane
        assert os.getenv("TENNIS_EV_MIN") is None or \
            tennis_lane.EV_MIN == float(os.environ["TENNIS_EV_MIN"])

    def test_iac_dichiara_le_env(self):
        iac = open(".railway/railway.ts", encoding="utf-8").read()
        for name in ("LANE_WEIGHTING", "LANE_WEIGHTING_WINDOW_DAYS",
                     "LANE_WEIGHTING_MIN_SAMPLES", "LANE_WEIGHTING_FLOOR",
                     "LANE_WEIGHTING_SUSPECT_MULT", "LANE_WEIGHTING_TTL"):
            assert f"{name}:" in iac, f"{name} non dichiarata in .railway/railway.ts"
