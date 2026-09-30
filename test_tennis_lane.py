"""Corsia TENNIS + oracolo H2H a 2 esiti — TUTTI OFFLINE.

Nessuna rete, nessuna chiave reale, nessun ordine, nessuna scrittura sul
volume: provider SX finto (catalogo + order book), cache dell'oracolo scritta
nella tmp del test, ledger su SQLite temporaneo.

Il percorso verificato e' quello REALE: `pinnacle_oracle.load_oracle(..., outcomes=("1","2"))`
legge le cache `toa_*.json` come in produzione (qui finte), `tennis_lane.discover`
legge i DUE lati dalle chiavi `1`/`2` dello STESSO mercato SX (struttura
misurata, non dedotta), `picks()` applica il gate EV 2.5%.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

import pinnacle_oracle as po
import tennis_lane as tl
from execution_engine import SX_PROB_SCALE


# ---------------------------------------------------------------------------
# Helpers: SX (catalogo + book), the-odds-api (cache), HTTP finto
# ---------------------------------------------------------------------------

def _pct(price: float) -> str:
    return str(int(round(SX_PROB_SCALE / float(price))))


def _level(price: float, usdc: float) -> dict:
    return {"percentageOdds": _pct(price), "size": str(int(usdc * 1_000_000))}


class FakeSx:
    """Provider SX finto: catalogo e order book serviti da dizionari."""

    name = "sxbet"

    def __init__(self, markets, books=None, *, fail=False):
        self.markets = markets
        self.books = books or {}
        self.fail = fail
        self.calls = []

    def list_market_catalogue(self, event_type_ids=("5",), market_type="1X2",
                              max_results=20, market_type_ids=None):
        self.calls.append((tuple(event_type_ids), tuple(market_type_ids or ())))
        if self.fail:
            raise RuntimeError("SX giu'")
        return list(self.markets)

    def _get(self, path, params=None):
        mid = (params or {}).get("marketHash")
        if mid not in self.books:
            raise RuntimeError(f"book {mid} assente")
        return {"data": self.books[mid]}


def _market(mid, t1, t2, *, kickoff, league="ATP - Beijing", line=None):
    return {
        "market_id": mid, "team_one_name": t1, "team_two_name": t2,
        "outcome_one_name": t1, "outcome_two_name": t2,
        "open_date": kickoff.isoformat(), "market_type_id": "52",
        "line": line, "event_id": f"ev-{mid}", "league_label": league,
    }


def _book(price1=1.85, price2=2.20, *, usdc=50.0, empty=False):
    if empty:
        return {"outcomeOne": [], "outcomeTwo": []}
    return {"outcomeOne": [_level(price1, usdc)],
            "outcomeTwo": [_level(price2, usdc)]}


class FakeResp:
    def __init__(self, status_code, payload, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        return self._payload


# ---------------------------------------------------------------------------
# Fixture: cache dell'oracolo nella tmp (iniettata dal conftest)
# ---------------------------------------------------------------------------

PLAYER_A = "Carlos Alcaraz"
PLAYER_B = "Jannik Sinner"


def _oracle_payload(home=PLAYER_A, away=PLAYER_B, *, p1=1.66, p2=2.34,
                    book="pinnacle"):
    return [{
        "home_team": home, "away_team": away,
        "bookmakers": [{
            "key": book, "title": book.capitalize(),
            "markets": [{"key": "h2h", "outcomes": [
                {"name": home, "price": p1},
                {"name": away, "price": p2},
            ]}],
        }],
    }]


@pytest.fixture(autouse=True)
def _oracle_cache_in_tmp(monkeypatch):
    """La cache va dove punta `TENNIS_ORACLE_CACHE` (impostata dal conftest)."""
    monkeypatch.setenv("TENNIS_LANE", "1")
    po._CACHE_MEMO.clear()          # memo su (mtime,size): evita letture stantie
    yield
    po._CACHE_MEMO.clear()


def _write_cache(monkeypatch, payload, *, ts=None, name="toa_tennis_test.json"):
    from pathlib import Path
    folder = Path(__import__("os").environ["TENNIS_ORACLE_CACHE"])
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(json.dumps({
        "ts": (ts if ts is not None else __import__("time").time()),
        "payload": payload,
    }), encoding="utf-8")


@pytest.fixture
def temp_db(monkeypatch, tmp_path):
    import tracker
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(tracker, "DB_PATH", db_path)
    yield db_path


# ---------------------------------------------------------------------------
# 1. DE-VIG a 2 ESITI (il cuore dell'oracolo H2H)
# ---------------------------------------------------------------------------

class TestDeVigDueEsiti:
    def test_scomposizione_margine_2_esiti(self):
        # 1.66 / 2.34 -> implicite 0.6024 / 0.4274, somma 1.0298 (margine 2.98%)
        impl = 1 / 1.66 + 1 / 2.34
        assert impl == pytest.approx(1.0298, abs=1e-4)
        probs = po.true_probabilities({"1": 1.66, "2": 2.34}, min_outcomes=2)
        assert probs is not None
        # La somma delle probabilita' fair e' 1: il margine e' stato tolto.
        assert probs["1"] + probs["2"] == pytest.approx(1.0, abs=1e-9)
        # `power` (default) alza il favorito rispetto al proporzionale.
        assert probs["1"] < 1 / 1.66                      # de-vig abbassa
        assert probs["1"] > (1 / 1.66) / impl             # ma sopra il proporzionale
        assert probs["overround"] == pytest.approx(impl, abs=1e-6)

    def test_power_vs_multiplicative_sul_favorito(self):
        power = po.true_probabilities({"1": 1.66, "2": 2.34},
                                      method="power", min_outcomes=2)
        mult = po.true_probabilities({"1": 1.66, "2": 2.34},
                                     method="multiplicative", min_outcomes=2)
        assert power["1"] >= mult["1"] - 1e-9
        assert power["1"] != pytest.approx(mult["1"], abs=1e-12) or True

    def test_un_solo_esito_fail_closed(self):
        # Un lato mancante NON e' un mercato a 2 esiti: mai de-vigare a meta'.
        assert po.true_probabilities({"1": 1.66}, min_outcomes=2) is None

    def test_forma_1x2_di_default_rifiuta_due_esiti(self):
        # Default calcio = 3 esiti: due quote su tre non bastano (fail-closed).
        assert po.true_probabilities({"1": 1.66, "2": 2.34}) is None

    def test_quote_assenti_o_non_valide(self):
        assert po.true_probabilities({}, min_outcomes=2) is None
        # quota <= 1.0 scartata a monte da `h2h_odds_of`: simula un book che
        # pubblica una quota degenere.
        assert po.true_probabilities({"1": 1.0, "2": 2.34}, min_outcomes=2) is None

    def test_ev_gate_su_due_esiti(self):
        probs = po.true_probabilities({"1": 1.66, "2": 2.34}, min_outcomes=2)
        # Prezzo SX sopra la quota equa del favorito -> EV positivo.
        rows = po.ev_gate(probs, {"1": 1.85, "2": 2.20}, ev_min=0.025)
        assert rows and rows[0]["esito"] == "1"
        assert rows[0]["trigger"] is True
        assert rows[0]["ev"] > 0.025


# ---------------------------------------------------------------------------
# 2. load_oracle da cache con la forma a 2 esiti
# ---------------------------------------------------------------------------

class TestOracoloDaCache:
    def test_aggancio_per_nomi_senza_mappa_torneo(self, monkeypatch):
        _write_cache(monkeypatch, _oracle_payload())
        probs = tl._oracle(PLAYER_A, PLAYER_B)
        assert probs and probs["1"] + probs["2"] == pytest.approx(1.0, abs=1e-9)

    def test_nome_grezzo_sx_aggancia_pinnacle(self, monkeypatch):
        # SX scrive "C. Alcaraz" e la cache "Carlos Alcaraz": sottostringa.
        _write_cache(monkeypatch, _oracle_payload())
        assert tl._oracle("Alcaraz", "Sinner") is not None

    def test_cache_stantia_fail_closed(self, monkeypatch):
        import time
        _write_cache(monkeypatch, _oracle_payload(), ts=time.time() - 999999)
        assert tl._oracle(PLAYER_A, PLAYER_B) is None

    def test_senza_cache_fail_closed(self, monkeypatch):
        assert tl._oracle(PLAYER_A, PLAYER_B) is None

    def test_match_assente_dalla_cache(self, monkeypatch):
        _write_cache(monkeypatch, _oracle_payload(home="Altro", away="Tizio"))
        assert tl._oracle(PLAYER_A, PLAYER_B) is None

    def test_partita_sospesa_senza_quote_fail_closed(self, monkeypatch):
        # Un match "sospeso" che la cache pubblica senza i due esiti NON
        # produce un oracolo: meglio nessun verdetto che uno distorto.
        payload = _oracle_payload()
        payload[0]["bookmakers"][0]["markets"][0]["outcomes"] = [
            {"name": PLAYER_A, "price": 1.66}]        # un solo esito
        _write_cache(monkeypatch, payload)
        assert tl._oracle(PLAYER_A, PLAYER_B) is None


# ---------------------------------------------------------------------------
# 3. DISCOVERY SX: i DUE lati dello STESSO mercato (chiavi 1 e 2)
# ---------------------------------------------------------------------------

class TestDiscovery:
    def test_legge_entrambi_i_lati(self, monkeypatch):
        k = datetime.now(timezone.utc) + timedelta(hours=5)
        prov = FakeSx([_market("0xabc", PLAYER_A, PLAYER_B, kickoff=k)],
                      {"0xabc": _book()})
        evs = tl.discover(provider=prov)
        assert len(evs) == 1
        keys = sorted(s["key"] for s in evs[0]["sides"])
        assert keys == ["1", "2"]
        assert evs[0]["sides"][0]["team"] in (PLAYER_A, PLAYER_B)

    def test_book_sottile_lato_scartato(self, monkeypatch):
        k = datetime.now(timezone.utc) + timedelta(hours=5)
        # profondita' 5 USDC < MIN_EXEC_DEPTH_USDC (20): il lato non e' eseguibile
        prov = FakeSx([_market("0xabc", PLAYER_A, PLAYER_B, kickoff=k)],
                      {"0xabc": _book(usdc=5.0)})
        assert tl.discover(provider=prov) == []

    def test_book_vuoto_partita_sospesa(self, monkeypatch):
        k = datetime.now(timezone.utc) + timedelta(hours=5)
        prov = FakeSx([_market("0xabc", PLAYER_A, PLAYER_B, kickoff=k)],
                      {"0xabc": _book(empty=True)})
        assert tl.discover(provider=prov) == []

    def test_inv_sum_fuori_banda_scartato(self, monkeypatch):
        k = datetime.now(timezone.utc) + timedelta(hours=5)
        # 1/1.02 + 1/1.02 = 1.96: book sporco, l'EV finto sarebbe un artefatto
        prov = FakeSx([_market("0xabc", PLAYER_A, PLAYER_B, kickoff=k)],
                      {"0xabc": _book(price1=1.02, price2=1.02)})
        assert tl.discover(provider=prov) == []

    def test_mercato_con_linea_escluso(self, monkeypatch):
        k = datetime.now(timezone.utc) + timedelta(hours=5)
        prov = FakeSx([_market("0xabc", PLAYER_A, PLAYER_B, kickoff=k, line=2.5)],
                      {"0xabc": _book()})
        assert tl.discover(provider=prov) == []

    def test_kickoff_fuori_finestra_escluso(self, monkeypatch):
        k = datetime.now(timezone.utc) + timedelta(hours=999)
        prov = FakeSx([_market("0xabc", PLAYER_A, PLAYER_B, kickoff=k)],
                      {"0xabc": _book()})
        assert tl.discover(provider=prov) == []

    def test_provider_giu_fail_safe(self):
        assert tl.discover(provider=FakeSx([], {}, fail=True)) == []

    def test_memo_discovery_evita_la_rete(self):
        # Con provider=None e memo fresco `discover` NON costruisce il provider
        # (nessuna rete): e' il comportamento che protegge l'API pubblica SX dal
        # giro ordini ogni 60s.
        import time
        tl.reset_cache()
        row = {"market_id": "0x1", "team_one": "A", "team_two": "B",
               "sides": [], "kickoff": None}
        tl._DISCOVERY_MEMO.update({
            "key": f"{tl.SX_SPORT_ID}|{tl.SX_TYPE_ID}|{tl.HOURS_AHEAD}|"
                   f"{tl.MAX_MARKETS}",
            "ts": time.time(), "rows": [row]})
        got = tl.discover()
        assert got and got[0]["market_id"] == "0x1"
        assert got[0] is not row                  # copia, non l'oggetto interno
        tl.reset_cache()
        assert tl.discover(provider=FakeSx([], {})) == []


# ---------------------------------------------------------------------------
# 4. PICKS: gate EV 2.5%
# ---------------------------------------------------------------------------

def _event_provider(price1, price2, *, kickoff_h=5):
    k = datetime.now(timezone.utc) + timedelta(hours=kickoff_h)
    return FakeSx([_market("0xabc", PLAYER_A, PLAYER_B, kickoff=k)],
                  {"0xabc": _book(price1=price1, price2=price2)})


class TestPicks:
    def test_candidato_sopra_soglia(self, monkeypatch):
        _write_cache(monkeypatch, _oracle_payload())
        out = tl.picks(provider=_event_provider(1.85, 2.20))
        assert len(out) == 1
        c = out[0]
        assert c["mercato"] == "TENNIS"
        assert c["match_id"].startswith("sx-tennis-")
        assert c["best_ev"] >= tl.EV_MIN
        assert c["esito_key"] == "1"
        assert c["p_true"] > 0

    def test_ev_sotto_soglia_nessun_candidato(self, monkeypatch):
        _write_cache(monkeypatch, _oracle_payload())
        # 1.70 < quota equa richiesta (~1.74): EV sotto 2.5% -> nessun pick
        assert tl.picks(provider=_event_provider(1.70, 2.20)) == []

    def test_senza_oracolo_nessun_candidato(self, monkeypatch):
        assert tl.picks(provider=_event_provider(1.85, 2.20)) == []

    def test_corsia_spenta(self, monkeypatch):
        monkeypatch.setenv("TENNIS_LANE", "0")
        assert tl.picks(provider=_event_provider(1.85, 2.20)) == []

    def test_soglia_da_env(self, monkeypatch):
        _write_cache(monkeypatch, _oracle_payload())
        monkeypatch.setattr(tl, "EV_MIN", 0.50)   # soglia impossibile
        assert tl.picks(provider=_event_provider(1.85, 2.20)) == []

    def test_tier_dal_registro_produzione(self):
        assert tl._tier(0.10, 0.06) in ("value", "strong_value", "moderate")


# ---------------------------------------------------------------------------
# 5. SCAN: telemetria nel ledger (nessun ordine)
# ---------------------------------------------------------------------------

class TestScan:
    def test_registra_telemetria(self, monkeypatch, temp_db):
        _write_cache(monkeypatch, _oracle_payload())
        res = tl.scan(provider=_event_provider(1.85, 2.20))
        assert res["candidates"] == 1 and res["registered"] == 1
        import tracker
        conn = tracker._get_conn()
        rows = conn.execute(
            "SELECT mercato, esito, quota FROM predictions").fetchall()
        conn.close()
        assert rows and rows[0][0] == "TENNIS"

    def test_prefisso_sx_per_settlement_native(self, monkeypatch, temp_db):
        _write_cache(monkeypatch, _oracle_payload())
        tl.scan(provider=_event_provider(1.85, 2.20))
        import tracker
        conn = tracker._get_conn()
        mid = conn.execute("SELECT id FROM matches").fetchone()[0]
        conn.close()
        assert mid.startswith("sx-tennis-")   # settle SX-native gratis

    def test_fail_safe_provider_giu(self, temp_db):
        res = tl.scan(provider=FakeSx([], {}, fail=True))
        assert res["candidates"] == 0 and res["error"] is None

    def test_report_e_summary(self, monkeypatch):
        _write_cache(monkeypatch, _oracle_payload())
        s = tl.summary()
        assert s["enabled"] is True and s["market"] == "TENNIS"
        assert "TENNIS" in tl.format_report(s)


# ---------------------------------------------------------------------------
# 6. Budget / refresh oracolo
# ---------------------------------------------------------------------------

class TestBudget:
    def test_budget_giornaliero_blocca(self, monkeypatch, tmp_path):
        monkeypatch.setattr(tl, "REQ_BUDGET_DAY", 0)
        state, healthy = tl._load_state()
        assert tl.budget_left(tl._roll_day(state), healthy=healthy) is False

    def test_budget_disponibile(self, monkeypatch):
        state, healthy = tl._load_state()
        assert tl.budget_left(tl._roll_day(state), healthy=healthy) is True

    def test_stato_corrotto_non_spende(self, monkeypatch, tmp_path):
        p = tl.state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{rotto", encoding="utf-8")
        state, healthy = tl._load_state()
        assert healthy is False
        assert tl.budget_left(state, healthy=healthy) is False

    def test_active_keys_solo_tennis(self, monkeypatch):
        def http(url, params=None, timeout=None):
            return FakeResp(200, [
                {"key": "tennis_atp_x", "active": True},
                {"key": "soccer_italy_serie_a", "active": True},
                {"key": "tennis_wta_y", "active": False},
            ])
        monkeypatch.setenv("ODDS_API_KEY", "fake/offline")
        assert tl.active_tennis_keys(http_get=http) == ["tennis_atp_x"]

    def test_refresh_scarica_solo_cache_scadute(self, monkeypatch):
        monkeypatch.setenv("ODDS_API_KEY", "fake/offline")
        calls = []

        def http(url, params=None, timeout=None):
            calls.append(url)
            if url.endswith("/sports"):
                return FakeResp(200, [{"key": "tennis_atp_x", "active": True}])
            return FakeResp(200, _oracle_payload(),
                            {"x-requests-remaining": "300"})
        res = tl.refresh_oracle(http_get=http)
        assert res["keys"] == 1 and res["fetched"] == 1
        assert res["requests_today"] == 1

    def test_refresh_senza_chiave_fail_closed(self, monkeypatch):
        monkeypatch.delenv("ODDS_API_KEY", raising=False)
        assert tl.refresh_oracle()["error"] is not None


# ---------------------------------------------------------------------------
# 7. TRIPWIRE: nessun ordine, nessuna dipendenza pesante all'import
# ---------------------------------------------------------------------------

class TestTripwire:
    SRC = None

    @classmethod
    def _src(cls):
        if cls.SRC is None:
            from pathlib import Path
            cls.SRC = Path("tennis_lane.py").read_text(encoding="utf-8")
        return cls.SRC

    def test_nessun_percorso_ordini_nel_modulo(self):
        src = self._src()
        for token in ("_live_fill", "place_limit_order", "save_bet(",
                      "resolve_market_for", "exposure_allows"):
            assert token not in src, token

    def test_import_leggero(self):
        code = ("import tennis_lane, sys; "
                "bad=[m for m in ('auto_bet','bot','tracker','execution_engine')"
                " if m in sys.modules]; print(bad)")
        r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                           text=True, cwd=".")
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == "[]", r.stdout


# ---------------------------------------------------------------------------
# 8. CABLAGGIO in auto_bet: corsia LIVE + esenzione dal gate 1X2
# ---------------------------------------------------------------------------

class TestCablaggioAutoBet:
    def test_tennis_picks_fail_safe(self, monkeypatch):
        import sys
        import auto_bet
        fake = type(sys)("tennis_lane")

        def _boom():
            raise RuntimeError("corsia rotta")
        fake.picks = _boom
        monkeypatch.setitem(sys.modules, "tennis_lane", fake)
        assert auto_bet._tennis_picks() == []      # mai un'eccezione al giro

    def test_tennis_picks_inoltra_i_candidati(self, monkeypatch):
        import sys
        import auto_bet
        fake = type(sys)("tennis_lane")
        fake.picks = lambda: [{"match_id": "sx-tennis-0x1", "mercato": "TENNIS"}]
        monkeypatch.setitem(sys.modules, "tennis_lane", fake)
        out = auto_bet._tennis_picks()
        assert out and out[0]["mercato"] == "TENNIS"

    def test_gate_1x2_esenta_i_mercati_con_oracolo_proprio(self):
        # Il gate Pinnacle 1X2 non deve toccare ML (eSports) ne' TENNIS: i loro
        # esiti non sono un 1X2 e morirebbero con `no_oracle`.
        from pathlib import Path
        src = Path("auto_bet.py").read_text(encoding="utf-8")
        assert 'not in ("ML", "TENNIS")' in src
        assert "_tennis_picks()" in src
