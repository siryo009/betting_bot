"""Test della copertura intelligente pre-match (module 4).

Coprono la matematica del lock (peggiore fra i tre esiti, non una stima), le
soglie di eseguibilita' (minimo ordine exchange, cap per gamba), il trigger di
movimento, le guardie (riga gia' presente sul ledger, kill switch, stop-loss,
dry-run, finestra pre-kickoff), la delega dell'esecuzione a `auto_bet._live_fill`
(non reimplementata) e la telemetria fail-safe.

Tutti i test sono OFFLINE: SQLite temporaneo, `fill` e `price_lookup` iniettati,
zero rete, zero ordini reali, nessun credito.
"""
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import smart_hedging as sh
import tracker


@pytest.fixture(autouse=True)
def _isolated_log(tmp_path, monkeypatch):
    monkeypatch.setenv("HEDGE_LOG", str(tmp_path / "hedge_events.jsonl"))
    yield


@pytest.fixture()
def temp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        monkeypatch.setattr(tracker, "DB_PATH", db_path)
        tracker.init_db()
        yield db_path


def _iso(dt):
    return dt.replace(tzinfo=timezone.utc).isoformat()


def _seed_bet(match_id="m1", esito="1", price=1.75, stake=10.0, mode="live",
              *, minutes_ahead=120, home="Casa", away="Ospite",
              league="Serie A"):
    tracker.save_match(match_id, league, home, away,
                       _iso(datetime.now(timezone.utc)
                            + timedelta(minutes=minutes_ahead)))
    tracker.save_bet(match_id, "1X2", esito, price=price, stake=stake, mode=mode,
                     status="FULLY_FILLED", bet_id="orig-1", market_id="mk-orig")


# ---------------------------------------------------------------------------
# 1. Matematica del lock (pura)
# ---------------------------------------------------------------------------

class TestHedgePlan:

    def test_lock_esatto_tre_esiti_uguali(self):
        p = sh.hedge_plan(10, 1.75, {"X": 5.0, "2": 6.0}, const=(1, 5))
        assert p["ok"] is True
        # con f=1 l'incasso e' lo STESSO sui tre esiti (lock, non stima)
        vals = list(p["payouts"].values())
        assert max(vals) - min(vals) < 1e-3
        # incasso = stake x quota d'ingresso
        assert p["payout"] == pytest.approx(17.5, abs=1e-3)

    def test_gambe_proporzionali_agli_inversi(self):
        p = sh.hedge_plan(10, 1.75, {"X": 5.0, "2": 6.0}, const=(1, 5))
        legs = {l["esito"]: l for l in p["legs"]}
        assert legs["X"]["stake"] == pytest.approx(10 * 1.75 / 5.0)
        assert legs["2"]["stake"] == pytest.approx(10 * 1.75 / 6.0)
        assert p["outlay"] == pytest.approx(10 + 3.5 + 2.916667, abs=1e-4)

    def test_profitto_e_roi(self):
        p = sh.hedge_plan(10, 1.75, {"X": 5.0, "2": 6.0}, const=(1, 5))
        assert p["locked_profit"] == pytest.approx(
            p["outlay"] + p["locked_profit"] - p["outlay"])  # coerenza interna
        assert p["locked_profit"] > 0
        assert p["locked_roi"] == pytest.approx(
            p["locked_profit"] / p["outlay"], abs=1e-6)

    def test_payout_coerente_con_outlay(self):
        p = sh.hedge_plan(10, 1.75, {"X": 5.0, "2": 6.0}, const=(1, 5))
        assert p["payout"] == pytest.approx(p["outlay"] + p["locked_profit"],
                                            abs=1e-4)

    def test_sotto_il_minimo_ordine(self):
        # Caso REALE di produzione: stake 1 USDC -> la gamba e' 0.35 USDC, sotto
        # il minimo ordine di SX Bet. Non si inventa la copertura.
        p = sh.hedge_plan(1, 1.75, {"X": 5.0, "2": 6.0}, const=(1, 5))
        assert p["ok"] is False
        assert p["reason"] == sh.REASON_BELOW_MIN

    def test_sopra_il_cap_per_gamba(self):
        p = sh.hedge_plan(100, 1.75, {"X": 5.0, "2": 6.0}, const=(1, 5))
        assert p["reason"] == sh.REASON_ABOVE_CAP

    def test_no_lock_rendimento_peggiore_negativo(self):
        p = sh.hedge_plan(5, 1.75, {"X": 3.0, "2": 3.5}, const=(1, 5))
        assert p["reason"] == sh.REASON_NO_LOCK
        assert p["locked_profit"] < 0

    def test_parziale_non_garantisce_il_profitto(self):
        # Direttiva "contropuntata parziale": con f<1 il peggiore puo' essere
        # negativo e il piano NON e' un lock. Il modulo lo dichiara.
        p = sh.hedge_plan(10, 1.75, {"X": 5.0, "2": 6.0}, const=(1, 5), f=0.5)
        assert p["reason"] == sh.REASON_NO_LOCK
        assert min(p["payouts"].values()) < 0

    def test_soglia_roi_da_env(self, monkeypatch):
        monkeypatch.setenv("HEDGE_MIN_LOCK_PCT", "0.20")
        p = sh.hedge_plan(10, 1.75, {"X": 5.0, "2": 6.0}, const=(1, 5))
        assert p["reason"] == sh.REASON_NO_LOCK
        monkeypatch.setenv("HEDGE_MIN_LOCK_PCT", "0.01")
        assert sh.hedge_plan(10, 1.75, {"X": 5.0, "2": 6.0},
                             const=(1, 5))["ok"] is True

    @pytest.mark.parametrize("stake,odds,comps", [
        (0, 1.75, {"X": 5.0, "2": 6.0}),
        (10, 1.0, {"X": 5.0, "2": 6.0}),
        (10, 1.75, {"X": 5.0}),                 # manca un complementare
        (10, 1.75, {"X": 1.0, "2": 6.0}),       # quota non valida
        (10, 1.75, {}),
        ("x", 1.75, {"X": 5.0, "2": 6.0}),
    ])
    def test_input_non_validi_fail_closed(self, stake, odds, comps):
        p = sh.hedge_plan(stake, odds, comps, const=(1, 5))
        assert p["ok"] is False
        assert p["reason"] == sh.REASON_NO_PRICE

    def test_mai_eccezioni_su_griglia(self):
        for stake in (-1, 0, 1, 10, 1e6):
            for odds in (0.5, 1.0, 1.5, 3.0):
                for comps in ({}, {"X": 2.0, "2": 2.0},
                              {"X": 5.0, "2": 6.0}, {"X": None, "2": 2.0}):
                    p = sh.hedge_plan(stake, odds, comps, const=(1, 5))
                    assert p["ok"] in (True, False)
                    assert p["reason"]

    def test_solo_tre_esiti_1x2(self):
        assert sh.OUTCOMES_1X2 == ("1", "X", "2")


# ---------------------------------------------------------------------------
# 2. Rilevamento (letture iniettate)
# ---------------------------------------------------------------------------

def _bet(esito="1", price=1.75, stake=10.0, **kw):
    return {"match_id": kw.get("match_id", "m1"), "esito": esito,
            "price": price, "stake": stake, "home": "Casa", "away": "Ospite",
            "commence": kw.get("commence"),
            "league": "Serie A", "minutes": kw.get("minutes", 120)}


class TestFindOpportunities:

    def test_movimento_insufficiente(self):
        # Quota della nostra selezione invariata: nessuna anomalia.
        def lookup(home, away, esito, kick):
            return {"1": 1.75, "X": 5.0, "2": 6.0}[esito]
        out = sh.find_opportunities(bets=[_bet()], price_lookup=lookup)
        assert out[0]["reason"] == sh.REASON_MOVE_INSUFFICIENT

    def test_movimento_contro_di_noi(self):
        # La nostra quota si ALLUNGA: coprirsi costa, non blocca nulla.
        def lookup(home, away, esito, kick):
            return {"1": 2.10, "X": 4.0, "2": 5.0}[esito]
        out = sh.find_opportunities(bets=[_bet()], price_lookup=lookup)
        assert out[0]["reason"] == sh.REASON_MOVE_INSUFFICIENT
        assert out[0]["move_pct"] > 0

    def test_ok_con_movimento_drastico(self):
        def lookup(home, away, esito, kick):
            return {"1": 1.45, "X": 5.0, "2": 6.0}[esito]
        out = sh.find_opportunities(bets=[_bet()], price_lookup=lookup)
        assert out[0]["ok"] is True
        assert out[0]["move_pct"] < 0
        assert out[0]["reason"] == sh.REASON_OK

    def test_prezzo_mancante(self):
        def lookup(home, away, esito, kick):
            return None if esito == "2" else {"1": 1.45, "X": 5.0}[esito]
        out = sh.find_opportunities(bets=[_bet()], price_lookup=lookup)
        assert out[0]["reason"] == sh.REASON_NO_PRICE

    def test_esito_non_canonico(self):
        out = sh.find_opportunities(bets=[_bet(esito="Casa")],
                                    price_lookup=lambda *a: 2.0)
        assert out[0]["reason"] == sh.REASON_NOT_1X2

    def test_soglia_movimento_da_env(self, monkeypatch):
        def lookup(home, away, esito, kick):
            return {"1": 1.70, "X": 5.0, "2": 6.0}[esito]
        out = sh.find_opportunities(bets=[_bet()], price_lookup=lookup)
        assert out[0]["reason"] == sh.REASON_MOVE_INSUFFICIENT  # -2.9%
        monkeypatch.setenv("HEDGE_MIN_MOVE_PCT", "0.02")
        out = sh.find_opportunities(bets=[_bet()], price_lookup=lookup)
        assert out[0]["ok"] is True

    def test_lookup_che_esplode_non_propaga(self):
        def lookup(*a):
            raise RuntimeError("rete giu'")
        out = sh.find_opportunities(bets=[_bet()], price_lookup=lookup)
        assert out[0]["reason"] == sh.REASON_NO_PRICE

    def test_finestra_dal_ledger(self, temp_db):
        _seed_bet("m-vicino", minutes_ahead=2)     # sotto HEDGE_MIN_MINUTES
        _seed_bet("m-lontano", minutes_ahead=48 * 60)
        _seed_bet("m-buono", minutes_ahead=120)
        rows = sh._open_live_bets()
        ids = {r["match_id"] for r in rows}
        assert ids == {"m-buono"}, ids

    def test_solo_puntate_live(self, temp_db):
        _seed_bet("m-sim", mode="sim")
        assert sh._open_live_bets() == []

    def test_posizione_gia_coperta_non_ritorna(self, temp_db):
        # Guardia in DETECTION (non solo in esecuzione): se sul ledger esiste
        # gia' una gamba hedge aperta sul complementare, la posizione non deve
        # essere proposta di nuovo (il job gira ogni 15').
        _seed_bet("m-orig", esito="1")
        tracker.save_bet("m-orig", "1X2", "2", price=6.0, stake=2.9167,
                         mode="live", status="FULLY_FILLED", bet_id="h-2",
                         market_id="mk-h")
        rows = [r for r in sh._open_live_bets() if r["match_id"] == "m-orig"]
        out = sh.find_opportunities(bets=rows, price_lookup=lambda *a: 2.0)
        assert out[0]["reason"] == sh.REASON_ALREADY_OPEN

    def test_hedge_dell_hedge_non_esiste(self, temp_db):
        # Una GAMBA hedge (bet live aperta su esito X) non deve a sua volta
        # essere valutata come posizione da coprire: e' la copertura, non la
        # posizione. Senza questa guardia il ciclo coprirebbe le coperture.
        _seed_bet("m-leg", esito="X", price=5.0)
        rows = sh._open_live_bets()
        out = sh.find_opportunities(bets=rows, price_lookup=lambda *a: 2.0)
        assert out[0]["reason"] in (sh.REASON_ALREADY_OPEN,
                                    sh.REASON_NOT_1X2) or \
            out[0]["ok"] is False

    def test_complemento(self):
        assert sh.complement_of("1") == "2"
        assert sh.complement_of("2") == "1"


# ---------------------------------------------------------------------------
# 3. Esecuzione (delega a `_live_fill`)
# ---------------------------------------------------------------------------

class TestPlaceHedge:

    def _prop(self):
        return {"match_id": "m1", "home": "Casa", "away": "Ospite",
                "commence": _iso(datetime.now(timezone.utc)
                                 + timedelta(minutes=120)),
                "esito": "1", "legs": [
                    {"esito": "X", "odds": 5.0, "stake": 3.5},
                    {"esito": "2", "odds": 6.0, "stake": 2.916667}]}

    def test_scrive_le_gambe_sul_ledger(self, temp_db):
        calls = []

        def fill(pick, stake, odds):
            calls.append((pick["esito_key"], stake, odds))
            return {"ok": True, "stake": stake, "price": odds,
                    "market_id": "mk-" + pick["esito_key"], "selection_id": 1,
                    "bet_id": "b-" + pick["esito_key"],
                    "status": "FULLY_FILLED"}

        res = sh.place_hedge(self._prop(), fill=fill)
        assert [r["ok"] for r in res] == [True, True]
        assert len(calls) == 2
        bets = {(b["match_id"], b["esito"]): b for b in tracker.get_bets()}
        assert ("m1", "X") in bets and ("m1", "2") in bets
        assert bets[("m1", "X")]["mode"] == "live"
        assert bets[("m1", "X")]["bet_id"] == "b-X"
        assert bets[("m1", "X")]["market_id"] == "mk-X"

    def test_guardia_su_riga_gia_presente(self, temp_db):
        # Una riga CHIUSA (o aperta) sul complementare impedisce l'ordine: su
        # una riga chiusa `save_bet` non scriverebbe piu' -> ordine reale non
        # registrato. Fail-closed.
        tracker.save_match("m1", "Serie A", "Casa", "Ospite",
                           _iso(datetime.now(timezone.utc)))
        tracker.save_bet("m1", "1X2", "X", price=4.0, stake=1.0, mode="sim",
                         status="WON")
        called = []

        def fill(pick, stake, odds):
            called.append(pick["esito_key"])
            return {"ok": True, "stake": stake, "price": odds}

        res = sh.place_hedge(self._prop(), fill=fill)
        by = {r["esito"]: r for r in res}
        assert by["X"]["ok"] is False
        assert by["X"]["reason"] == sh.REASON_ALREADY_OPEN
        assert by["2"]["ok"] is True
        assert "X" not in called

    def test_fill_fallito(self, temp_db):
        res = sh.place_hedge(self._prop(), fill=lambda *a: None)
        assert all(r["ok"] is False for r in res)
        assert all(r["reason"] == sh.REASON_FILL_FAILED for r in res)

    def test_fill_che_esplode(self, temp_db):
        def fill(*a):
            raise RuntimeError("exchange giu'")
        res = sh.place_hedge(self._prop(), fill=fill)
        assert all(r["ok"] is False for r in res)

    def test_idempotenza_secondo_giro(self, temp_db):
        def fill(pick, stake, odds):
            return {"ok": True, "stake": stake, "price": odds,
                    "bet_id": "b-" + pick["esito_key"]}
        sh.place_hedge(self._prop(), fill=fill)
        res2 = sh.place_hedge(self._prop(), fill=fill)
        assert all(r["reason"] == sh.REASON_ALREADY_OPEN for r in res2)


# ---------------------------------------------------------------------------
# 4. Ciclo completo
# ---------------------------------------------------------------------------

class TestRunCycle:

    @pytest.fixture(autouse=True)
    def _free(self, monkeypatch):
        monkeypatch.setattr(sh, "_blocked_reason", lambda: None)

    def _lookup(self):
        def lookup(home, away, esito, kick):
            return {"1": 1.45, "X": 5.0, "2": 6.0}[esito]
        return lookup

    def test_disabilitato_non_fa_nulla(self, monkeypatch):
        monkeypatch.setenv("SMART_HEDGING", "0")
        res = sh.run_hedge_cycle(bets=[_bet()], price_lookup=self._lookup())
        assert res["enabled"] is False
        assert res["evaluated"] == 0

    def test_opportunita_non_eseguibile_viene_registrata(self, monkeypatch):
        res = sh.run_hedge_cycle(bets=[_bet(stake=1.0)],
                                 price_lookup=self._lookup())
        assert res["opportunities"] == 0
        assert any(s["reason"] == sh.REASON_BELOW_MIN
                   for s in res["skipped"])

    def test_dry_run_non_ordina(self, monkeypatch):
        res = sh.run_hedge_cycle(bets=[_bet()], price_lookup=self._lookup(),
                                 dry_run=True)
        assert res["opportunities"] == 1
        assert res["placed"] == []
        assert any(s["reason"] == sh.REASON_DRY_RUN for s in res["skipped"])

    def test_piazza_registra_e_logga(self, temp_db, monkeypatch):
        # Il giro completo deve: valutare, registrare sul ledger e scrivere il
        # JSONL con il profitto bloccato.
        tracker.save_match("m1", "Serie A", "Casa", "Ospite",
                           _iso(datetime.now(timezone.utc)
                                + timedelta(minutes=120)))
        tracker.save_bet("m1", "1X2", "1", price=1.75, stake=10.0, mode="live",
                         status="FULLY_FILLED", bet_id="orig")

        def fill(pick, stake, odds):
            return {"ok": True, "stake": stake, "price": odds,
                    "market_id": "mk", "selection_id": 1,
                    "bet_id": "h-" + pick["esito_key"],
                    "status": "FULLY_FILLED"}

        res = sh.run_hedge_cycle(bets=[_bet()], price_lookup=self._lookup(),
                                 fill=fill, dry_run=False)
        assert len(res["placed"]) == 1
        assert res["placed"][0]["locked_profit"] > 0
        bets = {(b["match_id"], b["esito"]) for b in tracker.get_bets()}
        assert ("m1", "X") in bets and ("m1", "2") in bets
        evts = sh.iter_events()
        assert any(e["kind"] == "placed" for e in evts)
        assert any(e["kind"] == "opportunity" for e in evts)

    def test_secondo_giro_non_riordina(self, temp_db):
        tracker.save_match("m1", "Serie A", "Casa", "Ospite",
                           _iso(datetime.now(timezone.utc)
                                + timedelta(minutes=120)))
        tracker.save_bet("m1", "1X2", "1", price=1.75, stake=10.0, mode="live",
                         status="FULLY_FILLED", bet_id="orig")
        fill = lambda pick, stake, odds: {"ok": True, "stake": stake,
                                          "price": odds, "bet_id": "h"}
        sh.run_hedge_cycle(bets=[_bet()], price_lookup=self._lookup(),
                           fill=fill, dry_run=False)
        res2 = sh.run_hedge_cycle(bets=[_bet()], price_lookup=self._lookup(),
                                  fill=fill, dry_run=False)
        assert res2["placed"] == []

    def test_bloccato_da_kill_switch(self, monkeypatch):
        monkeypatch.setattr(sh, "_blocked_reason", lambda: "kill_switch=off")
        res = sh.run_hedge_cycle(bets=[_bet()], price_lookup=self._lookup(),
                                 fill=lambda *a: {"ok": True}, dry_run=False)
        assert res["blocked"] == "kill_switch=off"
        assert res["placed"] == []
        assert any(s["reason"] == sh.REASON_BLOCKED for s in res["skipped"])

    def test_fail_safe_su_input_ostile(self):
        res = sh.run_hedge_cycle(bets=["non-un-dict"], price_lookup="x")
        assert res["error"] is not None


# ---------------------------------------------------------------------------
# 4b. Autorita' di sicurezza (sonde REALI, nessun patch del ciclo)
# ---------------------------------------------------------------------------

class TestBlocchi:

    def test_blocchi_reali_letti_dai_file_di_stato(self, temp_db, monkeypatch):
        import auto_bet
        monkeypatch.setattr(auto_bet, "kill_switch_status",
                            lambda: {"effective": "off"})
        assert sh._blocked_reason() == "kill_switch=off"
        monkeypatch.setattr(auto_bet, "kill_switch_status",
                            lambda: {"effective": "live"})
        monkeypatch.setattr(auto_bet, "daily_stop_status",
                            lambda: {"stopped": True})
        assert sh._blocked_reason() == "daily_stop"
        monkeypatch.setattr(auto_bet, "daily_stop_status",
                            lambda: {"stopped": False})
        monkeypatch.setattr(auto_bet, "weekly_stop_status",
                            lambda: {"stopped": True})
        assert sh._blocked_reason() == "weekly_stop"
        monkeypatch.setattr(auto_bet, "weekly_stop_status",
                            lambda: {"stopped": False})
        assert sh._blocked_reason() is None

    def test_stato_non_leggibile_e_fail_closed(self, monkeypatch):
        import sys
        monkeypatch.setitem(sys.modules, "auto_bet", None)
        assert sh._blocked_reason() == "stato_non_leggibile"


# ---------------------------------------------------------------------------
# 5. Telemetria e report
# ---------------------------------------------------------------------------

class TestTelemetria:

    def test_record_iter_summary(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HEDGE_LOG", str(tmp_path / "h.jsonl"))
        sh.record_event("placed", sh.REASON_OK, match_id="m1",
                        locked_profit=1.5, locked_roi=0.06, home="Casa",
                        away="Ospite")
        sh.record_event("skip", sh.REASON_BELOW_MIN, match_id="m2")
        assert len(sh.iter_events()) == 2
        res = sh.summary(days=1)
        assert res["events"] == 2
        assert res["placed"] == 1
        assert res["locked_profit"] == pytest.approx(1.5)
        assert res["by_reason"][sh.REASON_BELOW_MIN] == 1

    def test_filtro_giorni(self, tmp_path, monkeypatch):
        p = tmp_path / "h.jsonl"
        monkeypatch.setenv("HEDGE_LOG", str(p))
        old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        p.write_text(json.dumps({"ts": old, "kind": "placed", "reason": "ok"})
                     + "\n", encoding="utf-8")
        assert sh.iter_events(days=7) == []
        assert len(sh.iter_events(days=60)) == 1

    def test_righe_corrotte_ignorate(self, tmp_path, monkeypatch):
        p = tmp_path / "h.jsonl"
        monkeypatch.setenv("HEDGE_LOG", str(p))
        p.write_text("{non json}\n\n[1,2]\n" + json.dumps(
            {"ts": datetime.now(timezone.utc).isoformat(), "kind": "x",
             "reason": "ok"}) + "\n", encoding="utf-8")
        evts = sh.iter_events()
        assert len(evts) == 1 and evts[0]["kind"] == "x"

    def test_log_non_scrivibile_fail_safe(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HEDGE_LOG", str(tmp_path))   # una DIRECTORY
        evt = sh.record_event("skip", sh.REASON_NO_LOCK)
        assert evt.get("error")
        assert sh.iter_events() == []

    def test_log_inesistente(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HEDGE_LOG", str(tmp_path / "assente.jsonl"))
        assert sh.iter_events() == []
        assert sh.summary()["events"] == 0

    def test_format_alert(self):
        txt = sh.format_alert({"home": "Casa", "away": "Ospite", "esito": "1",
                               "league": "Serie A", "locked_profit": 1.5,
                               "locked_roi": 0.066,
                               "legs": [{"esito": "X", "odds": 5.0,
                                         "stake": 3.5, "ok": True}]})
        assert "HEDGE" in txt and "Casa" in txt and "Serie A" in txt

    def test_format_report(self):
        txt = sh.format_report({"days": 7, "placed": 2, "locked_profit": 3.2,
                                "by_reason": {sh.REASON_BELOW_MIN: 4},
                                "last": {"home": "Casa", "away": "Ospite",
                                         "locked_roi": 0.06}})
        assert "Copertura intelligente" in txt
        assert "profitto bloccato +3.20" in txt


# ---------------------------------------------------------------------------
# 6. Tripwire: nessuna reimplementazione, default e IaC
# ---------------------------------------------------------------------------

class TestTripwire:

    def test_default_on_live_con_soglia(self, monkeypatch):
        monkeypatch.delenv("SMART_HEDGING", raising=False)
        assert sh.enabled() is True
        assert sh.min_move_pct() == pytest.approx(0.05)
        assert sh.min_lock_pct() == pytest.approx(0.01)
        assert sh.min_stake_usdc() == pytest.approx(1.0)

    def test_non_reimplementa_l_esecuzione(self):
        src = Path("smart_hedging.py").read_text(encoding="utf-8")
        # L'ordine reale passa SEMPRE da auto_bet._live_fill: qui non si
        # costruisce nessun ordine a mano (niente firma, niente POST ordini).
        for forbidden in ("place_limit_order", "OrderResult", "signature",
                          "eip712", "orders-v3"):
            assert forbidden not in src, forbidden
        assert "auto_bet._live_fill" in src

    def test_nessun_import_di_rete_a_livello_modulo(self):
        src = Path("smart_hedging.py").read_text(encoding="utf-8")
        head = src.split("def _sx_price", 1)[0]
        assert "import execution_engine" not in head
        assert "import requests" not in head

    def test_import_del_modulo_non_carica_la_produzione(self):
        import subprocess, sys
        code = ("import sys, smart_hedging; "
                "assert 'auto_bet' not in sys.modules; "
                "assert 'tracker' not in sys.modules; "
                "assert 'execution_engine' not in sys.modules")
        r = subprocess.run([sys.executable, "-c", code],
                           capture_output=True, text=True, cwd=".")
        assert r.returncode == 0, r.stderr

    def test_solo_1x2_negli_ordini(self):
        src = Path("smart_hedging.py").read_text(encoding="utf-8")
        assert '"mercato": "1X2"' in src
        assert "not_1x2" in src

    def test_env_dichiarate_nella_iac(self):
        src = Path(".railway/railway.ts").read_text(encoding="utf-8")
        for env in ("SMART_HEDGING", "HEDGE_MIN_MOVE_PCT",
                    "HEDGE_MIN_LOCK_PCT", "HEDGE_FRACTION",
                    "HEDGE_MIN_STAKE_USDC", "HEDGE_MAX_STAKE_USDC",
                    "HEDGE_MIN_MINUTES", "HEDGE_HORIZON_H", "HEDGE_LOG"):
            assert f"{env}: preserve()" in src, env

    def test_log_isolato_nel_conftest(self):
        src = Path("conftest.py").read_text(encoding="utf-8")
        assert "HEDGE_LOG" in src
