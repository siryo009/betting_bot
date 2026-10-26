"""
Test unitari per value_filter.py (API attuale: lista di dict).

Verifica:
- compute_ev isolato con casi edge;
- filter_value_bets identifichi value bet con EV >= soglia e filtri di sanita';
- threshold configurabile;
- gestione lista vuota;
- STRATEGIA SOLO FAVORITI (11/09, fascia allargata a 1.20-2.00 il 14/10):
  cap quota + esito favorito di
  mercato (prob. devigata >= 50%), con eligible_favourites come gate.
"""

import pytest

from value_filter import compute_ev, filter_value_bets

#: Quota FUORI dalle bande escluse configurabili (`value_filter.in_dead_band`,
#: spenta di default dal 26/10/2026). I test che misurano EV / edge / tier
#: usano QUESTA: se un giorno la banda 1.50-1.80 venisse riaccesa da env, con
#: una quota nella banda leggerebbero il gate della fascia (che risponde
#: "quota nella fascia morta") invece della soglia che vogliono verificare.
#: La `ev` e' un argomento esplicito di `is_sane`, quindi il valore di EV non
#: cambia: cambia solo quale gate risponde per primo.
LIVE_PRICE = 1.90


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

        Solo FAVORITI NETTI (quota nella fascia 1.20-2.00 dal 14/10/2026):
        dal 11/09/2026 qualsiasi quota
        superiore e' fuori strategia e viene scartata a monte.
        """
        # ⚠️ Nessuna quota nella FASCIA MORTA 1.50-1.80: dal 25/10/2026
        # `is_sane` la esclude, e un dataset tutto dentro la morta farebbe
        # leggere a questi test il gate della fascia invece dell'EV.
        return [
            {"bookmaker": "Bet365", "evento": "Serie A – Roma vs Empoli", "sport": "calcio",
             "esito": "1", "quota_decimale": 1.95, "probabilita": 0.550,
             "timestamp": "2024-09-17T15:00:00Z"},
            {"bookmaker": "Snai", "evento": "Serie A – Roma vs Empoli", "sport": "calcio",
             "esito": "2", "quota_decimale": 4.20, "probabilita": 0.220,
             "timestamp": "2024-09-17T15:00:00Z"},
            {"bookmaker": "Bet365", "evento": "Serie A – Inter vs Milan", "sport": "calcio",
             "esito": "1", "quota_decimale": 1.45, "probabilita": 0.750,
             "timestamp": "2024-09-16T20:45:00Z"},
            {"bookmaker": "Snai", "evento": "Serie A – Inter vs Milan", "sport": "calcio",
             "esito": "X", "quota_decimale": 3.60, "probabilita": 0.170,
             "timestamp": "2024-09-16T20:45:00Z"},
            # ⚠️ probabilita' 0,575 e NON 0,60: con 0,60 l'EV sarebbe +14%,
            # cioe' SOPRA il tetto anti-overconfidence (`EV_MAX = 10%` dal
            # 26/10/2026) e la riga verrebbe respinta come ANOMALIA — il test
            # misurerebbe il tetto invece del filtro dei value bet.
            {"bookmaker": "Bet365", "evento": "Serie A – Atalanta vs Milan", "sport": "calcio",
             "esito": "1", "quota_decimale": 1.90, "probabilita": 0.575,
             "timestamp": "2024-09-15T18:00:00Z"},
            {"bookmaker": "Snai", "evento": "Serie A – Atalanta vs Milan", "sport": "calcio",
             "esito": "2", "quota_decimale": 3.40, "probabilita": 0.220,
             "timestamp": "2024-09-15T18:00:00Z"},
            {"bookmaker": "Snai", "evento": "Serie A – Sassuolo vs Napoli", "sport": "calcio",
             "esito": "2", "quota_decimale": 1.42, "probabilita": 0.650,
             "timestamp": "2024-09-14T20:45:00Z"},
            {"bookmaker": "Snai", "evento": "Serie A – Juventus vs Milan", "sport": "calcio",
             "esito": "X", "quota_decimale": 1.92, "probabilita": 0.500,
             "timestamp": "2024-09-14T20:45:00Z"},
            {"bookmaker": "Bet365", "evento": "Serie A – Juventus vs Milan", "sport": "calcio",
             "esito": "1", "quota_decimale": 1.85, "probabilita": 0.550,
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
        """STRATEGIA SOLO FAVORITI: nessuna quota oltre il cap passa il filtro."""
        from value_filter import ODDS_MAX
        result = filter_value_bets(odds_data, ev_threshold=0.02)
        assert all(r["quota_decimale"] <= ODDS_MAX for r in result)

    def test_scarta_ev_minore_uguale_soglia(self, odds_data):
        result = filter_value_bets(odds_data, ev_threshold=0.05)
        assert not self._has(result, "Serie A – Sassuolo vs Napoli", "2")
        assert not self._has(result, "Serie A – Juventus vs Milan", "X")
        assert not self._has(result, "Serie A – Juventus vs Milan", "1")

    def test_threshold_configurabile(self, odds_data):
        # Atalanta resta l'unica sopra +9% (EV +9,25%) e sotto il tetto
        # `EV_MAX` (10%): la soglia sceglie LEI, il tetto non c'entra.
        result = filter_value_bets(odds_data, ev_threshold=0.09)
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
        # Soglie RIPORTATE SU il 26/10/2026 dopo la misura sul ledger live
        # (le bet con edge 1-2pp hanno reso -70,5% su 8). Uguaglianza ESATTA di
        # proposito: la protezione e' la decisione tracciata, non il valore.
        assert vf.ODDS_MAX == 2.00         # fascia (14/10/2026, invariata)
        assert vf.ODDS_MIN == 1.20         # pavimento (14/10/2026, invariato)
        assert MARKET_EDGE_MIN == 0.02     # edge minimo +2pp (era 1pp)
        assert vf.EV_MIN == 0.025          # EV minimo +2,5% (era 1,5%)
        assert vf.EV_MAX == 0.10           # tetto anti-overconfidence (era 20%)
        # Fascia morta 1.50-1.80: meccanismo conservato, SPENTA di default
        # (26/10/2026 — il CLV +2,59% su 199 campioni non confermava il taglio).
        assert vf.dead_band_active() is False
        assert vf.in_dead_band(1.65) is False
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
        # Quota bassa ma il mercato la considera sfavorita (40%)
        ok, reason = is_sane(0.60, LIVE_PRICE, 0.02, market_prob=0.40)
        assert not ok and "favorito" in reason

    def test_favorito_netto_ammesso(self):
        from value_filter import is_sane
        ok, _ = is_sane(0.62, LIVE_PRICE, 0.026, market_prob=0.57)
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
        con strategia PIU' SEVERA del core (edge +2pp dal 14/10/2026,
        era +4pp; Kelly 0.4, cap 0.5%)."""
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
            # min_edge 0.025: e' il "core prudente" delle nazionali, riportato
            # su il 26/10/2026 dopo la parentesi a 1,25pp del 14/10.
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

    def test_is_sane_tier2_edge_meta(self):
        """In probation l'edge minimo e' +4pp: +2pp passa nel core, non qui.

        Soglie riportate su il 26/10/2026: core 2pp/2,5pp e probation 4pp (la
        parentesi "piu' volume" del 14/10 le aveva a 1pp e 2pp). La soglia del
        tier resta PIU' SEVERA di quella del core (invariante asserito in
        `test_tier2_probation_ammessa_con_parametri_severi`).
        """
        from value_filter import is_sane
        # edge = 0.63 - 0.60 = +3pp: passa nel core (Ligue 1, 2,5pp)...
        ok_core, why_core = is_sane(0.63, LIVE_PRICE, 0.04, market_prob=0.60,
                                    league="Ligue 1")
        assert ok_core, why_core
        # ...e NON in probation (2pp non bastano: servono 4pp).
        ok_prob, reason = is_sane(0.63, LIVE_PRICE, 0.04, market_prob=0.60,
                                  league="Serie B")
        assert ok_prob is False and "non batte il mercato" in reason
        # +4.5pp passa in probation.
        ok_prob2, reason2 = is_sane(0.645, LIVE_PRICE, 0.064, market_prob=0.60,
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
        assert strat["min_edge"] == 0.020   # riportato a +2pp il 26/10/2026

    def test_unknown_league_strategy_severo(self):
        """Le leghe non in lista usano fallback severo (min_edge piu' basso)."""
        from value_filter import get_league_strategy
        strat = get_league_strategy("Serie A")
        assert strat["max_stake"] == 0.005  # fallback severo
        assert strat["kelly_mult"] == 0.5
        assert strat["min_edge"] == 0.02     # allineato alla soglia +2pp (26/10)

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
        ok, reason = is_sane(0.60, LIVE_PRICE, 0.026, market_prob=0.57,
                              league="Premier League")
        assert ok, f"Expected ok, got: {reason}"

    def test_quota_sopra_il_cap_bocciata(self):
        """Sopra il cap della fascia (2.00 dal 14/10/2026) e' fuori strategia.

        Il caso 2.00 era il testimone del vecchio cap 1.80; col cap nuovo il
        testimone si sposta a 2.10 e si aggiunge la CONTROPROVA (1.95 ora
        passa): senza, il test non distinguerebbe "cap alzato" da
        "gate della quota disattivato".
        """
        from value_filter import is_sane
        ok, reason = is_sane(0.57, 2.10, 0.14, market_prob=0.52, league="Premier League")
        assert not ok and "quota troppo alta" in reason
        ok, _ = is_sane(0.55, 1.95, 0.05, market_prob=0.50, league="Premier League")
        assert ok


class TestOracoloAPagamento:
    """`ORACLE_PAID_TIERS` (07/10/2026): CHI puo' ricevere un refetch pagato.

    Prima erano solo le leghe Core cablate; ora il tier ammesso e' una
    configurazione, con default prudente (`core`).
    """

    def test_default_core(self, monkeypatch):
        import value_filter
        monkeypatch.delenv("ORACLE_PAID_TIERS", raising=False)
        assert value_filter.oracle_paid_tiers() == frozenset({"core"})
        assert value_filter.is_paid_oracle_league("Premier League") is True
        assert value_filter.is_paid_oracle_league("Liga MX") is False

    def test_probation_quando_dichiarata(self, monkeypatch):
        import value_filter
        monkeypatch.setenv("ORACLE_PAID_TIERS", "core,probation")
        assert value_filter.is_paid_oracle_league("Liga MX") is True
        assert value_filter.is_paid_oracle_league("MLS") is True
        assert value_filter.is_paid_oracle_league("Premier League") is True
        # Una lega VIETATA (ROI misurato negativo) non e' in nessun tier.
        assert value_filter.is_paid_oracle_league("Serie A") is False
        assert value_filter.is_paid_oracle_league("La Liga") is False

    def test_valore_vuoto_o_ignoto_ricade_sul_default(self, monkeypatch):
        import value_filter
        for raw in ("", "   ", "core,", "pippo", "core,pippo", ","):
            monkeypatch.setenv("ORACLE_PAID_TIERS", raw)
            assert value_filter.oracle_paid_tiers() == frozenset({"core"}), raw

    def test_coerente_col_tier_della_lega(self, monkeypatch):
        import value_filter
        monkeypatch.setenv("ORACLE_PAID_TIERS", "core,probation")
        for lg in ("Premier League", "Liga MX", "Serie A", "Inventata", ""):
            assert value_filter.is_paid_oracle_league(lg) is (
                value_filter.league_tier(lg) in value_filter.oracle_paid_tiers()), lg


class TestSogliaEvPerTier:
    """Soglia EV a DUE dimensioni (08/10/2026): mercato + TIER di lega.

    Direttiva del proprietario: la soglia EV ha una seconda dimensione, il
    tier della lega, con precedenza **la piu' PERMISSIVA** (`min`, revisione
    dell'08/10/2026 dopo la prima stesura con `max`). Questi test difendono la
    regola ESATTA — comprese le due conseguenze dichiarate: il 1X2 core scende
    a 1.0% e sui mercati liquidi vince la soglia base del mercato (0.5%) per
    TUTTE le leghe ammesse. Un ritocco silenzioso della precedenza rompe qui.
    ⚠️ I valori assoluti seguono la direttiva del 14/10/2026 (che li ha
    dimezzati): cio' che questi test difendono e' la PRECEDENZA (`min`) e il
    fatto che la soglia effettiva sia SEMPRE quella dichiarata dalle costanti,
    non i numeri in se'.
    """

    def test_core_abbassa_il_1x2(self):
        import value_filter as vf
        # 1X2: min(2.5%, 1.5%) -> vince il tier core (l'allargamento richiesto)
        assert vf.ev_min("Premier League", "1X2") == vf.EV_MIN_CORE
        assert vf.ev_min("Premier League", "") == vf.EV_MIN_CORE

    def test_core_su_mercato_liquido_usa_la_soglia_del_mercato(self):
        import value_filter as vf
        # min(0.5%, 1.0%) -> vince la soglia BASE del mercato liquido
        assert vf.ev_min_for_market("AH") == vf.EV_MIN_LIQUID
        assert vf.ev_min("Premier League", "AH") == vf.EV_MIN_LIQUID

    def test_non_core_usa_la_soglia_del_mercato(self):
        import value_filter as vf
        for lg in ("Serie A", "La Liga", "Inventata", "", "MLS"):
            assert vf.ev_min_for_tier(lg) == vf.EV_MIN_OTHER, lg
            assert vf.ev_min(lg, "1X2") == vf.EV_MIN, lg
            assert vf.ev_min(lg, "AH") == vf.EV_MIN_LIQUID, lg

    def test_il_tier_non_alza_mai_una_soglia(self):
        """Con `min` il tier puo' solo ABBASSARE: la soglia effettiva non
        supera mai quella del mercato (nessun appesantimento nascosto)."""
        import value_filter as vf
        leghe = (list(vf.STRATEGY_LEAGUES) + list(vf.PROBATION_LEAGUES)
                 + ["Serie A", "", "Inventata"])
        for lg in leghe:
            for mk in ("", "1X2", "AH", "OU", "TOTALS", "ML", "TENNIS"):
                assert vf.ev_min(lg, mk) <= vf.ev_min_for_market(mk) + 1e-12, (lg, mk)

    def test_gate_core_1x2_soglia_del_tier(self):
        import value_filter as vf
        # EV +1.6%: sopra EV_MIN_CORE (1,5% dal 26/10) -> PASSA
        ok, why = vf.is_sane(0.72, LIVE_PRICE, 0.016, market_prob=0.65,
                             league="Premier League", market="1X2",
                             favourites_only=False, market_edge_min=0.0)
        assert ok, why
        # EV +1.2%: sotto EV_MIN_CORE (era sopra quando la soglia era 1,0%)
        no, why2 = vf.is_sane(0.72, LIVE_PRICE, 0.012, market_prob=0.65,
                              league="Premier League", market="1X2",
                              favourites_only=False, market_edge_min=0.0)
        assert not no and "EV troppo basso" in why2, why2

    def test_gate_core_su_mercato_liquido_usa_la_soglia_del_mercato(self):
        import value_filter as vf
        # AH core: la soglia e' EV_MIN_LIQUID (1,0% dal 26/10) -> +1,2% passa
        ok, why = vf.is_sane(0.71, 1.55, 0.012, market_prob=0.65,
                             league="Premier League", market="AH",
                             favourites_only=False, market_edge_min=0.0)
        assert ok, why
        # +0.8%: sotto la soglia del mercato liquido -> respinto
        no, why2 = vf.is_sane(0.71, 1.55, 0.008, market_prob=0.65,
                              league="Premier League", market="AH",
                              favourites_only=False, market_edge_min=0.0)
        assert not no and "EV troppo basso" in why2, why2

    def test_non_core_su_mercato_liquido_usa_la_soglia_base(self):
        """CONSEGUENZA DICHIARATA della revisione: le leghe non core sui
        mercati liquidi passano alla soglia base del mercato (0.5%).
        Chi vuole la prudenza precedente usa `EV_MIN_LIQUID=0.025` (env),
        senza toccare il codice."""
        import value_filter as vf
        ok, why = vf.is_sane(0.71, 1.55, 0.02, market_prob=0.65,
                             league="MLS", market="OU",
                             favourites_only=False, market_edge_min=0.0)
        assert ok, why
        # ...ma il 1X2 di una lega non core resta alla soglia protettiva
        # (`EV_MIN`, 1.5%): +1.2% NON basta piu' li'.
        no, why2 = vf.is_sane(0.71, LIVE_PRICE, 0.012, market_prob=0.65,
                              league="MLS", market="1X2",
                              favourites_only=False, market_edge_min=0.0)
        assert not no and "EV troppo basso" in why2, why2

    def test_fail_safe_su_lega_sconosciuta(self):
        import value_filter as vf
        # Una lega che non si sa riconoscere NON e' core: nessuna riduzione.
        assert vf.ev_min_for_tier("Lega Che Non Esiste") == vf.EV_MIN_OTHER
        assert vf.EV_MIN_OTHER == vf.EV_MIN       # fallback = soglia unica
        assert vf.ev_min_for_tier(None) == vf.EV_MIN_OTHER
        assert vf.ev_min("Inventata", "1X2") == vf.EV_MIN
        assert vf.ev_min(None, "AH") == vf.EV_MIN_LIQUID

    def test_env_override(self, monkeypatch):
        import importlib
        import value_filter as vf
        monkeypatch.setenv("EV_MIN_CORE", "0.02")
        monkeypatch.setenv("EV_MIN_OTHER", "0.03")
        try:
            mod = importlib.reload(vf)
            assert mod.ev_min_for_tier("Premier League") == 0.02
            assert mod.ev_min_for_tier("Serie A") == 0.03
        finally:
            monkeypatch.undo()
            importlib.reload(vf)

    def test_iac_dichiara_le_env(self):
        """Ogni env LETTA dal modulo dev'essere in `preserve()`.

        `EV_MAX` e' letta dall'ambiente dal 26/10/2026: senza la dichiarazione
        un `config apply` la distruggerebbe e il tetto anti-overconfidence
        tornerebbe al valore di codice **in silenzio**.
        """
        from pathlib import Path
        src = Path(".railway/railway.ts").read_text(encoding="utf-8")
        for env in ("EV_MIN_CORE", "EV_MIN_OTHER", "EV_MIN_LIQUID",
                    "EV_MAX"):
            assert f"{env}: preserve()" in src, env


# ---------------------------------------------------------------------------
# Fascia morta 1.50-1.80 (25/10/2026)
# ---------------------------------------------------------------------------
class TestFasciaMorta:
    """La banda 1.50-1.80: meccanismo conservato, applicazione SPENTA.

    Ciclo completo, perche' e' quello che conta:
      25/10 — esclusa sulla misura del P/L: 1.20-1.50 +16,6% (n=17) ·
              1.50-1.80 **-16,4%** (n=31) · 1.80-2.00 -1,1% (n=24), con il
              caveat dichiarato nel codice (z ~ -0,96, intervallo che include
              lo zero).
      26/10 — **rimisura del CLV** (473 campioni con closing sharp): la banda
              da' **+2,59%** (n=199, 88% positivi), identico a 1.20-1.50
              (+2,23%) e 1.80-2.00 (+2,58%), e il P/L sulla QUOTA-SEGNALE
              scende a -5,55% (n=29). Il taglio toglieva una popolazione con
              edge: meccanismo conservato, **default OFF**.

    Questi test difendono le due cose che possono rompersi in silenzio: che la
    banda sia spenta quando nessuno la configura, e che riaccenderla da env
    torni a escludere esattamente 1.50-1.80 (niente di piu', niente di meno).
    """

    def test_default_spento(self):
        """Su un processo pulito la banda non esiste (env non impostata)."""
        import value_filter as vf
        assert vf.ODDS_EXCLUDE_MIN == 0.0
        assert vf.ODDS_EXCLUDE_MAX == 0.0
        assert vf.dead_band_active() is False

    def test_default_spento_non_esclude_nulla(self):
        """La fascia 1.20-2.00 torna CONTINUA, 1.65 compreso."""
        import value_filter as vf
        for price in (1.45, 1.50, 1.65, 1.75, 1.80, 1.90):
            assert vf.in_dead_band(price) is False
            assert vf.odds_in_band(price) is True, price
        ok, reason = vf.is_sane(0.60, 1.65, 0.03, market_prob=0.55)
        assert ok, reason

    def test_riaccensione_da_env(self, monkeypatch):
        """Con `ODDS_EXCLUDE_MIN/MAX` la banda torna a escludere 1.50-1.80."""
        import value_filter as vf
        monkeypatch.setattr(vf, "ODDS_EXCLUDE_MIN", 1.50)
        monkeypatch.setattr(vf, "ODDS_EXCLUDE_MAX", 1.80)
        assert vf.dead_band_active() is True
        for price in (1.50, 1.65, 1.80):
            assert vf.in_dead_band(price) is True, price
            assert vf.odds_in_band(price) is False, price
        for price in (1.49, 1.81, 1.30, 2.00):
            assert vf.in_dead_band(price) is False, price
            assert vf.odds_in_band(price) is True, price

    @pytest.mark.parametrize("price,expected", [
        (None, False), ("abc", False), ({}, False), (1.49, False),
        (1.81, False),
    ])
    def test_in_dead_band_fail_safe(self, price, expected, monkeypatch):
        """Un valore illeggibile non e' MAI "nella banda" (fail-open sul gate:
        lo esclude gia' `odds_in_band` con la sua regola)."""
        import value_filter as vf
        monkeypatch.setattr(vf, "ODDS_EXCLUDE_MIN", 1.50)
        monkeypatch.setattr(vf, "ODDS_EXCLUDE_MAX", 1.80)
        assert vf.in_dead_band(price) is expected

    def test_stringa_numerica_riconosciuta(self, monkeypatch):
        """Una quota che arriva come stringa e' comunque una quota."""
        import value_filter as vf
        monkeypatch.setattr(vf, "ODDS_EXCLUDE_MIN", 1.50)
        monkeypatch.setattr(vf, "ODDS_EXCLUDE_MAX", 1.80)
        assert vf.in_dead_band("1.65") is True
        assert vf.in_dead_band(" 1.65 ") is True

    def test_confini_espliciti_non_ereditano_la_morta(self, monkeypatch):
        """Chi chiede "sta fra 1.6 e 1.7?" fa una domanda su QUEI numeri.

        La banda esclusa e' misurata su una popolazione precisa: applicarla a
        un intervallo arbitrario cambierebbe la risposta a una domanda diversa.
        """
        import value_filter as vf
        monkeypatch.setattr(vf, "ODDS_EXCLUDE_MIN", 1.50)
        monkeypatch.setattr(vf, "ODDS_EXCLUDE_MAX", 1.80)
        assert vf.odds_in_band(1.65, odds_min=1.60, odds_max=1.70) is True

    def test_is_sane_boccia_la_morta_solo_se_attiva(self, monkeypatch):
        import value_filter as vf
        ok, reason = vf.is_sane(0.60, 1.65, 0.03, market_prob=0.55)
        assert ok, reason
        monkeypatch.setattr(vf, "ODDS_EXCLUDE_MIN", 1.50)
        monkeypatch.setattr(vf, "ODDS_EXCLUDE_MAX", 1.80)
        ok, reason = vf.is_sane(0.60, 1.65, 0.03, market_prob=0.55)
        assert not ok and "fascia morta" in reason

    @pytest.mark.parametrize("market", ["AH", "OU", "TOTALS", "BTTS"])
    def test_mercati_liquidi_non_esclusi(self, market, monkeypatch):
        """OU/AH non hanno una misura equivalente: non si stringono per analogia."""
        import value_filter as vf
        monkeypatch.setattr(vf, "ODDS_EXCLUDE_MIN", 1.50)
        monkeypatch.setattr(vf, "ODDS_EXCLUDE_MAX", 1.80)
        assert vf.is_liquid_market(market) is True
        ok, reason = vf.is_sane(0.60, 1.65, 0.02, market_prob=0.55,
                               market=market)
        assert ok, (market, reason)

    def test_il_tennis_e_mercato_di_strategia(self, monkeypatch):
        """TENNIS non e' liquido: quando la banda e' attiva vale anche per lui."""
        import value_filter as vf
        monkeypatch.setattr(vf, "ODDS_EXCLUDE_MIN", 1.50)
        monkeypatch.setattr(vf, "ODDS_EXCLUDE_MAX", 1.80)
        assert vf.in_dead_band(1.65) is True
        assert vf.is_liquid_market("TENNIS") is False

    def test_banda_non_valida_e_spenta(self, monkeypatch):
        """Estremi incoerenti (0 o invertiti) = banda NON configurata.

        Un env sbagliato non deve accendere un gate di strategia: la direzione
        e' prudente sul RISCHIO di configurazione, non sull'esclusione.
        """
        import value_filter as vf
        for lo, hi in ((0.0, 1.80), (1.50, 0.0), (1.80, 1.50), (-1.0, 1.0)):
            monkeypatch.setattr(vf, "ODDS_EXCLUDE_MIN", lo)
            monkeypatch.setattr(vf, "ODDS_EXCLUDE_MAX", hi)
            assert vf.dead_band_active() is False, (lo, hi)
            assert vf.in_dead_band(1.65) is False, (lo, hi)

    def test_env_illeggibile_ricade_sul_default_spento(self, monkeypatch):
        """Un env non numerico non accende nulla e non solleva."""
        import value_filter as vf
        monkeypatch.setenv("ODDS_EXCLUDE_MIN", "abc")
        monkeypatch.setenv("ODDS_EXCLUDE_MAX", "")
        assert vf._env_float("ODDS_EXCLUDE_MIN", 0.0) == 0.0
        assert vf._env_float("ODDS_EXCLUDE_MAX", 0.0) == 0.0
        assert vf._env_float("ODDS_EXCLUDE_NON_ESISTE", 0.0) == 0.0
