"""Test del FIX mapping leghe (11/09/2026).

Il vecchio fuzzy (SequenceMatcher >= 0.55) restituiva una lega anche quando
era SBAGLIATA — 'Major League Soccer' -> 'League One', 'German Bundesliga'
-> 'Austrian Bundesliga' — cosi' il settlement interrogava the-odds-api sulla
competizione sbagliata e le bet restavano aperte per sempre.

Copre: alias deterministici, fuzzy stretto (mai indovinare), resolver
`league_to_sport`, settlement SX con lega alias/storica e repair delle
leghe gia' salvate.
"""
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import sx_signals
import tracker


@pytest.fixture()
def temp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        monkeypatch.setattr(tracker, "DB_PATH", db_path)
        tracker.init_db()
        yield db_path


@pytest.fixture(autouse=True)
def _offline_sx_provider(monkeypatch):
    """Nessuna rete nei test di settlement: dalla fonte nativa SX (12/09)
    il settlement interroga anche markets/find e markets/active — qui il
    provider SX e' sostituito da un dummy con risposte vuote canned."""
    class _DummyProv:
        name = "sxbet"

        def _get(self, path, params=None):
            if path == "markets/find":
                return {"status": "success", "data": []}
            if path == "markets/active":
                return {"status": "success",
                        "data": {"markets": [], "nextKey": None}}
            return {"status": "success", "data": []}

    monkeypatch.setattr(sx_signals, "SxBetProvider",
                        lambda *a, **k: _DummyProv())


class TestLeagueMapping:
    """Etichette reali osservate sull'API pubblica SX Bet (sample 11/09)."""

    def test_alias_deterministici(self):
        cases = {
            "Major League Soccer": "MLS",
            "USA MLS": "MLS",
            "Liga Profesional": "Argentina Primera",
            "German Bundesliga": "Bundesliga",
            "Jupiler League": "Belgian First Div",
            "K1-League": "K League 1",
            "Primera Division": "Chile Primera",
            "Superliga": "Superliga Danimarca",
            "Europa League_UEFA": "Europa League",
            "Champions League_UEFA": "Champions League",
            "The Championship": "EFL Championship",
            "English Premier League": "Premier League",
            "Campeonato Brasileiro": "Brasileirao",
            "Brasileiro Serie B": "Brazil Serie B",
            "Premiership": "Scottish Premiership",
            "Superettan": "Sweden Superettan",
        }
        for label, want in cases.items():
            assert sx_signals._league_sx_to_sports_map(label) == want, label

    def test_chiavi_sports_map_esatte(self):
        assert sx_signals._league_sx_to_sports_map("Serie A") == "Serie A"
        assert sx_signals._league_sx_to_sports_map("Serie B") == "Serie B"
        assert sx_signals._league_sx_to_sports_map("La Liga 2") == "La Liga 2"
        assert sx_signals._league_sx_to_sports_map("Austrian Bundesliga") \
            == "Austrian Bundesliga"

    def test_rifiuti_espliciti_mai_indovinare(self):
        """Etichette NON coperte da SPORTS_MAP -> None (non 'la piu' simile')."""
        for label in ("Primera Nacional", "Primera A", "K2-League",
                      "First League", "LigaPro", "Division Profesional",
                      "Besta Deild Karla", "USL Championship"):
            assert sx_signals._league_sx_to_sports_map(label) is None, label

    def test_fuzzy_stretto_non_indovina(self):
        # 'First League' non deve diventare 'A-League', 'LigaPro' non '3. Liga'
        assert sx_signals._league_sx_to_sports_map("Campionato Inventato XY") is None
        assert sx_signals._league_sx_to_sports_map("Super League") is None

    def test_league_to_sport_resolver(self):
        assert sx_signals.league_to_sport("Serie A") == "soccer_italy_serie_a"
        assert sx_signals.league_to_sport("Major League Soccer") == "soccer_usa_mls"
        assert sx_signals.league_to_sport("Liga Profesional") \
            == "soccer_argentina_primera_division"
        assert sx_signals.league_to_sport("Campionato Inventato XY") is None
        assert sx_signals.league_to_sport(None) is None
        assert sx_signals.league_to_sport("") is None


class TestSettlementLeghe:
    def test_settle_risolve_lega_alias(self, temp_db, monkeypatch):
        """Lega salvata come etichetta SX: il settlement deve interrogare lo
        sport key GIUSTO (soccer_usa_mls) e chiudere la bet."""
        monkeypatch.setenv("ODDS_API_KEY", "test")
        monkeypatch.setenv("API_FOOTBALL_KEY", "")
        tracker.save_match("sx-L1", "Major League Soccer", "Atlanta United",
                           "Orlando City", "2026-09-09T19:00:00Z")
        tracker.save_bet("sx-L1", "1X2", "1", "0xabc", 1, 2.5, 1.0)
        calls = {}

        def fake_fetch(sport=None, days_from=2):
            calls["sport"] = sport
            return [{"id": "m1", "home_team": "Atlanta United",
                     "away_team": "Orlando City",
                     "commence_time": "2026-09-09T19:00:00Z",
                     "scores": [{"name": "Atlanta United", "score": 2},
                                {"name": "Orlando City", "score": 0}],
                     "completed": True}]

        monkeypatch.setattr("odds_api.fetch_scores", fake_fetch)
        res = sx_signals.settle_sx_bets()
        assert calls["sport"] == "soccer_usa_mls"
        assert res["settled"] == 1
        conn = tracker._get_conn()
        outcome = conn.execute("SELECT esito_finale FROM bets WHERE match_id='sx-L1'").fetchone()[0]
        conn.close()
        assert outcome == "won"

    def test_lega_non_mappata_lascia_aperto_senza_chiamate(self, temp_db, monkeypatch):
        """Etichetta non coperta: nessuna fetch_scores (nessun credito
        bruciato), bet lasciata APERTA (fail-closed) e loggata."""
        monkeypatch.setenv("ODDS_API_KEY", "test")
        monkeypatch.setenv("API_FOOTBALL_KEY", "")
        tracker.save_match("sx-L2", "Campionato Inventato XY", "Alpha", "Beta",
                           "2026-09-09T19:00:00Z")
        tracker.save_bet("sx-L2", "1X2", "1", "0xabc", 1, 2.5, 1.0)
        called = []
        monkeypatch.setattr("odds_api.fetch_scores",
                            lambda *a, **k: called.append(1) or [])
        res = sx_signals.settle_sx_bets()
        assert called == []
        assert res["settled"] == 0
        assert res["open"] == 1

    def test_repair_rimappa_lega_salvata(self, temp_db):
        """Recupero delle partite salvate col fuzzy vecchio (MLS->'League One')."""
        from test_sx_signals import FakeSxProvider
        tracker.save_match("sx-LTEST1", "League One", "Alpha", "Beta",
                           "2026-09-09T19:00:00Z")
        res = sx_signals.repair_sx_leagues(provider=FakeSxProvider())
        assert res["updated"] == 1
        conn = tracker._get_conn()
        lg = conn.execute("SELECT league FROM matches WHERE id='sx-LTEST1'").fetchone()[0]
        conn.close()
        assert lg == "Serie A"

    def test_scan_salva_lega_corretta(self, temp_db, monkeypatch):
        """scan() con label 'Major League Soccer': la partita entra nel ledger
        con la lega giusta (self-heal a ogni giro)."""
        from test_sx_signals import _book_for, _raw_markets

        raw = _raw_markets()
        for m in raw:
            m["leagueLabel"] = "Major League Soccer"

        class _Prov:
            name = "sxbet"

            def _get(self, path, params=None):
                if path == "markets/active":
                    return {"data": {"markets": raw, "nextKey": None}}
                if path == "orderbook-v3/snapshot":
                    return _book_for((params or {}).get("marketHash"))
                raise AssertionError(path)

        monkeypatch.setattr(sx_signals, "expected_goals", lambda h, a: (1.9, 0.8))
        monkeypatch.setattr(sx_signals, "prob_1x2", lambda lh, la: (0.66, 0.20, 0.14))
        monkeypatch.setattr(sx_signals, "adjusted_probability",
                            lambda mp, mkt, price, league=None: mp)
        sx_signals.scan(provider=_Prov())
        conn = tracker._get_conn()
        lg = conn.execute("SELECT league FROM matches WHERE id='sx-LTEST1'").fetchone()
        conn.close()
        assert lg is not None and lg[0] == "MLS"


# ---------------------------------------------------------------------------
# Settlement con nomi TOLLERANTI (12/09/2026). Misurato sul container: i nomi
# SX Bet e the-odds-api non coincidevano e le bet restavano aperte per sempre
# ('Cienciano' vs 'Club Cienciano', 'CR Flamengo' vs 'Flamengo-RJ',
# "Newell's Old Boys" vs 'Newells Old Boys', 'Vila Nova GO' vs 'Vila Nova').
# ---------------------------------------------------------------------------

class TestSettlementNomiTolleranti:
    def _mk(self, mid, home, away, esito="1", league="Serie A"):
        # Kickoff RELATIVO a now (ieri: passato ma non scaduto). Una data fissa
        # diventa una time-bomb che scatta da sola: superati i 5 giorni di
        # `SX_STALE_DAYS` la riga viene scaduta come push e i test che si
        # aspettano la bet ancora APERTA cadono senza che nessuno abbia toccato
        # il codice (osservato il 16/09 con la data fissa del 10/09).
        kickoff = (datetime.now(timezone.utc) - timedelta(days=1)) \
            .isoformat().replace("+00:00", "Z")
        tracker.save_match(mid, league, home, away, kickoff)
        tracker.save_bet(mid, "1X2", esito, "0xabc", 1, 2.0, 1.0)

    def _event(self, home, away, sh, sa, eid="m1"):
        return {"id": eid, "home_team": home, "away_team": away,
                "scores": [{"name": home, "score": sh},
                           {"name": away, "score": sa}],
                "completed": True}

    def _outcome(self, mid):
        conn = tracker._get_conn()
        row = conn.execute("SELECT esito_finale FROM bets WHERE match_id=?",
                           (mid,)).fetchone()
        conn.close()
        return row[0] if row else None

    def test_prefisso_club_chiude_la_bet(self, temp_db, monkeypatch):
        monkeypatch.setenv("ODDS_API_KEY", "test")
        monkeypatch.setenv("API_FOOTBALL_KEY", "")
        self._mk("sx-LN1", "Cienciano", "Montevideo City Torque")
        monkeypatch.setattr("odds_api.fetch_scores",
                            lambda sport=None, days_from=3: [
                                self._event("Club Cienciano",
                                            "Montevideo City Torque", 2, 0)])
        res = sx_signals.settle_sx_bets()
        assert res["settled"] == 1
        assert self._outcome("sx-LN1") == "won"

    def test_codice_stato_e_apostrofo(self, temp_db, monkeypatch):
        """'Vila Nova GO' vs 'Vila Nova' e "Newell's" vs 'Newells'."""
        monkeypatch.setenv("ODDS_API_KEY", "test")
        monkeypatch.setenv("API_FOOTBALL_KEY", "")
        self._mk("sx-LN2", "Vila Nova GO", "Goias", esito="2")
        self._mk("sx-LN3", "Newell's Old Boys", "Velez Sarsfield", esito="1")
        monkeypatch.setattr("odds_api.fetch_scores",
                            lambda sport=None, days_from=3: [
                                self._event("Vila Nova", "Goiás", 2, 0),
                                self._event("Newells Old Boys",
                                            "Velez Sarsfield BA", 1, 1, "m2")])
        res = sx_signals.settle_sx_bets()
        assert res["settled"] == 2
        assert self._outcome("sx-LN2") == "lost"    # esito 2, ha vinto la casa
        assert self._outcome("sx-LN3") == "lost"    # esito 1, pareggio

    def test_guardia_unicita_lascia_aperto(self, temp_db, monkeypatch):
        """Con piu' partite candidate il match e' AMBIGUO: meglio la bet
        aperta che un verdetto col risultato di un'altra partita."""
        monkeypatch.setenv("ODDS_API_KEY", "test")
        monkeypatch.setenv("API_FOOTBALL_KEY", "")
        self._mk("sx-LN4", "Alpha", "Beta")
        monkeypatch.setattr("odds_api.fetch_scores",
                            lambda sport=None, days_from=3: [
                                self._event("Alpha", "Beta", 1, 0, "m1"),
                                self._event("Alpha", "Beta", 3, 0, "m2")])
        res = sx_signals.settle_sx_bets()
        assert res["settled"] == 0
        assert res["open"] == 1
        assert self._outcome("sx-LN4") is None

    def test_salda_anche_le_previsioni_senza_bet(self, temp_db, monkeypatch):
        """Le previsioni SX senza una bet restavano aperte per SEMPRE (il
        settlement guardava solo la tabella `bets`): inquinavano la
        telemetria di calibrazione. Ora il risultato viene salvato anche per
        i match con sole previsioni.

        ⚠️ COPERTURA ESTESA DICHIARATA: dal 04/10/2026 la politica di
        default e' `SETTLEMENT_BETS_ONLY=1` ("il referto segue il DENARO":
        le fonti PAGATE interrogano solo le leghe con una puntata). Qui si
        esercita il percorso esteso — quello che questo tripwire difende —
        accendendo esplicitamente l'escape hatch, come fa `TestResiduoSettlement`
        in `test_settlement_watchdog`: senza, il test misurerebbe la policy
        del giorno invece del comportamento che vuole proteggere.
        """
        monkeypatch.setenv("ODDS_API_KEY", "test")
        monkeypatch.setenv("API_FOOTBALL_KEY", "")
        monkeypatch.setenv("SETTLEMENT_BETS_ONLY", "0")
        tracker.save_match("sx-LNP", "Serie A", "Alpha", "Beta",
                           "2026-09-10T00:00:00Z")
        tracker.save_prediction("sx-LNP", "1X2", "1", 1.8, 0.6, 0.05,
                                status="value")
        monkeypatch.setattr("odds_api.fetch_scores",
                            lambda sport=None, days_from=3: [
                                self._event("Alpha", "Beta", 2, 0)])
        res = sx_signals.settle_sx_bets()
        assert res["open"] == 1          # nessuna bet, ma la previsione c'e'
        assert res["predictions"] == 1
        conn = tracker._get_conn()
        out = conn.execute(
            "SELECT esito_finale FROM predictions WHERE match_id='sx-LNP'"
        ).fetchone()[0]
        conn.close()
        assert out == "won"

    def test_finestra_tre_giorni(self, temp_db, monkeypatch):
        """L'API copre al massimo 3 giorni: con 2 le partite di due sere
        prima restavano fuori e la bet non si saldava mai."""
        import bot
        import inspect
        import odds_api

        assert odds_api.SCORES_DAYS_FROM == 3
        assert "SCORES_DAYS_FROM" in inspect.getsource(bot._update_results)
        monkeypatch.setenv("ODDS_API_KEY", "test")
        monkeypatch.setenv("API_FOOTBALL_KEY", "")
        self._mk("sx-LN5", "Alpha", "Beta")
        seen = {}

        def fake_fetch(sport=None, days_from=None):
            seen["days_from"] = days_from
            return []

        monkeypatch.setattr("odds_api.fetch_scores", fake_fetch)
        sx_signals.settle_sx_bets()
        assert seen["days_from"] == 3


# ---------------------------------------------------------------------------
# Repair dei residui STORICI: partite non piu' su SX -> lega inferita dai
# roster di ALL_LEAGUES (deterministico: serve UNA sola lega compatibile).
# ---------------------------------------------------------------------------

class TestInferenzaLegaDaiRoster:
    def test_coppia_non_ambigua(self):
        assert sx_signals._infer_league_from_teams("Atlanta", "Chicago") == \
            "MLS"

    def test_nomi_sx_agganciano_il_roster(self):
        # I nomi SX reali (report sul container) devono agganciare il roster:
        # senza la risoluzione dei nomi il repair non li avrebbe recuperati.
        assert sx_signals._infer_league_from_teams(
            "Atlanta United", "Orlando City") == "MLS"
        assert sx_signals._infer_league_from_teams(
            "Toronto FC", "Nashville SC") == "MLS"
        assert sx_signals._infer_league_from_teams(
            "Philadelphia Union FC", "FC Cincinnati") == "MLS"

    def test_coppia_ambigua_rifiutata(self):
        # Bologna/Empoli stanno in piu' competizioni (Serie A + Coppa Italia).
        assert sx_signals._infer_league_from_teams("Bologna", "Empoli") is None

    def test_lega_attuale_con_supporto_non_si_tocca(self):
        """Se la lega attuale ha anche UNA sola squadra nel roster, l'etichetta
        SX resta (caso reale: 'Champions League' con rosa incompleta non deve
        diventare 'Europa League')."""
        assert sx_signals._infer_league_from_teams(
            "Fenerbahçe", "AS Roma", "Champions League") is None
        assert sx_signals._roster_support(
            "Champions League", "Fenerbahçe", "AS Roma") >= 1

    def test_lega_attuale_senza_supporto_viene_corretta(self):
        # 'League One' ha roster vuoto: il residuo MLS viene corretto.
        assert sx_signals._roster_support(
            "League One", "Atlanta United", "Orlando City") == 0
        assert sx_signals._infer_league_from_teams(
            "Atlanta United", "Orlando City", "League One") == "MLS"

    def test_residui_belgi_in_premier_league(self):
        """Caso REALE del report sul container: KV Mechelen vs RSC Anderlecht
        salvata come 'Premier League'. Nessuno dei due e' in PL (supporto 0) e
        la coppia sta in una sola lega: Belgian First Div."""
        assert sx_signals._roster_support(
            "Premier League", "KV Mechelen", "RSC Anderlecht") == 0
        assert sx_signals._infer_league_from_teams(
            "KV Mechelen", "RSC Anderlecht", "Premier League") == \
            "Belgian First Div"

    def test_squadre_ignote(self):
        assert sx_signals._infer_league_from_teams("Squadra X",
                                                  "Squadra Y") is None
        assert sx_signals._infer_league_from_teams("", "") is None

    def test_repair_corregge_le_partite_vecchie(self, temp_db):
        """Le partite MLS salvate come 'League One' dal fuzzy vecchio non
        sono piu' sui mercati SX: la lega viene dedotta dalle squadre."""
        from test_sx_signals import FakeSxProvider
        for mid, h, a in (("sx-OLD1", "Atlanta", "Chicago"),
                          ("sx-OLD2", "Atlanta United", "Orlando City"),
                          ("sx-OLD3", "Toronto FC", "Nashville SC")):
            tracker.save_match(mid, "League One", h, a,
                               "2026-08-01T19:00:00Z")
        res = sx_signals.repair_sx_leagues(provider=FakeSxProvider())
        assert res["inferred"] == 3 and res["updated"] == 3
        conn = tracker._get_conn()
        lgs = [r[0] for r in conn.execute(
            "SELECT league FROM matches WHERE id LIKE 'sx-OLD%'").fetchall()]
        conn.close()
        assert lgs == ["MLS", "MLS", "MLS"]

    def test_repair_non_indovina_se_ambiguo(self, temp_db):
        from test_sx_signals import FakeSxProvider
        tracker.save_match("sx-AMB1", "League One", "Bologna", "Empoli",
                           "2026-08-01T19:00:00Z")
        res = sx_signals.repair_sx_leagues(provider=FakeSxProvider())
        assert res["inferred"] == 0 and res["updated"] == 0
        conn = tracker._get_conn()
        lg = conn.execute(
            "SELECT league FROM matches WHERE id='sx-AMB1'").fetchone()[0]
        conn.close()
        assert lg == "League One"     # invariata: mai indovinare
