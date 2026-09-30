"""Test OFFLINE dell'oracolo a linea OU/AH (30/09/2026).

Cosa si verifica, senza rete ne' crediti:
1. **Budget e cache dedicate** (`odds_api`): `ORACLE_BUDGET_DAY` come tetto
   effettivo (dopo il tetto la chiamata NON parte, zero HTTP), cache
   `toao_*` separata da `toa_*`, TTL 24h.
2. **Follow-the-money** (`line_oracle`): la selezione leghe segue i pick
   OU/AH aperti della corsia d'ordine (`multi_market.live_picks`), le leghe
   con cache fresca NON vengono riscaricate, il resoconto dichiara i motivi.
3. **Oracolo a linea** (`pinnacle_oracle`): de-vig a 2 esiti da
   totals/spreads Pinnacle, esito del ledger ('Over 2.5' / 'Home -0.75')
   -> p_true del lato giocato, fail-closed su lato mancante/linea diversa/
   cache stantia, gate top-down che ora passa con l'oracolo a linea e skip
   dichiarato `linea` quando la verita' non e' stata ancora pagata.
4. **Tripwire**: nessun ordine nel modulo, nessuna formula copiata,
   prefisso cache gemello, jobs bot registrati, IaC aggiornata.
"""

import json
import time
from pathlib import Path

import pytest

import odds_api
import pinnacle_oracle as po


def _write_oracle_cache(tmp_path: Path, sport: str, matches: list,
                        ts: float = None, prefix: str = None):
    payload = {"ts": ts if ts is not None else time.time(),
               "payload": matches, "remaining": 300,
               "remaining_ts": ts if ts is not None else time.time()}
    pfx = prefix if prefix is not None else odds_api.ORACLE_CACHE_PREFIX
    (tmp_path / f"{pfx}{sport}.json").write_text(json.dumps(payload))


def _match_with_lines(home="Inter", away="Cagliari", tot_line=2.5,
                      over=1.90, under=1.95, sp_line=-0.75,
                      home_odds=1.85, away_odds=2.00):
    """Match payload the-odds-api con h2h + totals + spreads Pinnacle."""
    markets = [{"key": "h2h", "outcomes": [
        {"name": home, "price": 1.70}, {"name": "Draw", "price": 3.60},
        {"name": away, "price": 5.00}]},
        {"key": "totals", "point": tot_line, "outcomes": [
            {"name": "Over", "price": over, "point": tot_line},
            {"name": "Under", "price": under, "point": tot_line}]},
        {"key": "spreads", "outcomes": [
            {"name": home, "price": home_odds, "point": sp_line},
            {"name": away, "price": away_odds, "point": -sp_line}]}]
    return {"id": "m1", "sport_key": "soccer_italy_serie_a",
            "home_team": home, "away_team": away,
            "commence_time": "2026-10-01T19:45:00Z",
            "bookmakers": [{"key": "pinnacle", "title": "Pinnacle",
                            "markets": markets}]}


# ---------------------------------------------------------------------------
# 1. Budget e cache dedicate
# ---------------------------------------------------------------------------

class TestBudgetECacheOracolo:
    def test_costanti_dedicate(self):
        assert odds_api.ORACLE_MARKETS_LIST == "h2h,totals,spreads"
        assert odds_api.ORACLE_EXTRA_CREDITS == 2
        assert odds_api.ORACLE_CACHE_TTL_S == 86400
        assert odds_api.ORACLE_CACHE_PREFIX == "toao_"
        assert odds_api.ORACLE_BUDGET_DAY >= 1

    def test_tetto_budget_blocca_la_chiamata(self, monkeypatch, tmp_path):
        """Dopo ORACLE_BUDGET_DAY chiamate la fetch NON parte: zero HTTP."""
        monkeypatch.setattr(odds_api, "CACHE_DIR", tmp_path)
        monkeypatch.setattr(odds_api, "ORACLE_BUDGET_DAY", 2)
        odds_api._oracle_req_day.update({"day": "2099-01-01", "n": 2})
        calls = []
        monkeypatch.setattr(odds_api.requests, "get",
                            lambda *a, **k: calls.append(1))
        payload, remaining = odds_api.fetch_line_odds("soccer_x", "f", "t")
        assert payload == [] and calls == []

    def test_budget_conta_solo_le_chiamate_fatte(self, monkeypatch, tmp_path):
        monkeypatch.setattr(odds_api, "CACHE_DIR", tmp_path)
        monkeypatch.setattr(odds_api, "ORACLE_BUDGET_DAY", 5)
        monkeypatch.setattr(odds_api, "should_query_sport", lambda s: True)
        monkeypatch.setattr(odds_api, "credits_hard_stopped", lambda: False)
        monkeypatch.setenv("ODDS_API_KEY", "test")

        class _R:
            status_code = 200
            headers = {"x-requests-remaining": "300"}
            text = "[]"
            def raise_for_status(self): pass
            def json(self): return []
        monkeypatch.setattr(odds_api.requests, "get", lambda *a, **k: _R())
        odds_api._oracle_req_day.update({"day": "2099-01-01", "n": 0})
        odds_api.fetch_line_odds("soccer_x", "f", "t")
        assert odds_api._oracle_req_day["n"] == 1

    def test_cache_oracolo_separata_dalla_rotazione(self, monkeypatch, tmp_path):
        """La fetch dell'oracolo scrive `toao_*` e NON tocca `toa_*`."""
        monkeypatch.setattr(odds_api, "CACHE_DIR", tmp_path)
        monkeypatch.setattr(odds_api, "should_query_sport", lambda s: True)
        monkeypatch.setattr(odds_api, "credits_hard_stopped", lambda: False)
        monkeypatch.setenv("ODDS_API_KEY", "test")

        class _R:
            status_code = 200
            headers = {"x-requests-remaining": "299"}
            text = "[]"
            def raise_for_status(self): pass
            def json(self): return [{"id": "m1"}]
        monkeypatch.setattr(odds_api.requests, "get", lambda *a, **k: _R())
        odds_api._oracle_req_day.update({"day": "2099-01-01", "n": 0})
        payload, _ = odds_api.fetch_line_odds("soccer_x", "f", "t")
        assert payload == [{"id": "m1"}]
        assert (tmp_path / f"{odds_api.ORACLE_CACHE_PREFIX}soccer_x.json").exists()
        assert not (tmp_path / "toa_soccer_x.json").exists()

    def test_cache_fresca_zero_http(self, monkeypatch, tmp_path):
        _write_oracle_cache(tmp_path, "soccer_x", [{"id": "m1"}])
        monkeypatch.setattr(odds_api, "CACHE_DIR", tmp_path)
        odds_api._oracle_req_day.update({"day": "2099-01-01", "n": 0})
        calls = []
        monkeypatch.setattr(odds_api.requests, "get",
                            lambda *a, **k: calls.append(1))
        payload, _ = odds_api.fetch_line_odds("soccer_x", "f", "t")
        assert payload == [{"id": "m1"}] and calls == []
        assert odds_api._oracle_req_day["n"] == 0   # cache = zero spesa


# ---------------------------------------------------------------------------
# 2. Follow-the-money
# ---------------------------------------------------------------------------

class TestFollowTheMoney:
    def test_leghe_con_cache_fresca_non_riscaricate(self, monkeypatch, tmp_path):
        _write_oracle_cache(tmp_path, "soccer_a", [{"id": "old"}])
        monkeypatch.setattr("config.DATA_DIR", tmp_path)
        monkeypatch.setattr("line_oracle.line_picks", lambda: [
            {"match_id": "m1", "esito_key": "Over 2.5", "league": "Serie A",
             "sport_key": "soccer_a", "kickoff": "2026-10-01T19:45:00+00:00",
             "kickoff_ts": time.time() + 3600}])
        import line_oracle
        pending = line_oracle.leagues_needing_fetch(time.time())
        assert all(x["sport_key"] != "soccer_a" for x in pending)

    def test_lega_senza_cache_e_nel_piano(self, monkeypatch, tmp_path):
        monkeypatch.setattr("config.DATA_DIR", tmp_path)
        monkeypatch.setattr("line_oracle.line_picks", lambda: [
            {"match_id": "m1", "esito_key": "Over 2.5", "league": "Serie A",
             "sport_key": "soccer_a", "kickoff": "2026-10-01T19:45:00+00:00",
             "kickoff_ts": time.time() + 3600}])
        import line_oracle
        pending = line_oracle.leagues_needing_fetch(time.time())
        assert [x["sport_key"] for x in pending] == ["soccer_a"]

    def test_solo_pick_in_finestra_contano(self, monkeypatch, tmp_path):
        monkeypatch.setattr("config.DATA_DIR", tmp_path)
        far = time.time() + 6 * 86400
        monkeypatch.setattr("line_oracle.line_picks", lambda: [
            {"match_id": "m1", "esito_key": "Over 2.5", "league": "Serie A",
             "sport_key": "soccer_a", "kickoff": "x", "kickoff_ts": far}])
        import line_oracle
        assert line_oracle.leagues_needing_fetch(time.time()) == []


# ---------------------------------------------------------------------------
# 3. Oracolo a linea
# ---------------------------------------------------------------------------

class TestEstrattoriLinea:
    def test_totals_alla_linea(self):
        bm = _match_with_lines()["bookmakers"][0]
        q = po.totals_odds_of(bm, 2.5)
        assert q == {"Over": 1.90, "Under": 1.95}

    def test_totals_linea_diversa_none(self):
        bm = _match_with_lines(tot_line=2.5)["bookmakers"][0]
        assert po.totals_odds_of(bm, 3.5) is None

    def test_totals_lato_singolo_none(self):
        bm = _match_with_lines()["bookmakers"][0]
        bm["markets"][1]["outcomes"] = [{"name": "Over", "price": 1.9,
                                         "point": 2.5}]
        assert po.totals_odds_of(bm, 2.5) is None

    def test_spreads_per_nome_e_linea(self):
        bm = _match_with_lines()["bookmakers"][0]
        q = po.spreads_odds_of(bm, "Inter", "Cagliari", -0.75)
        assert q == {"Home": 1.85, "Away": 2.00}

    def test_spreads_senza_nomi_decide_la_linea_speculare(self):
        bm = _match_with_lines()["bookmakers"][0]
        bm["markets"][2]["outcomes"] = [
            {"price": 1.85, "point": -0.75},
            {"price": 2.00, "point": 0.75}]
        q = po.spreads_odds_of(bm, "Inter", "Cagliari", -0.75)
        assert q == {"Home": 1.85, "Away": 2.00}

    def test_spreads_a_linea_zero_senza_nomi_fail_closed(self):
        """A linea 0 le linee coincidono: senza nomi NON si decide."""
        bm = _match_with_lines(sp_line=0.0, home_odds=1.9, away_odds=1.9)
        bm = bm["bookmakers"][0]
        bm["markets"][2]["outcomes"] = [
            {"price": 1.9, "point": 0.0}, {"price": 1.9, "point": 0.0}]
        assert po.spreads_odds_of(bm, "Inter", "Cagliari", 0.0) is None

    def test_line_probabilities_due_esiti(self):
        probs = po.line_probabilities({"Over": 2.0, "Under": 2.0})
        assert probs["Over"] == pytest.approx(0.5, abs=1e-9)
        assert probs["Under"] == pytest.approx(0.5, abs=1e-9)
        assert probs["overround"] == pytest.approx(1.0, abs=1e-9)


class TestLineTrueProbs:
    def test_ou_dalla_cache(self, tmp_path):
        _write_oracle_cache(tmp_path, "soccer_italy_serie_a",
                            [_match_with_lines()])
        probs = po.line_true_probs("Inter", "Cagliari", market_type="OU",
                                   line=2.5, cache_dir=tmp_path)
        assert probs is not None and 0 < probs["Over"] < 1
        assert 0 < probs["Under"] < 1

    def test_ah_dalla_cache(self, tmp_path):
        _write_oracle_cache(tmp_path, "soccer_italy_serie_a",
                            [_match_with_lines()])
        probs = po.line_true_probs("Inter", "Cagliari", market_type="AH",
                                   line=-0.75, cache_dir=tmp_path)
        assert probs is not None and 0 < probs["Home"] < 1

    def test_cache_stantia_none(self, tmp_path):
        old = time.time() - (po.CACHE_MAX_AGE_H + 5) * 3600
        _write_oracle_cache(tmp_path, "soccer_italy_serie_a",
                            [_match_with_lines()], ts=old)
        assert po.line_true_probs("Inter", "Cagliari", market_type="OU",
                                  line=2.5, cache_dir=tmp_path) is None

    def test_partita_assente_none(self, tmp_path):
        _write_oracle_cache(tmp_path, "soccer_italy_serie_a",
                            [_match_with_lines()])
        assert po.line_true_probs("Milan", "Juventus", market_type="OU",
                                  line=2.5, cache_dir=tmp_path) is None

    def test_mercato_sconosciuto_none(self, tmp_path):
        _write_oracle_cache(tmp_path, "soccer_italy_serie_a",
                            [_match_with_lines()])
        assert po.line_true_probs("Inter", "Cagliari", market_type="1X2",
                                  line=2.5, cache_dir=tmp_path) is None

    def test_linea_nan_inf_none(self):
        assert po.line_true_probs("A", "B", market_type="OU",
                                  line=float("nan")) is None


class TestGateTopDownConLinea:
    """Il pick OU/AH con oracolo a linea NON muore piu' con no_oracle."""

    def _pick(self, esito_key="Over 2.5", mercato="OU", quota=2.10):
        return {"match_id": "m1", "home": "Inter", "away": "Cagliari",
                "mercato": mercato, "esito_key": esito_key, "quota": quota,
                "league": "Serie A"}

    def test_ponte_pick_to_probs(self, tmp_path):
        _write_oracle_cache(tmp_path, "soccer_italy_serie_a",
                            [_match_with_lines()])
        probs = po.line_oracle_probs(self._pick(), cache_dir=tmp_path)
        assert probs is not None
        assert probs["line_key"] == "over"
        assert 0 < probs["Over"] < 1

    def test_pick_ah_linea_casa(self, tmp_path):
        _write_oracle_cache(tmp_path, "soccer_italy_serie_a",
                            [_match_with_lines()])
        probs = po.line_oracle_probs(self._pick(esito_key="Home -0.75",
                                                mercato="AH", quota=1.85),
                                     cache_dir=tmp_path)
        assert probs is not None and probs["line_key"] == "home"

    def test_gate_ev_con_oracolo_a_linea(self, monkeypatch, tmp_path):
        import auto_bet
        _write_oracle_cache(tmp_path, "soccer_italy_serie_a",
                            [_match_with_lines(over=1.55, under=2.50)])
        # Il gate legge la linea dalla cache TMP: p_over ~0.65, quota 2.10
        # -> EV ~ +36% >> soglia -> ok + trigger.
        monkeypatch.setattr(auto_bet, "_top_down_load", lambda h, a: None)
        monkeypatch.setattr(auto_bet, "_TOP_DOWN_CACHE_DIR", str(tmp_path))
        v = auto_bet._top_down_eval(self._pick(quota=2.10))
        assert v.get("ok") is True
        assert v.get("trigger") is True
        assert 0 < v.get("p_true", 0) < 1

    def test_skip_dichiarato_linea_senza_oracolo(self, monkeypatch, tmp_path):
        import auto_bet
        monkeypatch.setattr(auto_bet, "_top_down_load", lambda h, a: None)
        monkeypatch.setattr(auto_bet, "_TOP_DOWN_CACHE_DIR", str(tmp_path))
        monkeypatch.setattr(po, "line_oracle_probs", lambda *a, **k: None)
        monkeypatch.setattr(po, "h2h_cache_is_stale", lambda *a, **k: True)
        v = auto_bet._top_down_eval(self._pick())
        assert v.get("ok") is False and v.get("reason") == "linea"

    def test_no_oracle_secco_resta(self, monkeypatch, tmp_path):
        import auto_bet
        monkeypatch.setattr(auto_bet, "_top_down_load", lambda h, a: None)
        monkeypatch.setattr(auto_bet, "_TOP_DOWN_CACHE_DIR", str(tmp_path))
        monkeypatch.setattr(po, "line_oracle_probs", lambda *a, **k: None)
        monkeypatch.setattr(po, "h2h_cache_is_stale", lambda *a, **k: False)
        v = auto_bet._top_down_eval(self._pick())
        assert v.get("ok") is False and v.get("reason") == "no_oracle"


# ---------------------------------------------------------------------------
# 4. Tripwire
# ---------------------------------------------------------------------------

class TestTripwire:
    def test_prefisso_cache_gemello(self):
        assert odds_api.ORACLE_CACHE_PREFIX == "toao_"
        src = Path(po.__file__).read_text()
        assert '"toao_"' in src

    def test_line_oracle_senza_ordini_nessun_requests_diretto(self):
        src = Path("line_oracle.py").read_text()
        assert "requests." not in src
        for bad in ("place_limit_order", "_live_fill", "save_bet",
                    "save_prediction", "INSERT INTO"):
            assert bad not in src, f"line_oracle non deve contenere {bad}"

    def test_nessuna_formula_devig_copiata(self):
        src = Path("line_oracle.py").read_text()
        assert "market_implied" not in src
        assert "true_probabilities" not in src

    def test_import_leggero(self):
        import subprocess, sys
        code = ("import sys; import line_oracle; "
                "sys.exit(0 if 'tracker' not in sys.modules and "
                "'auto_bet' not in sys.modules and "
                "'bot' not in sys.modules else 1)")
        r = subprocess.run([sys.executable, "-c", code], capture_output=True)
        assert r.returncode == 0, r.stderr.decode()

    def test_iac_dichiara_le_env_oracolo(self):
        src = Path(".railway/railway.ts").read_text()
        for env in ("ORACLE_ENABLED", "ORACLE_BUDGET_DAY",
                    "ORACLE_PICK_WINDOW_H", "ORACLE_LEAGUES_PER_PASS"):
            assert env in src, f"{env} non dichiarata in preserve() IaC"

    def test_line_oracle_job_registrato(self):
        src = Path("bot.py").read_text()
        assert "def line_oracle_job" in src
        assert "run_repeating(line_oracle_job" in src

    def test_budget_credits_per_day(self):
        import line_oracle
        assert line_oracle.budget_credits_per_day() == \
            float(odds_api.ORACLE_BUDGET_DAY * 3)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
