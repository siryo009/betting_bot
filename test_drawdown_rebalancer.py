"""Test di `drawdown_rebalancer.py` (M6 Fase 4) — TUTTI OFFLINE.

Nessuna rete, nessun credito, nessun ordine. La misura del drawdown e' quella
di `auto_bet.weekly_drawdown` (delegata) e nei test viene iniettata: cosi' la
curva si verifica su valori ESATTI e non sul contenuto dello storico.

Tripwire: il modulo non scrive, non conosce il percorso ordini, non copia la
formula dello storico (`adaptive_staking.drawdown_factor` viene solo esposto
nel verdetto) e `import drawdown_rebalancer` non carica la produzione.
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

import drawdown_rebalancer as dr

ROOT = Path(__file__).resolve().parent


def _code_only(path) -> str:
    """Solo il CODICE del modulo: la prosa (docstring/commenti) nomina i confini
    per spiegarli e non e' un tripwire."""
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


def _cand(match_id="sx-1", stake=10.0, mercato="1X2"):
    return {"match_id": match_id, "mercato": mercato, "esito_key": "1",
            "stake": stake, "quota": 1.7}


def _state(dd_pct, *, peak=100.0, current=None, legacy=1.0, n=10):
    return {"ok": True, "reason": "ok", "drawdown_pct": float(dd_pct),
            "peak": peak, "current": current if current is not None
            else round(peak * (1 - dd_pct / 100.0), 4),
            "n_samples": n, "factor_legacy": legacy,
            "source": "auto_bet.weekly_drawdown"}


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    dr.reset_cache()
    for name in ("DRAWDOWN_REBALANCE", "DRAWDOWN_REBALANCE_MODE",
                 "DRAWDOWN_REBALANCE_MIN_MULTIPLIER",
                 "DRAWDOWN_REBALANCE_TIERS", "DRAWDOWN_REBALANCE_TTL_S",
                 "DRAWDOWN_REBALANCE_MIN_TICKET_USDC"):
        monkeypatch.delenv(name, raising=False)
    yield
    dr.reset_cache()


# ---------------------------------------------------------------------------
# Configurazione
# ---------------------------------------------------------------------------
class TestConfig:
    def test_default_di_codice(self):
        conf = dr.config()
        assert conf["enabled"] is True and conf["active"] is True
        assert conf["mode"] == "enforce"
        assert conf["min_multiplier"] == 0.10
        assert conf["tiers"] == dr.DEFAULT_TIERS

    def test_curva_default_crescente_e_coerente(self):
        dds = [d for d, _ in dr.DEFAULT_TIERS]
        assert dds == sorted(dds)
        assert dds[0] == pytest.approx(0.05)     # stop giornaliero
        assert 0.12 in [round(d, 4) for d in dds]  # breaker settimanale
        assert all(0.0 <= m <= 1.0 for _, m in dr.DEFAULT_TIERS)

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("DRAWDOWN_REBALANCE_MIN_MULTIPLIER", "0.30")
        monkeypatch.setenv("DRAWDOWN_REBALANCE_TIERS", "0.10:0.9,0.20:0.5")
        conf = dr.config()
        assert conf["min_multiplier"] == 0.30
        assert conf["tiers"] == ((0.10, 0.9), (0.20, 0.5))

    def test_valori_impossibili_ricadono_sul_default(self, monkeypatch):
        monkeypatch.setenv("DRAWDOWN_REBALANCE_MIN_MULTIPLIER", "2")
        assert dr.config()["min_multiplier"] == 0.10
        monkeypatch.setenv("DRAWDOWN_REBALANCE_MIN_MULTIPLIER", "abc")
        assert dr.config()["min_multiplier"] == 0.10

    def test_tier_malformati_scartati(self, monkeypatch):
        monkeypatch.setenv("DRAWDOWN_REBALANCE_TIERS",
                           "0.10:0.8,spazzatura,0.20:9,0.30:0.4")
        tiers = dr.config()["tiers"]
        # 0.20:9 fuori dominio (mult > 1) e "spazzatura" senza ':' -> scartati
        assert tiers == ((0.10, 0.8), (0.30, 0.4))

    def test_curva_vuota_o_inversa_ricade_sul_default(self, monkeypatch):
        monkeypatch.setenv("DRAWDOWN_REBALANCE_TIERS", ",,,")
        assert dr.config()["tiers"] == dr.DEFAULT_TIERS
        # curva data in ordine inverso: viene ORDINATA (mai una curva rotta)
        monkeypatch.setenv("DRAWDOWN_REBALANCE_TIERS", "0.30:0.2,0.10:0.8")
        assert dr.config()["tiers"] == ((0.10, 0.8), (0.30, 0.2))

    def test_modo_e_interruttore(self, monkeypatch):
        monkeypatch.setenv("DRAWDOWN_REBALANCE_MODE", "boh")
        assert dr.config()["mode"] == "enforce"
        monkeypatch.setenv("DRAWDOWN_REBALANCE_MODE", "off")
        assert dr.config()["active"] is False
        monkeypatch.setenv("DRAWDOWN_REBALANCE_MODE", "enforce")
        monkeypatch.setenv("DRAWDOWN_REBALANCE", "0")
        assert dr.config()["active"] is False

    def test_parse_tiers_non_solleva(self):
        assert dr.parse_tiers(None) == dr.DEFAULT_TIERS
        assert dr.parse_tiers("0.10:abc") == dr.DEFAULT_TIERS


# ---------------------------------------------------------------------------
# Curva a soglie
# ---------------------------------------------------------------------------
class TestCurva:
    def test_sotto_la_prima_soglia_nessuna_riduzione(self):
        for dd in (0.0, 0.01, 0.049):
            got = dr.multiplier_for(dd)
            assert got["multiplier"] == 1.0
            assert got["reason"] == "sotto_prima_soglia"

    def test_breaker_settimanale_piu_che_dimezzato(self):
        # 12% = soglia del breaker settimanale: lo stake e' x0.50
        assert dr.multiplier_for(0.12)["multiplier"] == pytest.approx(0.50)

    def test_tier_esatti(self):
        """Ai punti della curva il moltiplicatore e' ESATTAMENTE quello
        dichiarato (il primo tier e' l'unico che coincide col "nessuna
        riduzione": 5% -> 0.85, non 1.0)."""
        for i, (dd, expected) in enumerate(dr.DEFAULT_TIERS):
            got = dr.multiplier_for(dd)
            assert got["multiplier"] == pytest.approx(expected), (i, dd)
            assert got["reason"] in ("interpolato", "oltre_ultima_soglia")

    def test_prima_soglia_e_il_primo_tier(self):
        # Il bordo 5% vale GIA' 0.85 (soglia dello stop giornaliero): la
        # riduzione e' visibile prima che l'autorita' superiore fermi il giro.
        assert dr.multiplier_for(0.05)["multiplier"] == pytest.approx(0.85)
        assert dr.multiplier_for(0.049)["multiplier"] == 1.0

    def test_interpolazione_lineare_fra_due_tier(self):
        # fra 0.05 (0.85) e 0.10 (0.60) il punto medio vale 0.725
        assert dr.multiplier_for(0.075)["multiplier"] == pytest.approx(0.725)
        assert dr.multiplier_for(0.075)["reason"] == "interpolato"

    def test_oltre_l_ultima_soglia_pavimento(self):
        got = dr.multiplier_for(0.80)
        assert got["multiplier"] == pytest.approx(0.10)
        assert got["reason"] == "oltre_ultima_soglia"

    def test_mai_amplificazione_su_griglia(self):
        for i in range(0, 101):
            dd = i / 100.0
            assert 0.0 <= dr.multiplier_for(dd)["multiplier"] <= 1.0

    def test_monotona_non_crescente(self):
        vals = [dr.multiplier_for(i / 200.0)["multiplier"] for i in range(201)]
        assert all(b <= a + 1e-12 for a, b in zip(vals, vals[1:]))

    def test_drawdown_illeggibile(self):
        got = dr.multiplier_for("abc")
        assert got["ok"] is False and got["multiplier"] == 1.0
        assert got["reason"] == "drawdown_illeggibile"

    def test_curva_vuota(self):
        got = dr.multiplier_for(0.2, curve=())
        assert got["ok"] is False and got["reason"] == "curva_vuota"

    def test_tabella_leggibile(self):
        rows = dr.curve_table()
        assert len(rows) == len(dr.DEFAULT_TIERS)
        assert rows[0]["drawdown_pct"] == pytest.approx(5.0)
        assert rows[0]["stake_su_10_usdc"] == pytest.approx(8.5)


# ---------------------------------------------------------------------------
# Misura (delega)
# ---------------------------------------------------------------------------
class TestMisura:
    def test_delega_la_misura_ad_auto_bet(self, monkeypatch):
        import auto_bet
        seen = {}

        def _fake(bankroll=None, now=None):
            seen["bankroll"] = bankroll
            return {"drawdown_pct": 7.5, "peak": 50.0, "current": 46.25,
                    "n_samples": 12, "window_h": 168, "basis_key": "live_equity"}

        monkeypatch.setattr(auto_bet, "weekly_drawdown", _fake)
        st = dr.drawdown_state(bankroll=46.25, now=object(), use_cache=False)
        assert st["ok"] is True and st["drawdown_pct"] == 7.5
        assert st["source"] == "auto_bet.weekly_drawdown"
        assert seen["bankroll"] == 46.25
        assert st["factor_legacy"] is not None       # curva storica esposta

    def test_storico_insufficiente(self, monkeypatch):
        import auto_bet
        monkeypatch.setattr(auto_bet, "weekly_drawdown",
                            lambda bankroll=None, now=None: {
                                "drawdown_pct": 0.0, "peak": None,
                                "current": None, "n_samples": 0})
        st = dr.drawdown_state(bankroll=50.0, now=object(), use_cache=False)
        assert st["ok"] is False and st["reason"] == "storico_insufficiente"

    def test_campione_insufficiente(self, monkeypatch):
        import auto_bet
        monkeypatch.setattr(auto_bet, "weekly_drawdown",
                            lambda bankroll=None, now=None: {
                                "drawdown_pct": 0.0, "peak": 50.0,
                                "current": 50.0, "n_samples": 1})
        st = dr.drawdown_state(bankroll=50.0, now=object(), use_cache=False)
        assert st["ok"] is False and st["reason"] == "campione_insufficiente"

    def test_errore_non_propaga(self, monkeypatch):
        import auto_bet

        def _boom(bankroll=None, now=None):
            raise RuntimeError("storico corrotto")

        monkeypatch.setattr(auto_bet, "weekly_drawdown", _boom)
        st = dr.drawdown_state(bankroll=50.0, now=object(), use_cache=False)
        assert st["ok"] is False and st["reason"] == "misura_non_disponibile"

    def test_memo_per_bankroll(self, monkeypatch):
        import auto_bet
        calls = {"n": 0}

        def _fake(bankroll=None, now=None):
            calls["n"] += 1
            return {"drawdown_pct": 0.0, "peak": 50.0,
                    "current": bankroll or 50.0, "n_samples": 5}

        monkeypatch.setattr(auto_bet, "weekly_drawdown", _fake)
        dr.drawdown_state(bankroll=50.0)
        dr.drawdown_state(bankroll=50.0)
        assert calls["n"] == 1                       # dalla memo
        dr.drawdown_state(bankroll=40.0)
        assert calls["n"] == 2                       # bankroll diverso
        dr.reset_cache()
        dr.drawdown_state(bankroll=50.0)
        assert calls["n"] == 3

    def test_now_esplicito_disattiva_la_memo(self, monkeypatch):
        import auto_bet
        calls = {"n": 0}

        def _fake(bankroll=None, now=None):
            calls["n"] += 1
            return {"drawdown_pct": 0.0, "peak": 50.0, "current": 50.0,
                    "n_samples": 5}

        monkeypatch.setattr(auto_bet, "weekly_drawdown", _fake)
        dr.drawdown_state(bankroll=50.0, now=object())
        dr.drawdown_state(bankroll=50.0, now=object())
        assert calls["n"] == 2


# ---------------------------------------------------------------------------
# Applicazione
# ---------------------------------------------------------------------------
class TestApply:
    def test_interruttore_spento_non_tocca_nulla(self, monkeypatch):
        monkeypatch.setenv("DRAWDOWN_REBALANCE", "0")
        cands = [_cand(stake=10.0)]
        kept, info = dr.apply(cands, bankroll=50.0, state=_state(25.0))
        assert kept[0]["stake"] == 10.0
        assert info["active"] is False
        assert "drawdown_rebalance" not in kept[0]

    def test_giro_in_salute_non_tocca_nulla(self):
        cands = [_cand(stake=10.0)]
        kept, info = dr.apply(cands, bankroll=50.0, state=_state(3.0))
        assert kept[0]["stake"] == 10.0
        assert info["scaled"] == 0 and info["blocked"] == 0
        assert "drawdown_rebalance" not in kept[0]   # nessun campo aggiunto

    def test_drawdown_moderato_riduce(self):
        cands = [_cand(stake=10.0)]
        kept, info = dr.apply(cands, bankroll=50.0, state=_state(7.5))
        assert kept[0]["stake"] == pytest.approx(7.25)   # x0.725
        assert info["scaled"] == 1
        dec = kept[0]["drawdown_rebalance"]
        assert dec["decision"] == "scaled" and dec["multiplier"] < 1.0
        assert dec["stake_before"] == 10.0

    def test_stake_ridotto_sotto_il_ticket_blocca(self, monkeypatch):
        """Drawdown profondo su uno stake piccolo: la riduzione scende sotto il
        ticket minimo e l'ordine viene BLOCCATO, non arrotondato per passare."""
        monkeypatch.setenv("DRAWDOWN_REBALANCE_MIN_TICKET_USDC", "1.00")
        cands = [_cand(stake=2.0)]
        kept, info = dr.apply(cands, bankroll=50.0, state=_state(40.0))
        assert kept[0]["stake"] == 0.0
        assert info["blocked"] == 1
        assert info["blocked_detail"][0]["reason"] == "blocked_drawdown"
        assert kept[0]["drawdown_rebalance"]["decision"] == "blocked"
        assert "ticket" in kept[0]["drawdown_rebalance"]["detail"]

    def test_stake_ridotto_sopra_il_ticket_passa(self):
        cands = [_cand(stake=10.0)]
        kept, info = dr.apply(cands, bankroll=50.0, state=_state(40.0))
        assert kept[0]["stake"] == pytest.approx(1.0)   # 10 x 0.10
        assert info["scaled"] == 1

    def test_mai_amplificato_su_griglia(self):
        for dd in (0.0, 5.0, 10.0, 15.0, 20.0, 30.0, 60.0):
            cands = [_cand(stake=10.0)]
            kept, _ = dr.apply(cands, bankroll=50.0, state=_state(dd))
            assert kept[0]["stake"] <= 10.0

    def test_stake_zero_ignorato(self):
        cands = [_cand(stake=0.0)]
        kept, info = dr.apply(cands, bankroll=50.0, state=_state(25.0))
        assert kept[0]["stake"] == 0.0 and info["scaled"] == 0

    def test_misura_non_disponibile_lascia_invariati(self):
        cands = [_cand(stake=10.0)]
        kept, info = dr.apply(cands, bankroll=50.0,
                              state={"ok": False, "reason": "storico_insufficiente"})
        assert kept[0]["stake"] == 10.0
        assert info["reason"] == "storico_insufficiente"
        assert info["scaled"] == 0

    def test_errore_imprevisto_lascia_invariati(self, monkeypatch):
        def _boom(**kwargs):
            raise RuntimeError("guasto")
        monkeypatch.setattr(dr, "drawdown_state", _boom)
        cands = [_cand(stake=10.0)]
        kept, info = dr.apply(cands, bankroll=50.0)
        assert kept[0]["stake"] == 10.0
        assert info["error"] == "errore_valutazione"

    def test_piu_candidati_tutti_ridotti(self):
        cands = [_cand(match_id="sx-%d" % i, stake=8.0) for i in range(4)]
        kept, info = dr.apply(cands, bankroll=50.0, state=_state(20.0))
        assert all(c["stake"] == pytest.approx(2.0) for c in kept)  # x0.25
        assert info["scaled"] == 4

    def test_arrotonda_per_difetto_mai_per_eccesso(self):
        cands = [_cand(stake=10.0)]
        kept, _ = dr.apply(cands, bankroll=50.0, state=_state(5.5))
        # 5.5% -> interpolato fra 0.05 (0.85) e 0.10 (0.60) = 0.825
        # 10 x 0.825 = 8.25 -> arrotondato per DIFETTO (mai sopra il calcolato)
        assert kept[0]["stake"] == pytest.approx(8.25)
        assert kept[0]["drawdown_rebalance"]["multiplier"] == pytest.approx(0.825)

    def test_by_reason_conta_le_decisioni(self):
        cands = [_cand(stake=10.0), _cand(match_id="sx-2", stake=10.0)]
        _, info = dr.apply(cands, bankroll=50.0, state=_state(7.5))
        assert info["by_reason"]["scaled_drawdown"] == 2


# ---------------------------------------------------------------------------
# Report e CLI
# ---------------------------------------------------------------------------
class TestReport:
    def test_report_dichiara_curva_e_stato(self):
        text = dr.format_report(_state(12.0))
        assert "DRAWDOWN REBALANCING" in text
        assert "drawdown rolling 12.00%" in text
        assert "curva:" in text and "x0.85" in text

    def test_report_su_misura_assente_non_solleva(self):
        text = dr.format_report({"ok": False, "reason": "storico_insufficiente"})
        assert "misura non disponibile" in text

    def test_cli_json_con_stato_iniettato(self, monkeypatch, capsys):
        import auto_bet
        monkeypatch.setattr(auto_bet, "weekly_drawdown",
                            lambda bankroll=None, now=None: {
                                "drawdown_pct": 12.0, "peak": 50.0,
                                "current": 44.0, "n_samples": 9})
        assert dr.main(["--json", "--no-cache"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is True
        assert payload["config"]["min_ticket"] == pytest.approx(1.0)
        assert payload["applied_multiplier"] == pytest.approx(0.50)
        assert payload["curve"]

    def test_cli_curve_non_tocca_lo_storico(self, monkeypatch, capsys):
        import auto_bet

        def _boom(bankroll=None, now=None):
            raise AssertionError("--curve non deve misurare nulla")

        monkeypatch.setattr(auto_bet, "weekly_drawdown", _boom)
        assert dr.main(["--curve", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert len(payload["tiers"]) == len(dr.DEFAULT_TIERS)


# ---------------------------------------------------------------------------
# Tripwire
# ---------------------------------------------------------------------------
class TestTripwire:
    def _src(self) -> str:
        return _code_only(dr.__file__)

    def test_sorgente_senza_scritture(self):
        src = self._src()
        for token in ("INSERT", "UPDATE ", "DELETE FROM", "save_bet(",
                      "save_prediction(", "save_clv(", "unlink(", "open("):
            assert token not in src, token

    def test_sorgente_senza_rete_ne_ordini(self):
        src = self._src()
        for token in ("import requests", "httpx", "odds_api", "sx_signals",
                      "_live_fill", "place_limit_order", "resolve_market_for",
                      "execution_engine", "value_filter", "poisson_engine"):
            assert token not in src, token

    def test_non_ricopia_la_curva_storica(self):
        """Il fattore di `adaptive_staking` si LEGGE (o si espone), mai si copia."""
        src = self._src()
        assert "DRAWDOWN_REDUCTION" not in src
        assert "DRAWDOWN_THRESHOLD" not in src

    def test_import_leggero(self):
        code = ("import sys, drawdown_rebalancer;"
                "print(any(m in sys.modules for m in "
                "('tracker','auto_bet','bot','odds_api')))")
        out = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT),
                             capture_output=True, text=True, timeout=120)
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == "False"
