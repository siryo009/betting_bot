"""Test del TRACKER AUTOMATICO del CLV (direttiva 13/10/2026).

Cosa difendono (tutti OFFLINE: DB SQLite temporaneo, provider/lookup iniettati,
zero rete, zero crediti, zero ordini):

1. **Il campione e' la QUOTA D'ACQUISTO**: si parte da `bets.price` delle
   puntate `mode='live'` (il prezzo che l'ordine ha davvero ottenuto: il ledger
   registra i riempimenti, non le intenzioni) e si confronta con la closing
   line — non con il prezzo del segnale.
2. **La fonte della chiusura viaggia col campione**: `closing_odds` +
   `closing_source` (`pinnacle_t0` da `closing_line`, `sx` da
   `capture_sx_closing`). Un valore senza fonte non si confronta, e l'ECO della
   quota del segnale (`closing_quota` seminato da `save_clv`) NON e' una
   chiusura: contarla darebbe un CLV finto a zero.
3. **Nessuna formula ricopiata**: CLV e vig-free delegano a `market_calib`;
   la statistica a `significance` (una sola idea di "campione affidabile").
4. **Fail-safe totale**: un DB assente, una riga ostile o un lookup rotto non
   sollevano mai e non inventano un numero — il campione viene ESCLUSO e
   CONTATO per motivo.
5. **Sola lettura nel percorso di analisi**: `--db` apre la connessione in
   `mode=ro` (il test tenta una `UPDATE` e pretende che SQLite la rifiuti);
   l'unica scrittura e' `capture_sx_closing`, che passa da `tracker.save_clv`.
6. **Confini**: nessun ordine, nessuna soglia di strategia, import leggero
   (`import clv_tracker` non carica `auto_bet`/`bot`/`execution_engine`).
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import clv_tracker as ct
import tracker

SOURCE = Path(ct.__file__).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Fixture e helper
# ---------------------------------------------------------------------------

@pytest.fixture()
def temp_db(monkeypatch, tmp_path):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(tracker, "DB_PATH", db_path)
    yield db_path


def _seed_bet(match_id="sx-1", home="Osasuna", away="Getafe", esito="1",
              price=1.85, mercato="1X2", league="Serie A", minutes=5.0,
              mode="live", market_id="0xm", selection_id=1, stake=2.0):
    """Una puntata sul ledger con la sua riga in `matches`."""
    tracker.save_match(match_id, league, home, away,
                       (datetime.now(timezone.utc) + timedelta(minutes=minutes))
                       .isoformat())
    tracker.save_bet(match_id=match_id, mercato=mercato, esito=esito,
                     market_id=market_id, selection_id=selection_id,
                     price=price, stake=stake, mode=mode,
                     status="FULLY_FILLED", bet_id="0xb")


def _set_closing(match_id, esito, quota, value, source="pinnacle_t0"):
    """Chiude il campione CLV come farebbe `closing_line` / `capture_sx_closing`."""
    tracker.save_clv(match_id, esito, quota, closing_odds=value,
                     closing_source=source)


def _age_bet(match_id, esito, days):
    """Invecchia `created_at` della puntata (per il filtro `--days`)."""
    old = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn = tracker._get_conn()
    conn.execute("UPDATE bets SET created_at=? WHERE match_id=? AND esito=?",
                 (old, match_id, esito))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# 1. RACCOLTA — la quota d'acquisto contro la closing line
# ---------------------------------------------------------------------------

class TestRaccolta:

    def test_trade_chiuso_e_misurato(self, temp_db):
        _seed_bet(price=1.90)
        _set_closing("sx-1", "1", 1.90, 1.80)
        bundle = ct.collect()
        assert len(bundle["samples"]) == 1
        s = bundle["samples"][0]
        assert s["price"] == 1.90 and s["closing"] == 1.80
        assert s["closing_source"] == "pinnacle_t0"
        assert s["clv"] == pytest.approx((1.90 / 1.80) - 1.0)
        assert s["origin"] == "trade"
        assert bundle["totals"]["trades"] == 1

    def test_la_quota_dacquisto_e_il_prezzo_del_ledger(self, temp_db):
        """Il campione usa `bets.price`, non la quota del segnale."""
        _seed_bet(price=1.72)
        _set_closing("sx-1", "1", 1.95, 1.80)     # signal_quota 1.95 (altro)
        s = ct.collect()["samples"][0]
        assert s["price"] == 1.72
        assert s["clv"] == pytest.approx((1.72 / 1.80) - 1.0)

    def test_senza_chiusura_e_pending_non_escluso(self, temp_db):
        _seed_bet()
        b = ct.collect()
        assert b["samples"] == []
        assert len(b["pending"]) == 1
        assert b["pending"][0]["reason"] == "no_closing"
        assert b["excluded"] == {}

    def test_prezzo_non_valido_escluso(self, temp_db):
        _seed_bet(match_id="sx-1", esito="1", price=1.0)
        b = ct.collect()
        assert b["excluded"] == {"bad_price": 1}

    def test_puntata_sim_non_entra_nel_campione(self, temp_db):
        """Solo `mode='live'`: la SIM la misura non la fa (non e' denaro)."""
        _seed_bet(match_id="sx-sim", mode="sim")
        b = ct.collect()
        assert b["totals"]["trades"] == 0
        assert b["samples"] == []

    def test_filtro_giorni(self, temp_db):
        _seed_bet(match_id="sx-old", esito="1", price=1.9)
        _set_closing("sx-old", "1", 1.9, 1.8)
        _age_bet("sx-old", "1", 30)
        _seed_bet(match_id="sx-new", esito="1", price=1.9)
        _set_closing("sx-new", "1", 1.9, 1.8)
        assert len(ct.collect(days=7)["samples"]) == 1
        assert len(ct.collect()["samples"]) == 2

    def test_segnali_solo_su_richiesta(self, temp_db):
        tracker.save_match("sx-p", "Serie A", "Roma", "Lazio",
                           (datetime.now(timezone.utc)
                            + timedelta(minutes=30)).isoformat())
        tracker.save_prediction("sx-p", "1X2", "1", 1.85, 0.62, 0.05,
                                market_prob=0.58, market_edge=0.04,
                                status="value", league="Serie A")
        _set_closing("sx-p", "1", 1.85, 1.80)
        assert ct.collect()["samples"] == []
        got = ct.collect(include_signals=True)
        assert len(got["samples"]) == 1
        assert got["samples"][0]["origin"] == "signal"
        assert got["totals"]["signals"] == 1

    def test_stesso_esito_non_contato_due_volte(self, temp_db):
        """Trade + segnale sulla stessa coppia (match, esito) = UN campione."""
        _seed_bet()
        _set_closing("sx-1", "1", 1.85, 1.80)
        tracker.save_prediction("sx-1", "1X2", "1", 1.85, 0.62, 0.05,
                                market_prob=0.58, market_edge=0.04,
                                status="value", league="Serie A")
        got = ct.collect(include_signals=True)
        assert len(got["samples"]) == 1
        assert got["samples"][0]["origin"] == "trade"

    def test_chiusura_lleggibile_non_eccezione(self, temp_db):
        _seed_bet()
        conn = tracker._get_conn()
        conn.execute("UPDATE clv_history SET closing_odds='abc' "
                     "WHERE match_id='sx-1'")
        conn.commit()
        conn.close()
        assert ct.collect()["samples"] == []          # pendente, non escluso

    def test_db_assente_non_solleva_e_dichiara(self, monkeypatch, tmp_path):
        """Un ledger irraggiungibile e' un ERRORE dichiarato, non zero campioni."""
        monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "nope" / "x.db")
        b = ct.collect()
        assert b["samples"] == []
        assert b["error"] and "unable to open" in b["error"]
        rep = ct.report()
        assert rep["error"]
        assert "non calcolabile" in ct.format_report(rep)

    def test_db_senza_tabelle_risponde_vuoto(self, temp_db):
        """Ledger di un deploy precedente (tabelle mancanti): vuoto, non crash."""
        import sqlite3 as _s
        _s.connect(str(temp_db)).close()
        b = ct.collect()
        assert b["samples"] == [] and b["error"] is None


# ---------------------------------------------------------------------------
# 2. FONTI ed ECO — un campione senza fonte non si confronta
# ---------------------------------------------------------------------------

class TestFontiDellaChiusura:

    def test_priorita_a_pinnacle_t0(self, temp_db):
        _seed_bet()
        conn = tracker._get_conn()
        conn.execute("INSERT OR REPLACE INTO clv_history "
                     "(match_id, esito, signal_quota, closing_quota, "
                     " pinnacle_quota, closing_odds, closing_source, updated_at)"
                     " VALUES ('sx-1','1',1.85,1.70,1.75,1.80,"
                     "'pinnacle_t0','now')")
        conn.commit()
        conn.close()
        s = ct.collect()["samples"][0]
        assert s["closing"] == 1.80 and s["closing_source"] == "pinnacle_t0"

    def test_ripiego_su_pinnacle_di_cache(self, temp_db):
        _seed_bet()
        tracker.save_clv("sx-1", "1", 1.85, pinnacle_quota=1.78)
        s = ct.collect()["samples"][0]
        assert s["closing"] == 1.78 and s["closing_source"] == "pinnacle_cache"

    def test_ripiego_su_chiusura_bookmaker(self, temp_db):
        _seed_bet()
        tracker.save_clv("sx-1", "1", 1.85, signal_started=True)
        tracker.save_clv("sx-1", "1", 1.74)          # closing_quota = 1.74
        s = ct.collect()["samples"][0]
        assert s["closing"] == 1.74 and s["closing_source"] == "bookmaker"

    def test_eco_del_segnale_e_una_attesa_non_un_rifiuto(self, temp_db):
        """`closing_quota` == signal_quota: e' il prezzo di partenza, non la
        chiusura (un CLV a zero finto). NON e' pero' un rifiuto: la riga si
        aggiorna a ogni analisi, quindi va in `pending` come un trade senza
        alcuna riga (due situazioni identiche non possono stare in bucket
        diversi)."""
        _seed_bet(price=1.85)
        tracker.save_clv("sx-1", "1", 1.85, signal_started=True)
        b = ct.collect()
        assert b["samples"] == []
        assert b["excluded"] == {}
        assert len(b["pending"]) == 1
        assert b["pending"][0]["reason"] == "echo_signal"

    def test_il_seme_diventa_misurabile_alla_chiusura(self, temp_db):
        """La transizione che giustifica il bucket `pending`."""
        _seed_bet(price=1.85)
        tracker.save_clv("sx-1", "1", 1.85, signal_started=True)
        assert ct.collect()["pending"][0]["reason"] == "echo_signal"
        tracker.save_clv("sx-1", "1", 1.72)      # la chiusura e' arrivata
        samples = ct.collect()["samples"]
        assert len(samples) == 1
        assert samples[0]["closing"] == pytest.approx(1.72)
        assert samples[0]["clv"] == pytest.approx((1.85 / 1.72) - 1.0)

    def test_fonte_sx_viaggia_col_campione(self, temp_db):
        _seed_bet()
        _set_closing("sx-1", "1", 1.85, 1.79, source=ct.SOURCE_SX)
        s = ct.collect()["samples"][0]
        assert s["closing_source"] == "sx"

    def test_esito_non_canonico_escluso(self, temp_db):
        tracker.save_match("sx-1", "Serie A", "Roma", "Lazio",
                           datetime.now(timezone.utc).isoformat())
        tracker.save_bet(match_id="sx-1", mercato="1X2", esito="Chievo",
                         price=1.85, stake=1.0, mode="live")
        _set_closing("sx-1", "Chievo", 1.85, 1.80)
        b = ct.collect()
        assert b["excluded"] == {"outcome_uncanonical": 1}

    def test_nome_squadra_canonicalizzato(self, temp_db):
        """Il ledger non e' omogeneo: `fixture_engine` scrive il NOME squadra."""
        tracker.save_match("sx-1", "Serie A", "Roma", "Lazio",
                           datetime.now(timezone.utc).isoformat())
        tracker.save_bet(match_id="sx-1", mercato="1X2", esito="Roma",
                         price=1.85, stake=1.0, mode="live")
        _set_closing("sx-1", "1", 1.85, 1.80)
        s = ct.collect()["samples"][0]
        assert s["esito"] == "1"

    def test_indicizzazione_tollerante_dell_esito(self, temp_db):
        """Chiusura scritta col nome squadra, trade con `1`: aggancia."""
        _seed_bet()
        _set_closing("sx-1", "Osasuna", 1.85, 1.80)
        assert len(ct.collect()["samples"]) == 1


# ---------------------------------------------------------------------------
# 3. METRICHE — la statistica delega a `significance`
# ---------------------------------------------------------------------------

def _matured(rows=35, price=1.90, closing=1.80):
    """Semina un campione maturo (>= MIN_SAMPLES) con CLV positivo."""
    for i in range(rows):
        mid = f"sx-{i}"
        _seed_bet(match_id=mid, home=f"Home{i}", away=f"Away{i}",
                  esito="1", price=price + (i % 5) * 0.01)
        _set_closing(mid, "1", price, closing)


class TestMetriche:

    def test_soglia_condivisa_col_progetto(self):
        import significance
        assert ct.MIN_SAMPLES == significance.MIN_SAMPLES == 30

    def test_campione_vuoto_non_conclude(self):
        m = ct.summarize([])
        assert m["n"] == 0 and m["status"] == ct.STATUS_UNAVAILABLE
        assert m["avg_clv"] is None and m["note"]

    def test_campione_sotto_soglia_insufficiente(self, temp_db):
        for i in range(5):
            _seed_bet(match_id=f"sx-{i}", home=f"H{i}", away=f"A{i}",
                      esito="1")
            _set_closing(f"sx-{i}", "1", 1.85, 1.80)
        m = ct.summarize(ct.collect()["samples"])
        assert m["n"] == 5
        assert m["reliable"] is False
        assert m["status"] == ct.STATUS_INSUFFICIENT
        assert "campione insufficiente" in m["note"]

    def test_campione_maturo_positivo_batte_il_mercato(self, temp_db):
        _matured()
        m = ct.summarize(ct.collect()["samples"])
        assert m["n"] == 35 and m["reliable"] is True
        assert m["avg_clv"] > 0
        assert m["status"] == ct.STATUS_BEATING
        assert m["ci95"] and m["ci95"][0] > 0
        assert m["p_value"] is not None and m["p_value"] < 0.05

    def test_campione_maturo_negativo_perde(self, temp_db):
        for i in range(35):
            _seed_bet(match_id=f"sx-{i}", home=f"H{i}", away=f"A{i}",
                      esito="1", price=1.72 + (i % 5) * 0.01)
            _set_closing(f"sx-{i}", "1", 1.72, 1.80)
        m = ct.summarize(ct.collect()["samples"])
        assert m["status"] == ct.STATUS_LAGGING
        assert m["avg_clv"] < 0

    def test_detectable_clv_dichiarato(self, temp_db):
        _matured()
        m = ct.summarize(ct.collect()["samples"])
        assert m["detectable_clv"] is not None
        assert 0 < m["detectable_clv"] < 1

    def test_bucket_per_fonte_sport_mercato(self, temp_db):
        _seed_bet(match_id="sx-a", home="A", away="B", esito="1",
                  mercato="TENNIS", price=1.90, league="ATP - Shanghai")
        _set_closing("sx-a", "1", 1.90, 1.80, source=ct.SOURCE_SX)
        _seed_bet(match_id="sx-b", home="Roma", away="Lazio", esito="1",
                  mercato="1X2", price=1.90, league="Serie A")
        _set_closing("sx-b", "1", 1.90, 1.80)
        m = ct.summarize(ct.collect()["samples"])
        assert set(m["by_source"]) == {"sx", "pinnacle_t0"}
        assert set(m["by_sport"]) == {"tennis", "football"}
        assert set(m["by_market"]) == {"TENNIS", "1X2"}

    def test_metriche_robuste_a_input_ostile(self):
        hostile = [None, "x", {}, {"clv": "abc"}, {"clv": None},
                   {"clv": float("nan")}, {"clv": 0.05, "clv_vig_free": "z"}]
        m = ct.summarize(hostile)
        assert m["n"] == 1 and m["status"] == ct.STATUS_INSUFFICIENT

    def test_vig_free_solo_con_tutti_gli_esiti(self, temp_db):
        _seed_bet(match_id="sx-1", home="Roma", away="Lazio", esito="1")
        _set_closing("sx-1", "1", 1.85, 1.80)
        # manca X e 2: nessuna devig possibile
        assert ct.collect()["samples"][0]["clv_vig_free"] is None
        _seed_bet(match_id="sx-1", home="Roma", away="Lazio", esito="X",
                  price=3.40)
        _seed_bet(match_id="sx-1", home="Roma", away="Lazio", esito="2",
                  price=4.20)
        _set_closing("sx-1", "X", 3.40, 3.50)
        _set_closing("sx-1", "2", 4.20, 4.30)
        s = [x for x in ct.collect()["samples"] if x["esito"] == "1"][0]
        assert s["clv_vig_free"] is not None

    def test_classificazione_sport_deterministica(self):
        assert ct.sport_of("TENNIS", "") == "tennis"
        assert ct.sport_of("ML", "") == "esports"
        assert ct.sport_of("1X2", "NFL") == "football_usa"
        assert ct.sport_of("AH", "NCAAF") == "football_usa"
        assert ct.sport_of("1X2", "Serie A") == "football"

    def test_esiti_attesi_delegati_a_closing_line(self):
        assert ct.expected_outcomes("1X2") == ("1", "X", "2")
        assert ct.expected_outcomes("TENNIS") == ("1", "2")
        assert ct.expected_outcomes("AH") is None


# ---------------------------------------------------------------------------
# 4. CATTURA della closing line su SX Bet
# ---------------------------------------------------------------------------

class TestCatturaSx:

    def test_lookup_iniettato_scrive_la_chiusura(self, temp_db):
        _seed_bet(minutes=3.0)
        got = ct.capture_sx_closing(price_lookup=lambda row: 1.77)
        assert got["captured"] == 1 and got["checked"] == 1
        conn = tracker._get_conn()
        row = conn.execute("SELECT closing_odds, closing_source, signal_quota "
                           "FROM clv_history WHERE match_id='sx-1' AND "
                           "esito='1'").fetchone()
        conn.close()
        assert row[0] == pytest.approx(1.77)
        assert row[1] == ct.SOURCE_SX
        assert row[2] == pytest.approx(1.85)

    def test_fuori_finestra_non_cattura(self, temp_db):
        _seed_bet(minutes=600.0)
        got = ct.capture_sx_closing(price_lookup=lambda row: 1.77)
        assert got["captured"] == 0
        assert got["skipped"] == {"out_of_window": 1}

    def test_senza_kickoff_non_cattura(self, temp_db):
        tracker.save_bet(match_id="sx-orphan", mercato="1X2", esito="1",
                         market_id="0xm", price=1.85, stake=1.0, mode="live")
        got = ct.capture_sx_closing(price_lookup=lambda row: 1.77)
        assert got["skipped"] == {"no_kickoff": 1}

    def test_prima_chiusura_vince(self, temp_db):
        _seed_bet(minutes=3.0)
        _set_closing("sx-1", "1", 1.85, 1.80)
        got = ct.capture_sx_closing(price_lookup=lambda row: 1.77)
        assert got["skipped"] == {"already_captured": 1}
        conn = tracker._get_conn()
        row = conn.execute("SELECT closing_odds FROM clv_history WHERE "
                           "match_id='sx-1' AND esito='1'").fetchone()
        conn.close()
        assert row[0] == pytest.approx(1.80)     # Pinnacle resta la fonte

    def test_prezzo_non_leggibile_salta(self, temp_db):
        _seed_bet(minutes=3.0)
        assert ct.capture_sx_closing(
            price_lookup=lambda row: None)["skipped"] == {"no_price": 1}
        assert ct.capture_sx_closing(
            price_lookup=lambda row: 0.98)["skipped"] == {"no_price": 1}

    def test_lookup_rotto_non_solleva(self, temp_db):
        _seed_bet(minutes=3.0)

        def boom(_row):
            raise RuntimeError("book giu'")

        got = ct.capture_sx_closing(price_lookup=boom)
        assert got["captured"] == 0
        assert got["skipped"] == {"read_error": 1}

    def test_provider_non_sx_non_cattura(self, temp_db, monkeypatch):
        _seed_bet(minutes=3.0)
        import execution_engine as ee
        monkeypatch.setattr(ee, "EXECUTION_PROVIDER", "smarkets")
        got = ct.capture_sx_closing()
        assert got["captured"] == 0
        assert got["skipped"] == {"provider_not_sx": 1}

    def test_legge_il_book_sx_dal_provider(self, temp_db):
        _seed_bet(minutes=3.0, market_id="0xhash", selection_id=2)

        class Prov:
            calls = []

            def best_back_price(self, market_id, selection_id):
                Prov.calls.append((market_id, selection_id))
                return 2.11

        got = ct.capture_sx_closing(provider=Prov())
        assert got["captured"] == 1
        assert Prov.calls == [("0xhash", 2)]

    def test_finestra_delega_a_closing_line(self):
        pre, post = ct.window_minutes()
        import closing_line
        assert pre == closing_line.pre_minutes()
        assert post == closing_line.post_minutes()

    def test_puntata_senza_market_id_fuori_dal_batch(self, temp_db):
        tracker.save_match("sx-nom", "Serie A", "Roma", "Lazio",
                           (datetime.now(timezone.utc)
                            + timedelta(minutes=3)).isoformat())
        tracker.save_bet(match_id="sx-nom", mercato="1X2", esito="1",
                         price=1.85, stake=1.0, mode="live")
        got = ct.capture_sx_closing(price_lookup=lambda row: 1.77)
        assert got["checked"] == 0


# ---------------------------------------------------------------------------
# 5. REPORT, CLI, ISOLAMENTO
# ---------------------------------------------------------------------------

class TestReport:

    def test_struttura_e_righe(self, temp_db):
        _seed_bet()
        _set_closing("sx-1", "1", 1.85, 1.80)
        rep = ct.report()
        assert set(rep) >= {"totals", "excluded", "pending", "samples",
                            "metrics", "include_signals"}
        text = ct.format_report(rep)
        assert "CLV TRACKER" in text and "CLV medio" in text
        assert "Verdetto" in text

    def test_report_vuoto_non_mente(self, temp_db):
        text = ct.format_report()
        assert "nessun campione misurabile" in text
        assert "Verdetto" in text

    def test_cli_json_eseguibile(self, temp_db):
        """La CLI gira end-to-end su un ledger in SOLA LETTURA (`--db`)."""
        _seed_bet()
        _set_closing("sx-1", "1", 1.85, 1.80)
        assert ct.main(["--json", "--db", str(temp_db)]) == 0


class TestSolaLettura:

    def test_connessione_db_mode_ro_rifiuta_scritture(self, tmp_path):
        path = tmp_path / "ro.db"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE t (a INTEGER)")
        conn.execute("INSERT INTO t VALUES (1)")
        conn.commit()
        conn.close()
        ro = ct._connect(str(path))
        assert ro.execute("SELECT a FROM t").fetchone() == (1,)
        with pytest.raises(sqlite3.OperationalError):
            ro.execute("UPDATE t SET a = 2")
        ro.close()

    def test_nessuna_scrittura_diretta_nel_sorgente(self):
        """L'analisi NON scrive: l'unica scrittura passa da `tracker.save_clv`."""
        for banned in ("INSERT INTO", "DELETE FROM", "UPDATE ",
                       "place_limit_order", "_live_fill", "save_bet(",
                       "commit()"):
            assert banned not in SOURCE, f"scrittura/ordine nel sorgente: {banned}"

    def test_confine_col_tracker(self):
        """Le scritture passano da `tracker.save_clv`: mai SQL diretto."""
        assert "from tracker import save_clv" in SOURCE
        assert "sqlite3.connect(f\"file:{db}?mode=ro\"" in SOURCE

    def test_nessuna_soglia_di_strategia_ricopiata(self):
        for banned in ("ODDS_MAX =", "ODDS_MIN =", "EV_MIN =",
                       "MARKET_EDGE_MIN =", "KELLY_"):
            assert banned not in SOURCE, f"soglia ricopiata: {banned}"

    def test_import_leggero(self):
        out = subprocess.run(
            [sys.executable, "-c",
             "import sys, clv_tracker; "
             "print(' '.join(m for m in ('auto_bet', 'bot', 'execution_engine',"
             " 'odds_api') if m in sys.modules))"],
            capture_output=True, text=True, cwd=str(Path(ct.__file__).parent))
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == "", out.stdout


class TestCablaggioBot:
    """Il tracker e' COLLEGATO: job + comando, altrimenti e' codice morto."""

    @pytest.fixture()
    def bot_src(self):
        return Path("bot.py").read_text(encoding="utf-8")

    def test_job_registrato(self, bot_src):
        assert "clv_tracker_job" in bot_src
        assert "run_repeating(clv_tracker_job" in bot_src

    def test_comando_registrato_e_documentato(self, bot_src):
        assert 'CommandHandler("clv", cmd_clv)' in bot_src
        assert "`/clv" in bot_src

    def test_cattura_sx_dentro_closing_line_job(self, bot_src):
        assert "capture_sx_closing" in bot_src
        assert "CLV_SX_CAPTURE" in bot_src

    def test_job_non_notifica_gli_iscritti(self, bot_src):
        """Telemetria di ingegneria: solo admin."""
        start = bot_src.index("async def clv_tracker_job")
        end = bot_src.index("async def market_shadow_job")
        body = bot_src[start:end]
        assert "_admin_chat_ids()" in body
        assert "mark_notified(\"CLV_TRACKER\"" in body

    def test_anti_spam_giornaliero(self, bot_src):
        start = bot_src.index("async def clv_tracker_job")
        end = bot_src.index("async def market_shadow_job")
        assert "is_notified" in bot_src[start:end]


class TestIaC:
    """Le env devono stare in `preserve()`: un `config apply` non le distrugge."""

    def test_env_dichiarate(self):
        iac = Path(".railway/railway.ts").read_text(encoding="utf-8")
        for name in ("CLV_TRACKER_ENABLED", "CLV_SX_CAPTURE", "CLV_SX_PRE_MIN",
                     "CLV_SX_POST_MIN"):
            assert f"{name}: preserve()" in iac, name

    def test_ogni_env_letta_e_dichiarata(self):
        import re
        iac = Path(".railway/railway.ts").read_text(encoding="utf-8")
        read = set(re.findall(r'os\.getenv\(\s*"([A-Z0-9_]+)"', SOURCE))
        read |= set(re.findall(r'_num_env\(\s*"([A-Z0-9_]+)"', SOURCE))
        read |= set(re.findall(r'_flag\(\s*"([A-Z0-9_]+)"', SOURCE))
        missing = {n for n in read if n.startswith(("CLV_",))
                   and f"{n}: preserve()" not in iac}
        assert not missing, missing


# ---------------------------------------------------------------------------
# 5. COPERTURA: l'identita' dell'evento vive SULLA RIGA DEL DENARO (15/10/2026)
# ---------------------------------------------------------------------------
# Difetto misurato in produzione: la tabella `matches` viene POTATA
# (`clear_old_matches`, 2 giorni) mentre una puntata resta nel ledger per
# sempre. La closing line di una bet di una settimana prima non aveva piu' i
# nomi delle squadre -> il campione diventava "no_closing" e la copertura del
# CLV si fermava al 67%. I test qui sotto difendono il fix: la riga della
# puntata e' AUTOSUFFICIENTE, e il ripiego sulla partita resta per le righe
# storiche.

class TestIdentitaSullaRigaDelDenaro:

    def _prune_match(self, match_id):
        """Simula la potatura di `matches` (cio' che fa `clear_old_matches`)."""
        conn = tracker._get_conn()
        conn.execute("DELETE FROM matches WHERE id = ?", (match_id,))
        conn.commit()
        conn.close()

    def test_bet_sopravvive_alla_potatura_della_partita(self, temp_db):
        """SENZA il fix: riga `matches` cancellata -> campione perso."""
        tracker.save_bet(match_id="sx-vecchia", mercato="1X2", esito="1",
                         market_id="0xm", price=2.10, stake=2.0, mode="live",
                         status="FULLY_FILLED", bet_id="0xb",
                         home="Osasuna", away="Getafe", league="La Liga",
                         commence="2026-10-05T19:00:00+00:00")
        _set_closing("sx-vecchia", "1", 2.10, 1.95)
        self._prune_match("sx-vecchia")
        bundle = ct.collect()
        assert bundle["totals"]["trades"] == 1
        assert len(bundle["samples"]) == 1          # il campione NON si perde
        assert bundle["excluded"] == {}
        s = bundle["samples"][0]
        assert s["home"] == "Osasuna" and s["away"] == "Getafe"
        assert s["league"] == "La Liga"
        assert s["clv"] == pytest.approx((2.10 / 1.95) - 1.0)

    def test_nomi_squadra_servono_a_canonizzare_lesito(self, temp_db):
        """Il nome squadra sulla riga rende canonizzabile un esito-name.

        E' il motivo per cui i quattro campi non sono decorativi: senza nomi,
        un esito `Osasuna` non si aggancia alla riga CLV canonica `1`.
        """
        tracker.save_bet(match_id="sx-nome", mercato="1X2", esito="Osasuna",
                         market_id="0xm", price=1.90, stake=2.0, mode="live",
                         status="FULLY_FILLED", bet_id="0xb",
                         home="Osasuna", away="Getafe", league="La Liga",
                         commence="2026-10-05T19:00:00+00:00")
        _set_closing("sx-nome", "1", 1.90, 1.80)   # riga canonica `1`
        self._prune_match("sx-nome")
        bundle = ct.collect()
        assert len(bundle["samples"]) == 1
        assert bundle["samples"][0]["closing"] == pytest.approx(1.80)

    def test_riga_storica_senza_identita_usa_il_ripiego(self, temp_db):
        """Retrocompatibilita': le bet vecchie prendono i nomi da `matches`."""
        _seed_bet(match_id="sx-storica", home="Roma", away="Lazio", esito="1",
                  price=1.85)
        _set_closing("sx-storica", "1", 1.85, 1.75)
        s = ct.collect()["samples"][0]
        assert s["home"] == "Roma" and s["away"] == "Lazio"
        assert s["closing"] == pytest.approx(1.75)

    def test_la_riga_del_denaro_vince_sulla_partita(self, temp_db):
        """Se i due valori divergono, vale quello scritto dalla puntata.

        Un nome aggiornato/riscritto in `matches` non deve cambiare l'identita'
        di un ordine gia' eseguito: il Ledger del denaro e' la fonte.
        """
        tracker.save_match("sx-x", "Serie A", "Squadra Vecchia", "Altro",
                           (datetime.now(timezone.utc)
                            + timedelta(minutes=5)).isoformat())
        tracker.save_bet(match_id="sx-x", mercato="1X2", esito="1",
                         market_id="0xm", price=1.9, stake=1.0, mode="live",
                         status="FULLY_FILLED", bet_id="0xb",
                         home="Squadra Nuova", away="Avversario", league="Serie A",
                         commence="2026-10-05T19:00:00+00:00")
        _set_closing("sx-x", "1", 1.9, 1.8)
        s = ct.collect()["samples"][0]
        assert s["home"] == "Squadra Nuova"

    def test_cattura_sx_funziona_su_partita_potata(self, temp_db):
        """La cattura della chiusura SX usa la stessa identita' (LEFT JOIN)."""
        tracker.save_bet(match_id="sx-open", mercato="1X2", esito="1",
                         market_id="0xhash", selection_id=1, price=1.85,
                         stake=1.0, mode="live", status="FULLY_FILLED",
                         bet_id="0xb", home="Roma", away="Lazio",
                         league="Serie A",
                         commence=(datetime.now(timezone.utc)
                                   + timedelta(minutes=3)).isoformat())
        self._prune_match("sx-open")
        got = ct.capture_sx_closing(price_lookup=lambda row: 1.77)
        assert got["captured"] == 1
        assert got["skipped"] == {}

    def test_colonne_presenti_e_migrazione_idempotente(self, temp_db):
        """La migrazione aggiunge i 4 campi anche su un DB nato prima."""
        conn = tracker._get_conn()
        cols = [r[1] for r in conn.execute("PRAGMA table_info(bets)")]
        conn.close()
        assert set(tracker.BET_EVENT_FIELDS) <= set(cols)
        assert tracker.BET_EVENT_FIELDS == ("home", "away", "league",
                                            "commence")

    def test_un_salvataggio_senza_identita_non_la_cancella(self, temp_db):
        """COALESCE + NULLIF: l'UPDATE non azzera cio' che e' gia' registrato."""
        tracker.save_bet(match_id="sx-k", mercato="1X2", esito="1",
                         market_id="0xm", price=1.9, stake=1.0, mode="live",
                         home="Roma", away="Lazio", league="Serie A",
                         commence="2026-10-05T19:00:00+00:00")
        tracker.save_bet(match_id="sx-k", mercato="1X2", esito="1",
                         market_id="0xm", price=1.95, stake=1.0, mode="live")
        conn = tracker._get_conn()
        row = conn.execute("SELECT home, away, league, commence, price "
                           "FROM bets WHERE match_id='sx-k'").fetchone()
        conn.close()
        assert row[0] == "Roma" and row[1] == "Lazio"
        assert row[2] == "Serie A" and row[3] == "2026-10-05T19:00:00+00:00"
        assert row[4] == pytest.approx(1.95)      # il prezzo invece si aggiorna


class TestIdentitaDalPick:
    """`auto_bet.event_identity` e' l'unico punto di estrazione dal pick."""

    def test_estrazione_dai_pick_delle_corsie(self):
        import auto_bet as ab
        pick = {"home": "Roma", "away": "Lazio", "league": "Serie A",
                "commence": "2026-10-05T19:00:00+00:00", "mercato": "1X2"}
        assert ab.event_identity(pick) == {
            "home": "Roma", "away": "Lazio", "league": "Serie A",
            "commence": "2026-10-05T19:00:00+00:00"}

    def test_pick_senza_campi_non_produce_none(self):
        """Mai `None`: le colonne sono TEXT e le scrivono cosi' come sono."""
        import auto_bet as ab
        assert ab.event_identity({}) == {"home": "", "away": "",
                                         "league": "", "commence": ""}
        assert ab.event_identity({"home": None, "league": "  "})["home"] == ""
        assert ab.event_identity({"home": None})["league"] == ""

    def test_scrittura_live_del_giro_usa_la_identita(self):
        """Il percorso che scrive le bet live la passa a `save_bet`."""
        src = Path("auto_bet.py").read_text(encoding="utf-8")
        assert src.count("**event_identity(") >= 4
        assert "def event_identity(" in src


# ---------------------------------------------------------------------------
# 6. SECONDO BENCHMARK nel tracker: la conferma INDIPENDENTE (15/10/2026)
# ---------------------------------------------------------------------------
# Il CLV medio contro Pinnacle puo' essere un artefatto della singola fonte.
# Quando esiste anche la chiusura dell'EXCHANGE (Betfair EX EU), il campione
# porta DUE misure e il report dichiara in quanti casi i due sharp concordano.
# Nessun requisito nuovo: senza benchmark il trade resta misurabile come prima.

class TestSecondoBenchmark:

    def _with_benchmark(self, match_id="sx-b", home="Osasuna", away="Getafe",
                        esito="1", price=1.90, closing=1.80, bench=1.75):
        tracker.save_bet(match_id=match_id, mercato="1X2", esito=esito,
                         market_id="0xm", price=price, stake=2.0, mode="live",
                         status="FULLY_FILLED", bet_id="0xb",
                         home=home, away=away, league="La Liga",
                         commence="2026-10-05T19:00:00+00:00")
        tracker.save_clv(match_id, esito, price, closing_odds=closing,
                         closing_source="pinnacle_t0", betfair_odds=bench)

    def test_il_campione_porta_entrambe_le_misure(self, temp_db):
        self._with_benchmark()
        s = ct.collect()["samples"][0]
        assert s["clv"] == pytest.approx((1.90 / 1.80) - 1.0)
        assert s["benchmark"] == pytest.approx(1.75)
        assert s["clv_benchmark"] == pytest.approx((1.90 / 1.75) - 1.0)
        assert s["benchmark_source"] == ct.SOURCE_BENCHMARK

    def test_senza_benchmark_il_campione_resta_misurabile(self, temp_db):
        """Nessun benchmark a ledger: il CLV primario non cambia di una virgola."""
        _seed_bet(match_id="sx-nb", price=1.90)
        _set_closing("sx-nb", "1", 1.90, 1.80)
        bundle = ct.collect()
        s = bundle["samples"][0]
        assert s["clv"] == pytest.approx((1.90 / 1.80) - 1.0)
        assert s["benchmark"] is None and s["clv_benchmark"] is None
        assert s["benchmark_source"] is None
        m = ct.summarize(bundle["samples"])
        assert m["benchmark"]["n"] == 0
        assert m["benchmark"]["confirmed_both"] == 0
        assert m["benchmark"]["source"] == ct.SOURCE_BENCHMARK

    def test_metrics_contano_la_conferma_doppia(self, temp_db):
        self._with_benchmark(match_id="sx-b1", esito="1", price=1.90,
                             closing=1.80, bench=1.75)   # entrambi positivi
        self._with_benchmark(match_id="sx-b2", home="Roma", away="Lazio",
                             esito="1", price=1.70, closing=1.80,
                             bench=1.85)                  # primario negativo
        m = ct.summarize(ct.collect()["samples"])
        b = m["benchmark"]
        assert b["n"] == 2
        assert b["confirmed_both"] == 1
        assert b["confirmed_share"] == pytest.approx(0.5)
        assert b["avg_clv"] == round(
            (((1.90 / 1.75) - 1.0) + ((1.70 / 1.85) - 1.0)) / 2.0, 6)
        assert b["share_positive"] == pytest.approx(0.5)

    def test_un_benchmark_assente_non_declassa_gli_altri(self, temp_db):
        """I campioni senza benchmark non entrano nella media del benchmark."""
        self._with_benchmark(match_id="sx-c1", esito="1", bench=1.75)
        _seed_bet(match_id="sx-c2", home="Roma", away="Lazio", esito="1",
                  price=1.80)
        _set_closing("sx-c2", "1", 1.80, 1.70)
        m = ct.summarize(ct.collect()["samples"])
        assert m["n"] == 2                       # entrambi misurati (primaria)
        assert m["benchmark"]["n"] == 1          # uno solo ha l'exchange
        assert m["benchmark"]["confirmed_both"] == 1

    def test_save_clv_non_cancella_il_benchmark(self, temp_db):
        """COALESCE anche sulla colonna del secondo benchmark."""
        self._with_benchmark(match_id="sx-k", esito="1")
        tracker.save_clv("sx-k", "1", 1.95)      # scrittura senza benchmark
        conn = tracker._get_conn()
        row = conn.execute("SELECT closing_odds, betfair_odds FROM clv_history "
                           "WHERE match_id='sx-k'").fetchone()
        conn.close()
        assert row[0] == pytest.approx(1.80) and row[1] == pytest.approx(1.75)

    def test_report_dichiara_la_fonte_e_la_conferma(self, temp_db):
        self._with_benchmark()
        text = ct.format_report(ct.report())
        assert ct.SOURCE_BENCHMARK in text
        assert "ENTRAMBI" in text

    def test_report_senza_benchmark_non_inventa_una_conferma(self, temp_db):
        _seed_bet(match_id="sx-nb2", price=1.90)
        _set_closing("sx-nb2", "1", 1.90, 1.80)
        text = ct.format_report(ct.report())
        assert "conferma indipendente non e' disponibile" in text

    def test_lesito_non_canonicale_non_rompe_il_benchmark(self, temp_db):
        """Riga ostile nel campo benchmark: il campione resta, il valore no."""
        tracker.save_bet(match_id="sx-ost", mercato="1X2", esito="1",
                         market_id="0xm", price=1.9, stake=1.0, mode="live",
                         home="Roma", away="Lazio", league="Serie A",
                         commence="2026-10-05T19:00:00+00:00")
        _set_closing("sx-ost", "1", 1.9, 1.8)
        conn = tracker._get_conn()
        conn.execute("UPDATE clv_history SET betfair_odds='abc' WHERE "
                     "match_id='sx-ost'")
        conn.commit()
        conn.close()
        s = ct.collect()["samples"][0]
        assert s["clv"] == pytest.approx((1.9 / 1.8) - 1.0)
        assert s["clv_benchmark"] is None

    def test_il_loader_dello_sharp_e_generico(self):
        """`sharp_odds_from_cache` esiste e `pinnacle_odds_from_cache` delega."""
        import inspect
        import pinnacle_oracle as po
        assert hasattr(po, "sharp_odds_from_cache")
        assert hasattr(po, "benchmark_odds_from_cache")
        src = inspect.getsource(po.pinnacle_odds_from_cache)
        assert "sharp_odds_from_cache(" in src      # nessuna copia del percorso
        assert po.BENCHMARK_BOOK == "betfair_ex_eu"


# ---------------------------------------------------------------------------
# 12. SCHEMA DEGRADATO — "0 campioni" e "query rotta" sono diagnosi diverse
# ---------------------------------------------------------------------------

_OLD_BETS = """CREATE TABLE bets (
    id INTEGER PRIMARY KEY AUTOINCREMENT, match_id TEXT, mercato TEXT,
    esito TEXT, market_id TEXT, selection_id INTEGER, price REAL, stake REAL,
    mode TEXT, status TEXT, bet_id TEXT, esito_finale TEXT, profit REAL,
    created_at TEXT, settled_at TEXT)"""
_OLD_MATCHES = """CREATE TABLE matches (
    id TEXT PRIMARY KEY, league TEXT, home_team TEXT, away_team TEXT,
    commence_time TEXT)"""
_OLD_CLV = """CREATE TABLE clv_history (
    match_id TEXT, esito TEXT, signal_quota REAL, closing_quota REAL,
    updated_at TEXT, pinnacle_quota REAL, closing_odds REAL,
    closing_source TEXT, PRIMARY KEY (match_id, esito))"""


def _legacy_db(tmp_path, *, with_bets=True, with_closing=True):
    """Ledger come un deploy PRECEDENTE alla migrazione del 15/10/2026.

    `bets` senza home/away/league/commence e `clv_history` senza
    `betfair_odds`: e' lo stato REALE del ledger di produzione al momento della
    prima misura (16/10/2026), dove una colonna assente faceva leggere
    "0 trade live" a fronte di 67 puntate a ledger.
    """
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.execute(_OLD_MATCHES)
    conn.execute(_OLD_CLV)
    if with_bets:
        conn.execute(_OLD_BETS)
    conn.execute("INSERT INTO matches VALUES (?,?,?,?,?)",
                 ("sx-old", "Serie A", "Osasuna", "Getafe",
                  (datetime.now(timezone.utc) + timedelta(minutes=8)).isoformat()))
    if with_bets:
        conn.execute("INSERT INTO bets (match_id, mercato, esito, market_id, "
                     "selection_id, price, stake, mode, status, bet_id, "
                     "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     ("sx-old", "1X2", "1", "0xm", 1, 1.90, 1.5, "live",
                      "FULLY_FILLED", "0xb",
                      datetime.now(timezone.utc).isoformat()))
    if with_closing:
        conn.execute("INSERT INTO clv_history (match_id, esito, signal_quota, "
                     "closing_odds, closing_source) VALUES (?,?,?,?,?)",
                     ("sx-old", "1", 1.90, 1.80, "pinnacle_t0"))
    conn.commit()
    conn.close()
    return path


class TestSchemaDegradato:

    def test_lo_schema_reale_viene_dichiarato(self, temp_db):
        _seed_bet()
        _set_closing("sx-1", "1", 1.90, 1.80)
        got = ct.collect()["schema"]
        assert set(got["bets_event_fields"]) == set(ct._BET_EVENT_COLS)
        assert "betfair_odds" in got["clv_columns"]

    def test_ledger_vecchio_misura_lo_stesso_grazie_a_matches(self, tmp_path):
        """REGRESSIONE della prima misura in produzione (16/10/2026)."""
        db = _legacy_db(tmp_path)
        bundle = ct.collect(db=str(db))
        assert bundle["query_errors"] == []
        assert bundle["totals"]["trades"] == 1
        assert len(bundle["samples"]) == 1
        s = bundle["samples"][0]
        assert s["clv"] == pytest.approx((1.90 / 1.80) - 1.0)
        assert s["benchmark"] is None

    def test_le_due_degradazioni_sono_dichiarate(self, tmp_path):
        rep = ct.report(db=str(_legacy_db(tmp_path)))
        text = " | ".join(rep["degraded"])
        assert "NON sulla puntata" in text
        assert "betfair_odds" in text
        assert rep["schema"]["bets_event_fields"] == []

    def test_il_report_dice_la_degradazione(self, tmp_path):
        text = ct.format_report(ct.report(db=str(_legacy_db(tmp_path))))
        assert "⚠️ identita' dell'evento NON sulla puntata" in text
        assert "secondo benchmark non disponibile" in text

    def test_senza_tabella_bets_la_query_rotta_e_dichiarata(self, tmp_path):
        """`bets` assente NON si legge come "0 puntate": si legge come guasto."""
        rep = ct.report(db=str(_legacy_db(tmp_path, with_bets=False)))
        assert rep["query_errors"], "una tabella assente va dichiarata"
        assert "no such table" in rep["query_errors"][0]
        assert "query non eseguibile" in ct.format_report(rep)

    def test_la_cattura_sx_regge_lo_schema_vecchio(self, tmp_path):
        """`capture_sx_closing` costruisce la query sulle colonne esistenti."""
        conn = sqlite3.connect(str(_legacy_db(tmp_path)))
        out = ct.capture_sx_closing(conn=conn, price_lookup=lambda p: None)
        conn.close()
        assert out["checked"] == 1 and out["captured"] == 0

    def test_le_query_si_costruiscono_sulle_colonne_esistenti(self):
        """Tripwire: nessuna SELECT nominale sulle colonne migrabili."""
        assert "_table_columns(" in SOURCE
        assert "_bet_sql(" in SOURCE and "_open_bets_sql(" in SOURCE
        assert "_clv_sql(" in SOURCE
        for banned in ("_BET_SQL = (", "_OPEN_BET_SQL = (", "_CLV_SQL = ("):
            assert banned not in SOURCE, banned

    def test_il_fail_safe_conta_gli_errori(self, tmp_path):
        """`_rows` continua a non sollevare, ma ora CONTA (mai un silenzio)."""
        conn = sqlite3.connect(str(_legacy_db(tmp_path)))
        errs = []
        assert ct._rows(conn, "SELECT boh FROM non_esiste", errors=errs) == []
        assert errs and "no such table" in errs[0]
        conn.close()
