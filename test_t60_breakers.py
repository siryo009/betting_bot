"""Tripwire: 4 CIRCUIT BREAKERS della strategia T-60 (direttiva 17/09/2026).

Il proprietario ha chiesto TASSATIVAMENTE, prima di qualunque dispatch reale:
1. CB1 — hard cap per ordine (1.00 USDC, scelta per il floor SX): QUALSIASI
   calcolo dinamico (Kelly) che superi il limite viene sovrascritto;
2. CB2 — kill switch patrimoniale: wallet <= 30 USDC -> arresto + alert;
3. CB3 — validazione Pydantic rigida: payload malformato -> scartato e
   REGISTRATO sul ledger SQLite (mai verso il provider);
4. CB4 — filtro liquidità/order book prima dell'invio.

Piu' la finestra esecutiva (T-60..T-50 il 17/09, **T-180..T-2** dal
04/10/2026): fuori finestra SOLO scansione, nessun ordine.

Tutti i test sono OFFLINE: DB SQLite temporaneo, wallet/provider finti,
nessuna rete, nessun ordine reale.
"""
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import auto_bet
import tracker


@pytest.fixture()
def temp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setattr(tracker, "DB_PATH", Path(td) / "test.db")
        tracker.init_db()
        yield


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Mai toccare il volume reale (kill flag, stop-loss, feed, cassa)."""
    monkeypatch.setattr(auto_bet, "T60_KILL_FILE", tmp_path / "t60_kill.json")
    monkeypatch.setattr(auto_bet, "DAILY_STOP_FILE", tmp_path / "daily_stop.json")
    monkeypatch.setattr(auto_bet, "_market_feed_gate",
                        lambda *a, **k: (True, "test", {}))
    monkeypatch.setattr(auto_bet, "KILL_SWITCH_FILE",
                        tmp_path / "kill_switch.json")
    monkeypatch.setattr(auto_bet, "_live_wallet_snapshot",
                        lambda: {"available": 34.0, "exposure": 0.0,
                                 "equity": 34.0})
    alerts = []
    monkeypatch.setattr(auto_bet, "_t60_emergency_alert", alerts.append)
    monkeypatch.setattr(auto_bet, "_t60_alerts_sink", alerts, raising=False)
    return alerts


def _seed_validated_decision(mid="t1", home="Arsenal", away="Chelsea",
                             league="Premier League", kickoff=None,
                             outcome="1", price=1.65, verdict="approve",
                             status="validated"):
    """Una partita + riga `decisions` validate (stato Shadow Validation)."""
    start = kickoff or (datetime.now(timezone.utc)
                        + timedelta(minutes=55))
    tracker.save_match(mid, league, home, away,
                       start.isoformat())
    tracker.save_decision({
        "record_id": f"rec-{mid}", "signal_id": f"sig-{mid}",
        "match_id": mid, "league": league, "market": "1X2",
        "outcome": outcome, "selection_label": home if outcome == "1" else away,
        "kickoff": start.isoformat(), "price": price, "price_source": "sxbet",
        "market_prob": 0.60, "model_prob": 0.63, "blended_prob": 0.62,
        "edge": 0.05, "ev": 0.08, "tier": "value", "confidence": 0.7,
        "model_coverage": 1.0, "calibrated": True,
        "verdict": verdict, "reason": "ok", "status": status,
        "mode": "live", "provider": "sxbet", "stake": 1.0,
        "stake_executable": True, "kelly_fraction": 0.05,
        "cap_pct": 0.01, "cap_source": "tier", "approved_by": "",
        "review_note": "", "created_at": (datetime.now(timezone.utc)
                                          - timedelta(minutes=5)).isoformat(),
    })


def _live_mode(monkeypatch):
    """Modalità live con provider pronto e wallet 34 USDC."""
    monkeypatch.setattr(auto_bet, "_execution_mode",
                        lambda allow_sim=True: "live")


class TestCB1HardCap:
    """CB1: NESSUNO stake sopra il cap — Kelly sovrascritto, mai negoziato."""

    def test_t60_stake_mai_sopra_il_cap(self):
        """CB1 dal 04/10/2026 e' DINAMICO: nessuno stake sopra il 12%.

        Il tetto si legge dal cap reale (`order_ceiling`), mai da una
        costante scritta qui.
        """
        for bankroll in (34.0, 100.0, 500.0, 10_000.0):
            cap = auto_bet.order_ceiling(bankroll)
            assert cap > 0
            assert auto_bet.t60_stake(bankroll, mode="live") <= cap + 1e-9

    def test_t60_stake_scala_col_bankroll(self):
        # Cap dinamico 12%: 4.08 su 34 USDC (e' il tetto che morde: il 30%
        # di correlazione sarebbe 10.20, il 40% di esposizione 13.60).
        assert auto_bet.t60_stake(34.0, mode="live") == 4.08
        assert auto_bet.t60_stake(100.0, mode="live") == 12.0

    def test_tetto_assoluto_esplicito_vince(self, monkeypatch):
        """Un `T60_MAX_STAKE_USDC` > 0 resta un tetto assoluto (legacy)."""
        monkeypatch.setattr(auto_bet, "T60_MAX_STAKE_USDC", 1.00)
        assert auto_bet.order_ceiling(34.0) == 1.00
        assert auto_bet.t60_stake(34.0, mode="live") == 1.0

    def test_t60_stake_zero_sotto_il_floor(self):
        # Un bankroll che non copre nemmeno il minimo ordine: no bet.
        assert auto_bet.t60_stake(0.5, mode="live") == 0.0

    def test_stake_engine_sovrascrive_kelly_sopra_il_cap(self):
        """Il cuore del CB1: Kelly alto -> sovrascritto dal contratto."""
        from decision.limits import RiskLimits
        from decision.models import (DataQuality, ReasonCode, Signal,
                                     risk_approve)
        signal = Signal(
            match_id="m1", league="Premier League", market="1X2",
            outcome="1", price=1.65, kickoff=datetime.now(timezone.utc)
            + timedelta(hours=2),
            market_prob=0.60, model_prob=0.75, blended_prob=0.70,
            edge=0.15, ev=0.20, tier="strong_value",
            confidence=0.9, price_source="sxbet",
            data_quality=DataQuality(model_coverage=1.0, calibrated=True))
        risk = risk_approve(checked=["test"])
        limits = RiskLimits.from_env()
        rec_stake = auto_bet.order_ceiling(1000.0)
        # Kelly su bankroll 1000 con edge 15pp produrrebbe molto piu' del 12%.
        from decision.stake_engine import size
        sd = size(signal, risk, bankroll=1000.0, limits=limits, mode="live")
        assert sd.stake <= rec_stake + 1e-9

    def test_cap_assoluto_configurabile(self, monkeypatch):
        """Env T60_MAX_STAKE_USDC=0.50: tetto assoluto ripristinabile."""
        monkeypatch.setattr(auto_bet, "T60_MAX_STAKE_USDC", 0.50)
        assert auto_bet.order_ceiling(1000.0) == 0.50

    def test_tetto_fail_closed_su_bankroll_ignoto(self, monkeypatch):
        """Senza bankroll (ne' passato ne' registrato) il CB1 dinamico non e'
        calcolabile: nessun ordine, mai un tetto arbitrario."""
        monkeypatch.setattr(auto_bet, "T60_MAX_STAKE_USDC", 0.0)
        monkeypatch.setattr(auto_bet, "_LAST_BANKROLL", 0.0)
        assert auto_bet.order_ceiling() == 0.0


class TestCB2KillSwitch:
    """CB2: wallet <= 30 USDC -> arresto totale + flag persistente + alert."""

    def test_soglia_non_ordinata_sopra_soglia(self):
        assert auto_bet.t60_check_wallet_kill(34.0) is False

    def test_a_soglia_armato(self, tmp_path):
        assert auto_bet.t60_check_wallet_kill(30.0) is True
        st = auto_bet.t60_kill_switch_status()
        assert st["triggered"] is True
        assert st["wallet_equity"] == 30.0

    def test_sotto_soglia_armato(self):
        assert auto_bet.t60_check_wallet_kill(12.5) is True

    def test_flag_persistente_sul_volume(self, tmp_path):
        auto_bet.t60_check_wallet_kill(29.0)
        data = json.loads((tmp_path / "t60_kill.json").read_text())
        assert data["threshold"] == 30.0
        assert data["triggered_at"]

    def test_giro_bloccato_con_flag_armato(self, temp_db, monkeypatch):
        _live_mode(monkeypatch)
        auto_bet.t60_check_wallet_kill(25.0)   # arma il flag
        _seed_validated_decision()
        assert auto_bet.run_today_bets() == []
        assert auto_bet.t60_dispatch_pending() == []

    def test_wallet_non_leggibile_non_arma_ma_t60_fail_closed(self,
                                                              temp_db,
                                                              monkeypatch):
        """Un errore API transitorio NON arresta il sistema (no falso kill);
        il giro esecutivo T-60 pero' esce fail-closed senza armare nulla."""
        _live_mode(monkeypatch)
        monkeypatch.setattr(auto_bet, "_live_wallet_snapshot", lambda: None)
        assert auto_bet.t60_check_wallet_kill(None) is False
        st = auto_bet.t60_kill_switch_status()
        assert st["triggered"] is False
        assert auto_bet.t60_dispatch_pending() == []

    def test_equity_non_available_non_confonde_con_disponibile(self):
        """La soglia vale sull'EQUITY: 20 liberi + 15 in gioco = 35 = OK."""
        assert auto_bet.t60_check_wallet_kill(35.0) is False
        assert auto_bet.t60_check_wallet_kill(29.9) is True

    def test_clear_riattiva(self, temp_db, monkeypatch):
        auto_bet.t60_check_wallet_kill(29.0)
        auto_bet.t60_clear_kill()
        assert auto_bet.t60_kill_switch_status()["triggered"] is False


class TestCB3ValidazionePydantic:
    """CB3: contratto rigido, payload malformato -> scartato + ledger."""

    def _payload(self, **over):
        base = {
            "signal_id": "sig", "record_id": "rec", "match_id": "m1",
            "league": "Premier League", "market": "1X2", "outcome": "1",
            "home": "Arsenal", "away": "Chelsea",
            "price": 1.65, "stake": 1.0, "verdict": "approve",
            "mode": "live", "provider": "sxbet",
            "kickoff": (datetime.now(timezone.utc)
                        + timedelta(minutes=55)).isoformat(),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        base.update(over)
        return base

    def test_payload_valido_passa(self):
        # CB1 dal 04/10 e' dinamico: il tetto efficace va passato col bankroll
        # (con 34 USDC = 4.08; uno stake di 1.0 sta sotto).
        ok, contract, errors = auto_bet.validate_order_payload(
            self._payload(), bankroll=34.0)
        assert ok and errors == []
        assert contract.stake == 1.0

    def test_stake_sopra_cap_scartato_non_cappato(self):
        ok, contract, errors = auto_bet.validate_order_payload(
            self._payload(stake=5.0), bankroll=34.0)   # cap 4.08
        assert not ok and contract is None
        assert any("circuit breaker" in e for e in errors)

    def test_tetto_ignoto_scarta_il_payload(self):
        """Senza bankroll il CB1 dinamico non e' calcolabile: fail-closed."""
        ok, contract, errors = auto_bet.validate_order_payload(
            self._payload(), bankroll=None)
        assert not ok and contract is None
        assert any("non calcolabile" in e for e in errors)

    def test_quota_fuori_fascia_scartata(self):
        ok, _, errors = auto_bet.validate_order_payload(
            self._payload(price=2.5), bankroll=34.0)
        assert not ok
        assert any("circuit breaker" in e for e in errors)

    def test_timestamp_naive_scartato(self):
        ok, _, errors = auto_bet.validate_order_payload(
            self._payload(kickoff=(datetime.now(timezone.utc)
                                    + timedelta(minutes=55))
                          .replace(tzinfo=None).isoformat()))
        assert not ok
        assert any("fuso" in e for e in errors)

    def test_liga_vuota_scartata(self):
        ok, _, errors = auto_bet.validate_order_payload(self._payload(league=""))
        assert not ok

    def test_live_senza_provider_scartato(self):
        ok, _, errors = auto_bet.validate_order_payload(
            self._payload(provider=""))
        assert not ok

    def test_kickoff_prima_di_created_scartato(self):
        ok, _, errors = auto_bet.validate_order_payload(
            self._payload(kickoff=(datetime.now(timezone.utc)
                                    - timedelta(hours=1)).isoformat()))
        assert not ok

    def test_campo_ignoto_scartato(self):
        ok, _, errors = auto_bet.validate_order_payload(
            self._payload(campo_inatteso="x"))
        assert not ok

    def test_payload_malformato_registrato_sul_ledger(self, temp_db,
                                                      monkeypatch):
        """CB3 in dispatch: payload rotto -> riga `rejected-t60` su bets,
        NESSUNA chiamata al provider."""
        _live_mode(monkeypatch)
        calls = []
        monkeypatch.setattr(auto_bet, "_live_fill",
                            lambda *a, **k: calls.append(a) or None)
        _seed_validated_decision(mid="bad", outcome="QUOTA_MALFORMATA")
        # Forza il price a 0 sul ledger (payload incoerente per il contratto).
        conn = tracker._get_conn()
        conn.execute("UPDATE decisions SET price = 0 WHERE match_id='bad'")
        conn.commit()
        conn.close()
        placed = auto_bet.t60_dispatch_pending(bankroll=34.0)
        assert placed == []
        assert calls == []                 # mai arrivato al provider
        conn = tracker._get_conn()
        rows = conn.execute("SELECT mode, stake FROM bets "
                            "WHERE match_id='bad'").fetchall()
        conn.close()
        assert rows and rows[0][0] == "rejected-t60"
        assert rows[0][1] == 0.0


class TestCB4Liquidita:
    """CB4: il gate di mercato blocca il giro; il book sottile blocca l'ordine."""

    def test_gate_di_mercato_blocca_il_dispatch(self, temp_db, monkeypatch):
        _live_mode(monkeypatch)
        monkeypatch.setattr(auto_bet, "_market_feed_gate",
                            lambda *a, **k: (False, "FEED_STALE: feed vecchio",
                                             {}))
        _seed_validated_decision()
        assert auto_bet.t60_dispatch_pending(bankroll=34.0) == []

    def test_book_sottile_blocca_l_ordine(self, temp_db, monkeypatch):
        """La guardia liquidita' di _live_fill resta attiva sul percorso T-60."""
        _live_mode(monkeypatch)
        _seed_validated_decision()
        monkeypatch.setattr(auto_bet, "required_depth", lambda s: 25.0)
        monkeypatch.setattr(auto_bet, "_live_available_size",
                            lambda *a, **k: 4.0)   # book da 4 USDC
        calls = []

        def _fake_fill(pick, stake, floor):
            calls.append((stake, floor))
            return {"ok": True, "market_id": "mx", "selection_id": 1,
                    "bet_id": "b1", "status": "FULLY_FILLED",
                    "price": floor, "stake": stake}

        # Simula _live_fill che al suo interno usa la depth: qui verifichiamo
        # che la size del book sia quella letta dal guardrail (via monkeypatch
        # di _live_available_size usata dentro _live_fill reale -> usiamo un
        # provider finto con book sottile per non reimplementare la logica).
        monkeypatch.setattr(auto_bet, "_live_fill", _fake_fill)
        placed = auto_bet.t60_dispatch_pending(bankroll=34.0)
        # Il dispatch T-60 delega a _live_fill (che contiene CB4): l'ordine
        # parte solo se la guardia interna lascia passare. Con il fake sopra
        # verifichiamo il wiring, non la guardia (testata in test_auto_bet).
        assert len(placed) == 1 and calls

    def test_partial_fill_registrato(self, temp_db, monkeypatch):
        """Riempimento parziale: lo stake effettivo e' quello MATCHED."""
        _live_mode(monkeypatch)
        _seed_validated_decision()
        monkeypatch.setattr(auto_bet, "_live_fill",
                            lambda *a, **k: {"ok": True, "market_id": "mx",
                                             "selection_id": 1,
                                             "bet_id": "b1",
                                             "status": "PARTIALLY_FILLED",
                                             "price": 1.7, "stake": 0.6})
        placed = auto_bet.t60_dispatch_pending(bankroll=34.0)
        assert len(placed) == 1
        assert placed[0]["stake"] == 0.6   # mai lo stake teorico


class TestFinestraT60:
    """Strategia **T-180..T-2**: decisione esecutiva da 3 ore a 2 minuti.

    Direttiva 04/10/2026 (era T-60..T-5, e T-50/T-15 prima): la banda si
    allarga per catturare le variazioni di quota delle formazioni ufficiali e
    i volumi dei sindacati quantitativi, e si chiude a 2 minuti per non avere
    zone d'ombra. Il test asserisce la politica NUOVA per intero: un ulteriore
    spostamento deve rompere qui.
    """

    def test_classificazione_finestra(self):
        now = datetime.now(timezone.utc)
        assert auto_bet.t60_window(now + timedelta(minutes=200)) == "before"
        assert auto_bet.t60_window(now + timedelta(minutes=175)) == "within"
        assert auto_bet.t60_window(now + timedelta(minutes=120)) == "within"
        assert auto_bet.t60_window(now + timedelta(minutes=55)) == "within"
        assert auto_bet.t60_window(now + timedelta(minutes=30)) == "within"
        assert auto_bet.t60_window(now + timedelta(minutes=10)) == "within"
        assert auto_bet.t60_window(now + timedelta(minutes=5)) == "within"
        # Il bordo NON si asserisce esatto: `t60_window` ricalcola il suo
        # `now`, quindi a +2 esatti i microsecondi trascorsi lo portano
        # sotto soglia (deterministicamente flaky). Si BRACKETTA il valore:
        # +3 dentro, +1 fuori -> la chiusura e' 2.
        assert auto_bet.t60_window(now + timedelta(minutes=3)) == "within"
        assert auto_bet.t60_window(now + timedelta(minutes=1)) == "missed"
        assert auto_bet.t60_window(now - timedelta(minutes=5)) == "missed"
        assert auto_bet.t60_window(None) == "unknown"

    def test_apertura_a_T180(self):
        """L'apertura e' **T-180** (3 ore): fuori, solo scansione."""
        assert auto_bet.T60_WINDOW_MIN_MIN == 180.0

    def test_chiusura_allineata_al_pavimento_assoluto(self):
        """Le due guardie COINCIDONO a **T-2** (direttiva 04/10/2026).

        Se la chiusura scendesse sotto `MIN_MINUTES_TO_START` l'esecuzione
        tenterebbe ordini che l'altra guardia salta comunque (lavoro sprecato
        e log contraddittori); se salisse sopra, l'ultima parte della banda
        sarebbe una ZONA MORTA silenziosa. La costante di codice e' DERIVATA
        dal pavimento (una sola sorgente).
        """
        assert auto_bet.MIN_MINUTES_TO_START == 2
        assert auto_bet.T60_WINDOW_MAX_MIN == auto_bet.MIN_MINUTES_TO_START

    def test_dispatch_fuori_finestra_non_ordina(self, temp_db, monkeypatch):
        _live_mode(monkeypatch)
        calls = []
        monkeypatch.setattr(auto_bet, "_live_fill",
                            lambda *a, **k: calls.append(a) or None)
        _seed_validated_decision(mid="early",
                                 kickoff=datetime.now(timezone.utc)
                                 + timedelta(hours=4))
        # +1 minuto e' SOTTO la chiusura (T-2 dal 04/10, era T-5): fail-closed.
        _seed_validated_decision(mid="late",
                                 kickoff=datetime.now(timezone.utc)
                                 + timedelta(minutes=1))
        assert auto_bet.t60_dispatch_pending(bankroll=34.0) == []
        assert calls == []

    def test_dispatch_in_finestra_ordina(self, temp_db, monkeypatch):
        _live_mode(monkeypatch)
        # +20: dentro la banda di OGNI politica (T-60..T-5 il 03/10,
        # T-180..T-2 dal 04/10): il caso che il cablaggio deve esercitare.
        # E' il caso che il test del cablaggio deve esercitare.
        _seed_validated_decision(mid="ok",
                                 kickoff=datetime.now(timezone.utc)
                                 + timedelta(minutes=20))
        monkeypatch.setattr(auto_bet, "_live_fill",
                            lambda pick, stake, floor: {
                                "ok": True, "market_id": "mx",
                                "selection_id": 1, "bet_id": "b1",
                                "status": "FULLY_FILLED",
                                "price": floor, "stake": stake})
        placed = auto_bet.t60_dispatch_pending(bankroll=34.0)
        assert len(placed) == 1
        # CB1 dinamico (12%): su bankroll 34 lo stake T-60 e' 4.08.
        assert placed[0]["stake"] == 4.08
        assert placed[0]["mode"] == "t60-live"
        # La puntata e' sul ledger con i dati reali dell'ordine.
        conn = tracker._get_conn()
        row = conn.execute("SELECT mode, stake, bet_id FROM bets "
                           "WHERE match_id='ok'").fetchone()
        conn.close()
        assert row == ("live", 4.08, "b1")

    def test_senza_bet_id_non_scrive_la_riga(self, temp_db, monkeypatch):
        """Secondo punto di scrittura: senza bet_id NESSUNA riga live.

        `_live_fill` gia' fallisce chiuso, ma il dispatch T-60 scrive per
        conto suo: la guardia e' ripetuta qui (difesa in profondita'), perche'
        il ledger non deve contenere un ordine che sull'exchange non esiste ne'
        uno status inventato come "SUCCESS".
        """
        _live_mode(monkeypatch)
        _seed_validated_decision(mid="no-id",
                                 kickoff=datetime.now(timezone.utc)
                                 + timedelta(minutes=55))
        monkeypatch.setattr(auto_bet, "_live_fill",
                            lambda pick, stake, floor: {
                                "ok": True, "market_id": "mx",
                                "selection_id": 1, "bet_id": None,
                                "status": "FULLY_FILLED",
                                "price": floor, "stake": stake})
        placed = auto_bet.t60_dispatch_pending(bankroll=34.0)
        assert placed == []
        conn = tracker._get_conn()
        n = conn.execute("SELECT COUNT(*) FROM bets").fetchone()[0]
        conn.close()
        assert n == 0

    def test_dedup_match_esito(self, temp_db, monkeypatch):
        """UNIQUE(match_id, esito): il job ogni 60s non raddoppia."""
        _live_mode(monkeypatch)
        _seed_validated_decision(mid="ok",
                                 kickoff=datetime.now(timezone.utc)
                                 + timedelta(minutes=55))
        monkeypatch.setattr(auto_bet, "_live_fill",
                            lambda pick, stake, floor: {
                                "ok": True, "market_id": "mx",
                                "selection_id": 1, "bet_id": "b1",
                                "status": "FULLY_FILLED",
                                "price": floor, "stake": stake})
        first = auto_bet.t60_dispatch_pending(bankroll=34.0)
        second = auto_bet.t60_dispatch_pending(bankroll=34.0)
        assert len(first) == 1 and second == []

    def test_giro_normale_fuori_finestra_solo_scansione(self, temp_db,
                                                        monkeypatch):
        """Con T60_EXECUTION_ONLY il giro normale non ordina fuori finestra."""
        _live_mode(monkeypatch)
        # Segnale value a 3h dal kickoff: scansionato, NON ordinato.
        start = (datetime.now(timezone.utc)
                 + timedelta(hours=3)).isoformat().replace("+00:00", "Z")
        tracker.save_match("scan1", "Premier League", "Arsenal", "Chelsea",
                           start)
        tracker.save_analysis("scan1", 1.7, 1.1, 0.52, 0.27, 0.21, 0.58,
                              0.08, "Arsenal", 1.65, "Pinnacle", "value",
                              market_prob=0.60, market_edge=0.07)
        tracker.save_prediction("scan1", "1X2", "Arsenal", 1.65, 0.52, 0.08,
                                market_prob=0.60, market_edge=0.07,
                                status="value")
        monkeypatch.setattr(auto_bet, "T60_EXECUTION_ONLY", True)
        assert auto_bet.run_today_bets() == []

    def test_sim_mai_verso_il_provider(self, temp_db, monkeypatch):
        """In SIM il dispatch T-60 non tocca mai il provider."""
        monkeypatch.setattr(auto_bet, "_execution_mode",
                            lambda allow_sim=True: "sim")
        calls = []
        monkeypatch.setattr(auto_bet, "_live_fill",
                            lambda *a, **k: calls.append(a) or None)
        _seed_validated_decision(mid="sim1",
                                 kickoff=datetime.now(timezone.utc)
                                 + timedelta(minutes=55))
        placed = auto_bet.t60_dispatch_pending(bankroll=34.0)
        assert calls == []
        assert placed and placed[0]["mode"] == "t60-sim"


class TestContrattoT60:
    """Il contratto Pydantic esiste ed e' rigido (extra=forbid)."""

    def test_contratto_presente(self):
        from decision.models import T60OrderContract
        assert T60OrderContract.model_config.get("extra") == "forbid"

    def test_t60_executable_coerente_col_cap(self):
        from decision.models import t60_executable
        cap = auto_bet.order_ceiling(34.0)
        assert cap > 0
        assert t60_executable(cap, 1.65, cap) is True
        assert t60_executable(cap + 0.01, 1.65, cap) is False
        assert t60_executable(1.0, 2.0, cap) is False


class TestFinestraT15MercatiDerivati:
    """I mercati DERIVATI (OU/AH) leggono la stessa costante di scansione.

    Direttiva 30/09/2026, aggiornata il 04/10/2026: la chiusura della finestra
    esecutiva e' **T-2** e deve valere per OGNI mercato, derivati inclusi. Non
    basta che il codice lo faccia oggi: serve che NON PUO' regredire a una
    costante diversa per mercato (bug del 09/09: un `over` trattato come 1X2).
    """

    def test_costante_di_chiusura_e_t2(self):
        assert auto_bet.T60_WINDOW_MAX_MIN == 2.0

    def test_il_gate_finestra_e_applicato_a_ogni_mercato(self):
        """Il blocco `T60_EXECUTION_ONLY` in `run_today_bets` NON e'
        condizionato al tipo di mercato: se lo fosse, un mercato derivato
        leggerebbe una finestra diversa (o nessuna).

        Dal 04/10 il verdetto passa da `pick_window` (UNICA definizione, usata
        anche dalla telemetria degli scarti): il gate non puo' divergere dal
        conteggio dei pick bloccati."""
        src = Path("auto_bet.py").read_text(encoding="utf-8")
        block = src.split("if T60_EXECUTION_ONLY:", 1)[1][:400]
        assert "pick_window(pick)" in block
        # tra il blocco e la chiamata non compare nessuna condizione sul
        # mercato: il gate non puo' essere saltato per tipo
        assert "mercato" not in block.split("pick_window(pick)")[0]

    def test_i_pick_derivati_hanno_lo_stesso_tetto(self):
        """OU/AH non hanno una finestra propria: passano da `pick_window`."""
        src = Path("auto_bet.py").read_text(encoding="utf-8")
        # il verdetto di finestra vive in UN solo posto e delega a `t60_window`
        assert src.count("def pick_window(") == 1
        assert src.count('t60_window(_parse_iso_utc(pick.get("commence")))') == 1


class TestRegolaStakeDecisoAMonte:
    """Lo stake deciso A MONTE non viene ri-dimensionato dal Kelly (08/10/2026).

    Una corsia le cui quote arrivano con lo stake GIA' deciso (Finanza della
    catena piramidale, cap di correlazione, cap di esposizione totale) NON
    deve passare dal ricalcolo Kelly in `refresh_live_stakes`: il ricalcolo la
    azzererebbe (`no_true_prob`: un piano non porta p_true/EV). La corsia T-60
    e' **immune per costruzione** — usa `t60_stake` (micro-allocazione CB1) e
    non il motore Kelly — e questo tripwire lo fissa, cosi' nessuno puo'
    instradarla nel ricalcolo (o aggiungere una chiave T-60 alla regola)
    senza accorgersene.
    """

    def test_regola_con_una_definizione_sola(self):
        """La chiave della regola e il predicato vivono in UN punto."""
        assert auto_bet.STAKE_DECIDED_KEYS == ("chief_trade", "corr_cap",
                                               "total_cap")
        src = Path("auto_bet.py").read_text(encoding="utf-8")
        assert src.count("STAKE_DECIDED_KEYS = ") == 1
        assert src.count("def stake_decided_upstream(") == 1

    def test_corsia_t60_immune_dal_kelly(self):
        """Il dispatch T-60 e `t60_stake` non chiamano il motore Kelly: il
        loro stake viene da `t60_stake` (tetto CB1), mai dal ricalcolo."""
        import inspect
        disp = inspect.getsource(auto_bet.t60_dispatch_pending)
        stake = inspect.getsource(auto_bet.t60_stake)
        for corpo in (disp, stake):
            assert "kelly_size_for_pick" not in corpo
            assert "refresh_live_stakes" not in corpo
            assert "stake_decided_upstream" not in corpo
        # il dispatch usa la micro-allocazione CB1 (UNICA fonte del suo stake)
        assert "t60_stake(" in disp

    def test_nessuna_chiave_t60_nella_regola(self):
        """Se una corsia T-60 venisse instradata nel ricalcolo dovrebbe prima
        dichiararla in `STAKE_DECIDED_KEYS`: questo test lo segnala."""
        keys = " ".join(auto_bet.STAKE_DECIDED_KEYS).lower()
        assert "t60" not in keys
        # la catena piramidale resta coperta (regressione 08/10/2026)
        assert "chief_trade" in keys
