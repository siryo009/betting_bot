"""Corsia FOOTBALL USA (NFL/NCAA, `gridiron_lane.py`) — TUTTI OFFLINE.

Nessuna rete, nessuna chiave reale, nessun ordine, nessuna scrittura sul
volume: provider SX finto (catalogo `/markets/active` + order book
`orderbook-v3/snapshot`), cache dell'oracolo scritta nella tmp del test,
ledger su SQLite temporaneo.

La corsia serve il torneo SX **Gridiron Gauntlet** (08/10 06:00 -> 20/10
06:00, return-based): il football USA non era visibile al sistema (la
discovery del calcio e' `sportIds=5`, il tennis 6, gli eSports 9, e
`market_shadow` legge lo sport 8 solo in telemetria). Qui si verifica il
percorso REALE: discovery type 226 (moneyline) -> oracolo Pinnacle a 2 esiti
(`regions=eu`) -> gate EV -> pick con `market_id`/`selection_id` SX.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import pinnacle_oracle as po
import gridiron_lane as gl
from execution_engine import SX_PROB_SCALE


# ---------------------------------------------------------------------------
# Helpers: SX (catalogo + book), the-odds-api (cache), HTTP finto
# ---------------------------------------------------------------------------

def _pct(price: float) -> str:
    return str(int(round(SX_PROB_SCALE / float(price))))


def _level(price: float, usdc: float) -> dict:
    return {"percentageOdds": _pct(price), "size": str(int(usdc * 1_000_000))}


class FakeSx:
    """Provider SX finto: discovery e order book serviti da dizionari."""

    name = "sxbet"

    def __init__(self, markets, books=None, *, fail=False):
        self.markets = markets
        self.books = books or {}
        self.fail = fail
        self.calls = []

    def _get(self, path, params=None):
        if self.fail:
            raise RuntimeError("SX giu'")
        if "markets/active" in str(path):
            self.calls.append(dict(params or {}))
            wanted = str((params or {}).get("type") or "")
            rows = [m for m in self.markets
                    if not wanted
                    or str(m.get("market_type_id")) == wanted]
            return {"data": {"markets": list(rows)}}
        mid = str((params or {}).get("marketHash"))
        if mid not in self.books:
            raise RuntimeError(f"book {mid} assente")
        return {"data": self.books[mid]}


def _market(mid, t1, t2, *, kickoff, league="NFL", mtype="226", line=None,
            o1=None, o2=None):
    return {
        "marketHash": mid, "sportXeventId": f"ev-{mid}",
        "teamOneName": t1, "teamTwoName": t2,
        "outcomeOneName": o1 if o1 is not None else t1,
        "outcomeTwoName": o2 if o2 is not None else t2,
        "gameTime": int(kickoff.timestamp()),
        "leagueLabel": league, "market_type_id": mtype, "line": line,
    }


def _book(price1=1.85, price2=2.20, *, usdc=50.0, empty=False):
    if empty:
        return {"outcomeOne": [], "outcomeTwo": []}
    return {"outcomeOne": [_level(price1, usdc)],
            "outcomeTwo": [_level(price2, usdc)]}


def _kickoff(hours: float = 6.0) -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=hours)


class FakeResp:
    def __init__(self, status_code, payload, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        return self._payload


HOME = "Dallas Cowboys"
AWAY = "Philadelphia Eagles"


def _oracle_payload(home=HOME, away=AWAY, *, p1=1.66, p2=2.34,
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


def _write_cache(payload, *, ts=None, name="toa_americanfootball_nfl.json"):
    folder = Path(__import__("os").environ["GRIDIRON_ORACLE_CACHE"])
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(json.dumps({
        "ts": (ts if ts is not None else __import__("time").time()),
        "payload": payload,
    }), encoding="utf-8")


@pytest.fixture(autouse=True)
def _corsia_accesa(monkeypatch):
    """La corsia e' SPENTA dal conftest: qui si accende, con cache in tmp."""
    monkeypatch.setenv("GRIDIRON_LANE", "1")
    po._CACHE_MEMO.clear()
    gl.reset_cache()
    yield
    po._CACHE_MEMO.clear()
    gl.reset_cache()


@pytest.fixture
def temp_db(monkeypatch, tmp_path):
    import tracker
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(tracker, "DB_PATH", db_path)
    yield db_path


# ---------------------------------------------------------------------------
# 1. CONFIGURAZIONE
# ---------------------------------------------------------------------------

class TestConfig:
    def test_interruttore(self, monkeypatch):
        monkeypatch.delenv("GRIDIRON_LANE", raising=False)
        assert gl.enabled() is True                    # assente = attiva
        monkeypatch.setenv("GRIDIRON_LANE", "0")
        assert gl.enabled() is False
        monkeypatch.setenv("GRIDIRON_LANE", "off")
        assert gl.enabled() is False

    def test_sport_id_football_usa(self):
        # 8 = football americano (5 calcio, 6 tennis, 9 eSports, 1 basket):
        # misurato con probe reali, non dedotto.
        assert gl.SX_SPORT_ID == "8"
        assert gl.DEFAULT_TYPES == ("226",)            # moneyline (no linea)

    def test_ev_min_dal_modulo_di_produzione(self):
        import value_filter as vf
        assert gl.ev_min() == pytest.approx(vf.EV_MIN)
        assert gl.ev_min() > 0

    def test_fascia_quota_fail_closed(self, monkeypatch):
        assert gl.in_odds_band(1.30) is True
        assert gl.in_odds_band(2.50) is True
        assert gl.in_odds_band(3.00) is True
        assert gl.in_odds_band(1.29) is False
        assert gl.in_odds_band(3.01) is False
        # fail-closed: non numerico o assente NON e' nella banda
        assert gl.in_odds_band(None) is False
        assert gl.in_odds_band("abc") is False
        assert gl.in_odds_band("") is False

    def test_stato_e_cache_isolati_nella_tmp(self):
        # Il conftest li sposta nella tmp: un test non deve mai scrivere sulla
        # cartella dati REALE (la produzione li leggerebbe come freschi).
        assert "tmp" in str(gl.state_path()) or "pytest" in str(gl.state_path())
        assert gl.oracle_cache_dir() != Path("data")

    def test_tipi_e_sport_da_env(self, monkeypatch):
        monkeypatch.setenv("GRIDIRON_TYPES", "226,342")
        assert gl._types() == ("226", "342")
        monkeypatch.setenv("GRIDIRON_TYPES", "")
        assert gl._types() == gl.DEFAULT_TYPES
        monkeypatch.setenv("GRIDIRON_SPORTS", "americanfootball_nfl")
        assert gl._sports() == ("americanfootball_nfl",)
        monkeypatch.delenv("GRIDIRON_SPORTS")
        assert gl._sports() == gl.DEFAULT_SPORTS


# ---------------------------------------------------------------------------
# 2. DISCOVERY (sportId 8, type 226)
# ---------------------------------------------------------------------------

class TestDiscovery:
    def test_discovery_moneyline(self):
        ko = _kickoff(6)
        m = _market("0xAAA", HOME, AWAY, kickoff=ko)
        prov = FakeSx([m], {"0xAAA": _book(1.85, 2.20)})
        events = gl.discover(provider=prov)
        assert len(events) == 1
        ev = events[0]
        assert ev["market_id"] == "0xAAA"
        assert ev["event_id"] == "ev-0xAAA"
        assert ev["market_type"] == "ML_OT"
        assert ev["sx_type"] == "226"
        assert ev["line"] is None
        assert ev["home"] == HOME and ev["away"] == AWAY
        assert len(ev["sides"]) == 2
        assert {s["key"] for s in ev["sides"]} == {"1", "2"}
        # e' CHIAMATA con sportIds=8 (il football USA, non il calcio)
        assert prov.calls and prov.calls[0]["sportIds"] == "8"
        assert prov.calls[0]["type"] == "226"

    def test_fuori_finestra_scartato(self):
        # partita fra 10 giorni (oltre la finestra HOURS_AHEAD, 168h)
        m = _market("0xBBB", HOME, AWAY, kickoff=_kickoff(240))
        prov = FakeSx([m], {"0xBBB": _book()})
        assert gl.discover(provider=prov) == []

    def test_book_sottile_scartato(self):
        m = _market("0xCCC", HOME, AWAY, kickoff=_kickoff(6))
        # un solo lato con profondita' >= soglia -> meno di due lati
        prov = FakeSx([m], {"0xCCC": {"outcomeOne": [_level(1.85, 50.0)],
                                      "outcomeTwo": []}})
        assert gl.discover(provider=prov) == []

    def test_book_sporco_inv_sum(self):
        m = _market("0xDDD", HOME, AWAY, kickoff=_kickoff(6))
        # 1/1.05 + 1/1.05 = 1.905 -> fuori banda
        prov = FakeSx([m], {"0xDDD": _book(1.05, 1.05)})
        assert gl.discover(provider=prov) == []

    def test_provider_rotto_fail_safe(self):
        prov = FakeSx([], fail=True)
        assert gl.discover(provider=prov) == []

    def test_memo_discovery(self, monkeypatch):
        # Con provider iniettato NON si memoizza; con provider None la memo
        # evita centinaia di richieste/ora (il giro gira ogni 60s).
        ko = _kickoff(6)
        m = _market("0xEEE", HOME, AWAY, kickoff=ko)
        gl.reset_cache()
        monkeypatch.setattr(gl, "_DISCOVERY_MEMO",
                            {"key": None, "ts": 0.0, "rows": []})
        # memoizza a mano
        gl._DISCOVERY_MEMO["key"] = (f"{gl.SX_SPORT_ID}|226|"
                                     f"{gl.HOURS_AHEAD}|{gl.MAX_MARKETS}")
        gl._DISCOVERY_MEMO["ts"] = __import__("time").time()
        gl._DISCOVERY_MEMO["rows"] = [{"market_id": "memo"}]
        rows = gl.discover(provider=None)
        assert rows and rows[0]["market_id"] == "memo"


# ---------------------------------------------------------------------------
# 3. ETICHETTE DI LEDGER (_side_labels)
# ---------------------------------------------------------------------------

class TestSideLabels:
    def test_moneyline(self):
        assert gl._side_labels("226", None, HOME) == ("1", "2")

    def test_spread(self):
        labels = gl._side_labels("342", 2.5, HOME)
        assert labels == ("Home +2.5", "Away -2.5")

    def test_totale_rispetta_il_lato(self):
        assert gl._side_labels("28", 49.5, "Over 49.5") == \
            ("Over 49.5", "Under 49.5")
        assert gl._side_labels("28", 49.5, "Under 49.5") == \
            ("Under 49.5", "Over 49.5")

    def test_linea_mancante_su_tipi_a_linea(self):
        assert gl._side_labels("342", None, HOME) is None
        assert gl._side_labels("28", None, "Over 49.5") is None

    def test_tipo_sconosciuto(self):
        assert gl._side_labels("999", None, HOME) is None


# ---------------------------------------------------------------------------
# 4. PICKS (oracolo a 2 esiti + gate EV)
# ---------------------------------------------------------------------------

class TestPicks:
    def test_oracolo_assente_fail_closed(self):
        ko = _kickoff(6)
        m = _market("0x111", HOME, AWAY, kickoff=ko)
        prov = FakeSx([m], {"0x111": _book(1.85, 2.20)})
        # nessuna cache -> load_oracle None -> nessun pick
        assert gl.picks(provider=prov) == []

    def test_pick_con_ev_positivo(self):
        _write_cache(_oracle_payload(p1=1.66, p2=2.34))
        ko = _kickoff(6)
        m = _market("0x222", HOME, AWAY, kickoff=ko)
        # quota SX 1.85 sopra la quota equa del favorito -> EV positivo
        prov = FakeSx([m], {"0x222": _book(1.85, 2.20)})
        picks = gl.picks(provider=prov)
        assert picks, "atteso almeno un pick +EV"
        p = picks[0]
        assert p["match_id"] == "sx-ev-0x222"
        assert p["mercato"] == "1X2"
        assert p["esito_key"] == "1"
        assert p["selection_id"] == 1
        assert p["market_id"] == "0x222"
        assert p[gl.PICK_FLAG] is True
        assert p["best_ev"] > 0.025
        assert p["status"] in ("value", "strong_value", "moderate")
        assert gl.in_odds_band(p["quota"])

    def test_quota_fuori_fascia_scartata(self, monkeypatch):
        _write_cache(_oracle_payload(p1=1.66, p2=2.34))
        ko = _kickoff(6)
        m = _market("0x333", HOME, AWAY, kickoff=ko)
        # SX quota 4.0 (favorevole all'oracolo) ma FUORI fascia 1.30-3.00
        prov = FakeSx([m], {"0x333": _book(4.0, 2.20)})
        assert gl.picks(provider=prov) == []

    def test_solo_moneyline(self, monkeypatch):
        # Con i tipi a LINEA abilitati, `picks` deve comunque valutare solo il
        # moneyline (l'oracolo a 2 esiti non copre uno spread).
        monkeypatch.setenv("GRIDIRON_TYPES", "226,342")
        _write_cache(_oracle_payload())
        ko = _kickoff(6)
        m = _market("0x444", HOME, AWAY, kickoff=ko, mtype="342", line=2.5,
                    o1=f"{HOME} +2.5", o2=f"{AWAY} -2.5")
        prov = FakeSx([m], {"0x444": _book(1.85, 2.20)})
        assert gl.picks(provider=prov) == []

    def test_corsia_spenta(self, monkeypatch):
        monkeypatch.setenv("GRIDIRON_LANE", "0")
        assert gl.picks(provider=FakeSx([])) == []
        assert gl.discover(provider=FakeSx([])) == []


# ---------------------------------------------------------------------------
# 5. SCAN (telemetria nel ledger)
# ---------------------------------------------------------------------------

class TestScan:
    def test_scan_registra_telemetria(self, temp_db):
        _write_cache(_oracle_payload())
        ko = _kickoff(6)
        m = _market("0x555", HOME, AWAY, kickoff=ko)
        prov = FakeSx([m], {"0x555": _book(1.85, 2.20)})
        res = gl.scan(provider=prov, register=True)
        assert res["enabled"] is True
        assert res["events"] == 1
        assert res["candidates"] >= 1
        assert res["registered"] >= 1
        import tracker
        rows = tracker.get_predictions()
        assert any(r.get("match_id") == "sx-ev-0x555" for r in rows)
        assert all(r.get("mercato") == "1X2"
                   for r in rows if str(r.get("match_id")).startswith("sx-ev-"))

    def test_scan_no_register(self, temp_db):
        _write_cache(_oracle_payload())
        ko = _kickoff(6)
        m = _market("0x666", HOME, AWAY, kickoff=ko)
        prov = FakeSx([m], {"0x666": _book(1.85, 2.20)})
        res = gl.scan(provider=prov, register=False)
        assert res["registered"] == 0
        import tracker
        assert not [r for r in tracker.get_predictions()
                    if r.get("match_id") == "sx-ev-0x666"]

    def test_scan_spento(self, monkeypatch):
        monkeypatch.setenv("GRIDIRON_LANE", "0")
        res = gl.scan(provider=FakeSx([]))
        assert res["enabled"] is False and res["candidates"] == 0


# ---------------------------------------------------------------------------
# 6. REFRESH ORACOLO (regions=eu, budget, hard-stop)
# ---------------------------------------------------------------------------

class TestRefreshOracle:
    def test_regioni_eu_nel_request(self, monkeypatch):
        seen = []

        def fake_get(url, params=None, timeout=None):
            seen.append(dict(params or {}))
            return FakeResp(200, _oracle_payload(),
                            {"x-requests-remaining": "400"})

        monkeypatch.setenv("ODDS_API_KEY", "fake-key")
        res = gl.refresh_oracle(http_get=fake_get)
        assert res["fetched"] == len(gl.DEFAULT_SPORTS)
        # regions=eu: misurato come l'UNICA regione dove Pinnacle pubblica il
        # football USA (con regions=us Pinnacle ha 0 eventi).
        assert all(p.get("regions") == "eu" for p in seen)
        assert all(p.get("markets") == "h2h" for p in seen)

    def test_cache_fresca_non_spende(self, monkeypatch):
        _write_cache(_oracle_payload())              # nfl (fresca)
        _write_cache(_oracle_payload(), name="toa_americanfootball_ncaaf.json")
        called = []

        def fake_get(url, params=None, timeout=None):
            called.append(url)
            return FakeResp(200, [], {})

        monkeypatch.setenv("ODDS_API_KEY", "fake-key")
        res = gl.refresh_oracle(http_get=fake_get)
        assert res["fetched"] == 0
        assert res["skipped"] == len(gl.DEFAULT_SPORTS)
        assert not called

    def test_budget_giornaliero(self, monkeypatch, tmp_path):
        # portiamo il budget a 1 e la cache a 1 sport: la seconda lega non
        # viene scaricata (budget esaurito) e i contatori lo dichiarano.
        # il budget e' letto all'IMPORT (come in produzione: si tara da env e
        # richiede il redeploy): nel test si forza la costante
        monkeypatch.setattr(gl, "REQ_BUDGET_DAY", 1)
        monkeypatch.setenv("GRIDIRON_SPORTS", "americanfootball_nfl,"
                                              "americanfootball_ncaaf")
        monkeypatch.setenv("ODDS_API_KEY", "fake-key")
        calls = []

        def fake_get(url, params=None, timeout=None):
            calls.append(url)
            return FakeResp(200, _oracle_payload(), {})

        res = gl.refresh_oracle(http_get=fake_get)
        assert res["fetched"] == 1
        assert len(calls) == 1

    def test_hard_stop_crediti_blocca(self, monkeypatch, tmp_path):
        import odds_api
        monkeypatch.setenv("ODDS_API_KEY", "fake-key")
        monkeypatch.setattr(odds_api, "credits_hard_stopped", lambda: True)
        calls = []

        def fake_get(url, params=None, timeout=None):
            calls.append(url)
            return FakeResp(200, _oracle_payload(), {})

        res = gl.refresh_oracle(http_get=fake_get)
        assert res["fetched"] == 0
        assert not calls

    def test_senza_chiave_nessuna_chiamata(self, monkeypatch):
        monkeypatch.delenv("ODDS_API_KEY", raising=False)
        calls = []

        def fake_get(url, params=None, timeout=None):
            calls.append(url)
            return FakeResp(200, _oracle_payload(), {})

        res = gl.refresh_oracle(http_get=fake_get)
        assert res["fetched"] == 0
        assert "ODDS_API_KEY" in (res["error"] or "")
        assert not calls

    def test_http_errore_fail_safe(self, monkeypatch):
        monkeypatch.setenv("ODDS_API_KEY", "fake-key")

        def fake_get(url, params=None, timeout=None):
            return FakeResp(500, {}, {})

        res = gl.refresh_oracle(http_get=fake_get)
        assert res["fetched"] == 0
        assert res["error"]


# ---------------------------------------------------------------------------
# 7. SUMMARY / REPORT
# ---------------------------------------------------------------------------

class TestReport:
    def test_summary_e_report(self):
        s = gl.summary()
        assert s["enabled"] is True
        assert s["sx_sport_id"] == "8"
        assert "226" in s["types"]
        text = gl.format_report(s)
        assert "FOOTBALL USA" in text
        assert "sportId 8" in text

    def test_report_spento(self, monkeypatch):
        monkeypatch.setenv("GRIDIRON_LANE", "0")
        assert "SPENTA" in gl.format_report(gl.summary())


# ---------------------------------------------------------------------------
# 8. TRIPWIRE
# ---------------------------------------------------------------------------

def _code_only(path: str) -> str:
    """Sorgente senza docstring/commenti: i tripwire giudicano il CODICE.

    I commenti che SPIEGANO la direttiva (es. "il flag esenta `_live_fill`")
    nominano i simboli per prosa: senza questo filtro il tripwire boccia la
    documentazione invece del percorso d'ordine (lezione del 30/09).
    """
    import ast
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body:
            first = body[0]
            if (isinstance(first, ast.Expr)
                    and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                first.value.value = ""
    return ast.unparse(tree)


class TestTripwire:
    def test_nessun_percorso_ordine_nel_sorgente(self):
        src = _code_only(gl.__file__)
        for bad in ("place_limit_order", "_live_fill", "resolve_market_for",
                    "OrderResult", "sign_order"):
            assert bad not in src, f"percorso ordine nel modulo: {bad}"

    def test_nessuna_formula_copiata(self):
        src = _code_only(gl.__file__)
        # la formula EV e il de-vig vivono UNA volta sola (pinnacle_oracle):
        # qui si DELEGA, non si riscrive l'aritmetica.
        assert "p * (price - 1.0)" not in src
        assert "def true_probabilities" not in src
        assert "def devig" not in src

    def test_import_leggero_in_subprocess(self):
        code = (
            "import sys, gridiron_lane;"
            "bad = [m for m in ('tracker','auto_bet','bot','odds_api')"
            " if m in sys.modules];"
            "print(','.join(bad))"
        )
        out = subprocess.run([sys.executable, "-c", code],
                             capture_output=True, text=True, cwd=str(Path.cwd()))
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == ""

    def test_env_dichiarate_in_iac(self):
        iac = Path(".railway/railway.ts").read_text(encoding="utf-8")
        for name in ("GRIDIRON_LANE", "GRIDIRON_SX_SPORT_ID",
                     "GRIDIRON_TYPES", "GRIDIRON_SPORTS",
                     "GRIDIRON_HOURS_AHEAD", "GRIDIRON_MAX_MARKETS",
                     "GRIDIRON_EV_MIN", "GRIDIRON_ODDS_MIN",
                     "GRIDIRON_ODDS_MAX", "GRIDIRON_MIN_INV_SUM",
                     "GRIDIRON_MAX_INV_SUM", "GRIDIRON_ORACLE_TTL_MIN",
                     "GRIDIRON_REQ_BUDGET_DAY", "GRIDIRON_ORACLE_CACHE",
                     "GRIDIRON_LANE_STATE", "GRIDIRON_DISCOVERY_TTL_S",
                     "GRIDIRON_JOB_INTERVAL_MIN", "GRIDIRON_ODDS_BASE"):
            assert name in iac, f"env non dichiarata nella IaC: {name}"

    def test_flag_allineato_con_auto_bet(self):
        import auto_bet
        assert auto_bet.GRIDIRON_FLAG == gl.PICK_FLAG
