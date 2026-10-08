"""Test del settlement NATIVO SX (12/09/2026).

Perche': le leghe che SX scansiona ma the-odds-api NON copre (Primera A
Colombia, Primera Nacional Argentina, K2-League) lasciavano bet e
previsioni aperte per sempre — nessuna fonte esterna puo' refertarle.
Ma l'esito lo conosce SX stesso: `markets/find` (market_hash salvato sulla
bet) ritorna outcome + punteggi del mercato saldato, e `/markets/active`
esponde i punteggi live degli eventi in corso. Entrambi GRATUITI e senza
matching per nome (la causa principale delle bet rimaste aperte il 12/09).

Semantica SX: `outcome` e' relativo alla GAMBA del mercato binario
(1 = vince outcomeOne, 2 = vince outcomeTwo, 0 = void), mentre i punteggi
teamOneScore/teamTwoScore sono SEMPRE quelli dell'evento (casa vs trasferta):
il verdetto 1X2 lo emette comunque settle_bets/settle_predictions dai
punteggi (fail-closed), non dall'outcome della gamba.
"""
import time
from datetime import datetime, timedelta, timezone

import pytest

import sx_signals
import tracker
from execution_engine import SX_PROB_SCALE


def _pct(price: float) -> int:
    return int(SX_PROB_SCALE / price)


NOW_S = int(time.time())


def _find_market(market_hash, outcome=1, sh=2, sa=0, home="Alpha",
                 away="Beta", league="Primera A"):
    """Mercato saldato come lo ritorna markets/find (camposet reale)."""
    return {"marketHash": market_hash, "type": 1, "status": "ACTIVE",
            "outcome": outcome, "teamOneScore": sh, "teamTwoScore": sa,
            "teamOneName": home, "teamTwoName": away,
            "outcomeOneName": home, "outcomeTwoName": f"Not {home}",
            "leagueLabel": league, "sportXeventId": "LEV1",
            "gameTime": NOW_S - 7200}


def _active_market(ev_id="LEV2", ko_s=None, sh=1, sa=1, home="Gamma",
                   away="Delta", league="K2-League"):
    return {"marketHash": "0xact" + ev_id, "type": 1,
            "sportXeventId": ev_id, "gameTime": ko_s or (NOW_S - 3600),
            "teamOneName": home, "teamTwoName": away,
            "outcomeOneName": home, "outcomeTwoName": f"Not {home}",
            "leagueLabel": league, "teamOneScore": sh, "teamTwoScore": sa}


class FakeSxSettle:
    """Provider SX canned per il settlement (nessuna rete)."""

    def __init__(self, find_data=None, active_data=None):
        self.find_data = find_data or []
        self.active_data = active_data or []
        self.find_calls = []
        self.active_calls = 0

    def _get(self, path, params=None):
        if path == "markets/find":
            self.find_calls.append(params)
            return {"status": "success", "data": list(self.find_data)}
        if path == "markets/active":
            self.active_calls += 1
            return {"status": "success",
                    "data": {"markets": list(self.active_data),
                             "nextKey": None}}
        raise AssertionError(f"endpoint inatteso: {path}")


@pytest.fixture()
def temp_db(monkeypatch):
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        monkeypatch.setattr(tracker, "DB_PATH", db_path)
        tracker.init_db()
        yield db_path


@pytest.fixture(autouse=True)
def sources_on(monkeypatch):
    """Fonti punteggi configurate: il percorso SX nativo e' attivo."""
    monkeypatch.setenv("ODDS_API_KEY", "test")
    monkeypatch.setenv("API_FOOTBALL_KEY", "")


def _bet_outcome(mid):
    conn = tracker._get_conn()
    row = conn.execute("SELECT esito_finale, profit FROM bets WHERE match_id=?",
                       (mid,)).fetchone()
    conn.close()
    return row


def _pred_outcome(mid):
    conn = tracker._get_conn()
    row = conn.execute(
        "SELECT esito_finale, profit FROM predictions WHERE match_id=?",
        (mid,)).fetchone()
    conn.close()
    return row


class TestFindPath:
    """Percorso (1): market_hash delle bet -> markets/find."""

    def test_bet_sx_saldata_dal_mercato_sx(self, temp_db, monkeypatch):
        tracker.save_match("sx-LEV1", "Primera A", "Alpha", "Beta",
                           "2026-09-11T23:15:00Z")
        tracker.save_bet("sx-LEV1", "1X2", "1", "0xh1", 1, 2.3324, 1.0,
                         mode="live")
        prov = FakeSxSettle(find_data=[_find_market("0xh1", outcome=1,
                                                    sh=2, sa=2)])
        res = sx_signals.settle_sx_bets(provider=prov)
        assert res["source"] == "sx" and res["results"] == 1
        assert res["settled"] == 1
        # score 2-2: esito 1 -> lost (i punteggi dell'evento decidono,
        # NON l'outcome della gamba)
        assert _bet_outcome("sx-LEV1") == ("lost", -1.0)

    def test_bet_orfana_senza_riga_matches(self, temp_db, monkeypatch):
        """Bet #21 (senza riga in matches): si salda lo stesso, nomi dalla
        risposta SX."""
        tracker.save_bet("sx-LEV1", "1X2", "1", "0xh1", 1, 3.252, 1.0,
                         mode="live")
        prov = FakeSxSettle(find_data=[_find_market("0xh1", outcome=1,
                                                    sh=2, sa=1)])
        res = sx_signals.settle_sx_bets(provider=prov)
        assert res["settled"] == 1
        assert _bet_outcome("sx-LEV1")[0] == "won"
        assert _bet_outcome("sx-LEV1")[1] == pytest.approx(2.25)  # round(,2)

    def test_hash_batchati_in_una_sola_chiamata(self, temp_db):
        for i in (1, 2):
            tracker.save_bet(f"sx-LEV{i}", "1X2", "1", f"0xh{i}", 1, 2.0,
                             1.0, mode="live")
        find = [_find_market(f"0xh{i}", sh=1, sa=0) for i in (1, 2)]
        prov = FakeSxSettle(find_data=find)
        sx_signals.settle_sx_bets(provider=prov)
        assert len(prov.find_calls) == 1
        sent = prov.find_calls[0]["marketHashes"]
        assert sent == "0xh1,0xh2"

    def test_punteggi_mancanti_fail_closed(self, temp_db):
        """Nomi o punteggi assenti dalla find: NESSUN risultato salvato."""
        tracker.save_bet("sx-LEV1", "1X2", "1", "0xh1", 1, 2.0, 1.0,
                         mode="live")
        prov = FakeSxSettle(find_data=[
            {"marketHash": "0xh1", "type": 1, "outcome": 1,
             "teamOneName": "Alpha", "teamTwoName": "Beta"}])
        res = sx_signals.settle_sx_bets(provider=prov)
        assert res["results"] == 0 and res["settled"] == 0
        assert _bet_outcome("sx-LEV1") == (None, None)

    def test_evento_non_concluso_non_salda(self, temp_db):
        """REGRESSION 17/09: la find puo' portare i punteggi di un evento
        ancora in corso (o appena iniziato). Salvarli chiuderebbe la bet a
        partita in corso: la guardia di conclusione (SX_LIVE_MIN_AGE_MS,
        120') tiene la riga APERTA, con i punteggi veri che arrivano al
        giro successivo."""
        tracker.save_bet("sx-LEV1", "1X2", "1", "0xh1", 1, 2.0, 1.0,
                         mode="live")
        in_corso = dict(_find_market("0xh1", sh=0, sa=0))
        in_corso["gameTime"] = NOW_S - 900       # 15 minuti fa: in gioco
        prov = FakeSxSettle(find_data=[in_corso])
        res = sx_signals.settle_sx_bets(provider=prov)
        assert res["results"] == 0 and res["settled"] == 0
        assert _bet_outcome("sx-LEV1") == (None, None)

    def test_evento_concluso_salda_anche_col_guard(self, temp_db):
        """Controprova del guard: a 2h dal kickoff (oltre la soglia) il
        punteggio viene salvato e la bet saldata come prima."""
        tracker.save_bet("sx-LEV1", "1X2", "1", "0xh1", 1, 2.0, 1.0,
                         mode="live")
        prov = FakeSxSettle(find_data=[_find_market("0xh1", sh=3, sa=1)])
        res = sx_signals.settle_sx_bets(provider=prov)
        assert res["results"] == 1 and res["settled"] == 1
        assert _bet_outcome("sx-LEV1")[0] == "won"

    def test_hash_sconosciuti_ignorati(self, temp_db):
        tracker.save_bet("sx-LEV1", "1X2", "1", "0xh1", 1, 2.0, 1.0,
                         mode="live")
        prov = FakeSxSettle(find_data=[_find_market("0xALTRO", sh=9, sa=9)])
        res = sx_signals.settle_sx_bets(provider=prov)
        assert res["results"] == 0 and res["settled"] == 0

    def test_senza_fonti_nessuna_rete(self, temp_db, monkeypatch):
        """Chiavi assenti (fail-closed offline): il percorso SX non viene
        nemmeno interrogato (stessa logica delle fonti esterne)."""
        monkeypatch.setenv("ODDS_API_KEY", "")
        tracker.save_bet("sx-LEV1", "1X2", "1", "0xh1", 1, 2.0, 1.0,
                         mode="live")
        prov = FakeSxSettle(find_data=[_find_market("0xh1")])
        res = sx_signals.settle_sx_bets(provider=prov)
        assert prov.find_calls == [] and prov.active_calls == 0
        assert res["settled"] == 0 and res["source"] is None

    def test_gate_env_sx_native_settlement(self, temp_db, monkeypatch):
        """SX_NATIVE_SETTLEMENT=0: percorso SX disattivabile da env."""
        monkeypatch.setenv("SX_NATIVE_SETTLEMENT", "0")
        tracker.save_bet("sx-LEV1", "1X2", "1", "0xh1", 1, 2.0, 1.0,
                         mode="live")
        prov = FakeSxSettle(find_data=[_find_market("0xh1")])
        res = sx_signals.settle_sx_bets(provider=prov)
        assert prov.find_calls == []
        assert res["settled"] == 0 and res["source"] is None


def _tennis_market(market_hash, outcome=1, home="Adrian Mannarino",
                   away="Nikoloz Basilashvili", league="ATP - Shanghai",
                   sh=None, sa=None, mtype=52, with_outcome=True):
    """Mercato TENNIS (type 52, 2 esiti) come lo ritorna markets/find.

    Sul tennis i campi `teamOneScore`/`teamTwoScore` sono GAME e possono
    mancare del tutto sui mercati ritirati: `sh`/`sa` restano None per
    riprodurre il caso reale (bet #14).
    """
    m = {"marketHash": market_hash, "type": mtype, "status": "INACTIVE",
         "teamOneName": home, "teamTwoName": away,
         "outcomeOneName": home, "outcomeTwoName": away,
         "outcomeVoidName": "NO_CONTEST", "leagueLabel": league,
         "sportXeventId": "L20430101", "sportId": 6,
         "gameTime": NOW_S - 8 * 3600}
    if with_outcome:
        m["outcome"] = outcome
    if sh is not None:
        m["teamOneScore"] = sh
    if sa is not None:
        m["teamTwoScore"] = sa
    return m


class TestMercatoDueEsiti:
    """Type 52 (tennis / eSports / "12 senza pareggio"): il verdetto e' il
    campo `outcome` SALDATO DALL'EXCHANGE, non i punteggi (03/10/2026).

    Prima di questo percorso il tennis si saldava (male) dai game e i
    mercati ritirati senza punteggi restavano aperti fino alla scadenza
    push — con P/L 0 al posto del verdetto vero.
    """

    def test_bet_14_senza_punteggi_usa_l_outcome(self, temp_db):
        """REGRESSION bet #14 (Mannarino-Basilashvili): il tennis NON porta
        punteggi sui mercati ritirati. `outcome` 1 = ha vinto Mannarino ->
        la nostra selezione 2 ha PERSO -1.31 (prima: riga aperta fino alla
        scadenza push, con +1.31 di P/L INVENTATO)."""
        mid = "sx-tennis-0x0691"
        tracker.save_match(mid, "ATP - Shanghai", "Adrian Mannarino",
                           "Nikoloz Basilashvili", "2026-10-07T05:30:00Z")
        tracker.save_bet(mid, "TENNIS", "2", "0x0691", 2, 2.0408, 1.31,
                         mode="live")
        prov = FakeSxSettle(find_data=[_tennis_market("0x0691", outcome=1)])
        res = sx_signals.settle_sx_bets(provider=prov)
        assert res["results"] == 1 and res["settled"] == 1
        assert _bet_outcome(mid) == ("lost", -1.31)

    def test_selezione_1_vincente_con_outcome_1(self, temp_db):
        mid = "sx-tennis-0x0001"
        tracker.save_bet(mid, "TENNIS", "1", "0x0001", 1, 2.0, 1.0,
                         mode="live")
        prov = FakeSxSettle(find_data=[_tennis_market("0x0001", outcome=1)])
        sx_signals.settle_sx_bets(provider=prov)
        assert _bet_outcome(mid) == ("won", 1.0)

    def test_punteggi_pareggiati_non_decidono_il_verdetto(self, temp_db):
        """REGRESSION bet #19 (Giron-Baez): game 13-13 — i game possono
        pareggiare con un vincitore — ma `outcome` 2 dice che ha vinto la
        selezione 2. I punteggi l'avevano registrata PERSA (-1.34) invece
        che VINTA (+1.15)."""
        mid = "sx-tennis-0x4be2"
        tracker.save_match(mid, "ATP - Shanghai", "Marcos Giron",
                           "Sebastian Baez", "2026-10-08T07:00:00Z")
        tracker.save_bet(mid, "TENNIS", "2", "0x4be2", 2, 1.8561, 1.34,
                         mode="live")
        prov = FakeSxSettle(find_data=[
            _tennis_market("0x4be2", outcome=2, home="Marcos Giron",
                           away="Sebastian Baez", sh=13, sa=13)])
        sx_signals.settle_sx_bets(provider=prov)
        assert _bet_outcome(mid) == ("won", 1.15)

    def test_outcome_void_non_chiude(self, temp_db):
        """outcome 0 (NO_CONTEST), senza punteggi: fail-closed, nessuna
        chiusura qui. Ci pensa la scadenza (`expire_stale_sx_rows` -> push,
        che per un void e' il P/L corretto: nessun verdetto inventato)."""
        mid = "sx-tennis-0x0002"
        tracker.save_bet(mid, "TENNIS", "1", "0x0002", 1, 2.0, 1.0,
                         mode="live")
        prov = FakeSxSettle(find_data=[_tennis_market("0x0002", outcome=0)])
        res = sx_signals.settle_sx_bets(provider=prov)
        assert res["results"] == 0 and res["settled"] == 0
        assert _bet_outcome(mid) == (None, None)

    def test_outcome_assente_non_chiude(self, temp_db):
        """Mercato NON saldato (nessun `outcome`): fail-closed anche coi
        punteggi presenti (il percorso find del tennis non li usa piu',
        ma la riga non deve chiudersi per un verdetto che non esiste)."""
        mid = "sx-tennis-0x0003"
        tracker.save_bet(mid, "TENNIS", "1", "0x0003", 1, 2.0, 1.0,
                         mode="live")
        prov = FakeSxSettle(find_data=[
            _tennis_market("0x0003", with_outcome=False)])
        res = sx_signals.settle_sx_bets(provider=prov)
        assert res["results"] == 0 and res["settled"] == 0
        assert _bet_outcome(mid) == (None, None)

    def test_type1_calcio_ignora_l_outcome_della_gamba(self, temp_db):
        """Sul 1X2 calcio (type 1) la regola NON cambia: il verdetto si
        deriva dai punteggi, MAI dal campo `outcome` della gamba (qui
        `outcome` 2 e punteggi 3-0: comanda il 3-0 -> la selezione 2 perde).
        """
        mid = "sx-LEV9"
        tracker.save_bet(mid, "1X2", "2", "0xL9", 2, 3.0, 1.0,
                         mode="live")
        prov = FakeSxSettle(find_data=[
            _find_market("0xL9", outcome=2, sh=3, sa=0)])
        sx_signals.settle_sx_bets(provider=prov)
        assert _bet_outcome(mid)[0] == "lost"

    def test_previsione_tennis_chiusa_col_verdetto_dell_exchange(
            self, temp_db, monkeypatch):
        """Anche la PREVISIONE dello stesso match (telemetria) si chiude col
        verdetto dell'exchange: il risultato salvato dal percorso find ha la
        chiave sx-<id>, quindi `settle_predictions` la aggancia per match_id.

        NOTA: il percorso find parte dal `market_id` delle BET — un match con
        la SOLA previsione (nessuna puntata) non storea market_id nel ledger e
        non e' coperto qui. E' telemetria, non denaro: nella corsia tennis la
        previsione e la puntata nascono insieme.
        """
        monkeypatch.setattr(
            "odds_api.fetch_scores",
            lambda sport=None, days_from=3: (_ for _ in ()).throw(
                AssertionError("no crediti")))
        mid = "sx-tennis-0x0004"
        tracker.save_match(mid, "ATP - Shanghai", "Adrian Mannarino",
                           "Nikoloz Basilashvili", "2026-10-07T05:30:00Z")
        tracker.save_bet(mid, "TENNIS", "1", "0x0004", 1, 1.9, 1.0,
                         mode="live")
        tracker.save_prediction(mid, "TENNIS", "2", 2.0408, 0.49, 0.03)
        prov = FakeSxSettle(find_data=[_tennis_market("0x0004", outcome=1)])
        res = sx_signals.settle_sx_bets(provider=prov)
        assert res["predictions"] == 1 and res["settled"] == 1
        assert _bet_outcome(mid)[0] == "won"
        assert _pred_outcome(mid)[0] == "lost"


class TestActivePath:
    """Percorso (2): match con riga nel ledger -> punteggi live su active."""

    def test_previsione_senza_bet_saldata(self, temp_db, monkeypatch):
        """Match con SOLE previsioni (niente market_id sul ledger): il
        percorso find non lo vede, lo copre active (punteggi live)."""
        monkeypatch.setattr("odds_api.fetch_scores",
                            lambda sport=None, days_from=3: (
                                (_ for _ in ()).throw(AssertionError(
                                    "no crediti")))
                            )
        tracker.save_match("sx-LEV2", "K2-League", "Gamma", "Delta",
                           "2026-09-12T15:00:00Z")
        tracker.save_prediction("sx-LEV2", "1X2", "X", 3.5, 0.30, 0.05)
        prov = FakeSxSettle(active_data=[
            _active_market(ev_id="LEV2", ko_s=NOW_S - 3 * 3600, sh=1, sa=1)])
        res = sx_signals.settle_sx_bets(provider=prov)
        assert res["results"] == 1 and res["predictions"] == 1
        assert _pred_outcome("sx-LEV2") == ("won", 1.0 * (3.5 - 1))

    def test_match_futuro_non_saldata(self, temp_db, monkeypatch):
        """Kickoff futuro su active: nessun punteggio salvato (finestra
        live -90')."""
        tracker.save_match("sx-LEV3", "Primera A", "Alpha", "Beta",
                           "2026-09-13T15:00:00Z")
        tracker.save_prediction("sx-LEV3", "1X2", "1", 1.7, 0.60, 0.04)
        prov = FakeSxSettle(active_data=[
            _active_market(ev_id="LEV3", ko_s=NOW_S + 3600, sh=0, sa=0)])
        res = sx_signals.settle_sx_bets(provider=prov)
        assert res["results"] == 0 and res["predictions"] == 0

    def test_esterno_solo_per_match_senza_risultato_sx(self, temp_db,
                                                       monkeypatch):
        """Con il risultato sx-* gia' salvato il match NON passa alle fonti
        esterne (zero crediti): il filtro `missing` esclude i coperti."""
        tracker.save_match("sx-LEV4", "Primera A", "Alpha", "Beta",
                           "2026-09-11T23:15:00Z")
        tracker.save_prediction("sx-LEV4", "1X2", "1", 2.0, 0.55, 0.05)

        def _boom(*a, **k):
            raise AssertionError("fetch_scores non deve essere chiamata")

        monkeypatch.setattr("odds_api.fetch_scores", _boom)
        prov = FakeSxSettle(active_data=[
            _active_market(ev_id="LEV4", ko_s=NOW_S - 3 * 3600, sh=2, sa=0)])
        res = sx_signals.settle_sx_bets(provider=prov)
        assert res["results"] == 1 and res["predictions"] == 1
        assert res["source"] == "sx"

    def test_idempotenza_giro_doppio(self, temp_db, monkeypatch):
        """Secondo giro: i punteggi gia' salvati non vengono riscritti e le
        righe chiuse restano chiuse (INSERT OR REPLACE + esito_finale)."""
        tracker.save_match("sx-LEV5", "Primera A", "Alpha", "Beta",
                           "2026-09-11T23:15:00Z")
        tracker.save_bet("sx-LEV5", "1X2", "2", "0xh5", 1, 3.0, 1.0,
                         mode="live")
        prov = FakeSxSettle(find_data=[_find_market("0xh5", sh=0, sa=1)])
        r1 = sx_signals.settle_sx_bets(provider=prov)
        r2 = sx_signals.settle_sx_bets(provider=prov)
        assert r1["settled"] == 1 and r2["settled"] == 0
        assert r2["results"] == 0
        assert _bet_outcome("sx-LEV5") == ("won", 1.0 * (3.0 - 1))


class TestScadenzaRigheStale:
    """Scadenza (12/09): le righe sx-* con kickoff > SX_STALE_DAYS giorni e
    senza NESSUN risultato si chiudono come push (P/L 0) e smettono di
    generare fetch_scores infiniti (crediti sprecati)."""

    def test_riga_stale_chiusa_push(self, temp_db, monkeypatch):
        monkeypatch.setenv("SX_STALE_DAYS", "2")
        # kickoff 4 giorni fa: scaduta
        tracker.save_match("sx-OLD1", "Primera A", "Alpha", "Beta",
                           "2026-09-08T15:00:00Z")
        tracker.save_prediction("sx-OLD1", "1X2", "1", 1.7, 0.60, 0.04)
        tracker.save_bet("sx-OLD1", "1X2", "1", "0xh1", 1, 1.7, 1.0,
                         mode="live")
        prov = FakeSxSettle()   # nessun risultato dalle fonti
        res = sx_signals.settle_sx_bets(provider=prov)
        assert res["expired"] == {"bets": 1, "predictions": 1}
        conn = tracker._get_conn()
        b = conn.execute("SELECT esito_finale, profit FROM bets WHERE "
                         "match_id='sx-OLD1'").fetchone()
        p = conn.execute("SELECT esito_finale, profit FROM predictions WHERE "
                         "match_id='sx-OLD1'").fetchone()
        conn.close()
        assert b == ("push", 0.0) and p == ("push", 0.0)

    def test_riga_con_risultato_non_scade(self, temp_db, monkeypatch):
        """Se le fonti hanno salvato il risultato, la riga e' gia' chiusa col
        verdetto vero: la scadenza non la tocca (e il settle normale la
        chiusa prima della scadenza)."""
        monkeypatch.setenv("SX_STALE_DAYS", "2")
        tracker.save_match("sx-OLD2", "Primera A", "Alpha", "Beta",
                           "2026-09-08T15:00:00Z")
        tracker.save_prediction("sx-OLD2", "1X2", "1", 1.7, 0.60, 0.04)
        tracker.save_result("sx-OLD2", "Primera A", "Alpha", "Beta", 2, 0,
                            datetime.now(timezone.utc).isoformat())
        prov = FakeSxSettle()
        res = sx_signals.settle_sx_bets(provider=prov)
        assert res["expired"] == {"bets": 0, "predictions": 0}
        assert _pred_outcome("sx-OLD2") == ("won", 0.7)

    def test_match_recente_non_scade(self, temp_db, monkeypatch):
        monkeypatch.setenv("SX_STALE_DAYS", "5")
        # Kickoff RELATIVO a now (lezione 15/09 sui test trappola sulla data):
        # con una data FISSA ("2026-09-11", 6 giorni fa il 17/09) il test
        # scadeva da solo col calendario — un falso allarme che arriva sempre
        # nel momento peggiore, senza che nulla fosse rotto nel codice.
        recente = (datetime.now(timezone.utc) - timedelta(days=1))\
            .isoformat().replace("+00:00", "Z")
        tracker.save_match("sx-NEW1", "Primera A", "Alpha", "Beta", recente)
        tracker.save_prediction("sx-NEW1", "1X2", "1", 1.7, 0.60, 0.04)
        prov = FakeSxSettle()
        res = sx_signals.settle_sx_bets(provider=prov)
        assert res["expired"] == {"bets": 0, "predictions": 0}
        assert _pred_outcome("sx-NEW1") == (None, None)

    def test_created_at_iso_con_T_non_ritarda_la_scadenza(self, temp_db,
                                                         monkeypatch):
        """Il confronto SQL non deve degradare in ordine ALFABETICO (fix 17/09).

        `created_at` e' salvato in ISO con 'T' ('2026-09-12T07:11:39') mentre
        `datetime('now', ?)` produce il formato con lo SPAZIO
        ('2026-09-12 10:48:40'): fra stringhe 'T' (0x54) > ' ' (0x20), quindi
        a parita' di giorno la riga risultava piu' NUOVA del cutoff e la
        scadenza arrivava con ~1 giorno di ritardo (misurato sul container:
        `overdue_orphans` 2 invece di 0). Qui la riga ha 5 giorni e 1 ora:
        deve scadere SUBITO.
        """
        monkeypatch.setenv("SX_STALE_DAYS", "5")
        vecchia = (datetime.now(timezone.utc)
                   - timedelta(days=5, hours=1)).isoformat()
        # ISO con 'T' (e senza fuso, come le righe scritte dal ledger).
        tracker.save_prediction("sx-ISO1", "1X2", "1", 1.7, 0.60, 0.04)
        conn = tracker._get_conn()
        conn.execute("UPDATE predictions SET created_at=? "
                     "WHERE match_id='sx-ISO1'", (vecchia,))
        conn.commit()
        conn.close()
        assert "T" in vecchia
        # Riga orfana: nessuna riga in `matches` (ramo creato il 13/09).
        res = sx_signals.settle_sx_bets(provider=FakeSxSettle())
        assert res["expired"]["predictions"] >= 1
        assert _pred_outcome("sx-ISO1") == ("push", 0.0)

    def test_commence_time_con_Z_non_ritarda_la_scadenza(self, temp_db,
                                                         monkeypatch):
        """Stesso confronto, lato `matches.commence_time` (ISO con 'Z')."""
        monkeypatch.setenv("SX_STALE_DAYS", "5")
        vecchio = (datetime.now(timezone.utc)
                   - timedelta(days=5, hours=1)).isoformat()
        tracker.save_match("sx-ISO2", "Primera A", "Alpha", "Beta",
                           vecchio.replace("+00:00", "Z"))
        tracker.save_prediction("sx-ISO2", "1X2", "1", 1.7, 0.60, 0.04)
        res = sx_signals.settle_sx_bets(provider=FakeSxSettle())
        assert res["expired"]["predictions"] >= 1
        assert _pred_outcome("sx-ISO2") == ("push", 0.0)

    def test_bet_orfana_senza_matches_scade(self, temp_db, monkeypatch):
        """Ramo orfani: la bet del batch 09/09 senza riga in `matches` non ha
        kickoff -> la scadenza usa `created_at`. Senza questo ramo resterebbe
        aperta per sempre e finirebbe in `missing` a ogni giro (fetch_scores
        a credito sprecato)."""
        monkeypatch.setenv("SX_STALE_DAYS", "2")
        tracker.save_bet("sx-ORF1", "1X2", "1", "0xorf1", 1, 3.25, 1.0,
                         mode="live")
        tracker.save_prediction("sx-ORF1", "1X2", "1", 3.25, 0.31, 0.02)
        conn = tracker._get_conn()
        conn.execute("UPDATE bets SET created_at=datetime('now', '-5 days') "
                     "WHERE match_id='sx-ORF1'")
        conn.execute("UPDATE predictions SET created_at=datetime('now', "
                     "'-5 days') WHERE match_id='sx-ORF1'")
        conn.commit()
        conn.close()
        prov = FakeSxSettle()   # mercato non piu' attivo: find tornerebbe vuoto
        res = sx_signals.settle_sx_bets(provider=prov)
        assert res["expired"] == {"bets": 1, "predictions": 1}
        assert _bet_outcome("sx-ORF1") == ("push", 0.0)
        assert _pred_outcome("sx-ORF1") == ("push", 0.0)

    def test_orfana_recente_non_scade(self, temp_db, monkeypatch):
        monkeypatch.setenv("SX_STALE_DAYS", "2")
        tracker.save_bet("sx-ORF2", "1X2", "1", "0xorf2", 1, 1.75, 1.0,
                         mode="live")
        prov = FakeSxSettle()
        res = sx_signals.settle_sx_bets(provider=prov)
        assert res["expired"] == {"bets": 0, "predictions": 0}
        assert _bet_outcome("sx-ORF2") == (None, None)

    def test_orfana_non_sx_senza_matches_scade(self, temp_db, monkeypatch):
        """13/09: il ramo orfani NON e' piu' limitato a `sx-%`. Una riga
        senza partita in `matches` (es. la previsione OU del 01/09 rimasta
        senza match) e' insaldabile per costruzione — nessun kickoff, nessun
        nome squadra da abbinare: scade come le altre invece di restare
        aperta per sempre."""
        monkeypatch.setenv("SX_STALE_DAYS", "2")
        mid = "6185eb4fd80c8e430e49b2f45c4edd5a"
        tracker.save_prediction(mid, "OU", "Over 2.5", 2.4, 0.42, 0.01,
                                status="strong_value")
        conn = tracker._get_conn()
        conn.execute("UPDATE predictions SET created_at=datetime('now', "
                     "'-6 days') WHERE match_id=?", (mid,))
        conn.commit()
        conn.close()
        res = sx_signals.settle_sx_bets(provider=FakeSxSettle())
        assert res["expired"]["predictions"] == 1
        assert _pred_outcome(mid) == ("push", 0.0)

    def test_orfana_non_sx_con_match_non_scade(self, temp_db, monkeypatch):
        """Guardia opposta: con una riga in `matches` la partita e' ancora
        refertabile per nome/lega, quindi la scadenza NON la tocca."""
        monkeypatch.setenv("SX_STALE_DAYS", "2")
        tracker.save_match("api-OLD9", "Serie A", "Alpha", "Beta",
                           "2026-09-08T15:00:00Z")
        tracker.save_prediction("api-OLD9", "1X2", "1", 1.7, 0.60, 0.04)
        res = sx_signals.settle_sx_bets(provider=FakeSxSettle())
        assert res["expired"] == {"bets": 0, "predictions": 0}
        assert _pred_outcome("api-OLD9") == (None, None)

    def test_pausa_blocca_la_scadenza(self, temp_db, monkeypatch):
        monkeypatch.setenv("SX_STALE_DAYS", "2")
        tracker.save_match("sx-OLD3", "Primera A", "Alpha", "Beta",
                           "2026-09-08T15:00:00Z")
        tracker.save_prediction("sx-OLD3", "1X2", "1", 1.7, 0.60, 0.04)
        tracker.set_settlement_paused(True)
        try:
            prov = FakeSxSettle()
            res = sx_signals.settle_sx_bets(provider=prov)
            assert res.get("paused") is True
            assert _pred_outcome("sx-OLD3") == (None, None)
        finally:
            tracker.set_settlement_paused(False)


class TestSettlementPausa:
    def test_pausa_blocca_anche_il_percorso_sx(self, temp_db, monkeypatch):
        """Pausa settlement: nessuna lettura SX, nessuna chiusura."""
        tracker.save_bet("sx-LEV1", "1X2", "1", "0xh1", 1, 2.0, 1.0,
                         mode="live")
        tracker.set_settlement_paused(True)
        try:
            prov = FakeSxSettle(find_data=[_find_market("0xh1")])
            res = sx_signals.settle_sx_bets(provider=prov)
            assert res.get("paused") is True
            assert prov.find_calls == []
            assert _bet_outcome("sx-LEV1") == (None, None)
        finally:
            tracker.set_settlement_paused(False)
