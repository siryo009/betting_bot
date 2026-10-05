"""Test della FINANZA sul percorso ValidatedTrade (direttiva 04/10/2026).

La Finanza e' l'UNICO agente che trasforma una decisione in un importo. Qui si
verifica che:

1. usi il motore Kelly dinamico (k 0.15-0.25 da EV/edge/lega, cap 12%,
   ticket 1.00) — la stessa fonte della corsia storica
   (`decision.stake_engine`), mai una copia;
2. rispetti il Portfolio Shield del Cervello (`block` = non eseguibile,
   `scale` = tetto allo spazio residuo del blocco correlato);
3. legga il saldo USDC fresco SUBITO prima del sizing (compounding), con una
   catena di precedenza dichiarata e fail-safe sul lettore rotto;
4. la catena Analisi -> Cervello -> Finanza produca un importo eseguibile e
   finisca nel `CycleReport` del Capo (`analysis`/`brain`/`sizing`).

Tutto OFFLINE: dipendenze iniettate, nessun provider, nessun ordine, ledger
temporaneo dove serve.
"""

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

import auto_bet
import value_filter as vf
from agents.analysis_agent import AnalysisAgent
from agents.brain_agent import BrainAgent
from agents.contracts import (AnalysisOutput, BrainOutput, CycleReport,
                              FinanceOutput, OracleSignal, SizingOutput,
                              ValidatedTrade, DEFAULT_MAX_AGE_S)
from agents.finance_agent import FinanceAgent
from decision.models import DataQuality, Signal


def _signal(match_id="sx-1", outcome="1", price=1.66, blended=0.65,
            tier="strong_value", league="Premier League"):
    return Signal(
        match_id=match_id, outcome=outcome, market="1X2", price=price,
        league=league, tier=tier, confidence=0.7,
        kickoff=(datetime.now(timezone.utc) + timedelta(hours=3)).isoformat(),
        market_prob=0.55, model_prob=blended, blended_prob=blended,
        data_quality=DataQuality(ratings_home_n=10, ratings_away_n=10,
                                 market_coherent=True, inv_sum=1.04,
                                 depth_usdc=200.0),
    )


def _trade(**kw):
    base = dict(signal_id="s1", match_id="sx-1", esito="1", market="1X2",
                league="Premier League", home="Home", away="Away",
                price=1.66, true_prob=0.60, ev=0.10)
    base.update(kw)
    return ValidatedTrade(**base)


# ---------------------------------------------------------------------------
# 1. SIZE_TRADE — motore Kelly + Shield
# ---------------------------------------------------------------------------

class TestSizeTrade:
    def test_stake_dal_motore_kelly(self):
        fin = FinanceAgent(bankroll=100.0)
        trade = fin.size_trade(_trade())
        assert trade.executable is True
        # k dinamico (EV 10% + lega core -> verso il massimo della banda)
        assert 0.15 <= trade.kelly_fraction <= 0.25
        full = vf.kelly_fraction(0.60, 1.66, fraction=1.0)
        assert trade.stake == round(100.0 * full * trade.kelly_fraction, 2)
        assert trade.min_ticket == 1.00
        assert trade.reason == "ok"

    def test_motore_sotto_il_ticket_non_esegue(self):
        fin = FinanceAgent(bankroll=10.0)          # raw ~0.88 < ticket 1.00
        trade = fin.size_trade(_trade())
        assert trade.stake == 0.0 and trade.executable is False
        assert trade.reason == "below_min_ticket"

    def test_shield_block_ha_la_precedenza(self):
        """Un blocco del blocco correlato non si \"aggiusta\" col Kelly: il
        trade resta non eseguibile anche se il motore avrebbe dato 12.00."""
        fin = FinanceAgent(bankroll=100.0)
        trade = fin.size_trade(_trade(shield_action="block",
                                      shield_reason="saturo"))
        assert trade.stake == 0.0 and trade.executable is False
        assert trade.reason == "shield_block"

    def test_shield_scale_cappa_lo_stake(self):
        fin = FinanceAgent(bankroll=100.0)
        trade = fin.size_trade(_trade(shield_max_usdc=5.0))
        assert trade.stake == 5.0 and trade.executable is True
        assert trade.reason == "shield_scaled"

    def test_senza_probabilita_non_si_dimensiona(self):
        fin = FinanceAgent(bankroll=100.0)
        trade = fin.size_trade(_trade(true_prob=None))
        assert trade.stake == 0.0 and trade.reason == "no_true_prob"

    def test_sempre_un_trade_mai_un_eccezione(self):
        fin = FinanceAgent(bankroll=100.0)
        for kw in ({"price": 1.01}, {"true_prob": 0.999}, {"shield_action": "block"}):
            out = fin.size_trade(_trade(**kw))
            assert isinstance(out, ValidatedTrade)


# ---------------------------------------------------------------------------
# 2. SALDO FRESCO (fetch pulito prima del calcolo size)
# ---------------------------------------------------------------------------

class TestFreshBankroll:
    def test_saldo_dal_provider(self):
        fin = FinanceAgent(bankroll=100.0,
                           balance_fn=lambda: {"equity": 250.0})
        assert fin.fresh_bankroll() == 250.0

    def test_override_vince_sul_provider(self):
        fin = FinanceAgent(bankroll=100.0,
                           balance_fn=lambda: {"equity": 250.0})
        assert fin.fresh_bankroll(override=42.0) == 42.0

    def test_senza_provider_usa_il_bankroll_del_ciclo(self):
        fin = FinanceAgent(bankroll=33.55)
        assert fin.fresh_bankroll() == pytest.approx(33.55)

    def test_lettore_rotto_ricade_sul_bankroll_del_ciclo(self):
        """Un lettore rotto NON genera un importo casuale."""
        def boom():
            raise RuntimeError("provider giu'")
        fin = FinanceAgent(bankroll=100.0, balance_fn=boom)
        assert fin.fresh_bankroll() == 100.0

    def test_il_compounding_usa_il_saldo_fresco(self):
        """Il fetto pulito pre-size: raddoppia il saldo, raddoppia lo stake."""
        fin = FinanceAgent(bankroll=100.0,
                           balance_fn=lambda: {"equity": 200.0})
        trade = fin.size_trade(_trade())
        assert trade.bankroll == 200.0
        # il capitale scala col capitale (compounding sul saldo fresco)
        assert trade.stake == round(200.0 * trade.kelly_full * trade.kelly_fraction, 2)

    def test_dict_senza_equity_usa_il_disponibile(self):
        fin = FinanceAgent(bankroll=100.0,
                           balance_fn=lambda: {"available": 33.0})
        assert fin.fresh_bankroll() == 33.0


# ---------------------------------------------------------------------------
# 3. CICLO DEI TRADE
# ---------------------------------------------------------------------------

class TestProcessTrades:
    def test_contatori(self):
        fin = FinanceAgent(bankroll=100.0)
        out = fin.process_trades([_trade(match_id="a"),
                                  _trade(match_id="b", shield_action="block"),
                                  _trade(match_id="c", true_prob=None)])
        assert isinstance(out, SizingOutput)
        assert out.sized == 3 and out.executable == 1 and out.skipped == 2

    def test_lista_vuota(self):
        out = FinanceAgent(bankroll=100.0).process_trades([])
        assert out.trades == [] and out.sized == 0

    def test_output_serializzabile(self):
        out = FinanceAgent(bankroll=100.0).process_trades([_trade()])
        data = out.as_json()
        assert data["trades"] == 1 and data["executable"] == 1


# ---------------------------------------------------------------------------
# 4. CONTRATTI
# ---------------------------------------------------------------------------

class TestContratti:
    def test_validated_trade_created_at_aware(self):
        t = ValidatedTrade(**{"signal_id": "s", "match_id": "m", "esito": "1",
                              "price": 1.66, "created_at":
                              datetime.now(timezone.utc).isoformat()})
        assert t.created_at.tzinfo is not None
        assert t.shield_action == "allow" and t.ev_multiplier == 1.0

    def test_validated_trade_created_at_naive_rifiutato(self):
        with pytest.raises(ValidationError):
            ValidatedTrade(signal_id="s", match_id="m", esito="1", price=1.66,
                           created_at=datetime.now())

    def test_oracle_signal_default_di_freschezza(self):
        s = OracleSignal(signal_id="s", match_id="m", esito="1", price=1.66,
                         observed_at=datetime.now(timezone.utc))
        assert s.max_age_s == DEFAULT_MAX_AGE_S and s.fresh is True

    def test_output_degli_agenti_serializzabili(self):
        assert set(AnalysisOutput().as_json()) >= {
            "signals", "steam_moves", "juice_anomalies", "stale"}
        assert set(BrainOutput().as_json()) >= {
            "trades", "validated", "rejected", "scaled", "blocked"}
        assert set(SizingOutput().as_json()) >= {
            "trades", "sized", "executable", "skipped"}

    def test_cycle_report_porta_i_tre_nuovi_blocchi(self):
        data = CycleReport().as_json()
        for key in ("analysis", "brain", "sizing", "market", "strategy",
                    "finance", "execution", "exposure"):
            assert key in data


# ---------------------------------------------------------------------------
# 5. CATENA Analisi -> Cervello -> Finanza (end-to-end, offline)
# ---------------------------------------------------------------------------

class TestCatenaEndToEnd:
    def _chain(self, bankroll=100.0, open_bets=None, **trade_kw):
        analysis = AnalysisAgent(
            steam_fn=lambda h, a, m, e, o: {"steam_move": True, "move_pct": -2.0,
                                            "span_minutes": 10.0,
                                            "reason": "steam_down"},
            oracle_fn=lambda h, a: {"overround": 0.045, "sources": ["pinnacle"]},
            names_fn=lambda mid: {"home": "Home FC", "away": "Away FC",
                                  "league": "Premier League"})
        brain = BrainAgent(bankroll=bankroll,
                           open_bets_fn=lambda: list(open_bets or []),
                           min_ticket_fn=lambda: 2.0)
        fin = FinanceAgent(bankroll=bankroll)
        return analysis, brain, fin

    def test_la_catena_produce_uno_stake_eseguibile(self):
        analysis, brain, fin = self._chain()
        a = analysis.process([_signal()])
        assert a.signals[0].velocity_pct_min == -0.2
        b = brain.process(a.signals)
        assert b.validated == 1
        s = fin.process_trades([t for t in b.trades
                                if t.shield_action != "block"])
        assert s.executable == 1
        trade = s.trades[0]
        assert trade.stake == round(100.0 * trade.kelly_full * trade.kelly_fraction, 2)
        assert trade.analysis["steam_move"] is True
        assert trade.analysis["juice"] == 0.045

    def test_la_volatilita_alza_la_soglia_e_il_trade_puo_cadere(self):
        """EV 3% con sharp in movimento: la soglia dinamica sale al 3% e il
        trade non passa piu' (il Cervello non allarga, restringe)."""
        analysis = AnalysisAgent(
            steam_fn=lambda *a: {"steam_move": True, "move_pct": -2.0,
                                 "span_minutes": 10.0},
            oracle_fn=lambda h, a: None,
            names_fn=lambda mid: {"home": "H", "away": "A", "league": "PL"})
        brain = BrainAgent(bankroll=100.0, open_bets_fn=lambda: [],
                           min_ticket_fn=lambda: 2.0)
        sig = _signal(blended=0.62)      # EV = 0.62*1.66 - 1 = 0.0292
        b = brain.process(analysis.process([sig]).signals)
        assert b.validated == 0 and b.rejected == 1

    def test_shield_scale_limita_lo_stake_finale(self):
        analysis, brain, fin = self._chain(
            open_bets=[{"league": "Premier League", "stake": 25.0}])
        b = brain.process(analysis.process([_signal()]).signals)
        s = fin.process_trades([t for t in b.trades
                                if t.shield_action != "block"])
        assert s.trades[0].stake == 5.0            # residuo del blocco


# ---------------------------------------------------------------------------
# 6. CICLO DEL CAPO: il sizing finisce nel report
# ---------------------------------------------------------------------------

class _Data:
    def __init__(self, signals):
        self._signals = signals

    def process(self, conn=None, now=None):
        from types import SimpleNamespace
        return SimpleNamespace(
            validated=True, signals=list(self._signals),
            gate=SimpleNamespace(reason=SimpleNamespace(value="ok")),
            as_json=lambda: {"signals": len(self._signals),
                             "gate": {"allowed": True}})


class _FinanceSpy(FinanceAgent):
    """Finanza VERA per il sizing, con `process_many` neutro (non e' il tema)."""

    def process_many(self, signals, *, feed=None, now=None):
        return FinanceOutput()


class TestCicloDelCapo:
    def test_report_porta_analysis_brain_e_sizing(self, monkeypatch):
        from chief_orchestrator import ChiefOrchestrator
        monkeypatch.delenv("CHIEF_EXECUTION", raising=False)
        sig = _signal()
        fin = _FinanceSpy(bankroll=100.0)
        chief = ChiefOrchestrator(
            data=_Data([sig]),
            finance=fin,
            analysis=AnalysisAgent(
                steam_fn=lambda *a: {"steam_move": True, "move_pct": -2.0,
                                     "span_minutes": 10.0},
                oracle_fn=lambda h, a: {"overround": 0.04},
                names_fn=lambda mid: {"home": "H", "away": "A",
                                      "league": "Premier League"}),
            brain=BrainAgent(bankroll=100.0, open_bets_fn=lambda: [],
                             min_ticket_fn=lambda: 2.0),
            advisor_enabled=False,
        )
        report = chief.run_cycle()
        assert report.ok is True
        assert report.analysis["signals"] == 1
        assert report.analysis["steam_moves"] == 1
        assert report.brain["validated"] == 1
        assert report.sizing["executable"] == 1
        # lo stake del motore Kelly e' nel report (dato, non ordine)
        assert report.sizing["trades"] == 1

    def test_gate_di_mercato_non_validato_blocca_il_ciclo(self, monkeypatch):
        from chief_orchestrator import ChiefOrchestrator

        class _DataBloccata(_Data):
            def process(self, conn=None, now=None):
                from types import SimpleNamespace
                return SimpleNamespace(
                    validated=False, signals=[],
                    gate=SimpleNamespace(reason=SimpleNamespace(value="feed_missing")),
                    as_json=lambda: {"gate": {"allowed": False}})

        chief = ChiefOrchestrator(
            data=_DataBloccata([]), finance=FinanceAgent(bankroll=100.0),
            advisor_enabled=False)
        report = chief.run_cycle()
        assert report.ok is False
        assert "market_gate" in report.blocked_reason
        assert report.sizing == {}      # nessun sizing senza mercato validato

    def test_fail_safe_su_errore_di_ciclo(self):
        from chief_orchestrator import ChiefOrchestrator

        class _DataRotta:
            def process(self, conn=None, now=None):
                raise RuntimeError("boom")

        report = ChiefOrchestrator(data=_DataRotta(),
                                   finance=FinanceAgent(bankroll=100.0),
                                   advisor_enabled=False).run_cycle()
        assert report.ok is False
        assert "orchestrator_error" in report.blocked_reason


# ---------------------------------------------------------------------------
# 7. TRIPWIRE: nessuna soglia duplicata, nessun ordine negli agenti
# ---------------------------------------------------------------------------

class TestTripwire:
    def test_il_motore_kelly_e_unico(self):
        """La Finanza delega a `decision.stake_engine`: nessuna formula di
        Kelly ricopiata nel modulo."""
        from pathlib import Path
        src = Path(agents_path("finance_agent.py")).read_text(encoding="utf-8")
        code = "\n".join(line for line in src.splitlines()
                         if not line.strip().startswith("#"))
        assert "prob * odds" not in code
        assert "calculate_kelly_stake" in code      # la delega c'e'

    def test_nessun_ordine_negli_agenti(self):
        from pathlib import Path
        for name in ("analysis_agent.py", "brain_agent.py", "finance_agent.py"):
            src = Path(agents_path(name)).read_text(encoding="utf-8")
            assert "_live_fill(" not in src
            assert "place_limit_order" not in src
            assert "PlaceOrderGateway(" not in src

    def test_shield_cap_non_duplicato(self):
        """Il cap del blocco correlato arriva da `auto_bet`, non da una copia."""
        assert BrainAgent is not None
        from agents.brain_agent import shield_cap_pct
        assert shield_cap_pct() == auto_bet.CORRELATION_CAP_PCT


def agents_path(name: str) -> str:
    import agents
    from pathlib import Path
    return str(Path(agents.__file__).parent / name)
