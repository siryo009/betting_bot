"""Test intel live (live_intel.py + DataAgent) — TUTTI OFFLINE.

Nessuna rete, nessun credito, nessun ordine: i provider sono finti (funzioni
iniettate o moduli finti in sys.modules), il DB e' temporaneo, la cache vive
nella tmp del test.
"""

from __future__ import annotations

import ast
import json
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path

import pytest

import live_intel
from live_intel import (INTEL_ERRORS, MatchIntel, NewsItem, ProviderStatus,
                        TeamStats, _injuries_query, _sd_league,
                        assemble_match_intel, collect_news, mlb_probable_pitchers,
                        nba_team_stats, reset_cache_dir, soccer_team_stats)


@pytest.fixture(autouse=True)
def intel_isolation(tmp_path, monkeypatch):
    """Ogni test ha la SUA cache e errori puliti (il modulo porta lo stato)."""
    reset_cache_dir(tmp_path / "intel")
    monkeypatch.setenv("LIVE_INTEL", "1")
    INTEL_ERRORS.clear()
    yield
    INTEL_ERRORS.clear()
    # `None` RIPRISTINA il default (env `LIVE_INTEL_CACHE` / DATA_DIR/intel).
    # Passare `live_intel._CACHE_DIR` lasciava il modulo puntato alla cartella
    # temporanea ormai CANCELLATA per il resto del processo: i test successivi
    # non trovavano mai la cache e ripetevano le letture vere (lezione 29/09).
    reset_cache_dir(None)


# ---------------------------------------------------------------------------
# Contratti
# ---------------------------------------------------------------------------

class TestContratti:
    def test_match_intel_serializzabile(self):
        intel = MatchIntel(
            match_id="m1", home="Inter", away="Milan", league="Serie A",
            home_stats=TeamStats(provider="fbref", xg_for=1.9, matches=5),
            injuries_news=[NewsItem(title="Russel AFC infortunato", url="https://x.y")],
            providers=[ProviderStatus(provider="soccerdata", ok=True, detail="ok")],
        )
        blob = intel.as_json()
        assert blob["home_stats"]["xg_for"] == 1.9
        assert blob["partial"] is False
        assert json.loads(json.dumps(blob)) == blob  # JSON-safe

    def test_partial_dichiara_il_degrado(self):
        intel = MatchIntel(providers=[
            ProviderStatus(provider="ddgs", ok=False, detail="error: connection reset"),
            ProviderStatus(provider="soccerdata", ok=True, detail="ok"),
        ])
        assert intel.partial is True
        assert intel.errors == 1


# ---------------------------------------------------------------------------
# Mappa leghe / query news (tabelle esplicitive, mai fuzzy)
# ---------------------------------------------------------------------------

class TestMappaLeghe:
    @pytest.mark.parametrize("league,expected", [
        ("Premier League", "ENG-Premier League"),
        ("EFL Championship", "ENG-Championship"),
        ("Serie A", "ITA-Serie A"),
        ("Italy Serie A", "ITA-Serie A"),
        ("La Liga", "ESP-La Liga"),
        ("Bundesliga", "GER-Bundesliga"),
        ("MLS", "USA-Major League Soccer"),
    ])
    def test_leghe_coperte(self, league, expected):
        assert _sd_league(league) == expected

    @pytest.mark.parametrize("league", [
        "Africa Cup of Nations", "UEFA Nations League", "Primera A",
        "Primera Nacional", "K2-League", "",
    ])
    def test_leghe_non_coperte_tornano_none(self, league):
        assert _sd_league(league) is None

    def test_lega_senza_chiave_non_importa_soccerdata(self, monkeypatch):
        """Fuori copertura: `None` PRIMA di toccare la libreria."""
        import types
        monkeypatch.setitem(sys.modules, "soccerdata", None)  # import -> ImportError
        assert soccer_team_stats("Inter", "Africa Cup of Nations") is None
        assert "fbref" not in INTEL_ERRORS


class TestQueryNews:
    def test_query_italiana_per_serie_a(self):
        q = _injuries_query("Inter", "Milan", "Serie A")
        assert "infortuni" in q and "Inter" in q and "Milan" in q

    def test_query_inglese_altrove(self):
        q = _injuries_query("Arsenal", "Chelsea", "Premier League")
        assert "injury" in q


# ---------------------------------------------------------------------------
# Provider con moduli finti (offline)
# ---------------------------------------------------------------------------

class TestCollectNews:
    def test_ok(self, monkeypatch):
        class _FakeDDGS:
            def news(self, query, max_results=5):
                return [{"title": "Star out for derby", "url": "https://n/1",
                         "source": "Gazzetta", "date": "2026-09-29"}]

        fake = types.SimpleNamespace(DDGS=_FakeDDGS)
        monkeypatch.setitem(sys.modules, "ddgs", fake)
        items = collect_news("Inter vs Milan infortuni")
        assert len(items) == 1 and items[0].title == "Star out for derby"

    def test_bloccato_restituisce_vuoto_e_conta(self, monkeypatch):
        class _FakeDDGS:
            def news(self, query, max_results=5):
                raise ConnectionError("connection reset by peer")

        monkeypatch.setitem(sys.modules, "ddgs",
                            types.SimpleNamespace(DDGS=_FakeDDGS))
        assert collect_news("qualcosa") == []
        assert "ddgs" in INTEL_ERRORS

    def test_cache_evita_la_seconda_chiamata(self, monkeypatch):
        calls = []

        class _FakeDDGS:
            def news(self, query, max_results=5):
                calls.append(query)
                return [{"title": "t1", "url": "u"}]

        monkeypatch.setitem(sys.modules, "ddgs",
                            types.SimpleNamespace(DDGS=_FakeDDGS))
        collect_news("q-cache")
        collect_news("q-cache")
        assert len(calls) == 1  # la seconda lettura e' cache


class TestMlb:
    def _fake_schedule(self, games):
        import requests

        class _Resp:
            def raise_for_status(self): ...
            def json(self):
                return {"dates": [{"games": games}]}

        return _Resp()

    def test_probabili_dal_giorno(self, monkeypatch):
        games = [{
            "teams": {
                "away": {"team": {"id": 142, "name": "Minnesota Twins"},
                         "probablePitcher": {"fullName": "Joe Ryan"}},
                "home": {"team": {"id": 147, "name": "New York Yankees"},
                         "probablePitcher": {"fullName": "Gerrit Cole"}},
            },
        }]
        import requests
        monkeypatch.setattr(requests, "get",
                            lambda *a, **k: self._fake_schedule(games))
        out = mlb_probable_pitchers("Yankees", "Twins")
        assert out == {"Yankees": "Gerrit Cole", "Twins": "Joe Ryan"}

    def test_fuori_giorno_vuoto_non_errore_di_rete(self, monkeypatch):
        import requests
        monkeypatch.setattr(requests, "get",
                            lambda *a, **k: self._fake_schedule([]))
        assert mlb_probable_pitchers("Yankees", "Red Sox") == {}


class TestNba:
    def test_squadra_non_nba_non_importa_la_libreria(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "nba_api", None)
        assert nba_team_stats("Inter") is None

    def test_statistiche_da_endpoint_finto(self, monkeypatch):
        import pandas as pd

        df = pd.DataFrame([{
            "TEAM_ABBREVIATION": "BOS", "PTS": 120.4, "OPP_PTS": 108.2,
            "GP": 10, "W_PCT": 0.8,
        }])

        class _FakeEndpoint:
            def __init__(self, **kw): ...
            def get_data_frames(self):
                return [df]

        fake_pkg = types.ModuleType("nba_api")
        fake_stats = types.ModuleType("nba_api.stats")
        fake_eps = types.ModuleType("nba_api.stats.endpoints")
        fake_eps.LeagueDashTeamStats = _FakeEndpoint
        # `from nba_api.stats.endpoints import leaguedashteamstats` importa il
        # SOTTOMODULO (come nell'api reale): il fake riproduce la struttura.
        fake_ls = types.ModuleType("nba_api.stats.endpoints.leaguedashteamstats")
        fake_ls.LeagueDashTeamStats = _FakeEndpoint
        fake_eps.leaguedashteamstats = fake_ls
        fake_pkg.stats = fake_stats
        fake_stats.endpoints = fake_eps
        monkeypatch.setitem(sys.modules, "nba_api", fake_pkg)
        monkeypatch.setitem(sys.modules, "nba_api.stats", fake_stats)
        monkeypatch.setitem(sys.modules, "nba_api.stats.endpoints", fake_eps)

        stats = nba_team_stats("boston celtics")
        assert stats is not None and stats.provider == "nba_api"
        assert stats.goals_for == 120.4 and stats.matches == 10


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

class TestCache:
    def test_scrittura_e_lettura(self, tmp_path):
        live_intel._cache_write("fbref", "fbref|X|Inter",
                                {"provider": "fbref", "xg_for": 1.5})
        blob = live_intel._cache_read("fbref", "fbref|X|Inter")
        assert blob["data"]["xg_for"] == 1.5

    def test_expired_scaduto(self, tmp_path):
        live_intel._cache_write("fbref", "k-exp", {"a": 1})
        path = next((tmp_path / "intel").glob("fbref_*.json"))
        blob = json.loads(path.read_text())
        blob["ts"] = time.time() - 25 * 3600  # 25h fa: oltre ogni TTL
        path.write_text(json.dumps(blob))
        assert live_intel._cache_read("fbref", "k-exp") is None

    def test_file_corrotto_non_rompe(self, tmp_path):
        (tmp_path / "intel").mkdir(exist_ok=True)
        (tmp_path / "intel" / "fbref_bad.json").write_text("{rotto")
        assert live_intel._cache_read("fbref", "bad") is None


# ---------------------------------------------------------------------------
# Guardie di rete (29/09/2026): la suite non deve poter bloccare se stessa
# ---------------------------------------------------------------------------

class TestGuardieDiRete:
    def test_conftest_spegne_l_intel_per_tutti_i_test(self):
        """Il default della SUITE e' intel SPENTA, e la cache sta nella tmp.

        Senza queste due righe in `conftest.py` qualunque file che costruisce
        un `DataAgent()` su un ledger con segnali fa partire lo scraping FBref
        (rete, senza timeout): e' esattamente cio' che ha bloccato la
        regressione il 29/09/2026.
        """
        src = Path("conftest.py").read_text(encoding="utf-8")
        assert 'setenv("LIVE_INTEL", "0")' in src
        assert 'setenv("LIVE_INTEL_CACHE"' in src

    def test_i_provider_girano_sotto_la_guardia_di_timeout(self):
        """Prova FUNZIONALE: durante la chiamata al provider il timeout e' attivo."""
        visto = {}

        def _stats(team, league):
            visto["timeout"] = socket.getdefaulttimeout()
            return None

        assemble_match_intel(
            {"match_id": "m9", "home": "Inter", "away": "Milan",
             "league": "Serie A"},
            stats_fn=_stats, elo_fn=lambda t: None, news_fetch=lambda q: [])
        assert visto["timeout"] == live_intel.DEFAULT_TIMEOUT_S

    def test_timeout_ripristinato_dopo_la_chiamata(self):
        """Nessun effetto residuo sul processo (che ospita lo scheduler del bot)."""
        prima = socket.getdefaulttimeout()
        with live_intel._network_deadline():
            assert socket.getdefaulttimeout() == live_intel.DEFAULT_TIMEOUT_S
        assert socket.getdefaulttimeout() == prima

    def test_timeout_da_env_e_valori_impossibili(self, monkeypatch):
        monkeypatch.setenv("LIVE_INTEL_TIMEOUT_S", "3.5")
        assert live_intel._timeout_seconds() == 3.5
        # Una guardia di sicurezza non si spegne con un env sbagliato.
        for invalido in ("", "abc", "0", "-4"):
            monkeypatch.setenv("LIVE_INTEL_TIMEOUT_S", invalido)
            assert live_intel._timeout_seconds() == live_intel.DEFAULT_TIMEOUT_S


# ---------------------------------------------------------------------------
# Assembler (provider iniettati: zero rete)
# ---------------------------------------------------------------------------

class TestAssembler:
    def test_soccer_completo(self):
        intel = assemble_match_intel(
            {"match_id": "m1", "home": "Inter", "away": "Milan",
             "league": "Serie A"},
            stats_fn=lambda t, l: TeamStats(provider="fbref", xg_for=1.8,
                                            xg_against=0.9, matches=6),
            elo_fn=lambda t: 1820.0,
            news_fetch=lambda q: [NewsItem(title="nessuno infortunato")],
            mlb_fn=lambda h, a: (_ for _ in ()).throw(AssertionError("MLB")),
            nba_fn=lambda t: (_ for _ in ()).throw(AssertionError("NBA")),
        )
        assert intel.home_stats.xg_for == 1.8
        assert intel.home_stats.elo == 1820.0
        assert intel.away_stats.elo == 1820.0
        assert len(intel.injuries_news) == 1
        assert intel.errors == 0

    def test_provider_rotto_non_nega_gli_altri(self):
        def _boom(*a, **k):
            raise RuntimeError("fonte giu'")

        def _stats(team, league):
            if team == "Inter":
                return TeamStats(provider="fbref", xg_for=1.8)
            return None  # Milan non coperto -> fallback ELO

        intel = assemble_match_intel(
            {"match_id": "m2", "home": "Inter", "away": "Milan",
             "league": "Serie A"},
            stats_fn=_stats,
            elo_fn=lambda t: 1750.0,
            news_fetch=_boom,
        )
        assert intel.errors == 1                      # solo ddgs
        assert intel.home_stats.xg_for == 1.8         # stats lavora
        assert intel.away_stats.elo == 1750.0         # fallback ELO lavora
        assert intel.partial is True
        failed = [p for p in intel.providers if p.detail.startswith("error:")]
        assert [p.provider for p in failed] == ["ddgs"]

    def test_stats_rotto_sutta_entrambe_le_squadre_dichiara_duo_errori(self):
        def _boom(*a, **k):
            raise RuntimeError("scraping giu'")

        intel = assemble_match_intel(
            {"match_id": "m2b", "home": "Inter", "away": "Milan",
             "league": "Serie A"},
            stats_fn=_boom,
            elo_fn=lambda t: 1750.0,
            news_fetch=lambda q: [],
        )
        # stats fallisce per ENTRAMBE le squadre (2 provider in errore,
        # dichiarati): l'ELO non viene raggiunto perche' il provider stats
        # non ha risposto "non coperto" ma e' CRASHATO — degrado onesto.
        assert intel.errors == 2
        assert intel.home_stats is None and intel.away_stats is None

    def test_mlb_solo_lanciatori(self):
        intel = assemble_match_intel(
            {"match_id": "m3", "home": "Yankees", "away": "Twins",
             "league": "MLB"},
            mlb_fn=lambda h, a: {"Yankees": "Cole", "Twins": "Ryan"},
        )
        assert intel.probable_pitchers["Yankees"] == "Cole"
        # soccerdata/ddgs non applicabili al baseball: dichiarati, non errori
        kinds = {p.provider: p.detail for p in intel.providers}
        assert all(v != "error: " for v in kinds.values())
        assert intel.errors == 0

    def test_nba_stats_squadre(self):
        intel = assemble_match_intel(
            {"match_id": "m4", "home": "Boston Celtics",
             "away": "Los Angeles Lakers", "league": "NBA"},
            nba_fn=lambda t: TeamStats(provider="nba_api", goals_for=118.0),
        )
        assert intel.home_stats.goals_for == 118.0
        assert intel.away_stats is not None


# ---------------------------------------------------------------------------
# Tripwire: il modulo NON ordina, NON carica librerie pesanti all'import
# ---------------------------------------------------------------------------

class TestTripwire:
    def test_nessun_import_di_produzione_nel_sorgente(self):
        tree = ast.parse(Path(live_intel.__file__).read_text(encoding="utf-8"))
        banned = {"execution_engine", "auto_bet", "bot", "tracker",
                  "sx_signals", "multi_market", "poisson_engine"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(a.name.split(".")[0] not in banned for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] not in banned

    def test_import_leggero_in_subprocesso(self):
        code = ("import sys, live_intel; "
                "bad = [m for m in ('soccerdata','nba_api','pybaseball','ddgs',"
                "'tracker','auto_bet','execution_engine') if m in sys.modules]; "
                "print('HEAVY:' + ','.join(bad))")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                             text=True, cwd=str(Path(live_intel.__file__).parent))
        assert "HEAVY:" in out.stdout
        assert out.stdout.split("HEAVY:")[1].strip() == ""

    def test_nessuna_scelta_di_strategia_nel_sorgente(self):
        src = Path(live_intel.__file__).read_text(encoding="utf-8")
        body = src.split('"""', 2)[2]  # fuori dal docstring di modulo
        for token in ("EV_MIN", "MARKET_EDGE", "ODDS_MAX", "STAKE_CAP",
                      "place_order"):
            assert token not in body


class TestIaC:
    """Le variabili e le dipendenze vivono anche nella configurazione.

    `preserve()` non crea valori: le variabili assenti restano assenti e
    valgono i default di codice. Senza la dichiarazione, pero', un
    `railway config apply` DISTRUGGE cio' che l'operatore ha impostato —
    stessa regola e stesso tripwire degli altri moduli (adaptive weighting,
    smart hedging, recinto di capitale).
    """

    ENV = ("LIVE_INTEL", "LIVE_INTEL_CACHE", "LIVE_INTEL_TIMEOUT_S",
           "LIVE_INTEL_TTL_FBREF", "LIVE_INTEL_TTL_ELO", "LIVE_INTEL_TTL_NEWS",
           "LIVE_INTEL_TTL_MLB", "LIVE_INTEL_TTL_NBA")

    def test_env_dichiarate_nella_iac(self):
        src = Path(".railway/railway.ts").read_text(encoding="utf-8")
        for env in self.ENV:
            assert f"{env}: preserve()" in src, env

    def test_ogni_env_letta_e_dichiarata(self):
        """Nessuna variabile letta dal modulo puo' restare fuori dalla IaC.

        Il prefisso `LIVE_INTEL_TTL_` e' dinamico (`_ttl` lo compone col nome
        del provider): si enumerano i provider realmente usati dalla cache.
        """
        src = Path(live_intel.__file__).read_text(encoding="utf-8")
        letti = set(re.findall(r'getenv\(\s*"(LIVE_INTEL[A-Z0-9_]*)"', src))
        # I TTL per provider sono composti a runtime: si ricavano dalle
        # chiamate `_cache_*(<kind>, ...)`, i provider effettivamente usati.
        kinds = set(re.findall(r'_cache_(?:read|write)\(\s*"([a-z]+)"', src))
        letti |= {f"LIVE_INTEL_TTL_{k.upper()}" for k in kinds}
        assert letti, "nessuna env letta: il test non sta misurando nulla"
        dichiarati = set(self.ENV)
        assert letti <= dichiarati, f"env non dichiarate: {sorted(letti - dichiarati)}"

    def test_dipendenze_dichiarate_nei_requirements(self):
        """Le librerie dei provider sono PIGRE: se non sono dichiarate, in
        produzione l'adapter resta permanentemente "offline" senza che nulla
        lo dica (il fail-safe le rende indistinguibili da una fonte giu')."""
        req = Path("requirements.txt").read_text(encoding="utf-8").lower()
        for pkg in ("soccerdata", "nba_api", "pybaseball", "ddgs"):
            assert pkg in req, pkg


# ---------------------------------------------------------------------------
# Integrazione DataAgent
# ---------------------------------------------------------------------------

@pytest.fixture()
def temp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "intel.db"
        monkeypatch.setattr(tracker, "DB_PATH", db_path)
        tracker.init_db()
        yield db_path


import tracker  # noqa: E402  (dopo il monkeypatch di fixture: import pigro nel file)


def _seed_match_and_signal(db_path: Path, match_id="sx-L1",
                           league="Serie A", home="Inter", away="Milan"):
    conn = sqlite3.connect(db_path)
    tracker.save_match(match_id, league, home, away,
                       "2026-09-29T20:45:00Z")
    tracker.save_prediction(match_id=match_id, mercato="1X2", esito=home,
                            quota=1.65, prob=0.60, ev=0.05,
                            market_prob=0.55, market_edge=0.05,
                            status="strong_value", league=league)
    conn.close()


class TestDataAgentIntel:
    def test_intel_su_match_in_finestra(self, temp_db):
        from agents.data_agent import DataAgent

        _seed_match_and_signal(temp_db)
        calls = []

        def _fake_intel(row):
            calls.append(row["home"])
            return MatchIntel(match_id=row["match_id"], home=row["home"],
                              away=row["away"], league=row["league"],
                              home_stats=TeamStats(provider="fbref",
                                                   xg_for=2.1))

        out = DataAgent(intel_fn=_fake_intel).process(conn=None)
        assert calls == ["Inter"]
        assert len(out.intel) == 1
        assert out.intel[0]["home_stats"]["xg_for"] == 2.1
        assert out.validated is True  # l'intel NON tocca il gate

    def test_una_sola_intel_per_match_con_piu_segnali(self, temp_db):
        from agents.data_agent import DataAgent

        _seed_match_and_signal(temp_db)
        conn = sqlite3.connect(temp_db)
        tracker.save_prediction(match_id="sx-L1", mercato="AH",
                                esito="Home +1.5", quota=1.5, prob=0.66,
                                ev=0.08, market_prob=0.58, market_edge=0.08,
                                status="value", league="Serie A")
        conn.close()
        calls = []
        out = DataAgent(
            intel_fn=lambda row: calls.append(row) or
            MatchIntel(match_id=row["match_id"]),
        ).process()
        assert len(calls) == 1

    def test_match_senza_riga_nessuna_intel_mai_inventata(self, temp_db, monkeypatch):
        """Senza nomi reali (riga `matches` non leggibile) NESSUNA intel: l'intel
        e' costruita su nomi, mai su match_id opachi. (L'adapter segnali fa la
        JOIN con `matches`: una riga match assente elimina il segnale stesso,
        quindi il percorso difensivo si simula con la lettura guasta.)"""
        import agents.data_agent as da

        _seed_match_and_signal(temp_db)
        monkeypatch.setattr(da, "_match_rows", lambda conn, ids: {})
        out = da.DataAgent(intel_fn=lambda row: 1 / 0).process()
        assert out.intel == []       # nessuna intel SENZA nomi reali
        assert out.signals           # ma i segnali restano

    def test_intel_fn_rotta_fail_safe(self, temp_db):
        from agents.data_agent import DataAgent

        _seed_match_and_signal(temp_db)

        def _boom(row):
            raise RuntimeError("intel giu'")

        out = DataAgent(intel_fn=_boom).process()
        assert out.intel == [] and out.signals  # il ciclo prosegue

    def test_switch_env_spegne_l_intel(self, temp_db, monkeypatch):
        from agents.data_agent import DataAgent

        _seed_match_and_signal(temp_db)
        monkeypatch.setenv("LIVE_INTEL", "0")
        out = DataAgent(intel_fn=lambda row: 1 / 0).process()
        assert out.intel == []

    def test_senza_segnali_nessuna_chiamata(self):
        from agents.data_agent import DataAgent

        out = DataAgent(intel_fn=lambda row: 1 / 0).process(conn=None)
        assert out.intel == []

    def test_import_data_agent_non_carica_librerie(self):
        out = subprocess.run(
            [sys.executable, "-c",
             "import sys, agents.data_agent; "
             "print('HEAVY:' + ','.join(m for m in ('soccerdata','nba_api',"
             "'pybaseball','ddgs') if m in sys.modules))"],
            capture_output=True, text=True,
            cwd=str(Path(live_intel.__file__).parent))
        assert out.stdout.split("HEAVY:")[1].strip() == ""
