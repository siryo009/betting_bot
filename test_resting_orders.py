"""Test degli ordini RESTING (GTC) su SX Bet (10/10/2026).

TUTTI OFFLINE: provider finto in memoria, registro nella tmp (via
`conftest.py`), nessuna rete, nessuna credenziale, **zero ordini reali**.

Il filo conduttore e' il principio del modulo: un ordine parcheggiato NON e'
una puntata. Nessuna riga `bets`, nessun `placed`, finche' la riconciliazione
non ha la prova che il book l'ha riempito.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import auto_bet
import resting_orders as ro
import tracker


# ---------------------------------------------------------------------------
# Attrezzi
# ---------------------------------------------------------------------------

def _iso(dt) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _kickoff(hours: float = 3.0) -> str:
    return _iso(datetime.now(timezone.utc) + timedelta(hours=hours))


class _Order:
    """Sostituto minimale di `execution_engine.OrderResult`."""

    def __init__(self, ok=True, bet_id="0xabc", status="SUBMITTED",
                 matched=0.0, price=None, error=None):
        self.ok = ok
        self.bet_id = bet_id
        self.status = status
        self.size_matched = matched
        self.price_matched = price
        self.error = error


class _Prov:
    """Provider di exchange finto: registra OGNI chiamata."""

    name = "sxbet"

    def __init__(self, *, order=None, best=1.70, book_depth=4.0, live=None,
                 cancel_ok=True, accepts_expiry=True):
        self.order = order
        self.best = best
        self.book_depth = book_depth
        self.live = live                      # None = NON leggibile
        self.cancel_ok = cancel_ok
        self.accepts_expiry = accepts_expiry
        self.place_calls = []
        self.cancel_calls = []
        self.open_calls = 0

    def best_back_price(self, market_id, selection_id):
        return self.best

    def get_market_book(self, market_id):
        return {"runners": [{"selectionId": 1,
                             "availableToBack": [{"price": 1.70,
                                                  "size": self.book_depth}]}]}

    def place_limit_order(self, market_id, selection_id, side, price, size,
                          persistence="LAPSE", expiry_seconds=None):
        if expiry_seconds is not None and not self.accepts_expiry:
            raise TypeError("place_limit_order() got an unexpected keyword "
                            "argument 'expiry_seconds'")
        self.place_calls.append({"market_id": market_id,
                                 "selection_id": selection_id, "side": side,
                                 "price": price, "size": size,
                                 "persistence": persistence,
                                 "expiry_seconds": expiry_seconds})
        return self.order

    def list_open_orders(self):
        self.open_calls += 1
        return self.live

    def cancel_order(self, market_id, bet_id):
        self.cancel_calls.append((market_id, bet_id))
        return self.cancel_ok


def _pick(**kw) -> dict:
    base = {"match_id": "sx-L1", "home": "Osasuna", "away": "Getafe",
            "esito_key": "1", "mercato": "TENNIS", "market_id": "0xmid",
            "selection_id": 1, "team": "Osasuna", "commence": _kickoff()}
    base.update(kw)
    return base


@pytest.fixture(autouse=True)
def _resting_on(monkeypatch):
    """Questo file e' l'eccezione dichiarata: il modulo e' ACCESO e il registro
    e' quello temporaneo del conftest."""
    monkeypatch.setenv("RESTING_ORDERS", "1")
    monkeypatch.setattr(auto_bet, "DRY_RUN", False)
    yield


@pytest.fixture()
def temp_db(monkeypatch, tmp_path):
    """Ledger temporaneo: il capitale del recinto si legge da qui, mai dal
    volume di produzione."""
    monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "test.db")
    tracker.init_db()
    yield tmp_path / "test.db"


def _seed_open(**kw) -> dict:
    """Scrive una riga ST_OPEN nel registro temporaneo (senza provider)."""
    entry = {"order_id": "0xabc", "status": ro.ST_OPEN,
             "placed_at": datetime.now(timezone.utc).isoformat(),
             "deadline": _iso(datetime.now(timezone.utc) + timedelta(hours=1)),
             "expires_at": _iso(datetime.now(timezone.utc) + timedelta(hours=2)),
             "match_id": "sx-L1", "esito": "1", "mercato": "TENNIS",
             "market_id": "0xmid", "selection_id": 1, "stake": 2.0,
             "price": 1.70, "home": "Osasuna", "away": "Getafe",
             "kickoff": _kickoff(), "sx_status": "SUBMITTED",
             "cancel_attempted": False, "cancel_ok": False}
    entry.update(kw)
    state = ro.load()
    state["orders"] = [r for r in (state.get("orders") or [])
                       if isinstance(r, dict)] + [entry]
    assert ro.save(state) is True
    return entry


# ---------------------------------------------------------------------------
# 1. Config e interruttore
# ---------------------------------------------------------------------------

class TestConfig:
    def test_default_acceso(self, monkeypatch):
        monkeypatch.delenv("RESTING_ORDERS", raising=False)
        assert ro.enabled() is True

    @pytest.mark.parametrize("val", ["0", "false", "no", "off", "OFF", " 0 "])
    def test_valori_di_spegnimento(self, monkeypatch, val):
        monkeypatch.setenv("RESTING_ORDERS", val)
        assert ro.enabled() is False

    def test_default_dei_parametri(self, monkeypatch):
        for name in ("RESTING_MAX_OPEN", "RESTING_TTL_MIN",
                     "RESTING_CANCEL_BEFORE_MIN", "RESTING_EXPIRY_MARGIN_S"):
            monkeypatch.delenv(name, raising=False)
        cfg = ro.config()
        assert cfg["max_open"] == 5
        assert cfg["ttl_min"] == 720.0
        assert cfg["cancel_before_min"] == 2.0
        assert cfg["expiry_margin_s"] == 600.0

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("RESTING_MAX_OPEN", "2")
        monkeypatch.setenv("RESTING_TTL_MIN", "90")
        monkeypatch.setenv("RESTING_CANCEL_BEFORE_MIN", "5")
        monkeypatch.setenv("RESTING_EXPIRY_MARGIN_S", "120")
        cfg = ro.config()
        assert (cfg["max_open"], cfg["ttl_min"], cfg["cancel_before_min"],
                cfg["expiry_margin_s"]) == (2, 90.0, 5.0, 120.0)

    def test_valori_impossibili_non_spegnono_le_guardie(self, monkeypatch):
        """Un env sbagliato ricade sul default (guardia di sicurezza)."""
        monkeypatch.setenv("RESTING_MAX_OPEN", "abc")
        monkeypatch.setenv("RESTING_TTL_MIN", "0")
        monkeypatch.setenv("RESTING_EXPIRY_MARGIN_S", "-100")
        cfg = ro.config()
        assert cfg["max_open"] == 5          # non numerico -> default
        assert cfg["ttl_min"] == 1.0         # clamp al minimo
        assert cfg["expiry_margin_s"] == 60.0  # clamp al minimo

    def test_min_ticket_delega_al_motore(self):
        """Il ticket e' quello del MOTORE Kelly: una sola definizione."""
        from decision.stake_engine import aggressive_config
        assert ro.min_ticket() == float(aggressive_config()["min_ticket"])

    def test_min_ticket_fail_open(self, monkeypatch):
        import builtins
        real = builtins.__import__

        def _boom(name, *a, **k):
            if name == "decision.stake_engine":
                raise ImportError("no")
            return real(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", _boom)
        assert ro.min_ticket() == 1.0


# ---------------------------------------------------------------------------
# 2. Registro su volume
# ---------------------------------------------------------------------------

class TestRegistro:
    def test_file_assente_struttura_vuota(self):
        state = ro.load()
        assert state["orders"] == []
        assert "error" not in state

    def test_file_corrotto_dichiarato_e_non_sovrascritto(self):
        path = Path(ro.state_path())
        path.write_text("{non-json", encoding="utf-8")
        state = ro.load()
        assert state["orders"] == []
        assert state["error"]
        # Il file NON deve essere azzerato da una lettura.
        assert path.read_text(encoding="utf-8") == "{non-json"

    def test_formato_inatteso_dichiarato(self):
        Path(ro.state_path()).write_text('{"orders": "nope"}', encoding="utf-8")
        state = ro.load()
        assert state["orders"] == []
        assert state["error"] == "formato inatteso"

    def test_scrittura_atomica_e_updated_at(self):
        assert ro.save({"orders": [{"order_id": "x"}]}) is True
        raw = json.loads(Path(ro.state_path()).read_text(encoding="utf-8"))
        assert raw["orders"][0]["order_id"] == "x"
        assert raw["updated_at"]

    def test_open_orders_e_stake(self):
        _seed_open(order_id="a", stake=2.5)
        _seed_open(order_id="b", stake=1.5, status=ro.ST_FILLED,
                   match_id="sx-L2", esito="2")
        assert [o["order_id"] for o in ro.open_orders()] == ["a"]
        assert ro.open_stake() == 2.5

    def test_stake_ignora_valori_non_numerici(self):
        _seed_open(stake="non-un-numero")
        assert ro.open_stake() == 0.0

    def test_timestamp_difensivo(self):
        assert ro._ts("2026-10-10T12:00:00Z") is not None
        assert ro._ts("2026-10-10T12:00:00+02:00") is not None
        assert ro._ts("2026-10-10T12:00:00") is not None     # naive -> UTC
        assert ro._ts(None) is None
        assert ro._ts("non una data") is None


# ---------------------------------------------------------------------------
# 3. Deadline
# ---------------------------------------------------------------------------

class TestDeadline:
    def test_min_fra_ttl_e_kickoff(self):
        cfg = {"ttl_min": 720.0, "cancel_before_min": 2.0}
        now = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
        # kickoff fra 1h -> vince il kickoff (-2')
        d = ro.deadline_ts(_iso(now + timedelta(hours=1)), now=now, cfg=cfg)
        assert d == pytest.approx((now + timedelta(minutes=58)).timestamp())

    def test_kickoff_lontano_vince_il_ttl(self):
        cfg = {"ttl_min": 60.0, "cancel_before_min": 2.0}
        now = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
        d = ro.deadline_ts(_iso(now + timedelta(days=2)), now=now, cfg=cfg)
        assert d == pytest.approx((now + timedelta(hours=1)).timestamp())

    def test_kickoff_illeggibile_usa_il_solo_ttl(self):
        cfg = {"ttl_min": 60.0, "cancel_before_min": 2.0}
        now = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
        d = ro.deadline_ts("non una data", now=now, cfg=cfg)
        assert d == pytest.approx((now + timedelta(hours=1)).timestamp())


# ---------------------------------------------------------------------------
# 4. Piazzamento
# ---------------------------------------------------------------------------

class TestPlace:
    def test_modulo_spento(self, monkeypatch):
        monkeypatch.setenv("RESTING_ORDERS", "0")
        prov = _Prov(order=_Order())
        res = ro.place(prov, pick=_pick(), stake=2.0, price=1.70,
                       market_id="0xmid", selection_id=1)
        assert res == {"placed": False, "filled": False, "reason": "disabled"}
        assert prov.place_calls == []

    def test_sotto_il_ticket_del_motore(self, monkeypatch):
        monkeypatch.setattr(ro, "min_ticket", lambda: 1.0)
        prov = _Prov(order=_Order())
        res = ro.place(prov, pick=_pick(), stake=0.5, price=1.70,
                       market_id="0xmid", selection_id=1)
        assert res["reason"] == "below_min_ticket"
        assert prov.place_calls == []

    def test_registro_illeggibile_non_si_aggiunge(self):
        Path(ro.state_path()).write_text("{rotto", encoding="utf-8")
        prov = _Prov(order=_Order())
        res = ro.place(prov, pick=_pick(), stake=2.0, price=1.70,
                       market_id="0xmid", selection_id=1)
        assert res["reason"] == "state_unreadable"
        assert prov.place_calls == []

    def test_max_open(self, monkeypatch):
        monkeypatch.setenv("RESTING_MAX_OPEN", "1")
        _seed_open(order_id="a")
        prov = _Prov(order=_Order())
        res = ro.place(prov, pick=_pick(), stake=2.0, price=1.70,
                       market_id="0xmid", selection_id=1)
        assert res["reason"] == "max_open"
        assert prov.place_calls == []

    def test_duplicato_stessa_partita_ed_esito(self):
        _seed_open(order_id="a", match_id="sx-L1", esito="1")
        prov = _Prov(order=_Order())
        res = ro.place(prov, pick=_pick(), stake=2.0, price=1.70,
                       market_id="0xmid", selection_id=1)
        assert res["reason"] == "duplicate"
        assert prov.place_calls == []

    def test_deadline_troppo_vicina(self):
        prov = _Prov(order=_Order())
        res = ro.place(prov, pick=_pick(commence=_kickoff(0.005)), stake=2.0,
                       price=1.70, market_id="0xmid", selection_id=1)
        assert res["reason"] == "deadline_too_close"
        assert prov.place_calls == []

    def test_provider_che_solleva(self):
        class _Boom(_Prov):
            def place_limit_order(self, *a, **k):
                raise RuntimeError("rete giu'")

        res = ro.place(_Boom(), pick=_pick(), stake=2.0, price=1.70,
                       market_id="0xmid", selection_id=1)
        assert res["reason"] == "error:RuntimeError"

    def test_status_failed(self):
        prov = _Prov(order=_Order(ok=False, bet_id="0x1", status="FAILED",
                                  error="rifiutato"))
        res = ro.place(prov, pick=_pick(), stake=2.0, price=1.70,
                       market_id="0xmid", selection_id=1)
        assert res["reason"] == "failed" and res["error"] == "rifiutato"
        assert ro.open_orders() == []

    def test_senza_order_id_non_e_piazzato(self):
        """GTC senza orderId: l'ordine non e' dimostrabile -> nessuna riga."""
        prov = _Prov(order=_Order(bet_id=None))
        res = ro.place(prov, pick=_pick(), stake=2.0, price=1.70,
                       market_id="0xmid", selection_id=1)
        assert res["reason"] == "no_order_id"
        assert ro.open_orders() == []

    def test_riempito_all_istante_non_entra_nel_registro(self):
        """Un resting attraversabile e' una puntata NORMALE: la registra il
        percorso taker, non il registro resting (nessun doppione)."""
        prov = _Prov(order=_Order(status="FULLY_FILLED", matched=2.0,
                                  price=1.68))
        res = ro.place(prov, pick=_pick(), stake=2.0, price=1.70,
                       market_id="0xmid", selection_id=1)
        assert res["filled"] is True
        assert res["reason"] == "filled_immediately"
        assert res["stake"] == 2.0 and res["price"] == 1.68
        assert ro.load()["orders"] == []
        # La chiamata e' comunque un ordine RESTING (GTC).
        assert prov.place_calls[0]["persistence"] == "PERSIST"

    def test_piazzamento_riuscito(self):
        prov = _Prov(order=_Order(status="SUBMITTED"))
        res = ro.place(prov, pick=_pick(), stake=2.0, price=1.70,
                       market_id="0xmid", selection_id=1)
        assert res["placed"] is True and res["reason"] == "resting"
        call = prov.place_calls[0]
        assert (call["market_id"], call["selection_id"], call["side"]) == \
            ("0xmid", 1, "BACK")
        assert call["persistence"] == "PERSIST"
        # Scadenza PER-ORDINE: piu' larga della deadline (margine), cosi' un
        # ordine non puo' morire da solo mentre lo guardiamo.
        assert call["expiry_seconds"] > 0
        rows = ro.open_orders()
        assert len(rows) == 1
        assert rows[0]["order_id"] == "0xabc"
        assert rows[0]["deadline"] < rows[0]["expires_at"]
        assert rows[0]["stake"] == 2.0 and rows[0]["price"] == 1.70
        assert rows[0]["cancel_attempted"] is False

    def test_provider_vecchio_senza_expiry_per_ordine(self):
        """Fallback dichiarato: senza `expiry_seconds` si usa la costante."""
        prov = _Prov(order=_Order(), accepts_expiry=False)
        res = ro.place(prov, pick=_pick(), stake=2.0, price=1.70,
                       market_id="0xmid", selection_id=1)
        assert res["placed"] is True
        assert prov.place_calls[0]["expiry_seconds"] is None

    def test_registro_non_scrivibile_cancella_subito(self, monkeypatch):
        """Se l'ordine e' sul book senza tracciabilita': fail-closed."""
        monkeypatch.setattr(ro, "save", lambda state: False)
        prov = _Prov(order=_Order())
        res = ro.place(prov, pick=_pick(), stake=2.0, price=1.70,
                       market_id="0xmid", selection_id=1)
        assert res["placed"] is False
        assert res["reason"] == "state_unwritable_cancelled"
        assert prov.cancel_calls == [("0xmid", "0xabc")]

    def test_cancellazione_immediata_fallita_dichiarata(self, monkeypatch):
        monkeypatch.setattr(ro, "save", lambda state: False)
        prov = _Prov(order=_Order(), cancel_ok=False)
        res = ro.place(prov, pick=_pick(), stake=2.0, price=1.70,
                       market_id="0xmid", selection_id=1)
        assert res["reason"] == "state_unwritable"
        assert res["order_id"] == "0xabc"


# ---------------------------------------------------------------------------
# 5. Riconciliazione
# ---------------------------------------------------------------------------

class TestReconcile:
    def test_nessun_ordine_aperto(self):
        res = ro.reconcile(_Prov(live=[]))
        assert res["checked"] == 0 and res["filled"] == 0

    def test_book_non_leggibile_fail_closed(self):
        _seed_open()
        res = ro.reconcile(_Prov(live=None))
        assert res["unavailable"] is True
        assert res["filled"] == 0
        # La riga resta aperta: nessuna inferenza su denaro reale.
        assert len(ro.open_orders()) == 1

    def test_provider_senza_lista_ordini(self):
        _seed_open()
        prov = _Prov(live=[])

        class _NoList:
            pass

        res = ro.reconcile(_NoList())
        assert res["unavailable"] is True

    def test_ancora_aperto(self):
        _seed_open()
        res = ro.reconcile(_Prov(live=[{"orderId": "0xabc"}]))
        assert res["still_open"] == 1 and res["filled"] == 0

    def test_ritirato_da_noi(self):
        _seed_open(cancel_attempted=True, cancel_ok=True)
        res = ro.reconcile(_Prov(live=[]))
        assert res["cancelled"] == 1 and res["filled"] == 0
        assert ro.orders(status=ro.ST_CANCELLED)[0]["order_id"] == "0xabc"

    def test_scadenza_on_chain_non_inventa_riempimenti(self):
        """Assente dal book DOPO la scadenza: ambiguo -> da verificare a mano."""
        _seed_open(expires_at=_iso(datetime.now(timezone.utc) -
                                   timedelta(hours=1)))
        res = ro.reconcile(_Prov(live=[]))
        assert res["unconfirmed"] == 1
        assert res["filled"] == 0
        row = ro.orders(status=ro.ST_UNCONFIRMED)[0]
        assert "verificare" in row["note"]

    def test_riempimento_registra_la_bet(self, monkeypatch):
        monkeypatch.setattr(ro, "_bet_row_exists", lambda m, e: False)
        _seed_open()
        saved = {}

        def _save(**kw):
            saved.update(kw)
            return True

        res = ro.reconcile(_Prov(live=[]), save_bet_fn=_save)
        assert res["filled"] == 1
        assert saved["match_id"] == "sx-L1" and saved["esito"] == "1"
        assert saved["mode"] == "live"
        assert saved["status"] == "RESTING_FILLED"
        assert saved["bet_id"] == "0xabc"
        assert saved["price"] == 1.70 and saved["stake"] == 2.0
        assert saved["market_id"] == "0xmid" and saved["selection_id"] == 1
        assert res["fills"][0]["order_id"] == "0xabc"
        assert ro.orders(status=ro.ST_FILLED)[0]["filled_at"]

    def test_riga_gia_presente_non_sovrascrive(self, monkeypatch):
        """Un riempimento che collide col ledger e' DICHIARATO, mai scritto."""
        monkeypatch.setattr(ro, "_bet_row_exists", lambda m, e: True)
        _seed_open()
        chiamate = []
        res = ro.reconcile(_Prov(live=[]),
                           save_bet_fn=lambda **kw: chiamate.append(kw))
        assert chiamate == []
        assert res["filled"] == 0
        assert any(e.startswith("duplicate:") for e in res["errors"])
        assert ro.orders(status=ro.ST_DUPLICATE)

    def test_salvataggio_fallito_riga_resta_aperta(self, monkeypatch):
        monkeypatch.setattr(ro, "_bet_row_exists", lambda m, e: False)
        _seed_open()

        def _boom(**kw):
            raise RuntimeError("DB chiuso")

        res = ro.reconcile(_Prov(live=[]), save_bet_fn=_boom)
        assert res["filled"] == 0
        assert any(e.startswith("save:") for e in res["errors"])
        assert len(ro.open_orders()) == 1

    def test_riga_senza_match_id_da_verificare(self, monkeypatch):
        monkeypatch.setattr(ro, "_bet_row_exists", lambda m, e: False)
        _seed_open(match_id="", esito="")
        res = ro.reconcile(_Prov(live=[]))
        assert res["unconfirmed"] == 1

    def test_guardia_ledger_fail_closed(self, monkeypatch):
        """Lettura del ledger impossibile -> si considera ESISTENTE."""
        import tracker

        def _boom():
            raise RuntimeError("DB non apribile")

        monkeypatch.setattr(tracker, "_get_conn", _boom)
        assert ro._bet_row_exists("sx-L1", "1") is True

    def test_salvataggio_di_default_usa_il_ledger(self, monkeypatch):
        """Senza `save_bet_fn` la scrittura passa da `tracker.save_bet`."""
        import tracker
        monkeypatch.setattr(ro, "_bet_row_exists", lambda m, e: False)
        _seed_open()
        visti = []
        monkeypatch.setattr(tracker, "save_bet",
                            lambda **kw: visti.append(kw))
        res = ro.reconcile(_Prov(live=[]))
        assert res["filled"] == 1 and len(visti) == 1
        assert visti[0]["mode"] == "live"


# ---------------------------------------------------------------------------
# 6. Scadenza
# ---------------------------------------------------------------------------

class TestExpire:
    def test_oltre_la_deadline_ritira(self):
        _seed_open(deadline=_iso(datetime.now(timezone.utc) -
                                 timedelta(minutes=5)))
        prov = _Prov()
        res = ro.expire(prov)
        assert res["cancel_requested"] == 1 and res["cancel_ok"] == 1
        assert prov.cancel_calls == [("0xmid", "0xabc")]
        row = ro.open_orders()[0]
        assert row["cancel_attempted"] is True and row["cancel_ok"] is True
        # NON e' 'cancelled' qui: lo decide la riconciliazione (un ordine
        # riempito all'ultimo istante non va perso).
        assert row["status"] == ro.ST_OPEN

    def test_dentro_la_deadline_non_tocca(self):
        _seed_open(deadline=_iso(datetime.now(timezone.utc) +
                                 timedelta(hours=1)))
        prov = _Prov()
        assert ro.expire(prov)["cancel_requested"] == 0
        assert prov.cancel_calls == []

    def test_cancellazione_fallita_dichiarata(self):
        _seed_open(deadline=_iso(datetime.now(timezone.utc) -
                                 timedelta(minutes=5)))
        prov = _Prov(cancel_ok=False)
        res = ro.expire(prov)
        assert res["cancel_ok"] == 0
        assert ro.open_orders()[0]["cancel_ok"] is False

    def test_cancellazione_che_solleva(self):
        _seed_open(deadline=_iso(datetime.now(timezone.utc) -
                                 timedelta(minutes=5)))

        class _Boom(_Prov):
            def cancel_order(self, market_id, bet_id):
                raise RuntimeError("rete giu'")

        res = ro.expire(_Boom())
        assert res["cancel_ok"] == 0
        assert any(e.startswith("cancel:") for e in res["errors"])


# ---------------------------------------------------------------------------
# 7. Ciclo completo
# ---------------------------------------------------------------------------

class TestRunCycle:
    def test_ordine_dei_passi_e_fail_safe(self, monkeypatch):
        """`expire` PRIMA, `reconcile` DOPO: e' l'ordine che rende non ambigua
        l'assenza dal book."""
        ordine = []
        monkeypatch.setattr(ro, "expire", lambda *a, **k: ordine.append("e") or {})
        monkeypatch.setattr(ro, "reconcile", lambda *a, **k: ordine.append("r") or {})
        ro.run_cycle(_Prov())
        assert ordine == ["e", "r"]

    def test_errori_diventano_campi(self, monkeypatch):
        def _boom(*a, **k):
            raise RuntimeError("ko")

        monkeypatch.setattr(ro, "expire", _boom)
        monkeypatch.setattr(ro, "reconcile", _boom)
        out = ro.run_cycle(_Prov())
        assert "expire:RuntimeError" in out["error"]
        assert "reconcile:RuntimeError" in out["error"]

    def test_giro_reale_end_to_end(self, monkeypatch):
        """Ordine aperto + book che non lo mostra piu' -> riga `bets` scritta."""
        monkeypatch.setattr(ro, "_bet_row_exists", lambda m, e: False)
        _seed_open()
        visti = []
        out = ro.run_cycle(_Prov(live=[]),
                           save_bet_fn=lambda **kw: visti.append(kw))
        assert out["reconciled"]["filled"] == 1
        assert len(visti) == 1 and visti[0]["mode"] == "live"


# ---------------------------------------------------------------------------
# 8. Report e CLI
# ---------------------------------------------------------------------------

class TestReport:
    def test_summary(self):
        _seed_open(stake=2.0)
        _seed_open(order_id="b", stake=1.0, status=ro.ST_FILLED,
                   match_id="sx-L2", esito="2")
        s = ro.summary()
        assert s["enabled"] is True
        assert s["open"] == 1 and s["open_stake"] == 2.0
        assert s["filled"] == 1 and s["total"] == 2
        assert s["by_status"][ro.ST_OPEN] == 1

    def test_format_report_dichiara_lo_stato(self):
        _seed_open()
        text = ro.format_report()
        assert "ORDINI RESTING" in text
        assert "aperti: 1" in text

    def test_format_report_segnala_i_da_verificare(self):
        _seed_open(status=ro.ST_UNCONFIRMED)
        assert "da verificare a mano: 1" in ro.format_report()

    def test_cli_status(self, capsys):
        _seed_open()
        assert ro.main(["--status"]) == 0
        assert "ORDINI RESTING" in capsys.readouterr().out

    def test_cli_json(self, capsys):
        assert ro.main(["--status", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["open"] == 0


# ---------------------------------------------------------------------------
# 9. Wiring in auto_bet._live_fill
# ---------------------------------------------------------------------------

class TestWiringLiveFill:
    """Il percorso taker non eseguibile NON butta piu' via l'edge: parcheggia."""

    def _setup(self, monkeypatch, prov):
        import execution_engine as ee
        engine = type("_Eng", (), {"provider": prov})()
        monkeypatch.setattr(ee, "ExecutionEngine", lambda *a, **k: engine)
        return ee

    def test_book_sottile_parcheggia(self, monkeypatch):
        prov = _Prov(order=_Order(status="SUBMITTED"), book_depth=4.0)
        self._setup(monkeypatch, prov)
        res = auto_bet._live_fill(_pick(), stake=5.0, floor=1.70)
        assert res is not None and res["ok"] is True
        assert res["resting"] is True
        # `bet_id` resta None: anche senza il flag, il controllo sul bet_id
        # impedirebbe la scrittura di una riga mai riempita.
        assert res["bet_id"] is None
        assert res["order_id"] == "0xabc"
        assert res["status"] == "RESTING_OPEN"
        # La richiesta al book e' RESTING (GTC), non IOC.
        assert [c["persistence"] for c in prov.place_calls] == ["PERSIST"]
        assert ro.open_orders()

    def test_book_sottile_senza_resting_torna_none(self, monkeypatch):
        """`RESTING_ORDERS=0` ripristina ESATTAMENTE il comportamento di prima."""
        monkeypatch.setenv("RESTING_ORDERS", "0")
        import liquidity_monitor
        chiamate = []
        monkeypatch.setattr(liquidity_monitor, "record_skip",
                            lambda *a, **k: chiamate.append(a))
        prov = _Prov(order=_Order(status="SUBMITTED"), book_depth=4.0)
        self._setup(monkeypatch, prov)
        assert auto_bet._live_fill(_pick(), stake=5.0, floor=1.70) is None
        assert prov.place_calls == []
        # Lo scarto per book sottile resta tracciato come prima.
        assert any("depth_vs_stake" in a for a in chiamate)

    def test_ordine_non_riempito_diventa_resting(self, monkeypatch):
        """IOC cancellato per NO_LIQUIDITY -> si riprova RESTING."""
        ii = _Order(ok=False, bet_id=None, status="CANCELLED", matched=0.0,
                    error="ordine non riempito")
        done = _Order(status="SUBMITTED")
        stato = {"n": 0}

        class _Two(_Prov):
            def place_limit_order(self, *a, **k):
                super().place_limit_order(*a, **k)   # registra la chiamata
                stato["n"] += 1
                return ii if stato["n"] == 1 else done

        prov = _Two(book_depth=100.0)
        self._setup(monkeypatch, prov)
        res = auto_bet._live_fill(_pick(), stake=5.0, floor=1.70)
        assert res is not None and res.get("resting") is True
        assert [c["persistence"] for c in prov.place_calls] == ["LAPSE",
                                                               "PERSIST"]

    def test_failure_non_ritenta_come_resting(self, monkeypatch):
        """Un errore VERO (credenziali/rete/stake) non si riprova come resting."""
        ii = _Order(ok=False, bet_id=None, status="FAILURE", matched=0.0,
                    error="HTTP 403")
        prov = _Prov(order=ii, book_depth=100.0)
        self._setup(monkeypatch, prov)
        res = auto_bet._live_fill(_pick(), stake=5.0, floor=1.70)
        assert res is not None and res["ok"] is False
        assert len(prov.place_calls) == 1               # nessun secondo POST
        assert ro.open_orders() == []

    def test_riempito_subito_resta_un_ordine_normale(self, monkeypatch):
        prov = _Prov(order=_Order(status="FULLY_FILLED", matched=5.0,
                                  price=1.72), book_depth=4.0)
        self._setup(monkeypatch, prov)
        res = auto_bet._live_fill(_pick(), stake=5.0, floor=1.70)
        assert res["ok"] is True and not res.get("resting")
        assert res["bet_id"] == "0xabc"
        assert res["price"] == 1.72 and res["stake"] == 5.0

    def test_errore_del_modulo_non_rompe_il_flusso(self, monkeypatch):
        monkeypatch.setattr(ro, "place",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
        prov = _Prov(order=_Order(status="SUBMITTED"), book_depth=4.0)
        self._setup(monkeypatch, prov)
        assert auto_bet._live_fill(_pick(), stake=5.0, floor=1.70) is None


# ---------------------------------------------------------------------------
# 9b. Capitale immobilizzato: un RESTING aperto blocca escrow su SX ma non ha
#     una riga in `bets`: se non entrasse nel recinto, l'equity calerebbe al
#     piazzamento (drawdown fantasma, classe del 09/10) e il 40% non lo
#     vedrebbe.
# ---------------------------------------------------------------------------

class TestCapitaleImmobilizzato:
    def test_snapshot_include_il_resting(self, temp_db):
        _seed_open(order_id="0xa", stake=2.0, match_id="sx-L1")
        _seed_open(order_id="0xb", stake=1.5, match_id="sx-L2")
        stake, count = auto_bet._open_live_snapshot()
        assert stake == 3.5 and count == 2

    def test_ordine_riempito_non_conta_piu(self, temp_db):
        """Al riempimento la riga passa a `filled` E nasce la riga `bets`:
        il capitale non si conta due volte."""
        _seed_open(order_id="0xa", stake=2.0, status=ro.ST_FILLED)
        tracker.save_bet("sx-L1", "TENNIS", "1", market_id="0xmid",
                         selection_id=1, price=1.70, stake=2.0, mode="live",
                         status="RESTING_FILLED", bet_id="0xa")
        stake, count = auto_bet._open_live_snapshot()
        assert stake == 2.0 and count == 1

    def test_recinto_vede_il_capitale_parcheggiato(self, temp_db):
        """Con 5 resting da 2.0 (10.0 USDC) su un bankroll 20.0 il tetto del
        40% (8.0) e' gia' sfondato: nessun ordine nuovo."""
        for i in range(5):
            _seed_open(order_id=f"0x{i}", stake=2.0, match_id=f"sx-L{i}")
        status = auto_bet.open_exposure_status(20.0)
        assert status["open_stake"] == 10.0
        assert status["count"] == 5
        assert status["blocked"] is True

    def test_proiezione_respinge_oltre_il_tetto(self, temp_db):
        for i in range(3):
            _seed_open(order_id=f"0x{i}", stake=2.0, match_id=f"sx-L{i}")
        # cap = 20.0 x 40% = 8.0; 6.0 aperti + 2.5 nuovo = 8.5 -> respinto
        assert auto_bet.exposure_allows(20.0, 2.5)["allowed"] is False
        assert auto_bet.exposure_allows(20.0, 1.5)["allowed"] is True

    def test_registro_illeggibile_non_apre_il_recinto(self, monkeypatch,
                                                      temp_db):
        """Fail-open sulla lettura: una telemetria rotta aggiunge 0 e non
        allarga il recinto."""
        monkeypatch.setattr(ro, "open_stake",
                            lambda: (_ for _ in ()).throw(RuntimeError("ko")))
        stake, count = auto_bet._open_live_snapshot()
        assert stake == 0.0 and count == 0


# ---------------------------------------------------------------------------
# 10. Agenti + Chief
# ---------------------------------------------------------------------------

class TestAgenti:
    def test_open_resting_con_lettore_iniettato(self):
        from agents.execution_agent import ExecutionAgent
        ex = ExecutionAgent()
        state = ex.open_resting(reader=lambda: {"open": 2, "open_stake": 3.0})
        assert state["open"] == 2 and state["unavailable"] is False

    def test_open_resting_lettore_rotto_fail_safe(self):
        from agents.execution_agent import ExecutionAgent
        ex = ExecutionAgent()

        def _boom():
            raise RuntimeError("registro rotto")

        state = ex.open_resting(reader=_boom)
        assert state["unavailable"] is True
        assert "lettura registro fallita" in state["reason"]

    def test_open_resting_import_non_disponibile(self, monkeypatch):
        """Anche con il modulo assente l'agente risponde, non solleva."""
        import builtins
        from agents.execution_agent import ExecutionAgent
        real = builtins.__import__

        def _boom(name, *a, **k):
            if name == "resting_orders":
                raise ImportError("no")
            return real(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", _boom)
        state = ExecutionAgent().open_resting()
        assert state["unavailable"] is True

    def test_open_resting_default_delega_al_modulo(self):
        from agents.execution_agent import ExecutionAgent
        _seed_open(stake=1.5)
        state = ExecutionAgent().open_resting()
        assert state["open"] == 1 and state["open_stake"] == 1.5

    def test_execution_output_espone_il_resting(self):
        from agents.contracts import ExecutionOutput
        out = ExecutionOutput(resting={"open": 3, "open_stake": 4.5})
        js = out.as_json()
        assert js["resting_open"] == 3 and js["resting_stake"] == 4.5

    def test_cycle_report_espone_il_resting(self):
        from agents.contracts import CycleReport
        rep = CycleReport(resting={"open": 1})
        assert rep.as_json()["resting"] == {"open": 1}

    def test_chief_riporta_lo_stato_resting(self, monkeypatch):
        """Il ciclo del Capo legge il capitale parcheggiato dall'Esecuzione."""
        from agents.contracts import CycleReport
        from agents.execution_agent import ExecutionAgent
        ex = ExecutionAgent()
        monkeypatch.setattr(ex, "open_resting",
                            lambda **k: {"open": 2, "open_stake": 3.0})
        rep = CycleReport()
        rep.resting = ex.open_resting()
        assert rep.resting["open_stake"] == 3.0
        assert rep.as_json()["resting"]["open"] == 2

    def test_agenti_non_importano_il_percorso_ordini(self):
        """`import agents` resta leggero: nessun `auto_bet` a livello modulo."""
        src = Path("agents/execution_agent.py").read_text(encoding="utf-8")
        # `auto_bet` compare SOLO dentro la funzione (import pigro).
        assert "    import auto_bet\n" in src
        assert "\nimport auto_bet\n" not in src


# ---------------------------------------------------------------------------
# 11. Tripwire su sorgente, bot e IaC
# ---------------------------------------------------------------------------

class TestTripwire:
    def test_import_leggero_in_sottoprocesso(self):
        code = ("import sys, resting_orders;"
                "bad=[m for m in ('tracker','auto_bet','bot','odds_api',"
                "'decision') if m in sys.modules];"
                "print(','.join(bad))")
        out = subprocess.run([sys.executable, "-c", code],
                             capture_output=True, text=True,
                             cwd=str(Path(__file__).parent))
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == ""

    def test_wiring_presente_in_live_fill(self):
        src = Path("auto_bet.py").read_text(encoding="utf-8")
        assert "def _resting_place(" in src
        body = src.split("def _live_fill(")[1]
        assert "_resting_place(prov, pick" in body

    def test_il_resting_non_scrive_il_ledger_da_solo(self):
        """Il modulo non importa il ledger a livello di modulo (import pigro)."""
        src = Path("resting_orders.py").read_text(encoding="utf-8")
        head = src.split("def place(")[0]
        assert "from tracker import" not in head
        assert "import tracker" not in head

    def test_job_registrato_nel_bot(self):
        src = Path("bot.py").read_text(encoding="utf-8")
        assert "def resting_orders_job(" in src
        assert "run_repeating(resting_orders_job" in src
        assert "interval=300" in src.split("run_repeating(resting_orders_job")[1][:120]

    def test_job_non_notifica_senza_riempimenti(self):
        src = Path("bot.py").read_text(encoding="utf-8")
        body = src.split("async def resting_orders_job")[1].split(
            "async def btts_watch_job")[0]
        assert "if not fills:" in body
        assert "_send_report_to_recipients" in body

    def test_env_dichiarate_nella_iac(self):
        iac = Path(".railway/railway.ts").read_text(encoding="utf-8")
        for name in ("RESTING_ORDERS", "RESTING_MAX_OPEN", "RESTING_TTL_MIN",
                     "RESTING_CANCEL_BEFORE_MIN", "RESTING_EXPIRY_MARGIN_S",
                     "RESTING_STATE"):
            assert f"{name}: preserve()" in iac, name

    def test_ogni_env_letta_e_dichiarata(self):
        """Nessuna variabile letta dal modulo puo' restare fuori dalla IaC."""
        import re
        src = Path("resting_orders.py").read_text(encoding="utf-8")
        letti = set(re.findall(r"os\.getenv\(\s*\"(RESTING_[A-Z_]+)\"", src))
        assert letti, "nessuna env letta: il test non sta misurando nulla"
        iac = Path(".railway/railway.ts").read_text(encoding="utf-8")
        for name in sorted(letti):
            assert f"{name}: preserve()" in iac, name
