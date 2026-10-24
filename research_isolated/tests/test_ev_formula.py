"""Test OFFLINE della formula EV, del push, del settlement e dei costi.

Difendono le grandezze su cui poggia la decisione di riavvio LIVE
(``LIVE_RECOVERY_REPORT.md``):

1. ``compute_ev(p, q) = p*q - 1`` (value betting classico);
2. EV **push-aware** di OU/AH = ``p_win*(q-1) - p_lose`` — con il push che
   restituisce lo stake e vale 0, non una vincita;
3. il settlement del ledger (``tracker._prediction_outcome``) coerente con
   quella formula, comprese le linee intere (push) e le quarter line (split);
4. l'assenza di commissione nel P/L registrato (SX Bet: quota piena);
5. la differenza fra EV calcolato sul **prezzo di segnale** e sul **prezzo di
   fill** (se il fill e' migliore l'EV e' sottostimato: conservativo).

Tutto offline: nessuna rete, nessun DB, nessun credito, nessun ordine.
"""

from __future__ import annotations

import math

import pytest

import multi_market as mm
import poisson_engine as pe
import tracker
from research_isolated.lab.ou_settlement import ou_outcome as ou_quarter_outcome
from research_isolated.lab.ou_settlement import ou_quarter_pnl
from value_filter import compute_ev


# ---------------------------------------------------------------------------
# 1. EV classico
# ---------------------------------------------------------------------------

class TestEvClassico:
    def test_formula(self):
        assert compute_ev(0.55, 1.90) == pytest.approx(0.045)

    def test_break_even_e_a_prob_1_su_quota(self):
        q = 1.87
        assert compute_ev(1.0 / q, q) == pytest.approx(0.0, abs=1e-12)

    def test_sotto_break_even_e_negativo(self):
        assert compute_ev(0.50, 1.80) < 0

    def test_coerente_con_la_definizione_di_puntata(self):
        """EV = p*vincita - (1-p)*perdita, a stake 1."""
        p, q = 0.42, 2.60
        atteso = p * (q - 1) - (1 - p)
        assert compute_ev(p, q) == pytest.approx(atteso, abs=1e-12)


# ---------------------------------------------------------------------------
# 2. EV push-aware (OU / AH)
# ---------------------------------------------------------------------------

class TestEvPushAware:
    def test_con_p_push_zero_le_due_formule_coincidono(self):
        p_win, p_push, q = 0.58, 0.0, 1.95
        p_lose = 1.0 - p_win - p_push
        assert (p_win * (q - 1) - p_lose) == pytest.approx(p_win * q - 1.0)

    def test_identita_con_la_prob_efficace(self):
        """EV push-aware vs (p_win + p_push/2)*q - 1.

        Con push>0 le due grandezze differiscono di ``p_push*(q/2 - 1)``:
        e' l'identita' che spiega perche' ``ev`` e ``prob*quota-1`` NON
        possono essere confrontati alla leggera su linee intere (l'artefatto
        del 14/10 nasceva proprio dal confondere le due).
        """
        p_win, p_push, q = 0.30, 0.20, 2.10
        p_lose = 1.0 - p_win - p_push
        p_eff = p_win + 0.5 * p_push
        ev = p_win * (q - 1.0) - p_lose
        assert (p_eff * q - 1.0) - ev == pytest.approx(p_push * (q / 2.0 - 1.0))

    def test_con_p_push_zero_l_identita_si_annulla(self):
        p_win, q = 0.58, 1.95
        p_lose = 1.0 - p_win
        assert (p_win * q - 1.0) - (p_win * (q - 1.0) - p_lose) == pytest.approx(0.0)

    def test_ou_linea_mezza_non_ha_push(self):
        p_win, p_push, p_lose = pe.ou_outcome_probs(1.45, 1.10, 2.5, "over")
        assert p_push == 0.0
        assert p_win + p_lose == pytest.approx(1.0)
        assert compute_ev(p_win, 1.90) == pytest.approx(p_win * 1.90 - 1.0)

    def test_ou_linea_intera_ha_push(self):
        p_win, p_push, p_lose = pe.ou_outcome_probs(1.45, 1.10, 3.0, "over")
        assert p_push > 0.0
        assert p_win + p_push + p_lose == pytest.approx(1.0)

    def test_ou_quarter_line_push_pesato_meta(self):
        """Quarter line = due mezze puntate: push solo dalla mezza intera."""
        p_win_q, p_push_q, p_lose_q = pe.ou_outcome_probs(1.45, 1.10, 2.75, "over")
        p_win_p, p_push_p, p_lose_p = pe.ou_outcome_probs(1.45, 1.10, 2.50, "over")
        p_win_i, p_push_i, p_lose_i = pe.ou_outcome_probs(1.45, 1.10, 3.00, "over")
        # I due lati della quarter sono 2.5 e 3.0, ciascuno a mezza puntata.
        assert p_win_q == pytest.approx(0.5 * (p_win_p + p_win_i))
        assert p_push_q == pytest.approx(0.5 * p_push_i)
        assert p_lose_q == pytest.approx(0.5 * (p_lose_p + p_lose_i))
        assert p_win_q + p_push_q + p_lose_q == pytest.approx(1.0)

    def test_ah_linea_intera_ha_push(self):
        p_win, p_push, p_lose = pe.ah_outcome_probs(1.40, 1.10, -1.0, "home")
        assert p_push > 0.0
        assert p_win + p_push + p_lose == pytest.approx(1.0)

    def test_ah_quarter_split_coerente_con_le_metà(self):
        q_win, q_push, q_lose = pe.ah_outcome_probs(1.40, 1.10, -0.75, "home")
        h_win, h_push, h_lose = pe.ah_outcome_probs(1.40, 1.10, -0.5, "home")
        i_win, i_push, i_lose = pe.ah_outcome_probs(1.40, 1.10, -1.0, "home")
        assert q_win == pytest.approx(0.5 * (h_win + i_win))
        assert q_push == pytest.approx(0.5 * (h_push + i_push))
        assert q_lose == pytest.approx(0.5 * (h_lose + i_lose))

    def test_ev_non_puo_superare_il_payout_massimo(self):
        p_win, _p_push, q = 0.05, 0.0, 18.6
        ev = p_win * (q - 1.0) - (1.0 - p_win)
        assert ev < 0
        assert ev <= (q - 1.0)


# ---------------------------------------------------------------------------
# 3. Settlement: push, linee intere, quarter, AH
# ---------------------------------------------------------------------------

class TestOuReferenceImplementation:
    """La specifica eseguibile della quarter line (riferimento indipendente)."""

    def test_linea_mezza_e_soglia_secca(self):
        assert ou_quarter_pnl("Over", 2.5, 3, 1.90) == pytest.approx(0.9)
        assert ou_quarter_pnl("Over", 2.5, 2, 1.90) == pytest.approx(-1.0)

    def test_linea_intera_ha_push(self):
        assert ou_quarter_pnl("Over", 3.0, 3, 1.90) == pytest.approx(0.0)
        assert ou_quarter_pnl("Under", 3.0, 3, 1.90) == pytest.approx(0.0)
        assert ou_quarter_outcome("Over", 3.0, 3) == "push"

    def test_quarter_su_totale_uguale_alla_mezza_intera(self):
        # Over 3.25 = 3.0 (push) + 3.5 (persa) con 3 gol -> -0.5
        assert ou_quarter_pnl("Over", 3.25, 3, 1.90) == pytest.approx(-0.5)
        # Under 3.25 con 3 gol: mezza 3.0 push, mezza 3.5 vinta -> 0.5*(1.90-1).
        assert ou_quarter_pnl("Under", 3.25, 3, 1.90) == pytest.approx(0.45)

    def test_coerenza_con_la_media_delle_meta(self):
        """La quarter line vale esattamente la media delle sue due mezze."""
        for line, lo in ((1.25, 1.0), (2.75, 2.5), (3.75, 3.5)):
            for total in range(0, 7):
                for side in ("over", "under"):
                    meta = 0.5 * (ou_quarter_pnl(side, lo, total, 1.95)
                                  + ou_quarter_pnl(side, lo + 0.5, total, 1.95))
                    intero = ou_quarter_pnl(side, line, total, 1.95)
                    assert intero == pytest.approx(meta)

    def test_side_non_valido_solleva(self):
        with pytest.raises(ValueError):
            ou_quarter_pnl("draw", 2.5, 3, 1.9)


class TestSettlement:
    def test_ou_linea_intera_push(self):
        assert tracker._prediction_outcome("OU", "Over 3", 1.90, 2, 1, "A", "B") == ("push", 0.0)
        assert tracker._prediction_outcome("OU", "Under 3", 1.90, 2, 1, "A", "B") == ("push", 0.0)

    def test_ou_linea_mezza_non_ha_push(self):
        assert tracker._prediction_outcome("OU", "Over 2.5", 1.90, 1, 1, "A", "B") == ("lost", -1.0)
        assert tracker._prediction_outcome("OU", "Over 2.5", 1.90, 2, 1, "A", "B") == ("won", 0.9)

    def test_ou_quarter_line_reference_implementation(self):
        """La regola CORRETTA su una quarter line (riferimento indipendente).

        2.75 = metà a 2.5 e metà a 3.0; con 3 gol la metà 2.5 vince e la metà
        3.0 push -> mezzo payout, non un payout pieno.
        """
        assert ou_quarter_pnl("Over", 2.75, 3, 2.00) == pytest.approx(0.5)
        assert ou_quarter_pnl("Under", 2.75, 3, 2.00) == pytest.approx(-0.5)
        assert ou_quarter_pnl("Over", 2.75, 4, 2.00) == pytest.approx(1.0)
        assert ou_quarter_pnl("Over", 2.75, 2, 2.00) == pytest.approx(-1.0)

    @pytest.mark.xfail(strict=True, reason=(
        "BUG APERTO (audit 20/10): `tracker.ou_won` tratta la quarter line "
        "come una soglia secca, quindi paga il payout PIENO dove spetta "
        "mezzo + push. Se questo test passa, il settlement e' stato "
        "corretto: aggiornare il test e il report."))
    def test_ou_quarter_line_settlement_di_produzione(self):
        out, profit = tracker._prediction_outcome("OU", "Over 2.75", 2.00, 2, 1, "A", "B")
        assert out == "won"
        assert profit == pytest.approx(0.5)

    def test_ah_linea_intera_push(self):
        # Home -1 con vittoria per 1 -> push.
        assert tracker._prediction_outcome("AH", "Home -1", 1.90, 2, 1, "A", "B") == ("push", 0.0)
        # Away +1 con sconfitta per 1 -> push.
        assert tracker._prediction_outcome("AH", "Away +1", 1.90, 2, 1, "A", "B") == ("push", 0.0)

    def test_ah_quarter_mezza_vinta_mezza_persa(self):
        # Home -0.75 con vittoria di 1: metà a -0.5 vinta, metà a -1.0 push.
        out, profit = tracker._prediction_outcome("AH", "Home -0.75", 1.90, 1, 0, "A", "B")
        assert out == "won"
        assert profit == pytest.approx(0.45)

    def test_ah_quarter_split_negativo(self):
        # Away +0.75 con sconfitta di 1: metà a +0.5 persa, metà a +1.0 push.
        out, profit = tracker._prediction_outcome("AH", "Away +0.75", 1.90, 1, 0, "A", "B")
        assert out == "lost"
        assert profit == pytest.approx(-0.5)

    def test_esito_1x2_nome_squadra(self):
        assert tracker._prediction_outcome("1X2", "Osasuna", 1.65, 2, 1, "Osasuna", "Getafe") == ("won", 0.65)
        assert tracker._prediction_outcome("1X2", "1", 1.65, 0, 0, "A", "B") == ("lost", -1.0)


# ---------------------------------------------------------------------------
# 4. Commissioni: il P/L registrato non le applica (SX Bet = quota piena)
# ---------------------------------------------------------------------------

class TestCommissioni:
    def test_vincita_al_lordo_della_quota(self):
        out, profit = tracker._prediction_outcome("1X2", "1", 1.87, 2, 0, "A", "B")
        assert out == "won"
        assert profit == pytest.approx(0.87)          # (quota - 1) * stake

    def test_con_commissione_il_pareggio_sarebbe_piu_alto(self):
        """Documenta l'ipotesi: con fee f, break-even = (1+f)/q > 1/q."""
        q, fee = 1.87, 0.02
        breakeven_senza = 1.0 / q
        breakeven_con = (1.0 + fee) / q
        assert breakeven_con > breakeven_senza
        # Coerenza col settlement: a p = breakeven_senza l'EV e' 0.
        assert compute_ev(breakeven_senza, q) == pytest.approx(0.0, abs=1e-12)

    def test_il_settlement_non_ha_una_voce_commissione(self):
        """Il P/L di una vincita e' esattamente (quota-1): nessuna fee sottratta."""
        for q in (1.30, 1.75, 2.40):
            _out, profit = tracker._prediction_outcome("1X2", "1", q, 3, 0, "A", "B")
            assert profit == pytest.approx(q - 1.0)


# ---------------------------------------------------------------------------
# 5. Prezzo: EV sul prezzo di segnale vs prezzo di fill
# ---------------------------------------------------------------------------

class TestPrezzo:
    def test_fill_migliore_aumenta_lev(self):
        p, segnale, fill = 0.58, 1.85, 1.95
        ev_segnale = compute_ev(p, segnale)
        ev_fill = compute_ev(p, fill)
        assert ev_fill > ev_segnale

    def test_ev_calcolato_sul_segnale_e_conservativo(self):
        """Se il fill e' >= al prezzo del segnale, l'EV vero e' >= quello usato."""
        ev_usato = compute_ev(0.58, 1.85)
        ev_vero = compute_ev(0.58, 1.95)
        assert ev_vero >= ev_usato

    def test_fill_peggiore_non_puo_passare_il_floor(self):
        """Il floor EV e' 'fill >= prezzo del segnale': con fill peggiore si salta."""
        segnale, fill = 1.85, 1.70
        assert fill < segnale            # condizione di skip nel percorso d'ordine
        assert compute_ev(0.58, fill) < compute_ev(0.58, segnale)


# ---------------------------------------------------------------------------
# 6. Regressione: analyze_fixture coerente e orientato MALE (bug del 14/10)
# ---------------------------------------------------------------------------

def _quote(market_type, line, selection, price, liquidity=500.0, key="L"):
    return {"market_type": market_type, "line": line, "line_key": key,
            "selection": selection, "price": price, "liquidity": liquidity,
            "main_line": 1}


class TestAnalyzeFixtureCoerenza:
    """La riga reale 75062 ('Away -2.5' quota 18.6, EV +12.78) e' il caso da
    NON riprodurre: con prob di mercato ~4% un EV positivo e' impossibile."""

    def _cands(self):
        quotes = [
            _quote("OU", 2.5, "over", 1.95, key="2.5"),
            _quote("OU", 2.5, "under", 1.90, key="2.5"),
            _quote("OU", 3.0, "over", 2.40, key="3"),
            _quote("OU", 3.0, "under", 1.58, key="3"),
            _quote("AH", -0.5, "1", 1.78, key="-0.5"),
            _quote("AH", -0.5, "2", 2.10, key="-0.5"),
            _quote("AH", -2.5, "1", 6.50, key="-2.5"),
            _quote("AH", -2.5, "2", 1.12, key="-2.5"),
        ]
        return mm.analyze_fixture("fx-test", 2.25, 0.78, quotes=quotes,
                                  league="Serie B")

    def test_ev_e_sempre_la_formula_push_aware(self):
        cands = self._cands()
        assert cands, "nessun candidato: il test non misurerebbe nulla"
        for c in cands:
            atteso = c["p_win"] * (c["quota"] - 1.0) - c["p_lose"]
            assert c["ev"] == pytest.approx(atteso, abs=1e-12)

    def test_probabilita_sommano_a_uno(self):
        for c in self._cands():
            assert c["p_win"] + c["p_push"] + c["p_lose"] == pytest.approx(1.0)

    def test_linea_forte_del_favorito_non_e_un_super_ev(self):
        """'Away -2.5' con la casa favorita: p_win piccola -> EV NON enorme."""
        cands = [c for c in self._cands()
                 if c["market_type"] == "AH" and c["esito_key"].lower().startswith("away")
                 and abs(c["line"] + 2.5) < 1e-9]
        assert cands
        for c in cands:
            assert c["p_win"] < 0.10, "orientamento del lato sospetto"
            assert c["ev"] < 0, "un EV positivo qui e' l'artefatto del 14/10"

    def test_il_lato_favorito_della_linea_e_coerente(self):
        """Sulla linea -0.5 il lato 'Home' deve avere p_win > 0.5 (casa forte)."""
        cands = [c for c in self._cands()
                 if c["market_type"] == "AH" and abs(c["line"] + 0.5) < 1e-9
                 and c["selection"] == "1"]
        assert cands
        assert cands[0]["p_win"] > 0.5

    def test_ev_di_una_linea_intera_non_e_la_prob_efficace(self):
        """Su una linea intera con push, `ev` != `prob*quota - 1` (di progetto)."""
        cands = [c for c in self._cands() if c["market_type"] == "OU"
                 and abs(c["line"] - 3.0) < 1e-9]
        assert cands
        for c in cands:
            if c["p_push"] > 0:
                assert c["ev"] != pytest.approx(c["prob"] * c["quota"] - 1.0)

    def test_nessun_ev_matematicamente_impossibile(self):
        for c in self._cands():
            assert -1.0 <= c["ev"] <= c["quota"] - 1.0 + 1e-9
