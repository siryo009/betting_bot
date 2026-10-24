"""RECOVERY GATE 2 — test ISOLATI delle protezioni (20/10/2026).

Obiettivo: dimostrare, senza toccare la produzione, che le protezioni del
percorso ordini **bloccano davvero** nelle quattro situazioni che contano:

1. equity del wallet a/sotto la soglia CB2 (25.00 USDC);
2. perdita giornaliera oltre il 5% (e drawdown settimanale oltre il 12%);
3. errore di lettura del wallet (API giu') durante un giro LIVE;
4. tentativo di ordine DUPLICATO sullo stesso mercato.

Nessun file di produzione viene letto o scritto: il `conftest.py` sposta gia'
`T60_KILL_FILE`, `DAILY_STOP_FILE`, `WEEKLY_STOP_FILE` e
`BANKROLL_HISTORY_FILE` nella `tmp_path` del test, e il fixture `temp_db` di
questo file punta `tracker.DB_PATH` su un SQLite temporaneo.

Le protezioni NON vengono indebolite per far passare i test: dove il test
misura un comportamento (es. `equity=None` non arma il CB2) lo fa per
DOCUMENTARLO, non per autorizzarlo — la direzione e' scritta nell'assert e
nel report (`RECOVERY_GATE2.md`).
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

import auto_bet
import execution_engine
import tracker


@pytest.fixture()
def temp_db(monkeypatch, tmp_path):
    """Ledger SQLite temporaneo (nessun accesso al DB di produzione)."""
    db_path = tmp_path / "gate2.db"
    monkeypatch.setattr(tracker, "DB_PATH", db_path)
    tracker.init_db()
    yield db_path


@pytest.fixture(autouse=True)
def _soglie_di_produzione(monkeypatch, tmp_path):
    """Valori REALI delle protezioni + stato su file isolato nella tmp.

    Il conftest azzera alcune soglie per gli altri test (es. CB2 a 0.0): qui
    si riportano ai valori che girano in produzione, altrimenti i test
    misurerebbero un'altra configurazione.
    """
    monkeypatch.setattr(auto_bet, "T60_KILL_WALLET_USDC", 25.0)
    monkeypatch.setattr(auto_bet, "T60_KILL_FILE", tmp_path / "t60_kill.json")
    monkeypatch.setattr(auto_bet, "DAILY_STOP_FILE", tmp_path / "daily_stop.json")
    monkeypatch.setattr(auto_bet, "WEEKLY_STOP_FILE", tmp_path / "weekly_stop.json")
    monkeypatch.setattr(auto_bet, "BANKROLL_HISTORY_FILE", tmp_path / "bh.json")
    monkeypatch.setattr(auto_bet, "KILL_SWITCH_FILE", tmp_path / "mode.json")
    monkeypatch.setattr(auto_bet, "DAILY_STOP_LOSS_PCT", 0.05)
    monkeypatch.setattr(auto_bet, "WEEKLY_STOP_LOSS_PCT", 0.12)
    monkeypatch.setattr(auto_bet, "CB2_SOFT_FLOOR_USDC", 0.0)
    monkeypatch.setattr(auto_bet, "DRY_RUN", False)
    monkeypatch.setattr(auto_bet, "T60_EXECUTION_ONLY", False)
    # Lo stato "capitale dell'ultimo tick" e' un GLOBALE di modulo: senza
    # ripristino un test deciderebbe il successivo (stessa lezione della
    # cache di prontezza OU del 26/09).
    saved = auto_bet._LAST_BANKROLL
    yield
    auto_bet._LAST_BANKROLL = saved


# ---------------------------------------------------------------------------
# 1. CB2 — arming, soglia, direzioni del fail-safe
# ---------------------------------------------------------------------------

class TestCB2Arming:
    def test_equity_sotto_soglia_arma_il_flag(self, tmp_path):
        armed = auto_bet.t60_check_wallet_kill(24.90)
        assert armed is True
        flag = json.loads((tmp_path / "t60_kill.json").read_text())
        assert flag["wallet_equity"] == pytest.approx(24.90)
        assert flag["threshold"] == pytest.approx(25.0)
        assert "soglia 25.00" in flag["reason"]
        assert flag["triggered_at"]

    def test_equity_esattamente_alla_soglia_arma(self):
        """La soglia e' INCLUSIVA: 25.00 esatti e' gia' arresto."""
        assert auto_bet.t60_check_wallet_kill(25.0) is True

    def test_equity_sopra_soglia_non_arma(self, tmp_path):
        assert auto_bet.t60_check_wallet_kill(25.01) is False
        assert not (tmp_path / "t60_kill.json").exists()

    def test_equity_illeggibile_non_arma_documentato(self, tmp_path):
        """DIREZIONE DICHIARATA: un errore di lettura NON arresta il sistema.

        Un'API transitoria non deve fermare il bot fino all'intervento umano;
        il comportamento fail-closed per quel caso vive nel percorso ordini
        (`refresh_live_stakes`, vedi `TestErroreApi`).
        """
        assert auto_bet.t60_check_wallet_kill(None) is False
        assert not (tmp_path / "t60_kill.json").exists()

    def test_valore_non_numerico_non_arma(self):
        assert auto_bet.t60_check_wallet_kill("abc") is False

    def test_flag_corrotto_e_fail_closed(self, tmp_path):
        (tmp_path / "t60_kill.json").write_text("{non-json", encoding="utf-8")
        status = auto_bet.t60_kill_switch_status()
        assert status["triggered"] is True
        assert "illeggibile" in (status.get("reason") or "")

    def test_clear_rimuove_il_flag(self, tmp_path):
        auto_bet.t60_check_wallet_kill(20.0)
        assert (tmp_path / "t60_kill.json").exists()
        auto_bet.t60_clear_kill()
        assert auto_bet.t60_kill_switch_status()["triggered"] is False
        # idempotente: rimuovere un flag assente non solleva
        auto_bet.t60_clear_kill()

    def test_riarmo_immediato_dopo_il_reset_sotto_soglia(self):
        """`/t60reset` con equity 21.10 NON sblocca: si riarma subito.

        E' il motivo per cui il Gate 1 ha classificato BLOCCATO: disinnescare
        il CB2 senza un deposito e' un gesto inerte.
        """
        auto_bet.t60_clear_kill()
        assert auto_bet.t60_kill_switch_status()["triggered"] is False
        assert auto_bet.t60_check_wallet_kill(21.1004) is True
        assert auto_bet.t60_kill_switch_status()["triggered"] is True


# ---------------------------------------------------------------------------
# 2. CB2 e kill-switch BLOCCANO il percorso ordini (non solo il flag)
# ---------------------------------------------------------------------------

def _verdetto_top_down(ev: float = 0.05, price: float = 1.90) -> dict:
    """Verdetto nella FORMA ESATTA di `_top_down_eval` (nessuna chiave in meno).

    `run_today_bets` legge `p_true`, `ev`, `true_odd`, `required_price`,
    `trigger` ed `ev_min`: uno stub incompleto farebbe fallire il giro per
    `KeyError`, cioe' il test misurerebbe lo stub e non la protezione.
    """
    true_odd = round(1.0 / 0.60, 4)
    return {"ok": True, "ev": ev, "ev_min": 0.015, "p_true": 0.60,
            "true_odd": true_odd, "trigger": True,
            "required_price": round(true_odd * (1.0 + 0.025), 4),
            "price": price, "action": None, "reason": None}


def _pick() -> dict:
    """Candidato con kickoff RELATIVO a `now` (+3h).

    Un kickoff fisso nel 2030 fa scattare il gate di timing ("troppo presto,
    quote noise") e il test misurerebbe quello invece della protezione in
    esame — la stessa lezione delle date fisse del 15/09 e del 17/09.
    """
    ko = (datetime.now(timezone.utc) + timedelta(hours=3)).replace(
        microsecond=0).isoformat().replace("+00:00", "Z")
    return {
        "match_id": "gate2-1", "match": "Casa - Ospite", "home": "Casa",
        "away": "Ospite", "esito_key": "1", "esito": "Casa",
        "mercato": "1X2", "quota": 1.90, "league": "Serie A",
        "kickoff": ko, "commence": ko, "stake": 2.0,
    }


PICK = _pick()


@pytest.fixture()
def _giro_live_isolato(monkeypatch, temp_db):
    """Giro in modalita' LIVE con wallet finto e nessuna chiamata reale."""
    monkeypatch.setattr(auto_bet, "_execution_mode", lambda allow_sim=True: "live")
    monkeypatch.setattr(auto_bet, "_live_wallet_snapshot",
                        lambda *a, **k: {"available": 21.1004, "exposure": 0.0,
                                         "equity": 21.1004})
    monkeypatch.setattr(auto_bet, "_today_value_picks", lambda *a, **k: [_pick()])
    for lane in ("_multi_market_picks", "_top_down_picks", "_esports_picks",
                 "_tennis_picks", "_gridiron_picks"):
        monkeypatch.setattr(auto_bet, lane, lambda *a, **k: [])
    monkeypatch.setattr(auto_bet, "_top_down_eval",
                        lambda *a, **k: _verdetto_top_down())
    monkeypatch.setattr(auto_bet, "_harvest_oracle_board", lambda *a, **k: {})
    monkeypatch.setattr(auto_bet, "_apply_rush", lambda c, **k: (c, {}))
    monkeypatch.setattr(auto_bet, "_apply_tournament", lambda c, **k: (c, {}))
    calls = []
    monkeypatch.setattr(auto_bet, "_live_fill",
                        lambda pick, stake, floor: calls.append(
                            {"pick": pick, "stake": stake, "floor": floor}) or None)
    return calls


class TestCB2BloccaIlPercorso:
    def test_giro_normale_nessun_ordine_con_cb2_armato(self, _giro_live_isolato,
                                                       temp_db):
        auto_bet.t60_check_wallet_kill(21.1004)     # CB2 armato
        out = auto_bet.run_today_bets()
        assert out == []
        assert _giro_live_isolato == []             # nessun POST tentato
        rows = tracker._get_conn().execute(
            "SELECT COUNT(*) FROM bets").fetchone()[0]
        assert rows == 0                            # nessuna riga sul ledger

    def test_senza_cb2_lo_stesso_giro_prova_l_ordine(self, _giro_live_isolato,
                                                     temp_db, monkeypatch):
        """CONTROPROVA: il blocco viene dal CB2, non dal giro.

        Stesso setup, flag assente ed equity BEN sopra la soglia (400 USDC),
        cosi' lo stake adattivo supera il floor dell'exchange: il candidato
        arriva a `_live_fill`. Con equity 21.10 il giro riarma il CB2 da solo
        (`t60_check_wallet_kill` sul wallet reale) — difesa in profondita'
        verificata dal test successivo.
        """
        monkeypatch.setattr(auto_bet, "_live_wallet_snapshot",
                            lambda *a, **k: {"available": 400.0, "exposure": 0.0,
                                             "equity": 400.0})
        auto_bet.t60_clear_kill()
        out = auto_bet.run_today_bets(stake_eur=2.0)
        assert isinstance(out, list)
        assert len(_giro_live_isolato) == 1, (
            "senza CB2 il candidato deve raggiungere il percorso d'ordine")

    def test_il_giro_riarma_da_solo_il_cb2_con_equity_bassa(self,
                                                            _giro_live_isolato,
                                                            temp_db,
                                                            tmp_path):
        """Difesa in profondita': cancellare il flag non basta.

        `run_today_bets` ricontrolla il wallet a ogni giro: con 21.10 il
        flag viene RISCRITTO prima di qualunque valutazione.
        """
        auto_bet.t60_clear_kill()
        assert auto_bet.run_today_bets() == []
        assert _giro_live_isolato == []
        assert (tmp_path / "t60_kill.json").exists()
        assert auto_bet.t60_kill_switch_status()["triggered"] is True

    def test_kill_switch_off_esce_prima_di_leggere_il_wallet(self, monkeypatch,
                                                             temp_db):
        auto_bet.KILL_SWITCH_FILE.write_text(json.dumps({"mode": "off"}))
        letture = []
        monkeypatch.setattr(auto_bet, "_live_wallet_snapshot",
                            lambda *a, **k: letture.append(1) or None)
        monkeypatch.setattr(auto_bet, "_live_fill",
                            lambda *a, **k: pytest.fail("ordine tentato"))
        assert auto_bet.run_today_bets() == []
        assert letture == [], "il kill-switch deve uscire PRIMA del wallet"

    def test_override_non_puo_riaccendere_il_live(self, _giro_live_isolato,
                                                  temp_db):
        """Un override del chiamante puo' solo abbassare, mai alzare."""
        auto_bet.t60_clear_kill()
        out = auto_bet.run_today_bets(mode_override="live")
        assert out == []
        assert _giro_live_isolato == []

    def test_dispatch_t60_bloccato_dal_cb2(self, monkeypatch, temp_db):
        auto_bet.t60_check_wallet_kill(21.1004)
        monkeypatch.setattr(auto_bet, "_execution_mode", lambda allow_sim=True: "live")
        monkeypatch.setattr(auto_bet, "_market_feed_gate",
                            lambda **k: pytest.fail("gate di mercato dopo il CB2"))
        monkeypatch.setattr(auto_bet, "_live_fill",
                            lambda *a, **k: pytest.fail("ordine tentato"))
        assert auto_bet.t60_dispatch_pending(21.1004) == []


class TestCB2NonCopreLaCorsiaHedge:
    """GAP DICHIARATO (non corretto in questo audit: nessuna modifica alla
    produzione): `smart_hedging._blocked_reason` controlla kill switch, stop
    giornaliero e settimanale ma **non il CB2**, e `_live_fill` non lo
    ricontrolla. La corsia hedge puo' quindi piazzare ordini reali con il CB2
    armato se esiste un bet aperto da coprire."""

    def test_hedge_non_vede_il_cb2(self, monkeypatch, temp_db):
        monkeypatch.setenv("AUTO_BET_MODE", "live")
        monkeypatch.setattr(auto_bet, "_provider_ready", lambda: True)
        auto_bet.t60_check_wallet_kill(21.1004)
        assert auto_bet.t60_kill_switch_status()["triggered"] is True

        import smart_hedging
        assert smart_hedging._blocked_reason() is None

    def test_il_kill_switch_invece_lo_blocca(self, monkeypatch, temp_db):
        monkeypatch.setenv("AUTO_BET_MODE", "live")
        monkeypatch.setattr(auto_bet, "_provider_ready", lambda: True)
        auto_bet.KILL_SWITCH_FILE.write_text(json.dumps({"mode": "off"}))
        import smart_hedging
        assert smart_hedging._blocked_reason() == "kill_switch=off"


# ---------------------------------------------------------------------------
# 3. Stop giornaliero e settimanale
# ---------------------------------------------------------------------------

class TestStopLoss:
    def test_perdita_giornaliera_oltre_5pct_blocca(self, tmp_path):
        start = 21.1004
        first = auto_bet.check_daily_stop(start, basis="equity wallet",
                                          basis_key="live_equity")
        assert first["stopped"] is False
        # -5.0% esatti -> la soglia e' `>=`
        stop = auto_bet.check_daily_stop(start * 0.95, basis="equity wallet",
                                         basis_key="live_equity")
        assert stop["stopped"] is True
        assert stop["just_triggered"] is True
        # al giro successivo resta bloccato (stato persistente)
        again = auto_bet.check_daily_stop(start * 0.99, basis="equity wallet",
                                          basis_key="live_equity")
        assert again["stopped"] is True
        assert again["just_triggered"] is False

    def test_perdita_sotto_soglia_non_blocca(self):
        start = 21.1004
        auto_bet.check_daily_stop(start, basis_key="live_equity")
        stop = auto_bet.check_daily_stop(start * 0.96, basis_key="live_equity")
        assert stop["stopped"] is False

    def test_base_meno_autorevole_non_arma_il_blocco(self):
        """Regressione 21/09: cassa (20.00) contro equity (33.55) = -40% finto.

        La lettura su una base MENO autorevole non viene confrontata col
        riferimento preso dall'equity: nessun trigger.
        """
        auto_bet.check_daily_stop(33.5535, basis="equity wallet",
                                  basis_key="live_equity")
        stop = auto_bet.check_daily_stop(20.0, basis="cassa", basis_key="cassa")
        assert stop["stopped"] is False
        assert stop.get("basis_mismatch") or not stop.get("just_triggered")

    def test_drawdown_settimanale_oltre_12pct_blocca(self):
        now = datetime.now(timezone.utc)
        auto_bet.record_bankroll_sample(30.0, basis_key="live_equity",
                                        now=now - timedelta(hours=2),
                                        reconcile=False)
        stop = auto_bet.check_weekly_stop(25.0, basis="equity wallet",
                                          basis_key="live_equity", now=now)
        assert stop["stopped"] is True
        assert stop["drawdown_pct"] >= 12.0
        # 30 -> 25 = 16.7%
        assert stop["drawdown_pct"] == pytest.approx(16.67, abs=0.05)

    def test_drawdown_sotto_soglia_non_blocca(self):
        now = datetime.now(timezone.utc)
        auto_bet.record_bankroll_sample(30.0, basis_key="live_equity",
                                        now=now - timedelta(hours=2),
                                        reconcile=False)
        stop = auto_bet.check_weekly_stop(28.0, basis_key="live_equity", now=now)
        assert stop["stopped"] is False

    def test_rollback_manuale_azzera_solo_un_blocco_attivo(self, tmp_path):
        """La procedura di rollback esiste ed e' idempotente."""
        start = 21.1004
        auto_bet.check_daily_stop(start, basis_key="live_equity")
        auto_bet.check_daily_stop(start * 0.90, basis_key="live_equity")
        assert auto_bet.daily_stop_status()["stopped"] is True
        auto_bet.clear_daily_stop()
        assert auto_bet.daily_stop_status()["stopped"] is False
        auto_bet.clear_daily_stop()            # idempotente
        assert auto_bet.daily_stop_status()["stopped"] is False


# ---------------------------------------------------------------------------
# 4. Errore API (wallet non leggibile) durante un giro LIVE
# ---------------------------------------------------------------------------

class TestErroreApi:
    def test_wallet_non_leggibile_nessun_ordine(self, monkeypatch, temp_db):
        """CONFIGURAZIONE DI PRODUZIONE (Kelly aggressivo attivo).

        In LIVE il saldo si rilegge subito prima degli ordini
        (`refresh_live_stakes`) e una lettura impossibile e' fail-closed:
        nessun candidato, nessun POST, nessuna riga sul ledger.
        """
        monkeypatch.setattr(auto_bet, "_execution_mode", lambda allow_sim=True: "live")
        monkeypatch.setattr(auto_bet, "aggressive_live_active", lambda: True)
        monkeypatch.setattr(auto_bet, "_live_wallet_snapshot",
                            lambda *a, **k: None)          # API giu'
        monkeypatch.setattr(auto_bet, "_today_value_picks", lambda *a, **k: [_pick()])
        for lane in ("_multi_market_picks", "_top_down_picks", "_esports_picks",
                     "_tennis_picks", "_gridiron_picks"):
            monkeypatch.setattr(auto_bet, lane, lambda *a, **k: [])
        monkeypatch.setattr(auto_bet, "_top_down_eval",
                            lambda *a, **k: _verdetto_top_down())
        monkeypatch.setattr(auto_bet, "_harvest_oracle_board", lambda *a, **k: {})
        monkeypatch.setattr(auto_bet, "_live_fill",
                            lambda *a, **k: pytest.fail("ordine tentato senza saldo"))
        assert auto_bet.run_today_bets() == []
        assert tracker._get_conn().execute(
            "SELECT COUNT(*) FROM bets").fetchone()[0] == 0

    def test_wallet_non_leggibile_percorso_legacy_GAP_DICHIARATO(
            self, monkeypatch, temp_db):
        """GAP REALE (dichiarato, NON corretto in questo audit): percorso
        LEGACY (importo fisso, `ORDER_FIXED_STAKE_USDC` > 0).

        Senza `refresh_live_stakes` (che gira solo col Kelly aggressivo) una
        lettura impossibile del wallet fa ripiegare il giro sulla **cassa
        simulata** (100.00) e l'ordine REALE viene tentato. E' la stessa
        classe del falso stop-loss del 21/09 (due basi di misura confuse).
        In produzione oggi il percorso attivo e' quello aggressivo (l'env
        `ORDER_FIXED_STAKE_USDC` e' a 0), quindi il gap NON e' esposto; il
        test lo fissa perche' riaccendere l'importo fisso lo riaprirebbe in
        silenzio.
        """
        monkeypatch.setattr(auto_bet, "_execution_mode", lambda allow_sim=True: "live")
        monkeypatch.setattr(auto_bet, "aggressive_live_active", lambda: False)
        monkeypatch.setattr(auto_bet, "fixed_order_stake", lambda: 1.50)
        monkeypatch.setattr(auto_bet, "_live_wallet_snapshot",
                            lambda *a, **k: None)
        monkeypatch.setattr(auto_bet, "_today_value_picks", lambda *a, **k: [_pick()])
        for lane in ("_multi_market_picks", "_top_down_picks", "_esports_picks",
                     "_tennis_picks", "_gridiron_picks"):
            monkeypatch.setattr(auto_bet, lane, lambda *a, **k: [])
        monkeypatch.setattr(auto_bet, "_top_down_eval",
                            lambda *a, **k: _verdetto_top_down())
        monkeypatch.setattr(auto_bet, "_harvest_oracle_board", lambda *a, **k: {})
        calls = []
        monkeypatch.setattr(auto_bet, "_live_fill",
                            lambda pick, stake, floor: calls.append(stake) or None)
        auto_bet.run_today_bets()
        assert len(calls) == 1, (
            "documenta il gap: nel percorso legacy il wallet illeggibile NON "
            "ferma l'ordine (base = cassa simulata)")

    def test_refresh_live_stakes_e_fail_closed(self, monkeypatch):
        monkeypatch.setattr(auto_bet, "_live_wallet_snapshot", lambda *a, **k: None)
        cands, info = auto_bet.refresh_live_stakes([dict(PICK)])
        assert cands == []
        assert info["ok"] is False
        assert info["reason"] == "wallet_non_leggibile"

    def test_wallet_sotto_il_minimo_ordine_esce(self, monkeypatch, temp_db):
        monkeypatch.setattr(auto_bet, "_execution_mode", lambda allow_sim=True: "live")
        monkeypatch.setattr(auto_bet, "_live_wallet_snapshot",
                            lambda *a, **k: {"available": 0.40, "exposure": 0.0,
                                             "equity": 0.40})
        monkeypatch.setattr(auto_bet, "_today_value_picks", lambda *a, **k: [_pick()])
        monkeypatch.setattr(auto_bet, "_live_fill",
                            lambda *a, **k: pytest.fail("ordine tentato"))
        assert auto_bet.run_today_bets() == []


# ---------------------------------------------------------------------------
# 5. Duplicati: claim atomico per evento + guardia sul ledger
# ---------------------------------------------------------------------------

class TestDuplicato:
    def test_secondo_ordine_sullo_stesso_mercato_respinto(self, temp_db):
        eq = 21.1004
        first = auto_bet._event_guard("0xabc", 1, 1.50, eq)
        assert first["allowed"] is True
        try:
            second = auto_bet._event_guard("0xabc", 1, 1.50, eq)
            assert second["allowed"] is False
            assert second["reason"] in ("event_cap", "duplicate")
        finally:
            auto_bet._release_event_claim(first.get("claim_id"))

    def test_altro_esito_dello_stesso_evento_e_ammesso(self, temp_db):
        eq = 21.1004
        first = auto_bet._event_guard("0xabc", 1, 1.00, eq)
        assert first["allowed"] is True
        try:
            other = auto_bet._event_guard("0xabc", 2, 1.00, eq)
            assert other["allowed"] is True
        finally:
            auto_bet._release_event_claim(first.get("claim_id"))
            auto_bet._release_event_claim((other or {}).get("claim_id"))

    def test_bet_aperta_blocca_lo_stesso_mercato(self, temp_db):
        tracker.save_bet(match_id="m-dup", mercato="1X2", esito="1",
                         price=1.90, stake=1.50,
                         mode="live", market_id="0xdup", selection_id=1,
                         status="FULLY_FILLED")
        assert tracker.bet_exists_open_market("0xdup", 1) is True
        assert tracker.bet_exists_open_market("0xdup", 2) is False

    def test_lettura_impossibile_e_fail_closed(self, monkeypatch):
        """Su DB illeggibile la guardia risponde True: nessun ordine."""
        monkeypatch.setattr(tracker, "DB_PATH", "/nonexistent/dir/x.db")
        assert tracker.bet_exists_open_market("0xwhatever", 1) is True


# ---------------------------------------------------------------------------
# 6. Limiti di stake, recinto, minimo eseguibile
# ---------------------------------------------------------------------------

class TestLimiti:
    def test_tetto_per_ordine_dinamico(self, temp_db):
        from decision.stake_engine import aggressive_config
        pct = aggressive_config()["max_stake_pct"]
        expected = round(21.1004 * pct, 6)
        assert auto_bet.order_ceiling(21.1004) == pytest.approx(expected)
        assert auto_bet.cap_order_stake(999.0, 21.1004) == pytest.approx(expected)

    def test_tetto_non_calcolabile_e_zero(self, temp_db, monkeypatch):
        monkeypatch.setattr(auto_bet, "_LAST_BANKROLL", 0.0)
        assert auto_bet.order_ceiling() == 0.0

    def test_ticket_minimo_coerente_con_il_floor_exchange(self):
        """Il ticket del MOTORE e il floor dell'EXCHANGE sono la stessa soglia."""
        assert auto_bet.MIN_STAKE_EUR == execution_engine.EXECUTION_MIN_STAKE_EUR
        assert auto_bet.MIN_STAKE_EUR == 1.0

    def test_stake_sotto_il_floor_non_passa(self):
        """Il floor (1.00 USDC) e' applicato da `t60_stake` e `normalize_stake`.

        `order_stake` applica il TETTO e il vincolo di cassa, non il floor: la
        verifica sta dove il floor vive davvero (un `min()` a mano in un test
        misura una regola ricopiata, non quella di produzione).
        """
        assert auto_bet.t60_stake(21.1004, mode="live") >= 0.0
        assert auto_bet.t60_stake(0.40) == 0.0                       # < floor
        assert auto_bet.normalize_stake(0.50) == 0.0
        assert auto_bet.normalize_stake(1.00) == pytest.approx(1.00)

    def test_stake_oltre_il_tetto_viene_ridotto(self):
        cap = auto_bet.order_ceiling(21.1004)
        assert auto_bet.order_stake(999.0, spendable=1e9, bankroll=21.1004) \
            == pytest.approx(cap)
        assert auto_bet.t60_stake(21.1004, mode="live") == pytest.approx(
            min(cap, 21.1004 * auto_bet.CORRELATION_CAP_PCT,
                21.1004 * auto_bet.TOTAL_EXPOSURE_CAP_PCT), abs=0.01)

    def test_cassa_limita_lo_stake(self):
        """I soldi liberi sono un vincolo separato dal cap percentuale.

        Con una cassa (0.80) sotto il tetto (5% dell'equity = 1.055), lo stake
        scende alla cassa: i fondi in escrow non si spendono due volte.
        """
        assert auto_bet.order_stake(2.0, spendable=0.80, bankroll=21.1004) \
            == pytest.approx(0.80)

    def test_recinto_proietta_e_respinge(self, temp_db):
        eq = 21.1004
        cap = round(eq * auto_bet.OPEN_EXPOSURE_CAP_PCT, 2)
        ok = auto_bet.exposure_allows(eq, cap - 0.01)
        assert ok["allowed"] is True
        no = auto_bet.exposure_allows(eq, cap + 0.01)
        assert no["allowed"] is False
        assert "tetto" in no["reason"]

    def test_profondita_richiesta(self):
        assert auto_bet.required_depth(1.0) == pytest.approx(20.0)
        assert auto_bet.required_depth(20.0) > 20.0

    def test_cap_per_evento_e_proporzionale(self, temp_db):
        assert auto_bet.event_stake_cap(21.1004) == pytest.approx(
            round(21.1004 * auto_bet.MAX_STAKE_PER_EVENT_PCT, 6))
