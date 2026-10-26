"""Test del flusso di puntate automatiche (auto_bet.run_today_bets).

Dal 04/09 auto_bet è SIM-only permanente: nessun client Exchange, nessun
catalogo di scansione — le puntate sono simulate con la quota del segnale
(mode='sim') e registrate in `bets` per ledger/ML.
"""
import logging
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import tracker
import auto_bet
import value_filter


@pytest.fixture()
def temp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        monkeypatch.setattr(tracker, "DB_PATH", db_path)
        tracker.init_db()
        yield db_path


@pytest.fixture(autouse=True)
def _isolate_daily_stop(tmp_path, monkeypatch):
    """Lo stop-loss giornaliero usa un file temporaneo: i test non toccano
    mai il volume reale (data/execution/daily_stop.json)."""
    monkeypatch.setattr(auto_bet, "DAILY_STOP_FILE", tmp_path / "daily_stop.json")


ALLOWED_LEAGUE = "Premier League"   # in STRATEGY_LEAGUES


def _seed_value_match(mid="m1", home="Osasuna", away="Getafe", esito="1",
                      quota=1.85, status="value", commence=None):
    start = commence or (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat().replace("+00:00", "Z")
    # Lega AMMESSA dalla strategia (value_filter.STRATEGY_LEAGUES): dal
    # 15/09 la corsia ordini applica il gate di lega, quindi un seed in una
    # lega vietata (es. Serie A) farebbe uscire 0 puntate da OGNI test di
    # flusso, indipendentemente da cio' che il test vuole verificare.
    tracker.save_match(mid, ALLOWED_LEAGUE, home, away, start)
    # esito "1" -> best_esito = nome squadra di casa (come da API bookmaker)
    best_esito = home if esito == "1" else (away if esito == "2" else "Draw")
    tracker.save_analysis(mid, 1.7, 1.1, 0.52, 0.27, 0.21, 0.58, 0.08,
                          best_esito, quota, "Pinnacle", status,
                          market_prob=0.60, market_edge=0.07)
    # Ledger previsioni: dal 09/09 _today_value_picks legge da QUI (status
    # per ogni esito), non da match_analysis (solo best, che puo' essere
    # rejected anche quando altri esiti dello stesso match sono value).
    # quota 1.85 / prob. mercato 0.60 = favorito netto FUORI dalla fascia
    # morta 1.50-1.80 (esclusa il 25/10/2026): con 1.65 la corsia ordini
    # scarterebbe il seed per la banda e ogni test di flusso misurerebbe quel
    # gate invece del proprio.
    tracker.save_prediction(mid, "1X2", best_esito, quota, 0.52, 0.08,
                            market_prob=0.60, market_edge=0.07, status=status)


class TestGateDiMercato:
    """Lo stop del percorso d'ORDINE resta finche' il feed non e' validato.

    Il gate e' fail-closed: senza un refresh fresco, conforme al contratto e
    validato la catena si ferma qui — nessun ordine, nessuna riga sul ledger.
    Il feed primario e' SX Bet; qui la sorgente e' finta (zero rete, zero
    crediti) e `decision.feeds` viene iniettato.
    """

    def _feed(self, tmp_path, *, rows=True, min_refreshes=3):
        from decision import MarketFeed, StaticSource
        dati = [{
            "schema_version": "1.0", "event_id": "sx-m1", "market": "1X2",
            "selection": "1", "odds": 1.65,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": "sxbet", "gateway_id": "sxbet-feed",
        }] if rows else []
        return MarketFeed([StaticSource(dati, name="sxbet")],
                          min_refreshes=min_refreshes,
                          state_path=tmp_path / "feed_state.json")

    def _inietta(self, monkeypatch, feed):
        monkeypatch.setattr("decision.feeds.feed_enabled", lambda: True)
        monkeypatch.setattr("decision.feeds.feed_from_env", lambda *a, **k: feed)

    def test_feed_non_validato_blocca_le_puntate(self, monkeypatch, temp_db, tmp_path):
        _seed_value_match(quota=1.65)
        self._inietta(monkeypatch, self._feed(tmp_path, min_refreshes=3))
        assert auto_bet.run_today_bets(stake_eur=5.0) == []
        stato = auto_bet.market_gate_status()
        assert stato["blocked"] is True
        assert stato["reason"] == "feed_not_validated"
        assert stato["validated"] is False
        assert stato["gateway_id"] == "sxbet-feed"

    def test_refresh_fallito_blocca(self, monkeypatch, temp_db, tmp_path):
        from decision import MarketFeed, StaticSource
        _seed_value_match(quota=1.65)
        feed = MarketFeed([StaticSource([], name="sxbet", fail="SX 503")],
                          state_path=tmp_path / "feed_state.json")
        self._inietta(monkeypatch, feed)
        assert auto_bet.run_today_bets(stake_eur=5.0) == []
        assert auto_bet.market_gate_status()["reason"] == "feed_unavailable"

    def test_feed_validato_lascia_passare(self, monkeypatch, temp_db, tmp_path):
        monkeypatch.setitem(sys.modules, "adaptive_staking", None)
        _seed_value_match(quota=1.65)
        self._inietta(monkeypatch, self._feed(tmp_path, min_refreshes=1))
        placed = auto_bet.run_today_bets(stake_eur=5.0)
        assert placed and placed[0]["mode"] == "sim"
        assert auto_bet.market_gate_status()["blocked"] is False

    def test_nessuna_sorgente_blocca(self, monkeypatch, temp_db, tmp_path):
        from decision import MarketFeed
        _seed_value_match(quota=1.65)
        self._inietta(monkeypatch, MarketFeed([], state_path=tmp_path / "s.json"))
        assert auto_bet.run_today_bets(stake_eur=5.0) == []
        assert auto_bet.market_gate_status()["blocked"] is True

    def test_errore_imprevisto_e_fail_closed(self, monkeypatch, temp_db):
        def boom(*_args, **_kwargs):
            raise RuntimeError("feed esploso")

        monkeypatch.setattr("decision.feeds.feed_enabled", lambda: True)
        monkeypatch.setattr("decision.feeds.feed_from_env", boom)
        allowed, reason, identity = auto_bet._market_feed_gate()
        assert allowed is False and "non valutabile" in reason and identity == {}

    def test_feed_disattivato_non_blocca(self, monkeypatch, temp_db):
        monkeypatch.setattr("decision.feeds.feed_enabled", lambda: False)
        allowed, reason, _ = auto_bet._market_feed_gate()
        assert allowed is True and "DECISION_FEED_ENABLED" in reason


def test_sim_piazzata_con_quota_segnale(monkeypatch, temp_db):
    """SIM-only: puntata simulata con la quota del segnale, mode='sim'."""
    monkeypatch.setitem(sys.modules, "adaptive_staking", None)
    _seed_value_match(quota=1.65)
    placed = auto_bet.run_today_bets(stake_eur=5.0)
    assert len(placed) == 1
    p = placed[0]
    assert p["esito_key"] == "1" and p["price"] == 1.65 and p["stake"] == 5.0
    assert p["mode"] == "sim" and p["status"] == "SUCCESS"
    # registrata nel DB
    bets = tracker.get_bets()
    assert len(bets) == 1
    assert bets[0]["mode"] == "sim"
    assert tracker.bet_exists_open("m1", "1") is True


def test_no_duplicate_bet_on_rerun(monkeypatch, temp_db):
    _seed_value_match()
    auto_bet.run_today_bets(stake_eur=5.0)
    placed2 = auto_bet.run_today_bets(stake_eur=5.0)
    assert placed2 == []           # gia' aperta
    assert len(tracker.get_bets()) == 1


def test_skips_near_start(monkeypatch, temp_db):
    iniziata = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    _seed_value_match(quota=1.65, commence=iniziata)
    placed = auto_bet.run_today_bets(stake_eur=5.0)
    assert placed == []


def test_skips_quota_non_valida(monkeypatch, temp_db):
    _seed_value_match(quota=1.0)
    placed = auto_bet.run_today_bets(stake_eur=5.0)
    assert placed == []


def test_senza_segnali_nessuna_puntata(monkeypatch, temp_db):
    placed = auto_bet.run_today_bets(stake_eur=5.0)
    assert placed == []
    assert tracker.get_bets() == []


def test_best_rejected_ma_esito_value_viene_piazzato(monkeypatch, temp_db):
    """REGRESSION 09/09: match_analysis registra solo il best-per-EV (che
    puo' essere rejected), ma il ledger `predictions` ha lo status di OGNI
    esito. _today_value_picks legge da predictions: un match con best
    rejected ma esito value DEVE generare una puntata (prima: 0 candidati
    -> il bot non piazzava mai)."""
    monkeypatch.setitem(sys.modules, "adaptive_staking", None)
    start = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat().replace("+00:00", "Z")
    tracker.save_match("mX", ALLOWED_LEAGUE, "Osasuna", "Getafe", start)
    # best-per-EV rejected (come Derby: Draw @3.55 EV 0.151 ma non sano)
    tracker.save_analysis("mX", 1.7, 1.1, 0.52, 0.27, 0.21, 0.58, 0.151,
                          "Draw", 3.55, "Pinnacle", "rejected",
                          market_prob=0.30, market_edge=0.04)
    # esito value registrato nel ledger previsioni
    tracker.save_prediction("mX", "1X2", "Osasuna", 1.65, 0.68, 0.12,
                            market_prob=0.60, market_edge=0.08, status="value")
    picks = auto_bet._today_value_picks()
    assert len(picks) == 1
    assert picks[0]["match_id"] == "mX" and picks[0]["esito_key"] == "1"
    placed = auto_bet.run_today_bets(stake_eur=5.0)
    assert len(placed) == 1
    assert placed[0]["esito_key"] == "1"


def test_normalizes_stake_below_minimum(monkeypatch, temp_db):
    # Guardia minimo Exchange sul percorso a stake fisso (adaptive disattivo):
    # il floor e' il minimo ordine SX (1.0); sotto, nessuna puntata.
    monkeypatch.setitem(sys.modules, "adaptive_staking", None)
    _seed_value_match()
    placed = auto_bet.run_today_bets(stake_eur=0.5)
    assert placed == []


def test_auto_bet_usa_stake_adaptivo(monkeypatch, temp_db):
    """Con adaptive_staking disponibile lo stake arriva dal modulo adattivo
    (Kelly dinamico), non da stake_eur (solo fallback)."""
    import adaptive_staking
    calls = {}

    def _fake_adaptive(**kw):
        calls.update(kw)
        return {"stake": 7.5, "kelly_fraction": 0.2, "confidence_score": 0.6,
                "drawdown_factor": 1.0, "raw_stake": 7.5, "capped": False,
                "reason": "test"}

    monkeypatch.setattr(adaptive_staking, "adaptive_stake", _fake_adaptive)
    monkeypatch.setattr(adaptive_staking, "bankroll_stats",
                        lambda: {"current": 100.0, "peak": 100.0})
    _seed_value_match(quota=1.65)
    placed = auto_bet.run_today_bets(stake_eur=5.0)
    assert len(placed) == 1
    assert placed[0]["stake"] == 7.5            # stake adattivo, non 5.0
    assert calls["odds"] == 1.65                # quota del segnale


class TestCorrelationCap:
    """Correlation risk cap: gli stake di esiti correlati (stessa partita o
    stessa lega+finestra) vengono ridotti se l'esposizione del blocco supera
    il cap, per proteggere il bankroll dalla varianza condivisa."""

    def _cand(self, mid, stake, commence=None, league="Serie A", **kw):
        c = {"match_id": mid, "league": league,
             "commence": commence, "stake": stake, "esito_key": "1"}
        c.update(kw)
        return c

    def test_senza_correlazione_nessun_taglio(self):
        # Leghe diverse e kickoff lontani: nessun cap
        cands = [
            self._cand("a", 5.0, "2026-09-03T14:00:00Z", league="Serie A"),
            self._cand("b", 5.0, "2026-09-03T14:05:00Z", league="Premier League"),
            self._cand("c", 5.0, "2026-09-03T20:00:00Z", league="Serie A"),
        ]
        out = auto_bet.apply_correlation_cap(cands, bankroll=100.0)
        assert all(not c.get("corr_cap") for c in out)
        assert [c["stake"] for c in out] == [5.0, 5.0, 5.0]

    def test_stessa_lega_finestra_scala_gli_stake(self):
        # 4 esiti Serie A con kickoff ravvicinati: 4*5=20 > cap 30% di 50=15
        start = "2026-09-03T14:00:00Z"
        cands = [self._cand(f"m{i}", 5.0, start, league="Serie A")
                 for i in range(4)]
        out = auto_bet.apply_correlation_cap(cands, bankroll=50.0)
        tot = sum(c["stake"] for c in out)
        assert tot <= 15.0 + 0.01
        assert all(c.get("corr_cap") for c in out)
        # Scaling proporzionale: tutti ridotti dello stesso fattore
        assert out[0]["stake"] == out[1]["stake"]

    def test_stessa_partita_sempre_correlata(self):
        # 1X2 + Over sulla stessa partita (match_id uguale): correlati
        cands = [
            self._cand("x", 5.0, "2026-09-03T14:00:00Z", league="Serie A",
                       esito_key="1"),
            self._cand("x", 5.0, "2026-09-03T14:00:00Z", league="Serie A",
                       esito_key="Over 2.5"),
        ]
        out = auto_bet.apply_correlation_cap(cands, bankroll=20.0)
        assert sum(c["stake"] for c in out) <= 6.0 + 0.01  # 30% di 20

    def test_blocchi_temporali_disgiunti_niente_cap(self):
        # Stessa lega ma kickoff a 3h di distanza: indipendenti
        cands = [
            self._cand("a", 5.0, "2026-09-03T14:00:00Z", league="Serie A"),
            self._cand("b", 5.0, "2026-09-03T17:00:00Z", league="Serie A"),
        ]
        out = auto_bet.apply_correlation_cap(cands, bankroll=20.0)
        assert not any(c.get("corr_cap") for c in out)

    def test_cap_rispetta_il_ranking_ev(self):
        # Stake diversi: scaling proporzionale (l'esito a stake piu' alto
        # resta quello piu' alto)
        cands = [
            self._cand("a", 8.0, "2026-09-03T14:00:00Z", league="Serie A"),
            self._cand("b", 4.0, "2026-09-03T14:05:00Z", league="Serie A"),
            self._cand("c", 4.0, "2026-09-03T14:10:00Z", league="Serie A"),
        ]
        out = auto_bet.apply_correlation_cap(cands, bankroll=40.0)
        assert out[0]["stake"] > out[1]["stake"]  # ranking preservato
        assert sum(c["stake"] for c in out) <= 12.0 + 0.01

    def test_run_today_bets_sim_usa_il_cap(self, monkeypatch, temp_db):
        """Flusso completo SIM: 4 value della stessa lega ravvicinate -> gli
        stake passano dal correlation cap prima di essere salvati."""
        monkeypatch.setitem(sys.modules, "adaptive_staking", None)
        start = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()\
            .replace("+00:00", "Z")
        for i in range(4):
            _seed_value_match(mid=f"sim{i}", home=f"Home{i}", away=f"Away{i}",
                              esito="1", quota=1.65, status="value",
                              commence=start)
        placed = auto_bet.run_today_bets(stake_eur=5.0)
        assert len(placed) == 4
        assert all(p["mode"] == "sim" for p in placed)
        assert all(p["stake"] > 0 for p in placed)


class TestTotalExposureCap:
    """Cap di portafoglio: l'esposizione TOTALE del giorno non deve superare
    `TOTAL_EXPOSURE_CAP_PCT` del bankroll (varianza additiva tra pick
    indipendenti).

    ⚠️ Le attese sono DERIVATE dalla costante di produzione, mai scritte a
    mano: il 14/10/2026 il cap e' passato 40% -> 60% e quattro di questi test
    sono scaduti pur non essendoci alcuna regressione. Derivare li rende
    sensibili al COMPORTAMENTO (che e' l'oggetto del test) e immuni al valore.
    """
    PCT = auto_bet.TOTAL_EXPOSURE_CAP_PCT

    def _cand(self, mid, stake, league="Serie A"):
        return {"match_id": mid, "league": league, "stake": stake,
                "commence": "2026-09-03T14:00:00Z", "esito_key": "1"}

    def test_sotto_il_cap_nessun_taglio(self):
        cands = [self._cand("a", 3.0), self._cand("b", 3.0)]  # 6 <= cap di 100
        out = auto_bet.apply_total_exposure_cap(cands, bankroll=100.0)
        assert all(not c.get("total_cap") for c in out)
        assert [c["stake"] for c in out] == [3.0, 3.0]

    def test_sopra_il_cap_scala_proporzionalmente(self):
        # 5 cands da 10 = 50 > cap di 50 -> scala proporzionale, ranking
        # preservato (stake identici fra i due candidati di testa).
        bankroll = 50.0
        cap = bankroll * self.PCT
        cands = [self._cand(f"m{i}", 10.0, league=f"Lega{i}") for i in range(5)]
        assert 5 * 10.0 > cap      # il test misura davvero il taglio
        out = auto_bet.apply_total_exposure_cap(cands, bankroll=bankroll)
        tot = sum(c["stake"] for c in out)
        assert tot <= cap + 0.01
        assert all(c.get("total_cap") for c in out)
        assert out[0]["stake"] == out[1]["stake"]  # proporzionale

    def test_no_op_con_pochi_candidati(self):
        cands = [self._cand("a", 5.0)]
        out = auto_bet.apply_total_exposure_cap(cands, bankroll=100.0)
        assert out == cands and not cands[0].get("total_cap")

    def test_cap_sottrae_esposizione_gia_piazzata(self):
        """24/7: il cap totale e' RIMANENTE — conta anche le puntate gia'
        piazzate nei giri precedenti (residuo >= 6: nessun taglio)."""
        cands = [self._cand("a", 3.0), self._cand("b", 3.0)]
        out = auto_bet.apply_total_exposure_cap(
            cands, bankroll=100.0, already_placed=100.0 * self.PCT - 6.0)
        assert all(not c.get("total_cap") for c in out)
        assert [c["stake"] for c in out] == [3.0, 3.0]

    def test_cap_esaurito_zero(self):
        """Budget del giorno gia' esaurito (already_placed >= cap): nessun
        nuovo ordine — gli stake vengono azzerati."""
        cands = [self._cand("a", 3.0), self._cand("b", 3.0)]
        out = auto_bet.apply_total_exposure_cap(
            cands, bankroll=100.0, already_placed=100.0 * self.PCT)
        assert all(c["stake"] == 0.0 for c in out)
        assert all(c.get("total_cap") for c in out)

    def test_cap_ridotto_dal_residuo(self):
        """Cap residuo ridotto da quanto gia' piazzato: 25 > cap_residuo=5 ->
        scala proporzionale, ranking preservato."""
        bankroll = 50.0
        cands = [self._cand(f"m{i}", 5.0, league=f"Lega{i}") for i in range(5)]
        out = auto_bet.apply_total_exposure_cap(
            cands, bankroll=bankroll,
            already_placed=bankroll * self.PCT - 5.0)
        tot = sum(c["stake"] for c in out)
        assert tot <= 5.0 + 0.01
        assert all(c.get("total_cap") for c in out)
        assert out[0]["stake"] == out[1]["stake"]  # proporzionale

    def test_flusso_sim_applica_il_cap_totale(self, monkeypatch, temp_db):
        """9 value in 9 leghe diverse (nessuna correlazione): stake fisso 8
        -> totale 72 > cap di 100 -> gli stake vengono ridotti dal cap
        esposizione totale prima del salvataggio."""
        monkeypatch.setitem(sys.modules, "adaptive_staking", None)
        # Il gate di lega e' fuori scope in QUESTO test (che isola il cap
        # TOTALE di portafoglio): le 9 leghe inventate qui sotto servono a
        # evitare il correlation cap, non a rispettare la strategia.
        monkeypatch.setattr(value_filter, "league_allowed", lambda lg: True)
        start = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()\
            .replace("+00:00", "Z")
        for i in range(9):
            _seed_value_match(mid=f"tc{i}", home=f"Home{i}", away=f"Away{i}",
                              esito="1", quota=1.65, status="value",
                              commence=start)
        # Leghe diverse per match: evita il correlation cap (che agirebbe
        # prima) e isola il cap TOTALE.
        conn = tracker._get_conn()
        conn.execute("UPDATE matches SET league = 'Lega' || rowid WHERE id LIKE 'tc%'")
        conn.commit()
        conn.close()
        placed = auto_bet.run_today_bets(stake_eur=8.0)
        assert len(placed) == 9
        tot = sum(p["stake"] for p in placed)
        # Il cap e' rispettato a meno dell'arrotondamento al centesimo di
        # OGNI stake (9 x 0,005): senza la tolleranza derivata il test
        # fallirebbe per 0,02 USDC di resto.
        assert tot <= 100.0 * self.PCT + 0.01 + 0.005 * len(placed)
        assert all(p["stake"] > 0 for p in placed)   # ridotti, non azzerati


class TestFlatStake:
    """Flat-stake (09/09): AUTO_BET_STAKE_MODE=flat -> 1 USDC fisso per
    ogni segnale +EV, con risk cap a UNITA' INTERE (il minimo ordine SX
    Bet e' 1 USDC: le frazioni non sono piazzabili e rialzarle al floor
    sforerebbe i cap)."""

    def _cand(self, mid, ev, league="LegaA", commence="2026-09-09T14:00:00Z",
              **kw):
        c = {"match_id": mid, "league": league, "commence": commence,
             "stake": 1.0, "best_ev": ev, "esito_key": "1"}
        c.update(kw)
        return c

    def test_flat_default_unit_piu_cap(self):
        # Bankroll 12.28 USDC: cap correlazione 30% = 3.68 -> max 3 unita'
        # per blocco (vince sui cap totali, che oggi sono piu' alti).
        start = "2026-09-09T14:00:00Z"
        cands = [self._cand(f"m{i}", ev=0.30 - 0.03 * i, league="Serie A",
                            commence=start) for i in range(5)]
        out = auto_bet.apply_flat_budget(cands, bankroll=12.28)
        placed = [c for c in out if c.get("stake", 0) > 0]
        assert len(placed) == 3  # floor(3.68/1) unita' per blocco correlato
        assert all(c["stake"] == 1.0 for c in placed)
        # Gli esuberi (EV piu' basso) sono azzerati e marcati
        dropped = [c for c in out if not c.get("stake")]
        assert len(dropped) == 2 and all(c.get("corr_cap") for c in dropped)

    def test_flat_esposizione_totale_unita_intere(self):
        # 5 segni in 5 leghe diverse (nessun blocco correlato): il bankroll e'
        # scelto perche' il cap TOTALE valga ESATTAMENTE 4 unita' -> cade
        # l'EV piu' basso. Bankroll derivato: 4 / cap_pct.
        bankroll = 4.0 / auto_bet.TOTAL_EXPOSURE_CAP_PCT
        cands = [self._cand(f"m{i}", ev=0.30 - 0.03 * i,
                            league=f"Lega{i}") for i in range(5)]
        out = auto_bet.apply_flat_budget(cands, bankroll=bankroll)
        placed = [c for c in out if c.get("stake", 0) > 0]
        assert len(placed) == 4
        assert all(c["stake"] == 1.0 for c in placed)
        # il tagliato e' quello a best_ev minore
        dropped = [c for c in out if not c.get("stake")]
        assert len(dropped) == 1 and dropped[0]["match_id"] == "m4"
        assert dropped[0].get("total_cap")

    def test_flat_cap_residuo_sottrae_gia_piazzato(self):
        # bankroll scelto perche' il cap valga 4.0; gia' piazzati 3 -> residuo
        # 1.0 -> 1 solo segno
        bankroll = 4.0 / auto_bet.TOTAL_EXPOSURE_CAP_PCT
        cands = [self._cand(f"m{i}", ev=0.30 - 0.03 * i,
                            league=f"Lega{i}") for i in range(3)]
        out = auto_bet.apply_flat_budget(cands, bankroll=bankroll,
                                         already_placed=3.0)
        placed = [c for c in out if c.get("stake", 0) > 0]
        assert len(placed) == 1 and placed[0]["match_id"] == "m0"

    def test_flat_sotto_i_cap_tutti_piazzati(self):
        cands = [self._cand("a", ev=0.2, league="LegaA"),
                 self._cand("b", ev=0.1, league="LegaB")]
        out = auto_bet.apply_flat_budget(cands, bankroll=100.0)
        assert all(c.get("stake", 0) == 1.0 for c in out)
        assert not any(c.get("total_cap") or c.get("corr_cap") for c in out)

    def test_flat_sim_piazzata_1_euro_a_segno(self, monkeypatch, temp_db):
        """Flat mode in SIM: ogni segnale value viene piazzato a €1.00
        (niente Kelly, niente stake fisso dal parametro)."""
        monkeypatch.setitem(sys.modules, "adaptive_staking", None)
        monkeypatch.setattr(auto_bet, "STAKE_MODE", "flat")
        _seed_value_match(mid="m1", quota=1.65)
        _seed_value_match(mid="m2", home="Bari", away="Crotone", esito="2",
                          quota=1.70)
        placed = auto_bet.run_today_bets(stake_eur=5.0)
        assert len(placed) == 2
        assert all(p["stake"] == 1.0 for p in placed)
        assert all(p["mode"] == "sim" for p in placed)

    def test_flat_sotto_minimo_nessuna_puntata(self, monkeypatch, temp_db):
        """Flat stake sotto il minimo ordine (env < 1.0): nessuna puntata."""
        monkeypatch.setitem(sys.modules, "adaptive_staking", None)
        monkeypatch.setattr(auto_bet, "STAKE_MODE", "flat")
        monkeypatch.setattr(auto_bet, "FLAT_STAKE_EUR", 0.5)
        _seed_value_match(quota=1.65)
        placed = auto_bet.run_today_bets(stake_eur=5.0)
        assert placed == []


class TestClvWiring:
    """Il CLV storico (battere la closing line) deve arrivare ad
    adaptive_stake come has_clv_positive: edge confermato = stake piu' alto."""

    def _run_con_stub(self, monkeypatch, temp_db, avg_clv):
        import adaptive_staking
        calls = {}

        def _fake_adaptive(**kw):
            calls.update(kw)
            return {"stake": 4.0, "kelly_fraction": 0.2,
                    "confidence_score": 0.5, "drawdown_factor": 1.0,
                    "raw_stake": 4.0, "capped": False, "reason": "test"}

        monkeypatch.setattr(adaptive_staking, "adaptive_stake", _fake_adaptive)
        monkeypatch.setattr(adaptive_staking, "bankroll_stats",
                            lambda: {"current": 100.0, "peak": 100.0})
        _seed_value_match()
        auto_bet.run_today_bets(stake_eur=5.0)
        return calls

    def test_passa_has_clv_positive(self, monkeypatch, temp_db):
        """has_clv_positive arriva ad adaptive_stake (bool) anche senza
        storico CLV (None -> False, mai assente)."""
        calls = self._run_con_stub(monkeypatch, temp_db, avg_clv=0.0)
        assert "has_clv_positive" in calls
        assert calls["has_clv_positive"] is False

    def test_clv_positivo_vero(self, monkeypatch, temp_db):
        """Con una riga clv_history a CLV positivo (signal_quota >
        closing_quota) il flag passa True."""
        conn = tracker._get_conn()
        conn.execute(
            "INSERT INTO clv_history (match_id, esito, signal_quota, "
            "closing_quota, updated_at) VALUES ('m1','1', 2.20, 2.00, ?)",
            (datetime.now(timezone.utc).isoformat(),))
        conn.commit()
        conn.close()
        calls = self._run_con_stub(monkeypatch, temp_db, avg_clv=0.1)
        assert calls["has_clv_positive"] is True


class TestRumoreLog:
    """Pulizia dei log (21/09/2026).

    Il giro di `auto_bet` gira OGNI 60s: a INFO ripeteva la CONFIGURAZIONE
    (strategia e corsie multi-mercato) a ogni ciclo — insieme ai log di
    APScheduler era la voce piu' rumorosa del container e affogava i
    messaggi operativi (puntate, settlement, scarti).

    Regola: la configurazione sta a DEBUG (resta leggibile con `/autobet`),
    e a giro VUOTO esce UNA riga di heartbeat. Dal log si deve vedere che il
    job gira, non leggere 6 righe identiche al minuto.
    """

    @staticmethod
    def _messaggi(caplog, level):
        return [r.getMessage() for r in caplog.records if r.levelno == level]

    def test_giro_vuoto_non_ripete_la_configurazione(self, temp_db, caplog):
        with caplog.at_level(logging.DEBUG, logger="auto_bet"):
            auto_bet.run_today_bets(stake_eur=5.0)
        infos = self._messaggi(caplog, logging.INFO)
        assert not any("strategia favoriti netti" in m for m in infos)
        assert not any("corsie multi-mercato" in m for m in infos)
        # L'heartbeat invece c'e' sempre: dice che il giro e' girato davvero.
        assert any("nessuna puntata" in m or "puntate piazzate" in m
                   for m in infos)

    def test_la_configurazione_resta_leggibile_a_debug(self, temp_db, caplog):
        with caplog.at_level(logging.DEBUG, logger="auto_bet"):
            auto_bet.run_today_bets(stake_eur=5.0)
        debug = self._messaggi(caplog, logging.DEBUG)
        assert any("strategia favoriti netti" in m for m in debug)
        assert any("corsie multi-mercato" in m for m in debug)