"""Corsia eSports: discovery SX, oracolo OddsPapi, budget — TUTTI OFFLINE.

Nessuna rete, nessuna chiave, nessun ordine, nessuna scrittura sul ledger:
provider SX finto (catalogo + order book), trasporto OddsPapi finto, cache
nella tmp del test. Sono i tre confini che rendono la corsia verificabile
senza consumare la quota (250 richieste/mese) ne' muovere denaro.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import esports_lane as el
from execution_engine import SX_PROB_SCALE, resolve_moneyline_market


def _pct(price: float) -> str:
    """Quota decimale -> `percentageOdds` SX (prob. * 1e20)."""
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


def _market(mid, t1, t2, o1, *, kickoff, league="League of Legends",
            line=None, mtype="52", event_id="ev1"):
    return {
        "market_id": mid, "team_one_name": t1, "team_two_name": t2,
        "outcome_one_name": o1, "outcome_two_name": o1,
        "open_date": kickoff.isoformat(), "market_type_id": mtype,
        "line": line, "event_id": event_id, "league_label": league,
    }


def _book(price=1.60, usdc=50.0, *, other=2.40):
    return {"outcomeOne": [_level(price, usdc)],
            "outcomeTwo": [_level(other, usdc)]}


@pytest.fixture(autouse=True)
def _cache_in_tmp(tmp_path, monkeypatch):
    """Cache/budget SEMPRE nella tmp: mai il file di produzione.

    `CACHE_PATH` e' letto all'import del modulo, quindi l'env del conftest non
    basta: si sostituisce l'attributo (e' l'unico punto di verita' usato da
    `_load_state`/`_save_state`).
    """
    monkeypatch.setattr(el, "CACHE_PATH", tmp_path / "esports_state.json")
    monkeypatch.setenv("ESPORTS_LIVE", "1")
    # Chiave FINTA (marcata `fake/`): senza, l'oracolo e' fail-closed per
    # progetto e i test non arriverebbero mai al percorso da misurare. Il
    # trasporto e' comunque iniettato: nessuna rete.
    monkeypatch.setenv("ODDSPAPI_KEY", "fake/offline-esports-lane-key")
    # Pacing OFF: senza rete non c'e' un limite al minuto da rispettare, e
    # l'attesa reale renderebbe i test lenti senza verificare nulla.
    monkeypatch.setenv("ESPORTS_MIN_INTERVAL_S", "0")
    # Finestra oracolo larga: gli eventi dei test sono a +4h e devono essere
    # interrogati (la finestra produttiva e' 3h, coperta da un test dedicato).
    monkeypatch.setenv("ESPORTS_ORACLE_WINDOW_H", "24")
    yield


def _fixtures_payload(rows):
    return {"fixtures": rows}


def _odds_payload(p1=1.60, p2=2.40, *, id1="p1", id2="p2"):
    return {"participant1Id": id1, "participant2Id": id2,
            "bookmakerOdds": {"pinnacle": {"markets": {"185": {"outcomes": {
                "185": {"players": {"0": {"price": p1}}},
                "186": {"players": {"0": {"price": p2}}}}}}}}}


class FakeHttp:
    """Trasporto OddsPapi finto: risponde per path e CONTA le chiamate."""

    def __init__(self, fixtures=None, odds=None, *, status=200):
        self.fixtures = fixtures if fixtures is not None else []
        self.odds = odds if odds is not None else _odds_payload()
        self.status = status
        self.urls = []

    def __call__(self, url, params=None, timeout=None):
        self.urls.append(url)
        if "fixtures" in url:
            return self.status, _fixtures_payload(self.fixtures)
        if "/odds" in url:
            return self.status, self.odds
        return self.status, {}

    @property
    def calls(self):
        return len(self.urls)


def _scope(title="League of Legends"):
    return {"lol": "League of Legends"}.get(title, title)


# ---------------------------------------------------------------------------
# 1. Stato: cache, TTL, budget
# ---------------------------------------------------------------------------

class TestStato:
    def test_stato_vuoto_se_file_assente(self, tmp_path):
        state, healthy = el._load_state()
        assert healthy and state["requests"] == 0
        assert state["fixtures"] == {} and state["odds"] == {}

    def test_stato_corrotto_non_autorizza_richieste(self, tmp_path):
        el.CACHE_PATH.write_text("{non-json", encoding="utf-8")
        state, healthy = el._load_state()
        assert healthy is False
        assert el.budget_left(state, healthy=healthy) == 0

    def test_budget_scende_col_contatore(self):
        state = el._empty_state()
        assert el.budget_left(state) == el.REQ_BUDGET_DAY
        state["requests"] = el.REQ_BUDGET_DAY + 5
        assert el.budget_left(state) == 0

    def test_roll_day_azzera_solo_il_contatore(self):
        state = _scope_state()
        state["day"] = "2020-01-01"
        state["requests"] = 9
        out = el._roll_day(state)
        assert out["requests"] == 0
        assert "lol" in out["fixtures"]      # la cache non si butta

    def test_salvataggio_atomico_leggibile(self):
        state = el._empty_state()
        state["requests"] = 3
        assert el._save_state(state) is True
        again, healthy = el._load_state()
        assert healthy and again["requests"] == 3

    def test_fresh_rispetta_il_ttl(self):
        now = datetime.now(timezone.utc)
        fresh = {"ts": now.isoformat()}
        old = {"ts": (now - timedelta(minutes=999)).isoformat()}
        assert el._fresh(fresh, 10, now=now) is True
        assert el._fresh(old, 10, now=now) is False
        assert el._fresh({}, 10, now=now) is False
        assert el._fresh({"ts": "nonsense"}, 10, now=now) is False

    def test_finestra_oracolo_e_pacing_da_env(self, monkeypatch):
        # La fixture autouse imposta questi env per TUTTI i test (finestra
        # larga, pacing off): qui si parte dai DEFAULT, quindi vanno tolti.
        monkeypatch.delenv("ESPORTS_ORACLE_WINDOW_H", raising=False)
        monkeypatch.delenv("ESPORTS_MIN_INTERVAL_S", raising=False)
        assert el.oracle_window_h() == el.ORACLE_WINDOW_H_DEFAULT
        assert el.min_interval_s() == el.MIN_INTERVAL_S_DEFAULT
        monkeypatch.setenv("ESPORTS_ORACLE_WINDOW_H", "1.5")
        monkeypatch.setenv("ESPORTS_MIN_INTERVAL_S", "4")
        assert el.oracle_window_h() == 1.5
        assert el.min_interval_s() == 4.0

    def test_env_numerica_impossibile_torna_al_default(self, monkeypatch):
        monkeypatch.setenv("ESPORTS_ORACLE_WINDOW_H", "molto")
        monkeypatch.setenv("ESPORTS_MIN_INTERVAL_S", "-3")
        assert el.oracle_window_h() == el.ORACLE_WINDOW_H_DEFAULT
        assert el.min_interval_s() == 0.0     # negativo -> 0, mai un'attesa

    def test_ttl_diverso_per_oracolo_e_mancato(self):
        """Un "no_oracle" e' temporaneo (Pinnacle pubblica tardivo): TTL corto."""
        assert el._ttl_for({"ok": True}) == el.ODDS_TTL_MIN
        assert el._ttl_for({"ok": False}) == el.ODDS_MISS_TTL_MIN
        assert el.ODDS_MISS_TTL_MIN < el.ODDS_TTL_MIN

    def test_ttl_miss_tarato_sulla_finestra(self):
        """TTL-miss e finestra sono una COPPIA: 15min in 1h = 4 tentativi.

        30/09/2026: con TTL 60min dentro una finestra di 1h i ritentativi
        erano DUE e un drop pubblicato a T-45 non veniva mai visto. Il test
        difende i DEFAULT dichiarati (non l'env, che i test sovrascrivono):
        allungare il TTL o stringere la finestra deve rompere qui.
        """
        assert el.ODDS_MISS_TTL_MIN == 15
        tentativi = el.ORACLE_WINDOW_H_DEFAULT * 60.0 / el.ODDS_MISS_TTL_MIN
        assert tentativi >= 3, (
            "TTL-miss troppo lungo per la finestra oracolo: i drop tardivi di "
            "Pinnacle verrebbero persi")

    def test_budget_copre_due_eventi(self):
        """Il tetto giornaliero deve coprire ~2 eventi (obiettivo 30/09).

        4 richieste per evento nella finestra utile: il tetto e' 8.
        """
        assert el.REQ_BUDGET_DAY == 8
        richieste_per_evento = (
            el.ORACLE_WINDOW_H_DEFAULT * 60.0 / el.ODDS_MISS_TTL_MIN)
        assert el.REQ_BUDGET_DAY / richieste_per_evento >= 2.0

    def test_interruttore(self, monkeypatch):
        assert el.enabled() is True
        monkeypatch.setenv("ESPORTS_LIVE", "0")
        assert el.enabled() is False
        monkeypatch.setenv("ESPORTS_LIVE", "off")
        assert el.enabled() is False


def _scope_state():
    return {"day": el._now().strftime("%Y-%m-%d"), "requests": 0,
            "fixtures": {"lol": {"ts": el._now().isoformat(), "rows": []}},
            "odds": {}}


# ---------------------------------------------------------------------------
# 2. Discovery SX
# ---------------------------------------------------------------------------

class TestDiscovery:
    def _two_markets(self):
        kick = datetime.now(timezone.utc) + timedelta(hours=3)
        return [
            _market("m-t1", "T1 Alpha", "T2 Beta", "T1 Alpha", kickoff=kick),
            _market("m-t2", "T1 Alpha", "T2 Beta", "T2 Beta", kickoff=kick),
        ]

    def test_due_mercati_una_partita_due_lati(self):
        prov = FakeSx(self._two_markets(), {"m-t1": _book(1.60),
                                            "m-t2": _book(2.40)})
        events = el.discover(provider=prov)
        assert len(events) == 1
        ev = events[0]
        assert ev["team_one"] == "T1 Alpha" and ev["team_two"] == "T2 Beta"
        sides = {s["team"]: s for s in ev["sides"]}
        assert set(sides) == {"T1 Alpha", "T2 Beta"}
        # selection 1 = outcomeOne (semantica "X vs Not X" riusata)
        assert sides["T1 Alpha"]["selection_id"] == 1
        assert sides["T1 Alpha"]["price"] == 1.6
        assert sides["T2 Beta"]["market_id"] == "m-t2"

    def test_chiede_sport_9_e_type_52(self):
        prov = FakeSx(self._two_markets(), {"m-t1": _book(), "m-t2": _book()})
        el.discover(provider=prov)
        assert prov.calls and prov.calls[0] == (("9",), ("52",))

    def test_mercato_con_linea_escluso(self):
        kick = datetime.now(timezone.utc) + timedelta(hours=3)
        markets = [_market("m-t1", "A", "B", "A", kickoff=kick, line=2.5)]
        prov = FakeSx(markets, {"m-t1": _book()})
        assert el.discover(provider=prov) == []

    def test_fuori_finestra_escluso(self):
        kick = datetime.now(timezone.utc) + timedelta(hours=48)
        markets = [_market("m-t1", "A", "B", "A", kickoff=kick)]
        prov = FakeSx(markets, {"m-t1": _book()})
        assert el.discover(provider=prov) == []

    def test_liquidita_insufficiente_esclusa(self):
        kick = datetime.now(timezone.utc) + timedelta(hours=3)
        markets = [_market("m-t1", "A", "B", "A", kickoff=kick)]
        prov = FakeSx(markets, {"m-t1": _book(usdc=1.0)})
        assert el.discover(provider=prov) == []

    def test_provider_che_esplode_non_solleva(self):
        assert el.discover(provider=FakeSx([], fail=True)) == []

    def test_etichetta_lega_carriata(self):
        kick = datetime.now(timezone.utc) + timedelta(hours=3)
        markets = [_market("m-t1", "A", "B", "A", kickoff=kick,
                           league="CBLOL")]
        prov = FakeSx(markets, {"m-t1": _book()})
        events = el.discover(provider=prov)
        assert events and events[0]["league_label"] == "CBLOL"


# ---------------------------------------------------------------------------
# 3. Aggancio fixture (esports_oracle.match_fixture)
# ---------------------------------------------------------------------------

class TestMatchFixture:
    def _fx(self, n1, n2):
        return {"fixtureId": "f1", "participant1Name": n1,
                "participant2Name": n2, "participant1Id": "p1",
                "participant2Id": "p2"}

    def test_aggancia_la_fixture(self):
        import esports_oracle as eo
        rows = [self._fx("T1 Alpha", "T2 Beta")]
        got = eo.match_fixture("lol", "T1 Alpha", "T2 Beta", fixtures_rows=rows)
        assert got and got["fixtureId"] == "f1"

    def test_aggancia_anche_invertita(self):
        import esports_oracle as eo
        rows = [self._fx("T2 Beta", "T1 Alpha")]
        got = eo.match_fixture("lol", "T1 Alpha", "T2 Beta", fixtures_rows=rows)
        assert got is not None

    def test_ambiguo_rifiutato(self):
        """Due fixture uguali -> None: mai \"la piu' simile\"."""
        import esports_oracle as eo
        rows = [self._fx("T1 Alpha", "T2 Beta"),
                {"fixtureId": "f2", "participant1Name": "T1 Alpha",
                 "participant2Name": "T2 Beta"}]
        assert eo.match_fixture("lol", "T1 Alpha", "T2 Beta",
                                fixtures_rows=rows) is None

    def test_nessuna_corrispondenza(self):
        import esports_oracle as eo
        rows = [self._fx("Altro", "Altro2")]
        assert eo.match_fixture("lol", "T1 Alpha", "T2 Beta",
                                fixtures_rows=rows) is None


# ---------------------------------------------------------------------------
# 4. Corsia end-to-end (provider + trasporto finti)
# ---------------------------------------------------------------------------

class TestPicks:
    def _setup(self, *, sx1=1.60, sx2=2.55, fair=(1.45, 2.85), usdc=50.0):
        """Un evento con due lati prezzati su SX e un oracolo sharp.

        Default: SX 1.60 (in fascia) contro fair 1.45 -> EV positivo sul lato
        1; il lato 2 sta a 2.55, FUORI dalla fascia 1.30-1.80.
        """
        kick = datetime.now(timezone.utc) + timedelta(hours=4)
        markets = [
            _market("m-t1", "T1 Alpha", "T2 Beta", "T1 Alpha", kickoff=kick),
            _market("m-t2", "T1 Alpha", "T2 Beta", "T2 Beta", kickoff=kick),
        ]
        prov = FakeSx(markets, {"m-t1": _book(sx1, usdc),
                                "m-t2": _book(sx2, usdc)})
        http = FakeHttp(
            fixtures=[{"fixtureId": "f1", "participant1Name": "T1 Alpha",
                       "participant2Name": "T2 Beta", "participant1Id": "p1",
                       "participant2Id": "p2"}],
            odds=_odds_payload(fair[0], fair[1]))
        return prov, http

    def test_genera_pick_con_oracolo_favorevole(self):
        # SX 1.60 contro fair 1.45 -> EV sul lato 1 ben oltre il 2%.
        prov, http = self._setup()
        out = el.picks(provider=prov, http_get=http)
        assert len(out) == 1
        p = out[0]
        assert p["mercato"] == "ML" and p["esito_key"] == "1"
        assert p["team"] == "T1 Alpha"
        assert p["match_id"].startswith("sx-esports-")
        assert p["market_id"] == "m-t1" and p["selection_id"] == 1
        assert p["best_ev"] > 0.02 and p["p_true"] > 0
        assert p["esports_lane"] is True

    def test_prezzo_sopra_il_fair_nessun_pick(self):
        # SX 1.60 con fair 1.90 -> EV negativo: l'oracolo dice no.
        prov, http = self._setup(sx1=1.60, sx2=2.10, fair=(1.90, 2.05))
        assert el.picks(provider=prov, http_get=http) == []

    def test_fascia_quota_blocca_il_lato_fuori_fascia(self):
        """Valore sull'UNICO lato oltre 1.80: la fascia prevale sull'EV.

        SX 1.60 in fascia senza valore; SX 2.55 con valore (fair 1.45 sul lato
        2 significa p2 alto). Nessun pick: la strategia non compra fuori
        fascia, qualunque cosa dica l'oracolo.
        """
        prov, http = self._setup(sx1=1.60, sx2=2.55, fair=(2.85, 1.45))
        assert el.picks(provider=prov, http_get=http) == []

    def test_fascia_override_da_env(self, monkeypatch):
        monkeypatch.setenv("ESPORTS_ODDS_MAX", "3.00")
        prov, http = self._setup()
        out = el.picks(provider=prov, http_get=http)
        assert len(out) == 1      # il lato 1 resta l'unico con valore

    def test_oracolo_assente_nessun_pick(self):
        """Pinnacle non pubblica: fail-closed, nessun verdetto."""
        prov, _ = self._setup()
        http = FakeHttp(fixtures=[{"fixtureId": "f1",
                                   "participant1Name": "T1 Alpha",
                                   "participant2Name": "T2 Beta"}],
                        odds={"participant1Id": "p1", "participant2Id": "p2",
                              "bookmakerOdds": {}})
        assert el.picks(provider=prov, http_get=http) == []

    def test_lega_non_mappata_salta_senza_oracolo(self):
        kick = datetime.now(timezone.utc) + timedelta(hours=4)
        markets = [_market("m-t1", "A", "B", "A", kickoff=kick,
                           league="Torneo Sconosciuto")]
        prov = FakeSx(markets, {"m-t1": _book()})
        http = FakeHttp()
        assert el.picks(provider=prov, http_get=http) == []
        assert http.calls == 0      # nessuna richiesta sprecata

    def test_cache_evita_richieste_ripetute(self):
        prov, http = self._setup()
        el.picks(provider=prov, http_get=http)
        first = http.calls
        el.picks(provider=prov, http_get=http)
        assert http.calls == first          # fixtures + odds in cache

    def test_mancato_oracolo_ha_ttl_corto(self):
        """Un 'no oracle' non deve congelare la finestra per ore."""
        prov, _ = self._setup()
        http = FakeHttp(fixtures=[{"fixtureId": "f1",
                                   "participant1Name": "T1 Alpha",
                                   "participant2Name": "T2 Beta"}],
                        odds={"participant1Id": "p1", "participant2Id": "p2",
                              "bookmakerOdds": {}})
        el.picks(provider=prov, http_get=http)
        state, _ = el._load_state()
        entry = state["odds"]["f1"]
        assert entry["ok"] is False
        assert el._ttl_for(entry) == el.ODDS_MISS_TTL_MIN

    def test_budget_esaurito_nessuna_chiamata(self, monkeypatch):
        prov, http = self._setup()
        monkeypatch.setattr(el, "REQ_BUDGET_DAY", 0)
        assert el.picks(provider=prov, http_get=http) == []
        assert http.calls == 0

    def test_stato_corrotto_nessuna_chiamata(self):
        el.CACHE_PATH.write_text("{{{", encoding="utf-8")
        prov, http = self._setup()
        assert el.picks(provider=prov, http_get=http) == []
        assert http.calls == 0

    def test_oracolo_non_interrogato_fuori_finestra(self, monkeypatch):
        """Evento lontano: discovery si', oracolo NO (quota non sprecata).

        Su eSports Pinnacle pubblica tardivo: chiedere ore prima significa
        pagare una richiesta per un "non ancora".
        """
        monkeypatch.setenv("ESPORTS_ORACLE_WINDOW_H", "1")
        prov, http = self._setup()          # evento a +4h
        assert el.picks(provider=prov, http_get=http) == []
        assert http.calls == 0

    def test_budget_va_all_evento_piu_vicino(self, monkeypatch):
        """Budget scarso -> si processa PRIMA l'evento che sta per iniziare.

        Con 4 richieste per evento e 8 al giorno, un evento lontano che le
        consuma toglierebbe la copertura proprio a quello ordinabile. Il
        catalogo SX NON e' ordinato per kickoff: qui il piu' LONTANO e' primo
        in lista, quindi senza l'ordinamento il pick sarebbe del lontano.
        """
        monkeypatch.setenv("ESPORTS_ORACLE_WINDOW_H", "1")
        monkeypatch.setattr(el, "REQ_BUDGET_DAY", 2)   # fixtures + 1 evento
        now = datetime.now(timezone.utc)
        far = now + timedelta(minutes=55)
        near = now + timedelta(minutes=50)
        markets = [
            _market("m-far1", "Far Alpha", "Far Beta", "Far Alpha",
                    kickoff=far, event_id="ev-far"),
            _market("m-far2", "Far Alpha", "Far Beta", "Far Beta",
                    kickoff=far, event_id="ev-far"),
            _market("m-near1", "Near Alpha", "Near Beta", "Near Alpha",
                    kickoff=near, event_id="ev-near"),
            _market("m-near2", "Near Alpha", "Near Beta", "Near Beta",
                    kickoff=near, event_id="ev-near"),
        ]
        prov = FakeSx(markets, {"m-far1": _book(), "m-far2": _book(),
                                "m-near1": _book(), "m-near2": _book()})
        http = FakeHttp(
            fixtures=[{"fixtureId": "f1", "participant1Name": "Near Alpha",
                       "participant2Name": "Near Beta",
                       "participant1Id": "p1", "participant2Id": "p2"}],
            odds=_odds_payload(1.45, 2.85))
        out = el.picks(provider=prov, http_get=http)
        assert [p["home"] for p in out] == ["Near Alpha"], (
            "il budget e' finito sull'evento lontano")

    def test_summary_dichiara_gli_eventi_entro_la_finestra_oracolo(self,
                                                                   monkeypatch):
        """Due conteggi DISTINTI: discovery (24h, gratis) e finestra oracolo.

        Senza il secondo la corsia DORMIENTE (nessun evento vicino: zero
        richieste e **zero costo**, silenzio voluto nei log) sarebbe
        indistinguibile da una corsia rotta.
        """
        prov, http = self._setup()              # evento a +4h
        monkeypatch.setenv("ESPORTS_ORACLE_WINDOW_H", "1")
        s = el.summary(provider=prov, http_get=http)
        assert s["events_in_window"] == 1
        assert s["events_in_oracle_window"] == []
        assert "entro la finestra oracolo" in el.format_report(s)
        assert http.calls == 0                  # dormiente = nessuna spesa

        monkeypatch.setenv("ESPORTS_ORACLE_WINDOW_H", "24")
        s2 = el.summary(provider=prov, http_get=http)
        assert len(s2["events_in_oracle_window"]) == 1
        assert s2["events_in_oracle_window"][0]["home"] == "T1 Alpha"
        assert s2["events_in_oracle_window"][0]["title"] == "lol"

    def test_pacing_distanzia_le_chiamate(self, monkeypatch):
        """Il free tier limita al minuto: senza pacing si prendono 429."""
        sleeps: list[float] = []
        import time as _time
        monkeypatch.setenv("ESPORTS_MIN_INTERVAL_S", "2.5")
        monkeypatch.setattr(_time, "sleep", lambda s: sleeps.append(s))
        # prima chiamata: nessuna attesa (nessun precedente)
        el._LAST_CALL[0] = 0.0
        monkeypatch.setattr(_time, "monotonic", lambda: 100.0)
        el._pace()
        assert sleeps == []
        # seconda ravvicinata: attende il residuo
        monkeypatch.setattr(_time, "monotonic", lambda: 100.5)
        el._pace()
        assert sleeps and abs(sleeps[-1] - 2.0) < 0.01

    def test_pace_disattivabile(self, monkeypatch):
        calls = []
        import time as _time
        monkeypatch.setenv("ESPORTS_MIN_INTERVAL_S", "0")
        monkeypatch.setattr(_time, "sleep", lambda s: calls.append(s))
        el._pace()
        assert calls == []

    def test_corsia_spenta(self, monkeypatch):
        monkeypatch.setenv("ESPORTS_LIVE", "0")
        prov, http = self._setup()
        assert el.picks(provider=prov, http_get=http) == []
        assert http.calls == 0

    def test_errore_imprevisto_non_solleva(self, monkeypatch):
        monkeypatch.setattr(el, "discover", lambda provider=None: [
            {"kickoff": el._now(), "league_label": "League of Legends",
             "team_one": "A", "team_two": "B", "sides": [], "depth": 0}])
        assert el.picks() == []


# ---------------------------------------------------------------------------
# 5. Resolver d'ordine (sport 9, type 52)
# ---------------------------------------------------------------------------

class TestResolverMoneyline:
    def _prov(self, markets):
        return FakeSx(markets)

    def _mk(self, mid, o1, *, t1="T1 Alpha", t2="T2 Beta", line=None,
            kickoff=None):
        kick = kickoff or (datetime.now(timezone.utc) + timedelta(hours=2))
        return _market(mid, t1, t2, o1, kickoff=kick, line=line,
                       mtype="52", event_id="ev")

    def test_risolve_il_lato_richiesto(self):
        kick = datetime.now(timezone.utc) + timedelta(hours=2)
        prov = self._prov([self._mk("m-a", "T1 Alpha", kickoff=kick),
                           self._mk("m-b", "T2 Beta", kickoff=kick)])
        got = resolve_moneyline_market(prov, "T1 Alpha", "T2 Beta", "T1 Alpha",
                                       kick.isoformat())
        assert got and got["market_id"] == "m-a" and got["selection_id"] == 1
        got2 = resolve_moneyline_market(prov, "T1 Alpha", "T2 Beta", "T2 Beta",
                                        kick.isoformat())
        assert got2 and got2["market_id"] == "m-b"

    def test_squadra_estranea_non_risolve(self):
        kick = datetime.now(timezone.utc) + timedelta(hours=2)
        prov = self._prov([self._mk("m-a", "T1 Alpha", kickoff=kick)])
        assert resolve_moneyline_market(prov, "T1 Alpha", "T2 Beta", "Terza",
                                        kick.isoformat()) is None

    def test_provider_non_sxbet(self):
        class Altro:
            name = "smarkets"
        assert resolve_moneyline_market(Altro(), "A", "B", "A", None) is None

    def test_mercato_con_linea_non_e_moneyline(self):
        kick = datetime.now(timezone.utc) + timedelta(hours=2)
        prov = self._prov([self._mk("m-a", "T1 Alpha", line=2.5,
                                    kickoff=kick)])
        assert resolve_moneyline_market(prov, "T1 Alpha", "T2 Beta",
                                        "T1 Alpha", kick.isoformat()) is None

    def test_nessun_mercato(self):
        assert resolve_moneyline_market(self._prov([]), "T1 Alpha", "T2 Beta",
                                        "T1 Alpha", None) is None

    def test_chiede_sport_9_type_52(self):
        kick = datetime.now(timezone.utc) + timedelta(hours=2)
        prov = self._prov([self._mk("m-a", "T1 Alpha", kickoff=kick)])
        resolve_moneyline_market(prov, "T1 Alpha", "T2 Beta", "T1 Alpha",
                                 kick.isoformat())
        assert prov.calls and prov.calls[0] == (("9",), ("52",))


# ---------------------------------------------------------------------------
# 6. Cablaggio in auto_bet
# ---------------------------------------------------------------------------

class TestCablaggioAutoBet:
    def test_branch_ml_usa_il_resolver_per_nome(self, monkeypatch):
        import auto_bet
        calls = {}

        class FakeProv:
            name = "sxbet"

            def best_back_price(self, market_id, selection_id):
                return 1.65

        class FakeEngine:
            provider = FakeProv()

        monkeypatch.setattr(auto_bet, "DRY_RUN", False)

        class FakeEe:
            DryRunProvider = type("DryRunProvider", (), {})
            ExecutionEngine = lambda self=None: FakeEngine()

            @staticmethod
            def resolve_moneyline_market(prov, home, away, team, commence):
                calls.update(home=home, away=away, team=team)
                return {"market_id": "mkt", "selection_id": 1,
                        "event_name": "A vs B"}

        import sys
        monkeypatch.setitem(sys.modules, "execution_engine", FakeEe)
        monkeypatch.setattr(auto_bet, "required_depth", lambda stake: 0.0)
        monkeypatch.setattr(auto_bet, "_live_available_size",
                            lambda *a, **k: None)
        out = auto_bet._live_fill(
            {"match_id": "sx-esports-1", "home": "A", "away": "B",
             "team": "A", "mercato": "ML", "esito_key": "1"},
            stake=1.5, floor=1.60)
        assert calls.get("team") == "A"
        assert out is None or out.get("market_id") == "mkt"

    def test_pick_ml_senza_team_saltato(self, monkeypatch):
        import auto_bet
        monkeypatch.setattr(auto_bet, "DRY_RUN", False)
        out = auto_bet._live_fill(
            {"match_id": "sx-esports-1", "home": "A", "away": "B",
             "mercato": "ML", "esito_key": "1"}, stake=1.5, floor=1.60)
        assert out is None

    def test_corsia_esports_fail_safe(self, monkeypatch):
        import auto_bet
        import sys
        monkeypatch.setitem(sys.modules, "esports_lane", None)
        assert auto_bet._esports_picks() == []

    def test_il_gate_topdown_non_tocca_i_pick_ml(self):
        """Il gate Pinnacle legge le cache del CALCIO: su un pick eSports
        risponderebbe `no_oracle` e ucciderebbe la corsia."""
        import inspect
        import auto_bet
        src = inspect.getsource(auto_bet.run_today_bets)
        assert "\"ML\"" in src
        assert "TOP_DOWN_EV and mode == \"live\"" in src

    def test_corsia_wired_nel_board(self):
        import inspect
        import auto_bet
        src = inspect.getsource(auto_bet.run_today_bets)
        assert "_esports_picks()" in src


# ---------------------------------------------------------------------------
# 7. Tripwire: sola lettura, nessun ordine, nessuna rete all'import, IaC
# ---------------------------------------------------------------------------

class TestTripwire:
    def _code(self):
        import inspect
        src = inspect.getsource(el)
        # Le docstring NOMINANO le cose vietate per spiegarle: il tripwire
        # guarda il CODICE, non la prosa.
        out = []
        in_doc = False
        for line in src.splitlines():
            stripped = line.strip()
            if stripped.startswith(('"""', "'''")):
                in_doc = not in_doc
                if stripped.count('"""') == 2:
                    in_doc = False
                continue
            if not in_doc:
                out.append(line)
        return "\n".join(out)

    def test_nessuna_scrittura_sul_ledger(self):
        code = self._code()
        for forbidden in ("save_bet", "save_prediction", "save_analysis",
                          "save_result", "INSERT INTO", "UPDATE ", "sqlite3"):
            assert forbidden not in code, forbidden

    def test_nessun_ordine(self):
        code = self._code()
        for forbidden in ("place_limit_order", "_live_fill", "resolve_market",
                          "execution_engine.ExecutionEngine"):
            assert forbidden not in code, forbidden

    def test_nessuna_rete_a_livello_di_modulo(self):
        code = self._code()
        assert "import requests" not in code.split("def ")[0]
        # `requests`/provider entrano solo DENTRO le funzioni (import pigro).
        head = code.split("\ndef ", 1)[0]
        assert "requests" not in head

    def test_import_leggero_in_subprocess(self):
        import subprocess
        import sys
        code = ("import sys, esports_lane; "
                "bad=[m for m in ('auto_bet','bot','tracker','requests') "
                "if m in sys.modules]; print(bad)")
        res = subprocess.run([sys.executable, "-c", code],
                             capture_output=True, text=True, timeout=90)
        assert res.returncode == 0, res.stderr
        assert res.stdout.strip() == "[]"

    def test_env_dichiarate_nella_iac(self):
        from pathlib import Path
        iac = Path(".railway/railway.ts").read_text(encoding="utf-8")
        for name in ("ESPORTS_LIVE", "ESPORTS_SX_SPORT_ID", "ESPORTS_SX_TYPE_ID",
                     "ESPORTS_HOURS_AHEAD", "ESPORTS_MAX_EVENTS",
                     "ESPORTS_MAX_MARKETS", "ESPORTS_REQ_BUDGET_DAY",
                     "ESPORTS_FIXTURES_TTL_MIN", "ESPORTS_ODDS_TTL_MIN",
                     "ESPORTS_ODDS_MISS_TTL_MIN", "ESPORTS_CACHE",
                     "ESPORTS_ODDS_MIN", "ESPORTS_ODDS_MAX",
                     "ESPORTS_ORACLE_WINDOW_H", "ESPORTS_MIN_INTERVAL_S"):
            assert name in iac, name

    def test_guardrail_offline(self):
        """La diagnostica non deve toccare SX/OddsPapi ne' gli scraper."""
        from pathlib import Path
        src = Path("verify_guardrails.py").read_text(encoding="utf-8")
        assert 'os.environ["ESPORTS_LIVE"] = "0"' in src
        assert 'os.environ["LIVE_INTEL"] = "0"' in src
