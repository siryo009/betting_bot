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
from datetime import datetime, timezone
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
        # TTL allineata alla FINESTRA di fetch (03/10/2026), non piu' 24h;
        # 120 minuti dal 05/10/2026 (la finestra esecutiva e' T-180..T-2 e il
        # primo checkpoint di refetch e' a T-120').
        assert odds_api.oracle_fetch_window_min() == 120
        assert odds_api.oracle_cache_ttl_s() == 120 * 60
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

    def test_default_120_minuti(self, monkeypatch):
        monkeypatch.delenv("ORACLE_FETCH_WINDOW_MIN", raising=False)
        assert odds_api.oracle_fetch_window_min() == 120
        assert odds_api.oracle_cache_ttl_s() == 120 * 60

    def test_env_cambia_la_finestra(self, monkeypatch):
        monkeypatch.setenv("ORACLE_FETCH_WINDOW_MIN", "30")
        assert odds_api.oracle_fetch_window_min() == 30
        assert odds_api.oracle_cache_ttl_s() == 1800

    def test_env_impossibile_ricade_sul_default(self, monkeypatch):
        for bad in ("", "abc", "0", "-5"):
            monkeypatch.setenv("ORACLE_FETCH_WINDOW_MIN", bad)
            assert odds_api.oracle_fetch_window_min() == 120, bad

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
        assert 119.0 <= (to - frm).total_seconds() / 60.0 <= 121.0

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
        assert odds_api.oracle_cache_ttl_s() == 120 * 60         # senza kickoff
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
# 2b. Matching TOLLERANTE dei nomi nel percorso a cache (05/10/2026)
# ---------------------------------------------------------------------------

class TestMatchingNomiTollerante:
    """La partita si trova anche se i due provider scrivono il nome diverso.

    Misurato in produzione il 05/10/2026: the-odds-api scrive
    `Central Córdoba`, SX `Central Cordoba Santiago del Estero` — NESSUNA
    delle due e' sottostringa dell'altra, quindi la partita risultava ASSENTE
    e il pick restava `MISSING_MARKET` **dopo** aver pagato la fetch
    on-demand. Il primo stadio (contenimento di stringa) copre le varianti di
    suffisso; il secondo delega al matcher tollerante `team_names.same_team`
    (lo stesso del settlement dal 12/09), con guardia di ambiguita'.
    """

    def test_variante_di_nome_trovata(self, tmp_path):
        _write_oracle_cache(tmp_path, "soccer_argentina_primera_division",
                            [_match_with_lines(home="Deportivo Riestra",
                                               away="Central Córdoba")])
        status = po._oracle_fixture_status(
            "Deportivo Riestra", "Central Cordoba Santiago del Estero",
            market_type="OU", cache_dir=tmp_path)
        assert status["found"] is True
        assert status["has_market"] is True

    def test_la_diagnosi_non_e_piu_partita_assente(self, tmp_path):
        """Il motivo granulare non deve piu' accusare la cache."""
        _write_oracle_cache(tmp_path, "soccer_argentina_primera_division",
                            [_match_with_lines(home="Deportivo Riestra",
                                               away="Central Córdoba")])
        info = po.line_oracle_reason(
            "Deportivo Riestra", "Central Cordoba Santiago del Estero",
            market_type="OU", line=2.5, cache_dir=tmp_path)
        assert info["detail"] != "partita assente dalle cache oracolo"

    def test_suffisso_di_club_coperto_dal_primo_stadio(self, tmp_path):
        _write_oracle_cache(tmp_path, "soccer_italy_serie_a",
                            [_match_with_lines(home="Inter", away="Cagliari")])
        status = po._oracle_fixture_status("Inter", "Cagliari",
                                          market_type="OU",
                                          cache_dir=tmp_path)
        assert status["found"] is True

    def test_controprova_squadre_diverse_non_fuse(self, tmp_path):
        """`same_team` non e' fuzzy: united e city NON sono la stessa squadra."""
        _write_oracle_cache(tmp_path, "soccer_epl",
                            [_match_with_lines(home="Manchester United",
                                               away="Liverpool")])
        status = po._oracle_fixture_status("Manchester City", "Liverpool",
                                          market_type="OU",
                                          cache_dir=tmp_path)
        assert status["found"] is False

    def test_percorso_1x2_usa_lo_stesso_matching(self, tmp_path):
        """`load_oracle` soffriva dello stesso falso 'partita assente'."""
        from datetime import datetime, timezone
        kickoff = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        match = _match_with_lines(home="Deportivo Riestra",
                                  away="Central Córdoba")
        match["commence_time"] = kickoff
        _write_oracle_cache(tmp_path, "soccer_argentina_primera_division",
                            [match], prefix="toa_")
        rows = list(po._iter_cached_matches(
            "Deportivo Riestra", "Central Cordoba Santiago del Estero",
            cache_dir=tmp_path))
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# 3b. Fetch ON-DEMAND (05/10/2026): il budget segue il pick
# ---------------------------------------------------------------------------

class TestFetchOnDemand:
    """Il budget dell'oracolo (scarso e CONDIVISO) va speso sul pick in finestra.

    Lo scheduler fetcha ogni 30' per "kickoff piu' vicino": con i tier di TTL
    dinamici (2'/5'/30') la cache risultava scaduta proprio sui pick in
    finestra esecutiva e l'AH/OU restava a zero ordini (misurato il 05/10).
    Il gate paga ORA la fetch della lega di QUEL pick: stesso tetto
    giornaliero, speso dove sta per partire un ordine.
    """

    @pytest.fixture(autouse=True)
    def _clean_memo(self, monkeypatch):
        import line_oracle
        # `conftest` spegne la meccanica alla fonte (il percorso live nei test
        # non deve pagare rete): qui si RIACCENDE, perche' questa classe la
        # esercita con `odds_api.fetch_line_odds` iniettato.
        monkeypatch.setenv("ORACLE_ONDEMAND_ENABLED", "1")
        line_oracle.reset_ondemand_dedup()
        yield
        line_oracle.reset_ondemand_dedup()

    def test_interruttore_default_on_e_valori_di_off(self, monkeypatch):
        import line_oracle
        monkeypatch.delenv("ORACLE_ONDEMAND_ENABLED", raising=False)
        assert line_oracle.ondemand_enabled() is True
        for off in ("0", "false", "no", "off", "disabled", "OFF"):
            monkeypatch.setenv("ORACLE_ONDEMAND_ENABLED", off)
            assert line_oracle.ondemand_enabled() is False
        monkeypatch.setenv("ORACLE_ONDEMAND_ENABLED", "1")
        assert line_oracle.ondemand_enabled() is True

    # Base temporale FISSA con kickoff a +13' (dentro la finestra del payload,
    # 120'): un kickoff assoluto renderebbe i test dipendenti dall'orologio
    # (lezione delle date relative del 15/09 e del 17/09).
    NOW = 1_000_000_000.0
    KICKOFF = "2001-09-09T01:59:40Z"          # NOW + 13 minuti (UTC)

    def test_spento_non_paga_e_lo_dichiara(self, monkeypatch):
        import line_oracle, odds_api as oa
        monkeypatch.setenv("ORACLE_ONDEMAND_ENABLED", "0")
        calls = []
        monkeypatch.setattr(oa, "fetch_line_odds",
                            lambda *a, **k: calls.append(1))
        res = line_oracle.fetch_for_pick(self._pick(), now=self.NOW)
        assert res["fetched"] is False and calls == []
        assert "disattivata" in res["reason"]

    def _pick(self, league="Premier League", **kw):
        d = {"match_id": "m1", "home": "Arsenal", "away": "Everton",
             "mercato": "OU", "esito_key": "Over 2.5", "quota": 2.10,
             "league": league, "commence": self.KICKOFF}
        d.update(kw)
        return d

    def test_lega_non_mappata_non_paga_nulla(self):
        import line_oracle
        res = line_oracle.fetch_for_pick(self._pick(league="Lega Inventata"),
                                        now=self.NOW)
        assert res["fetched"] is False
        assert "lega non mappata" in res["reason"]

    def test_kickoff_fuori_finestra_payload_non_paga(self, monkeypatch):
        """3 crediti sarebbero buttati: la query non conterrebbe la partita."""
        import line_oracle, odds_api as oa
        calls = []
        monkeypatch.setattr(oa, "fetch_line_odds",
                            lambda *a, **k: (calls.append(1), ([], 300))[1])
        # kickoff a +3h (180'): oltre la finestra di fetch (120')
        res = line_oracle.fetch_for_pick(self._pick(
            commence="2001-09-09T04:46:40Z"), now=self.NOW)
        assert calls == []
        assert res["fetched"] is False
        assert "oltre la finestra di fetch" in res["reason"]

    def test_kickoff_gia_passato_non_paga(self):
        import line_oracle
        res = line_oracle.fetch_for_pick(
            self._pick(commence="2001-09-09T01:40:00Z"), now=self.NOW)
        assert res["fetched"] is False
        assert "gia' passato" in res["reason"]

    def test_kickoff_ignoto_fail_closed(self):
        import line_oracle
        res = line_oracle.fetch_for_pick(self._pick(commence=""),
                                        now=self.NOW)
        assert res["fetched"] is False
        assert "kickoff ignoto" in res["reason"]

    def test_paga_una_volta_per_lega_nella_stessa_tornata(self, monkeypatch):
        """12 pick sulla stessa lega = UNA fetch (dedup in-process)."""
        import line_oracle, odds_api as oa
        calls = []

        def fake_fetch(sport, frm, to, ttl_s=None, **kw):
            calls.append(sport)
            return ([{"id": "m1"}], 300)

        monkeypatch.setattr(oa, "fetch_line_odds", fake_fetch)
        r1 = line_oracle.fetch_for_pick(self._pick(), now=self.NOW)
        r2 = line_oracle.fetch_for_pick(self._pick(), now=self.NOW)
        assert r1["fetched"] is True and r1["matches"] == 1
        assert r2["fetched"] is False and "dedup" in r2["reason"]
        assert calls == ["soccer_epl"]

    def test_dedup_scade_dopo_l_intervallo(self, monkeypatch):
        import line_oracle, odds_api as oa
        calls = []
        monkeypatch.setattr(oa, "fetch_line_odds",
                            lambda s, f, t, ttl_s=None, **k: (calls.append(s)
                                                              or ([{}], 1)))
        line_oracle.fetch_for_pick(self._pick(), now=self.NOW)
        # T+1h: il kickoff si sposta con l'orologio (a +13' anche stavolta),
        # altrimenti la guardia "kickoff gia' passato" fermerebbe la chiamata
        # e il test misurerebbe la cosa sbagliata.
        line_oracle.fetch_for_pick(self._pick(commence="2001-09-09T02:59:40Z"),
                                   now=self.NOW + 3600.0)
        assert len(calls) == 2

    def test_env_dedup_default_e_valori_impossibili(self, monkeypatch):
        import line_oracle
        monkeypatch.delenv("ORACLE_ONDEMAND_DEDUP_S", raising=False)
        assert line_oracle.ondemand_dedup_s() == 120.0
        for bad in ("abc", "0", "-5"):
            monkeypatch.setenv("ORACLE_ONDEMAND_DEDUP_S", bad)
            assert line_oracle.ondemand_dedup_s() == 120.0
        monkeypatch.setenv("ORACLE_ONDEMAND_DEDUP_S", "30")
        assert line_oracle.ondemand_dedup_s() == 30.0

    def test_budget_esaurito_dichiarato(self, monkeypatch):
        """Nessun payload: la causa e' DICHIARATA (budget), non un 'vuoto'."""
        import line_oracle, odds_api as oa
        monkeypatch.setattr(oa, "fetch_line_odds",
                            lambda s, f, t, ttl_s=None, **k: ([], 250))
        monkeypatch.setattr(oa, "_oracle_req_day",
                            {"day": "2099-01-01", "n": oa.ORACLE_BUDGET_DAY})
        res = line_oracle.fetch_for_pick(self._pick(), now=self.NOW)
        assert res["fetched"] is False
        assert "budget oracolo esaurito" in res["reason"]

    def test_errore_di_rete_non_propaga(self, monkeypatch):
        import line_oracle, odds_api as oa

        def boom(*a, **k):
            raise RuntimeError("rete giu")

        monkeypatch.setattr(oa, "fetch_line_odds", boom)
        res = line_oracle.fetch_for_pick(self._pick(), now=self.NOW)
        assert res["fetched"] is False and "errore fetch" in res["reason"]

    def test_ttl_del_pick_passata_al_fetch(self, monkeypatch):
        """Il TTL dinamico del PICK arriva al fetch (payload fresco al gate)."""
        import line_oracle, odds_api as oa
        seen = {}

        def fake_fetch(sport, frm, to, ttl_s=None, **kw):
            seen["ttl_s"] = ttl_s
            seen["sport"] = sport
            return ([{}], 300)

        monkeypatch.setattr(oa, "fetch_line_odds", fake_fetch)
        line_oracle.fetch_for_pick(self._pick(), now=self.NOW)
        assert seen["sport"] == "soccer_epl"
        assert seen["ttl_s"] is not None and seen["ttl_s"] > 0

    def test_gate_paga_solo_in_finestra_e_solo_su_expired(self, monkeypatch):
        """Il gate NON paga fuori finestra ne' per cause diverse da EXPIRED."""
        import auto_bet, line_oracle
        paid = []
        monkeypatch.setattr(line_oracle, "fetch_for_pick",
                            lambda p, now=None, code=None: (
                                paid.append((p.get("match_id"), code))
                                or {"fetched": True,
                                    "reason": "x",
                                    "sport_key": "soccer_epl",
                                    "matches": 1,
                                    "remaining": 300}))
        pick = self._pick()
        rec = {"reason": "no_oracle/EXPIRED_CACHE", "recoverable": True}
        nrec = {"reason": "no_oracle/MISSING_MARKET", "recoverable": False}
        # (a) interruttore spento: nessuna spesa
        assert auto_bet._ondemand_fetch(pick, rec, False) == ""
        # (b) caso NON recuperabile (Pinnacle non pubblica la linea): pagare
        # non la farebbe comparire -> nessuna spesa, anche se in finestra
        monkeypatch.setattr(auto_bet, "pick_window", lambda p: "within")
        assert auto_bet._ondemand_fetch(pick, nrec, True) == ""
        # (c) diagnosi senza il campo: fail-closed (mai una spesa per ignoto)
        assert auto_bet._ondemand_fetch(
            pick, {"reason": "no_oracle/EXPIRED_CACHE"}, True) == ""
        # (d) fuori finestra esecutiva: nessuna spesa
        monkeypatch.setattr(auto_bet, "pick_window", lambda p: "before")
        assert auto_bet._ondemand_fetch(pick, rec, True) == ""
        assert paid == []
        # (e) in finestra + recuperabile: si paga e lo si DICHIARA nel log
        monkeypatch.setattr(auto_bet, "pick_window", lambda p: "within")
        extra = auto_bet._ondemand_fetch(pick, rec, True)
        assert "FETCH ON-DEMAND" in extra and "soccer_epl" in extra
        # Il CODICE della diagnosi viaggia col fetch: e' cio' che apre la
        # regola dei checkpoint per MISSING_MARKET (05/10/2026).
        assert paid == [("m1", "EXPIRED_CACHE")]

    def test_line_skip_reason_propaga_recoverable(self, monkeypatch, tmp_path):
        """La diagnosi del gate porta il campo su cui si decide di PAGARE."""
        import auto_bet
        monkeypatch.setattr(auto_bet, "_TOP_DOWN_CACHE_DIR", str(tmp_path))
        # partita assente + cache h2h stantia -> EXPIRED_CACHE recuperabile
        monkeypatch.setattr(po, "h2h_cache_is_stale", lambda *a, **k: True)
        info = auto_bet._line_skip_reason(self._pick(), "OU", 2.5)
        assert info["reason"] == "no_oracle/EXPIRED_CACHE"
        assert info["recoverable"] is True
        # Pinnacle pubblica la partita ma NON questa linea -> non recuperabile
        _write_oracle_cache(tmp_path, "soccer_epl", [_match_with_lines(
            home="Arsenal", away="Everton")])
        info2 = auto_bet._line_skip_reason(self._pick(esito_key="Over 9.5"),
                                          "OU", 9.5)
        assert info2["reason"] == "no_oracle/LINE_MISMATCH"
        assert info2["recoverable"] is False

    def test_gate_end_to_end_espone_il_fetch(self, monkeypatch, tmp_path):
        import auto_bet, line_oracle
        monkeypatch.setattr(auto_bet, "_top_down_load", lambda h, a: None)
        monkeypatch.setattr(auto_bet, "_TOP_DOWN_CACHE_DIR", str(tmp_path))
        monkeypatch.setattr(po, "line_oracle_probs", lambda *a, **k: None)
        monkeypatch.setattr(po, "h2h_cache_is_stale", lambda *a, **k: True)
        monkeypatch.setattr(auto_bet, "pick_window", lambda p: "within")
        monkeypatch.setattr(line_oracle, "fetch_for_pick",
                            lambda p, now=None, code=None: {
                                "fetched": True, "reason": "ok",
                                "sport_key": "soccer_epl", "matches": 4,
                                "remaining": 300})
        v = auto_bet._top_down_eval(self._pick(), fetch_missing=True)
        assert v["reason"] == "no_oracle/EXPIRED_CACHE"
        assert "FETCH ON-DEMAND" in v["detail"]

    def test_default_off_nei_test_e_on_in_produzione(self):
        """I chiamanti non-live non devono poter spendere crediti."""
        import inspect
        import auto_bet
        sig = inspect.signature(auto_bet._top_down_eval)
        assert sig.parameters["fetch_missing"].default is False
        src = Path("auto_bet.py").read_text()
        assert "fetch_missing=True" in src


# ---------------------------------------------------------------------------
# 3c. CHECKPOINT di refetch per MISSING_MARKET (05/10/2026)
# ---------------------------------------------------------------------------

class TestCheckpointRefetch:
    """Un mercato che Pinnacle non pubblica non si richiede a ogni ciclo di 60s.

    Senza freno il gate lo ri-chiede OGNI volta su una partita in finestra
    (fino a 1440 richieste/giorno sulla stessa partita): due soli checkpoint,
    T-120' e T-70', con stato PERSISTENTE sul volume (un redeploy non riapre
    la spesa).
    """

    NOW = 1_000_000_000.0

    @pytest.fixture(autouse=True)
    def _clean(self, monkeypatch, tmp_path):
        import line_oracle
        monkeypatch.setenv("ORACLE_ONDEMAND_ENABLED", "1")
        monkeypatch.setenv("ORACLE_CHECKPOINT_STATE",
                           str(tmp_path / "cp.json"))
        line_oracle.reset_checkpoints()
        line_oracle.reset_ondemand_dedup()
        yield
        line_oracle.reset_checkpoints()
        line_oracle.reset_ondemand_dedup()

    def _pick(self, minutes: float, **kw) -> dict:
        ko = (datetime.fromtimestamp(self.NOW + minutes * 60,
                                     tz=timezone.utc)
              .strftime("%Y-%m-%dT%H:%M:%SZ"))
        d = {"match_id": "m1", "home": "Arsenal", "away": "Everton",
             "mercato": "OU", "esito_key": "Over 2.5", "quota": 2.10,
             "league": "Premier League", "commence": ko}
        d.update(kw)
        return d

    def test_checkpoint_for_apre_solo_t120_e_t70(self):
        import line_oracle
        assert line_oracle.checkpoint_for(180) is None      # troppo presto
        assert line_oracle.checkpoint_for(121) is None
        assert line_oracle.checkpoint_for(120) == "T-120"
        assert line_oracle.checkpoint_for(100) == "T-120"
        assert line_oracle.checkpoint_for(70) == "T-70"
        assert line_oracle.checkpoint_for(30) == "T-70"
        assert line_oracle.checkpoint_for(0) is None        # gia' iniziata
        assert line_oracle.checkpoint_for(None) is None
        assert line_oracle.checkpoint_for("x") is None

    def test_missing_market_oltre_la_finestra_non_paga(self, monkeypatch):
        """A T-150 si esce PRIMA dei checkpoint: fuori dalla finestra di fetch."""
        import line_oracle, odds_api as oa
        calls = []
        monkeypatch.setattr(oa, "fetch_line_odds",
                            lambda *a, **k: (calls.append(1), ([], 300))[1])
        res = line_oracle.fetch_for_pick(self._pick(150),
                                         now=self.NOW,
                                         code="MISSING_MARKET")
        assert res["fetched"] is False and calls == []
        assert "oltre la finestra di fetch" in res["reason"]

    def test_missing_market_prima_del_checkpoint_non_paga(self, monkeypatch):
        """Con una finestra di fetch piu' larga il freno e' il CHECKPOINT.

        `T-120'` e' il primo tentativo ammesso: sopra quella soglia (qui resa
        raggiungibile con `ORACLE_FETCH_WINDOW_MIN=180`) il mercato mancante
        non e' ancora da chiedere — Pinnacle non ha ancora pubblicato.
        """
        import line_oracle, odds_api as oa
        calls = []
        monkeypatch.setenv("ORACLE_FETCH_WINDOW_MIN", "180")
        monkeypatch.setattr(oa, "fetch_line_odds",
                            lambda *a, **k: (calls.append(1), ([], 300))[1])
        res = line_oracle.fetch_for_pick(self._pick(150),
                                         now=self.NOW,
                                         code="MISSING_MARKET")
        assert res["fetched"] is False and calls == []
        assert "checkpoint non aperto" in res["reason"]

    def test_missing_market_paga_una_volta_per_checkpoint(self, monkeypatch):
        import line_oracle, odds_api as oa
        calls = []
        monkeypatch.setattr(oa, "fetch_line_odds",
                            lambda *a, **k: (calls.append(1), ([{"id": "m"}], 300))[1])
        # T-100: primo tentativo (checkpoint T-120) -> si paga
        r1 = line_oracle.fetch_for_pick(self._pick(100), now=self.NOW,
                                        code="MISSING_MARKET")
        assert r1["fetched"] is True and r1["checkpoint"] == "T-120"
        # secondo giro (stessa partita, checkpoint NON ancora scaduto): zero HTTP
        r2 = line_oracle.fetch_for_pick(self._pick(100), now=self.NOW,
                                        code="MISSING_MARKET")
        assert r2["fetched"] is False and "gia' onorato" in r2["reason"]
        assert len(calls) == 1
        # a T-60 si apre il SECONDO (e ultimo) checkpoint: si paga ancora
        # (la dedup per lega e' azzerata: in produzione i due tentativi
        # distano ~50 minuti, qui tutti i giri sono allo stesso istante).
        line_oracle.reset_ondemand_dedup()
        r3 = line_oracle.fetch_for_pick(self._pick(60), now=self.NOW,
                                        code="MISSING_MARKET")
        assert r3["checkpoint"] == "T-70" and r3["fetched"] is True
        assert len(calls) == 2
        # ...e da li' in poi MAI piu'
        line_oracle.reset_ondemand_dedup()
        r4 = line_oracle.fetch_for_pick(self._pick(30), now=self.NOW,
                                        code="MISSING_MARKET")
        assert r4["fetched"] is False and len(calls) == 2
        assert "gia' onorato" in r4["reason"]

    def test_il_tentativo_si_consuma_anche_con_payload_vuoto(self, monkeypatch):
        """La regola e' "due tentativi", non "due riusciti"."""
        import line_oracle, odds_api as oa
        calls = []
        monkeypatch.setattr(oa, "fetch_line_odds",
                            lambda *a, **k: (calls.append(1), ([], 300))[1])
        r1 = line_oracle.fetch_for_pick(self._pick(100), now=self.NOW,
                                        code="MISSING_MARKET")
        assert r1["fetched"] is False and r1["checkpoint"] == "T-120"
        r2 = line_oracle.fetch_for_pick(self._pick(100), now=self.NOW,
                                        code="MISSING_MARKET")
        assert "gia' onorato" in r2["reason"] and len(calls) == 1

    def test_errore_di_rete_non_consuma_il_checkpoint(self, monkeypatch):
        """Un errore transitorio non deve bruciare il tentativo."""
        import line_oracle, odds_api as oa
        state = {"n": 0}

        def _boom(*a, **k):
            state["n"] += 1
            raise RuntimeError("rete giu")

        monkeypatch.setattr(oa, "fetch_line_odds", _boom)
        r1 = line_oracle.fetch_for_pick(self._pick(100), now=self.NOW,
                                        code="MISSING_MARKET")
        assert "errore fetch" in r1["reason"]
        line_oracle.reset_ondemand_dedup()
        monkeypatch.setattr(oa, "fetch_line_odds",
                            lambda *a, **k: ([{"id": "m"}], 300))
        r2 = line_oracle.fetch_for_pick(self._pick(100), now=self.NOW,
                                        code="MISSING_MARKET")
        assert r2["fetched"] is True and r2["checkpoint"] == "T-120"

    def test_le_cause_diverse_da_missing_market_restano_libere(self, monkeypatch):
        """`EXPIRED_CACHE` = il dato esiste e va solo rinfrescato: nessun freno."""
        import line_oracle, odds_api as oa
        calls = []
        monkeypatch.setattr(oa, "fetch_line_odds",
                            lambda *a, **k: (calls.append(1), ([{"id": "m"}], 300))[1])
        for _ in range(3):
            line_oracle.reset_ondemand_dedup()
            line_oracle.fetch_for_pick(self._pick(50), now=self.NOW,
                                       code="EXPIRED_CACHE")
        assert len(calls) == 3

    def test_stato_persistente_su_file(self, monkeypatch, tmp_path):
        """Un redeploy non riapre la spesa: lo stato vive sul volume."""
        import line_oracle, odds_api as oa
        path = tmp_path / "cp.json"
        monkeypatch.setenv("ORACLE_CHECKPOINT_STATE", str(path))
        monkeypatch.setattr(oa, "fetch_line_odds",
                            lambda *a, **k: ([{"id": "m"}], 300))
        line_oracle.fetch_for_pick(self._pick(100), now=self.NOW,
                                   code="MISSING_MARKET")
        assert path.exists()
        line_oracle.reset_checkpoints()          # simula un processo nuovo
        assert line_oracle.checkpoint_honoured("m1") == "T-120"
        res = line_oracle.fetch_for_pick(self._pick(100), now=self.NOW,
                                         code="MISSING_MARKET")
        assert res["fetched"] is False and "gia' onorato" in res["reason"]

    def test_stato_corrotto_non_impedisce_e_non_propaga(self, monkeypatch,
                                                        tmp_path):
        import line_oracle
        path = tmp_path / "cp.json"
        path.write_text("non-json", encoding="utf-8")
        monkeypatch.setenv("ORACLE_CHECKPOINT_STATE", str(path))
        line_oracle.reset_checkpoints()
        assert line_oracle.checkpoint_honoured("m1") is None

    def test_default_state_dentro_data_dir(self, monkeypatch):
        import line_oracle
        monkeypatch.delenv("ORACLE_CHECKPOINT_STATE", raising=False)
        assert "oracle_checkpoints.json" in str(line_oracle.checkpoint_state_path())

    def test_mark_senza_match_o_label_non_scrive(self, monkeypatch, tmp_path):
        import line_oracle
        path = tmp_path / "cp.json"
        monkeypatch.setenv("ORACLE_CHECKPOINT_STATE", str(path))
        line_oracle.mark_checkpoint("", "T-120")
        line_oracle.mark_checkpoint("m1", None)
        assert not path.exists()


# ---------------------------------------------------------------------------
# 3d. LEAGUE TIERING sul refetch a pagamento (05/10/2026)
# ---------------------------------------------------------------------------

class TestLeagueTiering:
    """Il refetch a PAGAMENTO e' riservato alle leghe Tier-1/Core.

    Le leghe in probation restano giocabili ma si valutano SOLO sulla cache
    passiva: 3 crediti non si spendono su un campionato di cui non e' ancora
    stato misurato un ROI positivo.
    """

    def _pick(self, league):
        return {"match_id": "m1", "home": "A", "away": "B", "mercato": "OU",
                "esito_key": "Over 2.5", "quota": 2.0, "league": league}

    def test_lega_core_paga(self, monkeypatch):
        import auto_bet, line_oracle
        paid = []
        monkeypatch.setattr(auto_bet, "pick_window", lambda p: "within")
        monkeypatch.setattr(line_oracle, "fetch_for_pick",
                            lambda p, now=None, code=None: (
                                paid.append(p["league"])
                                or {"fetched": True, "reason": "ok",
                                    "sport_key": "soccer_epl", "matches": 1,
                                    "remaining": 300}))
        rec = {"reason": "no_oracle/EXPIRED_CACHE", "recoverable": True}
        out = auto_bet._ondemand_fetch(self._pick("Premier League"), rec, True)
        assert "FETCH ON-DEMAND" in out and paid == ["Premier League"]

    def test_lega_in_probation_non_paga_e_lo_dichiara(self, monkeypatch):
        import auto_bet, line_oracle
        paid = []
        monkeypatch.setattr(auto_bet, "pick_window", lambda p: "within")
        monkeypatch.setattr(line_oracle, "fetch_for_pick",
                            lambda p, now=None, code=None: (
                                paid.append(1)
                                or {"fetched": True, "reason": "ok"}))
        rec = {"reason": "no_oracle/EXPIRED_CACHE", "recoverable": True}
        out = auto_bet._ondemand_fetch(self._pick("Liga MX"), rec, True)
        assert "non Tier-1/Core" in out and "cache passiva" in out
        assert paid == []

    def test_lega_vietata_non_paga(self, monkeypatch):
        import auto_bet, line_oracle
        monkeypatch.setattr(auto_bet, "pick_window", lambda p: "within")
        monkeypatch.setattr(line_oracle, "fetch_for_pick",
                            lambda p, now=None, code=None: {"fetched": True})
        rec = {"reason": "no_oracle/EXPIRED_CACHE", "recoverable": True}
        out = auto_bet._ondemand_fetch(self._pick("Serie A"), rec, True)
        assert "non Tier-1/Core" in out

    def test_tier_non_leggibile_non_paga(self, monkeypatch):
        """Fail-closed: una spesa non autorizzata non passa per un import rotto."""
        import builtins
        import auto_bet, line_oracle
        monkeypatch.setattr(auto_bet, "pick_window", lambda p: "within")
        real_import = builtins.__import__

        def _fake_import(name, *a, **k):
            if name == "value_filter":
                raise ImportError("rotto")
            return real_import(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", _fake_import)
        rec = {"reason": "no_oracle/EXPIRED_CACHE", "recoverable": True}
        out = auto_bet._ondemand_fetch(self._pick("Premier League"), rec, True)
        assert "tier di lega non leggibile" in out

    def test_is_core_league_allineato_a_league_tier(self):
        from value_filter import is_core_league, league_tier
        for lega in ("Premier League", "Bundesliga", "Liga MX", "Serie A",
                     "Lega Inventata", ""):
            assert is_core_league(lega) is (league_tier(lega) == "core")


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
                    "ORACLE_ONDEMAND_DEDUP_S", "ORACLE_ONDEMAND_ENABLED",
                    "ORACLE_CHECKPOINT_STATE",
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
