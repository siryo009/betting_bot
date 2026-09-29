"""
Test unitari per value_filter.py (API attuale: lista di dict).

Verifica:
- compute_ev isolato con casi edge;
- filter_value_bets identifichi value bet con EV >= soglia e filtri di sanita';
- threshold configurabile;
- gestione lista vuota;
- STRATEGIA SOLO FAVORITI (11/09): cap quota 1.80 + esito favorito di
  mercato (prob. devigata >= 50%), con eligible_favourites come gate.
"""

import pytest

from value_filter import compute_ev, filter_value_bets


class TestComputeEv:
    """Test della funzione pura compute_ev."""

    def test_ev_positivo(self):
        assert compute_ev(0.50, 2.20) == pytest.approx(0.10)

    def test_ev_negativo(self):
        assert compute_ev(0.30, 2.00) == pytest.approx(-0.40)

    def test_ev_zero(self):
        assert compute_ev(0.50, 2.00) == pytest.approx(0.0)

    def test_ev_alta_probabilita(self):
        assert compute_ev(0.80, 1.50) == pytest.approx(0.20)


class TestFilterValueBets:
    """Test del filtro value bet su dataset Serie A mock (API dict)."""

    @pytest.fixture
    def odds_data(self):
        """Quote con probabilita' gia' stimate dal modello.

        Solo FAVORITI NETTI (quota <= 1.80): dal 11/09/2026 qualsiasi quota
        superiore e' fuori strategia e viene scartata a monte.
        """
        return [
            {"bookmaker": "Bet365", "evento": "Serie A – Roma vs Empoli", "sport": "calcio",
             "esito": "1", "quota_decimale": 1.75, "probabilita": 0.620,
             "timestamp": "2024-09-17T15:00:00Z"},
            {"bookmaker": "Snai", "evento": "Serie A – Roma vs Empoli", "sport": "calcio",
             "esito": "2", "quota_decimale": 4.20, "probabilita": 0.220,
             "timestamp": "2024-09-17T15:00:00Z"},
            {"bookmaker": "Bet365", "evento": "Serie A – Inter vs Milan", "sport": "calcio",
             "esito": "1", "quota_decimale": 1.55, "probabilita": 0.720,
             "timestamp": "2024-09-16T20:45:00Z"},
            {"bookmaker": "Snai", "evento": "Serie A – Inter vs Milan", "sport": "calcio",
             "esito": "X", "quota_decimale": 3.60, "probabilita": 0.170,
             "timestamp": "2024-09-16T20:45:00Z"},
            {"bookmaker": "Bet365", "evento": "Serie A – Atalanta vs Milan", "sport": "calcio",
             "esito": "1", "quota_decimale": 1.70, "probabilita": 0.660,
             "timestamp": "2024-09-15T18:00:00Z"},
            {"bookmaker": "Snai", "evento": "Serie A – Atalanta vs Milan", "sport": "calcio",
             "esito": "2", "quota_decimale": 3.40, "probabilita": 0.220,
             "timestamp": "2024-09-15T18:00:00Z"},
            {"bookmaker": "Snai", "evento": "Serie A – Sassuolo vs Napoli", "sport": "calcio",
             "esito": "2", "quota_decimale": 1.60, "probabilita": 0.600,
             "timestamp": "2024-09-14T20:45:00Z"},
            {"bookmaker": "Snai", "evento": "Serie A – Juventus vs Milan", "sport": "calcio",
             "esito": "X", "quota_decimale": 1.75, "probabilita": 0.500,
             "timestamp": "2024-09-14T20:45:00Z"},
            {"bookmaker": "Bet365", "evento": "Serie A – Juventus vs Milan", "sport": "calcio",
             "esito": "1", "quota_decimale": 1.80, "probabilita": 0.580,
             "timestamp": "2024-09-14T20:45:00Z"},
        ]

    def _has(self, result, evento, esito):
        return any(r.get("evento") == evento and r.get("esito") == esito for r in result)

    def test_identifica_almeno_tre_value_bet(self, odds_data):
        result = filter_value_bets(odds_data, ev_threshold=0.05)
        assert len(result) >= 3

    def test_value_bet_corretti(self, odds_data):
        result = filter_value_bets(odds_data, ev_threshold=0.05)
        assert self._has(result, "Serie A – Roma vs Empoli", "1")
        assert self._has(result, "Serie A – Inter vs Milan", "1")
        assert self._has(result, "Serie A – Atalanta vs Milan", "1")

    def test_nessun_esito_con_quota_alta(self, odds_data):
        """STRATEGIA SOLO FAVORITI: nessuna quota > 1.80 passa il filtro."""
        from value_filter import ODDS_MAX
        result = filter_value_bets(odds_data, ev_threshold=0.02)
        assert all(r["quota_decimale"] <= ODDS_MAX for r in result)

    def test_scarta_ev_minore_uguale_soglia(self, odds_data):
        result = filter_value_bets(odds_data, ev_threshold=0.05)
        assert not self._has(result, "Serie A – Sassuolo vs Napoli", "2")
        assert not self._has(result, "Serie A – Juventus vs Milan", "X")
        assert not self._has(result, "Serie A – Juventus vs Milan", "1")

    def test_threshold_configurabile(self, odds_data):
        result = filter_value_bets(odds_data, ev_threshold=0.12)
        assert len(result) == 1
        assert result[0]["evento"] == "Serie A – Atalanta vs Milan"
        assert result[0]["esito"] == "1"

    def test_campi_output_corretti(self, odds_data):
        result = filter_value_bets(odds_data, ev_threshold=0.05)
        required = {"sport", "evento", "esito", "quota_decimale",
                    "probabilita", "ev", "timestamp"}
        assert result
        for r in result:
            assert required.issubset(set(r.keys()))

    def test_ordinamento_ev_decrescente(self, odds_data):
        result = filter_value_bets(odds_data, ev_threshold=0.05)
        evs = [r["ev"] for r in result]
        assert evs == sorted(evs, reverse=True)

    def test_lista_vuota_se_nessun_match(self):
        result = filter_value_bets([], ev_threshold=0.05)
        assert result == []


class TestStrategiaSoloFavoriti:
    """Gate 11/09/2026: vietato puntare su sfavorite/quote alte."""

    def test_tripwire_soglie(self):
        """Le soglie non devono tornare indietro senza una decisione esplicita."""
        import value_filter as vf
        from market_calib import MARKET_EDGE_MIN
        assert vf.ODDS_MAX == 1.80         # favoriti NETTI (guardrail 11/09)
        assert vf.ODDS_MIN >= 1.30         # fascia 1.30-1.80 (esclude pantano)
        assert MARKET_EDGE_MIN == 0.02     # edge minimo +2pp (direttiva 21/09)
        assert vf.FAVOURITES_ONLY is True
        assert vf.MIN_FAVOURITE_MARKET_PROB == 0.50
        # Test funzioni nuove
        assert hasattr(vf, "dynamic_kelly")
        assert hasattr(vf, "detect_odds_movement")
        assert hasattr(vf, "get_optimal_timing")
        assert hasattr(vf, "calculate_exposure")

    def test_quota_alta_bocciata(self):
        from value_filter import is_sane
        # EV ottimo (+14%) ma quota 2.50: fuori strategiaa
        ok, reason = is_sane(0.57, 2.50, 0.14, market_prob=0.52)
        assert not ok and "quota troppo alta" in reason

    def test_sfavorita_bocciata_anche_a_quota_bassa(self):
        from value_filter import is_sane
        # Quota 1.70 ma il mercato la considera sfavorita (40%)
        ok, reason = is_sane(0.60, 1.70, 0.02, market_prob=0.40)
        assert not ok and "favorito" in reason

    def test_favorito_netto_ammesso(self):
        from value_filter import is_sane
        ok, _ = is_sane(0.62, 1.65, 0.023, market_prob=0.57)
        assert ok

    def test_eligible_favourites_solo_il_piu_probabile(self):
        from value_filter import eligible_favourites
        cands = [
            {"esito": "1", "quota": 1.65, "market_prob": 0.60},
            {"esito": "X", "quota": 3.80, "market_prob": 0.24},
            {"esito": "2", "quota": 5.50, "market_prob": 0.16},
        ]
        out = eligible_favourites(cands)
        assert [c["esito"] for c in out] == ["1"]

    def test_eligible_favourites_vuoto_senza_favorito(self):
        from value_filter import eligible_favourites
        # Favorito di mercato ma quota sopra il cap -> nessun candidato
        cands = [
            {"esito": "1", "quota": 2.10, "market_prob": 0.47},
            {"esito": "X", "quota": 3.20, "market_prob": 0.28},
            {"esito": "2", "quota": 3.40, "market_prob": 0.25},
        ]
        assert eligible_favourites(cands) == []

    def test_eligible_favourites_vuoto_sotto_50(self):
        from value_filter import eligible_favourites, favourites_gate_reason
        # Nessun esito e' il favorito netto (max 45%)
        cands = [
            {"esito": "1", "quota": 1.75, "market_prob": 0.45},
            {"esito": "X", "quota": 3.10, "market_prob": 0.30},
        ]
        assert eligible_favourites(cands) == []
        assert "favorito" in favourites_gate_reason()

    def test_eligible_favourites_senza_market_prob(self):
        from value_filter import eligible_favourites
        # Senza prob. di mercato non si puo' dimostrare che sia il favorito
        assert eligible_favourites([{"esito": "1", "quota": 1.60}]) == []


class TestAdjustedProbability:
    """PATCH CALIBRAZIONE bucket bassi (06/09): sotto LOW_PROB_THRESHOLD
    la probabilita' finale viene compressa verso il mercato; sopra soglia
    resta invariata. Misurata sul backtest: closing -6.11 -> -3.08%."""

    def test_compressione_sotto_soglia(self):
        from value_filter import adjusted_probability, LOW_PROB_SHRINK
        p = adjusted_probability(0.35, 0.30, 2.40)
        # blend (peso default) + FL (odds <= 2.5, nessuna modifica) +
        # compressione bassa: piu' vicino al mercato dell'originale
        assert p < 0.35
        assert p > 0.30  # mai sotto il mercato
        # compressione applicata = deviazione * LOW_PROB_SHRINK
        assert 0.30 + (p - 0.30) / LOW_PROB_SHRINK > 0.30

    def test_sopra_soglia_invariato(self):
        from value_filter import adjusted_probability
        p = adjusted_probability(0.45, 0.40, 2.0)
        # sopra LOW_PROB_THRESHOLD nessuna compressione aggiuntiva
        assert 0.40 <= p <= 0.45

    def test_senza_mercato_nessuna_compressione(self):
        from value_filter import adjusted_probability
        assert adjusted_probability(0.30, None, 2.40) == pytest.approx(0.30)


class TestStrategiaPerLega:
    """Verifica che la strategia filtri per campionato."""

    def test_league_vincente_ammessa(self):
        from value_filter import league_allowed
        assert league_allowed("Premier League") is True
        assert league_allowed("Bundesliga") is True
        assert league_allowed("Ligue 1") is True
        assert league_allowed("Turkey Super Lig") is True

    def test_league_perdente_bloccata(self):
        from value_filter import league_allowed
        assert league_allowed("Serie A") is False
        assert league_allowed("La Liga") is False
        assert league_allowed("Greek Super Lig") is False
        assert league_allowed("Liga Portugal") is False

    def test_league_desconosciuta_bloccata(self):
        from value_filter import league_allowed
        assert league_allowed("") is False
        assert league_allowed("Unknown League") is False

    def test_tier2_probation_ammessa_con_parametri_severi(self):
        """Tier-2 (21/09): leghe reali ma senza storico positivo -> ammesse
        con strategia PIU' SEVERA del core (edge +4pp, Kelly 0.4, cap 0.5%)."""
        import value_filter
        from value_filter import (league_allowed, league_tier,
                                  get_league_strategy, PROBATION_STRATEGY)
        for lg in ("EFL Championship", "Serie B", "MLS", "Brasileirao",
                   "Argentina Primera", "Eliteserien", "Liga MX",
                   "J1 League", "Saudi Pro League", "K League 1"):
            assert league_allowed(lg) is True, lg
            assert league_tier(lg) == "probation", lg
            assert get_league_strategy(lg) == PROBATION_STRATEGY
        # La severita' e' un INVARIANTE, non una coincidenza.
        assert PROBATION_STRATEGY["min_edge"] > max(
            s["min_edge"] for s in value_filter.STRATEGY_LEAGUES.values())
        assert PROBATION_STRATEGY["max_stake"] < min(
            s["max_stake"] for s in value_filter.STRATEGY_LEAGUES.values())
        assert PROBATION_STRATEGY["kelly_mult"] < min(
            s["kelly_mult"] for s in value_filter.STRATEGY_LEAGUES.values())

    def test_leghe_misurate_negative_restano_vietate(self):
        """Escluse per decisione esplicita: aggiungere volume a EV negativo."""
        from value_filter import league_allowed, league_tier, PROBATION_LEAGUES
        for lg in ("Serie A", "La Liga", "Belgian Pro League",
                   "Liga Portugal", "Greek Super League"):
            assert league_allowed(lg) is False, lg
            assert league_tier(lg) == "blocked", lg
            assert lg not in PROBATION_LEAGUES

    def test_tier_del_core(self):
        from value_filter import league_tier
        assert league_tier("Premier League") == "core"
        assert league_tier("Eredivisie") == "core"   # -1.8%: rumore, resta core
        assert league_tier("") == "blocked"
        assert league_tier("Serie A") == "blocked"

    def test_nazionali_in_core(self):
        """AFCON + Nations League in CORE (direttiva del proprietario, 29/09).

        Erano l'88% delle righe scartate nelle 24h del 24/09 e l'unico motivo
        di stop del flusso: le nazionali giocano quando i club sono in pausa.
        ⚠️ Queste due NON hanno un ROI misurato positivo (il backtest copre i
        club): entrano per decisione del proprietario, con i parametri PIU'
        PRUDENTI del core — l'uguaglianza qui e' la tracciabilita' di quella
        scelta, non una misura.
        """
        import value_filter as vf
        for lg in ("UEFA Nations League", "Africa Cup of Nations"):
            assert vf.league_tier(lg) == "core", lg
            assert vf.league_allowed(lg) is True, lg
            strat = vf.get_league_strategy(lg)
            assert strat["min_edge"] == 0.025, lg
            assert strat["kelly_mult"] == 0.8, lg
            assert strat["max_stake"] == 0.015, lg
        # Le leghe MISURATE negative non devono essersi aperte per sbaglio.
        for lg in ("Serie A", "La Liga", "Greek Super League"):
            assert vf.league_allowed(lg) is False, lg

    def test_alias_nazionali_puntano_al_nome_canonico(self):
        """REGRESSIONE 29/09: l'alias NON deve riscrivere il nome canonico in
        un nome bloccato.

        La prima stesura mappava `"uefa nations league" -> "Nations League"`:
        il nome corretto veniva riscritto in un nome ASSENTE da
        STRATEGY_LEAGUES/PROBATION_LEAGUES, quindi il gate rispondeva
        `tier=blocked` PRIMA e DOPO la modifica — un alias INERTE, che sembrava
        una fix e non cambiava una riga di flusso. La direzione dell'alias e'
        cio' che lo rende utile o dannoso: le varianti puntano al canonico,
        mai il contrario.
        """
        import value_filter as vf
        assert vf.canonical_league("UEFA Nations League") == "UEFA Nations League"
        assert vf.canonical_league("Africa Cup of Nations") == "Africa Cup of Nations"
        # le varianti convergono sul canonico
        for raw, expected in (("Nations League", "UEFA Nations League"),
                              ("Afrika Cup of Nations", "Africa Cup of Nations"),
                              ("CAF Africa Cup of Nations", "Africa Cup of Nations")):
            assert vf.canonical_league(raw) == expected, raw
            assert vf.league_allowed(raw) is True, raw
        # e l'invariante che il bug violava: nessun alias porta a un nome vietato
        for alias, target in vf.LEAGUE_ALIASES.items():
            assert vf.league_allowed(target), \
                f"alias {alias!r} -> {target!r} e' una lega vietata (alias inerte)"

    def test_is_sane_tier2_edge_4pp(self):
        """In probation l'edge minimo e' +4pp: +2.5pp passa nel core, non qui."""
        from value_filter import is_sane
        # edge = 0.625 - 0.60 = +2.5pp
        ok_core, _ = is_sane(0.625, 1.65, 0.031, market_prob=0.60,
                             league="Turkey Super Lig")   # min_edge 2.5pp
        ok_prob, reason = is_sane(0.625, 1.65, 0.031, market_prob=0.60,
                                  league="Serie B")
        assert ok_core is True
        assert ok_prob is False and "non batte il mercato" in reason
        # +4.5pp passa in probation.
        ok_prob2, reason2 = is_sane(0.645, 1.65, 0.064, market_prob=0.60,
                                    league="Serie B")
        assert ok_prob2 is True, reason2

    def test_is_sane_tier2_bloccata_resta_bloccata(self):
        """Il tier non apre le leghe misurate negative."""
        from value_filter import is_sane
        ok, reason = is_sane(0.68, 1.60, 0.088, market_prob=0.60,
                             league="La Liga")
        assert ok is False and "esclusa per ROI negativo" in reason

    def test_get_league_strategy(self):
        from value_filter import get_league_strategy
        strat = get_league_strategy("Premier League")
        assert strat["max_stake"] == 0.020
        assert strat["kelly_mult"] == 1.2
        assert strat["min_edge"] == 0.020

    def test_unknown_league_strategy_severo(self):
        """Le leghe non in lista usano fallback severo (min_edge piu' basso)."""
        from value_filter import get_league_strategy
        strat = get_league_strategy("Serie A")
        assert strat["max_stake"] == 0.005  # fallback severo
        assert strat["kelly_mult"] == 0.5
        assert strat["min_edge"] == 0.02     # allineato alla soglia +2pp

    def test_is_sane_league_filter(self):
        """Un segnale in Serie A viene rifiutato (ROI negativo)."""
        from value_filter import is_sane
        ok, reason = is_sane(0.60, 1.65, 0.023, market_prob=0.57,
                              league="Serie A")
        assert not ok
        assert "esclusa per ROI negativo" in reason

    def test_is_sane_league_winning(self):
        """Un segnale in Premier League passa i filtri."""
        from value_filter import is_sane
        ok, reason = is_sane(0.60, 1.65, 0.023, market_prob=0.57,
                              league="Premier League")
        assert ok, f"Expected ok, got: {reason}"

    def test_quota_2_00_bocciata(self):
        """Guardrail 11/09: sopra 1.80 e' fuori strategia (pantano)."""
        from value_filter import is_sane
        ok, reason = is_sane(0.57, 2.00, 0.14, market_prob=0.52, league="Premier League")
        assert not ok and "quota troppo alta" in reason
