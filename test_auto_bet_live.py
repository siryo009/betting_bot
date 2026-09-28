"""Test del wiring auto_bet → execution_engine (ordini REALI, dal 08/09).

La modalità live si attiva SOLO con AUTO_BET_MODE=live|real E provider reale
configurato (EXECUTION_PROVIDER + credenziali, niente DryRun). Con un ordine
riempito la riga in `bets` è mode='live' con market_id/selection_id/bet_id;
gli ordini rifiutati o i salti (mercato assente, prezzo sotto il floor EV)
NON lasciano righe sul ledger (un FAILED verrebbe saldato come perdita).
Senza provider reale configurato si ripiega sulla SIM (o fail-closed se il
chiamante passa allow_sim=False).
"""
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import tracker
import auto_bet


@pytest.fixture()
def temp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        monkeypatch.setattr(tracker, "DB_PATH", db_path)
        tracker.init_db()
        yield db_path


@pytest.fixture(autouse=True)
def _env_clean(monkeypatch):
    """I test partono sempre da AUTO_BET_MODE non impostato."""
    monkeypatch.delenv("AUTO_BET_MODE", raising=False)
    yield


@pytest.fixture(autouse=True)
def _isolate_daily_stop(tmp_path, monkeypatch):
    """Lo stop-loss giornaliero usa un file temporaneo (mai il volume reale)."""
    monkeypatch.setattr(auto_bet, "DAILY_STOP_FILE", tmp_path / "daily_stop.json")
    # CB2 (T-60, 17/09): la soglia patrimoniale del kill switch (30 USDC) NON
    # e' l'oggetto di questi test — i wallet finti sono volutamente piccoli
    # (12.28 / 3.0 / 0.5 USDC, gli importi documentati in AGENTS.md) e con la
    # soglia reale il giro risulterebbe "arrestato": ogni asserzione di
    # staking misurerebbe il CB2 invece del cap che vuole verificare (e i test
    # che attendono [] passerebbero per il motivo sbagliato). Il CB2 ha i suoi
    # tripwire dedicati in test_t60_breakers.py, che usano la soglia vera.
    monkeypatch.setattr(auto_bet, "T60_KILL_WALLET_USDC", 0.0)
    # Fase 2 top-down (25/09): il gate EV sull'oracolo Pinnacle governa la
    # corsia LIVE e senza oracolo e' fail-closed (no_oracle -> skip). Questi
    # test verificano staking/cap/wallet, non l'EV: l'oracolo e' stubbato con
    # una p_true che fa scattare il trigger per QUALSIASI esito (0.65 su quota
    # 1.65 -> EV +7.25% >= 2%). La semantica del gate (no_oracle, trigger,
    # bypass del modello) e' verificata in test_top_down.py.
    monkeypatch.setattr(auto_bet, "_top_down_load",
                        lambda home, away: {"1": 0.65, "X": 0.65, "2": 0.65,
                                            "overround": 0.0})


@pytest.fixture(autouse=True)
def _isolate_enclosure(monkeypatch):
    """Recinto di esposizione aperta LIBERO (direttiva 28/09/2026).

    Questi test misurano il tetto del WALLET, i cap di portafoglio e lo
    staking: il recinto del 40% e' un'altra cosa e con un wallet di 3 USDC
    respingerebbe QUALSIASI ordine (cap 1.20 < stake), nascondendo la
    grandezza che il test vuole verificare. Il recinto VERO ha i suoi tripwire
    in `test_exposure_gate.py` e `test_capital_enclosure.py` (che non sono
    isolati).
    """
    monkeypatch.setattr(auto_bet, "exposure_allows",
                        lambda bankroll, stake=0.0: {
                            "allowed": True, "blocked": False,
                            "open_stake": 0.0, "count": 0,
                            "cap": round(max(float(bankroll or 0.0), 0.0) * 0.40, 2),
                            "bankroll": round(float(bankroll or 0.0), 2),
                            "pct": 0.0, "new_stake": round(float(stake or 0.0), 2),
                            "projected": round(float(stake or 0.0), 2),
                            "reason": ""})


ALLOWED_LEAGUE = "Premier League"   # in STRATEGY_LEAGUES
# Le 5 leghe della strategia: servono a dare leghe DISTINTE (e ammesse) ai
# test di correlazione/esposizione senza uscire dal gate di lega.
ALLOWED_LEAGUES = ("Premier League", "Bundesliga", "Ligue 1",
                   "Eredivisie", "Turkey Super Lig")


def _seed_value_match(mid="m1", home="Osasuna", away="Getafe", esito="1",
                      quota=1.65, status="value", commence=None):
    start = commence or (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat().replace("+00:00", "Z")
    # Lega AMMESSA dalla strategia: dal 15/09 la corsia ordini applica il
    # gate STRATEGY_LEAGUES (una lega vietata -> 0 puntate).
    tracker.save_match(mid, ALLOWED_LEAGUE, home, away, start)
    best_esito = home if esito == "1" else (away if esito == "2" else "Draw")
    tracker.save_analysis(mid, 1.7, 1.1, 0.52, 0.27, 0.21, 0.58, 0.08,
                          best_esito, quota, "Pinnacle", status,
                          market_prob=0.60, market_edge=0.07)
    # Ledger previsioni: _today_value_picks legge da QUI dal 09/09.
    # quota 1.65 / prob. 0.60 = favorito netto (strategia 11/09).
    tracker.save_prediction(mid, "1X2", best_esito, quota, 0.52, 0.08,
                            market_prob=0.60, market_edge=0.07, status=status)


def _fixed_stake(monkeypatch):
    """Stake fisso (adaptive assente) per avere stake deterministici."""
    monkeypatch.setitem(sys.modules, "adaptive_staking", None)


def _filled():
    """Esito positivo simulato di _live_fill: ordine SX riempito."""
    return {"ok": True, "market_id": "0xbb4826699a0c7d80", "selection_id": 1,
            "bet_id": "0xabc123", "status": "FULLY_FILLED",
            "price": 1.65, "stake": 5.0}


def _wallet(available, exposure=0.0):
    """Istantanea del wallet reale: disponibile + in gioco = EQUITY."""
    return {"available": available, "exposure": exposure,
            "equity": available + exposure}


def _stub_wallet(monkeypatch, available, exposure=0.0):
    """Sostituisce la lettura del wallet con un'istantanea fissa.

    Dal 15/09 il modulo legge `_live_wallet_snapshot` (non piu' il solo
    disponibile): il test controlla ESPLICITAMENTE anche l'esposizione, che e'
    la grandezza che il fix dello stop-loss deve tenere in conto.
    """
    monkeypatch.setattr(auto_bet, "_live_wallet_snapshot",
                        lambda: _wallet(available, exposure))


def _sx_catalogue(home="Osasuna", away="Getafe", ts=None):
    """Tre mercati binari 'X vs Not X' di un evento SX Bet (1X2)."""
    if ts is None:
        ts = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat().replace("+00:00", "Z")

    def row(mid, o1):
        return {"market_id": mid, "event_name": f"{home} vs {away}",
                "open_date": ts, "team_one_name": home, "team_two_name": away,
                "outcome_one_name": o1, "outcome_two_name": f"Not {o1}",
                "runners": [{"selection_id": 1, "name": o1},
                            {"selection_id": 2, "name": f"Not {o1}"}]}

    return [row("m-home", home), row("m-away", away), row("m-tie", "Tie")]


class TestLiveMode:
    def test_ordine_reale_riempito_salva_mode_live(self, monkeypatch, temp_db):
        """AUTO_BET_MODE=live + ordine riempito: riga `bets` con mode='live',
        market_id/selection_id/bet_id reali e stake/prezzo matched."""
        _fixed_stake(monkeypatch)
        _seed_value_match(quota=1.65)
        monkeypatch.setattr(auto_bet, "_execution_mode",
                            lambda allow_sim=True: "live")
        monkeypatch.setattr(auto_bet, "_live_fill",
                            lambda pick, stake, floor: _filled())

        placed = auto_bet.run_today_bets(stake_eur=5.0)
        assert len(placed) == 1
        p = placed[0]
        assert p["mode"] == "live" and p["status"] == "FULLY_FILLED"
        assert p["market_id"] == "0xbb4826699a0c7d80"
        assert p["selection_id"] == 1 and p["bet_id"] == "0xabc123"
        assert p["price"] == 1.65 and p["stake"] == 5.0

        bets = tracker.get_bets()
        assert len(bets) == 1
        b = bets[0]
        assert b["mode"] == "live"
        assert b["market_id"] == "0xbb4826699a0c7d80"
        assert b["selection_id"] == 1 and b["bet_id"] == "0xabc123"
        assert b["price"] == 1.65 and b["stake"] == 5.0
        assert tracker.bet_exists_open("m1", "1") is True

    def test_riempito_senza_bet_id_non_lascia_righe(self, monkeypatch,
                                                    temp_db):
        """Il gate categorico: senza bet_id NIENTE riga mode='live'.

        E' la regressione del sintomo "il DB dice successo ma su SX Bet non
        c'e' nulla". Una riga live e' la prova che un ordine esiste: se
        l'exchange non ha emesso un id, la riga non si scrive (fail-closed).
        """
        _fixed_stake(monkeypatch)
        _seed_value_match(quota=1.65)
        monkeypatch.setattr(auto_bet, "_execution_mode",
                            lambda allow_sim=True: "live")
        senza_id = dict(_filled())
        senza_id["bet_id"] = None
        monkeypatch.setattr(auto_bet, "_live_fill",
                            lambda pick, stake, floor: senza_id)

        placed = auto_bet.run_today_bets(stake_eur=5.0)
        assert placed == []                     # niente nei riepiloghi
        assert tracker.get_bets() == []         # niente sul ledger
        assert tracker.bet_exists_open("m1", "1") is False

    def test_ordine_non_riempito_non_lascia_righe(self, monkeypatch, temp_db):
        """Ordine rifiutato/non riempito dall'exchange: nessuna riga sul
        ledger (un FAILED verrebbe saldato come perdita reale)."""
        _fixed_stake(monkeypatch)
        _seed_value_match(quota=1.65)
        monkeypatch.setattr(auto_bet, "_execution_mode",
                            lambda allow_sim=True: "live")
        monkeypatch.setattr(
            auto_bet, "_live_fill",
            lambda pick, stake, floor: {"ok": False, "market_id": "0xm",
                                        "selection_id": 1,
                                        "status": "FAILURE",
                                        "error": "INSUFFICIENT_LIQUIDITY"})
        placed = auto_bet.run_today_bets(stake_eur=5.0)
        assert placed == []
        assert tracker.get_bets() == []
        assert tracker.bet_exists_open("m1", "1") is False

    def test_salto_mercato_non_trovato_non_lascia_righe(self, monkeypatch, temp_db):
        """Mercato SX non trovato/ambiguo (o prezzo sotto il floor EV):
        _live_fill ritorna None -> nessun ordine, nessuna riga."""
        _fixed_stake(monkeypatch)
        _seed_value_match(quota=1.65)
        monkeypatch.setattr(auto_bet, "_execution_mode",
                            lambda allow_sim=True: "live")
        monkeypatch.setattr(auto_bet, "_live_fill",
                            lambda pick, stake, floor: None)
        placed = auto_bet.run_today_bets(stake_eur=5.0)
        assert placed == []
        assert tracker.get_bets() == []

class TestLiveBankroll:
    """Staking dinamico sul wallet REALE (dal 08/09): il Kelly usa come
    bankroll l'EQUITY del proxy wallet SX (disponibile + in gioco), mai la
    cassa simulata. Floor ordine 1 USDC, cap di cassa sul disponibile."""

    def test_bankroll_dal_wallet(self, monkeypatch, temp_db):
        """In LIVE il bankroll del Kelly e' il valore del wallet (12.28
        USDC nell'esempio), non la cassa."""
        import adaptive_staking
        _seed_value_match(quota=1.65)
        monkeypatch.setattr(auto_bet, "_execution_mode",
                            lambda allow_sim=True: "live")
        _stub_wallet(monkeypatch, 12.28)
        calls = {}

        def _fake(**kw):
            calls.update(kw)
            return {"stake": 2.0, "reason": "test"}

        monkeypatch.setattr(adaptive_staking, "adaptive_stake", _fake)
        monkeypatch.setattr(auto_bet, "_live_fill",
                            lambda pick, stake, floor: _filled())
        placed = auto_bet.run_today_bets(stake_eur=5.0)
        assert len(placed) == 1
        assert calls["bankroll"] == pytest.approx(12.28)
        assert calls["peak_bankroll"] == pytest.approx(12.28)

    def test_wallet_sotto_minimo_nessuna_puntata(self, monkeypatch, temp_db):
        """Wallet sotto il minimo ordine (1 USDC): fail-closed, niente righe."""
        _fixed_stake(monkeypatch)
        _seed_value_match(quota=1.65)
        monkeypatch.setattr(auto_bet, "_execution_mode",
                            lambda allow_sim=True: "live")
        _stub_wallet(monkeypatch, 0.5)
        placed = auto_bet.run_today_bets(stake_eur=5.0)
        assert placed == []
        assert tracker.get_bets() == []

    def _micro_stake(self, monkeypatch):
        """Adaptive che cappa lo stake a 0.6 USDC (sotto il minimo ordine)."""
        import adaptive_staking
        _seed_value_match(quota=1.65)
        monkeypatch.setattr(auto_bet, "_execution_mode",
                            lambda allow_sim=True: "live")
        _stub_wallet(monkeypatch, 12.28)
        monkeypatch.setattr(
            adaptive_staking, "adaptive_stake",
            lambda **kw: {"stake": 0.6, "reason": "micro", "capped": True})

    def test_cap_severo_salta_sotto_il_minimo(self, monkeypatch, temp_db):
        """CAP SEVERO (11/09, default): stake cappato 0.6 < minimo ordine
        1 USDC -> ordine SALTATO, mai alzato al floor (sforerebbe il cap)."""
        assert auto_bet.cap_hard_active() is True
        self._micro_stake(monkeypatch)
        sent = []
        monkeypatch.setattr(auto_bet, "_live_fill",
                            lambda pick, stake, floor: sent.append(stake))
        placed = auto_bet.run_today_bets(stake_eur=5.0)
        assert placed == [] and sent == []
        assert tracker.get_bets() == []

    def test_cap_disattivato_alza_al_floor(self, monkeypatch, temp_db):
        """Con STAKE_CAP_HARD=0 si accetta il floor dell'exchange (1 USDC):
        il cap non e' piu' vincolante, la bet viene piazzata."""
        monkeypatch.setattr(auto_bet, "STAKE_CAP_HARD", False)
        assert auto_bet.cap_hard_active() is False
        self._micro_stake(monkeypatch)
        sent = {}

        def _fake_fill(pick, stake, floor):
            sent["stake"] = stake
            return _filled()

        monkeypatch.setattr(auto_bet, "_live_fill", _fake_fill)
        placed = auto_bet.run_today_bets(stake_eur=5.0)
        assert len(placed) == 1
        assert sent["stake"] == 1.0

    def test_stake_mai_oltre_il_wallet(self, monkeypatch, temp_db):
        """In LIVE lo stake non supera mai il saldo disponibile del wallet."""
        import adaptive_staking
        _seed_value_match(quota=1.65)
        monkeypatch.setattr(auto_bet, "_execution_mode",
                            lambda allow_sim=True: "live")
        _stub_wallet(monkeypatch, 3.0)
        monkeypatch.setattr(
            adaptive_staking, "adaptive_stake",
            lambda **kw: {"stake": 5.0, "reason": "test"})
        sent = {}

        def _fake_fill(pick, stake, floor):
            sent["stake"] = stake
            return _filled()

        monkeypatch.setattr(auto_bet, "_live_fill", _fake_fill)
        placed = auto_bet.run_today_bets(stake_eur=5.0)
        assert len(placed) == 1
        assert sent["stake"] == 3.0

    def test_saldo_non_leggibile_ripiega_sulla_cassa(self, monkeypatch,
                                                     temp_db):
        """Wallet non leggibile (rete/errore): fallback sul bankroll cassa,
        il giro prosegue in live."""
        import adaptive_staking
        _seed_value_match(quota=1.65)
        monkeypatch.setattr(auto_bet, "_execution_mode",
                            lambda allow_sim=True: "live")
        monkeypatch.setattr(auto_bet, "_live_wallet_snapshot", lambda: None)
        calls = {}

        def _fake(**kw):
            calls.update(kw)
            return {"stake": 2.0, "reason": "test"}

        monkeypatch.setattr(adaptive_staking, "adaptive_stake", _fake)
        monkeypatch.setattr(auto_bet, "_live_fill",
                            lambda pick, stake, floor: _filled())
        placed = auto_bet.run_today_bets(stake_eur=5.0)
        assert len(placed) == 1
        # cassa vuota -> default 100.0
        assert calls["bankroll"] == pytest.approx(100.0)

    def test_esposizione_gia_piazzata_blocca_nuovi_ordini(self, monkeypatch,
                                                          temp_db):
        """24/7: se il budget di esposizione del giorno e' gia' consumato
        dai giri precedenti (already_placed >= cap), il nuovo giro non
        piazza nulla (fail-closed)."""
        import adaptive_staking
        _seed_value_match(mid="e1", home="Osasuna", away="Getafe", quota=1.65)
        _seed_value_match(mid="e2", home="Bari", away="Crotone", quota=1.65)
        monkeypatch.setattr(auto_bet, "_execution_mode",
                            lambda allow_sim=True: "live")
        _stub_wallet(monkeypatch, 12.28)
        monkeypatch.setattr(
            adaptive_staking, "adaptive_stake",
            lambda **kw: {"stake": 2.0, "reason": "test"})
        # cap 40% di 12.28 = 4.91 < gia' piazzato 6.0 -> budget esaurito
        monkeypatch.setattr(auto_bet, "_today_placed_stake", lambda: 6.0)
        placed = auto_bet.run_today_bets(stake_eur=5.0)
        assert placed == []
        assert tracker.get_bets() == []

    def test_stake_matched_usato_se_diverso(self, monkeypatch, temp_db):
        """Rippegno parziale: si registra lo stake/prezzo EFFETTIVAMENTE
        riempiti (non il richiesto)."""
        _fixed_stake(monkeypatch)
        _seed_value_match(quota=1.65)
        monkeypatch.setattr(auto_bet, "_execution_mode",
                            lambda allow_sim=True: "live")
        filled = _filled()
        filled["stake"] = 3.0          # fill parziale di 5 richiesti
        filled["price"] = 1.70         # matched meglio del floor 1.65
        monkeypatch.setattr(auto_bet, "_live_fill",
                            lambda pick, stake, floor: filled)

        placed = auto_bet.run_today_bets(stake_eur=5.0)
        assert placed[0]["stake"] == 3.0 and placed[0]["price"] == 1.70
        b = tracker.get_bets()[0]
        assert b["stake"] == 3.0 and b["price"] == 1.70


class TestBankrollEquity:
    """Fix 15/09/2026: in LIVE il valore di RISCHIO e' l'EQUITY del wallet.

    Prima lo stop-loss leggeva il solo `availableBalance`: piazzare una bet
    sposta i fondi da "libero" a "in escrow" e il calo del disponibile veniva
    letto come una perdita -> stop armato pochi secondi dopo l'ordine, per 24h
    (in produzione: 35.98 -> 33.98 con 2.0 in gioco, bot muto per un giorno).
    Ora bankroll, drawdown e stop-loss usano disponibile + in gioco; il
    DISPONIBILE resta il vincolo di cassa del singolo ordine.
    """

    def _live(self, monkeypatch):
        monkeypatch.setattr(auto_bet, "_execution_mode",
                            lambda allow_sim=True: "live")

    def test_bet_piazzata_non_arma_lo_stop(self, monkeypatch, temp_db):
        """Disponibile 35.98 -> 33.98 con 2.0 in escrow: l'equity resta
        35.98, quindi nessuno stop (regressione del bug)."""
        _fixed_stake(monkeypatch)
        _seed_value_match(quota=1.65)
        self._live(monkeypatch)
        _stub_wallet(monkeypatch, 35.98)
        monkeypatch.setattr(auto_bet, "_live_fill",
                            lambda pick, stake, floor: None)
        assert auto_bet.run_today_bets(stake_eur=5.0) == []
        st = auto_bet.daily_stop_status()
        assert st["start_bankroll"] == pytest.approx(35.98)
        # la bet e' passata in escrow: 2 USDC in meno liberi, 2 in gioco
        _stub_wallet(monkeypatch, 33.98, exposure=2.0)
        auto_bet.run_today_bets(stake_eur=5.0)
        st = auto_bet.daily_stop_status()
        assert st["stopped"] is False
        assert st["start_bankroll"] == pytest.approx(35.98)

    def test_perdita_vera_sull_equity_arma_lo_stop(self, monkeypatch, temp_db):
        """Equity 35.98 -> 32.0 (-11%): lo stop scatta ancora, e il motivo
        dichiara su QUALE valore e' stata misurata la perdita."""
        _fixed_stake(monkeypatch)
        _seed_value_match(quota=1.65)
        self._live(monkeypatch)
        _stub_wallet(monkeypatch, 35.98)
        auto_bet.run_today_bets(stake_eur=5.0)
        _stub_wallet(monkeypatch, 30.0, exposure=2.0)      # equity 32.0
        assert auto_bet.run_today_bets(stake_eur=5.0) == []
        st = auto_bet.daily_stop_status()
        assert st["stopped"] is True
        assert "equity wallet" in st["reason"]

    def test_stake_non_oltre_i_fondi_liberi(self, monkeypatch, temp_db):
        """L'equity (35.98) dimensiona il Kelly, ma un singolo ordine non puo'
        spendere piu' dei fondi LIBERI (3.98 su 32.0 in gioco)."""
        _fixed_stake(monkeypatch)
        _seed_value_match(quota=1.65)
        self._live(monkeypatch)
        _stub_wallet(monkeypatch, 3.98, exposure=32.0)
        sent = {}
        monkeypatch.setattr(auto_bet, "_live_fill",
                            lambda pick, stake, floor: sent.update(stake=stake)
                            or _filled())
        placed = auto_bet.run_today_bets(stake_eur=5.0)
        assert len(placed) == 1
        assert sent["stake"] == pytest.approx(3.98)

    class _Prov:
        name = "sxbet"

        def __init__(self, bal):
            self._bal = bal

        def get_balance(self):
            return self._bal

    def _engine(self, monkeypatch, provider):
        import execution_engine as ee
        engine = type("_Eng", (), {"provider": provider})()
        monkeypatch.setattr(ee, "ExecutionEngine", lambda *a, **k: engine)
        return ee

    def test_snapshot_legge_disponibile_e_esposizione(self, monkeypatch):
        self._engine(monkeypatch, self._Prov({"availableBalance": 33.98,
                                              "exposure": 2.0}))
        assert auto_bet._live_wallet_snapshot() == {
            "available": 33.98, "exposure": 2.0, "equity": 35.98}

    def test_snapshot_senza_exposure_e_prudente(self, monkeypatch):
        """Provider che non espone l'esposizione: equity = solo disponibile.
        La stima puo' far scattare lo stop PRIMA, mai dopo (fail-closed)."""
        self._engine(monkeypatch, self._Prov({"availableBalance": 10.0}))
        snap = auto_bet._live_wallet_snapshot()
        assert snap["exposure"] == 0.0 and snap["equity"] == 10.0

    def test_snapshot_dry_run_e_errore_danno_none(self, monkeypatch):
        import execution_engine as ee
        self._engine(monkeypatch, ee.DryRunProvider())
        assert auto_bet._live_wallet_snapshot() is None

        class _Boom:
            name = "sxbet"

            def get_balance(self):
                raise RuntimeError("rete giu")

        self._engine(monkeypatch, _Boom())
        assert auto_bet._live_wallet_snapshot() is None


class TestFlatLive:
    """Flat-stake LIVE (09/09): 1 USDC fisso per ogni segnale +EV del
    Calcio 1X2, con risk cap a unita' intere sul saldo REALE del wallet.
    Con ~12 USDC: cap esposizione 40% = ~4.9 -> max 4 ordini/giorno;
    cap correlazione 30% = ~3.7 -> max 3 ordini per blocco correlato.
    """

    def _seed_n(self, n, league_prefix=True):
        for i in range(n):
            _seed_value_match(mid=f"f{i}", home=f"Home{i}", away=f"Away{i}",
                              esito="1" if i % 2 == 0 else "2", quota=1.65)
        if league_prefix:
            conn = tracker._get_conn()
            rows = conn.execute("SELECT id FROM matches WHERE id LIKE 'f%'")\
                .fetchall()
            for i, (mid,) in enumerate(rows):
                conn.execute("UPDATE matches SET league = ? WHERE id = ?",
                             (ALLOWED_LEAGUES[i % len(ALLOWED_LEAGUES)], mid))
            conn.commit()
            conn.close()

    def _setup_live(self, monkeypatch, wallet=12.28):
        _fixed_stake(monkeypatch)
        monkeypatch.setattr(auto_bet, "STAKE_MODE", "flat")
        monkeypatch.setattr(auto_bet, "_execution_mode",
                            lambda allow_sim=True: "live")
        _stub_wallet(monkeypatch, wallet)
        sent = []

        def fake_fill(pick, stake, floor):
            sent.append((pick["match_id"], stake))
            return {"ok": True, "market_id": "0x" + pick["match_id"],
                    "selection_id": 1, "bet_id": "0xb" + pick["match_id"],
                    "status": "FULLY_FILLED", "price": floor, "stake": stake}

        monkeypatch.setattr(auto_bet, "_live_fill", fake_fill)
        return sent

    def test_flat_live_un_usdc_a_segno_cap_totale(self, monkeypatch, temp_db):
        """5 value in leghe diverse su wallet 12.28: entrano 4 segni da 1
        USDC (cap esposizione 40% ~4.91), il 5° esce."""
        self._seed_n(5)
        sent = self._setup_live(monkeypatch)
        placed = auto_bet.run_today_bets(stake_eur=5.0)
        assert len(placed) == 4
        assert all(p["stake"] == 1.0 for p in placed)
        assert all(p["mode"] == "live" for p in placed)
        assert len(sent) == 4 and all(s == 1.0 for _, s in sent)
        # sul ledger: 4 righe live da 1 USDC
        bets = tracker.get_bets()
        assert len(bets) == 4 and all(b["stake"] == 1.0 for b in bets)

    def test_flat_live_blocco_correlato_max_3(self, monkeypatch, temp_db):
        """5 value della STESSA lega e stesso kickoff (blocco correlato):
        cap correlazione 30% di 12.28 = ~3.7 -> solo 3 segni da 1 USDC."""
        self._seed_n(5, league_prefix=False)  # stessa lega, stesso kickoff
        sent = self._setup_live(monkeypatch)
        placed = auto_bet.run_today_bets(stake_eur=5.0)
        assert len(placed) == 3
        assert all(p["stake"] == 1.0 for p in placed)
        assert len(sent) == 3

    def test_flat_live_budget_esaurito_nessun_ordine(self, monkeypatch,
                                                      temp_db):
        """Budget del giorno gia' consumato (already_placed >= cap): il
        giro non piazza nulla."""
        self._seed_n(2)
        self._setup_live(monkeypatch)
        monkeypatch.setattr(auto_bet, "_today_placed_stake", lambda: 5.0)
        placed = auto_bet.run_today_bets(stake_eur=5.0)
        assert placed == []
        assert tracker.get_bets() == []


class TestModeSelection:
    def test_default_senza_env_resta_sim(self, monkeypatch, temp_db):
        _fixed_stake(monkeypatch)
        _seed_value_match(quota=1.65)
        placed = auto_bet.run_today_bets(stake_eur=5.0)
        assert len(placed) == 1 and placed[0]["mode"] == "sim"

    def test_live_richiesto_senza_provider_ripiega_sim(self, monkeypatch, temp_db):
        """AUTO_BET_MODE=live ma provider non configurato in questa env:
        nessun ordine reale — fallback SIM (allow_sim default True)."""
        monkeypatch.setenv("AUTO_BET_MODE", "live")
        _fixed_stake(monkeypatch)
        _seed_value_match(quota=1.65)
        placed = auto_bet.run_today_bets(stake_eur=5.0)
        assert len(placed) == 1 and placed[0]["mode"] == "sim"

    def test_live_richiesto_senza_provider_fail_closed(self, monkeypatch, temp_db):
        """allow_sim=False senza provider configurato: nessuna puntata."""
        monkeypatch.setenv("AUTO_BET_MODE", "live")
        _fixed_stake(monkeypatch)
        _seed_value_match(quota=1.65)
        placed = auto_bet.run_today_bets(stake_eur=5.0, allow_sim=False)
        assert placed == []
        assert tracker.get_bets() == []

    def test_provider_ready_riconosce_sxbet_configurato(self, monkeypatch):
        """_provider_ready deve leggere i flag del modulo execution_engine
        (monkeypatchati): con sxbet + credenziali -> True."""
        import execution_engine as ee
        monkeypatch.setattr(ee, "EXECUTION_DRY_RUN", False)
        monkeypatch.setattr(ee, "EXECUTION_PROVIDER", "sxbet")
        monkeypatch.setattr(ee, "_creds_configured", lambda: True)
        assert auto_bet._provider_ready() is True

    def test_provider_ready_dry_run_falso(self, monkeypatch):
        import execution_engine as ee
        monkeypatch.setattr(ee, "EXECUTION_DRY_RUN", True)
        monkeypatch.setattr(ee, "EXECUTION_PROVIDER", "sxbet")
        monkeypatch.setattr(ee, "_creds_configured", lambda: True)
        assert auto_bet._provider_ready() is False


class TestLiveFill:
    """La VERA _live_fill con ExecutionEngine finto (nessuna rete): floor EV,
    risoluzione mercato e riporto dell'esito dell'ordine."""

    def _setup(self, monkeypatch, provider):
        import execution_engine as ee
        _fixed_stake(monkeypatch)
        engine = type("_Eng", (), {"provider": provider})()
        monkeypatch.setattr(ee, "ExecutionEngine", lambda *a, **k: engine)
        return ee

    def _pick(self, **kw):
        base = {"match_id": "m1", "home": "Osasuna", "away": "Getafe",
                "esito_key": "1", "mercato": "1X2",
                "commence": (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat().replace("+00:00", "Z")}
        base.update(kw)
        return base

    class _Prov:
        name = "sxbet"

        def __init__(self, best=None, order=None, catalogue=None):
            self.best = best
            self.order = order
            self.catalogue = catalogue or _sx_catalogue()
            self.place_calls = []

        def best_back_price(self, market_id, selection_id):
            return self.best

        def list_market_catalogue(self, event_type_ids=("5",),
                                  market_type="1X2", max_results=400):
            return self.catalogue

        def place_limit_order(self, market_id, selection_id, side, price,
                              size, persistence="LAPSE"):
            self.place_calls.append((market_id, selection_id, side, price, size))
            return self.order

    def test_floor_ev_best_sotto_quota_salta(self, monkeypatch):
        """Best SX 1.60 < floor segnale 1.65: niente ordine (EV perso)."""
        import execution_engine as ee
        prov = self._Prov(best=1.60)
        self._setup(monkeypatch, prov)
        res = auto_bet._live_fill(self._pick(), stake=5.0, floor=1.65)
        assert res is None
        assert prov.place_calls == []

    def test_ordine_riempito_al_floor_o_meglio(self, monkeypatch):
        import execution_engine as ee
        order = ee.OrderResult(True, "0x9", "FULLY_FILLED", 1.65, 1.70,
                               5.0, 12.0)
        prov = self._Prov(best=None, order=order)
        self._setup(monkeypatch, prov)
        res = auto_bet._live_fill(self._pick(), stake=5.0, floor=1.65)
        assert res and res["ok"] is True
        assert res["market_id"] == "m-home" and res["selection_id"] == 1
        assert res["bet_id"] == "0x9" and res["status"] == "FULLY_FILLED"
        assert res["price"] == 1.70 and res["stake"] == 5.0
        # ordine richiesto al floor EV (bound), non sotto
        assert prov.place_calls[0][2] == "BACK"
        assert prov.place_calls[0][3] == 1.65

    def test_ordine_riempito_senza_bet_id_non_e_piazzato(self, monkeypatch):
        """Riempito ma senza bet_id: `_live_fill` NON lo dichiara piazzato.

        Lo stato di riempimento senza l'id emesso dall'exchange non e'
        verificabile sull'interfaccia reale: fail-closed, cosi' il ledger non
        puo' contenere un "successo" che sulla piattaforma non esiste.
        """
        import execution_engine as ee
        order = ee.OrderResult(True, None, "FULLY_FILLED", 1.65, 1.70,
                               5.0, 12.0)
        prov = self._Prov(best=None, order=order)
        self._setup(monkeypatch, prov)
        res = auto_bet._live_fill(self._pick(), stake=5.0, floor=1.65)
        assert res is not None and res["ok"] is False
        assert res["bet_id"] is None
        assert "bet_id" in (res["error"] or "")

    def test_ordine_rifiutato_riporta_ok_false(self, monkeypatch):
        import execution_engine as ee
        order = ee.OrderResult(False, None, "FAILURE", 1.65, None, 0.0, 5.0,
                               error="INSUFFICIENT_FUNDS")
        prov = self._Prov(best=None, order=order)
        self._setup(monkeypatch, prov)
        res = auto_bet._live_fill(self._pick(), stake=5.0, floor=1.65)
        assert res is not None and res["ok"] is False
        assert res["status"] == "FAILURE"

    def test_mercato_ambiguo_salta(self, monkeypatch):
        # Due eventi candidati (stessi nomi, kickoff diversi): ambiguo -> skip
        c1 = _sx_catalogue()
        c2 = _sx_catalogue()
        for m in c2:
            m["market_id"] = "x-" + m["market_id"]
            m["open_date"] = (datetime.now(timezone.utc) + timedelta(hours=4)).isoformat().replace("+00:00", "Z")
        prov = self._Prov(best=None, order=None, catalogue=c1 + c2)
        self._setup(monkeypatch, prov)
        res = auto_bet._live_fill(self._pick(), stake=5.0, floor=1.65)
        assert res is None


class TestBaseStopLossDichiarata:
    """Il chiamante dichiara la BASE della lettura dello stop-loss:
    'live_equity' SOLO se il wallet e' stato letto davvero, 'cassa' quando il
    giro ripiega sulla cassa simulata (regressione del 21/09/2026: basi
    diverse confrontate fra loro = -40.4% inesistente e 24h di stop col
    wallet intatto).
    """

    def _capture(self, monkeypatch):
        calls = []

        def _spy(bankroll, basis="bankroll", basis_key=None):
            calls.append((bankroll, basis, basis_key))
            return {"stopped": False, "just_triggered": False}

        monkeypatch.setattr(auto_bet, "check_daily_stop", _spy)
        return calls

    def test_wallet_letto_dichiara_equity(self, monkeypatch, temp_db):
        calls = self._capture(monkeypatch)
        _seed_value_match(quota=1.65)
        monkeypatch.setattr(auto_bet, "_execution_mode",
                            lambda allow_sim=True: "live")
        _stub_wallet(monkeypatch, 33.5535)
        monkeypatch.setattr(auto_bet, "_live_fill",
                            lambda pick, stake, floor: _filled())
        auto_bet.run_today_bets(stake_eur=1.0)
        assert calls, "lo stop-loss deve essere valutato nel giro"
        bankroll, basis, basis_key = calls[0]
        assert basis == "equity wallet" and basis_key == "live_equity"
        assert bankroll == pytest.approx(33.5535)

    def test_wallet_illeggibile_dichiara_cassa(self, monkeypatch, temp_db):
        """Regressione 21/09: col wallet illeggibile la lettura E' la cassa e
        va dichiarata come tale — mai etichettata 'equity wallet'."""
        calls = self._capture(monkeypatch)
        _seed_value_match(quota=1.65)
        monkeypatch.setattr(auto_bet, "_execution_mode",
                            lambda allow_sim=True: "live")
        monkeypatch.setattr(auto_bet, "_live_wallet_snapshot", lambda: None)
        monkeypatch.setattr(auto_bet, "_live_fill",
                            lambda pick, stake, floor: _filled())
        auto_bet.run_today_bets(stake_eur=1.0)
        assert calls, "il giro prosegue in live (fallback documentato)"
        _, basis, basis_key = calls[0]
        assert basis == "cassa" and basis_key == "cassa"
