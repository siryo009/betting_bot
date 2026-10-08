"""Test settlement_watchdog_job: self-healing delle pendenze.

Scenario 01/09: bet piazzate alle 16:40, redeploy alle 23:13 salta il
results_job delle 21:30 -> le bet restano aperte. Il watchdog (ogni 4h)
deve scaricare i risultati, saldare le bet e inviare i verdetti senza
intervento manuale.

Refertazione MIRATA (risparmio crediti the-odds-api): fetch_scores viene
chiamato SOLO per le leghe con scommesse attive (o chiuse da <48h) su
partite già iniziate — zero righe aperte = zero chiamate per quella lega.

I test usano il FLUSSO REALE di _update_results (nessun mock della logica
di saldatura): si monkeypatcha solo il confine esterno (fetch_scores di
odds_api e compute_ratings di rating_engine).
"""

import asyncio
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import tracker
import odds_api
import bot


@pytest.fixture()
def temp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        monkeypatch.setattr(tracker, "DB_PATH", db_path)
        monkeypatch.setattr(odds_api, "CACHE_DIR", Path(td))
        tracker.init_db()
        yield db_path


def _fake_context():
    ctx = MagicMock()
    ctx.bot = MagicMock()
    ctx.bot.send_message = asyncio.coroutine(lambda *a, **k: None)() \
        if False else MagicMock()
    return ctx


def _patch_send(monkeypatch):
    """Cattura l'invio Telegram dei verdetti."""
    calls = []

    async def fake_send(context, text):
        calls.append(text)

    monkeypatch.setattr(bot, "_send_bet_settlements", fake_send)
    return calls


def _patch_fetch_scores(monkeypatch, payload_per_sport):
    """fetch_scores finto: ritorna il payload per lo sport richiesto."""
    def fake_fetch(sport=None, days_from=2):
        return payload_per_sport.get(sport, [])

    monkeypatch.setattr(odds_api, "fetch_scores", fake_fetch)
    # _update_results fa l'import dentro la funzione: patch del modulo
    monkeypatch.setitem(sys.modules, "odds_api", odds_api)


def _patch_ratings(monkeypatch):
    import rating_engine
    monkeypatch.setattr(rating_engine, "compute_ratings", lambda: None)
    monkeypatch.setitem(sys.modules, "rating_engine", rating_engine)


def _patch_admin(monkeypatch):
    monkeypatch.setattr(bot, "_admin_chat_ids", lambda: [])
    import tracker as t
    monkeypatch.setattr(t, "get_subscribers", lambda *a, **k: [])


def _recent_iso(days_ago: int = 1, hours_ago: int = 3) -> str:
    """commence_time recente (entro la finestra di get_leagues_with_open_rows):
    date hardcoded tipo 2026-09-01 restano valide solo il giorno in cui il
    test e' scritto e poi escono dalla finestra (settlement mai eseguito →
    bet aperte per sempre)."""
    return (datetime.now(timezone.utc) - timedelta(days=days_ago,
                                                   hours=hours_ago)).isoformat()


MATCH = {"id": "mX", "home_team": "Inter", "away_team": "Napoli",
         "completed": True,
         "scores": [{"name": "Inter", "score": "2"},
                    {"name": "Napoli", "score": "1"}],
         "last_update": ""}


def test_watchdog_salda_bet_pendente_flusso_reale(temp_db, monkeypatch):
    """Bet aperta + risultato scaricato -> saldatura automatica completa."""
    tracker.save_match("mX", "Serie A", "Inter", "Napoli", _recent_iso())
    tracker.save_analysis("mX", 1.7, 1.0, 0.5, 0.27, 0.23, 0.55,
                          0.10, "1", 2.10, "Pinnacle", "value")
    tracker.save_bet("mX", "1X2", "1", "1.100", 101, 2.10, 10.0)

    _patch_send(monkeypatch)
    _patch_fetch_scores(monkeypatch, {"soccer_italy_serie_a": [dict(MATCH)]})
    _patch_ratings(monkeypatch)
    _patch_admin(monkeypatch)

    asyncio.run(bot.settlement_watchdog_job(_fake_context()))

    rows = tracker.get_bets(limit=10)
    assert rows[0]["esito_finale"] == "won"
    assert rows[0]["profit"] == pytest.approx(11.0)  # 10 * (2.10 - 1)


def test_watchdog_notifica_verdetti_nuovi(temp_db, monkeypatch):
    """Quando il watchdog stesso chiude una bet, il verdetto va a iscritti+admin."""
    tracker.save_match("mX", "Serie A", "Inter", "Napoli", _recent_iso())
    tracker.save_analysis("mX", 1.7, 1.0, 0.5, 0.27, 0.23, 0.55,
                          0.10, "1", 2.10, "Pinnacle", "value")
    tracker.save_bet("mX", "1X2", "1", "1.100", 101, 2.10, 10.0)

    calls = _patch_send(monkeypatch)
    _patch_fetch_scores(monkeypatch, {"soccer_italy_serie_a": [dict(MATCH)]})
    _patch_ratings(monkeypatch)
    _patch_admin(monkeypatch)

    asyncio.run(bot.settlement_watchdog_job(_fake_context()))
    assert len(calls) == 1
    # _send_bet_settlements riceve la lista settlements: il testo formattato
    # (che arriva davvero a Telegram) contiene il verdetto leggibile.
    text = bot.format_bet_verdicts(calls[0])
    assert "ESITO PUNTATE" in text
    assert "Inter" in text and "VINTA" in text


def test_watchdog_silenzioso_senza_pendenze(temp_db, monkeypatch):
    """Niente bet aperte, niente risultati: nessuna notifica, nessun errore."""
    calls = _patch_send(monkeypatch)
    _patch_fetch_scores(monkeypatch, {})
    _patch_ratings(monkeypatch)
    _patch_admin(monkeypatch)

    asyncio.run(bot.settlement_watchdog_job(_fake_context()))
    assert calls == []


def test_watchdog_sobrevive_errore_update(temp_db, monkeypatch):
    """Se il fetch esplode, il job non propaga l'eccezione."""
    def boom(*a, **k):
        raise RuntimeError("API giu'")
    monkeypatch.setattr(odds_api, "fetch_scores", boom)
    _patch_ratings(monkeypatch)
    _patch_admin(monkeypatch)

    asyncio.run(bot.settlement_watchdog_job(_fake_context()))  # no raise


def test_watchdog_saldatura_differita(temp_db, monkeypatch):
    """Scenario completo 01/09: bet piazzata, risultato NON ancora
    disponibile -> resta aperta; al giro successivo (risultato presente,
    anche a 12h di distanza) viene saldata senza intervento manuale."""
    tracker.save_match("mZ", "Serie A", "Inter", "Napoli", _recent_iso())
    tracker.save_analysis("mZ", 1.7, 1.0, 0.5, 0.27, 0.23, 0.55,
                          0.10, "1", 2.10, "Pinnacle", "value")
    tracker.save_bet("mZ", "1X2", "1", "1.100", 101, 2.10, 10.0)

    calls = _patch_send(monkeypatch)
    _patch_ratings(monkeypatch)
    _patch_admin(monkeypatch)

    # Giro 1: l'API non ha ancora il risultato (payload con completed=False)
    in_corso = [dict(MATCH, completed=False, scores=[])]
    _patch_fetch_scores(monkeypatch, {"soccer_italy_serie_a": in_corso})
    asyncio.run(bot.settlement_watchdog_job(_fake_context()))
    rows = tracker.get_bets(limit=10)
    assert rows[0]["esito_finale"] is None
    assert calls == []

    # Giro 2 (2h dopo): risultato arrivato -> auto-settlement + verdetto
    _patch_fetch_scores(monkeypatch,
                        {"soccer_italy_serie_a": [dict(MATCH, id="mZ",
                                                       completed=True)]})
    asyncio.run(bot.settlement_watchdog_job(_fake_context()))
    rows = tracker.get_bets(limit=10)
    assert rows[0]["esito_finale"] == "won"
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Refertazione MIRATA: fetch_scores SOLO per leghe con scommesse attive
# ---------------------------------------------------------------------------

def _recording_fetch(monkeypatch, payload_per_sport):
    """fetch_scores finto che registra gli sport interrogati."""
    requested = []

    def fake_fetch(sport=None, days_from=2):
        requested.append(sport)
        return payload_per_sport.get(sport, [])

    monkeypatch.setattr(odds_api, "fetch_scores", fake_fetch)
    monkeypatch.setitem(sys.modules, "odds_api", odds_api)
    return requested


def test_watchdog_fetch_solo_leghe_con_righe_aperte(temp_db, monkeypatch):
    """Leghe con SOLO un segnale value (nessuna scommessa aperta) NON
    vengono interrogate: zero righe attive = zero chiamate fetch_scores."""
    # Serie A: bet APERTA su partita appena iniziata -> va refertata
    tracker.save_match("mSerieA", "Serie A", "Inter", "Napoli", _recent_iso())
    tracker.save_bet("mSerieA", "1X2", "1", "1.100", 101, 2.10, 10.0)
    # J1 League: segnale value ma NESSUNA prediction/bet -> NON va refertata
    tracker.save_match("mJ1", "J1 League", "FC Machida Zelvia",
                       "Kawasaki Frontale", _recent_iso())
    tracker.save_analysis("mJ1", 1.2, 1.6, 0.28, 0.26, 0.46, 0.52,
                          0.14, "2", 4.0, "Pinnacle", "value")

    _patch_ratings(monkeypatch)
    _patch_admin(monkeypatch)
    requested = _recording_fetch(
        monkeypatch, {"soccer_italy_serie_a": [dict(MATCH, id="mSerieA")]})

    asyncio.run(bot.settlement_watchdog_job(_fake_context()))
    # Solo la lega con la scommessa attiva è stata interrogata
    assert requested == ["soccer_italy_serie_a"]
    rows = tracker.get_bets(limit=10)
    assert rows[0]["esito_finale"] == "won"   # la bet aperta è stata saldata


def test_watchdog_niente_fetch_per_partite_non_iniziate(temp_db, monkeypatch):
    """Bet aperta su partita FUTURA (non ancora iniziata): non c'è alcun
    risultato da scaricare -> zero chiamate API per quella lega."""
    future = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    tracker.save_match("mFut", "La Liga", "Osasuna", "Getafe", future)
    tracker.save_bet("mFut", "1X2", "1", None, None, 2.1, 10.0)

    _patch_ratings(monkeypatch)
    _patch_admin(monkeypatch)
    requested = _recording_fetch(monkeypatch, {})

    asyncio.run(bot.settlement_watchdog_job(_fake_context()))
    assert requested == []
    # La bet resta aperta (il match non è ancora iniziato)
    rows = tracker.get_bets(limit=10)
    assert rows[0]["esito_finale"] is None


def test_tracker_get_leagues_with_open_rows_finestra(temp_db):
    """Helper: solo leghe con righe aperte/chiuse-da-poco su partite
    già iniziate (mai partite future né leghe senza scommesse)."""
    # aperta su partita iniziata -> inclusa
    tracker.save_match("m1", "Serie A", "Inter", "Napoli", _recent_iso())
    tracker.save_bet("m1", "1X2", "1", None, None, 2.1, 10.0)
    # futura -> esclusa
    future = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    tracker.save_match("m2", "La Liga", "Osasuna", "Getafe", future)
    tracker.save_bet("m2", "1X2", "1", None, None, 2.1, 10.0)
    # nessuna scommessa -> esclusa
    tracker.save_match("m3", "J1 League", "A", "B", _recent_iso())

    leagues = tracker.get_leagues_with_open_rows()
    assert leagues == ["Serie A"]


class TestRisparmioCreditiSettlement:
    """13/09: la finestra di refertazione deve combaciare con quella della API
    /scores, altrimenti si pagano chiamate che non possono saldare nulla."""

    def test_finestra_allineata_alla_api_scores(self):
        import odds_api
        assert (tracker._settlement_window_days()
                == int(odds_api.SCORES_DAYS_FROM))

    def _settled_league(self, league, sport, tmp_path, age_hours):
        """Lega con una riga CHIUSA da poco (entra solo per la verifica)."""
        import json
        import time
        recent = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        mid = "h-" + league.replace(" ", "")
        tracker.save_match(mid, league, "A", "B", recent)
        tracker.save_bet(mid, "1X2", "1", None, None, 2.1, 10.0)
        conn = tracker._get_conn()
        conn.execute("UPDATE bets SET esito_finale='won', profit=1.1, "
                     "settled_at=? WHERE match_id=?",
                     (datetime.now(timezone.utc).isoformat(), mid))
        conn.commit()
        conn.close()
        if age_hours is not None:
            (tmp_path / f"toa_scores_{sport}.json").write_text(json.dumps(
                {"ts": time.time() - age_hours * 3600, "payload": []}))

    def test_verifica_periodica_leghe_senza_righe_aperte(self, temp_db,
                                                        monkeypatch, tmp_path):
        """Le leghe che entrano SOLO per la verifica di righe chiuse non si
        ri-interrogano a ogni scadenza della cache punteggi (24h): con
        l'intervallo di verifica (default 36h) una cache FRESCA le esclude dal
        giro. Al 13/09 erano 14 leghe su 28 — meta' del costo di settlement."""
        monkeypatch.setattr(tracker, "DATA_DIR", tmp_path)
        monkeypatch.delenv("SETTLEMENT_HEAL_INTERVAL_HOURS", raising=False)
        sport = odds_api.SPORTS_MAP["Serie A"]
        self._settled_league("Serie A", sport, tmp_path, age_hours=2)
        assert tracker.get_leagues_with_open_rows() == []      # cache fresca
        self._settled_league("Serie A", sport, tmp_path, age_hours=40)
        assert tracker.get_leagues_with_open_rows() == ["Serie A"]
        # intervallo 0 = comportamento pre-13/09 (verifica a ogni scadenza)
        assert tracker.get_leagues_with_open_rows(
            heal_interval_hours=0) == ["Serie A"]

    def test_lega_con_righe_aperte_sempre_interrogata(self, temp_db,
                                                     monkeypatch, tmp_path):
        """Controprova: una lega con righe APERTE si interroga sempre, anche
        con cache punteggi fresca (il risultato serve per saldare)."""
        import json
        import time
        monkeypatch.setattr(tracker, "DATA_DIR", tmp_path)
        recent = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        tracker.save_match("op-1", "Serie A", "Inter", "Napoli", recent)
        tracker.save_bet("op-1", "1X2", "1", None, None, 2.1, 10.0)
        sport = odds_api.SPORTS_MAP["Serie A"]
        (tmp_path / f"toa_scores_{sport}.json").write_text(json.dumps(
            {"ts": time.time(), "payload": []}))
        assert tracker.get_leagues_with_open_rows() == ["Serie A"]

    def test_lega_fuori_finestra_non_interrogata(self, temp_db, monkeypatch):
        """Una lega con sole righe aperte su partite FUORI dalla finestra
        /scores non viene piu' interrogata: era la voce di spreco principale
        (prima la finestra del pianificatore era 5 giorni vs 3 della API)."""
        # 4 giorni fa: dentro la vecchia finestra (5gg), fuori da quella
        # della API /scores (3gg) e da `daysBack` massimo.
        old = (datetime.now(timezone.utc)
               - timedelta(days=4, hours=1)).isoformat()
        tracker.save_match("old1", "Serie A", "Inter", "Napoli", old)
        tracker.save_bet("old1", "1X2", "1", None, None, 2.1, 10.0)
        # controprova: con la vecchia finestra a 5 giorni veniva interrogata
        assert tracker.get_leagues_with_open_rows() == []
        assert tracker.get_leagues_with_open_rows(days_back=5) == ["Serie A"]


class TestCoperturaSettlementSoloPuntate:
    """15/09: il referto segue il DENARO (ledger `bets`), non la telemetria.

    Misurato sul container: 27 chiamate /scores in 24h (~51-58 crediti/giorno,
    ~2 a chiamata) contro 18/giorno sostenibili fino al reset del 01/10, e la
    voce dominante erano le leghe con le SOLE previsioni aperte, riscaricate a
    ogni giro del watchdog (ogni 4h). Le previsioni delle leghe senza puntate
    restano aperte fino alla scadenza automatica (push): si perde telemetria,
    mai il referto di una puntata.
    """

    def _recent(self):
        return (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()

    def test_lega_senza_puntate_non_interrogata(self, temp_db, monkeypatch):
        monkeypatch.delenv("SETTLEMENT_BETS_ONLY", raising=False)
        tracker.save_match("p1", "Serie A", "Inter", "Napoli", self._recent())
        tracker.save_prediction("p1", "1X2", "1", 1.7, 0.60, 0.04)
        tracker.save_match("b1", "Ligue 1", "Lione", "Nizza", self._recent())
        tracker.save_bet("b1", "1X2", "1", None, None, 1.7, 5.0)
        assert tracker.get_leagues_with_open_rows() == ["Ligue 1"]
        # controprova: con la copertura estesa entra anche la lega di sola
        # telemetria (comportamento pre-15/09)
        assert tracker.get_leagues_with_open_rows(bets_only=False) == [
            "Ligue 1", "Serie A"]

    def test_env_riattiva_la_copertura_estesa(self, temp_db, monkeypatch):
        monkeypatch.setenv("SETTLEMENT_BETS_ONLY", "0")
        tracker.save_match("p1", "Serie A", "Inter", "Napoli", self._recent())
        tracker.save_prediction("p1", "1X2", "1", 1.7, 0.60, 0.04)
        assert tracker.get_leagues_with_open_rows() == ["Serie A"]

    def test_verifica_periodica_saltata_sotto_soglia_crediti(self, temp_db,
                                                             monkeypatch,
                                                             tmp_path):
        """Crediti sotto `CREDIT_LOW`: la verifica periodica (costo puro, non
        salda nulla) si salta; la lega con una PUNTATA aperta resta nel piano."""
        import json
        import time
        monkeypatch.setattr(tracker, "DATA_DIR", tmp_path)
        monkeypatch.delenv("SETTLEMENT_BETS_ONLY", raising=False)
        monkeypatch.delenv("SETTLEMENT_HEAL_INTERVAL_HOURS", raising=False)
        # lega con bet CHIUSA da poco (solo verifica), cache punteggi vecchia
        tracker.save_match("h1", "Serie A", "Inter", "Napoli", self._recent())
        tracker.save_bet("h1", "1X2", "1", None, None, 2.1, 5.0)
        conn = tracker._get_conn()
        conn.execute("UPDATE bets SET esito_finale='won', profit=1.1, "
                     "settled_at=? WHERE match_id='h1'",
                     (datetime.now(timezone.utc).isoformat(),))
        conn.commit()
        conn.close()
        sport = odds_api.SPORTS_MAP["Serie A"]
        (tmp_path / f"toa_scores_{sport}.json").write_text(json.dumps(
            {"ts": time.time() - 40 * 3600, "payload": []}))
        # crediti abbondanti: la verifica periodica entra
        monkeypatch.setattr(odds_api, "get_remaining", lambda: 300)
        assert tracker.get_leagues_with_open_rows() == ["Serie A"]
        # crediti sotto la soglia: il piano si svuota (nessuna riga da saldare)
        monkeypatch.setattr(odds_api, "get_remaining", lambda: 40)
        assert tracker.get_leagues_with_open_rows() == []
        # ...ma una PUNTATA aperta si referta comunque
        tracker.save_match("o1", "Ligue 1", "Lione", "Nizza", self._recent())
        tracker.save_bet("o1", "1X2", "1", None, None, 1.7, 5.0)
        assert tracker.get_leagues_with_open_rows() == ["Ligue 1"]

    def test_crediti_illeggibili_non_cambiano_il_piano(self, temp_db,
                                                       monkeypatch, tmp_path):
        """`get_remaining()` che solleva (o None) NON deve cambiare il piano:
        nessuna verifica silenziosamente saltata per un errore di lettura."""
        monkeypatch.setattr(tracker, "DATA_DIR", tmp_path)
        monkeypatch.delenv("SETTLEMENT_BETS_ONLY", raising=False)
        tracker.save_match("h1", "Serie A", "Inter", "Napoli", self._recent())
        tracker.save_bet("h1", "1X2", "1", None, None, 2.1, 5.0)
        monkeypatch.setattr(odds_api, "get_remaining", lambda: None)
        assert tracker.get_leagues_with_open_rows() == ["Serie A"]

        def _boom():
            raise RuntimeError("cache illeggibile")

        monkeypatch.setattr(odds_api, "get_remaining", _boom)
        assert tracker.get_leagues_with_open_rows() == ["Serie A"]

    def test_residuo_dichiara_la_politica(self, temp_db, monkeypatch):
        """Il residuo espone la politica attiva: senza dichiararla un residuo
        piu' basso sembrerebbe un referto migliore invece di una scelta."""
        monkeypatch.delenv("SETTLEMENT_BETS_ONLY", raising=False)
        res = tracker.settlement_residue()
        assert res["bets_only"] is True
        monkeypatch.setenv("SETTLEMENT_BETS_ONLY", "0")
        assert tracker.settlement_residue()["bets_only"] is False

    def test_log_dichiara_la_politica(self, monkeypatch):
        """La politica entra nella riga di log del giro (e del residuo)."""
        monkeypatch.delenv("SETTLEMENT_BETS_ONLY", raising=False)
        monkeypatch.setattr(odds_api, "get_remaining", lambda: 300)
        assert tracker.settlement_coverage_policy() == "solo-puntate"
        monkeypatch.setattr(odds_api, "get_remaining", lambda: 10)
        assert "verifica-periodica-saltata" in tracker.settlement_coverage_policy()
        monkeypatch.setenv("SETTLEMENT_BETS_ONLY", "0")
        assert tracker.settlement_coverage_policy().startswith("tutte-le-righe")
        # il log del watchdog la stampa davvero
        src = Path("bot.py").read_text(encoding="utf-8")
        assert "settlement_coverage_policy()" in src

    def test_il_watchdog_usa_il_pianificatore(self):
        """Tripwire: `_update_results` deve continuare a chiedere il piano a
        `get_leagues_with_open_rows` (la politica vive in un posto solo)."""
        src = Path("bot.py").read_text(encoding="utf-8")
        body = src.split("def _update_results")[1].split("def _admin_chat_ids")[0]
        assert "get_leagues_with_open_rows(" in body


class TestResiduoSettlement:
    """Diagnosi del residuo: perche' ogni riga e' ancora aperta."""

    def test_classifica_i_motivi(self, temp_db, monkeypatch, tmp_path):
        monkeypatch.setenv("SETTLEMENT_WINDOW_DAYS", "3")
        monkeypatch.setenv("SX_STALE_DAYS", "2")
        # Classificazione dei motivi con la copertura ESTESA (tutte le righe
        # aperte): la copertura SOLO-PUNTATE del 15/09 e' coperta da
        # TestCoperturaSettlementSoloPuntate.
        monkeypatch.setenv("SETTLEMENT_BETS_ONLY", "0")
        monkeypatch.setattr(tracker, "DATA_DIR", tmp_path)
        recent = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        # orfana senza riga in `matches` (e oltre la soglia stale)
        tracker.save_prediction("orf-1", "1X2", "1", 1.7, 0.60, 0.04)
        conn = tracker._get_conn()
        conn.execute("UPDATE predictions SET created_at=datetime('now', "
                     "'-6 days') WHERE match_id='orf-1'")
        conn.commit(); conn.close()
        # lega non mappata
        tracker.save_match("m-unk", "Lega Inventata", "A", "B", recent)
        tracker.save_prediction("m-unk", "1X2", "1", 1.7, 0.60, 0.04)
        # fuori finestra
        tracker.save_match("m-old", "Serie A", "Inter", "Napoli", old)
        tracker.save_prediction("m-old", "1X2", "1", 1.7, 0.60, 0.04)
        # futura
        tracker.save_match("m-fut", "Serie A", "Roma", "Lazio", future)
        tracker.save_prediction("m-fut", "1X2", "1", 1.7, 0.60, 0.04)
        # refertabile
        tracker.save_match("m-ok", "Serie A", "Milan", "Genoa", recent)
        tracker.save_prediction("m-ok", "1X2", "1", 1.7, 0.60, 0.04)

        res = tracker.settlement_residue()
        assert res["open"] == {"bets": 0, "predictions": 5}
        assert res["reasons"] == {"no_match_row": 1, "league_unmapped": 1,
                                  "out_of_window": 1, "not_started": 1,
                                  "awaiting_result": 1}
        # Il pianificatore elenca le leghe dal ledger (anche quelle non
        # mappate: le salta il chiamante, costo 0) — vedi estimated_credits.
        assert res["leagues_to_query"] == ["Lega Inventata", "Serie A"]
        assert res["leagues_to_query_mapped"] == ["Serie A"]
        assert res["cost_open_driven"] == ["Serie A"]
        assert res["cost_heal_only"] == []
        # l'orfana e' oltre la soglia: DEVE essere contata (se >0 la scadenza
        # automatica non sta girando)
        assert res["overdue_orphans"] == 1
        # senza cache scores la sola lega mappata costa 1 credito
        assert res["estimated_credits"] == 1
        # con la cache FRESCA il costo atteso scende a 0 (nessuna chiamata)
        import json
        import time
        sport = odds_api.SPORTS_MAP["Serie A"]
        (tmp_path / f"toa_scores_{sport}.json").write_text(json.dumps(
            {"ts": time.time(), "payload": []}))
        assert tracker.settlement_residue()["estimated_credits"] == 0

    def test_residuo_vuoto_senza_righe_aperte(self, temp_db, monkeypatch,
                                               tmp_path):
        monkeypatch.setattr(tracker, "DATA_DIR", tmp_path)
        monkeypatch.setenv("SETTLEMENT_BETS_ONLY", "0")   # copertura estesa
        tracker.save_match("m-x", "Serie A", "Inter", "Napoli",
                           (datetime.now(timezone.utc)
                            - timedelta(days=1)).isoformat())
        tracker.save_prediction("m-x", "1X2", "1", 1.7, 0.60, 0.04)
        tracker.save_result("m-x", "Serie A", "Inter", "Napoli", 2, 0, "")
        tracker.settle_predictions()
        res = tracker.settlement_residue()
        assert res["open"] == {"bets": 0, "predictions": 0}
        assert res["reasons"] == {"no_match_row": 0, "league_unmapped": 0,
                                  "out_of_window": 0, "not_started": 0,
                                  "awaiting_result": 0}
        assert res["overdue_orphans"] == 0
        # Nessuna riga aperta: la lega entra solo per la verifica periodica
        # delle righe appena chiuse (non per saldare).
        assert res["cost_open_driven"] == []
        assert res["cost_heal_only"] == ["Serie A"]


class TestCodaFantasma:
    """`past_kickoff_open` / `past_kickoff_unplanned` (08/10/2026).

    Rendono VISIBILE la "coda fantasma": righe di partite GIA' INIZIATE
    ancora aperte e quante di quelle il prossimo referto NON interroghera'.
    Caso reale che li ha motivati: **Botafogo RJ-CR Vasco da Gama**, 6
    previsioni aperte 22h dopo il kickoff in una lega (Brasileirao) senza
    alcuna puntata, quindi esclusa dal referto PAGATO da
    `SETTLEMENT_BETS_ONLY=1`.

    Senza questi due numeri una riga `awaiting_result` che nessun giro
    saldera' mai e' indistinguibile da una che sta per essere saldata.
    """

    @staticmethod
    def _seed_ghost(hours_ago: float = 22.0, mid: str = "m-ghost"):
        """Partita GIA' INIZIATA con una previsione aperta (nessuna bet)."""
        tracker.save_match(mid, "Brasileirao", "Botafogo RJ",
                           "CR Vasco da Gama",
                           (datetime.now(timezone.utc)
                            - timedelta(hours=hours_ago)).isoformat())
        tracker.save_prediction(mid, "OU", "Over 3.5", 2.0, 0.5, 0.03)

    def test_riga_gia_iniziata_conta_nella_coda(self, temp_db, monkeypatch):
        monkeypatch.setenv("SETTLEMENT_BETS_ONLY", "1")
        self._seed_ghost()
        # una partita FUTURA resta fuori dalla coda per costruzione
        tracker.save_match("m-fut", "Serie A", "Inter", "Napoli",
                           (datetime.now(timezone.utc)
                            + timedelta(hours=3)).isoformat())
        tracker.save_prediction("m-fut", "1X2", "1", 1.7, 0.60, 0.04)

        res = tracker.settlement_residue()
        assert res["reasons"]["awaiting_result"] == 1     # m-ghost
        assert res["reasons"]["not_started"] == 1         # m-fut
        assert res["past_kickoff_open"] == 1
        # la lega della partita iniziata NON e' nel piano (nessuna puntata:
        # e' la politica `SETTLEMENT_BETS_ONLY`, non un errore)
        assert res["past_kickoff_unplanned"] == 1

    def test_una_puntata_riporta_la_lega_nel_piano(self, temp_db, monkeypatch):
        """Con una PUNTATA aperta sulla stessa lega il referto la interroga:
        `past_kickoff_unplanned` torna a 0 (il piano segue il denaro)."""
        monkeypatch.setenv("SETTLEMENT_BETS_ONLY", "1")
        self._seed_ghost()
        tracker.save_bet("m-ghost", "1X2", "1", "0xh", 1, 2.0, 1.0,
                         mode="live")
        res = tracker.settlement_residue()
        assert res["past_kickoff_unplanned"] == 0
        assert "Brasileirao" in res["leagues_to_query"]

    def test_copertura_estesa_nessuna_coda_non_pianificata(
            self, temp_db, monkeypatch):
        """Controprova: senza `SETTLEMENT_BETS_ONLY` la lega entra nel piano
        anche con le sole previsioni — la coda non pianificata e' tutta e
        solo della politica solo-puntate."""
        monkeypatch.setenv("SETTLEMENT_BETS_ONLY", "0")
        self._seed_ghost()
        res = tracker.settlement_residue()
        assert res["past_kickoff_open"] == 1
        assert res["past_kickoff_unplanned"] == 0
        assert res["bets_only"] is False

    def test_nessuna_riga_iniziata_coda_a_zero(self, temp_db, monkeypatch):
        monkeypatch.setenv("SETTLEMENT_BETS_ONLY", "1")
        tracker.save_match("m-fut", "Serie A", "Inter", "Napoli",
                           (datetime.now(timezone.utc)
                            + timedelta(hours=3)).isoformat())
        tracker.save_prediction("m-fut", "1X2", "1", 1.7, 0.60, 0.04)
        res = tracker.settlement_residue()
        assert res["past_kickoff_open"] == 0
        assert res["past_kickoff_unplanned"] == 0
