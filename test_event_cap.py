"""test_event_cap.py — CAP MASSIMO PER EVENTO (P1) + soglia morbida CB2 (P5).

Contesto misurato il 17/10/2026: la stessa partita (Norwich-Portsmouth) era nel
sistema con **due `match_id`** diversi (uno `sx-*`, uno the-odds-api) e sono
partiti **due ordini reali** sullo stesso `market_id`/`selection_id`
(4.98 + 2.79 = **7.77 USDC**, il 18,5% dell'equity, contro un cap per-ordine del
12%). Il dedup per `(match_id, esito)` non poteva vederlo: la chiave era
diversa. Il cap per EVENTO usa l'identita' vera della puntata su un exchange
(`market_id` + `selection_id`) e la verifica e' ATOMICA.

Tutti i test sono OFFLINE: ledger SQLite temporaneo, nessuna rete, nessun
provider, nessun ordine reale.
"""
import threading

import pytest

import auto_bet
import tracker


@pytest.fixture()
def db(monkeypatch, tmp_path):
    monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "test.db")
    tracker.init_db()
    tracker.purge_event_claims()
    return tmp_path / "test.db"


def _bet(market_id="0xabc", selection_id=1, stake=2.0, match_id="sx-L1",
         mode="live", final=None, profit=None):
    tracker.save_bet(match_id, "1X2", "1", market_id=market_id,
                     selection_id=selection_id, price=1.8, stake=stake,
                     mode=mode, status="FULLY_FILLED", bet_id="b-" + match_id)
    if final:
        conn = tracker._get_conn()
        conn.execute("UPDATE bets SET esito_finale=?, profit=?, settled_at=? "
                     "WHERE match_id=? AND esito='1'",
                     (final, profit, "2026-10-17T12:00:00+00:00", match_id))
        conn.commit()
        conn.close()


# ---------------------------------------------------------------------------
# 1. Matematica del tetto
# ---------------------------------------------------------------------------

class TestTettoPerEvento:
    def test_default_dodici_per_cento(self):
        assert auto_bet.event_cap_pct() == pytest.approx(0.12)
        assert auto_bet.MAX_STAKE_PER_EVENT_DEFAULT_PCT == 0.12

    def test_cap_sull_equity(self):
        assert auto_bet.event_stake_cap(26.92) == pytest.approx(3.2304, abs=1e-4)
        assert auto_bet.event_stake_cap(41.83) == pytest.approx(5.0196, abs=1e-4)

    def test_equity_ignota_non_da_un_tetto(self):
        assert auto_bet.event_stake_cap(None) == 0.0
        assert auto_bet.event_stake_cap(0) == 0.0
        assert auto_bet.event_stake_cap(-5) == 0.0
        assert auto_bet.event_stake_cap("abc") == 0.0

    def test_percentuale_impossibile_torna_al_default(self, monkeypatch):
        """Una protezione sul denaro NON si spegne con un env sbagliato."""
        monkeypatch.setattr(auto_bet, "MAX_STAKE_PER_EVENT_PCT", 0.0)
        assert auto_bet.event_cap_pct() == pytest.approx(0.12)
        monkeypatch.setattr(auto_bet, "MAX_STAKE_PER_EVENT_PCT", -3)
        assert auto_bet.event_cap_pct() == pytest.approx(0.12)
        monkeypatch.setattr(auto_bet, "MAX_STAKE_PER_EVENT_PCT", 42)
        assert auto_bet.event_cap_pct() == pytest.approx(0.12)

    def test_percentuale_configurabile(self, monkeypatch):
        monkeypatch.setattr(auto_bet, "MAX_STAKE_PER_EVENT_PCT", 0.05)
        assert auto_bet.event_cap_pct() == pytest.approx(0.05)
        assert auto_bet.event_stake_cap(100.0) == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# 2. Il caso reale Norwich-Portsmouth
# ---------------------------------------------------------------------------

class TestNorwichPortsmouth:
    """4.98 + 2.79 USDC sullo stesso esito con due `match_id` diversi."""

    def test_secondo_ordine_bloccato_dal_capo_per_evento(self, db):
        # Equity al momento del PRIMO ordine (4.98 = 12% esatto di 41.50): il
        # cap per-ordine non aveva nulla da ridire, il secondo ordine si'.
        equity = 41.50
        first = auto_bet._event_guard("0x97ab", 1, 4.98, equity,
                                      owner="sx-L20471322")
        assert first["allowed"] is True
        assert first["total"] == pytest.approx(0.0)
        assert first["cap"] == pytest.approx(4.98, abs=1e-4)
        # Secondo ordine sullo STESSO mercato/lato ma con un altro match_id:
        # il cap lo vede (la chiave e' il mercato, non la partita).
        second = auto_bet._event_guard("0x97ab", 1, 2.79, equity,
                                       owner="7a8f8a07")
        assert second["allowed"] is False
        assert second["reason"] == "event_cap"
        assert second["projected"] == pytest.approx(7.77)

    def test_ordine_singolo_oltre_il_tetto_bloccato(self, db):
        """Con equity 26.92 il tetto e' 3.23: un ordine da 4.98 non parte."""
        res = auto_bet._event_guard("0x97ab", 1, 4.98, 26.92)
        assert res["allowed"] is False and res["reason"] == "event_cap"
        assert res["projected"] == pytest.approx(4.98)
        assert res["cap"] == pytest.approx(3.2304, abs=1e-4)

    def test_identita_diversa_non_si_somma(self, db):
        equity = 26.92
        assert auto_bet._event_guard("0xaaa", 1, 2.0, equity)["allowed"]
        # Mercato diverso (altro esito della stessa partita): non e' lo stesso
        # evento-mercato, quindi il totale riparte.
        other = auto_bet._event_guard("0xbbb", 1, 2.0, equity)
        assert other["allowed"] is True

    def test_puntata_gia_a_ledger_conta_anche_con_altro_match_id(self, db):
        """Il ledger e' l'altra meta' della verita': la riga aperta conta."""
        equity = 26.92
        _bet(match_id="sx-L20471322", stake=2.79, final=None)
        res = auto_bet._event_guard("0xabc", 1, 1.0, equity, owner="the-odds-api")
        assert res["allowed"] is False and res["reason"] == "event_cap"
        assert res["committed"] == pytest.approx(2.79)
        assert res["total"] == pytest.approx(2.79)

    def test_riga_saldata_libera_il_capitale(self, db):
        equity = 26.92
        _bet(match_id="sx-L20471322", stake=2.79, final="lost", profit=-2.79)
        res = auto_bet._event_guard("0xabc", 1, 2.0, equity)
        assert res["allowed"] is True
        assert res["committed"] == pytest.approx(0.0)

    def test_puntata_sim_non_conta(self, db):
        equity = 26.92
        _bet(match_id="sim-1", stake=5.0, mode="sim")
        assert auto_bet._event_guard("0xabc", 1, 2.0, equity)["allowed"] is True


# ---------------------------------------------------------------------------
# 3. Identita' incerta = nessun ordine
# ---------------------------------------------------------------------------

class TestIdentitaIncerta:
    def test_senza_market_id_blocca(self, db):
        res = auto_bet._event_guard(None, 1, 2.0, 26.92)
        assert res["allowed"] is False
        assert res["reason"] == "identity_unknown"

    def test_senza_selection_id_blocca(self, db):
        res = auto_bet._event_guard("0xabc", None, 2.0, 26.92)
        assert res["allowed"] is False and res["reason"] == "identity_unknown"

    def test_selection_non_numerica_blocca(self, db):
        res = auto_bet._event_guard("0xabc", "home", 2.0, 26.92)
        assert res["allowed"] is False and res["reason"] == "identity_unknown"

    def test_capitale_ignoto_blocca(self, db):
        res = auto_bet._event_guard("0xabc", 1, 2.0, None)
        assert res["allowed"] is False and res["reason"] == "cap_unknown"

    def test_tetto_zero_blocca(self, db):
        assert tracker.claim_event_stake("0xabc", 1, 2.0, 0.0)["allowed"] is False

    def test_stake_non_valido_blocca(self, db):
        assert auto_bet._event_guard("0xabc", 1, "abc", 26.92)["allowed"] is False
        assert auto_bet._event_guard("0xabc", 1, 0, 26.92)["allowed"] is False


# ---------------------------------------------------------------------------
# 4. Claim: rilascio, scadenza, concorrenza
# ---------------------------------------------------------------------------

class TestClaim:
    def test_rilascio_libera_l_evento(self, db):
        first = auto_bet._event_guard("0xabc", 1, 2.0, 26.92)
        assert first["allowed"] and auto_bet._event_guard(
            "0xabc", 1, 2.0, 26.92)["allowed"] is False
        auto_bet._release_event_claim(first["claim_id"])
        assert auto_bet._event_guard("0xabc", 1, 2.0, 26.92)["allowed"] is True

    def test_claim_scaduto_non_conta(self, db):
        from datetime import datetime, timedelta, timezone
        past = datetime.now(timezone.utc) - timedelta(hours=2)
        res = tracker.claim_event_stake("0xabc", 1, 3.0, 100.0, ttl_s=60,
                                        now=past)
        assert res["allowed"]
        # Con `now` corrente il claim e' scaduto (TTL 60s) e viene purgato.
        assert auto_bet._event_guard("0xabc", 1, 2.0, 26.92)["allowed"] is True

    def test_clear_svuota_i_claim(self, db):
        """`clear_event_claims` e' lo stato pulito degli strumenti di diagnosi.

        Serve a `verify_guardrails.py`: un claim lasciato da uno scenario
        farebbe misurare al successivo il CAP per evento invece del guardrail
        in esame (rosso osservato il 18/10 sullo scenario E).
        """
        assert auto_bet._event_guard("0xabc", 1, 2.0, 26.92)["allowed"]
        assert tracker.event_exposure("0xabc", 1)["pending"] > 0
        assert tracker.clear_event_claims() >= 1
        assert tracker.event_exposure("0xabc", 1)["pending"] == 0
        # Dopo lo svuotamento l'evento e' di nuovo prenotabile.
        assert auto_bet._event_guard("0xabc", 1, 2.0, 26.92)["allowed"]

    def test_claim_id_idempotente_nel_rilascio(self, db):
        res = tracker.claim_event_stake("0xabc", 1, 1.0, 100.0)
        assert tracker.release_event_claim(res["claim_id"]) is True
        assert tracker.release_event_claim(res["claim_id"]) is True
        assert tracker.release_event_claim(None) is False

    def test_concorrenza_due_ordini_simultanei(self, db):
        """La verifica e' atomica: due richieste insieme non sfondano il tetto.

        Il cap e' 3.0 e ogni ordine chiede 2.0: **esattamente uno** puo'
        passare. Senza `BEGIN IMMEDIATE` entrambe leggerebbero 0 e insieme
        arriverebbero a 4.0 (e' il bug del 17/10, a 0,7s di distanza).
        """
        barrier = threading.Barrier(2, timeout=10)
        results = []
        lock = threading.Lock()

        def worker(tag):
            barrier.wait()
            r = tracker.claim_event_stake("0xrace", 1, 2.0, 3.0,
                                          owner=tag, ttl_s=120)
            with lock:
                results.append(r)

        threads = [threading.Thread(target=worker, args=(str(i),))
                   for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        allowed = [r for r in results if r["allowed"]]
        denied = [r for r in results if not r["allowed"]]
        assert len(results) == 2
        assert len(allowed) == 1, results
        assert denied[0]["reason"] == "event_cap"
        assert allowed[0]["projected"] == pytest.approx(2.0)

    def test_concorrenza_quattro_ordini(self, db):
        """Con cap 5 e quattro ordini da 2.0 passano solo due."""
        results = []
        lock = threading.Lock()

        def worker(tag):
            r = tracker.claim_event_stake("0xrace2", 1, 2.0, 5.0, owner=tag)
            with lock:
                results.append(r)

        threads = [threading.Thread(target=worker, args=(str(i),))
                   for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        allowed = [r for r in results if r["allowed"]]
        assert len(allowed) == 2
        total = 2.0 * len(allowed)
        assert total <= 5.0 + 1e-9
        assert max(r["projected"] for r in allowed) <= 5.0 + 1e-9

    def test_lettura_rotta_e_fail_closed(self, db, monkeypatch):
        """Un errore di lettura non e' un'autorizzazione."""
        def _boom():
            raise sqlite_error()
        monkeypatch.setattr(tracker, "_get_conn", _boom)
        res = tracker.event_exposure("0xabc", 1)
        assert res["total"] == float("inf")
        assert res["error"] == "read_error"


def sqlite_error():
    import sqlite3
    return sqlite3.OperationalError("database is locked")


# ---------------------------------------------------------------------------
# 5. RESTING: escrow fuori dal ledger
# ---------------------------------------------------------------------------

class TestRestingNelCap:
    def test_ordine_resting_conta_come_capitale_impegnato(self, db, monkeypatch):
        import resting_orders
        monkeypatch.setattr(resting_orders, "open_orders",
                            lambda: [{"market_id": "0xabc", "selection_id": 1,
                                      "stake": 3.0}])
        res = auto_bet._event_guard("0xabc", 1, 1.0, 26.92)
        assert res["allowed"] is False and res["reason"] == "event_cap"
        assert res["pending"] == pytest.approx(3.0)

    def test_resting_su_altro_mercato_non_conta(self, db, monkeypatch):
        import resting_orders
        monkeypatch.setattr(resting_orders, "open_orders",
                            lambda: [{"market_id": "0xzzz", "selection_id": 1,
                                      "stake": 3.0}])
        assert auto_bet._event_guard("0xabc", 1, 1.0, 26.92)["allowed"] is True

    def test_resting_bloccato_dal_cap_non_piazza(self, db, monkeypatch):
        called = {"n": 0}

        def _place(*a, **k):
            called["n"] += 1
            return {"ok": True, "resting": True}
        monkeypatch.setattr(auto_bet, "_resting_place", _place)
        # Bankroll di processo: `_resting_with_cap` legge `_LAST_BANKROLL`
        # (il giro reale lo imposta prima della FASE 3).
        monkeypatch.setattr(auto_bet, "_LAST_BANKROLL", 26.92)
        assert auto_bet._event_guard("0xabc", 1, 3.0, 26.92)["allowed"] is True
        # Il primo claim (3.0) satura il cap 3.23: il resting viene respinto
        # dal guard PRIMA di arrivare al provider.
        res = auto_bet._resting_with_cap(object(), {"match_id": "sx-L1"},
                                         1.0, 1.8, "0xabc", 1, "book_sottile")
        assert res is None and called["n"] == 0

    def test_claim_rilasciato_dopo_il_parcheggio(self, db, monkeypatch):
        """Dopo il parcheggio conta il registro, non il claim (no doppio)."""
        monkeypatch.setattr(auto_bet, "_resting_place",
                            lambda *a, **k: {"ok": True, "resting": True})
        monkeypatch.setattr(auto_bet, "_LAST_BANKROLL", 26.92)
        open_rows = []

        def _open():
            return open_rows
        import resting_orders
        monkeypatch.setattr(resting_orders, "open_orders", _open)
        res = auto_bet._resting_with_cap(object(), {"match_id": "sx-L1"},
                                         2.0, 1.8, "0xabc", 1, "book_sottile")
        assert res and res["resting"] is True
        # Nessun claim residuo: il registro (qui vuoto perche' finto) ha preso
        # il suo posto, quindi un altro ordine non viene bloccato due volte.
        assert tracker.event_exposure("0xabc", 1)["pending"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 6. Soglia MORBIDA CB2 (P5): non si abbassa il muro, si anticipa il blocco
# ---------------------------------------------------------------------------

class TestSogliaMorbidaCB2:
    def test_default_spenta(self, monkeypatch):
        monkeypatch.setattr(auto_bet, "CB2_SOFT_FLOOR_USDC", 0.0)
        st = auto_bet.cb2_soft_floor_status(10.0)
        assert st["configured"] is False and st["blocked"] is False

    def test_blocca_in_prossimita_del_muro(self, monkeypatch):
        monkeypatch.setattr(auto_bet, "CB2_SOFT_FLOOR_USDC", 28.0)
        st = auto_bet.cb2_soft_floor_status(26.92)
        assert st["blocked"] is True
        assert st["hard"] == auto_bet.T60_KILL_WALLET_USDC
        assert "CB2" in st["reason"]

    def test_sopra_la_soglia_non_blocca(self, monkeypatch):
        monkeypatch.setattr(auto_bet, "CB2_SOFT_FLOOR_USDC", 25.5)
        assert auto_bet.cb2_soft_floor_status(26.92)["blocked"] is False

    def test_equity_illeggibile_blocca(self, monkeypatch):
        monkeypatch.setattr(auto_bet, "CB2_SOFT_FLOOR_USDC", 28.0)
        assert auto_bet.cb2_soft_floor_status(None)["blocked"] is True
        assert auto_bet.cb2_soft_floor_status("abc")["blocked"] is True

    def test_non_tocca_la_soglia_del_cb2(self, monkeypatch):
        """Il muro resta dove e' (env di produzione 25, default di codice 30):
        la guardia e' solo anticipata, non sostitutiva."""
        hard = auto_bet.T60_KILL_WALLET_USDC
        monkeypatch.setattr(auto_bet, "CB2_SOFT_FLOOR_USDC", hard + 2.0)
        st = auto_bet.cb2_soft_floor_status(hard + 1.0)
        assert st["hard"] == hard
        assert auto_bet.T60_KILL_WALLET_USDC == hard
        assert st["blocked"] is True

    def test_soglia_morbida_sopra_il_muro(self, monkeypatch):
        """Coerenza: la soglia morbida sta SOPRA il muro (il CB2 scatterebbe
        prima, quindi una soglia piu' bassa non anticiperebbe nulla)."""
        hard = auto_bet.T60_KILL_WALLET_USDC
        monkeypatch.setattr(auto_bet, "CB2_SOFT_FLOOR_USDC", hard + 2.0)
        st = auto_bet.cb2_soft_floor_status(hard + 1.0)
        assert st["blocked"] is True and st["floor"] > st["hard"]
