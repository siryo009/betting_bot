"""Test dell'Agente CERVELLO (direttiva 04/10/2026).

Due ragionamenti che nessun altro agente fa:

1. **EV dinamico** — la soglia minima di EV sale quando il trade e' fragile:
   libro sottile (rischio slippage) o sharp che si muove veloce (il prezzo puo'
   non esistere piu' all'arrivo). Parte SEMPRE da `value_filter.EV_MIN` e puo'
   solo SALIRE: il Cervello non allarga cio' che la pipeline ha stretto.
2. **Portfolio Shield** — misura la concentrazione del blocco correlato (lega)
   sugli ordini REALI aperti e restituisce `allow`/`scale`/`block`. Il cap e'
   quello di `auto_bet` (30%), il ticket quello del motore Kelly: nessuna
   soglia ricopiata.

Tutto OFFLINE: nessun ordine aperto viene letto dal DB di produzione (il
lettore e' iniettato o punta a un ledger temporaneo) e nessuna scrittura.
"""

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import auto_bet
import value_filter as vf
from agents.brain_agent import (BrainAgent, base_ev_min, dynamic_ev_min,
                                shield_cap_pct, shield_decision)
from agents.contracts import BrainOutput, OracleSignal


def _oracle(match_id="sx-1", esito="1", price=1.66, true_prob=0.60, ev=None,
            league="Premier League", depth=None, velocity=0.0, steam=False):
    return OracleSignal(
        signal_id=f"{match_id}|1X2|{esito}", match_id=match_id, esito=esito,
        market="1X2", price=price, true_prob=true_prob, ev=ev, league=league,
        home="Home FC", away="Away FC",
        kickoff=(datetime.now(timezone.utc) + timedelta(hours=3)).isoformat(),
        depth_usdc=depth, velocity_pct_min=velocity, steam_move=steam,
        observed_at=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# 1. EV DINAMICO
# ---------------------------------------------------------------------------

class TestEvDinamico:
    def test_base_e_quella_della_pipeline(self):
        """Una sola definizione di soglia: nessuna copia locale."""
        assert base_ev_min() == vf.EV_MIN

    def test_senza_segnali_negativi_resta_la_base(self):
        assert dynamic_ev_min()["ev_min"] == vf.EV_MIN
        assert dynamic_ev_min()["ev_multiplier"] == 1.0
        assert dynamic_ev_min()["dynamic_reason"] == "base"

    def test_libro_profondo_non_alza(self):
        got = dynamic_ev_min(depth_usdc=500.0, velocity_pct_min=0.0)
        assert got["ev_min"] == vf.EV_MIN and got["ev_multiplier"] == 1.0

    def test_libro_sottile_alza_del_25_percento(self):
        got = dynamic_ev_min(depth_usdc=5.0)      # < 20 USDC di default
        assert got["ev_multiplier"] == 1.25
        assert abs(got["ev_min"] - vf.EV_MIN * 1.25) < 1e-9
        assert "liquidita'" in got["dynamic_reason"]

    def test_volatilita_alza_proporzionalmente(self):
        got = dynamic_ev_min(velocity_pct_min=0.10)   # meta' del riferimento
        assert got["ev_multiplier"] == 1.10
        assert "volatilita'" in got["dynamic_reason"]

    def test_volatilita_satura_a_un_extra(self):
        """Oltre il riferimento l'extra non cresce piu' (cap 1x)."""
        a = dynamic_ev_min(velocity_pct_min=0.20)["ev_multiplier"]
        b = dynamic_ev_min(velocity_pct_min=99.0)["ev_multiplier"]
        assert a == b == 1.20

    def test_le_due_cause_si_sommano(self):
        got = dynamic_ev_min(depth_usdc=5.0, velocity_pct_min=0.60)
        assert got["ev_multiplier"] == 1.45
        assert abs(got["ev_min"] - vf.EV_MIN * 1.45) < 1e-9

    def test_la_soglia_puo_solo_salire(self):
        for depth in (0.0, 1.0, 19.9, 20.0, 1000.0):
            for vel in (None, -3.0, 0.0, 0.05, 5.0):
                got = dynamic_ev_min(depth_usdc=depth, velocity_pct_min=vel)
                assert got["ev_min"] >= vf.EV_MIN - 1e-12

    def test_velocita_negativa_usa_il_valore_assoluto(self):
        """Un crollo (velocity negativa) e' movimento: conta come tale."""
        assert dynamic_ev_min(velocity_pct_min=-0.10)["ev_multiplier"] == 1.10

    def test_dato_illegibile_non_inventa_un_extra(self):
        assert dynamic_ev_min(depth_usdc="abc", velocity_pct_min="x")["ev_multiplier"] == 1.0


# ---------------------------------------------------------------------------
# 2. PORTFOLIO SHIELD
# ---------------------------------------------------------------------------

class TestShieldCap:
    def test_il_cap_e_quello_di_auto_bet(self):
        assert shield_cap_pct() == auto_bet.CORRELATION_CAP_PCT == 0.30


class TestShieldDecision:
    def test_nessuna_esposizione_sul_blocco_allow(self):
        got = shield_decision(league="Premier League", bankroll=100.0,
                              open_bets=[], min_ticket=2.0)
        assert got["action"] == "allow"
        assert got["factor"] == 1.0 and got["max_usdc"] is None

    def test_esposizione_parziale_scale(self):
        open_bets = [{"league": "Premier League", "stake": 25.0}]
        got = shield_decision(league="Premier League", bankroll=100.0,
                              open_bets=open_bets, min_ticket=2.0)
        assert got["action"] == "scale"
        assert got["max_usdc"] == 5.0            # cap 30 - 25
        assert abs(got["factor"] - round(5.0 / 30.0, 4)) < 1e-9

    def test_blocco_saturo_block(self):
        open_bets = [{"league": "Premier League", "stake": 30.0}]
        got = shield_decision(league="Premier League", bankroll=100.0,
                              open_bets=open_bets, min_ticket=2.0)
        assert got["action"] == "block" and got["max_usdc"] == 0.0
        assert "saturo" in got["reason"]

    def test_residuo_sotto_il_ticket_block(self):
        """Non si \"aggiusta\" lo stake a meta' ticket: l'ordine non parte."""
        open_bets = [{"league": "Premier League", "stake": 29.0}]
        got = shield_decision(league="Premier League", bankroll=100.0,
                              open_bets=open_bets, min_ticket=2.0)
        assert got["action"] == "block"
        assert "ticket minimo" in got["reason"]

    def test_altre_leghe_non_occupano_questo_blocco(self):
        open_bets = [{"league": "Serie A", "stake": 29.0}]
        got = shield_decision(league="Premier League", bankroll=100.0,
                              open_bets=open_bets, min_ticket=2.0)
        assert got["action"] == "allow"

    def test_leghe_canoniche_dello_stesso_blocco(self):
        """Il blocco e' la lega CANONICA: le varianti di nome non lo sfuggono."""
        open_bets = [{"league": "Major League Soccer", "stake": 25.0}]
        got = shield_decision(league="MLS", bankroll=100.0,
                              open_bets=open_bets, min_ticket=2.0)
        assert got["action"] == "scale" and got["max_usdc"] == 5.0

    def test_bankroll_non_disponibile_block(self):
        got = shield_decision(league="X", bankroll=0.0, open_bets=[],
                              min_ticket=2.0)
        assert got["action"] == "block" and got["factor"] == 0.0


# ---------------------------------------------------------------------------
# 3. CICLO DEL CERVELLO
# ---------------------------------------------------------------------------

class TestProcess:
    def _agent(self, bankroll=100.0, open_bets=None, min_ticket=2.0):
        return BrainAgent(bankroll=bankroll,
                          open_bets_fn=lambda: list(open_bets or []),
                          min_ticket_fn=lambda: min_ticket)

    def test_trade_validato_con_shield_allow(self):
        out = self._agent().process([_oracle(ev=0.10)])
        assert isinstance(out, BrainOutput)
        assert out.validated == 1 and out.rejected == 0
        trade = out.trades[0]
        assert trade.shield_action == "allow"
        assert trade.dynamic_ev_min == vf.EV_MIN
        assert trade.signal_id == "sx-1|1X2|1"

    def test_ev_sotto_la_soglia_dinamica_rifiutato(self):
        # depth 5 alza la soglia a 3.125%: EV 3.0% non passa piu'
        out = self._agent().process([_oracle(ev=0.030, depth=5.0)])
        assert out.trades == [] and out.rejected == 1

    def test_ev_pari_alla_soglia_dinamica_passa(self):
        out = self._agent().process([_oracle(ev=0.03125, depth=5.0)])
        assert out.validated == 1

    def test_ev_derivato_da_prob_e_quota(self):
        """Senza `ev` esplicito: p x quota - 1 (formula dell'EV, non una stima)."""
        s = _oracle(true_prob=0.65, price=1.66, ev=None)
        out = self._agent().process([s])
        assert out.validated == 1
        assert out.trades[0].ev == pytest.approx(0.65 * 1.66 - 1.0)

    def test_shield_scale_marca_il_trade(self):
        agent = self._agent(open_bets=[{"league": "Premier League",
                                        "stake": 25.0}])
        out = agent.process([_oracle(ev=0.10, league="Premier League")])
        assert out.scaled == 1 and out.validated == 0
        trade = out.trades[0]
        assert trade.shield_action == "scale"
        assert trade.shield_max_usdc == 5.0

    def test_shield_block_marca_il_trade(self):
        agent = self._agent(open_bets=[{"league": "Premier League",
                                        "stake": 30.0}])
        out = agent.process([_oracle(ev=0.10, league="Premier League")])
        assert out.blocked == 1
        assert out.trades[0].shield_action == "block"

    def test_lettura_ordini_rotta_fail_closed(self):
        """Senza sapere cosa e' aperto non si autorizza: il trade resta ma
        bloccato, col motivo dichiarato."""
        def boom():
            raise RuntimeError("db chiuso")
        agent = BrainAgent(bankroll=100.0, open_bets_fn=boom,
                           min_ticket_fn=lambda: 2.0)
        out = agent.process([_oracle(ev=0.10)])
        assert out.blocked == 1
        assert out.trades[0].shield_action == "block"
        assert "read_error:RuntimeError" in out.trades[0].shield_reason

    def test_senza_ev_non_c_e_una_soglia_da_confrontare(self):
        """EV non determinabile (ne' esplicito ne' da p x quota): il Cervello
        non inventa un confronto. Il trade nasce con EV None e sara' la
        Finanza a scartarlo (`no_true_prob`), non un verdetto inventato qui."""
        out = self._agent().process([_oracle(true_prob=None, ev=None)])
        assert out.validated == 1
        assert out.trades[0].ev is None

    def test_segnale_rotto_non_ferma_gli_altri(self):
        class _Rotto:
            signal_id = "x"
            @property
            def depth_usdc(self):
                raise ValueError("rotto")
        good = _oracle(match_id="sx-2", ev=0.10)
        out = self._agent().process([_Rotto(), good])
        assert [t.match_id for t in out.trades] == ["sx-2"]

    def test_output_serializzabile(self):
        out = self._agent().process([_oracle(ev=0.10)])
        assert out.as_json()["validated"] == 1


# ---------------------------------------------------------------------------
# 4. LETTURA DEGLI ORDINI APERTI (ledger, sola lettura)
# ---------------------------------------------------------------------------

class TestOrdiniAperti:
    def test_legge_solo_bet_live_non_saldate(self, monkeypatch):
        import tracker
        import tempfile as _tf
        with _tf.TemporaryDirectory() as td:
            monkeypatch.setattr(tracker, "DB_PATH", Path(td) / "t.db")
            tracker.init_db()
            tracker.save_match("sx-1", "Premier League", "Home", "Away",
                               datetime.now(timezone.utc).isoformat())
            tracker.save_bet(match_id="sx-1", mercato="1X2", esito="1",
                             market_id="0xm", selection_id=1, price=1.66,
                             stake=25.0, mode="live", status="FULLY_FILLED",
                             bet_id="0xb")
            tracker.save_bet(match_id="sx-2", mercato="1X2", esito="1",
                             market_id="0xm", selection_id=1, price=1.66,
                             stake=99.0, mode="sim", status="FULLY_FILLED",
                             bet_id="0xc")
            from agents.brain_agent import _open_live_bets
            rows = _open_live_bets()
            assert [r["stake"] for r in rows] == [25.0]
            assert rows[0]["league"] == "Premier League"   # dal JOIN su matches

    def test_bet_orfana_resta_visibile_col_blocco_vuoto(self, monkeypatch):
        """LEFT JOIN: una bet senza riga in `matches` non sparisce dal
        conteggio (altrimenti il recinto sottostimerebbe l'esposizione)."""
        import tracker
        import tempfile as _tf
        with _tf.TemporaryDirectory() as td:
            monkeypatch.setattr(tracker, "DB_PATH", Path(td) / "t.db")
            tracker.init_db()
            tracker.save_bet(match_id="sx-orfana", mercato="1X2", esito="1",
                             market_id="0xm", selection_id=1, price=1.66,
                             stake=7.0, mode="live", status="FULLY_FILLED",
                             bet_id="0xb")
            from agents.brain_agent import _open_live_bets
            rows = _open_live_bets()
            assert rows == [{"match_id": "sx-orfana", "league": "",
                             "stake": 7.0}]

    def test_banco_non_leggibile_non_autorizza(self, monkeypatch):
        """TRIPWIRE: la lettura del DB reale non puo' diventare "nessun ordine
        aperto" in silenzio. La roba e' fail-closed a monte (`_open_bets`
        cattura e dichiara), quindi qui si verifica solo che l'errore sia
        DICHIARATO e non inghiottito come lista vuota."""
        agent = BrainAgent(bankroll=100.0, open_bets_fn=lambda: 1 / 0,
                           min_ticket_fn=lambda: 2.0)
        rows, err = agent._open_bets()
        assert rows == [] and err == "read_error:ZeroDivisionError"
