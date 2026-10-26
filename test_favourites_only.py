"""Tripwire della STRATEGIA SOLO FAVORITI (11/09/2026).

Direttiva del proprietario: vietato tassativamente puntare su squadre
sfavorite o quote alte (anche sotto a 3.0), per contenere il rischio di
bancarotta. I test bloccano ogni regressione che riapra il gate:

1. value_filter: cap quota 2.00 + esito favorito di mercato;
2. fixture_engine: un match senza favorito netto resta "rejected" e NON
   scrive previsioni (nessun esito sfavorito finisce nel ledger);
3. sx_signals: un book senza favorito giocabile non produce segnali;
4. auto_bet: una riga storica di segnale a quota alta non diventa ordine.
"""
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import tracker


@pytest.fixture()
def temp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        monkeypatch.setattr(tracker, "DB_PATH", db_path)
        tracker.init_db()
        yield db_path


@pytest.fixture(autouse=True)
def _isolate_daily_stop(tmp_path, monkeypatch):
    """Lo stop-loss giornaliero usa un file temporaneo (mai il volume reale)."""
    import auto_bet
    monkeypatch.setattr(auto_bet, "DAILY_STOP_FILE", tmp_path / "daily_stop.json")


def _match(home_price, draw_price, away_price, mid="fx1"):
    return {
        "id": mid,
        "home_team": "Roma", "away_team": "Empoli",
        "commence_time": "2026-09-12T18:45:00Z",
        "bookmakers": [{"title": "BookA", "markets": [
            {"key": "h2h", "outcomes": [
                {"name": "Roma", "price": home_price},
                {"name": "Draw", "price": draw_price},
                {"name": "Empoli", "price": away_price}]}]}],
    }


def _analyze(monkeypatch, match, mid):
    """Esegue _analyze_match con i confini esterni stubbati."""
    import fixture_engine
    import value_filter
    monkeypatch.setattr("fixture_engine.expected_goals", lambda h, a: (1.8, 1.2))
    # Modello col favorito DENTRO la fascia EV giocabile: la probabilita' si
    # deriva dal punto di mezzo fra `EV_MIN` e `EV_MAX` sulla quota 1.85. Un
    # valore fisso (0.62 fino al 24/10) dava EV +14,7%, cioe' SOPRA il tetto,
    # e il match usciva `rejected` per "EV troppo alto": il test avrebbe
    # misurato il gate EV invece della strategia solo-favoriti.
    _quota = 1.85
    _p = round((1 + (value_filter.EV_MIN + value_filter.EV_MAX) / 2) / _quota, 4)
    monkeypatch.setattr("fixture_engine.prob_1x2",
                        lambda lh, la: (_p, round(1 - _p - 0.15, 4), 0.15))
    # Blend deterministico: prob finale = prob del modello (il devig del
    # mercato resta reale per market_prob/market_edge).
    monkeypatch.setattr("fixture_engine.adjusted_probability",
                        lambda model_prob, market_prob, price, league="",
                        model_samples=0: model_prob)
    monkeypatch.setattr("fixture_engine.save_clv", lambda *a, **k: None)
    monkeypatch.setattr("fixture_engine.get_analysis_for_match", lambda m: None)
    monkeypatch.setattr("fixture_engine.record_snapshot", lambda *a, **k: None)
    monkeypatch.setattr("fixture_engine.detect_rlm", lambda *a, **k: None)
    monkeypatch.setattr("fixture_engine.detect_steam", lambda *a, **k: None)
    saved = {}
    monkeypatch.setattr("fixture_engine.save_analysis",
                        lambda *a, **k: saved.update({"args": a, "kwargs": k}))
    preds = []
    monkeypatch.setattr("fixture_engine.save_prediction",
                        lambda *a, **k: preds.append((a, k)))
    status = fixture_engine._analyze_match(mid, match, "Roma", "Empoli", "Premier League")
    return status, saved, preds


class TestFixtureEngineGate:
    def test_match_senza_favorito_netto_rejected(self, temp_db, monkeypatch):
        """Quote tutte sopra il cap (2.60/3.30/2.70): nessun segnale.

        Prima dell'11/09 il match generava candidati anche su X/2 a quota
        alta; ora non entra nulla, nemmeno nel ledger.
        """
        status, saved, preds = _analyze(
            monkeypatch, _match(2.60, 3.30, 2.70), "fx_no_fav")
        assert status == "rejected"
        assert preds == []
        # status propagato alla riga di analisi (args[11] = status)
        assert saved["args"][11] == "rejected"

    def test_favorito_netto_analizzato(self, temp_db, monkeypatch):
        """Favorito a 1.85 (prob. di mercato > 50%): il match resta giocabile.

        La quota sta FUORI dalla fascia morta 1.50-1.80 esclusa il 25/10: con
        1.65 il match resterebbe `rejected` per la banda, e questo test
        misurerebbe quel gate invece della strategia solo-favoriti.
        """
        status, saved, preds = _analyze(
            monkeypatch, _match(1.85, 3.75, 4.95), "fx_fav")
        assert status in ("value", "strong_value", "no_value")
        # Nel ledger entra SOLO il favorito (nessun esito sfavorito:
        # l'esito e' il nome della squadra di casa, come dal feed bookmaker).
        esiti = [a[2] for a, _ in preds]
        assert set(esiti) <= {"Roma"}
        assert all(a[3] <= 2.00 for a, _ in preds)

    def test_sfavorita_fuori_dal_ledger(self, temp_db, monkeypatch):
        """Favorito 1.85: X e 2 non compaiono tra le previsioni."""
        status, _, preds = _analyze(
            monkeypatch, _match(1.85, 3.75, 4.95), "fx_fav2")
        assert all(a[3] <= 2.00 for a, _ in preds)


class TestSxSignalsGate:
    def test_book_senza_favorito_nessun_segnale(self, temp_db, monkeypatch):
        """Order book con quote [2.5, 3.33, 3.33]: fuori strategia -> 0 segnali."""
        import sx_signals
        from execution_engine import SX_PROB_SCALE
        from test_sx_signals import FakeSxProvider, _raw_markets

        class NoFavouriteProvider(FakeSxProvider):
            """Book senza favorito giocabile (tutti gli esiti > 2.00)."""

            def _get(self, path, params=None):
                if path == "orderbook-v3/snapshot":
                    p = {"mkt1": 2.50, "mkt2": 3.3333,
                         "mkt3": 3.3333}[(params or {}).get("marketHash")]
                    return {"data": {
                        "outcomeOne": [{"percentageOdds":
                                        int(SX_PROB_SCALE / p),
                                        "size": 10 * 10 ** 6}],
                        "outcomeTwo": [{"percentageOdds":
                                        int(SX_PROB_SCALE /
                                            (1.0 / (1.0 - 1.0 / p))),
                                        "size": 10 * 10 ** 6}],
                    }}
                return super()._get(path, params)

        monkeypatch.setattr(sx_signals, "expected_goals", lambda h, a: (1.6, 1.1))
        monkeypatch.setattr(sx_signals, "prob_1x2", lambda lh, la: (0.45, 0.28, 0.27))
        monkeypatch.setattr(sx_signals, "adjusted_probability",
                            lambda model_prob, market_prob, price, league=None:
                            model_prob)
        saved = sx_signals.scan(provider=NoFavouriteProvider(_raw_markets()))
        assert saved == []
        conn = tracker._get_conn()
        n = conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
        conn.close()
        assert n == 0


class TestStakeCapSevero:
    """Cap per singola bet (11/09/2026): le quote corte dei favoriti fanno
    calcolare al Kelly frazioni maggiori, quindi 1% (value/moderate) e 2%
    (strong_value) sono la seconda barriera al rischio di bancarotta."""

    def test_tripwire_cap(self):
        import adaptive_staking as st
        assert st.MAX_STAKE_PCT <= 0.01
        assert st.MAX_STAKE_PCT_STRONG <= 0.02

    def test_cap_severo_attivo_di_default(self):
        import auto_bet
        assert auto_bet.cap_hard_active() is True
        assert auto_bet.MIN_STAKE_EUR > 0
        assert "CAP SEVERO" in auto_bet.hard_cap_skip_message(0.38, 38.0)

    def test_cap_severo_blocca_gli_ordini_con_wallet_piccolo(self, temp_db,
                                                              monkeypatch):
        """Wallet 38 USDC: cap 1% = 0.38 USDC, sotto il minimo ordine (1
        USDC). Col cap severo nessun ordine parte (fail-closed) invece di
        piazzare 1 USDC = 2.6% del bankroll."""
        import auto_bet
        import adaptive_staking
        start = (datetime.now(timezone.utc) + timedelta(hours=3)) \
            .isoformat().replace("+00:00", "Z")
        tracker.save_match("cap1", "Premier League", "Osasuna", "Getafe", start)
        tracker.save_prediction("cap1", "1X2", "Osasuna", 1.65, 0.62, 0.02,
                                market_prob=0.60, market_edge=0.05,
                                status="value")
        monkeypatch.setattr(auto_bet, "_execution_mode",
                            lambda allow_sim=True: "live")
        monkeypatch.setattr(
            auto_bet, "_live_wallet_snapshot",
            lambda: {"available": 38.0, "exposure": 0.0, "equity": 38.0})
        monkeypatch.setattr(
            adaptive_staking, "adaptive_stake",
            lambda **kw: {"stake": round(kw["bankroll"] * 0.01, 2),
                          "reason": "cap 1%", "capped": True})

        def _boom(*a, **k):
            raise AssertionError("nessun ordine col cap severo")

        monkeypatch.setattr(auto_bet, "_live_fill", _boom)
        assert auto_bet.run_today_bets(stake_eur=5.0) == []
        assert tracker.get_bets() == []

    def test_stake_cappato_per_tier(self):
        import adaptive_staking as st
        bankroll = 1000.0
        kw = dict(prob=0.70, odds=1.65, market_edge=0.10,
                  ml_confidence=0.9, has_clv_positive=True,
                  peak_bankroll=bankroll)
        strong = st.adaptive_stake(bankroll, status="strong_value", **kw)
        value = st.adaptive_stake(bankroll, status="value", **kw)
        moderate = st.adaptive_stake(bankroll, status="moderate", **kw)
        assert strong["stake"] <= 20.0 + 1e-6      # 2% di 1000
        assert value["stake"] <= 10.0 + 1e-6       # 1% di 1000
        assert moderate["stake"] <= 10.0 + 1e-6    # mai piu' dei value
        assert strong["capped"] and value["capped"]


class TestAutoBetDefenceInDepth:
    def test_riga_storica_quota_alta_non_ordina(self, temp_db, monkeypatch):
        """Difesa in profondita': una prediction a quota 2.10 con status
        'value' (scritta prima del cambio strategia) NON diventa un ordine."""
        import auto_bet
        start = (datetime.now(timezone.utc) + timedelta(hours=3)) \
            .isoformat().replace("+00:00", "Z")
        tracker.save_match("old1", "Serie A", "Osasuna", "Getafe", start)
        tracker.save_prediction("old1", "1X2", "Osasuna", 2.10, 0.55, 0.08,
                                market_prob=0.45, market_edge=0.07,
                                status="value")
        assert auto_bet._today_value_picks() == []
        assert auto_bet.run_today_bets(stake_eur=5.0) == []
        assert tracker.get_bets() == []

    def test_prediction_su_prob_bassa_non_ordina(self, temp_db):
        """Quota entro il cap ma mercato che la considera sfavorita (45%):
        non e' un favorito netto -> nessun ordine."""
        import auto_bet
        start = (datetime.now(timezone.utc) + timedelta(hours=3)) \
            .isoformat().replace("+00:00", "Z")
        tracker.save_match("old2", "Serie A", "Osasuna", "Getafe", start)
        tracker.save_prediction("old2", "1X2", "Osasuna", 1.75, 0.50, 0.03,
                                market_prob=0.45, market_edge=0.05,
                                status="moderate")
        assert auto_bet._today_value_picks() == []
