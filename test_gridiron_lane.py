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


def _oracle_line_payload(market="totals", *, line, home=HOME, away=AWAY,
                         p1=1.90, p2=1.95, book="pinnacle"):
    """Payload the-odds-api con un mercato A LINEA (`totals` o `spreads`).

    Ricorda la forma REALE (verificata il 30/09): il campo `point` sta
    sull'OUTCOME, non sull'oggetto mercato — un payload col punto a livello
    mercato farebbe scartare tutte le linee.
    """
    if market == "totals":
        outs = [{"name": "Over", "point": line, "price": p1},
                {"name": "Under", "point": line, "price": p2}]
    else:
        outs = [{"name": home, "point": line, "price": p1},
                {"name": away, "point": -line, "price": p2}]
    return [{
        "home_team": home, "away_team": away,
        "bookmakers": [{
            "key": book, "title": book.capitalize(),
            "markets": [{"key": market, "outcomes": outs}],
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

    def test_ev_min_dal_modulo_di_produzione(self, monkeypatch):
        monkeypatch.delenv("GRIDIRON_EV_MIN", raising=False)
        import value_filter as vf
        assert gl.ev_min() == pytest.approx(vf.EV_MIN)
        assert gl.ev_min() > 0

    def test_ev_min_override_della_corsia(self, monkeypatch):
        """La soglia del torneo e' una env DELLA CORSIA: tararla non tocca
        la soglia globale di `value_filter`, e togliendo l'override la corsia
        torna da sola alla soglia di progetto.

        ⚠️ Il valore di override e' scelto DIVERSO da quello di produzione
        (0.008): con `GRIDIRON_EV_MIN == vf.EV_MIN` il test non proverebbe
        nulla (14/10/2026 il default di progetto e' sceso proprio a 0.015 e
        l'assert originale `vf.EV_MIN != 0.015` e' diventato falso senza che
        nulla fosse rotto).
        """
        import value_filter as vf
        monkeypatch.setenv("GRIDIRON_EV_MIN", "0.008")
        assert gl.ev_min() == pytest.approx(0.008)
        # l'override della corsia NON tocca la soglia globale
        assert vf.EV_MIN != pytest.approx(0.008)
        # senza override la corsia riusa la soglia unica di progetto
        monkeypatch.delenv("GRIDIRON_EV_MIN", raising=False)
        assert gl.ev_min() == pytest.approx(vf.EV_MIN)

    def test_ev_min_env_impossibile_ricade_sul_default(self, monkeypatch):
        """Una soglia non si spegne con un env sbagliato: valori non numerici,
        <= 0 o > 1 ricadono sul default di produzione."""
        import value_filter as vf
        for bad in ("abc", "0", "-0.5", "2.5", ""):
            monkeypatch.setenv("GRIDIRON_EV_MIN", bad)
            assert gl.ev_min() == pytest.approx(vf.EV_MIN), bad

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

    def test_tipi_a_linea_e_stringa_markets(self, monkeypatch):
        """Il costo dell'oracolo dipende dai TIPI: 1 credito o 3.

        Col solo moneyline basta `h2h`; con un tipo a linea (spread/totali)
        servono anche `totals`/`spreads` — e' la spesa che il 07/10 aveva
        portato a spegnere il percorso a linea del CALCIO. Qui la corsia
        dichiara la propria, cosi' il budget non si scopre a runtime.
        """
        monkeypatch.delenv("GRIDIRON_TYPES", raising=False)
        assert gl.uses_line_oracle() is False
        assert gl.oracle_markets() == "h2h"                  # 1 credito
        monkeypatch.setenv("GRIDIRON_TYPES", "226,28")
        assert gl.uses_line_oracle() is True
        assert gl.oracle_markets() == "h2h,totals,spreads"   # 3 crediti
        monkeypatch.setenv("GRIDIRON_TYPES", "226,342")
        assert gl.uses_line_oracle() is True
        assert gl.oracle_markets() == "h2h,totals,spreads"
        monkeypatch.setenv("GRIDIRON_TYPES", "226")
        assert gl.uses_line_oracle() is False

    def test_cache_paths_per_i_due_percorsi(self, monkeypatch):
        """Il fetch deve scrivere DUE cache: i due percorsi leggono prefissi
        diversi (`toa_*` il moneyline, `toao_*` il percorso a linea) e la glob
        `toa_*` NON aggancia `toao_*`."""
        monkeypatch.setenv("GRIDIRON_TYPES", "226,28")
        names = {p.name for p in gl.cache_paths("americanfootball_nfl")}
        assert names == {"toa_americanfootball_nfl.json",
                         "toao_americanfootball_nfl.json"}
        monkeypatch.setenv("GRIDIRON_TYPES", "226")
        names = {p.name for p in gl.cache_paths("americanfootball_nfl")}
        assert names == {"toa_americanfootball_nfl.json"}

    def test_oracle_key_for(self):
        """Etichetta di ledger -> chiave dell'oracolo (mai fuzzy)."""
        assert gl._oracle_key_for("Over 49.5") == "Over"
        assert gl._oracle_key_for("Under 49.5") == "Under"
        assert gl._oracle_key_for("Home -2.5") == "Home"
        assert gl._oracle_key_for("Away +2.5") == "Away"
        # il moneyline ha un percorso dedicato ('1'/'2'): qui non si mappa
        assert gl._oracle_key_for("1") is None
        assert gl._oracle_key_for(None) is None
        assert gl._oracle_key_for("qualcosa") is None


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

    def test_discovery_tipi_a_linea(self, monkeypatch):
        """Con `GRIDIRON_TYPES=226,342,28` i tre tipi sono nativi del
        contratto 2.0 (ML_OT / AH_OT / OU_OT) e portano la LINEA + le
        etichette di ledger che il settlement legge senza codice nuovo."""
        monkeypatch.setenv("GRIDIRON_TYPES", "226,342,28")
        ko = _kickoff(6)
        ms = [
            _market("0xM1", HOME, AWAY, kickoff=ko, mtype="226"),
            _market("0xM2", HOME, AWAY, kickoff=ko, mtype="342", line=2.5,
                    o1=f"{HOME} +2.5", o2=f"{AWAY} -2.5"),
            _market("0xM3", HOME, AWAY, kickoff=ko, mtype="28", line=49.5,
                    o1="Over 49.5", o2="Under 49.5"),
        ]
        prov = FakeSx(ms, {m["marketHash"]: _book(1.85, 2.20) for m in ms})
        evs = {e["sx_type"]: e for e in gl.discover(provider=prov)}
        assert set(evs) == {"226", "342", "28"}
        assert evs["226"]["market_type"] == "ML_OT"
        assert evs["226"]["line"] is None
        assert evs["342"]["market_type"] == "AH_OT"
        assert evs["342"]["line"] == 2.5
        assert evs["28"]["market_type"] == "OU_OT"
        assert evs["28"]["line"] == 49.5
        assert {s["key"]: s["label"] for s in evs["342"]["sides"]} == \
            {"1": "Home +2.5", "2": "Away -2.5"}
        assert {s["key"]: s["label"] for s in evs["28"]["sides"]} == \
            {"1": "Over 49.5", "2": "Under 49.5"}

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

    def test_tipo_a_linea_senza_oracolo_a_linea(self, monkeypatch):
        """Un tipo a LINEA passa dal percorso a linea: senza una cache
        `totals`/`spreads` (qui c'e' solo l'h2h) l'oracolo e' fail-closed e
        non nasce nessun pick. Il percorso con la cache giusta e' in
        `TestPicksLinea`."""
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


class TestPicksLinea:
    """SPREAD (342) e TOTALI (28): il percorso che allarga il torneo.

    Il torneo Gridiron conta SOLO NFL/NCAAF, e i mercati a linea ne fanno
    parte come il moneyline: su SX hanno piu' eventi e piu' liquidita'.
    L'oracolo (`totals`/`spreads` di Pinnacle) e' l'unico giudice del prezzo:
    senza la sua p_true non si produce nulla (fail-closed).
    """

    def test_pick_ou_con_oracolo_a_linea(self, monkeypatch):
        monkeypatch.setenv("GRIDIRON_TYPES", "226,28")
        # il percorso a LINEA legge la cache `toao_*` (quella del moneyline e'
        # `toa_*`): e' la cartella che il fetch scrive quando le linee servono.
        _write_cache(_oracle_line_payload("totals", line=49.5,
                                          p1=1.90, p2=1.95),
                     name="toao_americanfootball_nfl.json")
        ko = _kickoff(6)
        m = _market("0xOK", HOME, AWAY, kickoff=ko, mtype="28", line=49.5,
                    o1="Over 49.5", o2="Under 49.5")
        # SX paga l'Over 2.10 contro una quota equa sharp ~1.94 -> +EV
        prov = FakeSx([m], {"0xOK": _book(2.10, 1.80)})
        picks = gl.picks(provider=prov)
        assert picks, "atteso il pick OU"
        p = picks[0]
        assert p["mercato"] == "OU"
        assert p["esito_key"] == "Over 49.5"
        assert p["esito_raw"] == "Over 49.5"
        assert p["market_id"] == "0xOK" and p["selection_id"] == 1
        assert p[gl.PICK_FLAG] is True
        assert p["best_ev"] > gl.ev_min()
        assert gl.in_odds_band(p["quota"])

    def test_pick_ah_con_oracolo_a_linea(self, monkeypatch):
        monkeypatch.setenv("GRIDIRON_TYPES", "226,342")
        _write_cache(_oracle_line_payload("spreads", line=2.5,
                                          p1=1.90, p2=1.95),
                     name="toao_americanfootball_nfl.json")
        ko = _kickoff(6)
        m = _market("0xAH", HOME, AWAY, kickoff=ko, mtype="342", line=2.5,
                    o1=f"{HOME} +2.5", o2=f"{AWAY} -2.5")
        prov = FakeSx([m], {"0xAH": _book(2.10, 1.80)})
        picks = gl.picks(provider=prov)
        assert picks, "atteso il pick AH"
        p = picks[0]
        assert p["mercato"] == "AH"
        assert p["esito_key"] == "Home +2.5"
        assert p["market_id"] == "0xAH" and p["selection_id"] == 1
        assert p["best_ev"] > gl.ev_min()

    def test_linea_non_prezzata_fail_closed(self, monkeypatch):
        """Pinnacle prezza 49.5, SX quota 47.5: nessun verdetto su una linea
        che l'oracolo non copre (mai un EV calcolato con la linea sbagliata)."""
        monkeypatch.setenv("GRIDIRON_TYPES", "226,28")
        _write_cache(_oracle_line_payload("totals", line=49.5),
                     name="toao_americanfootball_nfl.json")
        ko = _kickoff(6)
        m = _market("0xLM", HOME, AWAY, kickoff=ko, mtype="28", line=47.5,
                    o1="Over 47.5", o2="Under 47.5")
        prov = FakeSx([m], {"0xLM": _book(2.10, 1.80)})
        assert gl.picks(provider=prov) == []

    def test_nessuna_cache_a_linea_fail_closed(self, monkeypatch):
        monkeypatch.setenv("GRIDIRON_TYPES", "226,28")
        ko = _kickoff(6)
        m = _market("0xNC", HOME, AWAY, kickoff=ko, mtype="28", line=49.5,
                    o1="Over 49.5", o2="Under 49.5")
        prov = FakeSx([m], {"0xNC": _book(2.10, 1.80)})
        assert gl.picks(provider=prov) == []

    def test_linea_specchio_non_produce_pick(self, monkeypatch):
        """Regressione del bug REALE del 12/10/2026 (EV +29% falso, NFL).

        Caso riscontrato in produzione su Tampa Bay Buccaneers vs Pittsburgh
        Steelers: Pinnacle prezza la sola linea MAIN — la squadra di casa
        `point` **+2.5** (underdog), la trasferta **-2.5** — mentre SX quota
        ANCHE lo specchio ("Tampa Bay Buccaneers -2.5" @ 2.41). Il ripiego sul
        punto assegnava la p_true di *Pittsburgh -2.5* (0.535) al prezzo SX di
        *Tampa -2.5* → un `strong_value` con EV +29% su un mercato che
        l'oracolo NON prezza. Con i nomi leggibili il verdetto deve essere
        fail-closed: nessun pick.
        """
        monkeypatch.setenv("GRIDIRON_TYPES", "342")
        # Linea main di Pinnacle: casa +2.5, trasferta -2.5 (forma reale).
        _write_cache(_oracle_line_payload("spreads", line=2.5,
                                          p1=2.08, p2=1.82),
                     name="toao_americanfootball_nfl.json")
        ko = _kickoff(6)
        # SX quota lo SPECCHIO (linea -2.5) con prezzi che sembrano +EV.
        m = _market("0xMIR", HOME, AWAY, kickoff=ko, mtype="342", line=-2.5,
                    o1=f"{HOME} -2.5", o2=f"{AWAY} +2.5")
        prov = FakeSx([m], {"0xMIR": _book(2.41, 1.52)})
        assert gl.picks(provider=prov) == []

    def test_linea_main_resta_valutata(self, monkeypatch):
        """Controprova: la linea che Pinnacle PREZZA resta giudicata.

        Il fix non spegne il percorso a linea — elimina solo il verdetto sul
        verso sbagliato. Stessa partita, mercato alla linea 2.5 (casa +2.5):
        SX paga l'esito di casa 2.30 contro una quota equa sharp ~2.15 -> +EV.
        """
        monkeypatch.setenv("GRIDIRON_TYPES", "342")
        _write_cache(_oracle_line_payload("spreads", line=2.5,
                                          p1=2.08, p2=1.82),
                     name="toao_americanfootball_nfl.json")
        ko = _kickoff(6)
        m = _market("0xMAIN", HOME, AWAY, kickoff=ko, mtype="342", line=2.5,
                    o1=f"{HOME} +2.5", o2=f"{AWAY} -2.5")
        prov = FakeSx([m], {"0xMAIN": _book(2.30, 1.70)})
        picks = gl.picks(provider=prov)
        assert picks, "la linea main deve restare valutata"
        assert picks[0]["esito_key"] == "Home +2.5"
        # La linea del pick e' finalmente leggibile (prima era None).
        assert picks[0]["line"] == 2.5

    def test_tipi_disabilitati_non_valutati(self, monkeypatch):
        """Con `GRIDIRON_TYPES=226` un mercato a linea non entra in gioco:
        e' cio' che tiene il costo a 1 credito quando non serve di piu'."""
        monkeypatch.setenv("GRIDIRON_TYPES", "226")
        # Cache a LINEA scritta di proposito: senza, il test passerebbe per
        # l'assenza del dato e non proverebbe il filtro sul TIPO.
        _write_cache(_oracle_line_payload("totals", line=49.5),
                     name="toao_americanfootball_nfl.json")
        ko = _kickoff(6)
        m = _market("0xOFF", HOME, AWAY, kickoff=ko, mtype="28", line=49.5,
                    o1="Over 49.5", o2="Under 49.5")
        prov = FakeSx([m], {"0xOFF": _book(2.10, 1.80)})
        assert gl.picks(provider=prov) == []


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

    def test_markets_include_le_linee_coi_tipi_a_linea(self, monkeypatch):
        """Con un tipo a linea abilitato la fetch paga `h2h,totals,spreads`
        (3 crediti): il costo e' DICHIARATO dalla corsia, non scoperto a
        runtime — ed e' il motivo per cui il tetto di spesa e' anche in
        CREDITI (`TestBudgetCrediti`), non solo in richieste."""
        seen = []

        def fake_get(url, params=None, timeout=None):
            seen.append(dict(params or {}))
            return FakeResp(200, _oracle_payload(),
                            {"x-requests-remaining": "400"})

        monkeypatch.setenv("GRIDIRON_TYPES", "226,28")
        monkeypatch.setenv("ODDS_API_KEY", "fake-key")
        res = gl.refresh_oracle(http_get=fake_get)
        assert res["markets"] == "h2h,totals,spreads"
        assert seen and all(p.get("markets") == "h2h,totals,spreads"
                            for p in seen)

    def test_scrittura_su_entrambe_le_cache_coi_tipi_a_linea(self, monkeypatch):
        """BUG dell'11/10: un fetch con le linee scriveva SOLO `toa_*`, quindi
        il percorso a linea (`toao_*`) restava cieco e non nasceva nessun pick
        spread/totali — pur avendo pagato i 3 crediti. Ora si scrivono
        ENTRAMBE le copie dallo stesso payload (costo invariato)."""
        monkeypatch.setenv("GRIDIRON_TYPES", "226,28")
        monkeypatch.setenv("GRIDIRON_SPORTS", "americanfootball_nfl")
        monkeypatch.setenv("ODDS_API_KEY", "fake-key")

        def fake_get(url, params=None, timeout=None):
            return FakeResp(200, _oracle_payload(),
                            {"x-requests-remaining": "400"})

        res = gl.refresh_oracle(http_get=fake_get)
        assert res["fetched"] == 1
        assert res["files_written"] == 2
        folder = Path(__import__("os").environ["GRIDIRON_ORACLE_CACHE"])
        assert (folder / "toa_americanfootball_nfl.json").exists()
        assert (folder / "toao_americanfootball_nfl.json").exists()
        # e la seconda volta non si spende: entrambe le copie sono fresche
        res2 = gl.refresh_oracle(http_get=fake_get)
        assert res2["fetched"] == 0 and res2["skipped"] == 1

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


class TestBudgetCrediti:
    """Tetto di SPESA in crediti: con i tipi a linea la singola fetch costa
    3 crediti, quindi il solo tetto in richieste porterebbe la spesa
    giornaliera da 8 a 24 crediti senza che nessuno lo decida."""

    def test_crediti_per_fetch_dipende_dai_mercati(self, monkeypatch):
        monkeypatch.setenv("GRIDIRON_TYPES", "226")
        assert gl.credits_per_call() == 1
        monkeypatch.setenv("GRIDIRON_TYPES", "226,342,28")
        assert gl.credits_per_call() == 3

    def test_il_tetto_in_crediti_morde_prima_delle_richieste(self, monkeypatch):
        """Con 3 leghe e 6 crediti di budget partono DUE fetch (6 crediti):
        la terza costerebbe 9 e viene rifiutata — anche se le richieste
        (8) non sono esaurite."""
        monkeypatch.setattr(gl, "REQ_BUDGET_DAY", 8)
        monkeypatch.setattr(gl, "CREDIT_BUDGET_DAY", 6)
        monkeypatch.setenv("GRIDIRON_TYPES", "226,28")
        monkeypatch.setenv("GRIDIRON_SPORTS", "americanfootball_nfl,"
                                              "americanfootball_ncaaf,"
                                              "americanfootball_cfl")
        monkeypatch.setenv("ODDS_API_KEY", "fake-key")
        calls = []

        def fake_get(url, params=None, timeout=None):
            calls.append(url)
            return FakeResp(200, _oracle_payload(), {})

        res = gl.refresh_oracle(http_get=fake_get)
        assert res["fetched"] == 2
        assert len(calls) == 2
        assert res["credits_today"] == 6
        assert res["credits_per_call"] == 3
        assert res["credit_budget"] == 6

    def test_solo_moneyline_mantiene_la_spesa_di_prima(self, monkeypatch):
        """Col solo type 226 il budget in crediti (8) coincide col tetto in
        richieste (8): nessun cambiamento per quella configurazione."""
        monkeypatch.setattr(gl, "REQ_BUDGET_DAY", 8)
        monkeypatch.setattr(gl, "CREDIT_BUDGET_DAY", 8)
        monkeypatch.setenv("GRIDIRON_TYPES", "226")
        monkeypatch.setenv("GRIDIRON_SPORTS", "americanfootball_nfl,"
                                              "americanfootball_ncaaf")
        monkeypatch.setenv("ODDS_API_KEY", "fake-key")

        def fake_get(url, params=None, timeout=None):
            return FakeResp(200, _oracle_payload(), {})

        res = gl.refresh_oracle(http_get=fake_get)
        assert res["fetched"] == 2
        assert res["credits_today"] == 2

    def test_lo_stato_registra_i_crediti_spesi(self, monkeypatch):
        monkeypatch.setenv("GRIDIRON_TYPES", "226,28")
        monkeypatch.setenv("GRIDIRON_SPORTS", "americanfootball_nfl")
        monkeypatch.setenv("ODDS_API_KEY", "fake-key")

        def fake_get(url, params=None, timeout=None):
            return FakeResp(200, _oracle_payload(), {})

        gl.refresh_oracle(http_get=fake_get)
        data = json.loads(gl.state_path().read_text(encoding="utf-8"))
        assert data["credits"] == 3 and data["requests"] == 1
        # secondo giro: cache fresca, nessuna spesa aggiuntiva
        res2 = gl.refresh_oracle(http_get=fake_get)
        assert res2["credits_today"] == 3

    def test_i_crediti_si_azzerano_col_giorno(self, monkeypatch, tmp_path):
        path = tmp_path / "stato.json"
        path.write_text(json.dumps({"day": "2020-01-01", "requests": 4,
                                    "credits": 12}), encoding="utf-8")
        monkeypatch.setattr(gl, "state_path", lambda: path)
        state, healthy = gl._load_state()
        state = gl._roll_day(state)
        assert state["credits"] == 0 and state["requests"] == 0
        assert healthy is True

    def test_un_file_vecchio_senza_crediti_non_rompe(self, monkeypatch,
                                                     tmp_path):
        """Stato scritto da un deploy precedente: nessun campo `credits`."""
        path = tmp_path / "stato.json"
        path.write_text(json.dumps({"day": gl._now().strftime("%Y-%m-%d"),
                                    "requests": 1}), encoding="utf-8")
        monkeypatch.setattr(gl, "state_path", lambda: path)
        state, healthy = gl._load_state()
        assert healthy is True
        assert gl.budget_left(state, healthy=healthy) is True

    def test_budget_illeso_illegibile_non_spende(self):
        assert gl.budget_left({}, healthy=False) is False


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
                     "GRIDIRON_REQ_BUDGET_DAY",
                     "GRIDIRON_CREDIT_BUDGET_DAY", "GRIDIRON_ORACLE_CACHE",
                     "GRIDIRON_LANE_STATE", "GRIDIRON_DISCOVERY_TTL_S",
                     "GRIDIRON_JOB_INTERVAL_MIN", "GRIDIRON_ODDS_BASE"):
            assert name in iac, f"env non dichiarata nella IaC: {name}"

    def test_flag_allineato_con_auto_bet(self):
        import auto_bet
        assert auto_bet.GRIDIRON_FLAG == gl.PICK_FLAG


class TestDedupCorsie:
    """Il dedup cross-corsia deve preferire la versione con identita' di mercato.

    Bug del 12/10/2026 (torneo Gridiron): la telemetria del ledger scrive una
    riga 1X2 con lo STESSO `match_id` (`sx-<eventId>`) del pick della corsia ma
    SENZA `market_id`/flag. Il dedup teneva la PRIMA (quella del ledger) e la
    corsia perdeva il pick: l'ordine NFL non partiva mai (finiva in
    `top_down SKIP [no_oracle]` o in `mercato non trovato` sulla corsia chief).
    """

    LEDGER_PICK = {
        "match_id": "sx-L19431545", "esito_key": "1", "mercato": "1X2",
        "home": "Los Angeles Rams", "away": "Buffalo Bills",
        "quota": 1.6194, "status": "value",
    }

    LANE_PICK = {
        **LEDGER_PICK, "market_id": "0x426bdd259e83", "selection_id": 1,
        gl.PICK_FLAG: True, "esito_raw": "1", "team": "1",
    }

    def test_lane_specific_riconosce_le_corsie(self):
        import auto_bet as ab
        assert ab.lane_specific(self.LANE_PICK) is True
        assert ab.lane_specific(self.LEDGER_PICK) is False
        # eSports (ML) e tennis (TENNIS) senza il flag ma col mercato proprio
        assert ab.lane_specific({"mercato": "ML", "team": "X"}) is True
        assert ab.lane_specific({"mercato": "TENNIS"}) is True
        # un mercato_id presente e' di per se' identita' di mercato
        assert ab.lane_specific({"mercato": "1X2",
                                 "market_id": "0xabc"}) is True
        # leninario (nessuna identita') non e' lane-specific
        assert ab.lane_specific({"mercato": "1X2"}) is False

    def test_la_corsia_vince_sul_ledger_anche_se_arriva_dopo(self):
        import auto_bet as ab
        out = ab.dedup_lanes([self.LEDGER_PICK, self.LANE_PICK])
        assert len(out) == 1
        assert out[0].get(ab.GRIDIRON_FLAG) is True
        assert out[0]["market_id"] == "0x426bdd259e83"

    def test_ledger_prima_non_vince(self):
        """Ordine REALE in `run_today_bets`: `_today_value_picks()` precede
        `_gridiron_picks()` -> e' esattamente il caso che rompeva il torneo."""
        import auto_bet as ab
        out = ab.dedup_lanes([self.LEDGER_PICK, self.LANE_PICK])
        assert len(out) == 1
        assert ab.lane_specific(out[0]) is True

    def test_a_parita_di_specificita_vince_la_prima(self):
        import auto_bet as ab
        out = ab.dedup_lanes([self.LEDGER_PICK, dict(self.LEDGER_PICK)])
        assert len(out) == 1
        assert out[0] is self.LEDGER_PICK

    def test_eventi_diversi_restano_entrambi(self):
        import auto_bet as ab
        altro = {**self.LEDGER_PICK, "match_id": "sx-L999"}
        out = ab.dedup_lanes([self.LEDGER_PICK, altro, self.LANE_PICK])
        assert len(out) == 2

    def test_esiti_diversi_restano_entrambi(self):
        import auto_bet as ab
        due = {**self.LANE_PICK, "esito_key": "2"}
        out = ab.dedup_lanes([self.LANE_PICK, due])
        assert len(out) == 2


class TestLiveFillGridiron:
    """`_live_fill` usa l'identita' di mercato GIA' nel pick, anche a LINEA.

    La struttura football e' a due esiti con le chiavi 1/2 del book: il pick
    porta `market_id` + `selection_id`, quindi non si ri-risolve nulla (il
    catalogo 1X2 e' il calcio). Il bersaglio `order_target` e' la convenzione
    del CALCIO: su un pick gridiron a linea non deve nemmeno essere consultato,
    altrimenti una label non interpretata dai parser calcistici scarterebbe un
    pick che ha un'identita' di mercato valida (fail-closed sul percorso
    sbagliato).
    """

    def _pick(self, **kw):
        base = {
            "match_id": "sx-ev-0xAH", "home": HOME, "away": AWAY,
            "mercato": "AH", "esito_key": "Home +2.5",
            "esito_raw": "Home +2.5", "team": "Home +2.5",
            "quota": 2.10, "price": 2.10,
            "market_id": "0xAH", "selection_id": 1, "line": 2.5,
            "commence": (datetime.now(timezone.utc)
                         + timedelta(hours=3)).isoformat(),
            gl.PICK_FLAG: True,
        }
        base.update(kw)
        return base

    class _Prov:
        name = "sxbet"

        def __init__(self, best=None, order=None):
            self.best = best
            self.order = order
            self.place_calls = []

        def best_back_price(self, market_id, selection_id):
            return self.best

        def place_limit_order(self, market_id, selection_id, side, price,
                              size, persistence="LAPSE"):
            self.place_calls.append((market_id, selection_id, side, price,
                                     size))
            return self.order

    def _engine(self, monkeypatch, prov):
        import execution_engine as ee
        engine = type("_Eng", (), {"provider": prov})()
        monkeypatch.setattr(ee, "ExecutionEngine", lambda *a, **k: engine)
        return ee

    def test_pick_ah_a_linea_usa_l_identita_del_pick(self, monkeypatch):
        import auto_bet as ab
        import multi_market
        # il parser CALCISTICO fallirebbe: se venisse consultato, il pick
        # sarebbe scartato pur avendo un market_id/selection_id validi.
        monkeypatch.setattr(multi_market, "order_target", lambda pick: None)
        ee = self._engine(monkeypatch, None)
        order = ee.OrderResult(True, "0x9", "FULLY_FILLED", 2.10, 2.12,
                               1.0, 8.0)
        prov = self._Prov(best=None, order=order)
        self._engine(monkeypatch, prov)
        res = ab._live_fill(self._pick(), stake=1.0, floor=2.10)
        assert res is not None and res["ok"] is True
        # l'ordine e' andato sul mercato DEL PICK, non su uno ri-risolto
        assert prov.place_calls == [("0xAH", 1, "BACK", 2.10, 1.0)]
        assert res["market_id"] == "0xAH" and res["selection_id"] == 1

    def test_pick_gridiron_senza_identita_fail_closed(self, monkeypatch):
        """Senza `market_id`/`selection_id` NON si indovina il mercato."""
        import auto_bet as ab
        prov = self._Prov(best=None)
        self._engine(monkeypatch, prov)
        res = ab._live_fill(self._pick(market_id="", selection_id=None),
                            stake=1.0, floor=2.10)
        assert res is None
        assert prov.place_calls == []
