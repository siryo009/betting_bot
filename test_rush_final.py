"""test_rush_final.py — RUSH FINALE del torneo Gridiron Gauntlet (13/10/2026).

TUTTI OFFLINE: il wallet non si legge mai per davvero (le letture di rete sono
`auto_bet._live_wallet_snapshot` / `_open_live_snapshot`, iniettate o spente),
il registro RESTING vive nella `tmp_path` del test e il provider e' un finto in
memoria. Zero ordini, zero crediti, zero rete.

Perche' questi test difendono del DENARO: il rush alza lo stake di ordini reali
nell'ultima settimana del torneo. Le tre proprieta' che li rendono affidabili:
- **nessun pick nuovo** (il rush agisce solo su candidati proposti dai gate);
- **nessun tetto allargato** (il tetto e' il `min` di quattro limiti, ognuno dei
  quali puo' solo restringere);
- **fail-closed sull'incertezza** (equity illeggibile -> stake invariato).

⚠️ Le date sono SEMPRE relative a `now`: una finestra fissa (17-20/10) scadrebbe
col calendario e il test fallirebbe da solo nel momento peggiore (lezione del
15/09 e del 17/09).
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

import rush_final as rf


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


@pytest.fixture(autouse=True)
def _finestra(monkeypatch):
    """Finestra ATTIVA (ieri -> domani) e interruttore acceso.

    `conftest.py` spegne `RUSH_FINAL` per tutti gli altri file: qui e' il file
    dedicato e la finestra e' costruita relativa a `now` (mai fissa).
    """
    now = datetime.now(timezone.utc)
    monkeypatch.setenv("RUSH_FINAL", "1")
    monkeypatch.setenv("RUSH_START", _iso(now - timedelta(days=1)))
    monkeypatch.setenv("RUSH_END", _iso(now + timedelta(days=1)))
    yield


def _rep(**over) -> dict:
    """Istantanea del rush nella forma di `report()` (valori espliciti)."""
    base = {
        "ok": True, "reason": "active", "active": True, "note": "",
        "wallet": {"equity": 40.0, "available": 40.0, "exposure": 0.0,
                   "error": None},
        "exposure": {"open": 4.0, "orders": 2, "cap_pct": 0.70,
                     "cap_usdc": 28.0, "free": 24.0, "utilization": 0.1429,
                     "error": None},
        "recycle": {"stale": 0, "stale_stake": 0.0, "open_stake": 4.0,
                    "open": 2, "window_min": 180.0, "boost": 1.15,
                    "pressure": 0.0},
        "budget": {"declared": 0.0, "cap_usdc": 28.0, "open": 4.0, "free": 24.0,
                   "left": 24.0},
        "stake": {"max": 8.0, "aggression": 1.25},
        "resting": {"enabled": True, "open": 0, "open_stake": 0.0, "ttl_min": 45.0},
        "tournament": {"active": True},
        "classes": [],
        "window": {"start": None, "end": None, "valid": True,
                   "raw": {"start": "2026-10-17", "end": "2026-10-20"}},
    }
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = {**base[key], **value}
        else:
            base[key] = value
    return base


@pytest.fixture
def cap_torneo(monkeypatch):
    """Cap del torneo deterministico (altrimenti dipende dall'env di Railway)."""
    import tournament

    def _set(value: float):
        monkeypatch.setattr(tournament, "max_order_stake", lambda: float(value))

    return _set


class _ProviderFinto:
    """Provider minimo: registra le cancellazioni, mai una chiamata di rete."""

    def __init__(self, ok: bool = True, raises: bool = False):
        self.ok = ok
        self.raises = raises
        self.calls: list = []

    def cancel_order(self, market_id, order_id):
        self.calls.append((market_id, order_id))
        if self.raises:
            raise RuntimeError("provider giu'")
        return self.ok


# ---------------------------------------------------------------------------
# 1. Finestra e gate a costo zero
# ---------------------------------------------------------------------------

class TestFinestra:

    def test_default_e_finestra_del_torneo(self, monkeypatch):
        """Senza env la finestra e' quella del torneo (17-20/10)."""
        monkeypatch.delenv("RUSH_START", raising=False)
        monkeypatch.delenv("RUSH_END", raising=False)
        w = rf.window()
        assert w["raw"] == {"start": rf.DEFAULT_START, "end": rf.DEFAULT_END}
        assert w["valid"] is True
        assert w["start"].startswith("2026-10-17")
        assert w["end"].startswith("2026-10-20")

    def test_env_sovrascrive_la_finestra(self, monkeypatch):
        monkeypatch.setenv("RUSH_START", "2026-11-01")
        monkeypatch.setenv("RUSH_END", "2026-11-05")
        w = rf.window()
        assert w["raw"]["start"] == "2026-11-01"
        assert w["valid"] is True

    def test_finestra_invertita_non_valida(self, monkeypatch):
        monkeypatch.setenv("RUSH_START", "2026-10-20")
        monkeypatch.setenv("RUSH_END", "2026-10-17")
        assert rf.window()["valid"] is False
        assert rf.precheck() == {"active": False, "reason": "window_invalid"}

    def test_data_illeggibile_non_valida(self, monkeypatch):
        monkeypatch.setenv("RUSH_START", "pippo")
        assert rf.window()["valid"] is False
        assert rf.precheck()["reason"] == "window_invalid"

    def test_precheck_dentro_finestra(self):
        assert rf.precheck() == {"active": True, "reason": "in_window"}

    def test_precheck_prima_della_finestra(self, monkeypatch):
        now = datetime.now(timezone.utc)
        monkeypatch.setenv("RUSH_START", _iso(now + timedelta(hours=6)))
        monkeypatch.setenv("RUSH_END", _iso(now + timedelta(days=1)))
        assert rf.precheck() == {"active": False, "reason": "before_window"}

    def test_precheck_dopo_la_finestra(self, monkeypatch):
        now = datetime.now(timezone.utc)
        monkeypatch.setenv("RUSH_START", _iso(now - timedelta(days=2)))
        monkeypatch.setenv("RUSH_END", _iso(now - timedelta(hours=1)))
        assert rf.precheck() == {"active": False, "reason": "window_ended"}

    def test_interruttore_spento(self, monkeypatch):
        monkeypatch.setenv("RUSH_FINAL", "0")
        assert rf.enabled() is False
        assert rf.precheck() == {"active": False, "reason": "rush_off"}

    def test_valori_di_off_riconosciuti(self, monkeypatch):
        for value in ("0", "false", "FALSE", "no", "off", " "):
            monkeypatch.setenv("RUSH_FINAL", value)
            assert rf.enabled() is False, value

    def test_bordi_inclusivi(self, monkeypatch):
        """`start <= now <= end`: i bordi sono DENTRO (una giornata intera)."""
        monkeypatch.delenv("RUSH_START", raising=False)
        monkeypatch.delenv("RUSH_END", raising=False)
        w = rf.window()
        start = rf._stamp(w["start"])
        end = rf._stamp(w["end"])
        assert rf.precheck(now=start)["active"] is True
        assert rf.precheck(now=end)["active"] is True
        assert rf.precheck(now=start - timedelta(seconds=1))["reason"] == "before_window"
        assert rf.precheck(now=end + timedelta(seconds=1))["reason"] == "window_ended"

    def test_precheck_non_legge_il_wallet(self, monkeypatch):
        """Il gate caldo (ogni 60s) deve costare ZERO: nessuna lettura di rete."""
        chiamate: list = []

        def _boom():
            chiamate.append(1)
            raise AssertionError("il precheck ha toccato il wallet")

        monkeypatch.setattr(rf, "_wallet", _boom)
        monkeypatch.setattr(rf, "_exposure", _boom)
        assert rf.precheck()["active"] is True
        assert chiamate == []


# ---------------------------------------------------------------------------
# 2. Configurazione
# ---------------------------------------------------------------------------

class TestConfig:

    def test_default(self, monkeypatch):
        for name in ("RUSH_BUDGET_USDC", "RUSH_MAX_STAKE_USDC",
                     "RUSH_AGGRESSION", "RUSH_RECYCLE_WINDOW_MIN",
                     "RUSH_RECYCLE_BOOST", "RUSH_RESTING_TTL_MIN",
                     "RUSH_CLEAR_RESTING_AFTER_MIN", "RUSH_CLASSES",
                     "RUSH_CLEAR_RESTING"):
            monkeypatch.delenv(name, raising=False)
        assert rf.budget_declared() == 0.0
        assert rf.max_order_stake() == 8.0
        assert rf.aggression() == 1.25
        assert rf.recycle_window_min() == 180.0
        assert rf.recycle_boost() == 1.15
        assert rf.resting_ttl_minutes() == 45.0
        assert rf.clear_resting_enabled() is True
        assert rf.clear_resting_after_min() == 30.0
        assert rf.classes() == ()
        assert rf.covers("qualsiasi") is True

    def test_aggressione_non_riduce_mai(self, monkeypatch):
        """Un valore sotto 1.0 non puo' SGRASSARE lo stake (proprieta', non un numero).

        Il clamp va verso il DEFAULT (1.25/1.15), non verso 1.0: cio' che conta
        e' che il risultato non sia mai sotto 1.0 — un moltiplicatore < 1
        ridurrebbe lo stake deciso dai gate.
        """
        monkeypatch.setenv("RUSH_AGGRESSION", "0.5")
        assert rf.aggression() >= 1.0
        monkeypatch.setenv("RUSH_RECYCLE_BOOST", "0.8")
        assert rf.recycle_boost() >= 1.0
        monkeypatch.setenv("RUSH_AGGRESSION", "0")
        assert rf.aggression() >= 1.0

    def test_interruttore_autorevole_anche_con_rep_iniettato(self, monkeypatch, cap_torneo):
        """`RUSH_FINAL=0` vince su un verdetto iniettato: nessun ordine accelerato."""
        cap_torneo(50.0)
        monkeypatch.setenv("RUSH_FINAL", "0")
        v = rf.stake_for(3.0, pick={"mercato": "1X2"}, rep=_rep())
        assert v["applied"] is False and v["stake"] == 3.0
        assert v["reason"] == "rush_off"
        out = rf.apply([{"match_id": "sx-1", "mercato": "1X2", "stake": 3.0}],
                       rep=_rep())
        assert out[0]["stake"] == 3.0 and not out[0].get(rf.RUSH_FLAG)

    def test_valore_non_numerico_usa_il_default(self, monkeypatch):
        monkeypatch.setenv("RUSH_MAX_STAKE_USDC", "abc")
        assert rf.max_order_stake() == 8.0
        monkeypatch.setenv("RUSH_BUDGET_USDC", "")
        assert rf.budget_declared() == 0.0

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("RUSH_MAX_STAKE_USDC", "12.5")
        monkeypatch.setenv("RUSH_BUDGET_USDC", "30")
        monkeypatch.setenv("RUSH_RESTING_TTL_MIN", "20")
        assert rf.max_order_stake() == 12.5
        assert rf.budget_declared() == 30.0
        assert rf.resting_ttl_minutes() == 20.0

    def test_ttl_minimo_protetto(self, monkeypatch):
        """Un TTL a 0 minuti ritirerebbe ogni ordine all'istante."""
        monkeypatch.setenv("RUSH_RESTING_TTL_MIN", "0")
        assert rf.resting_ttl_minutes() == 45.0

    def test_perimetro(self, monkeypatch):
        monkeypatch.setenv("RUSH_CLASSES", "ML, nfl ,")
        assert rf.classes() == ("ML", "NFL")
        assert rf.covers("ml") is True
        assert rf.covers("1X2") is False
        assert rf.covers(None) is False

    def test_perimetro_vuoto_copre_tutto(self, monkeypatch):
        monkeypatch.setenv("RUSH_CLASSES", "")
        assert rf.classes() == ()
        assert rf.covers("qualunque mercato") is True
        assert rf.covers(None) is True


# ---------------------------------------------------------------------------
# 3. Il punto di decisione: `stake_for`
# ---------------------------------------------------------------------------

class TestStakeFor:

    def test_rush_non_attivo_stake_invariato(self, monkeypatch):
        monkeypatch.setenv("RUSH_FINAL", "0")
        v = rf.stake_for(3.0, pick={"mercato": "1X2"}, rep=_rep())
        assert v["applied"] is False and v["blocked"] is False
        assert v["stake"] == 3.0

    def test_rep_non_ok_propaga_il_motivo(self):
        v = rf.stake_for(3.0, pick={"mercato": "1X2"},
                         rep=_rep(ok=False, reason="enclosure_full"))
        assert v["applied"] is False and v["reason"] == "enclosure_full"
        assert v["stake"] == 3.0

    def test_mercato_fuori_perimetro(self, monkeypatch):
        monkeypatch.setenv("RUSH_CLASSES", "ML")
        v = rf.stake_for(3.0, pick={"mercato": "1X2"}, rep=_rep())
        assert v["reason"] == "class_not_covered"
        assert v["applied"] is False

    def test_base_sotto_il_minimo_ordine(self):
        v = rf.stake_for(0.5, pick={"mercato": "1X2"}, rep=_rep())
        assert v["reason"] == "below_min_stake"
        assert v["applied"] is False

    def test_base_zero_o_negativa_invariata(self):
        for value in (0.0, -5.0, None):
            v = rf.stake_for(value, pick={"mercato": "1X2"}, rep=_rep())
            assert v["applied"] is False and v["stake"] == float(value or 0.0)

    def test_stake_ostile_non_solleva(self):
        """Contratto "non solleva mai": anche un input illeggibile."""
        for value in ("abc", [], {}, object()):
            v = rf.stake_for(value, pick={"mercato": "1X2"}, rep=_rep())
            assert v["applied"] is False
            assert v["stake"] == 0.0

    def test_applicato_con_aggressione(self, cap_torneo):
        cap_torneo(5.0)
        v = rf.stake_for(3.0, pick={"mercato": "1X2"}, rep=_rep())
        assert v["applied"] is True
        assert v["boost"] == 1.25          # pressione 0 -> nessun boost ricircolo
        assert v["stake"] == pytest.approx(3.75)
        assert v["ceiling"] == pytest.approx(5.0)
        assert v["reason"] == "rush"

    def test_pressione_di_ricircolo_alza_il_boost(self, cap_torneo):
        cap_torneo(50.0)
        rep = _rep(recycle={"pressure": 1.0})
        v = rf.stake_for(3.0, pick={"mercato": "1X2"}, rep=rep)
        assert v["boost"] == pytest.approx(1.25 * 1.15)
        assert v["stake"] == pytest.approx(3.0 * 1.25 * 1.15)

    def test_tetto_del_recinto_limita(self, cap_torneo):
        cap_torneo(50.0)
        rep = _rep(exposure={"free": 3.2},
                   budget={"left": 3.2})
        v = rf.stake_for(3.0, pick={"mercato": "1X2"}, rep=rep)
        assert v["stake"] == pytest.approx(3.2)
        assert v["ceiling"] == pytest.approx(3.2)

    def test_gia_al_tetto(self, cap_torneo):
        cap_torneo(5.0)
        v = rf.stake_for(5.0, pick={"mercato": "1X2"}, rep=_rep())
        assert v["applied"] is False
        assert v["reason"] == "already_at_cap"
        assert v["stake"] == 5.0

    def test_nessuno_spazio(self, monkeypatch, cap_torneo):
        cap_torneo(0.0)
        monkeypatch.setenv("RUSH_MAX_STAKE_USDC", "0")
        rep = _rep(exposure={"free": 0.0}, budget={"left": 0.0})
        v = rf.stake_for(3.0, pick={"mercato": "1X2"}, rep=rep)
        assert v["reason"] == "no_room"
        assert v["applied"] is False

    def test_sotto_il_minimo_dopo_il_tetto_blocca(self, cap_torneo):
        """Un risanamento sotto il minimo ordine non si piazza: si SEGNALA."""
        cap_torneo(0.5)
        v = rf.stake_for(1.0, pick={"mercato": "1X2"}, rep=_rep())
        assert v["blocked"] is True
        assert v["reason"] == "below_min_after_cap"
        assert v["applied"] is False

    def test_stake_non_stringe_mai(self, cap_torneo):
        """Il rush puo' solo ALZARE rispetto alla base dei gate."""
        cap_torneo(5.0)
        for base in (1.0, 2.0, 3.5, 4.99):
            v = rf.stake_for(base, pick={"mercato": "1X2"}, rep=_rep())
            assert v["stake"] >= base - 1e-9


# ---------------------------------------------------------------------------
# 4. Ricircolo: pressione e budget
# ---------------------------------------------------------------------------

class TestRicircolo:

    def test_pressione_da_capitale_fermo(self):
        stale = {"stale_stake": 2.0, "open_stake": 4.0}
        p = rf.recycle_pressure(stale, {"utilization": 0.1})
        assert p == 0.5

    def test_pressione_da_recinto_saturo(self):
        stale = {"stale_stake": 0.0, "open_stake": 0.0}
        assert rf.recycle_pressure(stale, {"utilization": 0.8}) == 0.8

    def test_pressione_massimo_delle_due(self):
        stale = {"stale_stake": 1.0, "open_stake": 4.0}
        assert rf.recycle_pressure(stale, {"utilization": 0.9}) == 0.9

    def test_pressione_clampata_a_uno(self):
        stale = {"stale_stake": 9.0, "open_stake": 4.0}
        assert rf.recycle_pressure(stale, {"utilization": 2.0}) == 1.0

    def test_pressione_senza_dati(self):
        assert rf.recycle_pressure({"open_stake": 0.0}, None) == 0.0
        assert rf.recycle_pressure({"open_stake": 0.0}, {"utilization": None}) == 0.0

    def test_budget_mai_oltre_il_recinto(self, monkeypatch):
        ex = {"cap_usdc": 28.0}
        monkeypatch.delenv("RUSH_BUDGET_USDC", raising=False)
        assert rf.budget_usdc(ex) == 28.0            # env non impostata = recinto
        monkeypatch.setenv("RUSH_BUDGET_USDC", "10")
        assert rf.budget_usdc(ex) == 10.0            # piu' severa vince
        monkeypatch.setenv("RUSH_BUDGET_USDC", "100")
        assert rf.budget_usdc(ex) == 28.0            # NON allarga il recinto

    def test_budget_fail_closed_senza_equity(self):
        assert rf.budget_usdc({"cap_usdc": None}) == 0.0


# ---------------------------------------------------------------------------
# 5. `report()` — fail-closed sui casi che contano
# ---------------------------------------------------------------------------

class TestReport:

    def test_attivo(self, monkeypatch):
        monkeypatch.setattr(rf, "_wallet", lambda: {
            "equity": 40.0, "available": 40.0, "exposure": 0.0, "error": None})
        monkeypatch.setattr(rf, "_exposure", lambda: {
            "open": 4.0, "orders": 1, "cap_pct": 0.70, "cap_usdc": 28.0,
            "free": 24.0, "utilization": 0.1429, "error": None})
        monkeypatch.setattr(rf, "_stale_stake", lambda now: {
            "stale": 0, "stale_stake": 0.0, "open_stake": 4.0, "open": 1,
            "error": None})
        monkeypatch.setattr(rf, "_resting", lambda: {
            "enabled": True, "open": 0, "open_stake": 0.0, "ttl_min": 45.0})
        monkeypatch.setattr(rf, "_tournament", lambda: {"active": True})
        rep = rf.report()
        assert rep["ok"] is True and rep["active"] is True
        assert rep["reason"] == "active"
        assert rep["budget"]["cap_usdc"] > 0
        assert rep["days_left"] > 0
        assert "recinto" in rep["note"]

    def test_wallet_illeggibile_fail_closed(self, monkeypatch):
        monkeypatch.setattr(rf, "_wallet", lambda: {
            "equity": None, "available": None, "exposure": None,
            "error": "RuntimeError: rete giu'"})
        monkeypatch.setattr(rf, "_exposure", lambda: {
            "open": 0.0, "orders": 0, "cap_pct": 0.7, "cap_usdc": None,
            "free": None, "utilization": None, "error": None})
        monkeypatch.setattr(rf, "_stale_stake", lambda now: {"open_stake": 0.0})
        monkeypatch.setattr(rf, "_resting", lambda: {"open": 0, "open_stake": 0.0})
        monkeypatch.setattr(rf, "_tournament", lambda: {})
        rep = rf.report()
        assert rep["ok"] is False
        assert rep["reason"] == "wallet_unreadable"
        assert "fail-closed" in rep["note"]

    def test_recinto_pieno(self, monkeypatch):
        monkeypatch.setattr(rf, "_wallet", lambda: {
            "equity": 40.0, "available": 40.0, "exposure": 28.0, "error": None})
        monkeypatch.setattr(rf, "_exposure", lambda: {
            "open": 28.0, "orders": 7, "cap_pct": 0.70, "cap_usdc": 28.0,
            "free": 0.0, "utilization": 1.0, "error": None})
        monkeypatch.setattr(rf, "_stale_stake", lambda now: {"open_stake": 28.0})
        monkeypatch.setattr(rf, "_resting", lambda: {"open": 0, "open_stake": 0.0})
        monkeypatch.setattr(rf, "_tournament", lambda: {})
        rep = rf.report()
        assert rep["ok"] is False and rep["reason"] == "enclosure_full"

    def test_budget_ignoto(self, monkeypatch):
        monkeypatch.setattr(rf, "_wallet", lambda: {
            "equity": 40.0, "available": 40.0, "exposure": 0.0, "error": None})
        monkeypatch.setattr(rf, "_exposure", lambda: {
            "open": 0.0, "orders": 0, "cap_pct": 0.7, "cap_usdc": None,
            "free": None, "utilization": None, "error": None})
        monkeypatch.setattr(rf, "_stale_stake", lambda now: {"open_stake": 0.0})
        monkeypatch.setattr(rf, "_resting", lambda: {"open": 0, "open_stake": 0.0})
        monkeypatch.setattr(rf, "_tournament", lambda: {})
        rep = rf.report()
        assert rep["ok"] is False and rep["reason"] == "budget_unknown"

    def test_format_report_dichiara_lo_stato(self, monkeypatch):
        monkeypatch.setenv("RUSH_FINAL", "0")
        text = rf.format_report()
        assert "RUSH FINALE" in text and "rush_off" in text
        attivo = rf.format_report(_rep())
        assert "ATTIVO" in attivo
        assert "guardrail intatti" in attivo


# ---------------------------------------------------------------------------
# 6. `apply` — il board
# ---------------------------------------------------------------------------

class TestApply:

    def test_board_vuoto(self):
        assert rf.apply([], rep=_rep()) == []

    def test_non_crea_pick(self):
        """Il rush non ha una discovery: senza candidati non produce nulla."""
        assert rf.apply(None, rep=_rep()) == []
        assert rf.apply([], rep=_rep()) == []

    def test_accelera_e_flaggia(self, cap_torneo):
        cap_torneo(5.0)
        cand = {"match_id": "sx-1", "esito_key": "1", "mercato": "1X2",
                "stake": 3.0}
        out = rf.apply([cand], rep=_rep())
        assert len(out) == 1
        assert out[0]["stake"] == pytest.approx(3.75)
        assert out[0][rf.RUSH_FLAG] is True
        assert out[0]["rush_info"]["reason"] == "rush"
        # Il candidato ORIGINALE non viene mutato: il chiamante resta padrone
        # della propria lista (nessun effetto a distanza sul board).
        assert cand["stake"] == 3.0
        assert rf.RUSH_FLAG not in cand

    def test_pick_non_accelerato_resta_invariato(self, cap_torneo):
        cap_torneo(0.5)
        cand = {"match_id": "sx-1", "esito_key": "1", "mercato": "1X2",
                "stake": 1.0}
        out = rf.apply([cand], rep=_rep())
        assert out[0]["stake"] == 1.0
        assert not out[0].get(rf.RUSH_FLAG)
        assert out[0]["rush_info"]["blocked"] is True

    def test_pick_malformato_non_solleva(self, cap_torneo):
        cap_torneo(5.0)
        board = [None, "stringa", [], {}, {"match_id": "sx-1", "stake": "abc"}]
        out = rf.apply(board, rep=_rep())
        assert len(out) == len(board)
        assert out[0] is None and out[1] == "stringa"
        assert out[4].get("stake") == "abc"           # invariato, non corretto

    def test_senza_rep_fuori_finestra_non_legge_il_wallet(self, monkeypatch):
        """Il gate a costo zero vale anche per un chiamante che non passa `rep`."""
        chiamate: list = []
        monkeypatch.setenv("RUSH_FINAL", "0")

        def _boom():
            chiamate.append(1)
            return {"equity": 40.0}

        monkeypatch.setattr(rf, "_wallet", _boom)
        out = rf.apply([{"match_id": "sx-1", "mercato": "1X2", "stake": 3.0}])
        assert chiamate == []
        assert out[0]["stake"] == 3.0

    def test_dentro_finestra_il_wallet_si_legge_una_volta(self, monkeypatch):
        chiamate: list = []
        monkeypatch.setattr(rf, "_wallet", lambda: (
            chiamate.append(1) or
            {"equity": 40.0, "available": 40.0, "exposure": 0.0, "error": None}))
        monkeypatch.setattr(rf, "_exposure", lambda: {
            "open": 0.0, "orders": 0, "cap_pct": 0.70, "cap_usdc": 28.0,
            "free": 24.0, "utilization": 0.0, "error": None})
        monkeypatch.setattr(rf, "_stale_stake", lambda now: {
            "stale": 0, "stale_stake": 0.0, "open_stake": 0.0, "open": 0,
            "error": None})
        monkeypatch.setattr(rf, "_resting", lambda: {
            "open": 0, "open_stake": 0.0, "ttl_min": 45.0})
        monkeypatch.setattr(rf, "_tournament", lambda: {})
        board = [{"match_id": "sx-1", "mercato": "1X2", "stake": 3.0},
                 {"match_id": "sx-2", "mercato": "1X2", "stake": 3.0}]
        out = rf.apply(board)
        assert len(chiamate) == 1        # un verdetto per il BOARD, non per pick
        assert all(p[rf.RUSH_FLAG] for p in out)


# ---------------------------------------------------------------------------
# 7. Ricircolo degli ordini RESTING
# ---------------------------------------------------------------------------

class TestRestingTTL:

    def test_fuori_finestra_ttl_invariato_senza_wallet(self, monkeypatch):
        chiamate: list = []
        monkeypatch.setenv("RUSH_FINAL", "0")
        monkeypatch.setattr(rf, "_wallet", lambda: chiamate.append(1) or {})
        assert rf.override_resting_ttl(720.0) == 720.0
        assert chiamate == []

    def test_in_finestra_accorcia(self, monkeypatch):
        monkeypatch.setattr(rf, "_wallet", lambda: {
            "equity": 40.0, "available": 40.0, "exposure": 0.0, "error": None})
        monkeypatch.setattr(rf, "_exposure", lambda: {
            "open": 0.0, "orders": 0, "cap_pct": 0.70, "cap_usdc": 28.0,
            "free": 24.0, "utilization": 0.0, "error": None})
        monkeypatch.setattr(rf, "_stale_stake", lambda now: {"open_stake": 0.0})
        monkeypatch.setattr(rf, "_resting", lambda: {"open": 0, "open_stake": 0.0})
        monkeypatch.setattr(rf, "_tournament", lambda: {})
        assert rf.override_resting_ttl(720.0) == 45.0

    def test_in_finestra_ma_bloccato_non_accorcia(self, monkeypatch):
        monkeypatch.setattr(rf, "_wallet", lambda: {
            "equity": None, "available": None, "exposure": None,
            "error": "ko"})
        monkeypatch.setattr(rf, "_exposure", lambda: {
            "open": 0.0, "orders": 0, "cap_pct": 0.7, "cap_usdc": None,
            "free": None, "utilization": None, "error": None})
        monkeypatch.setattr(rf, "_stale_stake", lambda now: {"open_stake": 0.0})
        monkeypatch.setattr(rf, "_resting", lambda: {"open": 0, "open_stake": 0.0})
        monkeypatch.setattr(rf, "_tournament", lambda: {})
        assert rf.override_resting_ttl(720.0) == 720.0

    def test_resting_orders_usa_lhook(self, monkeypatch):
        """Integrazione: `resting_orders.config()` passa dall'hook del rush."""
        import resting_orders as ro
        monkeypatch.setenv("RUSH_FINAL", "0")
        monkeypatch.setenv("RESTING_TTL_MIN", "720")
        assert ro.config()["ttl_min"] == 720.0       # fuori finestra: invariato
        monkeypatch.setenv("RUSH_FINAL", "1")
        monkeypatch.setattr(rf, "_wallet", lambda: {
            "equity": 40.0, "available": 40.0, "exposure": 0.0, "error": None})
        monkeypatch.setattr(rf, "_exposure", lambda: {
            "open": 0.0, "orders": 0, "cap_pct": 0.70, "cap_usdc": 28.0,
            "free": 24.0, "utilization": 0.0, "error": None})
        monkeypatch.setattr(rf, "_stale_stake", lambda now: {"open_stake": 0.0})
        monkeypatch.setattr(rf, "_resting", lambda: {"open": 0, "open_stake": 0.0})
        monkeypatch.setattr(rf, "_tournament", lambda: {})
        assert ro.config()["ttl_min"] == 45.0


class TestClearResting:

    @pytest.fixture(autouse=True)
    def _rep_ok(self, monkeypatch):
        """Verdetto ottimistico iniettato (il percorso del wallet si testa a parte)."""
        monkeypatch.setattr(rf, "_wallet", lambda: {
            "equity": 40.0, "available": 40.0, "exposure": 0.0, "error": None})
        monkeypatch.setattr(rf, "_exposure", lambda: {
            "open": 0.0, "orders": 0, "cap_pct": 0.70, "cap_usdc": 28.0,
            "free": 24.0, "utilization": 0.0, "error": None})
        monkeypatch.setattr(rf, "_stale_stake", lambda now: {
            "open": 0.0, "stale": 0, "stale_stake": 0.0, "open": 0})
        monkeypatch.setattr(rf, "_resting", lambda: {
            "open": 0, "open_stake": 0.0, "ttl_min": 45.0})
        monkeypatch.setattr(rf, "_tournament", lambda: {})

    def _seed(self, rows):
        import resting_orders as ro
        ro.save({"orders": rows, "updated_at": None})
        return ro

    def _row(self, *, order_id="0xabc", aged_min=120.0, status="open"):
        now = datetime.now(timezone.utc)
        return {"order_id": order_id, "market_id": "0xmkt", "match_id": "sx-1",
                "status": status, "stake": 2.0,
                "placed_at": _iso(now - timedelta(minutes=aged_min))}

    def test_fuori_finestra_non_cancella_nulla(self, monkeypatch):
        monkeypatch.setenv("RUSH_FINAL", "0")
        prov = _ProviderFinto()
        out = rf.clear_stale_resting(prov)
        assert out["reason"] == "rush_off"
        assert out["checked"] == 0 and out["cancel_requested"] == 0
        assert prov.calls == []

    def test_nessun_ordine_aperto(self):
        self._seed([])
        out = rf.clear_stale_resting(_ProviderFinto())
        assert out["reason"] == "active_no_orders"
        assert out["cancel_requested"] == 0

    def test_interruttore_del_ritiro(self, monkeypatch):
        monkeypatch.setenv("RUSH_CLEAR_RESTING", "0")
        self._seed([self._row()])
        prov = _ProviderFinto()
        out = rf.clear_stale_resting(prov)
        assert out["reason"] == "clear_disabled"
        assert prov.calls == []

    def test_ordine_vecchio_ritirato(self):
        ro = self._seed([self._row(aged_min=120.0)])
        prov = _ProviderFinto()
        out = rf.clear_stale_resting(prov)
        assert out["checked"] == 1
        assert out["cancel_requested"] == 1 and out["cancel_ok"] == 1
        assert prov.calls == [("0xmkt", "0xabc")]
        row = ro.load()["orders"][0]
        assert row["cancel_attempted"] is True
        assert row["cancel_ok"] is True
        assert row["cancel_reason"] == "rush_recycle"

    def test_ordine_fresco_non_toccato(self):
        self._seed([self._row(aged_min=5.0)])
        prov = _ProviderFinto()
        out = rf.clear_stale_resting(prov)
        assert out["cancel_requested"] == 0
        assert out["skipped"]["fresh"] == 1
        assert prov.calls == []

    def test_soglia_di_eta_configurabile(self):
        self._seed([self._row(aged_min=120.0)])
        prov = _ProviderFinto()
        out = rf.clear_stale_resting(prov, hours=5.0)
        assert out["cancel_requested"] == 0
        assert out["skipped"]["fresh"] == 1

    def test_solo_gli_ordini_aperti(self):
        """Un ordine gia' riempito/cancellato non e' capitale da riciclare."""
        ro = self._seed([self._row(order_id="0xfilled", status="filled")])
        prov = _ProviderFinto()
        out = rf.clear_stale_resting(prov)
        assert out["checked"] == 0
        assert out["reason"] == "active_no_orders"
        assert ro.load()["orders"][0].get("cancel_attempted") is None

    def test_provider_che_esplode_non_ferma_nulla(self):
        self._seed([self._row()])
        out = rf.clear_stale_resting(_ProviderFinto(raises=True))
        assert out["cancel_requested"] == 1 and out["cancel_ok"] == 0
        assert out["reason"] in (None, "")      # il giro si chiude comunque

    def test_provider_rifiuta_la_cancellazione(self):
        self._seed([self._row()])
        out = rf.clear_stale_resting(_ProviderFinto(ok=False))
        assert out["cancel_requested"] == 1 and out["cancel_ok"] == 0

    def test_registro_corrotto_non_cancella(self, monkeypatch, tmp_path):
        import resting_orders as ro
        path = os.getenv("RESTING_STATE")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{ non json")
        prov = _ProviderFinto()
        out = rf.clear_stale_resting(prov)
        assert prov.calls == []
        assert out["cancel_requested"] == 0
        assert ro.load().get("error")           # l'errore e' DICHIARATO

    def test_report_umano(self):
        self._seed([self._row()])
        out = rf.clear_stale_resting(_ProviderFinto())
        assert out["cancel_ok"] == 1

    def test_comando_dichiarato_nel_report(self):
        self._seed([])
        rep = rf.report()
        assert rep["resting"]["ttl_min"] >= 0
        assert "resting" in rf.format_report(rep)


# ---------------------------------------------------------------------------
# 8. Integrazione con il giro di produzione (wiring)
# ---------------------------------------------------------------------------

class TestIntegrazione:
    """Il rush deve stare nel giro ORDINI senza creare un canale parallelo."""

    def test_auto_bet_usa_il_precheck_prima_del_wallet(self, monkeypatch):
        """`_apply_rush` fuori finestra: board invariato e zero letture."""
        import auto_bet
        chiamate: list = []
        monkeypatch.setenv("RUSH_FINAL", "0")
        monkeypatch.setattr(rf, "_wallet", lambda: chiamate.append(1) or {})
        board = [{"match_id": "sx-1", "esito_key": "1", "mercato": "1X2",
                  "stake": 3.0}]
        out, info = auto_bet._apply_rush(board, bankroll=40.0)
        assert out[0]["stake"] == 3.0
        assert info["enabled"] is False
        assert info["reason"] == "rush_off"
        assert chiamate == []

    def test_auto_bet_accelera_dentro_la_finestra(self, monkeypatch, cap_torneo):
        import auto_bet
        cap_torneo(5.0)
        monkeypatch.setattr(rf, "report", lambda **kw: _rep())
        board = [{"match_id": "sx-1", "esito_key": "1", "mercato": "1X2",
                  "stake": 3.0}]
        out, info = auto_bet._apply_rush(board, bankroll=40.0)
        assert info["enabled"] is True and info["raised"] == 1
        assert out[0]["stake"] == pytest.approx(3.75)
        assert out[0][rf.RUSH_FLAG] is True

    def test_fail_safe_se_il_modulo_non_e_importabile(self, monkeypatch):
        import auto_bet
        monkeypatch.setitem(sys.modules, "rush_final", None)
        board = [{"match_id": "sx-1", "mercato": "1X2", "stake": 3.0}]
        out, info = auto_bet._apply_rush(board, bankroll=40.0)
        assert out[0]["stake"] == 3.0
        assert info["enabled"] is False

    def test_bot_espone_il_comando(self):
        """Sorgente: comando `/rush` registrato, in help e senza ordini."""
        with open("bot.py", "r", encoding="utf-8") as fh:
            src = fh.read()
        assert 'CommandHandler("rush", cmd_rush)' in src
        assert "async def cmd_rush(" in src
        assert "`/rush`" in src

    def test_bot_usa_il_ricircolo_nel_job_resting(self):
        with open("bot.py", "r", encoding="utf-8") as fh:
            src = fh.read()
        assert "rush_final.clear_stale_resting" in src

    def test_auto_bet_chiama_il_rush_nel_giro(self):
        with open("auto_bet.py", "r", encoding="utf-8") as fh:
            src = fh.read()
        assert "_apply_rush(candidates" in src
        # Una sola chiamata: il rush e' l'ULTIMO layer prima della FASE 3.
        assert src.count("_apply_rush(") == 2      # definizione + call site

    def test_resting_orders_configura_il_ttl_dal_rush(self):
        with open("resting_orders.py", "r", encoding="utf-8") as fh:
            src = fh.read()
        assert "rush_final.override_resting_ttl" in src

    def test_iac_dichiara_le_env_del_rush(self):
        with open(".railway/railway.ts", "r", encoding="utf-8") as fh:
            src = fh.read()
        for name in ("RUSH_FINAL", "RUSH_START", "RUSH_END", "RUSH_BUDGET_USDC",
                     "RUSH_MAX_STAKE_USDC", "RUSH_AGGRESSION", "RUSH_CLASSES",
                     "RUSH_RECYCLE_WINDOW_MIN", "RUSH_RECYCLE_BOOST",
                     "RUSH_RESTING_TTL_MIN", "RUSH_CLEAR_RESTING",
                     "RUSH_CLEAR_RESTING_AFTER_MIN"):
            assert f"{name}: preserve()" in src, name

    def test_ogni_env_letta_e_dichiarata_nella_iac(self):
        """Un env letto e non dichiarato viene DISTRUTTO da `config apply`."""
        import re
        with open(".railway/railway.ts", "r", encoding="utf-8") as fh:
            iac = fh.read()
        with open("rush_final.py", "r", encoding="utf-8") as fh:
            code = fh.read()
        names = set(re.findall(r'_num_env\(\s*"([A-Z0-9_]+)"', code))
        names |= set(re.findall(r'_flag\(\s*"([A-Z0-9_]+)"', code))
        names |= set(re.findall(r'os\.getenv\(\s*"([A-Z0-9_]+)"', code))
        assert names, "nessuna env rilevata: il tripwire non sta controllando nulla"
        mancanti = [n for n in sorted(names) if f"{n}: preserve()" not in iac]
        assert mancanti == []

    def test_conftest_isola_il_rush(self):
        """Senza isolamento, dal 17/10 ogni test entrerebbe nella finestra."""
        with open("conftest.py", "r", encoding="utf-8") as fh:
            src = fh.read()
        assert 'monkeypatch.setenv("RUSH_FINAL", "0")' in src
        assert '"test_rush_final" not in request.node.nodeid' in src

    def test_import_leggero(self):
        """`import rush_final` non carica la produzione (import in subprocess)."""
        code = ("import sys, rush_final; "
                "mods = [m for m in ('auto_bet', 'bot', 'tracker', 'execution_engine')"
                " if m in sys.modules]; print(mods)")
        res = subprocess.run([sys.executable, "-c", code], cwd=os.getcwd(),
                             capture_output=True, text=True, timeout=90)
        assert res.returncode == 0, res.stderr
        assert res.stdout.strip() == "[]", res.stdout

    def test_sorgente_senza_ordini_ne_scritture(self):
        """Il rush non piazza ordini e non scrive sul ledger: solo decisioni."""
        with open("rush_final.py", "r", encoding="utf-8") as fh:
            code = fh.read()
        for token in ("place_limit_order(", "save_bet(", "_live_fill(",
                      "save_prediction(", "resolve_market_for(", "import requests",
                      "httpx"):
            assert token not in code, token

    def test_nessuna_soglia_di_strategia_copiata(self):
        """EV, fascia quota e cap di strategia restano nei loro moduli."""
        with open("rush_final.py", "r", encoding="utf-8") as fh:
            code = fh.read()
        for token in ("ODDS_MIN", "ODDS_MAX", "EV_MIN", "MARKET_EDGE_MIN",
                      "kelly", "Kelly"):
            assert token not in code, token

    def test_flag_esposto(self):
        assert rf.RUSH_FLAG == "rush_final"
