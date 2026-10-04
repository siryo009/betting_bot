"""Test del MOTORE KELLY AGGRESSIVO (direttiva 04/10/2026).

Quattro cose che questa direttiva cambia rispetto al 28/09:

1. **k = 0.65** (non piu' Kelly dimezzato): il Kelly pieno viene scalato UNA
   volta sola, e la formula vive in `value_filter.kelly_fraction` (mai
   ricopiata qui).
2. **Cap DINAMICO**: `bankroll x 12%` al posto dell'importo fisso 1.50. Con
   bankroll 30 il tetto e' 3.60, con 100 e' 12.00 — il capitale scala col
   capitale (compounding).
3. **Ticket minimo 2.00 USDC** del MOTORE (sotto soglia lo stake e' 0.0,
   operazione scartata). Il floor dell'EXCHANGE resta 1.00: sono due numeri
   diversi con due significati diversi.
4. **Fetch del saldo fresco subito prima dell'ordine** (`refresh_live_stakes`).

Il conftest spegne il motore per gli altri test (lezione del 13/09: un solo
punto di verita' per la corsia): qui e' ACCESO, quindi questi test misurano il
comportamento che va in produzione.
"""

import auto_bet
import value_filter as vf
from decision import stake_engine as se


# ---------------------------------------------------------------------------
# 1. CONFIGURAZIONE (default + env + clamp)
# ---------------------------------------------------------------------------

class TestConfigurazione:
    def test_default_di_codice(self, monkeypatch):
        monkeypatch.delenv("KELLY_AGGRESSIVE_FRACTION", raising=False)
        monkeypatch.delenv("KELLY_MAX_STAKE_PCT", raising=False)
        monkeypatch.delenv("KELLY_MIN_TICKET_USDC", raising=False)
        cfg = se.aggressive_config()
        assert cfg["kelly_fraction"] == 0.65
        assert cfg["max_stake_pct"] == 0.12
        assert cfg["min_ticket"] == 2.00

    def test_env_tara_i_parametri(self, monkeypatch):
        monkeypatch.setenv("KELLY_AGGRESSIVE_FRACTION", "0.5")
        monkeypatch.setenv("KELLY_MAX_STAKE_PCT", "0.05")
        monkeypatch.setenv("KELLY_MIN_TICKET_USDC", "1.0")
        cfg = se.aggressive_config()
        assert cfg == {"kelly_fraction": 0.5, "max_stake_pct": 0.05,
                       "min_ticket": 1.0}

    def test_valore_impossibile_ricade_sul_default(self, monkeypatch):
        """Una soglia di rischio non si spegne con una variabile sbagliata."""
        monkeypatch.setenv("KELLY_AGGRESSIVE_FRACTION", "abc")
        monkeypatch.setenv("KELLY_MAX_STAKE_PCT", "")
        monkeypatch.setenv("KELLY_MIN_TICKET_USDC", "molto")
        cfg = se.aggressive_config()
        assert cfg["kelly_fraction"] == 0.65
        assert cfg["max_stake_pct"] == 0.12          # stringa vuota = default
        assert cfg["min_ticket"] == 2.00

    def test_frazioni_clampate(self, monkeypatch):
        monkeypatch.setenv("KELLY_AGGRESSIVE_FRACTION", "3.0")
        monkeypatch.setenv("KELLY_MAX_STAKE_PCT", "5.0")
        cfg = se.aggressive_config()
        assert cfg["kelly_fraction"] == 1.0
        assert cfg["max_stake_pct"] == 1.0


# ---------------------------------------------------------------------------
# 2. CALCULATE_KELLY_STAKE — la formula e i tre vincoli
# ---------------------------------------------------------------------------

class TestCalculateKellyStake:
    def test_kelly_scalato_una_volta(self):
        """k applicato UNA volta sola: raw = bankroll x kelly_pieno x k.

        Il bug del 13/09 nasceva dal doppio scaling. Qui il valore atteso e'
        verificato col Kelly pieno letto dalla STESSA fonte di produzione.
        """
        full = vf.kelly_fraction(0.60, 1.66, fraction=1.0)
        res = se.calculate_kelly_stake(0.60, 1.66, 30.0)
        assert res["kelly_full"] == full
        assert res["raw_stake"] == round(30.0 * full * 0.65, 6)
        assert res["kelly_fraction"] == 0.65

    def test_cap_dinamico_tronca(self):
        """Con bankroll alto il 12% vince sul Kelly: stake = cap, capped True."""
        res = se.calculate_kelly_stake(0.60, 1.66, 100.0)
        assert res["cap_usdc"] == 12.0
        assert res["capped"] is True
        assert res["stake"] == 12.0
        assert res["executable"] is True

    def test_cap_dinamico_sotto_il_kelly_non_tronca(self):
        """Quando il Kelly sta sotto il cap il cap NON tocca nulla."""
        # p=0.45 a quota 2.00: kelly pieno 0.175 -> raw 11.375 (< 12.00 di cap)
        res = se.calculate_kelly_stake(0.45, 2.00, 100.0)
        assert res["capped"] is False
        assert res["stake"] == round(res["raw_stake"], 2)

    def test_ticket_minimo_scarta_l_operazione(self):
        """Sotto il ticket del motore lo stake e' 0.0: mai un ordine piu'
        piccolo del ticket."""
        res = se.calculate_kelly_stake(0.60, 1.66, 10.0)   # cap 1.20 < 2.00
        assert res["stake"] == 0.0
        assert res["executable"] is False
        assert res["reason"] == "below_min_ticket"

    def test_sopra_il_ticket_passa(self):
        res = se.calculate_kelly_stake(0.60, 1.66, 20.0)   # cap 2.40 >= 2.00
        assert res["stake"] == 2.40
        assert res["executable"] is True
        assert res["reason"] == "ok"

    def test_compounding_sul_bankroll(self):
        """Il capitale scala col capitale: raddoppia il bankroll, raddoppia il
        tetto (e' il senso del cap dinamico rispetto all'importo fisso)."""
        a = se.calculate_kelly_stake(0.60, 1.66, 100.0)
        b = se.calculate_kelly_stake(0.60, 1.66, 200.0)
        assert a["cap_usdc"] == 12.0 and b["cap_usdc"] == 24.0
        assert b["stake"] == 2 * a["stake"]

    def test_motivi_machine_readable(self):
        assert se.calculate_kelly_stake("x", 1.66, 30.0)["reason"] == "invalid_inputs"
        assert se.calculate_kelly_stake(0.6, 1.66, 0.0)["reason"] == "no_bankroll"
        assert se.calculate_kelly_stake(0.6, 1.66, -5.0)["reason"] == "no_bankroll"
        assert se.calculate_kelly_stake(0.30, 1.66, 30.0)["reason"] == "no_edge"
        assert se.calculate_kelly_stake(0.6, 1.0, 30.0)["reason"] == "invalid_inputs"
        assert se.calculate_kelly_stake(0.0, 1.66, 30.0)["reason"] == "invalid_inputs"

    def test_override_esplicito_dei_parametri(self):
        res = se.calculate_kelly_stake(0.60, 1.66, 100.0,
                                       kelly_fraction=0.5,
                                       max_stake_pct=0.20,
                                       min_ticket=1.0)
        assert res["kelly_fraction"] == 0.5
        assert res["max_stake_pct"] == 0.20
        assert res["min_ticket"] == 1.0
        assert res["cap_usdc"] == 20.0
        assert res["raw_stake"] == round(100.0 * res["kelly_full"] * 0.5, 6)

    def test_sempre_un_dict_mai_eccezioni(self):
        for args in ((None, None, None), (0.6, "x", 30), ("a", 1.6, 30),
                     (0.6, 1.66, "b")):
            res = se.calculate_kelly_stake(*args)
            assert isinstance(res, dict) and res["stake"] == 0.0


# ---------------------------------------------------------------------------
# 3. aggressive_cap_usdc — UNICA fonte del tetto dinamico
# ---------------------------------------------------------------------------

class TestAggressiveCap:
    def test_e_il_12_percento(self):
        assert se.aggressive_cap_usdc(30.0) == round(30.0 * 0.12, 6)
        assert se.aggressive_cap_usdc(100.0) == 12.0

    def test_bankroll_ignoto_o_nullo_non_produce_un_tetto(self):
        assert se.aggressive_cap_usdc(0.0) == 0.0
        assert se.aggressive_cap_usdc(-3.0) == 0.0
        assert se.aggressive_cap_usdc("abc") == 0.0
        assert se.aggressive_cap_usdc(None) == 0.0


# ---------------------------------------------------------------------------
# 4. auto_bet.cap_order_stake — tetto per singolo ordine
# ---------------------------------------------------------------------------

class TestCapOrderStake:
    def test_cap_dal_bankroll_passato(self, monkeypatch):
        monkeypatch.setattr(auto_bet, "ORDER_MAX_STAKE_USDC", 0.0)
        assert auto_bet.cap_order_stake(20.0, 100.0) == 12.0   # tronca al 12% di 100
        assert auto_bet.cap_order_stake(2.0, 30.0) == 2.0      # sotto il cap 3.60

    def test_usa_l_ultimo_bankroll_registrato(self, monkeypatch):
        monkeypatch.setattr(auto_bet, "ORDER_MAX_STAKE_USDC", 0.0)
        auto_bet.set_last_bankroll(50.0)
        assert auto_bet.cap_order_stake(99.0) == 6.0           # 12% di 50

    def test_bankroll_ignoto_fail_closed(self, monkeypatch):
        """Un tetto che non si sa misurare NON autorizza denaro reale."""
        monkeypatch.setattr(auto_bet, "ORDER_MAX_STAKE_USDC", 0.0)
        monkeypatch.setattr(auto_bet, "_LAST_BANKROLL", 0.0)
        assert auto_bet.cap_order_stake(5.0) == 0.0

    def test_tetto_assoluto_esplicito_vince(self, monkeypatch):
        """Un ORDER_MAX_STAKE_USDC > 0 resta un tetto assoluto (diagnostica)."""
        monkeypatch.setattr(auto_bet, "ORDER_MAX_STAKE_USDC", 1.50)
        assert auto_bet.cap_order_stake(10.0, 100.0) == 1.50
        assert auto_bet.cap_order_stake(0.5, 100.0) == 0.5

    def test_non_alza_mai(self, monkeypatch):
        monkeypatch.setattr(auto_bet, "ORDER_MAX_STAKE_USDC", 0.0)
        assert auto_bet.cap_order_stake(0.1, 100.0) == 0.1


class TestOrderStake:
    def test_vincolo_di_cassa(self, monkeypatch):
        monkeypatch.setattr(auto_bet, "ORDER_MAX_STAKE_USDC", 0.0)
        monkeypatch.setattr(auto_bet, "FIXED_STAKE_USDC", 0.0)
        # cap 12.00 ma solo 5 USDC liberi: vince la cassa
        assert auto_bet.order_stake(10.0, spendable=5.0, bankroll=100.0) == 5.0

    def test_importo_fisso_legacy(self, monkeypatch):
        """`ORDER_FIXED_STAKE_USDC` > 0 ripristina la direttiva del 28/09."""
        monkeypatch.setattr(auto_bet, "FIXED_STAKE_USDC", 1.50)
        monkeypatch.setattr(auto_bet, "ORDER_MAX_STAKE_USDC", 0.0)
        assert auto_bet.order_stake(99.0, spendable=10.0) == 1.50
        assert auto_bet.order_stake(99.0, spendable=1.0) == 0.0   # non coperto


# ---------------------------------------------------------------------------
# 5. Probabilita' vera + size del pick
# ---------------------------------------------------------------------------

class TestTrueProbability:
    def test_oracolo_esplicito(self):
        assert auto_bet.true_probability({"p_true": 0.61}, 1.66) == 0.61

    def test_derivata_dall_ev(self):
        """p = (EV + 1) / quota: algebra dell'EV, non una stima inventata."""
        p = auto_bet.true_probability({"top_down_ev": 0.10}, 1.66)
        assert abs(p - (0.10 + 1.0) / 1.66) < 1e-9

    def test_ordine_delle_fonti(self):
        pick = {"p_true": 0.55, "top_down_ev": 0.99, "ev": 0.99}
        assert auto_bet.true_probability(pick, 1.66) == 0.55

    def test_none_senza_fonte(self):
        """Fail-closed: senza probabilita' non c'e' size."""
        assert auto_bet.true_probability({}, 1.66) is None
        assert auto_bet.true_probability({"p_true": None}, 1.66) is None

    def test_quota_non_valida(self):
        assert auto_bet.true_probability({"p_true": 0.6}, 1.0) is None
        assert auto_bet.true_probability({"p_true": 0.6}, "x") is None

    def test_probabilita_fuori_range(self):
        assert auto_bet.true_probability({"p_true": 1.5}, 1.66) is None
        assert auto_bet.true_probability({"p_true": 0.0}, 1.66) is None


class TestKellySizeForPick:
    def test_size_dal_motore(self):
        pick = {"match_id": "m1", "esito_key": "1", "p_true": 0.60}
        res = auto_bet.kelly_size_for_pick(pick, price=1.66, bankroll=100.0)
        assert res["stake"] == 12.0          # cap 12% = 12.00
        assert res["kelly_fraction"] == 0.65
        assert res["true_prob"] == 0.60

    def test_senza_probabilita_non_si_ordina(self):
        res = auto_bet.kelly_size_for_pick({}, price=1.66, bankroll=100.0)
        assert res == {"stake": 0.0, "reason": "no_true_prob"}

    def test_vincolo_di_cassa(self):
        pick = {"p_true": 0.60}
        res = auto_bet.kelly_size_for_pick(pick, price=1.66, bankroll=100.0,
                                           spendable=5.0)
        assert res["stake"] == 5.0 and res.get("cassa_capped") is True

    def test_cassa_sotto_ticket_scarta(self):
        """Anche col vincolo di cassa il ticket minimo vale."""
        pick = {"p_true": 0.60}
        res = auto_bet.kelly_size_for_pick(pick, price=1.66, bankroll=100.0,
                                           spendable=1.0)
        assert res["stake"] == 0.0
        assert res["reason"] == "below_min_ticket_cassa"


# ---------------------------------------------------------------------------
# 6. refresh_live_stakes — saldo fresco PRIMA dell'ordine
# ---------------------------------------------------------------------------

def _snapshot(available=50.0, exposure=0.0):
    return {"available": available, "exposure": exposure,
            "equity": available + exposure}


class TestRefreshLiveStakes:
    def test_wallet_non_leggibile_fail_closed(self, monkeypatch):
        monkeypatch.setattr(auto_bet, "_live_wallet_snapshot", lambda: None)
        kept, info = auto_bet.refresh_live_stakes([{"stake": 5.0}])
        assert kept == []
        assert info["ok"] is False
        assert info["reason"] == "wallet_non_leggibile"

    def test_ri_kelly_fresco_col_saldo(self, monkeypatch):
        monkeypatch.setattr(auto_bet, "_live_wallet_snapshot",
                            lambda: _snapshot(available=100.0))
        cand = [{"match_id": "m1", "esito_key": "1", "p_true": 0.60,
                 "price": 1.66, "stake": 1.0}]
        kept, info = auto_bet.refresh_live_stakes(cand)
        assert info["ok"] is True and info["equity"] == 100.0
        assert len(kept) == 1
        # cap 12% di 100 = 12.00, troncato dalla cassa libera (100)
        assert kept[0]["stake"] == 12.0
        assert kept[0]["kelly"]["max_stake_pct"] == 0.12

    def test_il_capitale_fresco_alimenta_il_compounding(self, monkeypatch):
        monkeypatch.setattr(auto_bet, "_live_wallet_snapshot",
                            lambda: _snapshot(available=200.0))
        cand = [{"match_id": "m1", "esito_key": "1", "p_true": 0.60,
                 "price": 1.66, "stake": 1.0}]
        kept, _ = auto_bet.refresh_live_stakes(cand)
        assert kept[0]["stake"] == 24.0     # 12% di 200

    def test_cap_di_portafoglio_non_viene_ri_kelly(self, monkeypatch):
        """corr_cap/total_cap decidono SE e QUANTO: il Kelly non li scavalca."""
        monkeypatch.setattr(auto_bet, "_live_wallet_snapshot",
                            lambda: _snapshot(available=100.0))
        cand = [{"match_id": "m1", "esito_key": "1", "p_true": 0.60,
                 "price": 1.66, "stake": 9.0, "corr_cap": True}]
        kept, _ = auto_bet.refresh_live_stakes(cand)
        assert kept[0]["stake"] == 9.0      # solo min(stake, cap 12.00)
        assert "kelly" not in kept[0]

    def test_scarta_sotto_il_ticket(self, monkeypatch):
        monkeypatch.setattr(auto_bet, "_live_wallet_snapshot",
                            lambda: _snapshot(available=10.0))   # cap 1.20
        cand = [{"match_id": "m1", "esito_key": "1", "p_true": 0.60,
                 "price": 1.66, "stake": 1.0}]
        kept, info = auto_bet.refresh_live_stakes(cand)
        assert kept == [] and info["skipped"] == 1

    def test_setta_l_ultimo_bankroll_per_il_tetto(self, monkeypatch):
        monkeypatch.setattr(auto_bet, "_live_wallet_snapshot",
                            lambda: _snapshot(available=100.0))
        auto_bet.set_last_bankroll(0.0)
        auto_bet.refresh_live_stakes([])
        assert auto_bet._LAST_BANKROLL == 100.0

    def test_cap_non_calcolabile_fail_closed(self, monkeypatch):
        """Cassa libera zero: nessun ordine, mai un cap inventato."""
        monkeypatch.setattr(auto_bet, "_live_wallet_snapshot",
                            lambda: _snapshot(available=0.0))
        kept, info = auto_bet.refresh_live_stakes([{"stake": 1.0}])
        assert kept == [] and info["reason"] == "cap_non_calcolabile"


# ---------------------------------------------------------------------------
# 7. Interruttore e precedenza della corsia
# ---------------------------------------------------------------------------

class TestInterruttore:
    def test_default_acceso(self, monkeypatch):
        monkeypatch.delenv("KELLY_AGGRESSIVE_ENABLED", raising=False)
        assert auto_bet.aggressive_enabled() is True

    def test_env_spegne(self, monkeypatch):
        for raw in ("0", "false", "no", "off"):
            monkeypatch.setenv("KELLY_AGGRESSIVE_ENABLED", raw)
            assert auto_bet.aggressive_enabled() is False, raw

    def test_lettura_a_ogni_giro(self, monkeypatch):
        monkeypatch.setenv("KELLY_AGGRESSIVE_ENABLED", "0")
        assert auto_bet.aggressive_enabled() is False
        monkeypatch.setenv("KELLY_AGGRESSIVE_ENABLED", "1")
        assert auto_bet.aggressive_enabled() is True

    def test_precedenza_importo_fisso_e_flat(self, monkeypatch):
        """L'importo fisso (28/09) e il flat hanno la priorita' sul motore:
        un solo posto decide la corsia, cosi' calcolo e refresh non divergono."""
        monkeypatch.delenv("KELLY_AGGRESSIVE_ENABLED", raising=False)
        monkeypatch.setattr(auto_bet, "FIXED_STAKE_USDC", 0.0)
        monkeypatch.setattr(auto_bet, "STAKE_MODE", "adaptive")
        assert auto_bet.aggressive_live_active() is True

        monkeypatch.setattr(auto_bet, "FIXED_STAKE_USDC", 1.50)
        assert auto_bet.aggressive_live_active() is False

        monkeypatch.setattr(auto_bet, "FIXED_STAKE_USDC", 0.0)
        monkeypatch.setattr(auto_bet, "STAKE_MODE", "flat")
        assert auto_bet.aggressive_live_active() is False

    def test_interruttore_spegne_la_corsia(self, monkeypatch):
        monkeypatch.setenv("KELLY_AGGRESSIVE_ENABLED", "0")
        monkeypatch.setattr(auto_bet, "FIXED_STAKE_USDC", 0.0)
        monkeypatch.setattr(auto_bet, "STAKE_MODE", "adaptive")
        assert auto_bet.aggressive_live_active() is False
