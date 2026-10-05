"""Test OFFLINE dell'oracolo a linea OU/AH (30/09/2026).

Cosa si verifica, senza rete ne' crediti:
1. **Budget e cache dedicate** (`odds_api`): `ORACLE_BUDGET_DAY` come tetto
   effettivo (dopo il tetto la chiamata NON parte, zero HTTP), cache
   `toao_*` separata da `toa_*`, TTL allineata alla finestra di fetch +
   TTL **DINAMICA** sul tempo al kickoff (05/10/2026).
2. **Follow-the-money** (`line_oracle`): la selezione leghe segue i pick
   OU/AH aperti della corsia d'ordine (`multi_market.live_picks`), le leghe
   con cache fresca NON vengono riscaricate, il resoconto dichiara i motivi.
3. **Oracolo a linea** (`pinnacle_oracle`): de-vig a 2 esiti da
   totals/spreads Pinnacle, esito del ledger ('Over 2.5' / 'Home -0.75')
   -> p_true del lato giocato, fail-closed su lato mancante/linea diversa/
   cache stantia, gate top-down che ora passa con l'oracolo a linea.
4. **Normalizzazione canonica delle linee** (05/10/2026): la stessa linea
   scritta in formati diversi sui due provider (`'2.50'`, `'+0.25'`, la
   quarter-line come due mezze-linee `'0.0, 0.5'`) DEVE agganciarsi, sia sul
   lato SX (`multi_market.parse_line`) sia su quello Pinnacle/the-odds-api.
5. **Motivi granulari dello scarto**: `no_oracle/EXPIRED_CACHE`,
   `no_oracle/LINE_MISMATCH`, `no_oracle/MISSING_MARKET` al posto del
   generico "Pinnacle assente/incompleto/stantia" (il motivo dice COSA FARE).
6. **Tripwire**: nessun ordine nel modulo, nessuna formula copiata,
   prefisso cache gemello, jobs bot registrati, IaC aggiornata.
"""

import json
import time
from datetime import datetime
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
        # TTL allineata alla FINESTRA di fetch (03/10/2026), non piu' 24h.
        assert odds_api.oracle_fetch_window_min() == 70
        assert odds_api.oracle_cache_ttl_s() == 70 * 60
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
# 2b. Finestra di fetch (03/10/2026): 70 minuti, non il palinsesto intero
# ---------------------------------------------------------------------------

class TestFinestraFetch:
    """Query e selezione dei pick usano la STESSA finestra.

    Direttiva del proprietario: si ordina solo nella finestra esecutiva
    T-60..T-5, quindi non si scarica (ne' si parsa) l'intero palinsesto
    giornaliero della lega. ⚠️ Il costo the-odds-api e' per CHIAMATA, non per
    evento: la finestra stretta riduce il PAYLOAD, non i crediti.
    """

    def test_default_70_minuti(self, monkeypatch):
        monkeypatch.delenv("ORACLE_FETCH_WINDOW_MIN", raising=False)
        assert odds_api.oracle_fetch_window_min() == 70
        assert odds_api.oracle_cache_ttl_s() == 70 * 60

    def test_env_cambia_la_finestra(self, monkeypatch):
        monkeypatch.setenv("ORACLE_FETCH_WINDOW_MIN", "30")
        assert odds_api.oracle_fetch_window_min() == 30
        assert odds_api.oracle_cache_ttl_s() == 1800

    def test_env_impossibile_ricade_sul_default(self, monkeypatch):
        for bad in ("", "abc", "0", "-5"):
            monkeypatch.setenv("ORACLE_FETCH_WINDOW_MIN", bad)
            assert odds_api.oracle_fetch_window_min() == 70, bad

    def test_la_vecchia_costante_24h_e_rimossa(self):
        """Una TTL da 24h su una finestra da 70' e' una bugia: non torni."""
        assert not hasattr(odds_api, "ORACLE_CACHE_TTL_S")

    def test_selezione_allineata_alla_query(self, monkeypatch):
        import line_oracle
        monkeypatch.setenv("ORACLE_FETCH_WINDOW_MIN", "120")
        assert line_oracle._window_h() == pytest.approx(2.0)
        monkeypatch.setenv("ORACLE_FETCH_WINDOW_MIN", "70")
        assert line_oracle._window_h() == pytest.approx(70 / 60.0)

    def test_ensure_oracle_payloads_usa_la_finestra(self, monkeypatch):
        import line_oracle
        captured = {}

        def _fake_fetch(sport, frm, to, **kw):
            # `**kw` accoglie `ttl_s` (TTL dinamica passata dal 05/10/2026).
            captured.update({"sport": sport, "frm": frm, "to": to, **kw})
            return [{"id": "m1"}], 300

        monkeypatch.setattr(odds_api, "fetch_line_odds", _fake_fetch)
        monkeypatch.setattr(line_oracle, "leagues_needing_fetch",
                            lambda: [{"sport_key": "soccer_a"}])
        res = line_oracle.ensure_oracle_payloads(max_leagues=1)
        assert res["fetched"] == 1
        assert captured["sport"] == "soccer_a"
        frm = datetime.fromisoformat(captured["frm"].replace("Z", "+00:00"))
        to = datetime.fromisoformat(captured["to"].replace("Z", "+00:00"))
        assert 69.0 <= (to - frm).total_seconds() / 60.0 <= 71.0

    def test_cache_oltre_la_finestra_e_rifatta(self, monkeypatch, tmp_path):
        """80 minuti di eta': NON fresca (a 24h lo sarebbe stata)."""
        _write_oracle_cache(tmp_path, "soccer_a", [{"id": "old"}],
                            ts=time.time() - 80 * 60)
        monkeypatch.setattr("config.DATA_DIR", tmp_path)
        monkeypatch.setattr("line_oracle.line_picks", lambda: [
            {"match_id": "m1", "esito_key": "Over 2.5", "league": "Serie A",
             "sport_key": "soccer_a", "kickoff": "x",
             "kickoff_ts": time.time() + 600}])
        import line_oracle
        pending = line_oracle.leagues_needing_fetch(time.time())
        assert [x["sport_key"] for x in pending] == ["soccer_a"]

    def test_pick_a_tre_ore_e_fuori_finestra(self, monkeypatch, tmp_path):
        """3h era DENTRO il vecchio orizzonte (24h): ora e' fuori."""
        monkeypatch.setattr("config.DATA_DIR", tmp_path)
        monkeypatch.setattr("line_oracle.line_picks", lambda: [
            {"match_id": "m1", "esito_key": "Over 2.5", "league": "Serie A",
             "sport_key": "soccer_a", "kickoff": "x",
             "kickoff_ts": time.time() + 3 * 3600}])
        import line_oracle
        assert line_oracle.leagues_needing_fetch(time.time()) == []

    def test_nessun_riferimento_al_vecchio_orizzonte(self):
        src = Path("line_oracle.py").read_text()
        assert "ORACLE_PICK_WINDOW_H" not in src
        assert "_pick_window_h" not in src


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

    def test_skip_refetch_e_expired_cache(self, monkeypatch, tmp_path):
        """Lega coperta ma oracolo a linea non pagato -> EXPIRED_CACHE.

        E' la sotto-causa che il 01-04/10 veniva chiamata genericamente
        `linea`: la diagnosi dice COSA FARE (rifetch follow-the-money), quindi
        il pick non e' perso per sempre.
        """
        import auto_bet
        monkeypatch.setattr(auto_bet, "_top_down_load", lambda h, a: None)
        monkeypatch.setattr(auto_bet, "_TOP_DOWN_CACHE_DIR", str(tmp_path))
        monkeypatch.setattr(po, "line_oracle_probs", lambda *a, **k: None)
        monkeypatch.setattr(po, "h2h_cache_is_stale", lambda *a, **k: True)
        v = auto_bet._top_down_eval(self._pick())
        assert v.get("ok") is False
        assert v.get("reason") == "no_oracle/EXPIRED_CACHE"
        assert "fetch_line_odds" in (v.get("detail") or "")

    def test_no_oracle_secco_diventa_missing_market(self, monkeypatch, tmp_path):
        """Partita fuori da ogni cache: non recuperabile -> MISSING_MARKET."""
        import auto_bet
        monkeypatch.setattr(auto_bet, "_top_down_load", lambda h, a: None)
        monkeypatch.setattr(auto_bet, "_TOP_DOWN_CACHE_DIR", str(tmp_path))
        monkeypatch.setattr(po, "line_oracle_probs", lambda *a, **k: None)
        monkeypatch.setattr(po, "h2h_cache_is_stale", lambda *a, **k: False)
        v = auto_bet._top_down_eval(self._pick())
        assert v.get("ok") is False
        assert v.get("reason") == "no_oracle/MISSING_MARKET"

    def test_1x2_conserva_il_motivo_secco(self, monkeypatch, tmp_path):
        """Il 1X2 NON cambia: il motivo granulare e' solo per i mercati a linea."""
        import auto_bet
        monkeypatch.setattr(auto_bet, "_top_down_load", lambda h, a: None)
        monkeypatch.setattr(auto_bet, "_TOP_DOWN_CACHE_DIR", str(tmp_path))
        pick = self._pick(esito_key="1", mercato="1X2")
        v = auto_bet._top_down_eval(pick)
        assert v.get("ok") is False and v.get("reason") == "no_oracle"

    def test_gate_espone_line_mismatch_end_to_end(self, monkeypatch, tmp_path):
        """Linea che Pinnacle NON prezza: il gate lo dice, non 'no_oracle'."""
        import auto_bet
        _write_oracle_cache(tmp_path, "soccer_italy_serie_a",
                            [_match_with_lines()])
        monkeypatch.setattr(auto_bet, "_top_down_load", lambda h, a: None)
        monkeypatch.setattr(auto_bet, "_TOP_DOWN_CACHE_DIR", str(tmp_path))
        v = auto_bet._top_down_eval(self._pick(esito_key="Over 3.5"))
        assert v.get("ok") is False
        assert v.get("reason") == "no_oracle/LINE_MISMATCH"
        assert "3.5" in (v.get("detail") or "")

    def test_oracolo_1x2_non_maschera_la_diagnosi_a_linea(self, monkeypatch,
                                                         tmp_path):
        """Regressione 05/10/2026 (log di produzione delle 18:23).

        Se l'oracolo 1X2 risponde ma quello A LINEA no, il dict 1X2 non deve
        essere usato come verita' per un mercato a linea: lasciandolo in
        `probs` il flusso ricadeva sul ramo generico `LINE_MISMATCH`
        ("lato/linea non riconosciuti") ANZICHE' sulla diagnosi granulare che
        sa dire la causa vera (linea non prezzata + linee disponibili).
        """
        import auto_bet
        _write_oracle_cache(tmp_path, "soccer_italy_serie_a",
                            [_match_with_lines()])
        # La cache 1X2 ESISTE e risponde (e' il caso reale dei log).
        monkeypatch.setattr(auto_bet, "_top_down_load",
                            lambda h, a: {"1": 0.60, "X": 0.25, "2": 0.15})
        monkeypatch.setattr(auto_bet, "_TOP_DOWN_CACHE_DIR", str(tmp_path))
        monkeypatch.setattr(po, "line_oracle_probs", lambda *a, **k: None)
        v = auto_bet._top_down_eval(self._pick(esito_key="Over 2"))
        assert v.get("ok") is False
        assert v.get("reason") == "no_oracle/LINE_MISMATCH"
        # Il dettaglio e' quello della DIAGNOSI ("non prezzata su entrambi i
        # lati" + linee disponibili), NON il generico "lato/linea non
        # riconosciuti" che il fall-through 1X2 produceva in produzione.
        det = v.get("detail") or ""
        assert "prezzata" in det, det
        assert "non coperto" not in det, det


# ---------------------------------------------------------------------------
# 5. Normalizzazione canonica delle linee (05/10/2026)
# ---------------------------------------------------------------------------

class TestNormalizzazioneLinee:
    """La stessa linea scritta in formati diversi DEVE agganciarsi.

    Prima del fix il matching SX-vs-Pinnacle confrontava `float()` grezzi:
    `'2.50'`, `'+0.25'` e la quarter-line `'0.0, 0.5'` (due mezze-linee) non si
    agganciavano alla forma numerica dell'altra fonte e il pick cadeva con
    `no_oracle` per un FALSO disallineamento.
    """

    @pytest.mark.parametrize("raw,expected", [
        (2.5, 2.5), ("2.50", 2.5), ("+0.25", 0.25), ("-0.75", -0.75),
        ("0.0, 0.5", 0.25), ("0.25, 0.75", 0.5), ("Over 2.5", 2.5),
        ("Home -0.75", -0.75), (0, 0.0), ("0", 0.0), (3, 3.0),
    ])
    def test_formati_diversi_stessa_linea(self, raw, expected):
        assert po.normalize_line(raw) == pytest.approx(expected, abs=1e-9)

    def test_zero_e_una_linea_valida(self):
        """0.0 NON e' 'assente': l'handicap pari esiste (fail-closed non qui)."""
        assert po.normalize_line(0.0) == 0.0
        assert po.normalize_line("0") == 0.0

    def test_input_non_interpretabili(self):
        for bad in (True, False, "", "   ", None, "abc", float("nan"),
                    float("inf")):
            with pytest.raises(ValueError):
                po.normalize_line(bad)
        assert po.normalize_line_or_none("abc") is None
        assert po.normalize_line_or_none(None) is None

    def test_arrotondamento_a_due_decimali(self):
        assert po.normalize_line(2.4999999) == 2.5
        assert po.normalize_line("2.4999999") == 2.5

    def test_matching_totals_con_punto_come_stringa(self):
        bm = _match_with_lines()["bookmakers"][0]
        bm["markets"][1]["outcomes"][0]["point"] = "2.50"
        bm["markets"][1]["outcomes"][1]["point"] = "2.5"
        assert po.totals_odds_of(bm, 2.5) == {"Over": 1.90, "Under": 1.95}
        assert po.totals_odds_of(bm, "2.50") == {"Over": 1.90, "Under": 1.95}

    def test_matching_totals_quarter_come_due_mezze_linee(self):
        bm = _match_with_lines()["bookmakers"][0]
        for o in bm["markets"][1]["outcomes"]:
            o["point"] = "2.25, 2.75"        # quarter-line = media = 2.5
        assert po.totals_odds_of(bm, 2.5) == {"Over": 1.90, "Under": 1.95}

    def test_matching_spreads_con_segno_esplicito(self):
        bm = _match_with_lines()["bookmakers"][0]
        bm["markets"][2]["outcomes"][0]["point"] = "-0.75"
        bm["markets"][2]["outcomes"][1]["point"] = "+0.75"
        assert po.spreads_odds_of(bm, "Inter", "Cagliari", "-0.75") == \
            {"Home": 1.85, "Away": 2.00}

    def test_linee_diverse_restano_fail_closed(self):
        """La normalizzazione NON deve agganciare linee DIVERSE."""
        bm = _match_with_lines()["bookmakers"][0]
        assert po.totals_odds_of(bm, 2.25) is None
        assert po.totals_odds_of(bm, 3.0) is None

    def test_lato_sx_usa_la_stessa_definizione(self):
        """Il lato SX (`multi_market.parse_line`) normalizza allo stesso modo."""
        import multi_market as mm
        assert mm.parse_line("Over 2.5") == 2.5
        assert mm.parse_line("0.0, 0.5") == 0.25
        assert mm.parse_line("+0.25") == 0.25
        assert mm.parse_line("Home -0.75") == -0.75
        assert mm.parse_line("2.50") == 2.5
        assert mm.parse_line("abc") is None
        assert mm.parse_line(None) is None
        assert mm.line_key("2.50") == "2.5"
        assert mm.line_key(-0.75) == "-0.75"
        assert mm.line_key(None) == ""

    def test_nessuna_seconda_formula_nel_lato_sx(self):
        """`multi_market` DELEGA: la forma canonica vive in un solo posto."""
        src = Path("multi_market.py").read_text()
        assert "from pinnacle_oracle import normalize_line" in src


# ---------------------------------------------------------------------------
# 6. TTL dinamico sul tempo al kickoff (05/10/2026)
# ---------------------------------------------------------------------------

class TestTTLDinamico:
    """T > 180' -> 30' | 60' <= T <= 180' -> 5' | T < 60' -> 2'."""

    def test_tre_tier_e_i_loro_bordi(self, monkeypatch):
        for env in ("PINNACLE_TTL_LONG_MIN", "PINNACLE_TTL_MID_MIN",
                    "PINNACLE_TTL_SHORT_MIN"):
            monkeypatch.delenv(env, raising=False)
        assert po.cache_ttl_minutes(300) == 30.0
        assert po.cache_ttl_minutes(181) == 30.0
        assert po.cache_ttl_minutes(180) == 5.0     # bordo: entra nel tier 'mid'
        assert po.cache_ttl_minutes(60) == 5.0      # bordo: ancora 'mid'
        assert po.cache_ttl_minutes(59) == 2.0
        assert po.cache_ttl_minutes(0) == 2.0
        assert po.cache_ttl_minutes(-30) == 2.0

    def test_tempo_ignoto_o_invalido_e_conservativo(self):
        """Un'incertezza NON allunga mai la vita di un dato."""
        assert po.cache_ttl_minutes(None) == 2.0
        assert po.cache_ttl_minutes("abc") == 2.0
        assert po.cache_ttl_minutes(float("nan")) == 2.0

    def test_env_tara_i_valori(self, monkeypatch):
        monkeypatch.setenv("PINNACLE_TTL_LONG_MIN", "45")
        monkeypatch.setenv("PINNACLE_TTL_MID_MIN", "7")
        monkeypatch.setenv("PINNACLE_TTL_SHORT_MIN", "1")
        assert po.cache_ttl_minutes(300) == 45.0
        assert po.cache_ttl_minutes(120) == 7.0
        assert po.cache_ttl_minutes(10) == 1.0

    def test_env_impossibile_ricade_sul_default(self, monkeypatch):
        for bad in ("", "abc", "0", "-5"):
            monkeypatch.setenv("PINNACLE_TTL_LONG_MIN", bad)
            assert po.cache_ttl_minutes(300) == 30.0, bad

    def test_minutes_to_kickoff_da_iso_e_millisecondi(self):
        import datetime as _dt
        now = 1_800_000_000.0
        iso = _dt.datetime.fromtimestamp(now + 3600, _dt.timezone.utc)
        assert po.minutes_to_kickoff(iso.isoformat(), now=now) == \
            pytest.approx(60.0)
        assert po.minutes_to_kickoff((now + 3600) * 1000, now=now) == \
            pytest.approx(60.0)
        assert po.minutes_to_kickoff("non-una-data", now=now) is None

    def test_oracle_cache_ttl_s_delega_la_formula(self):
        assert odds_api.oracle_cache_ttl_s() == 70 * 60          # senza kickoff
        assert odds_api.oracle_cache_ttl_s(minutes_to_kickoff=300) == 30 * 60
        assert odds_api.oracle_cache_ttl_s(minutes_to_kickoff=120) == 5 * 60
        assert odds_api.oracle_cache_ttl_s(minutes_to_kickoff=30) == 2 * 60

    def test_gate_rifiuta_il_dato_fuori_dal_ttl_dinamico(self, tmp_path):
        """20h di eta': con la vecchia TTL 24h era 'fresco' — ora no."""
        _write_oracle_cache(tmp_path, "soccer_italy_serie_a",
                            [_match_with_lines()], ts=time.time() - 20 * 3600)
        assert po.line_true_probs("Inter", "Cagliari", market_type="OU",
                                  line=2.5, cache_dir=tmp_path) is None

    def test_gate_accetta_il_dato_dentro_il_ttl(self, tmp_path):
        _write_oracle_cache(tmp_path, "soccer_italy_serie_a",
                            [_match_with_lines()])
        assert po.line_true_probs("Inter", "Cagliari", market_type="OU",
                                  line=2.5, cache_dir=tmp_path) is not None


# ---------------------------------------------------------------------------
# 7. Motivi granulari dello scarto (05/10/2026)
# ---------------------------------------------------------------------------

class TestMotiviGranulari:
    """`line_oracle_reason`: la sotto-causa dice COSA FARE."""

    def test_expired_cache(self, tmp_path):
        _write_oracle_cache(tmp_path, "soccer_italy_serie_a",
                            [_match_with_lines()], ts=time.time() - 3600)
        info = po.line_oracle_reason("Inter", "Cagliari", market_type="OU",
                                     line=2.5, cache_dir=tmp_path)
        assert info["code"] == "EXPIRED_CACHE"
        assert "refetch" in info["detail"]

    def test_line_mismatch(self, tmp_path):
        _write_oracle_cache(tmp_path, "soccer_italy_serie_a",
                            [_match_with_lines()])
        info = po.line_oracle_reason("Inter", "Cagliari", market_type="OU",
                                     line=3.5, cache_dir=tmp_path)
        assert info["code"] == "LINE_MISMATCH"
        assert "3.5" in info["detail"]

    def test_missing_market_partita_assente(self, tmp_path):
        info = po.line_oracle_reason("Milan", "Juventus", market_type="OU",
                                     line=2.5, cache_dir=tmp_path)
        assert info["code"] == "MISSING_MARKET"

    def test_missing_market_mercato_non_pubblicato(self, tmp_path):
        match = _match_with_lines()
        match["bookmakers"][0]["markets"] = [
            m for m in match["bookmakers"][0]["markets"]
            if m["key"] != "totals"]
        _write_oracle_cache(tmp_path, "soccer_italy_serie_a", [match])
        info = po.line_oracle_reason("Inter", "Cagliari", market_type="OU",
                                     line=2.5, cache_dir=tmp_path)
        assert info["code"] == "MISSING_MARKET"

    def test_lega_coperta_ma_linea_non_pagata_e_un_refetch(self, tmp_path):
        """La rotazione h2h copre la partita: il rimedio e' il fetch, non la linea.

        E' il caso che il gate chiamava `linea`: il nome granulare e'
        `EXPIRED_CACHE` (il dato serve e si puo' pagare).
        """
        match = _match_with_lines()
        match["bookmakers"][0]["markets"] = [
            m for m in match["bookmakers"][0]["markets"] if m["key"] != "h2h"]
        _write_oracle_cache(tmp_path, "soccer_italy_serie_a", [match],
                            prefix="toa_")
        info = po.line_oracle_reason("Inter", "Cagliari", market_type="OU",
                                     line=2.5, cache_dir=tmp_path)
        assert info["code"] == "EXPIRED_CACHE"

    def test_linea_mancante_nella_chiamata_non_inventa_un_codice(self, tmp_path):
        """Senza linea la diagnosi resta valida (mai un codice a caso)."""
        _write_oracle_cache(tmp_path, "soccer_italy_serie_a",
                            [_match_with_lines()])
        info = po.line_oracle_reason("Inter", "Cagliari", market_type="OU",
                                     line=None, cache_dir=tmp_path)
        assert info["code"] in ("MISSING_MARKET", "LINE_MISMATCH",
                                "EXPIRED_CACHE")

    def test_mai_eccezioni_su_input_ostili(self):
        for args in (("", ""), (None, None)):
            info = po.line_oracle_reason(args[0], args[1], market_type="OU",
                                         line="abc")
            assert isinstance(info.get("code"), str)
            assert isinstance(info.get("detail"), str)


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
                    "ORACLE_FETCH_WINDOW_MIN", "ORACLE_LEAGUES_PER_PASS",
                    "PINNACLE_TTL_LONG_MIN", "PINNACLE_TTL_MID_MIN",
                    "PINNACLE_TTL_SHORT_MIN"):
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
