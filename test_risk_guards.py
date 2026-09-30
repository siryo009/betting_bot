"""Test dei guardrail di rischio aggiunti l'11/09/2026.

1. Quota minima: fascia favoriti 1.30-1.80 (ODDS_MIN 1.30).
2. Filtro di edge: +2pp minimo vs mercato (MARKET_EDGE_MIN), +4pp strong
   (direttiva del proprietario, 21/09/2026: era +3pp/+5pp).
3. Stop-loss giornaliero: -5% dal bankroll di inizio giornata -> puntate
   bloccate per 24h (stato persistente sul volume).
4. Filtro liquidita' SX Bet: niente segnali/ordini su mercati sottili
   (rischio slippage su un exchange).
"""
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import auto_bet
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
    monkeypatch.setattr(auto_bet, "DAILY_STOP_FILE", tmp_path / "daily_stop.json")


# ---------------------------------------------------------------------------
# 1-2. Fascia quota + edge minimo
# ---------------------------------------------------------------------------

class TestFasciaFavoriti:
    def test_odmins_e_edge_tripwire(self):
        import value_filter as vf
        from market_calib import (MARKET_EDGE_MIN, MARKET_EDGE_MODERATE,
                                  MARKET_EDGE_STRONG)
        assert vf.ODDS_MIN == 1.30
        assert vf.ODDS_MAX == 1.80         # favoriti netti (niente "pantano")
        # Direttiva 21/09/2026: soglia di edge abbassata a +2pp (strong +4pp)
        # per aumentare il volume di pick. Uguaglianza ESATTA di proposito:
        # un ulteriore abbassamento e' una decisione di strategia, non un
        # dettaglio — va tracciata in AGENTS.md, non fatto in silenzio.
        assert MARKET_EDGE_MIN == 0.02     # +2pp minimo vs mercato
        assert MARKET_EDGE_MODERATE == 0.02
        assert MARKET_EDGE_STRONG == 0.04  # +4pp per strong_value

    def test_quota_sotto_il_minimo_bocciata(self):
        from value_filter import is_sane
        ok, reason = is_sane(0.80, 1.29, 0.05, market_prob=0.75)
        assert not ok and "quota troppo bassa" in reason
        # 1.31 in fascia: passa (edge 5pp, EV 4.8%)
        ok, _ = is_sane(0.80, 1.31, 0.048, market_prob=0.75)
        assert ok

    def test_edge_sotto_2pp_bocciato(self):
        from value_filter import is_sane
        # Direttiva 21/09/2026: +2pp minimo vs mercato (il fallback
        # DEFAULT_LEAGUE_STRATEGY e' allineato a +2pp).
        ok, reason = is_sane(0.61, 1.65, 0.03, market_prob=0.60)   # +1pp
        assert not ok and "non batte il mercato" in reason
        # +2pp: OK (con la soglia a +3pp questo caso era respinto)
        ok, reason = is_sane(0.62, 1.65, 0.03, market_prob=0.60)   # +2pp
        assert ok, reason
        # +4pp (fascia strong_value): OK
        ok, _ = is_sane(0.64, 1.65, 0.05, market_prob=0.60)
        assert ok


# ---------------------------------------------------------------------------
# 3. Stop-loss giornaliero
# ---------------------------------------------------------------------------

class TestStopLossGiornaliero:
    def test_baseline_e_trigger_a_meno_5pct(self):
        r = auto_bet.check_daily_stop(100.0)
        assert r["stopped"] is False and r["start_bankroll"] == 100.0
        # -4%: nessun blocco
        r = auto_bet.check_daily_stop(96.0)
        assert r["stopped"] is False
        # -6%: trigger + blocco 24h
        r = auto_bet.check_daily_stop(94.0)
        assert r["stopped"] is True and r["just_triggered"] is True
        until = datetime.fromisoformat(r["until"])
        delta_h = (until - datetime.now(timezone.utc)).total_seconds() / 3600
        assert 23 <= delta_h <= 24.1

    def test_blocco_persiste_anche_se_il_bankroll_risale(self):
        auto_bet.check_daily_stop(100.0)
        auto_bet.check_daily_stop(90.0)          # trigger
        r = auto_bet.check_daily_stop(130.0)     # risalito
        assert r["stopped"] is True              # il blocco resta fino a scadenza
        st = auto_bet.daily_stop_status()
        assert st["stopped"] is True and st["until"]

    def test_si_riarma_il_giorno_dopo(self):
        now = datetime.now(timezone.utc)
        auto_bet.DAILY_STOP_FILE.write_text(json.dumps({
            "day": (now - timedelta(days=2)).date().isoformat(),
            "start_bankroll": 100.0,
            "stopped_until": (now - timedelta(hours=1)).isoformat()}))
        r = auto_bet.check_daily_stop(70.0)
        assert r["stopped"] is False and r["start_bankroll"] == 70.0

    def test_clear_riattiva(self):
        auto_bet.check_daily_stop(100.0)
        auto_bet.check_daily_stop(90.0)
        assert auto_bet.daily_stop_status()["stopped"] is True
        auto_bet.clear_daily_stop()
        assert auto_bet.daily_stop_status()["stopped"] is False

    def test_basis_dichiarato_nel_messaggio(self):
        """Il messaggio dice su QUALE valore e' misurata la perdita: in LIVE
        e' l'equity del wallet (disponibile + in gioco), non la cassa libera
        (fix del 15/09, `basis` passato dal chiamante)."""
        auto_bet.check_daily_stop(35.98, basis="equity wallet")
        r = auto_bet.check_daily_stop(32.0, basis="equity wallet")
        assert r["stopped"] is True
        assert "equity wallet" in auto_bet.daily_stop_status()["reason"]

    def test_fail_open_su_errore_di_scrittura(self, tmp_path, monkeypatch):
        """Un errore I/O non deve mai bloccare le puntate (fail-open)."""
        blocker = tmp_path / "not_a_dir"
        blocker.write_text("x")
        monkeypatch.setattr(auto_bet, "DAILY_STOP_FILE", blocker / "x.json")
        r = auto_bet.check_daily_stop(100.0)
        assert r["stopped"] is False

    def test_basi_diverse_non_si_confrontano(self):
        """Regressione 21/09/2026: l'EQUITY del wallet (33.5535) e la cassa
        simulata (20.00) NON vanno MAI confrontate.

        Sequenza reale dell'incidente: il riferimento del giorno era stato
        preso dall'equity del wallet (33.5535); una lettura fallita del wallet
        fece ripiegare il giro sulla cassa simulata (20.00), che confrontata
        col riferimento produsse un -40.4% inesistente e un blocco di 24h col
        wallet INTATTO (verificato: 33.5535 USDC disponibili, 0 ordini).
        """
        auto_bet.check_daily_stop(33.5535, basis="equity wallet",
                                  basis_key="live_equity")
        # Lettura dalla cassa (wallet illeggibile): NESSUN trigger.
        r = auto_bet.check_daily_stop(20.0, basis="cassa", basis_key="cassa")
        assert r["stopped"] is False
        assert r.get("basis_mismatch") is True
        # Il riferimento del giorno resta quello dell'equity, intatto.
        auto_bet.check_daily_stop(33.5535, basis="equity wallet",
                                  basis_key="live_equity")
        st = auto_bet.daily_stop_status()
        assert st["stopped"] is False
        assert st["start_bankroll"] == pytest.approx(33.5535)
        assert st["basis_key"] == "live_equity"

    def test_base_piu_autorevole_riarma_il_riferimento(self):
        """Primo giro del giorno con la cassa (wallet non ancora leggibile),
        poi arriva il wallet reale: il riferimento si ri-arma sul valore
        buono, senza mai confrontare le due basi."""
        auto_bet.check_daily_stop(20.0, basis="cassa", basis_key="cassa")
        r = auto_bet.check_daily_stop(33.5535, basis="equity wallet",
                                      basis_key="live_equity")
        assert r["stopped"] is False and r.get("basis_changed") is True
        st = auto_bet.daily_stop_status()
        assert st["start_bankroll"] == pytest.approx(33.5535)
        assert st["basis_key"] == "live_equity"

    def test_stessa_base_triggera_ancora(self):
        """Nessuna regressione: sulla STESSA base il blocco scatta come prima."""
        auto_bet.check_daily_stop(35.98, basis="equity wallet",
                                  basis_key="live_equity")
        r = auto_bet.check_daily_stop(32.0, basis="equity wallet",
                                      basis_key="live_equity")
        assert r["stopped"] is True and r["just_triggered"] is True

    def test_base_meno_autorevole_non_declassa(self):
        """Una base meno autorevole non ri-arma il riferimento (mai una
        regressione da equity a cassa dentro lo stesso giorno)."""
        auto_bet.check_daily_stop(33.5535, basis="equity wallet",
                                  basis_key="live_equity")
        auto_bet.check_daily_stop(20.0, basis="cassa", basis_key="cassa")
        assert auto_bet.daily_stop_status()["start_bankroll"] == pytest.approx(33.5535)


class TestStopLossBloccaIlGiro:
    def test_run_today_bets_non_piazza_con_stop_attivo(self, temp_db):
        start = (datetime.now(timezone.utc) + timedelta(hours=3)) \
            .isoformat().replace("+00:00", "Z")
        tracker.save_match("stop1", "Premier League", "Osasuna", "Getafe", start)
        tracker.save_prediction("stop1", "1X2", "Osasuna", 1.65, 0.62, 0.08,
                                market_prob=0.60, market_edge=0.07,
                                status="value")
        auto_bet.check_daily_stop(100.0)
        auto_bet.check_daily_stop(90.0)          # trigger
        placed = auto_bet.run_today_bets(stake_eur=5.0)
        assert placed == []
        assert tracker.get_bets() == []

    def test_giro_normale_senza_stop(self, temp_db):
        """Controprova: senza stop lo stesso candidato viene piazzato (SIM)."""
        start = (datetime.now(timezone.utc) + timedelta(hours=3)) \
            .isoformat().replace("+00:00", "Z")
        tracker.save_match("stop2", "Premier League", "Osasuna", "Getafe", start)
        tracker.save_prediction("stop2", "1X2", "Osasuna", 1.65, 0.62, 0.08,
                                market_prob=0.60, market_edge=0.07,
                                status="value")
        placed = auto_bet.run_today_bets(stake_eur=5.0)
        assert len(placed) == 1


# ---------------------------------------------------------------------------
# 4. Liquidita' SX Bet
# ---------------------------------------------------------------------------

class TestLiquiditaSx:
    def test_soglie_liquidita_configurate(self):
        """Taratura 21/09 (era 25/5/25/x2.0 dell'11/09): soglie allentate del
        20% su tutte le corsie, allo stesso ordine di grandezza fra scan e
        ordine, cosi' i segnali generati sono eseguibili.

        I valori sono ASSERTI ESATTI di proposito: un ulteriore allentamento
        deve essere una decisione tracciata in AGENTS.md, non un silenzio.
        """
        import sx_signals
        assert sx_signals.MIN_DEPTH_USDC == 20.0      # totale del match
        assert sx_signals.MIN_LEG_DEPTH_USDC == 4.0   # ogni esito
        assert sx_signals.MIN_EXEC_DEPTH_USDC == 20.0  # leg giocata
        assert auto_bet.MIN_EXEC_DEPTH_USDC == 20.0
        assert auto_bet.SX_DEPTH_MULTIPLIER == 1.6
        # Le protezioni NON si azzerano mai: un multiplo sotto 1.5 non
        # garantirebbe piu' nulla sul consumo del book.
        assert auto_bet.SX_DEPTH_MULTIPLIER >= 1.5
        assert sx_signals.MIN_EXEC_DEPTH_USDC >= 15.0
        # La soglia della leg giocata e' GEMELLA nelle due fasi.
        assert sx_signals.MIN_EXEC_DEPTH_USDC == auto_bet.MIN_EXEC_DEPTH_USDC

    def test_required_depth_scala_con_lo_stake(self):
        """Il vincolo e' max(stake x multiplo, minimo assoluto)."""
        m = auto_bet.SX_DEPTH_MULTIPLIER
        mn = auto_bet.MIN_EXEC_DEPTH_USDC
        # stake piccolo: prevale il minimo assoluto
        assert auto_bet.required_depth(mn / m / 2) == pytest.approx(mn)
        # stake grande: prevale il multiplo (margine sul book)
        assert auto_bet.required_depth(mn / m * 2) == pytest.approx(mn * 2)
        # stake non numerico: si ripiega sulla soglia assoluta (fail-closed
        # sul valore minimo, mai un book "a zero").
        assert auto_bet.required_depth(None) == pytest.approx(mn)

    def test_mercato_sottile_nessun_segnale(self, temp_db, monkeypatch):
        """Alzando la soglia di profondita', lo scan scarta il book."""
        import sx_signals
        from test_sx_signals import FakeSxProvider
        monkeypatch.setattr(sx_signals, "expected_goals", lambda h, a: (1.9, 0.8))
        monkeypatch.setattr(sx_signals, "prob_1x2", lambda lh, la: (0.66, 0.20, 0.14))
        monkeypatch.setattr(sx_signals, "adjusted_probability",
                            lambda mp, mkt, price, league=None: mp)
        monkeypatch.setattr(sx_signals, "MIN_DEPTH_USDC", 1_000_000.0)
        assert sx_signals.scan(provider=FakeSxProvider()) == []

    def test_available_size_somma_solo_sopra_il_prezzo(self):
        class _Prov:
            def get_market_book(self, market_id):
                return {"runners": [
                    {"selectionId": 1, "availableToBack": [
                        {"price": 1.60, "size": 2.0},
                        {"price": 1.70, "size": 3.0},
                        {"price": 1.80, "size": 4.0}]},
                    {"selectionId": 2, "availableToBack": [
                        {"price": 1.95, "size": 10.0}]}]}

        assert auto_bet._live_available_size(_Prov(), "m", 1, 1.65) == 7.0
        assert auto_bet._live_available_size(_Prov(), "m", 2, 1.65) == 10.0
        assert auto_bet._live_available_size(_Prov(), "m", 1, 5.0) == 0.0

    def test_available_size_book_ignoto_non_blocca(self):
        class _Prov:
            def get_market_book(self, market_id):
                return {"runners": [{"selectionId": 1, "quotes": {"back": []}}]}

        assert auto_bet._live_available_size(_Prov(), "m", 1, 1.5) is None

    def test_provider_senza_book_non_blocca(self):
        assert auto_bet._live_available_size(object(), "m", 1, 1.5) is None
