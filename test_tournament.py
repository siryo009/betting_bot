"""Layer di staking del TORNEO SX (return-based) — `tournament.py`, OFFLINE.

Il torneo **Gridiron Gauntlet** (08/10 06:00 -> 20/10 06:00) e' return-based:
"wins increase your standing, losses will have no impact". Cio' significa che
il punteggio e' la somma dei RITORNI LORDI delle vincite e una puntata persa
NON toglie punti.

⚠️ Un torneo return-based NON e' un edge: matematicamente l'ottimale *per la
classifica* e' la massima varianza possibile, anche a EV negativo. Il progetto
e' costruito al contrario (solo +EV, oracolo fail-closed, edge minimo, cap
severo). Questo layer quindi NON bypassa nessun gate: puo' solo ALZARE lo
stake di un segnale che i gate hanno GIA' approvato, dentro un budget di
esposizione e una soglia di sicurezza sull'equity.

Qui si verificano le TRE protezioni (budget, soglia di sicurezza, cap per
ordine), l'ordine dei controlli, il fail-safe e il cablaggio in `auto_bet`.
Nessuna rete, nessun ordine: i provider sono finti e il "capitale" e'
iniettato.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import tournament as tn


@pytest.fixture(autouse=True)
def _torneo_spento(monkeypatch):
    """Default dei test: torneo SPENTO e nessun capitale gia' impegnato.

    Il conftest spegne `TOURNAMENT_MODE` per tutti i file tranne questo; qui
    partiamo comunque da uno stato pulito e i singoli test accendono cio' che
    serve. `deployed()` viene azzerato per isolare i test del budget dal
    ledger reale (che nei test e' temporaneo o assente).
    """
    monkeypatch.delenv("TOURNAMENT_MODE", raising=False)
    monkeypatch.delenv("TOURNAMENT_CLASSES", raising=False)
    # Lo snapshot dell'esposizione e' l'UNICO punto che "sa" cosa e' il
    # capitale impegnato: nei test lo si neutralizza alla FONTE (il ledger),
    # non la funzione che lo legge — altrimenti si misurerebbe lo stub.
    import auto_bet
    monkeypatch.setattr(auto_bet, "_open_live_snapshot", lambda: (0.0, 0))
    yield


# ---------------------------------------------------------------------------
# 1. CONFIGURAZIONE
# ---------------------------------------------------------------------------

class TestConfig:
    def test_num_env_default_e_clamp(self, monkeypatch):
        monkeypatch.delenv("TOURNAMENT_BUDGET_USDC", raising=False)
        assert tn._num_env("TOURNAMENT_BUDGET_USDC", 10.0) == 10.0
        monkeypatch.setenv("TOURNAMENT_BUDGET_USDC", "25")
        assert tn._num_env("TOURNAMENT_BUDGET_USDC", 10.0) == 25.0
        # valore non numerico -> default (una soglia non si spegne con un env
        # sbagliato)
        monkeypatch.setenv("TOURNAMENT_BUDGET_USDC", "abc")
        assert tn._num_env("TOURNAMENT_BUDGET_USDC", 10.0) == 10.0
        # sotto il minimo -> default
        monkeypatch.setenv("TOURNAMENT_BUDGET_USDC", "-5")
        assert tn._num_env("TOURNAMENT_BUDGET_USDC", 10.0, minimum=0.0) == 10.0

    def test_active_default_off(self, monkeypatch):
        monkeypatch.delenv("TOURNAMENT_MODE", raising=False)
        assert tn.active() is False
        for off in ("0", "false", "no", "off", ""):
            monkeypatch.setenv("TOURNAMENT_MODE", off)
            assert tn.active() is False
        monkeypatch.setenv("TOURNAMENT_MODE", "1")
        assert tn.active() is True

    def test_valori_di_default(self):
        # Budget 20 / cap 5.00 dal 12/10/2026 (sprint del torneo: in un
        # formato RETURN i punti sono i ritorni LORDI, quindi il cap per
        # ordine e' il parametro che decide la corsa al taglio).
        assert tn.budget() == 20.0
        assert tn.safety_floor() == 20.0
        assert tn.max_order_stake() == 5.00
        assert tn.aggression() == 1.5

    def test_aggressione_mai_sotto_uno(self, monkeypatch):
        # un moltiplicatore sotto 1 RIDURREBBE lo stake: il torneo non e' un
        # cap, e' un incremento. Valore impossibile -> default.
        monkeypatch.setenv("TOURNAMENT_AGGRESSION", "0.5")
        assert tn.aggression() >= 1.0
        monkeypatch.setenv("TOURNAMENT_AGGRESSION", "2.0")
        assert tn.aggression() == 2.0

    def test_classi_e_perimetro(self, monkeypatch):
        monkeypatch.delenv("TOURNAMENT_CLASSES", raising=False)
        assert tn.classes() == ()
        assert tn.covers("1X2") is True          # vuoto = tutte le corsie
        assert tn.covers(None) is True
        monkeypatch.setenv("TOURNAMENT_CLASSES", "1x2, ah")
        assert tn.classes() == ("1X2", "AH")
        assert tn.covers("1X2") is True
        assert tn.covers("AH") is True
        assert tn.covers("OU") is False          # fuori perimetro

    def test_min_stake_dal_modulo_di_produzione(self):
        import auto_bet
        assert tn.min_stake() == pytest.approx(auto_bet.MIN_STAKE_EUR)
        assert tn.min_stake() > 0


# ---------------------------------------------------------------------------
# 2. ESPOSIZIONE / BUDGET
# ---------------------------------------------------------------------------

class TestBudget:
    def test_deployed_delega_al_snapshot_unico(self, monkeypatch):
        # `deployed` NON reimplementa l'esposizione: usa l'UNICO punto del
        # progetto che sa cos'e' il "capitale immobilizzato".
        import auto_bet
        monkeypatch.setattr(auto_bet, "_open_live_snapshot",
                            lambda: (3.5, 2))
        assert tn.deployed() == pytest.approx(3.5)

    def test_deployed_fail_open(self, monkeypatch):
        import auto_bet

        def boom():
            raise RuntimeError("ledger rotto")

        monkeypatch.setattr(auto_bet, "_open_live_snapshot", boom)
        # un problema di telemetria non azzera il budget (i cap di portafoglio
        # restano comunque attivi)
        assert tn.deployed() == 0.0

    def test_budget_left(self, monkeypatch):
        monkeypatch.setenv("TOURNAMENT_BUDGET_USDC", "10")
        monkeypatch.setattr(tn, "deployed", lambda: 4.0)
        assert tn.budget_left() == pytest.approx(6.0)
        monkeypatch.setattr(tn, "deployed", lambda: 20.0)
        assert tn.budget_left() == 0.0                # mai negativo


# ---------------------------------------------------------------------------
# 3. stake_for: l'ordine dei controlli e i motivi
# ---------------------------------------------------------------------------

class TestStakeFor:
    def test_stake_zero_o_invalido(self):
        assert tn.stake_for(0.0)["stake"] == 0.0
        assert tn.stake_for(0.0)["applied"] is False
        assert tn.stake_for(None)["stake"] == 0.0
        assert tn.stake_for("abc")["stake"] == 0.0

    def test_torneo_spento_lascia_lo_stake(self, monkeypatch):
        monkeypatch.delenv("TOURNAMENT_MODE", raising=False)
        res = tn.stake_for(2.0, capital=30.0)
        assert res["applied"] is False and res["blocked"] is False
        assert res["reason"] == "mode_off"
        assert res["stake"] == 2.0                    # invariato

    def test_classe_fuori_perimetro(self, monkeypatch):
        monkeypatch.setenv("TOURNAMENT_MODE", "1")
        monkeypatch.setenv("TOURNAMENT_CLASSES", "1X2")
        res = tn.stake_for(2.0, pick={"mercato": "OU"}, capital=30.0)
        assert res["applied"] is False and res["blocked"] is False
        assert res["reason"] == "class_not_covered"

    def test_soglia_di_sicurezza_blocca(self, monkeypatch):
        monkeypatch.setenv("TOURNAMENT_MODE", "1")
        res = tn.stake_for(2.0, pick={"mercato": "1X2"}, capital=15.0)
        assert res["blocked"] is True
        assert res["stake"] == 0.0
        assert res["reason"] == "safety_floor"
        assert "15.00" in res["detail"]

    def test_soglia_di_sicurezza_non_scatta_senza_capitale(self, monkeypatch):
        # capitale non fornito -> la soglia non e' valutabile: si prosegue
        # (fail-open sulla LETTURA, il budget resta comunque il vincolo).
        monkeypatch.setenv("TOURNAMENT_MODE", "1")
        res = tn.stake_for(2.0, pick={"mercato": "1X2"}, capital=None)
        assert res["applied"] is True

    def test_budget_esaurito_blocca(self, monkeypatch):
        monkeypatch.setenv("TOURNAMENT_MODE", "1")
        monkeypatch.setenv("TOURNAMENT_BUDGET_USDC", "10")
        import auto_bet
        monkeypatch.setattr(auto_bet, "_open_live_snapshot", lambda: (9.5, 1))
        res = tn.stake_for(2.0, pick={"mercato": "1X2"}, capital=30.0)
        assert res["blocked"] is True
        assert res["reason"] == "budget_exhausted"
        assert res["stake"] == 0.0

    def test_below_min_blocca(self, monkeypatch):
        monkeypatch.setenv("TOURNAMENT_MODE", "1")
        # base 0.5 x 1.5 = 0.75 < minimo ordine (1.00) -> non eseguibile
        res = tn.stake_for(0.5, pick={"mercato": "1X2"}, capital=30.0)
        assert res["blocked"] is True
        assert res["reason"] == "below_min"

    def test_applicato_con_incremento(self, monkeypatch):
        monkeypatch.setenv("TOURNAMENT_MODE", "1")
        # base 1.5 x 1.5 = 2.25 (sotto il cap per ordine 2.50)
        res = tn.stake_for(1.5, pick={"mercato": "1X2"}, capital=30.0)
        assert res["applied"] is True and res["blocked"] is False
        assert res["reason"] == "tournament"
        assert res["stake"] == pytest.approx(2.25)

    def test_cap_per_ordine(self, monkeypatch):
        monkeypatch.setenv("TOURNAMENT_MODE", "1")
        # 4.0 x 1.5 = 6.0 -> cap per ordine 5.00 (default dal 12/10/2026)
        res = tn.stake_for(4.0, pick={"mercato": "1X2"}, capital=30.0)
        assert res["stake"] == pytest.approx(5.00)

    def test_cap_dal_residuo_di_budget(self, monkeypatch):
        monkeypatch.setenv("TOURNAMENT_MODE", "1")
        monkeypatch.setenv("TOURNAMENT_BUDGET_USDC", "10")
        import auto_bet
        monkeypatch.setattr(auto_bet, "_open_live_snapshot", lambda: (7.6, 2))
        res = tn.stake_for(2.0, pick={"mercato": "1X2"}, capital=30.0)
        assert res["stake"] == pytest.approx(2.40)

    def test_cap_configurabile(self, monkeypatch):
        monkeypatch.setenv("TOURNAMENT_MODE", "1")
        monkeypatch.setenv("TOURNAMENT_MAX_STAKE_USDC", "2")
        res = tn.stake_for(2.0, pick={"mercato": "1X2"}, capital=30.0)
        assert res["stake"] == pytest.approx(2.0)      # cap 2 morde il 3.0

    def test_mai_riduce(self, monkeypatch):
        # anche con aggressione 1.0 il risultato non scende sotto la base
        monkeypatch.setenv("TOURNAMENT_MODE", "1")
        monkeypatch.setenv("TOURNAMENT_AGGRESSION", "1.0")
        res = tn.stake_for(2.0, pick={"mercato": "1X2"}, capital=30.0)
        assert res["stake"] >= 2.0

    def test_fail_safe_su_errore(self, monkeypatch):
        monkeypatch.setenv("TOURNAMENT_MODE", "1")

        def boom():
            raise RuntimeError("deployed rotto")

        monkeypatch.setattr(tn, "budget_left", boom)
        res = tn.stake_for(2.0, pick={"mercato": "1X2"}, capital=30.0)
        # l'errore NON alza lo stake e non solleva: si torna allo standard
        assert res["applied"] is False
        assert res["reason"] == "error"


# ---------------------------------------------------------------------------
# 4. STATUS / REPORT
# ---------------------------------------------------------------------------

class TestReport:
    def test_status_e_report_spento(self, monkeypatch):
        monkeypatch.delenv("TOURNAMENT_MODE", raising=False)
        s = tn.status()
        assert s["active"] is False
        assert s["budget"] == 20.0
        assert "SPENTO" in tn.format_report(s)

    def test_report_attivo(self, monkeypatch):
        monkeypatch.setenv("TOURNAMENT_MODE", "1")
        import auto_bet
        monkeypatch.setattr(auto_bet, "_open_live_snapshot", lambda: (2.0, 1))
        text = tn.format_report(tn.status())
        assert "ATTIVO" in text
        assert "2.00/20.00" in text
        assert "x1.50" in text
        assert "tutte le corsie" in text

    def test_label(self):
        assert tn.LABEL                              # nome del torneo non vuoto


# ---------------------------------------------------------------------------
# 5. CABLAGGIO in auto_bet (`_apply_tournament`)
# ---------------------------------------------------------------------------

class TestCablaggio:
    def _cands(self):
        return [{"match_id": "m1", "mercato": "1X2", "esito_key": "1",
                 "price": 1.80, "stake": 2.0},
                {"match_id": "m2", "mercato": "1X2", "esito_key": "2",
                 "price": 2.10, "stake": 2.0}]

    def test_spento_lascia_invariata(self, monkeypatch):
        import auto_bet
        monkeypatch.setenv("TOURNAMENT_MODE", "0")
        cands = self._cands()
        kept, info = auto_bet._apply_tournament(cands, bankroll=30.0)
        assert kept == cands
        assert info["enabled"] is False
        assert [c["stake"] for c in kept] == [2.0, 2.0]

    def test_acceso_alza_gli_stake(self, monkeypatch):
        import auto_bet
        monkeypatch.setenv("TOURNAMENT_MODE", "1")
        kept, info = auto_bet._apply_tournament(self._cands(), bankroll=30.0)
        assert info["enabled"] is True
        assert info["raised"] == 2
        assert all(c["stake"] == pytest.approx(3.0) for c in kept)   # x1.5

    def test_bloccati_rimossi(self, monkeypatch):
        import auto_bet
        monkeypatch.setenv("TOURNAMENT_MODE", "1")
        # equity sotto la soglia: nessun candidato deve arrivare alla FASE 3
        kept, info = auto_bet._apply_tournament(self._cands(), bankroll=10.0)
        assert kept == []
        assert info["blocked"] == 2

    def test_fail_safe_su_errore(self, monkeypatch):
        import auto_bet
        monkeypatch.setenv("TOURNAMENT_MODE", "1")
        monkeypatch.setattr(tn, "active",
                            lambda: (_ for _ in ()).throw(RuntimeError("x")))
        cands = self._cands()
        kept, info = auto_bet._apply_tournament(cands, bankroll=30.0)
        assert kept == cands and info["enabled"] is False

    def test_chiamata_nel_percorso_live(self):
        src = Path("auto_bet.py").read_text(encoding="utf-8")
        assert "_apply_tournament(candidates" in src
        assert "GRIDIRON_FLAG" in src

    def test_env_torneo_in_iac(self):
        iac = Path(".railway/railway.ts").read_text(encoding="utf-8")
        for name in ("TOURNAMENT_MODE", "TOURNAMENT_LABEL",
                     "TOURNAMENT_BUDGET_USDC", "TOURNAMENT_SAFETY_FLOOR_USDC",
                     "TOURNAMENT_MAX_STAKE_USDC", "TOURNAMENT_AGGRESSION",
                     "TOURNAMENT_CLASSES"):
            assert name in iac, f"env non dichiarata nella IaC: {name}"

    def test_import_leggero(self):
        code = ("import sys, tournament;"
                "bad = [m for m in ('tracker','bot') if m in sys.modules];"
                "print(','.join(bad))")
        out = subprocess.run([sys.executable, "-c", code],
                             capture_output=True, text=True, cwd=str(Path.cwd()))
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == ""
