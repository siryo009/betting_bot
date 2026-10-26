"""Gate STRATEGY_LEAGUES sulla corsia auto-bet (15/09).

Contesto misurato: fino al 15/09 la corsia ordini era **cieca alla lega**. I
candidati di `fixture_engine`/`sx_signals` non portavano la chiave `league`,
quindi `is_sane(league="")` ammetteva tutto (lega vuota = nessun divieto) e il
bot ha puntato leghe che la strategia del 12/09 vieta: nel ledger live le 4
chiusure in leghe vietate sono 0 vinte / 4 perse, mentre l'unica in una lega
ammessa e' vinta. Sintomo del bug: `match_analysis.status = rejected` (dove la
lega VENIVA passata) e `predictions.status = value` (dove non veniva).

Qui si blinda il fix su TRE livelli:
1. **propagazione** della lega sul candidato (la causa radice);
2. **gate in corsia** (`auto_bet._today_value_picks`), fail-closed quando la
   lega manca: nessun ordine su cio' che non si sa classificare;
3. **risoluzione deterministica** dei nomi delle leghe AMMESSE: un falso
   divieto (lega ammessa letta come vietata) e' il rischio peggiore del gate,
   perche' azzererebbe il flusso autorizzato invece di tagliare quello vietato.

Tutti i test sono OFFLINE: DB SQLite temporaneo, nessuna rete, nessun ordine.
"""
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import auto_bet
import fixture_engine
import sx_signals
import tracker
import value_filter

ALLOWED = "Premier League"          # in STRATEGY_LEAGUES (core)
BANNED = "La Liga"                  # esclusa per ROI negativo (12/09)
PROBATION = "Serie B"               # tier-2 dal 21/09 (PROBATION_LEAGUES)


@pytest.fixture()
def temp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setattr(tracker, "DB_PATH", Path(td) / "test.db")
        tracker.init_db()
        yield


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Mai toccare il volume reale (stop-loss) ne' la rete (feed di mercato)."""
    monkeypatch.setattr(auto_bet, "DAILY_STOP_FILE", tmp_path / "daily_stop.json")
    monkeypatch.setattr(auto_bet, "_market_feed_gate",
                        lambda *a, **k: (True, "test"))


def _seed(mid, league, quota=1.65, status="value", ev=0.08,
          market_prob=0.60, market_edge=0.07, commence=None):
    """Una partita + una previsione 1X2 giocabile (favorito netto)."""
    start = commence or (datetime.now(timezone.utc) + timedelta(hours=3)) \
        .isoformat().replace("+00:00", "Z")
    tracker.save_match(mid, league, "Osasuna", "Getafe", start)
    tracker.save_analysis(mid, 1.7, 1.1, 0.52, 0.27, 0.21, 0.58, ev,
                          "Osasuna", quota, "Pinnacle", status,
                          market_prob=market_prob, market_edge=market_edge)
    tracker.save_prediction(mid, "1X2", "Osasuna", quota, 0.52, ev,
                            market_prob=market_prob, market_edge=market_edge,
                            status=status)


def _picks():
    return {p["match_id"]: p for p in auto_bet._today_value_picks()}


class TestGateCorsiaOrdini:
    """`_today_value_picks`: il denaro non va dove la strategia vieta."""

    def test_lega_ammessa_passa(self, temp_db):
        _seed("ok", ALLOWED)
        picks = _picks()
        assert "ok" in picks
        assert picks["ok"]["league"] == ALLOWED

    def test_lega_vietata_bloccata(self, temp_db):
        _seed("no", BANNED)
        assert "no" not in _picks()

    def test_lega_vietata_bloccata_anche_con_ev_alto(self, temp_db):
        """Il caso reale (EFL Cup / La Liga): EV eccellente non basta.

        Era esattamente cosi' che il bot ha puntato Liverpool in EFL Cup:
        il segnale piu' appetitoso era proprio in una lega vietata.
        """
        _seed("top", BANNED, quota=1.55, ev=0.17, status="strong_value",
              market_edge=0.09)
        assert "top" not in _picks()

    def test_lega_assente_fail_closed(self, temp_db):
        """Senza lega non si sa cosa si sta giocando: nessun ordine.

        Sul volume nessuna riga con partita in `matches` ha lega vuota (le
        vuote sono orfane senza riga, quindi senza mercato): il fail-closed
        qui non taglia nulla di legittimo.
        """
        _seed("senza", "")
        assert "senza" not in _picks()

    def test_lega_non_classificata_bloccata(self, temp_db):
        """Lega mai elencata (ne' ammessa ne' esplicitamente persa) = vietata."""
        _seed("x", "Lega Inventata")
        assert "x" not in _picks()

    def test_lega_vietata_resta_nel_ledger_ma_non_si_punta(self, temp_db):
        """Telemetria intatta: la riga resta nel ledger, cambia solo lo status.

        Il gate NON cancella nulla: e' cosi' che si continua a misurare cosa
        la strategia taglia (strumento league_gate_impact).
        """
        _seed("no", BANNED)
        conn = tracker._get_conn()
        row = conn.execute("SELECT status FROM predictions WHERE match_id='no'"
                           ).fetchone()
        conn.close()
        assert row == ("value",)   # il ledger registra il segnale del motore
        assert "no" not in _picks()  # ma l'ordine non parte

    def test_tutte_le_leghe_ammesse_passano(self, temp_db):
        for i, league in enumerate(value_filter.STRATEGY_LEAGUES):
            _seed(f"a{i}", league)
        picks = _picks()
        assert len(picks) == len(value_filter.STRATEGY_LEAGUES)

    def test_tutte_le_leghe_tier2_passano(self, temp_db):
        """Tier-2 (21/09): la corsia ordini non deve VIETARE nessuna delle 15
        leghe in probation — il gate e' a tre stati, non binario."""
        for i, league in enumerate(sorted(value_filter.PROBATION_LEAGUES)):
            _seed(f"p{i}", league)
        picks = _picks()
        assert len(picks) == len(value_filter.PROBATION_LEAGUES)
        for mid in picks:
            assert value_filter.league_tier(picks[mid]["league"]) == "probation"


class TestPropagazioneLega:
    """La causa radice: il candidato DEVE portare la lega."""

    def test_candidate_status_boccia_la_lega_vietata(self):
        """Il meccanismo su cui si regge il fix.

        `_candidate_status` classifica con `is_sane(..., league=cand["league"])`:
        con la chiave la lega vietata esce `rejected`, senza la chiave
        l'esito e' `value` (fail-open storico, il bug).
        """
        cand = {"prob": 0.72, "quota": 1.85, "ev": 0.08,
                "market_prob": 0.60, "market_edge": 0.07}
        assert fixture_engine._candidate_status(dict(cand)) == "value"
        assert fixture_engine._candidate_status(
            {**cand, "league": BANNED}) == "rejected"
        assert fixture_engine._candidate_status(
            {**cand, "league": ALLOWED}) == "value"

    def test_tier2_edge_alzato_applicato_dal_motore(self):
        """In probation l'edge minimo e' piu' severo di quello core, e vale nel
        codice VERO del motore (`_candidate_status`), non solo in `is_sane`.

        Le due soglie si DERIVANO dalle costanti (lezione del 13/09: un numero
        copiato a mano smette di proteggere al primo ritocco). L'edge scelto sta
        FRA le due: rifiutato in probation, accettato in una lega core.
        """
        # La prob. si DERIVA dall'EV voluto (meta' strada fra il pavimento e
        # il tetto del gate): una `prob` fissa puo' cadere fuori dalla fascia
        # e far misurare il gate EV invece di quello dell'edge.
        quota = 1.85
        ev_target = (value_filter.EV_MIN + value_filter.EV_MAX) / 2
        prob = round((1 + ev_target) / quota, 4)
        probation_edge = value_filter.PROBATION_STRATEGY["min_edge"]
        core_edge = value_filter.STRATEGY_LEAGUES["Turkey Super Lig"]["min_edge"]
        assert probation_edge > core_edge, "la probation deve essere piu' severa"
        edge = round((probation_edge + core_edge) / 2, 5)
        cand = {"prob": prob, "quota": quota, "ev": prob * quota - 1,
                "market_prob": prob - edge, "market_edge": edge}
        assert fixture_engine._candidate_status(
            {**cand, "league": PROBATION}) == "rejected"
        assert fixture_engine._candidate_status(
            {**cand, "league": "Turkey Super Lig"}) != "rejected"
        # Sopra la soglia di probation: accettato come `value` (EV ~6%, sotto
        # la soglia di strong_value). La probabilita' si DERIVA dall'EV voluto:
        # con una `prob` fissa il livello di EV dipende dalla quota, e il
        # tier cambierebbe al primo ritocco della fascia quote.
        prob_forte = round((1 + 0.06) / quota, 4)
        edge_forte = probation_edge + 0.02
        forte = {**cand, "prob": prob_forte, "ev": prob_forte * quota - 1,
                 "market_edge": edge_forte,
                 # `market_prob` va RIDEFINITO con la nuova prob: lasciando
                 # quello di `cand` l'edge diventerebbe negativo e il rifiuto
                 # arriverebbe dall'edge, non dal tier (falso testimone).
                 "market_prob": prob_forte - edge_forte}
        assert fixture_engine._candidate_status(
            {**forte, "league": PROBATION}) == "value"

    def test_i_candidati_1x2_e_ah_portano_la_lega(self):
        """Tripwire sul sorgente: ogni dict candidato ha la chiave `league`."""
        src = Path(fixture_engine.__file__).read_text()
        assert src.count('"league": league,') >= 2   # 1X2 + Asian Handicap

    def test_i_candidati_sx_portano_la_lega(self):
        src = Path(sx_signals.__file__).read_text()
        assert '"league": league_name,' in src

    def test_scan_su_lega_vietata_non_genera_segnali(self, temp_db, monkeypatch):
        """End-to-end sul percorso SX reale (provider fake, zero rete).

        Con la lega propagata, `is_sane` boccia i candidati e il ledger li
        scrive come `rejected`: nessun segnale value salvato.
        """
        from test_sx_signals import FakeSxProvider, _MODEL_PROBS, _raw_markets
        monkeypatch.setattr(sx_signals, "expected_goals", lambda h, a: (1.9, 0.8))
        monkeypatch.setattr(sx_signals, "prob_1x2",
                            lambda lh, la: _MODEL_PROBS)
        monkeypatch.setattr(sx_signals, "adjusted_probability",
                            lambda model_prob, market_prob, price, league=None:
                            model_prob)
        saved = sx_signals.scan(provider=FakeSxProvider(_raw_markets(BANNED)))
        assert saved == []
        conn = tracker._get_conn()
        rows = conn.execute("SELECT status FROM predictions").fetchall()
        conn.close()
        assert rows and all(r == ("rejected",) for r in rows)

    def test_scan_su_lega_ammessa_genera_segnali(self, temp_db, monkeypatch):
        """Controprova: la stessa fixture su lega ammessa produce il segnale."""
        from test_sx_signals import FakeSxProvider, _MODEL_PROBS, _raw_markets
        monkeypatch.setattr(sx_signals, "expected_goals", lambda h, a: (1.9, 0.8))
        monkeypatch.setattr(sx_signals, "prob_1x2",
                            lambda lh, la: _MODEL_PROBS)
        monkeypatch.setattr(sx_signals, "adjusted_probability",
                            lambda model_prob, market_prob, price, league=None:
                            model_prob)
        saved = sx_signals.scan(
            provider=FakeSxProvider(_raw_markets("English Premier League")))
        assert len(saved) == 1
        assert saved[0]["league"] == ALLOWED
        # e supera anche il gate della corsia ordini
        assert saved[0]["match_id"] in _picks()


class TestNomiDelleLegheAmmesse:
    """Un falso divieto azzererebbe il flusso autorizzato: nomi blindati."""

    @pytest.mark.parametrize("label,expected", [
        ("English Premier League", "Premier League"),
        ("England Premier League", "Premier League"),
        ("Premier League", "Premier League"),
        ("German Bundesliga", "Bundesliga"),
        ("Germany Bundesliga", "Bundesliga"),
        ("Bundesliga", "Bundesliga"),
        ("France Ligue 1", "Ligue 1"),
        ("Ligue 1", "Ligue 1"),
        ("Netherlands Eredivisie", "Eredivisie"),
        ("Eredivisie", "Eredivisie"),
        ("Super Lig", "Turkey Super Lig"),
        ("Turkish Super Lig", "Turkey Super Lig"),
    ])
    def test_etichetta_sx_risolta_verso_una_lega_ammessa(self, label, expected):
        resolved = sx_signals._league_sx_to_sports_map(label) or label
        assert resolved == expected
        assert value_filter.league_allowed(resolved)

    @pytest.mark.parametrize("league", [
        "EFL Cup", "La Liga", "Serie A", "Belgian Pro League",
        "Liga Portugal", "Greek Super League", "Primera A",
    ])
    def test_lega_vietata_resta_vietata_dopo_la_risoluzione(self, league):
        """Nessuna scorciatoia: il resolver non 'promuove' una lega persa.

        Solo le leghe MISURATE NEGATIVE restano vietate (dal 21/09 l'elenco
        dei vietati si e' ristretto: EFL Championship, Scottish Premiership,
        Liga MX & co. sono passate in PROBATION — vedi
        `test_tier2_non_bloccata_per_errore`).
        """
        resolved = sx_signals._league_sx_to_sports_map(league) or league
        assert not value_filter.league_allowed(resolved)

    @pytest.mark.parametrize("label,expected", [
        ("England Championship", "EFL Championship"),
        ("The Championship", "EFL Championship"),
        ("Italy Serie B", "Serie B"),
        ("Major League Soccer", "MLS"),
        ("Liga Profesional", "Argentina Primera"),
        ("Premiership", "Scottish Premiership"),
        ("Superliga", "Superliga Danimarca"),
        ("Switzerland Super League", "Swiss Super League"),
        ("Mexico Liga MX", "Liga MX"),
        ("Saudi Arabia Pro League", "Saudi Pro League"),
        ("South Korea K League 1", "K League 1"),
        ("Japan J1 League", "J1 League"),
    ])
    def test_tier2_non_bloccata_per_errore(self, label, expected):
        """Tier-2 (21/09): il resolver non deve VIETARE per errore una lega
        giocabile — un falso divieto varrebbe piu' di un divieto mancante,
        perche' azzererebbe il flusso autorizzato."""
        resolved = sx_signals._league_sx_to_sports_map(label) or label
        assert resolved == expected
        assert value_filter.league_tier(resolved) == "probation"
        assert value_filter.league_allowed(resolved)

    def test_le_leghe_della_strategia_esistono_in_sports_map(self):
        """Ogni lega ammessa deve avere anche la chiave the-odds-api (settlement)."""
        from odds_api import SPORTS_MAP
        for league in value_filter.STRATEGY_LEAGUES:
            assert league in SPORTS_MAP, f"{league} assente da SPORTS_MAP"

    @pytest.mark.parametrize("raw", [
        "Major League Soccer", "USA MLS", "United States MLS",
    ])
    def test_gate_ammette_il_nome_grezzo_del_provider(self, raw):
        """Difesa in profondita': il gate NON dipende da come una fonte
        scrive il nome della lega.

        Misurato il 24/09/2026: `multi_market` salvava l'etichetta GREZZA di
        SX (`Major League Soccer`) invece della chiave della strategia
        (`MLS`), quindi il gate la leggeva come lega vietata e scartava
        candidati con EV +52% ed edge +9.5pp con "ROI negativo" — un divieto
        FALSO su una lega in probation. Il resolver era corretto: il difetto
        era che quel percorso non lo usava, percio' il gate si difende da solo.
        """
        assert value_filter.canonical_league(raw) == "MLS"
        assert value_filter.league_allowed(raw)
        assert value_filter.league_tier(raw) == "probation"
        # La soglia si legge dalla costante: la probation e' definita UNA volta.
        assert (value_filter.get_league_strategy(raw)["min_edge"]
                == value_filter.PROBATION_STRATEGY["min_edge"])

    def test_alias_non_fonde_leghe_diverse(self):
        """`Brazil Serie B` NON e' `Serie B`: l'alias non deve promuovere la
        Serie B brasiliana (legittimamente vietata, misurato il 24/09)."""
        assert value_filter.canonical_league("Brazil Serie B") == "Brazil Serie B"
        assert not value_filter.league_allowed("Brazil Serie B")
        assert not value_filter.league_allowed("Brasileiro Serie B")
        # una lega sconosciuta resta se stessa: nessun nome inventato
        assert value_filter.canonical_league("Lega Fantasma") == "Lega Fantasma"
        assert value_filter.canonical_league("") == ""
        assert value_filter.league_tier("Lega Fantasma") == "blocked"


class TestCorsiaTorneoApreLeLeghe:
    """Sprint "Gridiron Gauntlet" (12/10/2026): il formato RETURN non toglie
    punti alle scommesse perse, quindi il gate di lega (che protegge il ROI
    della CASSA, non il punteggio) si apre alla corsia torneo. Doppia chiave
    obbligatoria, letta a runtime, fail-closed su qualunque valore ambiguo."""

    def test_default_chiuso(self, monkeypatch):
        monkeypatch.delenv("TOURNAMENT_MODE", raising=False)
        monkeypatch.delenv("TOURNAMENT_OPEN_LEAGUES", raising=False)
        assert value_filter.tournament_all_leagues() is False
        assert not value_filter.league_allowed(BANNED)

    def test_serve_entrambe_le_chiavi(self, monkeypatch):
        """Solo MODE o solo OPEN_LEAGUES NON aprono nulla: l'apertura e' una
        decisione esplicita, non un effetto collaterale della modalita'."""
        monkeypatch.setenv("TOURNAMENT_MODE", "1")
        monkeypatch.delenv("TOURNAMENT_OPEN_LEAGUES", raising=False)
        assert value_filter.tournament_all_leagues() is False
        monkeypatch.delenv("TOURNAMENT_MODE", raising=False)
        monkeypatch.setenv("TOURNAMENT_OPEN_LEAGUES", "1")
        assert value_filter.tournament_all_leagues() is False
        monkeypatch.setenv("TOURNAMENT_MODE", "1")
        assert value_filter.tournament_all_leagues() is True
        assert value_filter.league_allowed(BANNED) is True

    @pytest.mark.parametrize("raw", ["0", "false", "no", "off", "", "  ", "si", "2"])
    def test_valori_ambigui_non_aprono(self, monkeypatch, raw):
        monkeypatch.setenv("TOURNAMENT_MODE", "1")
        monkeypatch.setenv("TOURNAMENT_OPEN_LEAGUES", raw)
        assert value_filter.tournament_all_leagues() is False
        assert not value_filter.league_allowed(BANNED)

    @pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on"])
    def test_valori_veri_aprono(self, monkeypatch, raw):
        monkeypatch.setenv("TOURNAMENT_MODE", "1")
        monkeypatch.setenv("TOURNAMENT_OPEN_LEAGUES", raw)
        assert value_filter.tournament_all_leagues() is True

    def test_lega_vuota_resta_vietata(self, monkeypatch):
        """Fail-closed anche col torneo aperto: senza sapere cosa si gioca non
        si ordina (stessa regola di `auto_bet`)."""
        monkeypatch.setenv("TOURNAMENT_MODE", "1")
        monkeypatch.setenv("TOURNAMENT_OPEN_LEAGUES", "1")
        assert value_filter.league_allowed("") is False

    def test_strategia_resta_il_fallback_severo(self, monkeypatch):
        """L'apertura allarga l'UNIVERSO, non le soglie: sulle leghe vietate
        resta `DEFAULT_LEAGUE_STRATEGY` (edge minimo alto, kelly 0.5x, cap
        0.5%), non i parametri generosi del core."""
        monkeypatch.setenv("TOURNAMENT_MODE", "1")
        monkeypatch.setenv("TOURNAMENT_OPEN_LEAGUES", "1")
        s = value_filter.get_league_strategy(BANNED)
        assert s == value_filter.DEFAULT_LEAGUE_STRATEGY
        core = value_filter.get_league_strategy(ALLOWED)
        assert s["max_stake"] <= core["max_stake"]
        # e il tier resta dichiarato `blocked`: la telemetria non mente
        assert value_filter.league_tier(BANNED) == "blocked"

    def test_is_sane_vietata_a_torneo_chiuso(self):
        """A torneo chiuso il rifiuto per lega resta quello di sempre."""
        ok, why = value_filter.is_sane(0.60, 1.85, 0.05, 0.55,
                                       league=BANNED, market="1X2")
        assert ok is False
        assert "esclusa" in why

    def test_is_sane_vietata_a_torneo_aperto(self, monkeypatch):
        """Col torneo aperto la stessa riga passa il gate di lega (edge +5pp,
        EV +5%): il filtro che resta e' quello di valore, non quello di lega."""
        monkeypatch.setenv("TOURNAMENT_MODE", "1")
        monkeypatch.setenv("TOURNAMENT_OPEN_LEAGUES", "1")
        ok, why = value_filter.is_sane(0.60, 1.85, 0.05, 0.55,
                                       league=BANNED, market="1X2")
        assert ok is True, why

    def test_corsia_ordini_apre_la_lega_vietata(self, temp_db, monkeypatch):
        """End-to-end sulla corsia che piazza: a torneo aperto il candidato di
        una lega vietata diventa ordinabile; a torneo chiuso NO (controprova)."""
        _seed("m-banned", BANNED)
        assert "m-banned" not in _picks()          # default: escluso
        monkeypatch.setenv("TOURNAMENT_MODE", "1")
        monkeypatch.setenv("TOURNAMENT_OPEN_LEAGUES", "1")
        assert "m-banned" in _picks()              # torneo: ammesso

    def test_corsia_ordini_resta_fail_closed_sulla_lega_assente(
            self, temp_db, monkeypatch):
        """Il fix del 17/09 (lega assente = nessun ordine) NON viene scavalcato
        dall'apertura del torneo."""
        monkeypatch.setenv("TOURNAMENT_MODE", "1")
        monkeypatch.setenv("TOURNAMENT_OPEN_LEAGUES", "1")
        start = (datetime.now(timezone.utc) + timedelta(hours=3)) \
            .isoformat().replace("+00:00", "Z")
        tracker.save_match("m-noleague", "", "Osasuna", "Getafe", start)
        tracker.save_prediction("m-noleague", "1X2", "Osasuna", 1.65, 0.52, 0.08,
                                market_prob=0.60, market_edge=0.07,
                                status="value")
        assert "m-noleague" not in _picks()


class TestTierAPagamentoBlocked:
    """`ORACLE_PAID_TIERS` con `blocked`/`all` (12/10/2026): senza il tier
    `blocked` le leghe aperte dal torneo resterebbero `tier_not_paid` ->
    `no_oracle` -> zero ordini (l'apertura sarebbe inerte)."""

    def test_blocked_ammesso(self, monkeypatch):
        monkeypatch.setenv("ORACLE_PAID_TIERS", "core,probation,blocked")
        assert "blocked" in value_filter.oracle_paid_tiers()
        assert value_filter.is_paid_oracle_league(BANNED) is True

    def test_all_espande_a_tutti_i_tier(self, monkeypatch):
        monkeypatch.setenv("ORACLE_PAID_TIERS", "all")
        assert value_filter.oracle_paid_tiers() == frozenset(
            {"core", "probation", "blocked"})

    def test_default_non_cambia(self, monkeypatch):
        monkeypatch.delenv("ORACLE_PAID_TIERS", raising=False)
        assert value_filter.oracle_paid_tiers() == frozenset({"core"})
        assert value_filter.is_paid_oracle_league(BANNED) is False

    def test_nomi_ignoti_scartati_senza_aprire(self, monkeypatch):
        """Meta' di un valore sbagliato non deve accendere una spesa diversa
        da quella dichiarata."""
        monkeypatch.setenv("ORACLE_PAID_TIERS", "blocked,boh")
        assert value_filter.oracle_paid_tiers() == frozenset({"blocked"})

    def test_lega_vuota_non_paga_anche_con_blocked_ammesso(self, monkeypatch):
        """`league_tier("")` risponde `blocked`: col tier `blocked` ammesso una
        lega SENZA nome finirebbe nello stesso bucket delle leghe aperte dal
        torneo. Il gate di spesa deve restare fail-closed come `league_allowed`
        (una fetch pagata su una lega ignota non si puo' giustificare)."""
        monkeypatch.setenv("ORACLE_PAID_TIERS", "all")
        assert value_filter.is_paid_oracle_league("") is False
        assert value_filter.is_paid_oracle_league("   ") is False
        # controprova: una lega vera `blocked` paga normalmente
        assert value_filter.is_paid_oracle_league(BANNED) is True


class TestCoppeEuropeeInProbation:
    """Coppe europee in probation (23/10/2026).

    Contesto: il board serale di coppe (Europa e Conference League) era il 100%
    dei pick NON giocabili — `blocked` non per una misura, ma per default. Le
    coppe non hanno un ROI storico che le promuova nel core e non sono nemmeno
    fra le leghe misurate negative: entrano quindi in probation, con la
    strategia piu' severa del progetto (edge piu' alto, Kelly 0.4x, cap 0.5%).

    Questi test difendono le tre cose che le rendono davvero giocabili: il
    tier, l'alias dell'etichetta SX (senza, la coppa e' insaldabile) e la
    rotazione delle quote (senza, la lega e' dormiente di fatto).
    """

    CUPS = ("Europa League", "Conference League")

    def test_entrambe_sono_in_probation(self):
        for league in self.CUPS:
            assert value_filter.league_tier(league) == "probation", league
            assert value_filter.league_allowed(league) is True, league

    def test_strategia_probation_piu_severa_del_core(self):
        for league in self.CUPS:
            strat = value_filter.get_league_strategy(league)
            assert strat["min_edge"] >= value_filter.PROBATION_STRATEGY["min_edge"]
            assert strat["max_stake"] <= 0.01
            assert strat["kelly_mult"] <= 0.5

    def test_varianti_del_nome_verso_la_chiave_sports_map(self):
        """"UEFA Conference League" e "Europa Conference League" sono lo
        STESSO torneo: la variante deve portare al nome canonico, mai il
        contrario (la trappola del 29/09 sulle nazionali)."""
        for raw in ("UEFA Conference League", "Europa Conference League",
                    "UEFA Europa Conference League"):
            assert value_filter.canonical_league(raw) == "Conference League"
        assert value_filter.canonical_league("UEFA Europa League") == "Europa League"

    def test_etichetta_sx_risolta_e_saldabile(self):
        import odds_api
        for label, want in (("Conference League_UEFA", "Conference League"),
                            ("Europa League_UEFA", "Europa League")):
            # `_league_sx_to_sports_map` -> nome canonico (chiave del gate)
            assert sx_signals._league_sx_to_sports_map(label) == want
            # `league_to_sport` -> SPORT KEY (quella che usa il settlement)
            assert sx_signals.league_to_sport(label) == odds_api.SPORTS_MAP[want]
            # saldabile: la chiave esiste in SPORTS_MAP (settlement the-odds-api)
            assert want in odds_api.SPORTS_MAP

    def test_rotazione_non_dormiente(self):
        import odds_api
        for league in self.CUPS:
            days = odds_api.SPORTS_INTERVAL_DAYS[league]
            assert days <= 7, (league, days)

    def test_taglio_del_cap_e_del_kelly_rispettato_dal_motore(self):
        """La probation deve costare MENO del core a parita' di segnale."""
        core = value_filter.get_league_strategy("Premier League")
        cups = value_filter.get_league_strategy("Conference League")
        assert cups["kelly_mult"] < core["kelly_mult"]
        assert cups["min_edge"] >= core["min_edge"]

    def test_serie_a_resta_vietata(self):
        """Controprova: aprire le coppe non deve riaprire le leghe misurate
        negative (Serie A -5.9%, La Liga -6.3% sul backtest 12/09)."""
        assert value_filter.league_allowed("Serie A") is False
        assert value_filter.league_allowed("La Liga") is False
