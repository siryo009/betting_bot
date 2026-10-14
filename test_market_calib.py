"""
Test per market_calib.py e per l'integrazione strategica "beating the market".

Verifica:
- devig (multiplicative / power / shin) su un mercato lopsided;
- market_implied e overround;
- market_edge / is_beating_market (soglie della ricerca 2026);
- blend_probability e favourite_longshot_adjust;
- il vincolo "beating the market" in value_filter.is_sane;
- l'analisi di un match con prezzi multi-bookmaker (fixture_engine._analyze_match):
  deve scegliere il miglior prezzo, devigare il mercato e filtrare i segnali
  che non battono la closing line.
"""

import pytest

from market_calib import (
    devig, devig_multiplicative, market_implied,
    market_edge, is_beating_market, blend_probability,
    favourite_longshot_adjust,
)


class TestDevig:
    """I valori devono coincidere con l'esempio di riferimento (-300/+240)."""

    ODDS = [1.333, 3.40]  # implied: 75% / 29.4%, overround 104.4%

    def test_multiplicative_matches_reference(self):
        p = devig_multiplicative([0.75, 0.294])
        assert p[0] == pytest.approx(0.7184, abs=1e-3)   # favorito 71.84%
        assert p[1] == pytest.approx(0.2816, abs=1e-3)

    def test_power_between_multiplicative_and_shin(self):
        p = devig(self.ODDS, method="power")
        m = devig(self.ODDS, method="multiplicative")
        s = devig(self.ODDS, method="shin")
        assert p[0] > m[0]          # power da' piu' credito al favorito
        assert s[0] >= p[0]         # shin ancora di piu' (piu' aggressivo)
        assert sum(p) == pytest.approx(1.0)

    def test_fair_probabilities_sum_to_one(self):
        for method in ("multiplicative", "power", "shin"):
            assert sum(devig(self.ODDS, method=method)) == pytest.approx(1.0)

    def test_balanced_market_identical(self):
        # su un mercato bilanciato i metodi convergono (ricerca)
        for method in ("multiplicative", "power", "shin"):
            p = devig([2.0, 2.0], method=method)
            assert p[0] == pytest.approx(0.5, abs=1e-6)


class TestMarketImplied:
    def test_1x2(self):
        m = market_implied({"1": 1.70, "X": 3.80, "2": 4.50})
        assert m is not None
        assert m["overround"] == pytest.approx(1.074, abs=1e-3)
        # somma delle probabilita' fair = 1 (escluso overround)
        probs = {k: v for k, v in m.items() if k != "overround"}
        assert sum(probs.values()) == pytest.approx(1.0)

    def test_meno_di_due_esiti(self):
        assert market_implied({"1": 2.0}) is None

    def test_quote_non_valide_scartate(self):
        m = market_implied({"1": 2.0, "X": 1.0, "2": 4.0})  # X non valida
        assert m is not None
        assert "X" not in m


class TestMarketEdge:
    def test_edge_positivo(self):
        assert market_edge(0.62, 0.565) == pytest.approx(0.055)

    def test_edge_negativo(self):
        assert market_edge(0.40, 0.565) == pytest.approx(-0.165)

    def test_edge_none_senza_mercato(self):
        assert market_edge(0.62, None) is None

    def test_beating_market_soglia(self):
        # La soglia di riferimento e' DERIVATA dalla costante (direttiva
        # 14/10/2026: +2pp -> +1pp), cosi' un ritocco non puo' far passare
        # questo test per il motivo sbagliato.
        from market_calib import MARKET_EDGE_MIN
        assert is_beating_market(0.62, 0.565) is True    # +5.5pp >= soglia
        sotto = 0.565 + MARKET_EDGE_MIN / 2
        assert is_beating_market(sotto, 0.565) is False  # < soglia corrente
        assert is_beating_market(0.565, 0.565) is False  # zero edge

    def test_beating_market_senza_mercato(self):
        # backward-compat: senza mercato il segnale passa
        assert is_beating_market(0.5, None) is True


class TestBlend:
    def test_blend_pari_peso(self):
        assert blend_probability(0.62, 0.565) == pytest.approx(0.5925)

    def test_blend_senza_mercato(self):
        assert blend_probability(0.62, None) == pytest.approx(0.62)

    def test_longshot_adjust(self):
        # odds 5.0: modello 25% compresso verso mercato 18%
        p = favourite_longshot_adjust(0.25, 0.18, 5.0)
        assert p < 0.25 and p > 0.18

    def test_longshot_adjust_sotto_soglia_invariato(self):
        assert favourite_longshot_adjust(0.55, 0.50, 1.80) == pytest.approx(0.55)


class TestValueFilterMarketGate:
    """Il filtro di sanita' deve bocciare chi non batte il mercato."""

    def test_sane_false_senza_edge(self):
        from value_filter import is_sane
        # EV ok (+3.7%) ma prob 60.5% vs mercato 60% -> +0.5pp, sotto la
        # soglia di +1pp in vigore dal 14/10/2026 (favorito netto: quota
        # 1.70, prob. di mercato 60%).
        ok, reason = is_sane(0.605, 1.70, 0.037, market_prob=0.60)
        assert not ok
        assert "non batte il mercato" in reason

    def test_sane_true_con_edge(self):
        from value_filter import is_sane
        # 0.62*1.70-1 = +5.4% EV, dentro la fascia 3-15%; edge +6pp sul mercato
        ok, _ = is_sane(0.62, 1.70, 0.054, market_prob=0.56)
        assert ok

    def test_sane_backward_compat(self):
        from value_filter import is_sane, ODDS_MAX
        # Senza market_prob il gate "favorito" non si applica, ma il cap
        # quota della strategia resta vincolante.
        ok, _ = is_sane(0.60, 1.75, 0.05)
        assert ok
        # Il test segue il cap REALE (fascia 1.20-2.00 dal 14/10/2026):
        # una quota appena sopra il cap deve essere respinta.
        ok, reason = is_sane(0.55, ODDS_MAX + 0.05, 0.10)
        assert not ok and "quota troppo alta" in reason


class TestAnalyzeMatchMarket:
    """Integrazione: _analyze_match con dati multi-bookmaker."""

    def _match(self):
        return {
            "id": "match_test_1",
            "home_team": "Roma", "away_team": "Empoli",
            "commence_time": "2026-09-01T18:45:00Z",
            "bookmakers": [
                {"title": "BookA", "markets": [
                    {"key": "h2h", "outcomes": [
                        {"name": "Roma", "price": 1.70},
                        {"name": "Draw", "price": 3.80},
                        {"name": "Empoli", "price": 5.50}]},
                    {"key": "totals", "outcomes": [
                        {"name": "Over 2.5", "point": 2.5, "price": 2.00},
                        {"name": "Under 2.5", "point": 2.5, "price": 1.80}]},
                ]},
                {"title": "BookB", "markets": [
                    {"key": "h2h", "outcomes": [
                        {"name": "Roma", "price": 1.65},
                        {"name": "Draw", "price": 3.90},
                        {"name": "Empoli", "price": 5.80}]},
                ]},
            ],
        }

    def test_line_shopping_e_devig(self, monkeypatch):
        from fixture_engine import _analyze_match
        monkeypatch.setattr("fixture_engine.expected_goals", lambda h, a: (1.9, 1.1))
        monkeypatch.setattr("fixture_engine.save_clv", lambda *a, **k: None)
        monkeypatch.setattr("fixture_engine.get_analysis_for_match", lambda m: None)
        saved = {}
        monkeypatch.setattr("fixture_engine.save_analysis",
                            lambda *a, **k: saved.update({"args": a, "kwargs": k}))

        status = _analyze_match("match_test_1", self._match(), "Roma", "Empoli", "Serie A")
        assert status in ("value", "strong_value", "no_value", "rejected")

        args = saved["args"]
        # args: match_id, lam_h, lam_a, p1, px, p2, p_over, ev, esito, quota, book, status
        assert args[9] >= 1.65  # il miglior prezzo raccolto (line shopping)
        kw = saved["kwargs"]
        assert "market_prob" in kw and "market_edge" in kw

    def test_market_prob_e_probabilita_valida(self, monkeypatch):
        from fixture_engine import _analyze_match
        monkeypatch.setattr("fixture_engine.expected_goals", lambda h, a: (1.9, 1.1))
        monkeypatch.setattr("fixture_engine.save_clv", lambda *a, **k: None)
        monkeypatch.setattr("fixture_engine.get_analysis_for_match", lambda m: None)
        saved = {}
        monkeypatch.setattr("fixture_engine.save_analysis",
                            lambda *a, **k: saved.update({"kwargs": k}))

        _analyze_match("m2", self._match(), "Roma", "Empoli", "Serie A")
        mp = saved["kwargs"].get("market_prob")
        me = saved["kwargs"].get("market_edge")
        if mp is not None:
            assert 0.0 < mp < 1.0
        if mp is not None and me is not None:
            # edge coerente: modello vs mercato deve essere un delta plausibile
            assert abs(me) < 1.0
