"""Test della CLOSING LINE di Pinnacle a T-0 (direttiva 04/10/2026, punto 5).

Cosa difendono (tutti OFFLINE: DB SQLite temporaneo, cache sharp FINTA,
zero rete, zero crediti, zero ordini):

1. **Il campione nasce a T-0**: la routine trova le righe APERTE (previsioni
   giocabili + puntate live) col kickoff nella finestra `T-PRE..T+POST` e
   scrive la quota finale dello sharp in `clv_history.closing_odds`.
2. **Nessuna formula copiata**: il beat % delega a `market_calib.clv_raw`;
   il modulo non contiene ne` un de-vig ne` una formula di Kelly ne` un
   percorso d'ordine (tripwire sul sorgente).
3. **Fail-closed**: senza cache sharp, su un mercato non testa-a-testa (OU/AH)
   o con esito non canonicale la riga viene SALTATA con motivo
   machine-readable — mai un numero inventato, mai una quota di un altro
   mercato.
4. **Non sovrascrive**: un `closing_odds` gia` catturato resta (la routine gira
   ogni 5 minuti, il valore deve essere quello del T-0, non l'ultimo giro).
5. **Fail-safe totale**: un DB assente/corrotto o una riga ostile non
   sollevano mai al chiamante (la routine gira dentro il job del bot).
"""
from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import closing_line as cl
import tracker

SOURCE = Path(cl.__file__).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture()
def temp_db(monkeypatch, tmp_path):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(tracker, "DB_PATH", db_path)
    yield db_path


@pytest.fixture()
def sharp(monkeypatch):
    """Cache sharp FINTA: nessuna rete, controllo totale dei prezzi."""
    calls: dict = {"args": [], "ret": None}

    def fake(home, away, sport_key=None, *, cache_dir=None, now=None,
             outcomes=("1", "X", "2")):
        calls["args"].append({"home": home, "away": away,
                              "outcomes": tuple(outcomes), "now": now})
        ret = calls["ret"]
        return None if ret is None else {"odds": dict(ret),
                                         "ts": 1.0, "sport_key": "test"}

    import pinnacle_oracle
    monkeypatch.setattr(pinnacle_oracle, "pinnacle_odds_from_cache", fake)
    return calls


def _seed_prediction(match_id="sx-1", home="Osasuna", away="Getafe",
                     esito="1", quota=1.65, mercato="1X2", minutes=5.0,
                     status="value", league="Premier League"):
    tracker.save_match(match_id, league, home, away,
                       (datetime.now(timezone.utc) + timedelta(minutes=minutes))
                       .isoformat())
    tracker.save_prediction(match_id, mercato, esito, quota, 0.62, 0.05,
                            market_prob=0.58, market_edge=0.04, status=status,
                            league=league)


def _seed_live_bet(match_id="sx-2", home="Roma", away="Lazio", esito="1",
                   price=1.70, minutes=3.0):
    tracker.save_match(match_id, "Serie A", home, away,
                       (datetime.now(timezone.utc) + timedelta(minutes=minutes))
                       .isoformat())
    tracker.save_bet(match_id=match_id, mercato="1X2", esito=esito,
                     market_id="0xm", selection_id=1, price=price, stake=1.5,
                     mode="live", status="FULLY_FILLED", bet_id="0xb")


# ---------------------------------------------------------------------------
# 1. BEAT % — formula delegata
# ---------------------------------------------------------------------------

class TestBeatPct:
    def test_delega_a_clv_raw(self):
        """Unica definizione della formula: `market_calib.clv_raw`."""
        from market_calib import clv_raw
        assert cl.beat_pct(1.80, 1.65) == clv_raw(1.80, 1.65)
        assert cl.beat_pct(1.80, 1.65) == pytest.approx(0.09090909, abs=1e-6)

    def test_quota_inferiore_al_mercato_e_negativa(self):
        assert cl.beat_pct(1.50, 1.65) < 0

    def test_input_non_valido_non_inventa_numeri(self):
        assert cl.beat_pct(0.9, 1.65) is None
        assert cl.beat_pct(1.80, 0.9) is None
        assert cl.beat_pct("x", 1.65) is None

    def test_nessuna_formula_copiata_nel_sorgente(self):
        for banned in ("(order / closing)", "order_odds / closing_odds",
                       "def devig", "def kelly", "shin"):
            assert banned not in SOURCE, f"formula copiata: {banned}"


# ---------------------------------------------------------------------------
# 2. FINESTRA E CANDIDATI
# ---------------------------------------------------------------------------

class TestCandidati:
    def test_riga_in_finestra_e_candidata(self, temp_db, sharp):
        _seed_prediction(minutes=5.0)
        got = cl.candidates(now=datetime.now(timezone.utc))
        assert [r["match_id"] for r in got] == ["sx-1"]
        assert got[0]["esito"] == "1" and got[0]["price"] == 1.65

    def test_troppo_presto_non_e_candidata(self, temp_db, sharp):
        """Kickoff a 2 ore: la closing line non esiste ancora."""
        _seed_prediction(minutes=120.0)
        assert cl.candidates(now=datetime.now(timezone.utc)) == []

    def test_troppo_tardi_non_e_candidata(self, temp_db, sharp):
        """Kickoff passato da 30': la finestra di tolleranza e' chiusa."""
        _seed_prediction(minutes=-30.0)
        assert cl.candidates(now=datetime.now(timezone.utc)) == []

    def test_puntata_live_chiusa_esclusa(self, temp_db, sharp):
        _seed_live_bet(minutes=2.0)
        tracker.settle_bets()
        # Nessun risultato -> la bet resta aperta: la si chiude a mano per
        # verificare che una riga SALDATA non venga piu' catturata.
        conn = tracker._get_conn()
        conn.execute("UPDATE bets SET esito_finale='1' WHERE match_id='sx-2'")
        conn.commit(); conn.close()
        assert cl.candidates(now=datetime.now(timezone.utc)) == []

    def test_previsione_non_giocabile_esclusa(self, temp_db, sharp):
        """Solo `value`/`strong_value`/`moderate`: le `rejected` non si toccano."""
        _seed_prediction(minutes=5.0, status="rejected")
        assert cl.candidates(now=datetime.now(timezone.utc)) == []

    def test_dedup_per_match_ed_esito(self, temp_db, sharp):
        """Previsione + puntata sulla stessa coppia: UNA sola cattura."""
        _seed_prediction(minutes=5.0)
        _seed_live_bet(match_id="sx-1", home="Osasuna", away="Getafe",
                       minutes=5.0)
        got = cl.candidates(now=datetime.now(timezone.utc))
        assert len(got) == 1

    def test_ordinati_per_kickoff(self, temp_db, sharp):
        _seed_prediction(match_id="sx-1", minutes=8.0)
        _seed_prediction(match_id="sx-2", home="Roma", away="Lazio",
                         minutes=1.0)
        got = cl.candidates(now=datetime.now(timezone.utc))
        assert [r["match_id"] for r in got] == ["sx-2", "sx-1"]

    def test_kickoff_iso_con_z_parsato(self, temp_db, sharp):
        """Il ledger salva ISO con 'T' e a volte 'Z': si confronta in PYTHON."""
        _seed_prediction(minutes=4.0)
        conn = tracker._get_conn()
        conn.execute(
            "UPDATE matches SET commence_time=? WHERE id='sx-1'",
            ((datetime.now(timezone.utc) + timedelta(minutes=4))
             .isoformat().replace("+00:00", "Z"),))
        conn.commit(); conn.close()
        assert len(cl.candidates(now=datetime.now(timezone.utc))) == 1

    def test_data_illeggibile_non_rompe_la_scansione(self, temp_db, sharp):
        _seed_prediction(match_id="sx-1", minutes=5.0)
        _seed_prediction(match_id="sx-2", home="Roma", away="Lazio",
                         minutes=6.0)
        conn = tracker._get_conn()
        conn.execute("UPDATE matches SET commence_time='boh' WHERE id='sx-1'")
        conn.commit(); conn.close()
        got = cl.candidates(now=datetime.now(timezone.utc))
        assert [r["match_id"] for r in got] == ["sx-2"]   # solo la valida

    def test_max_rows_limita_il_batch(self, temp_db, sharp, monkeypatch):
        monkeypatch.setenv("CLOSING_LINE_MAX_ROWS", "2")
        for i in range(5):
            _seed_prediction(match_id=f"sx-{i}", home=f"H{i}", away=f"A{i}",
                             minutes=4.0 + i)
        assert len(cl.candidates(now=datetime.now(timezone.utc))) == 2


# ---------------------------------------------------------------------------
# 3. QUOTA SHARP DI CHIUSURA
# ---------------------------------------------------------------------------

class TestSharpClosing:
    def test_quota_della_cache(self, temp_db, sharp):
        sharp["ret"] = {"1": 1.72, "X": 3.60, "2": 4.80}
        got = cl.sharp_closing("Osasuna", "Getafe", "1X2", "1")
        assert got == 1.72
        assert sharp["args"][0]["outcomes"] == ("1", "X", "2")

    def test_mercato_a_linea_saltato(self, temp_db, sharp):
        """OU/AH non hanno il prezzo nell'h2h: mai la quota di un altro mercato."""
        sharp["ret"] = {"Over 2.5": 1.90}
        assert cl.sharp_closing("Osasuna", "Getafe", "OU", "Over 2.5") is None
        assert cl.sharp_closing("Osasuna", "Getafe", "AH", "Home -0.75") is None
        assert sharp["args"] == []            # la cache non viene nemmeno letta

    def test_tennis_usa_due_esiti(self, temp_db, sharp):
        sharp["ret"] = {"1": 1.66, "2": 2.34}
        assert cl.sharp_closing("Sinner", "Alcaraz", "TENNIS", "2") == 2.34
        assert sharp["args"][0]["outcomes"] == ("1", "2")

    def test_esito_nome_squadra_canonicalizzato(self, temp_db, sharp):
        """`predictions` scrive a volte il NOME della squadra, non 1/X/2."""
        sharp["ret"] = {"1": 1.70, "X": 3.5, "2": 4.5}
        assert cl.sharp_closing("Osasuna", "Getafe", "1X2", "Osasuna") == 1.70

    def test_esito_non_canonicale_saltato(self, temp_db, sharp):
        sharp["ret"] = {"1": 1.70, "X": 3.5, "2": 4.5}
        assert cl.sharp_closing("Osasuna", "Getafe", "1X2", "Inter") is None

    def test_senza_cache_e_none(self, temp_db, sharp):
        sharp["ret"] = None
        assert cl.sharp_closing("Osasuna", "Getafe", "1X2", "1") is None

    def test_quota_degenere_scartata(self, temp_db, sharp):
        sharp["ret"] = {"1": 1.0, "X": 3.5, "2": 4.5}
        assert cl.sharp_closing("Osasuna", "Getafe", "1X2", "1") is None


# ---------------------------------------------------------------------------
# 4. CATTURA: scrittura sul ledger
# ---------------------------------------------------------------------------

class TestCapture:
    def test_scrive_closing_odds(self, temp_db, sharp):
        _seed_prediction(minutes=5.0)
        sharp["ret"] = {"1": 1.72, "X": 3.60, "2": 4.80}
        out = cl.capture_closing_lines(now=datetime.now(timezone.utc))
        assert out["checked"] == 1 and out["captured"] == 1
        assert out["rows"][0]["closing_odds"] == 1.72
        assert out["rows"][0]["beat_pct"] == pytest.approx(
            (1.65 / 1.72) - 1.0, abs=1e-6)
        row = tracker._get_conn().execute(
            "SELECT signal_quota, closing_odds FROM clv_history "
            "WHERE match_id='sx-1' AND esito='1'").fetchone()
        assert row[0] == 1.65 and row[1] == 1.72

    def test_non_sovrascrive_un_campione_gia_catturato(self, temp_db, sharp):
        """La routine gira ogni 5': il valore resta quello del primo T-0."""
        _seed_prediction(minutes=5.0)
        sharp["ret"] = {"1": 1.72, "X": 3.60, "2": 4.80}
        first = cl.capture_closing_lines(now=datetime.now(timezone.utc))
        assert first["captured"] == 1
        sharp["ret"] = {"1": 1.55, "X": 3.60, "2": 4.80}
        second = cl.capture_closing_lines(now=datetime.now(timezone.utc))
        assert second["captured"] == 0
        assert second["skipped"] == {"already_captured": 1}
        row = tracker._get_conn().execute(
            "SELECT closing_odds FROM clv_history "
            "WHERE match_id='sx-1' AND esito='1'").fetchone()
        assert row[0] == 1.72

    def test_save_clv_senza_closing_non_cancella_quello_catturato(self, temp_db):
        """`fixture_engine` continua ad aggiornare la chiusura: COALESCE."""
        tracker.save_clv("sx-9", "1", 1.65, signal_started=True,
                         closing_odds=1.70)
        tracker.save_clv("sx-9", "1", 1.80)          # analisi successiva
        row = tracker._get_conn().execute(
            "SELECT signal_quota, closing_quota, closing_odds FROM clv_history "
            "WHERE match_id='sx-9'").fetchone()
        assert row[0] == 1.65 and row[1] == 1.80 and row[2] == 1.70

    def test_puntata_live_semina_il_campione(self, temp_db, sharp):
        _seed_live_bet(minutes=2.0)
        sharp["ret"] = {"1": 1.60, "X": 3.5, "2": 5.0}
        out = cl.capture_closing_lines(now=datetime.now(timezone.utc))
        assert out["captured"] == 1
        row = tracker._get_conn().execute(
            "SELECT signal_quota, closing_quota, closing_odds FROM clv_history "
            "WHERE match_id='sx-2'").fetchone()
        assert row == (1.70, 1.70, 1.60)

    def test_motivo_machine_readable_per_mercato_a_linea(self, temp_db, sharp):
        _seed_prediction(mercato="OU", esito="Over 2.5", minutes=5.0)
        out = cl.capture_closing_lines(now=datetime.now(timezone.utc))
        assert out["captured"] == 0
        assert out["skipped"] == {"unsupported_market": 1}

    def test_motivo_machine_readable_senza_cache(self, temp_db, sharp):
        _seed_prediction(minutes=5.0)
        out = cl.capture_closing_lines(now=datetime.now(timezone.utc))
        assert out["captured"] == 0
        assert out["skipped"] == {"no_sharp_cache": 1}

    def test_una_riga_rotta_non_ferma_le_altre(self, temp_db, sharp,
                                               monkeypatch):
        """FAIL-SAFE: una cache che esplode salta UNA riga, non il giro."""
        _seed_prediction(match_id="sx-1", minutes=5.0)
        _seed_prediction(match_id="sx-2", home="Roma", away="Lazio",
                         minutes=6.0)
        orig = cl.sharp_closing

        def boom(home, away, market, esito, *, now=None):
            if home == "Osasuna":
                raise RuntimeError("cache esplosa")
            return orig(home, away, market, esito, now=now)

        monkeypatch.setattr(cl, "sharp_closing", boom)
        sharp["ret"] = {"1": 1.60, "X": 3.5, "2": 5.0}
        out = cl.capture_closing_lines(now=datetime.now(timezone.utc))
        assert out["checked"] == 2 and out["captured"] == 1
        assert out["skipped"] == {"read_error": 1}
        assert [r["match_id"] for r in out["rows"]] == ["sx-2"]

    def test_db_assente_non_solleva(self, monkeypatch, tmp_path):
        monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "nope" / "x.db")
        monkeypatch.setattr(tracker, "_get_conn",
                            lambda: (_ for _ in ()).throw(RuntimeError("no db")))
        out = cl.capture_closing_lines()
        assert out["checked"] == 0 and out["captured"] == 0

    def test_tutto_il_flusso_su_conn_iniettata(self, temp_db, sharp):
        _seed_prediction(minutes=5.0)
        sharp["ret"] = {"1": 1.72, "X": 3.60, "2": 4.80}
        conn = tracker._get_conn()
        out = cl.capture_closing_lines(conn=conn,
                                       now=datetime.now(timezone.utc))
        assert out["captured"] == 1


# ---------------------------------------------------------------------------
# 5. REPORT + CLI
# ---------------------------------------------------------------------------

class TestReport:
    def test_report_senza_campioni(self, temp_db):
        rep = cl.report()
        assert rep["with_closing"] == 0 and rep["avg_beat"] is None
        assert "Nessun campione" in cl.format_report(rep)

    def test_report_con_campioni(self, temp_db, sharp):
        _seed_prediction(minutes=5.0)
        sharp["ret"] = {"1": 1.72, "X": 3.60, "2": 4.80}
        cl.capture_closing_lines(now=datetime.now(timezone.utc))
        rep = cl.report()
        assert rep["with_closing"] == 1 and rep["closed_n"] == 1
        assert rep["avg_beat"] == pytest.approx((1.65 / 1.72) - 1.0, abs=1e-6)
        text = cl.format_report(rep)
        assert "Closing line Pinnacle" in text and "Beat medio" in text

    def test_report_db_rotto_non_solleva(self, monkeypatch, tmp_path):
        monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "nope.db")
        monkeypatch.setattr(tracker, "_get_conn",
                            lambda: (_ for _ in ()).throw(RuntimeError("no")))
        rep = cl.report()
        assert rep["closed_n"] == 0 and rep["with_closing"] == 0


# ---------------------------------------------------------------------------
# 6. TRIPWIRE
# ---------------------------------------------------------------------------

class TestTripwire:
    def test_nessun_percorso_dordine_nel_modulo(self):
        for banned in ("_live_fill", "place_limit_order", "OrderResult",
                       "resolve_market_for", "execution_engine", "save_bet"):
            assert banned not in SOURCE, f"il modulo tocca il denaro: {banned}"

    def test_nessuna_scrittura_su_predictions(self):
        for banned in ("save_prediction(", "settle_", "UPDATE predictions"):
            assert banned not in SOURCE, f"scrittura inattesa: {banned}"

    def test_nessuna_rete_all_import(self):
        code = ("import sys, closing_line; "
                "assert not any(m in sys.modules for m in "
                "('requests', 'httpx', 'aiohttp', 'auto_bet', 'bot'))")
        r = subprocess.run([sys.executable, "-c", code],
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stderr

    def test_formula_del_beat_non_ricopiata(self):
        assert "from market_calib import clv_raw" in SOURCE

    def test_job_registrato_e_spegnibile(self):
        bot_src = (Path(__file__).parent / "bot.py").read_text(encoding="utf-8")
        assert "async def closing_line_job" in bot_src
        assert "closing_line_job, interval=300" in bot_src
        assert "CLOSING_LINE_ENABLED" in bot_src


# ---------------------------------------------------------------------------
# 7. SAVE_CLV: la colonna nuova convive con le vecchie
# ---------------------------------------------------------------------------

class TestSaveClvColonna:
    def test_colonna_presente_dopo_la_migrazione(self, temp_db):
        cols = [r[1] for r in tracker._get_conn().execute(
            "PRAGMA table_info(clv_history)").fetchall()]
        assert "closing_odds" in cols
        assert {"signal_quota", "closing_quota", "pinnacle_quota"} <= set(cols)

    def test_migrazione_idempotente(self, temp_db):
        tracker._get_conn().close()
        tracker._get_conn().close()      # seconda apertura: nessun errore
        cols = [r[1] for r in tracker._get_conn().execute(
            "PRAGMA table_info(clv_history)").fetchall()]
        assert cols.count("closing_odds") == 1

    def test_valori_degeneri_ignorati(self, temp_db):
        tracker.save_clv("sx-9", "1", 1.65, closing_odds=0.0)
        row = tracker._get_conn().execute(
            "SELECT closing_odds FROM clv_history WHERE match_id='sx-9'")
        assert row.fetchone()[0] is None
