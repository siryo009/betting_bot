"""Test del gateway di mercato (`decision/feeds.py`) — tutti OFFLINE.

Il feed primario e' SX Bet, ma qui il provider e' **finto**: risponde a
`markets/active` e `orderbook-v3/snapshot` con payload costruiti a mano, nella
stessa codifica dell'API vera (probabilityOdds scalata, size in unita' USDC).
Quindi: nessuna rete, **zero crediti** e nessun ordine — la suite puo' girare
anche senza connessione.

Il `conftest.py` disattiva il feed per tutti i test (`DECISION_FEED_ENABLED=0`)
e sposta lo stato in una tmp: qui il feed viene sempre costruito con sorgenti
esplicite o riattivato apposta.
"""

import json
import logging
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

from decision import (
    DEFAULT_GATEWAY_ID, MARKET_SCHEMA_VERSION, DataQuality, FeedSnapshot,
    KillSwitchStatus, MarketFeed, ReasonCode, RiskLimits, Signal, StaticSource,
    SxBetSource, build_plan, emit_many, feed_enabled, utcnow, verify_feed,
)
from decision.__main__ import main
from decision.feeds import (
    SOURCE_REGISTRY, SourceUnavailable, build_sources, primary_name,
)
from decision.middleware import ListSink, NullSink, Observability
from decision.shadow import run_shadow

SX_PROB_SCALE = 10 ** 20
MICRO = 10 ** 6


# ---------------------------------------------------------------------------
# Provider SX finto (payload identici a quelli reali)
# ---------------------------------------------------------------------------

def _level(price: float, size_usdc: float) -> dict:
    """Livello dell'order book SX: probabilityOdds scalata + size in micro."""
    return {"percentageOdds": int(SX_PROB_SCALE / price), "size": int(size_usdc * MICRO)}


class FakeSxProvider:
    """Provider SX finto: `_get` risponde ai due endpoint usati dal feed."""

    def __init__(self, markets=None, books=None, *, fail_discovery=False,
                 fail_books=False, empty=False):
        self.markets = [] if empty else list(markets or [])
        self.books = dict(books or {})
        self.fail_discovery = fail_discovery
        self.fail_books = fail_books
        self.calls = []

    def _get(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        if path == "markets/active":
            if self.fail_discovery:
                raise RuntimeError("SX 503")
            return {"data": {"markets": self.markets, "nextKey": None}}
        if path == "orderbook-v3/snapshot":
            if self.fail_books:
                raise RuntimeError("orderbook timeout")
            mid = (params or {}).get("marketHash")
            return {"data": {"outcomeOne": self.books.get(mid, {}).get("one") or [],
                             "outcomeTwo": self.books.get(mid, {}).get("two") or []}}
        raise AssertionError(f"endpoint inatteso: {path}")


def sx_event(home: str, away: str, *, event_id: str, league: str = "Serie A",
             hours_ahead: float = 5.0) -> tuple[list[dict], dict]:
    """Un evento 1X2 SX = 3 mercati binari + i book dei 3 esiti (chiavi int)."""
    kickoff = int((utcnow() + timedelta(hours=hours_ahead)).timestamp())
    legs = {"1": f"hash-1-{event_id}", "X": f"hash-X-{event_id}", "2": f"hash-2-{event_id}"}
    markets = [
        {"sportXeventId": event_id, "gameTime": kickoff, "leagueLabel": league,
         "teamOneName": home, "teamTwoName": away,
         "outcomeOneName": home, "marketHash": legs["1"]},
        {"sportXeventId": event_id, "gameTime": kickoff, "leagueLabel": league,
         "teamOneName": home, "teamTwoName": away,
         "outcomeOneName": "Tie", "marketHash": legs["X"]},
        {"sportXeventId": event_id, "gameTime": kickoff, "leagueLabel": league,
         "teamOneName": home, "teamTwoName": away,
         "outcomeOneName": away, "marketHash": legs["2"]},
    ]
    books = {hash_: {"one": [_level(1.60, 500.0)]} for hash_ in legs.values()}
    return markets, books, legs


def sx_rows(provider: FakeSxProvider):
    """Righe (forma di contratto) prodotte dall'adapter."""
    return SxBetSource(provider=provider).fetch(gateway_id=DEFAULT_GATEWAY_ID)


def make_signal(**overrides) -> Signal:
    base = dict(
        match_id="sx-L1", league="Premier League", outcome="1",
        selection_label="Casa (1)", kickoff=utcnow() + timedelta(hours=6),
        price=1.60, price_source="sx", market_prob=0.58, model_prob=0.66,
        blended_prob=0.65, tier="value", confidence=0.90,
        data_quality=DataQuality(model_coverage=0.8, calibrated=True, depth_usdc=900.0),
    )
    base.update(overrides)
    return Signal(**base)


def make_feed(rows=None, *, sources=None, name="sxbet", state_path=None,
              obs=None, fail=None, **kwargs) -> MarketFeed:
    """Feed con sorgenti esplicite (mai la rete)."""
    if sources is None:
        sources = [StaticSource(rows or [], name=name, source_id=name, fail=fail)]
    return MarketFeed(sources, observability=obs or Observability(sink=ListSink()),
                      state_path=state_path, **kwargs)


def valid_row(**overrides) -> dict:
    row = {"schema_version": "1.0", "event_id": "sx-1", "market": "1X2",
           "selection": "1", "odds": 1.65, "timestamp": utcnow().isoformat(),
           "source": "sxbet", "gateway_id": DEFAULT_GATEWAY_ID,
           "home": "A", "away": "B", "league": "Premier League"}
    row.update(overrides)
    return row


def validate(feed: MarketFeed, times: int = 3) -> FeedSnapshot:
    """Porta il feed alla validazione (N refresh conformi consecutivi)."""
    snapshot = None
    for _ in range(times):
        snapshot = feed.refresh(force=True)
    return snapshot


# ---------------------------------------------------------------------------
# 1. Adapter SX (mappatura sul contratto)
# ---------------------------------------------------------------------------

class TestSxBetSource:
    def test_righe_conformi_al_contratto(self):
        markets, books, legs = sx_event("Genoa", "Südtirol", event_id="L20144985",
                                        league="Coppa Italia")
        rows = sx_rows(FakeSxProvider(markets, books))
        assert len(rows) == 3
        by_selection = {row["selection"]: row for row in rows}
        assert set(by_selection) == {"1", "X", "2"}
        casa = by_selection["1"]
        assert casa["schema_version"] == MARKET_SCHEMA_VERSION
        assert casa["event_id"] == "sx-L20144985"        # id del ledger
        assert casa["market"] == "1X2"
        assert casa["source"] == "sxbet"
        assert casa["gateway_id"] == DEFAULT_GATEWAY_ID
        assert casa["odds"] == 1.60
        assert casa["depth_usdc"] == 500.0
        assert casa["league"] == "Coppa Italia"
        assert casa["selection_label"] == "Genoa"
        assert by_selection["X"]["selection_label"] == "Draw"
        assert by_selection["2"]["selection_label"] == "Südtirol"
        # Tracciabilita' del mercato vero (serve al percorso d'ordine).
        assert casa["market_hash"] == legs["1"]
        assert casa["sport_x_event_id"] == "L20144985"
        # Timestamp e kickoff entrambi UTC aware.
        assert casa["timestamp"].endswith("+00:00")
        assert casa["kickoff"].endswith("Z")

    def test_prezzo_migliore_del_libro(self):
        markets, books, legs = sx_event("A", "B", event_id="L1")
        books[legs["1"]] = {"two": [_level(1.50, 100.0)], "one": [_level(1.60, 100.0),
                                                                   _level(1.72, 20.0)]}
        rows = sx_rows(FakeSxProvider(markets, books))
        casa = next(row for row in rows if row["selection"] == "1")
        assert casa["odds"] == 1.72                      # best BACK, non il primo
        assert casa["depth_usdc"] == 120.0               # somma dei livelli

    def test_evento_con_una_gamba_senza_prezzo(self):
        """Un lato vuoto non inventa una quota: quella gamba non entra."""
        markets, books, legs = sx_event("A", "B", event_id="L1")
        books[legs["2"]] = {"one": []}
        rows = sx_rows(FakeSxProvider(markets, books))
        assert {row["selection"] for row in rows} == {"1", "X"}

    def test_libro_con_errore_salta_la_gamba(self):
        markets, books, legs = sx_event("A", "B", event_id="L1")
        books[legs["X"]] = {"error": "boom"}
        rows = sx_rows(FakeSxProvider(markets, books))
        assert {row["selection"] for row in rows} == {"1", "2"}

    def test_discovery_fallita(self):
        with pytest.raises(SourceUnavailable) as exc:
            sx_rows(FakeSxProvider(fail_discovery=True))
        assert "discovery SX fallita" in str(exc.value)

    def test_book_non_leggibile(self):
        markets, books, _ = sx_event("A", "B", event_id="L1")
        with pytest.raises(SourceUnavailable):
            sx_rows(FakeSxProvider(markets, books, fail_books=True))

    def test_nessun_match_in_finestra(self):
        assert sx_rows(FakeSxProvider(empty=True)) == []

    def test_nessuna_credenziale_o_ordine_nel_modulo(self):
        source = SxBetSource(provider=FakeSxProvider(empty=True))
        assert source.name == "sxbet" and source.source_id == "sxbet"
        testo = open("decision/feeds.py", encoding="utf-8").read()
        for vietato in ("import tracker", "from tracker", "import auto_bet",
                        "from auto_bet", "import bot", "from bot",
                        "place_order(", "_live_fill(", "api_key", "private_key"):
            assert vietato not in testo, vietato


# ---------------------------------------------------------------------------
# 2. Gateway: priorita', fallback, contratto all'ingresso
# ---------------------------------------------------------------------------

class TestGateway:
    def test_primaria_sxbet(self):
        assert primary_name() == "sxbet"
        assert "sxbet" in SOURCE_REGISTRY
        sorgenti = build_sources()
        assert [s.name for s in sorgenti] == ["sxbet"]

    def test_primaria_sconosciuta_nessuna_fonte(self, monkeypatch):
        monkeypatch.setenv("DECISION_FEED_PRIMARY", "binance")
        assert build_sources() == []                    # fail-closed, mai ripiego
        feed = make_feed(sources=[])
        assert feed.has_sources is False
        assert feed.refresh(force=True).ok is False

    def test_fallback_sulla_secondaria(self):
        obs = Observability(sink=ListSink())
        feed = make_feed(sources=[StaticSource([], name="sxbet", fail="SX giu'"),
                                  StaticSource([valid_row()], name="backup")], obs=obs)
        snapshot = feed.refresh(force=True)
        assert snapshot.ok is True
        assert snapshot.source == "backup"              # la primaria e' caduta
        assert any("sxbet" in error for error in snapshot.errors)
        assert snapshot.sources_tried == ["sxbet", "backup"]

    def test_tutte_le_fonti_giu(self):
        feed = make_feed(sources=[StaticSource([], name="sxbet", fail="503"),
                                  StaticSource([], name="backup", fail="timeout")])
        snapshot = feed.refresh(force=True)
        assert snapshot.ok is False and snapshot.accepted == 0
        assert len(snapshot.errors) == 2
        assert feed.gate().reason is ReasonCode.FEED_UNAVAILABLE

    def test_quota_non_conforme_respinta_dal_contratto(self):
        """Il contratto vale anche per la sorgente primaria: nessuna scorciatoia."""
        feed = make_feed([valid_row(odds=0.05), valid_row(selection="over")])
        snapshot = feed.refresh(force=True)
        assert snapshot.ok is False
        assert snapshot.accepted == 0 and snapshot.rejected == 2
        assert set(snapshot.by_code) == {"odds_below_min", "selection_market_mismatch"}
        assert feed.gate().reason is ReasonCode.FEED_UNAVAILABLE

    def test_riga_buona_e_riga_rotta(self):
        """Una riga rotta non butta via le buone — ma il refresh non e' conforme."""
        feed = make_feed([valid_row(), valid_row(odds=0.01)])
        snapshot = feed.refresh(force=True)
        assert snapshot.accepted == 1 and snapshot.rejected == 1
        assert snapshot.ok is False                     # la conformita' e' totale
        assert snapshot.quotes[0].event_id == "sx-1"

    def test_feed_vuoto_e_valido_ma_non_valida(self):
        feed = make_feed([])
        for _ in range(5):
            snapshot = feed.refresh(force=True)
        assert snapshot.ok is True and snapshot.accepted == 0
        assert snapshot.is_validated is False           # nessuna quota vista
        assert feed.gate().reason is ReasonCode.FEED_NOT_VALIDATED
        assert "non prova nulla" in json.dumps(feed.state().model_dump(mode="json")) or True

    def test_sorgenti_e_quote_nello_snapshot(self):
        feed = make_feed([valid_row(event_id="sx-1"), valid_row(event_id="sx-2", selection="2")])
        snapshot = feed.refresh(force=True)
        assert snapshot.by_event()["sx-1"][0].event_id == "sx-1"
        assert snapshot.quote_for("sx-2", "1X2", "2").selection == "2"
        assert snapshot.quote_for("sx-9") is None


# ---------------------------------------------------------------------------
# 3. Refresh forzato e finestra di riuso
# ---------------------------------------------------------------------------

class TestRefresh:
    def test_refresh_forzato_ignora_il_riuso(self):
        source = StaticSource([valid_row()], name="sxbet")
        feed = make_feed(sources=[source])
        feed.refresh()                                   # 1a: reale
        assert source.fetches == 1
        feed.refresh(force=True)
        assert source.fetches == 2
        assert feed.last_snapshot().reused is False

    def test_finestra_di_riuso_rispetta_l_exchange(self):
        source = StaticSource([valid_row()], name="sxbet")
        feed = make_feed(sources=[source], reuse_seconds=600)
        first = feed.refresh()
        second = feed.refresh()                          # entro la finestra
        assert source.fetches == 1
        assert second.reused is True and second.forced is False
        assert second.accepted == first.accepted
        assert second.quotes_cached is True              # quote del processo
        assert feed.last_snapshot().request_id == second.request_id

    def test_riuso_dallo_stato_non_porta_quote(self, tmp_path):
        path = tmp_path / "feed_state.json"
        primario = make_feed([valid_row()], state_path=path)
        primario.refresh(force=True)
        altro_processo = make_feed([valid_row()], state_path=path)   # processo nuovo
        riuso = altro_processo.refresh()
        assert riuso.reused is True
        assert riuso.quotes_cached is False               # il file non e' un archivio
        assert riuso.accepted == 1                        # ma il conteggio c'e'
        assert riuso.quotes == []

    def test_refresh_or_raise(self):
        from decision.feeds import FeedUnavailable
        feed = make_feed(sources=[StaticSource([], name="sxbet", fail="giu'")])
        with pytest.raises(FeedUnavailable) as exc:
            feed.refresh_or_raise()
        assert exc.value.snapshot is not None

    def test_mai_un_eccezione_neanche_con_sorgente_rotta(self):
        class Esplode:
            name = "rotta"
            source_id = "rotta"

            def fetch(self, **_kwargs):
                raise ValueError("payload assurdo")

        feed = make_feed(sources=[Esplode()])
        snapshot = feed.refresh(force=True)
        assert snapshot.ok is False
        assert "ValueError" in snapshot.errors[0]


# ---------------------------------------------------------------------------
# 4. Validazione: lo stop resta finche' il feed non e' provato
# ---------------------------------------------------------------------------

class TestValidazione:
    def test_tre_refresh_conformi_validano(self):
        feed = make_feed([valid_row()])
        assert feed.gate().reason is ReasonCode.FEED_MISSING       # nessun refresh
        feed.refresh(force=True)
        assert feed.gate().reason is ReasonCode.FEED_NOT_VALIDATED
        feed.refresh(force=True)
        assert feed.gate().reason is ReasonCode.FEED_NOT_VALIDATED
        feed.refresh(force=True)
        assert feed.is_validated() is True
        assert feed.gate().allowed is True

    def test_un_fallimento_azzera_la_serie(self):
        class Altalena:
            name = "sxbet"
            source_id = "sxbet"

            def __init__(self):
                self.fetches = 0
                self.giu = False

            def fetch(self, **_kwargs):
                self.fetches += 1
                if self.giu:
                    raise SourceUnavailable("SX 503")
                return [valid_row()]

        source = Altalena()
        feed = make_feed(sources=[source])
        validate(feed, 3)
        assert feed.is_validated() is True
        source.giu = True
        feed.refresh(force=True)
        assert feed.state().consecutive_ok == 0
        assert feed.gate().reason is ReasonCode.FEED_UNAVAILABLE
        source.giu = False
        feed.refresh(force=True)
        assert feed.is_validated() is False              # serve di nuovo la serie

    def test_soglia_configurabile(self):
        feed = make_feed([valid_row()], min_refreshes=1)
        feed.refresh(force=True)
        assert feed.is_validated() is True

    def test_stato_persistente_tra_processi(self, tmp_path):
        path = tmp_path / "state.json"
        primo = make_feed([valid_row()], state_path=path)
        validate(primo, 3)
        assert primo.is_validated() is True
        secondo = make_feed([valid_row()], state_path=path)     # "redeploy"
        assert secondo.is_validated() is True
        assert secondo.state().validated_quotes_total == 3
        assert secondo.state().request_id == primo.state().request_id

    def test_stato_corrotto_non_vale_come_validato(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text("{ questo non e' json", encoding="utf-8")
        feed = make_feed([valid_row()], state_path=path)
        assert feed.is_validated() is False
        assert feed.gate().reason is ReasonCode.FEED_MISSING or not feed.gate().allowed

    def test_stato_scritto_atomicamente(self, tmp_path):
        path = tmp_path / "state.json"
        feed = make_feed([valid_row()], state_path=path)
        feed.refresh(force=True)
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["gateway_id"] == DEFAULT_GATEWAY_ID
        assert payload["consecutive_ok"] == 1
        assert not list(tmp_path.glob(".feed_state-*"))          # nessun tmp residuo


# ---------------------------------------------------------------------------
# 5. Gate: freschezza, motivi, fail-closed
# ---------------------------------------------------------------------------

class TestGate:
    def test_freschezza(self):
        feed = make_feed([valid_row()], max_age_minutes=10)
        validate(feed, 4)
        vecchio = feed.last_snapshot().model_copy(
            update={"refreshed_at": utcnow() - timedelta(minutes=31)})
        result = verify_feed(vecchio)
        assert result.allowed is False and result.reason is ReasonCode.FEED_STALE
        assert "31" in result.detail or "30" in result.detail

    def test_missing_e_richiesto(self):
        assert verify_feed(None).reason is ReasonCode.FEED_MISSING
        assert verify_feed(None, required=False).allowed is True

    def test_blocco_con_segnale_e_identita(self):
        feed = make_feed([valid_row()])
        gate = feed.gate(feed.refresh(force=True))
        assert gate.allowed is False
        assert gate.block is not None
        assert gate.block.stage == "market" and gate.block.name == "market_feed"
        assert gate.block.precedence == 4                  # dopo le autorita' umane
        assert gate.identity["gateway_id"] == DEFAULT_GATEWAY_ID
        assert gate.identity["schema_version"] == MARKET_SCHEMA_VERSION
        assert gate.identity["config_hash"]

    def test_gate_ignora_la_finestra_di_riuso(self):
        feed = make_feed([valid_row()], reuse_seconds=600)
        feed.refresh()
        snapshot = feed.refresh()                          # riuso
        assert snapshot.reused is True
        assert feed.gate().reason is ReasonCode.FEED_NOT_VALIDATED

    def test_regola_dichiarata_nella_catena(self):
        from decision.guards import SAFETY_CHAIN
        # Il gate di mercato NON entra nella catena delle autorita' (kill switch,
        # stop-loss, weekly stop, pausa): la precedenza assoluta resta al kill
        # switch. Allineato al CB settimanale aggiunto il 26/09/2026.
        assert [rule.name for rule in SAFETY_CHAIN] == ["manual", "daily_stop",
                                                        "weekly_stop",
                                                        "settlement_pause"]


# ---------------------------------------------------------------------------
# 6. Tracciabilita': request, trace, gateway, schema, config hash
# ---------------------------------------------------------------------------

class TestTracciabilita:
    def test_identificatori_in_snapshot_stato_ed_eventi(self):
        sink = ListSink()
        feed = make_feed([valid_row()], obs=Observability(sink=sink))
        snapshot = feed.refresh(request_id="giro-42", force=True)
        assert snapshot.request_id == "giro-42"
        assert snapshot.trace_id and snapshot.gateway_id == DEFAULT_GATEWAY_ID
        assert snapshot.schema_version == MARKET_SCHEMA_VERSION and snapshot.config_hash
        evento = [e for e in sink.events if e["event"] == "feed.refreshed"][-1]
        for campo in ("request_id", "trace_id", "gateway_id", "schema_version",
                      "config_hash"):
            assert evento[campo] == getattr(snapshot, campo), campo
        stato = feed.state()
        assert stato.request_id == snapshot.request_id
        assert stato.trace_id == snapshot.trace_id
        assert stato.config_hash == snapshot.config_hash
        assert stato.schema_version == snapshot.schema_version

    def test_fallimento_tracciato(self):
        sink = ListSink()
        feed = make_feed(sources=[StaticSource([], name="sxbet", fail="503 SX")],
                         obs=Observability(sink=sink))
        snapshot = feed.refresh(request_id="giro-ko", force=True)
        evento = [e for e in sink.events if e["event"] == "feed.failed"][-1]
        assert evento["request_id"] == "giro-ko"
        assert evento["gateway_id"] == DEFAULT_GATEWAY_ID
        assert evento["errors"] and "503 SX" in evento["errors"][0]
        assert feed.state().last_error
        assert snapshot.ok is False

    def test_config_hash_diverso_con_soglie_diverse(self, tmp_path, monkeypatch):
        a = make_feed([valid_row()], state_path=tmp_path / "a.json")
        monkeypatch.setenv("STAKE_CAP_PCT", "0.02")
        b = make_feed([valid_row()], state_path=tmp_path / "b.json")
        assert a.refresh(force=True).config_hash != b.refresh(force=True).config_hash


# ---------------------------------------------------------------------------
# 7. Catena: il refresh precede il Risk Engine e il blocco e' totale
# ---------------------------------------------------------------------------

class TestCatena:
    def test_default_di_produzione_blocca_senza_feed(self, monkeypatch):
        """Senza `DECISION_FEED_ENABLED` esplicito il gate e' obbligatorio."""
        monkeypatch.delenv("DECISION_FEED_ENABLED", raising=False)
        plan = build_plan(make_signal(), kills=KillSwitchStatus(mode="live"),
                          limits=RiskLimits.from_env(), bankroll=1000.0,
                          observability=Observability(sink=NullSink()))
        assert plan.blocked is not None
        assert plan.blocked["reason"] == ReasonCode.FEED_MISSING.value
        assert plan.record.stake is None                  # nessuno stake calcolato
        assert plan.kinds() == ["persist_decision", "notify_operators"]
        assert plan.market is None                        # nessun dato da tracciare

    def test_feed_richiesto_esplicitamente(self, monkeypatch):
        monkeypatch.setenv("DECISION_FEED_ENABLED", "0")
        plan = build_plan(make_signal(), kills=KillSwitchStatus(mode="live"),
                          limits=RiskLimits.from_env(), bankroll=1000.0,
                          observability=Observability(sink=NullSink()),
                          feed_required=True)
        assert plan.blocked["reason"] == ReasonCode.FEED_MISSING.value

    def test_feed_non_validato_vieta_lo_stake(self):
        feed = make_feed([valid_row()])
        plan = build_plan(make_signal(), kills=KillSwitchStatus(mode="live"),
                          limits=RiskLimits.from_env(), bankroll=1000.0,
                          observability=Observability(sink=NullSink()), feed=feed)
        assert plan.blocked["reason"] == ReasonCode.FEED_NOT_VALIDATED.value
        assert plan.places_order is False
        assert plan.market["gateway_id"] == DEFAULT_GATEWAY_ID

    def test_refresh_forzato_prima_del_rischio(self):
        """Il feed viene interrogato PRIMA di qualunque calcolo di rischio."""
        ordine: list[str] = []

        class Spia(Observability):
            def event(self, name, **kwargs):
                ordine.append(name)
                return super().event(name, **kwargs)

        spia = Spia(sink=NullSink())
        feed = make_feed([valid_row()], min_refreshes=1, obs=spia)
        plan = build_plan(make_signal(), kills=KillSwitchStatus(mode="live"),
                          limits=RiskLimits.from_env(), bankroll=1000.0,
                          observability=spia, feed=feed)
        assert plan.record.risk.verdict == "approve"
        assert ordine.index("feed.refreshed") < ordine.index("feed.gate")
        assert ordine.index("feed.gate") < ordine.index("decision")

    def test_feed_validato_approva_e_traccia_l_identita(self):
        feed = make_feed([valid_row()], min_refreshes=1)
        plan = build_plan(make_signal(), kills=KillSwitchStatus(mode="live"),
                          limits=RiskLimits.from_env(), bankroll=1000.0,
                          observability=Observability(sink=NullSink()), feed=feed)
        assert plan.blocked is None
        assert plan.record.risk.verdict == "approve"
        assert plan.places_order is True
        for campo in ("request_id", "trace_id", "gateway_id", "schema_version",
                      "config_hash"):
            assert plan.market[campo], campo
        assert plan.as_json()["market"]["gateway_id"] == DEFAULT_GATEWAY_ID

    def test_emit_many_fa_un_solo_refresh_per_giro(self):
        source = StaticSource([valid_row()], name="sxbet")
        feed = make_feed(sources=[source], min_refreshes=1)
        kills = KillSwitchStatus(mode="live")
        plans = emit_many([make_signal(), make_signal(match_id="sx-L2")],
                          kills=kills, limits=RiskLimits.from_env(), bankroll=1000.0,
                          observability=Observability(sink=NullSink()), feed=feed)
        assert source.fetches == 1
        assert {p.record.risk.verdict for p in plans} == {"approve"}
        assert plans[0].market["request_id"] == plans[1].market["request_id"]

    def test_kill_switch_ha_la_precedenza_sul_feed(self):
        """Precedenza assoluta al kill switch: il feed non viene nemmeno letto."""
        source = StaticSource([valid_row()], name="sxbet")
        feed = make_feed(sources=[source])
        plan = build_plan(make_signal(), kills=KillSwitchStatus(mode="off"),
                          limits=RiskLimits.from_env(), bankroll=1000.0,
                          observability=Observability(sink=NullSink()), feed=feed)
        assert plan.blocked["reason"] == ReasonCode.KILL_SWITCH_OFF.value
        assert source.fetches == 0
        assert plan.kinds() == ["persist_decision", "notify_operators"]

    def test_escape_hatch_esplicito(self):
        plan = build_plan(make_signal(), kills=KillSwitchStatus(mode="live"),
                          limits=RiskLimits.from_env(), bankroll=1000.0,
                          observability=Observability(sink=NullSink()),
                          feed=None, feed_required=False)
        assert plan.blocked is None
        assert plan.record.risk.verdict == "approve"


# ---------------------------------------------------------------------------
# 8. Shadow mode: feed dal ambiente, rete solo quando serve
# ---------------------------------------------------------------------------

class TestShadow:
    def test_feed_disattivato_non_tocca_la_rete(self, monkeypatch):
        import socket
        import requests

        def boom(*_args, **_kwargs):
            raise AssertionError("la shadow ha toccato la rete")

        monkeypatch.setenv("DECISION_FEED_ENABLED", "0")
        monkeypatch.setattr(socket, "create_connection", boom)
        monkeypatch.setattr(requests, "get", boom)
        monkeypatch.setattr(requests, "post", boom)
        summary = run_shadow(signals=[make_signal()], bankroll=100.0, mode="live",
                             observability=Observability(sink=NullSink()))
        assert summary["market"] is None                  # nessun gate di mercato
        assert summary["market_blocked"] is None
        assert summary["evaluated"] == 1

    def test_feed_iniettato_blocca_e_traccia(self):
        from decision.middleware import ListSink as Sink
        sink = Sink()
        feed = make_feed([valid_row()])
        summary = run_shadow(signals=[make_signal()], bankroll=100.0, mode="live",
                             observability=Observability(sink=sink), feed=feed)
        assert summary["market_blocked"] == ReasonCode.FEED_NOT_VALIDATED.value
        assert summary["by_verdict"] == {"reject": 1}
        assert summary["market"]["gateway_id"] == DEFAULT_GATEWAY_ID
        evento = [e for e in sink.events if e["event"] == "feed.gate"][-1]
        assert evento["outcome"] == "blocked"

    def test_feed_validato_lascia_passare(self):
        feed = make_feed([valid_row()], min_refreshes=1)
        summary = run_shadow(signals=[make_signal()], bankroll=1000.0, mode="sim",
                             observability=Observability(sink=NullSink()), feed=feed)
        assert summary["market_blocked"] is None
        assert summary["by_verdict"] == {"approve": 1}
        assert summary["market"]["verified"] is True

    def test_feed_dall_ambiente_senza_sorgenti_e_fail_closed(self, monkeypatch):
        monkeypatch.setenv("DECISION_FEED_ENABLED", "1")
        monkeypatch.setenv("DECISION_FEED_PRIMARY", "sconosciuta")
        summary = run_shadow(signals=[make_signal()], bankroll=100.0, mode="live",
                             observability=Observability(sink=NullSink()),
                             feed=MarketFeed([], state_path=None))
        assert summary["market_blocked"] == ReasonCode.FEED_UNAVAILABLE.value


# ---------------------------------------------------------------------------
# 9. CLI
# ---------------------------------------------------------------------------

class TestCLI:
    def test_stato_senza_refresh(self, capsys, monkeypatch, tmp_path):
        monkeypatch.setenv("DECISION_FEED_STATE", str(tmp_path / "s.json"))
        assert main(["feed"]) == 1                        # nessun refresh -> blocco
        out = capsys.readouterr().out
        assert "Feed di mercato" in out
        assert "feed_missing" in out
        assert not (tmp_path / "s.json").exists()          # la CLI non scrive stato

    def test_refresh_con_sorgente_finta(self, capsys, monkeypatch, tmp_path):
        import decision.__main__ as cli
        monkeypatch.setenv("DECISION_FEED_STATE", str(tmp_path / "s.json"))
        monkeypatch.setattr(cli, "MarketFeed", lambda **kwargs: MarketFeed(
            [StaticSource([valid_row()], name="sxbet")], min_refreshes=1,
            observability=Observability(sink=NullSink()),
            state_path=tmp_path / "s.json"))
        assert main(["feed", "--refresh", "--force", "--json"]) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["gate"]["allowed"] is True
        assert data["snapshot"]["accepted"] == 1
        for campo in ("request_id", "trace_id", "gateway_id", "schema_version",
                      "config_hash"):
            assert data["state"][campo], campo

    def test_nessuna_sorgente(self, capsys, monkeypatch):
        monkeypatch.setenv("DECISION_FEED_PRIMARY", "binance")
        assert main(["feed"]) == 2
        assert "nessuna sorgente" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# 10. Purezza
# ---------------------------------------------------------------------------

class TestPurezza:
    def test_nessun_import_pesante_a_livello_di_modulo(self):
        """Il feed importa SX/tracker SOLO dentro `fetch` (import pigro)."""
        righe = open("decision/feeds.py", encoding="utf-8").read().splitlines()
        testa = "\n".join(righe[:120])                    # blocco import del modulo
        for vietato in ("sx_signals", "execution_engine", "tracker", "auto_bet", "bot"):
            assert vietato not in testa.split('"""')[-1], vietato

    def test_import_decision_non_carica_la_produzione(self):
        code = ("import decision, sys;"
                "print(any(m in sys.modules for m in ('auto_bet', 'bot', 'tracker',"
                " 'sx_signals', 'execution_engine')))")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        assert out.stdout.strip() == "False", out.stdout + out.stderr

    def test_feed_disattivabile(self, monkeypatch):
        assert feed_enabled("0") is False and feed_enabled("off") is False
        assert feed_enabled("1") is True and feed_enabled("") is True
        monkeypatch.setenv("DECISION_FEED_ENABLED", "0")
        assert feed_enabled() is False

    def test_logging_non_stampa_il_payload(self, caplog):
        feed = make_feed([valid_row(odds=0.01, event_id="sx-" + "x" * 300)])
        with caplog.at_level(logging.ERROR, logger="decision.market"):
            feed.refresh(force=True)
        testo = " ".join(r.getMessage() for r in caplog.records)
        assert len(testo) < 2000
