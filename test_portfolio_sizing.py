"""Test di `portfolio_sizing.py` — promozione dei gate M6 Fase 3+4 (16/10/2026).

TUTTI OFFLINE: nessuna rete, nessun credito, nessun ordine. I due moduli di
rischio (`risk_parity`, `drawdown_rebalancer`) vengono SOSTITUITI con versioni
deterministiche: qui si misura il **wiring** (chi riduce cosa, quando e con
quale modo), non la matematica dei due moduli — che ha gia' la sua suite
(`test_risk_parity.py`, `test_drawdown_rebalancer.py`).

Cosa deve essere vero perche' la promozione sia sicura:
1. **shadow = nessun effetto**: in modo `shadow` (default) gli stake NON
   cambiano, gli oggetti veri non ricevono nemmeno un campo, e il log dichiara
   che la riduzione NON e' applicata;
2. **enforce = applica, mai amplifica**: la riduzione e' scritta sul candidato,
   lo stake non supera mai quello di partenza (difesa in profondita' contro un
   modulo che tornasse un moltiplicatore > 1) e cio' che scende sotto il ticket
   minimo del MOTORE viene scartato (fail-closed);
3. **parita' shadow/enforce**: le due modalita' calcolano le STESSE riduzioni
   (cambia solo se vengono scritte) — e' la proprieta' che rende la shadow una
   misura e non un'altra cosa;
4. **reversibilita' per env**: `PORTFOLIO_SIZING=off` e' una variabile e nient'altro.
"""
from __future__ import annotations

import ast
import math
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

import portfolio_sizing as ps

ROOT = Path(__file__).resolve().parent


def _code_only(path) -> str:
    """Solo il CODICE (le docstring nominano i confini: la prosa non e' un tripwire)."""
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and \
                    isinstance(body[0].value, ast.Constant) and \
                    isinstance(body[0].value.value, str):
                node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def _cand(match_id="sx-1", mercato="1X2", stake=4.0, esito="1"):
    return {"match_id": match_id, "mercato": mercato, "esito_key": esito,
            "stake": stake, "quota": 1.7, "league": "Premier League"}


def _floor2(x: float) -> float:
    return math.floor(x * 100.0) / 100.0


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Modo esplicito + memorie dei due moduli azzerate (stato di MODULO)."""
    monkeypatch.delenv("PORTFOLIO_SIZING", raising=False)
    ps.reset_caches()
    # Nessun accesso al ledger REALE: si sostituisce il LETTORE
    # (`auto_bet._open_live_rows`, l'unica fonte di "cosa e' aperto"), non
    # `read_open_positions`: cosi' la classe `TestPosizioniAperte` esercita la
    # funzione vera su righe iniettate (sostituendo lei il lettore) invece di
    # misurare se stessa.
    import auto_bet
    monkeypatch.setattr(auto_bet, "_open_live_rows", lambda: [])
    yield
    ps.reset_caches()


def _fake_dd(monkeypatch, *, multiplier=0.60, block=False, boom=False,
             strip_marker=False):
    """Sostituisce `drawdown_rebalancer.apply` con una curva deterministica.

    `strip_marker=True` simula un modulo che RICREA le voci (collaudo
    dell'allineamento perso): le copie tornano senza la chiave privata che le
    lega al candidato d'origine.
    """
    import drawdown_rebalancer as dd

    def _apply(candidates, *, bankroll=None, cfg=None, state=None, now=None):
        if boom:
            raise RuntimeError("storico corrotto")
        info = {"enabled": True, "active": True, "mode": "enforce",
                "kept": len(candidates), "scaled": 0, "blocked": 0,
                "drawdown_pct": 15.0, "multiplier": multiplier,
                "factor_legacy": 0.5, "reason": "interpolato",
                "min_multiplier": 0.10, "min_ticket": 1.0, "tier": 0.15,
                "by_reason": {}, "blocked_detail": [], "error": None}
        out = []
        for cand in candidates:
            if strip_marker:
                item = {k: v for k, v in cand.items()
                        if k != ps._PSIZE_IDX}
            else:
                item = cand
            before = float(item.get("stake") or 0.0)
            if before <= 0:
                out.append(item)
                continue
            after = 0.0 if block else _floor2(before * multiplier)
            item["stake"] = after
            item["drawdown_rebalance"] = {
                "decision": "blocked" if after <= 0 else "scaled",
                "multiplier": multiplier, "stake_before": before,
                "stake_after": after, "detail": "curva di test"}
            info["blocked" if after <= 0 else "scaled"] += 1
            out.append(item)
        return out, info

    monkeypatch.setattr(dd, "apply", _apply)


def _fake_rp(monkeypatch, *, multiplier=None, boom=False, fresh=False,
             seen=None):
    """Sostituisce `risk_parity.apply` (default: nessuna riduzione, 'clear')."""
    import risk_parity as rp

    def _apply(candidates, *, bankroll=0.0, open_positions=None, cfg=None,
               snapshot=None):
        if boom:
            raise RuntimeError("varianza illeggibile")
        if seen is not None:
            seen["stakes_in"] = [float(c.get("stake") or 0.0) for c in candidates]
            seen["open_positions"] = list(open_positions or [])
            seen["bankroll"] = bankroll
        info = {"mode": "enforce", "kept": len(candidates), "scaled": 0,
                "blocked": 0, "clear": 0, "skipped_no_sample": 0,
                "by_reason": {}, "error": None, "max_share_ratio": 1.25,
                "min_multiplier": 0.25}
        out = []
        for cand in candidates:
            item = dict(cand) if fresh else cand
            before = float(item.get("stake") or 0.0)
            if multiplier is None or before <= 0:
                info["clear"] += 1
                out.append(item)
                continue
            after = _floor2(before * multiplier)
            item["stake"] = after
            item["risk_parity"] = {"decision": "scaled", "multiplier": multiplier,
                                   "stake_before": before, "stake_after": after,
                                   "detail": "griglia di test"}
            info["scaled"] += 1
            out.append(item)
        return out, info

    monkeypatch.setattr(rp, "apply", _apply)


# ---------------------------------------------------------------------------
# 1. Configurazione (i tre modi e la reversibilita')
# ---------------------------------------------------------------------------
class TestConfig:
    def test_default_shadow(self):
        """'Shadow per primo': di default i gate misurano e NON applicano."""
        assert ps.mode() == "shadow" and ps.DEFAULT_MODE == "shadow"
        assert ps.enabled() is True and ps.applies() is False
        assert ps.config()["shadow"] is True

    def test_enforce(self, monkeypatch):
        monkeypatch.setenv("PORTFOLIO_SIZING", "enforce")
        assert ps.applies() is True and ps.enabled() is True

    def test_off_e_reversibile(self, monkeypatch):
        monkeypatch.setenv("PORTFOLIO_SIZING", "off")
        assert ps.enabled() is False and ps.mode() == "off"

    def test_valore_ignoto_ricade_sul_default(self, monkeypatch, caplog):
        monkeypatch.setenv("PORTFOLIO_SIZING", "yes-please")
        with caplog.at_level("WARNING"):
            assert ps.mode() == "shadow"
        assert any("modo sconosciuto" in r.message for r in caplog.records)

    def test_maiuscole_e_spazi(self, monkeypatch):
        monkeypatch.setenv("PORTFOLIO_SIZING", "  ENFORCE ")
        assert ps.mode() == "enforce"

    def test_modi_dichiarati(self):
        assert ps.MODES == ("off", "shadow", "enforce")

    def test_report_dichiara_la_shadow(self, monkeypatch):
        text = ps.format_report()
        assert "SHADOW" in text and "PORTFOLIO_SIZING" in text
        monkeypatch.setenv("PORTFOLIO_SIZING", "enforce")
        assert "SHADOW" not in ps.format_report()

    def test_report_dichiara_il_ticket(self, monkeypatch):
        monkeypatch.setattr(ps, "min_ticket", lambda: 1.0)
        assert "1.00" in ps.format_report()
        monkeypatch.setattr(ps, "min_ticket", lambda: None)
        assert "n/d" in ps.format_report()


# ---------------------------------------------------------------------------
# 2. Il modo OFF: nessuna valutazione, nessun import, nessuna lettura
# ---------------------------------------------------------------------------
class TestModoOff:
    def test_non_tocca_nulli(self, monkeypatch):
        monkeypatch.setenv("PORTFOLIO_SIZING", "off")
        cands = [_cand(stake=4.0)]
        kept, info = ps.apply(cands, bankroll=50.0)
        assert kept[0]["stake"] == 4.0 and info["kept"] == 1
        assert info["enabled"] is False and info["mode"] == "off"

    def test_non_interroga_i_moduli(self, monkeypatch):
        """Spento = i due moduli non devono nemmeno essere chiamati."""
        monkeypatch.setenv("PORTFOLIO_SIZING", "off")
        called = {"n": 0}

        def _boom(*a, **k):
            called["n"] += 1
            raise AssertionError("non deve essere chiamato")

        _fake_dd(monkeypatch, boom=True)
        import drawdown_rebalancer as dd
        monkeypatch.setattr(dd, "apply", _boom)
        kept, info = ps.apply([_cand()], bankroll=50.0)
        assert called["n"] == 0 and kept[0]["stake"] == 4.0


# ---------------------------------------------------------------------------
# 3. SHADOW: calcola, logga, NON applica
# ---------------------------------------------------------------------------
class TestShadow:
    def test_stake_invariato(self, monkeypatch):
        _fake_dd(monkeypatch, multiplier=0.50)
        _fake_rp(monkeypatch)
        cands = [_cand(match_id="sx-a", stake=4.0)]
        kept, info = ps.apply(cands, bankroll=50.0)
        assert kept[0]["stake"] == 4.0
        assert info["applied"] is False and info["mode"] == "shadow"

    def test_nessun_campo_scritto_sull_oggetto_vero(self, monkeypatch):
        """Nemmeno un campo: un `risk_parity` sui candidati veri verrebbe letto
        a valle come se il gate fosse stato applicato."""
        _fake_dd(monkeypatch, multiplier=0.50)
        _fake_rp(monkeypatch, multiplier=0.80)
        cands = [_cand(stake=4.0)]
        before = set(cands[0].keys())
        ps.apply(cands, bankroll=50.0)
        assert set(cands[0].keys()) == before

    def test_dichiara_cosa_avrebbe_fatto(self, monkeypatch):
        """`stake_after` e' il valore FINALE (cio' che l'ordine userebbe); i
        passi intermedi restano leggibili in `steps` (in shadow i verbi dei
        moduli non vengono scritti sui candidati veri: senza `steps` la
        diagnosi di CHI ha tagliato quanto andrebbe persa)."""
        _fake_dd(monkeypatch, multiplier=0.50)
        _fake_rp(monkeypatch, multiplier=0.80)
        kept, info = ps.apply([_cand(match_id="sx-a", stake=4.0)], bankroll=50.0)
        assert len(info["reductions"]) == 1
        row = info["reductions"][0]
        assert row["match_id"] == "sx-a" and row["stake_before"] == 4.0
        assert row["stake_after"] == 1.60       # 4 -> 2 (dd) -> 1.60 (rp)
        assert "drawdown" in row["source"] and "risk_parity" in row["source"]
        assert row["steps"] == [{"gate": "drawdown", "stake_after": 2.0,
                                 "multiplier": 0.50},
                                {"gate": "risk_parity", "stake_after": 1.60,
                                 "multiplier": 0.80}]

    def test_blocco_riportato_ma_non_applicato(self, monkeypatch):
        _fake_dd(monkeypatch, block=True)
        _fake_rp(monkeypatch)
        cands = [_cand(stake=4.0)]
        kept, info = ps.apply(cands, bankroll=50.0)
        assert len(info["blocks"]) == 1 and info["blocks"][0]["reason"] == "blocked_by_gate"
        assert kept[0]["stake"] == 4.0 and info["dropped"] == 0

    def test_log_dichiara_che_non_e_applicato(self, monkeypatch, caplog):
        _fake_dd(monkeypatch, multiplier=0.50)
        _fake_rp(monkeypatch)
        with caplog.at_level("INFO"):
            ps.apply([_cand(stake=4.0)], bankroll=50.0)
        text = "\n".join(r.getMessage() for r in caplog.records)
        assert "[SHADOW]" in text and "NON applicato" in text

    def test_giro_in_salute_non_logga_a_info(self, monkeypatch, caplog):
        _fake_dd(monkeypatch, multiplier=1.0)
        _fake_rp(monkeypatch)
        with caplog.at_level("INFO"):
            kept, info = ps.apply([_cand(stake=4.0)], bankroll=50.0)
        assert kept[0]["stake"] == 4.0 and info["reductions"] == []
        assert not [r for r in caplog.records if "portfolio_sizing" in r.name]


# ---------------------------------------------------------------------------
# 4. ENFORCE: applica, mai amplifica, blocco sotto ticket
# ---------------------------------------------------------------------------
class TestEnforce:
    def _enforce(self, monkeypatch):
        monkeypatch.setenv("PORTFOLIO_SIZING", "enforce")

    def test_riduzione_scritta(self, monkeypatch):
        self._enforce(monkeypatch)
        _fake_dd(monkeypatch, multiplier=0.50)
        _fake_rp(monkeypatch)
        cands = [_cand(stake=4.0)]
        kept, info = ps.apply(cands, bankroll=50.0)
        assert cands[0]["stake"] == 2.0 and kept[0]["stake"] == 2.0
        assert info["applied"] is True

    def test_il_verdetto_dei_moduli_e_conservato(self, monkeypatch):
        self._enforce(monkeypatch)
        _fake_dd(monkeypatch, multiplier=0.50)
        _fake_rp(monkeypatch)
        kept, _ = ps.apply([_cand(stake=4.0)], bankroll=50.0)
        assert kept[0]["drawdown_rebalance"]["decision"] == "scaled"
        assert kept[0]["drawdown_rebalance"]["stake_after"] == 2.0

    def test_blocco_scarta_il_candidato(self, monkeypatch):
        """Uno stake a 0 non deve arrivare al percorso d'ordine: li' non c'e'
        nessuna guardia sul sotto-ticket e `_live_fill` costruirebbe un ordine."""
        self._enforce(monkeypatch)
        _fake_dd(monkeypatch, block=True)
        _fake_rp(monkeypatch)
        kept, info = ps.apply([_cand(stake=4.0), _cand(match_id="sx-b", stake=4.0)],
                              bankroll=50.0)
        assert info["dropped"] == 2 and kept == [] and info["kept"] == 0

    def test_sotto_il_ticket_viene_scartato(self, monkeypatch):
        self._enforce(monkeypatch)
        monkeypatch.setattr(ps, "min_ticket", lambda: 1.00)
        _fake_dd(monkeypatch, multiplier=0.60)
        _fake_rp(monkeypatch)
        cands = [_cand(stake=1.50)]              # 1.50 x 0.60 = 0.90 < 1.00
        kept, info = ps.apply(cands, bankroll=50.0)
        assert kept == [] and info["dropped"] == 1
        assert info["blocks"][0]["reason"] == "below_ticket_after_reduction"

    def test_sopra_il_ticket_resta(self, monkeypatch):
        self._enforce(monkeypatch)
        monkeypatch.setattr(ps, "min_ticket", lambda: 1.00)
        _fake_dd(monkeypatch, multiplier=0.60)
        _fake_rp(monkeypatch)
        kept, info = ps.apply([_cand(stake=4.0)], bankroll=50.0)
        assert kept[0]["stake"] == 2.40 and info["blocks"] == []

    def test_ticket_illeggibile_non_blocca(self, monkeypatch, caplog):
        """Fail-open dichiarato: senza ticket non si inventa una soglia."""
        self._enforce(monkeypatch)
        monkeypatch.setattr(ps, "min_ticket", lambda: None)
        _fake_dd(monkeypatch, multiplier=0.20)
        _fake_rp(monkeypatch)
        with caplog.at_level("WARNING"):
            kept, info = ps.apply([_cand(stake=1.50)], bankroll=50.0)
        assert kept[0]["stake"] == 0.30 and info["blocks"] == []
        assert any("ticket minimo non leggibile" in r.message
                   for r in caplog.records)

    def test_mai_amplificato(self, monkeypatch):
        """Difesa in profondita': un modulo che ALZASSE lo stake viene riportato
        indietro e l'evento e' contato (mai un aumento silenzioso)."""
        self._enforce(monkeypatch)
        _fake_dd(monkeypatch, multiplier=3.0)          # fuori contratto
        _fake_rp(monkeypatch)
        cands = [_cand(stake=4.0)]
        kept, info = ps.apply(cands, bankroll=50.0)
        assert kept[0]["stake"] == 4.0 and info["clamped"] == 1
        assert info["reductions"] == []

    def test_catena_drawdown_poi_risk_parity(self, monkeypatch):
        """L'ordine dei due gate non e' arbitrario: prima la salute del capitale,
        poi la distribuzione fra bucket (sugli importi sopravvissuti)."""
        self._enforce(monkeypatch)
        _fake_dd(monkeypatch, multiplier=0.50)
        seen = {}
        _fake_rp(monkeypatch, multiplier=0.50, seen=seen)
        kept, _ = ps.apply([_cand(stake=8.0)], bankroll=50.0)
        assert seen["stakes_in"] == [4.0]      # il risk parity vede 8 x 0.50
        assert kept[0]["stake"] == 2.0         # poi 4 x 0.50

    def test_le_posizioni_aperte_arrivano_al_risk_parity(self, monkeypatch):
        self._enforce(monkeypatch)
        _fake_dd(monkeypatch, multiplier=1.0)
        seen = {}
        _fake_rp(monkeypatch, seen=seen)
        pos = [{"match_id": "sx-open", "mercato": "1X2", "stake": 3.0}]
        _kept, info = ps.apply([_cand(stake=4.0)], bankroll=42.0,
                               open_positions=pos)
        assert seen["open_positions"] == pos and seen["bankroll"] == 42.0
        assert info["open_positions"] == 1

    def test_stake_illeggibile_non_riduce(self, monkeypatch):
        self._enforce(monkeypatch)
        _fake_dd(monkeypatch, multiplier=0.50)
        _fake_rp(monkeypatch)
        kept, info = ps.apply([{"match_id": "sx-x", "mercato": "1X2",
                               "esito_key": "1", "stake": "abc"}], bankroll=50.0)
        assert info["reductions"] == [] and info["blocks"] == []
        assert len(kept) == 1

    def test_stake_zero_passa_invariato(self, monkeypatch):
        self._enforce(monkeypatch)
        _fake_dd(monkeypatch, multiplier=0.50)
        _fake_rp(monkeypatch)
        kept, info = ps.apply([_cand(stake=0.0)], bankroll=50.0)
        assert kept[0]["stake"] == 0.0 and info["blocks"] == []


# ---------------------------------------------------------------------------
# 5. Parita' shadow/enforce (la proprieta' che rende la shadow una misura)
# ---------------------------------------------------------------------------
class TestParita:
    @pytest.mark.parametrize("mult", [0.10, 0.25, 0.50, 0.75, 0.99, 1.0])
    def test_stesse_riduzioni_nei_due_modi(self, monkeypatch, mult):
        cands = [_cand(match_id="sx-%s" % mult, stake=6.0)]
        _fake_dd(monkeypatch, multiplier=mult)
        _fake_rp(monkeypatch)
        shadow_cands, shadow = ps.apply([dict(c) for c in cands], bankroll=50.0)
        monkeypatch.setenv("PORTFOLIO_SIZING", "enforce")
        _fake_dd(monkeypatch, multiplier=mult)
        _fake_rp(monkeypatch)
        enforced, enforce = ps.apply([dict(c) for c in cands], bankroll=50.0)
        assert shadow["reductions"] == enforce["reductions"]
        assert [c["stake"] for c in shadow_cands] == [6.0]
        assert [c["stake"] for c in enforced] == \
            [r["stake_after"] for r in enforce["reductions"]] or not enforce["reductions"]

    def test_blocchi_uguali_nei_due_modi(self, monkeypatch):
        _fake_dd(monkeypatch, block=True)
        _fake_rp(monkeypatch)
        monkeypatch.setattr(ps, "min_ticket", lambda: None)
        _s_cands, shadow = ps.apply([_cand(stake=4.0)], bankroll=50.0)
        monkeypatch.setenv("PORTFOLIO_SIZING", "enforce")
        _fake_dd(monkeypatch, block=True)
        _fake_rp(monkeypatch)
        _e_cands, enforce = ps.apply([_cand(stake=4.0)], bankroll=50.0)
        assert shadow["blocks"] == enforce["blocks"]

    def test_info_confrontabile(self, monkeypatch):
        _fake_dd(monkeypatch, multiplier=0.50)
        _fake_rp(monkeypatch, multiplier=0.90)
        _c, info = ps.apply([_cand(stake=4.0)], bankroll=50.0)
        for key in ("mode", "applied", "passed", "kept", "reductions", "blocks",
                    "clamped", "dropped", "drawdown", "risk_parity", "ticket",
                    "error", "open_positions"):
            assert key in info, key
        assert info["drawdown"]["multiplier"] == 0.50
        assert info["risk_parity"]["scaled"] == 1


# ---------------------------------------------------------------------------
# 6. Fail-open (un guasto di telemetria non e' un rischio misurato)
# ---------------------------------------------------------------------------
class TestFailOpen:
    def test_drawdown_che_esplode(self, monkeypatch):
        _fake_dd(monkeypatch, boom=True)
        _fake_rp(monkeypatch)
        cands = [_cand(stake=4.0)]
        kept, info = ps.apply(cands, bankroll=50.0)
        assert kept[0]["stake"] == 4.0 and info["error"] == "errore_valutazione"

    def test_risk_parity_che_esplode(self, monkeypatch):
        _fake_dd(monkeypatch, multiplier=0.50)
        _fake_rp(monkeypatch, boom=True)
        kept, info = ps.apply([_cand(stake=4.0)], bankroll=50.0)
        assert kept[0]["stake"] == 4.0 and info["error"] == "errore_valutazione"

    def test_allineamento_perso(self, monkeypatch):
        """Se le copie di lavoro non sono piu' riconoscibili non si applica
        NULLA: meglio nessuna riduzione che la riduzione del candidato sbagliato."""
        _fake_dd(monkeypatch, multiplier=0.50, strip_marker=True)
        _fake_rp(monkeypatch)
        cands = [_cand(stake=4.0)]
        kept, info = ps.apply(cands, bankroll=50.0)
        assert kept[0]["stake"] == 4.0 and info["error"] == "allineamento_perso"

    def test_candidato_non_valido(self, monkeypatch):
        _fake_dd(monkeypatch, multiplier=0.50)
        _fake_rp(monkeypatch)
        kept, info = ps.apply(["non-un-dict"], bankroll=50.0)
        assert info["error"] == "candidato_non_valido" and kept == ["non-un-dict"]

    def test_chiave_privata_non_arriva_ai_candidati(self, monkeypatch):
        _fake_dd(monkeypatch, multiplier=0.50)
        _fake_rp(monkeypatch)
        monkeypatch.setenv("PORTFOLIO_SIZING", "enforce")
        cands = [_cand(stake=4.0)]
        kept, _ = ps.apply(cands, bankroll=50.0)
        assert ps._PSIZE_IDX not in kept[0]
        assert ps._PSIZE_IDX not in cands[0]

    def test_lista_vuota(self, monkeypatch):
        _fake_dd(monkeypatch)
        _fake_rp(monkeypatch)
        kept, info = ps.apply([], bankroll=50.0)
        assert kept == [] and info["passed"] == 0


# ---------------------------------------------------------------------------
# 7. Posizioni aperte (il risk parity e' una proprieta' del portafoglio)
# ---------------------------------------------------------------------------
class TestPosizioniAperte:
    def test_mappa_le_righe_del_ledger(self, monkeypatch):
        import auto_bet
        monkeypatch.setattr(auto_bet, "_open_live_rows", lambda: [
            {"match_id": "sx-1", "esito": "1", "stake": 3.0, "mercato": "1X2"},
            {"match_id": "sx-tennis-2", "esito": "2", "stake": 2.0,
             "mercato": "TENNIS"}])
        rows = ps.read_open_positions()
        assert rows == [{"match_id": "sx-1", "mercato": "1X2", "stake": 3.0},
                        {"match_id": "sx-tennis-2", "mercato": "TENNIS",
                         "stake": 2.0}]

    def test_salta_stake_nullo_e_righe_ostili(self, monkeypatch):
        import auto_bet
        monkeypatch.setattr(auto_bet, "_open_live_rows", lambda: [
            {"match_id": "a", "stake": 0.0, "mercato": "1X2"},
            {"match_id": "b", "stake": "abc", "mercato": "1X2"},
            "riga-non-dict",
            {"match_id": "c", "stake": 1.5}])
        rows = ps.read_open_positions()
        assert rows == [{"match_id": "c", "mercato": "", "stake": 1.5}]

    def test_lettore_che_esplode(self, monkeypatch):
        def _boom():
            raise RuntimeError("db rotto")
        assert ps.read_open_positions(reader=_boom) == []

    def test_lettore_iniettato(self):
        rows = ps.read_open_positions(reader=lambda: [
            {"match_id": "z", "stake": 4.0, "mercato": "OU"}])
        assert rows[0]["mercato"] == "OU"

    def test_il_gate_usa_il_lettore_vero_quando_non_gliene_si_passa_uno(
            self, monkeypatch):
        """`apply` senza `open_positions` deve passare dal lettore REALE
        (patched qui solo per non leggere il ledger): se una modifica futura
        iniettasse una lista vuota di default, il risk parity non vedrebbe mai
        il capitale gia' impegnato."""
        monkeypatch.setenv("PORTFOLIO_SIZING", "enforce")
        import auto_bet
        monkeypatch.setattr(auto_bet, "_open_live_rows", lambda: [
            {"match_id": "sx-open", "stake": 3.0, "mercato": "1X2"}])
        _fake_dd(monkeypatch, multiplier=1.0)
        seen = {}
        _fake_rp(monkeypatch, seen=seen)
        _kept, info = ps.apply([_cand(stake=4.0)], bankroll=42.0)
        assert seen["open_positions"] == [{"match_id": "sx-open",
                                           "mercato": "1X2", "stake": 3.0}]
        assert info["open_positions"] == 1

    def test_usa_open_live_rows_come_unica_fonte(self):
        """Una seconda definizione di 'cosa e' aperto' divergerebbe."""
        src = _code_only(ps.__file__)
        assert "_open_live_rows" in src
        assert "esito_finale IS NULL" not in src


# ---------------------------------------------------------------------------
# 8. Cablaggio in auto_bet (ordine dei gate + percorso T-60)
# ---------------------------------------------------------------------------
class TestCablaggio:
    def _src(self) -> str:
        return (ROOT / "auto_bet.py").read_text(encoding="utf-8")

    def test_ordine_dei_gate_nella_fase_2(self):
        """tetto per-ordine -> [M6] -> copula: nessuno rialza cio' che i due gate
        riducono, e il gate di dipendenza resta l'ULTIMO passo di sizing."""
        src = self._src()
        ceiling = src.index("candidates, _ceiling = enforce_order_ceiling(")
        psize = src.index("candidates, _psize = _apply_portfolio_sizing(")
        copula = src.index("candidates, _cop = _apply_copula(")
        assert ceiling < psize < copula

    def test_anche_il_percorso_t60_applica_i_gate(self):
        src = self._src()
        psize = src.index("picks, _psize = _apply_portfolio_sizing(")
        copula = src.index("picks, _cop = _apply_copula(")
        assert psize < copula

    def test_wrapper_delega_e_non_ricopia(self):
        src = _code_only(ROOT / "auto_bet.py")
        assert "import portfolio_sizing" in src
        # Nessuna soglia dei due moduli ricopiata in auto_bet
        for token in ("RISK_PARITY_", "DRAWDOWN_REBALANCE", "max_share_ratio",
                      "min_multiplier"):
            assert token not in src, token

    def test_open_live_rows_espone_il_mercato(self):
        """Il bucket del risk parity si costruisce su (mercato, sport)."""
        src = self._src()
        assert "SELECT match_id, esito, stake, price, created_at, mercato" in src

    def test_sorgente_senza_scritture_ne_ordini(self):
        src = _code_only(ps.__file__)
        for token in ("INSERT", "UPDATE ", "DELETE FROM", "save_bet(",
                      "save_prediction(", "place_limit_order", "_live_fill",
                      "execution_engine", "import requests", "httpx",
                      "sqlite3", "odds_api"):
            assert token not in src, token

    def test_import_leggero(self):
        code = ("import sys, portfolio_sizing;"
                "print(any(m in sys.modules for m in "
                "('tracker','auto_bet','bot','odds_api','risk_parity',"
                "'drawdown_rebalancer')))")
        out = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT),
                             capture_output=True, text=True, timeout=120)
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == "False"

    def test_nessuna_soglia_negli_agenti(self):
        """Gli agenti non devono duplicare queste soglie (tripwire del recinto)."""
        for name in ("analysis_agent", "brain_agent", "finance_agent",
                     "execution_agent", "data_agent", "advisor_agent"):
            src = (ROOT / "agents" / ("%s.py" % name)).read_text(encoding="utf-8")
            for token in ("PORTFOLIO_SIZING", "RISK_PARITY_", "DRAWDOWN_REBALANCE"):
                assert token not in src, (name, token)


# ---------------------------------------------------------------------------
# 9. IaC (un `config apply` non deve distruggere gli interruttori)
# ---------------------------------------------------------------------------
class TestIaC:
    def _text(self) -> str:
        return (ROOT / ".railway" / "railway.ts").read_text(encoding="utf-8")

    def test_env_della_promozione_dichiarate(self):
        text = self._text()
        for name in ("PORTFOLIO_SIZING", "RISK_PARITY_MODE", "RISK_PARITY_GUARD",
                     "RISK_PARITY_MAX_SHARE_RATIO", "RISK_PARITY_MIN_MULTIPLIER",
                     "RISK_PARITY_MIN_SAMPLE", "RISK_PARITY_DAYS",
                     "RISK_PARITY_MIN_DAYS", "RISK_PARITY_GRID",
                     "RISK_PARITY_TTL_S", "DRAWDOWN_REBALANCE",
                     "DRAWDOWN_REBALANCE_MODE", "DRAWDOWN_REBALANCE_TIERS",
                     "DRAWDOWN_REBALANCE_MIN_MULTIPLIER",
                     "DRAWDOWN_REBALANCE_MIN_TICKET_USDC",
                     "DRAWDOWN_REBALANCE_TTL_S"):
            assert re.search(r"%s\s*:\s*preserve\(\)" % name, text), name

    def test_ogni_env_letta_dai_moduli_e_dichiarata(self):
        """Nessuna variabile letta dai tre moduli puo' mancare dalla IaC.

        I due moduli di rischio leggono la maggior parte delle variabili via
        helper (`_env_bool`/`_env_float`/`_env_int`): un tripwire che guardasse
        solo `os.getenv("...")` ne controllerebbe TRE su diciotto e passerebbe
        mentre le altre quindici non sono dichiarate (e un `config apply` le
        cancellerebbe).
        """
        text = self._text()
        pattern = (r'(?:os\.getenv|_env_bool|_env_float|_env_int|_env_str)'
                   r'\(\s*"([A-Z0-9_]+)"')
        # `portfolio_sizing` legge il SUO interruttore da una costante
        # (`MODE_ENV`): senza questo secondo pattern il file non dichiarerebbe
        # nessuna variabile e il tripwire resterebbe muto proprio sull'env
        # della promozione.
        named = re.compile(r'^[A-Z_]*ENV(?:\s*:\s*\w+)?\s*=\s*"([A-Z0-9_]+)"',
                           re.M)
        names = {"PORTFOLIO_SIZING"}
        for path in (ps.__file__, ROOT / "risk_parity.py",
                     ROOT / "drawdown_rebalancer.py"):
            src = Path(path).read_text(encoding="utf-8")
            found = set(re.findall(pattern, src)) | set(named.findall(src))
            assert found, "nessuna env trovata in %s" % path
            names |= found
        assert len(names) >= 15, sorted(names)
        missing = [n for n in sorted(names)
                   if not re.search(r"%s\s*:\s*preserve\(\)" % n, text)]
        assert missing == [], missing


# ---------------------------------------------------------------------------
# 10. End-to-end nel giro REALE (`auto_bet.run_today_bets`)
# ---------------------------------------------------------------------------
@pytest.fixture()
def temp_db(monkeypatch):
    import tracker
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        monkeypatch.setattr(tracker, "DB_PATH", db_path)
        tracker.init_db()
        yield db_path


class TestGiroReale:
    """Il gate nel giro di denaro: shadow invariato, enforce ridotto.

    Configurazione: stake FISSO legacy (1.50 USDC via `FIXED_STAKE_USDC`), che
    e' l'unica in cui lo stake che ARRIVA al gate e' deterministico — il
    motore Kelly e' isolato dal `conftest` (i test dello staking hanno il loro
    file) — e sufficiente a non farsi scartare dal CAP SEVERO
    (`STAKE_CAP_HARD`, che in produzione e' attivo) prima del gate.
    """

    BASE = 1.50                    # = `FIXED_STAKE_USDC` di questo test
    MUL = 0.80                     # 1.50 x 0.80 = 1.20 >= ticket (1.00)

    def _run(self, monkeypatch, mode, *, multiplier=None):
        import auto_bet
        from test_auto_bet_live import _filled, _stub_wallet
        from test_capital_enclosure import _seed_one
        monkeypatch.setenv("PORTFOLIO_SIZING", mode)
        monkeypatch.setattr(auto_bet, "FIXED_STAKE_USDC", self.BASE)
        _fake_dd(monkeypatch, multiplier=self.MUL if multiplier is None
                 else multiplier)
        _fake_rp(monkeypatch)
        _seed_one(monkeypatch)
        _stub_wallet(monkeypatch, 60.0)
        monkeypatch.setattr(auto_bet, "T60_EXECUTION_ONLY", False)
        monkeypatch.setattr(auto_bet, "_execution_mode",
                            lambda allow_sim=True: "live")
        monkeypatch.setattr(auto_bet, "_top_down_load",
                            lambda home, away: {"1": 0.65, "X": 0.65, "2": 0.65,
                                                "overround": 0.0})
        seen: dict = {"stake": None, "info": None}

        def _fill(pick, stake, floor):
            seen["stake"] = float(stake)
            out = _filled()
            out["stake"] = stake
            return out

        # Spia sul gate VERO (il modulo, non il wrapper): e' l'unico modo di
        # distinguere "il candidato e' stato scartato dal gate" da "non e'
        # mai arrivato" (es. CAP SEVERO o finestra T-60).
        real_apply = ps.apply

        def _spy(cands, **kw):
            kept, info = real_apply(cands, **kw)
            seen["info"] = info
            return kept, info

        monkeypatch.setattr(ps, "apply", _spy)
        monkeypatch.setattr(auto_bet, "_live_fill", _fill)
        placed = auto_bet.run_today_bets(stake_eur=5.0)
        return placed, seen

    def test_off_non_cambia_il_flusso(self, monkeypatch, temp_db):
        """OFF = nessuna valutazione: i due moduli di rischio non vengono
        nemmeno importati/chiamati e lo stake resta quello dei gate."""
        placed, seen = self._run(monkeypatch, "off")
        assert len(placed) == 1 and seen["stake"] == self.BASE
        assert seen["info"]["enabled"] is False and seen["info"]["mode"] == "off"
        # Nessun giro sui due moduli: se fossero stati chiamati, le riduzioni
        # sarebbero state contate (le curve finte sono deterministiche).
        assert seen["info"]["reductions"] == [] and seen["info"]["blocks"] == []

    def test_shadow_non_cambia_lo_stake(self, monkeypatch, temp_db):
        placed, seen = self._run(monkeypatch, "shadow")
        assert len(placed) == 1 and seen["stake"] == self.BASE
        assert seen["info"]["applied"] is False
        assert seen["info"]["reductions"][0]["stake_after"] < self.BASE

    def test_enforce_riduce_lo_stake(self, monkeypatch, temp_db):
        placed, seen = self._run(monkeypatch, "enforce")
        assert ps.min_ticket() is None or self.BASE * self.MUL >= ps.min_ticket()
        assert len(placed) == 1
        assert seen["stake"] == _floor2(self.BASE * self.MUL) < self.BASE
        assert seen["stake"] == placed[0]["stake"]

    def test_enforce_non_alza_mai(self, monkeypatch, temp_db):
        """Con moltiplicatore 1.0 lo stake resta quello dei gate: il M6 puo'
        solo ridurre (nessun arrotondamento nuovo, nessun rialzo)."""
        placed, seen = self._run(monkeypatch, "enforce", multiplier=1.0)
        assert len(placed) == 1 and seen["stake"] == self.BASE
        assert seen["info"]["reductions"] == [] and seen["info"]["clamped"] == 0

    def test_il_blocco_non_lascia_righe(self, monkeypatch, temp_db):
        import auto_bet
        import tracker
        placed, seen = self._run(monkeypatch, "enforce", multiplier=0.0)
        assert placed == [] and seen["stake"] is None   # _live_fill mai chiamato
        assert seen["info"]["dropped"] == 1
        assert seen["info"]["blocks"][0]["reason"] == "blocked_by_gate"
        assert tracker.get_bets() == []
