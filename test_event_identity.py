"""test_event_identity.py — IDENTITA' UNICA DEGLI EVENTI (P3, 17/10/2026).

Perche' esiste: il 17/10 la stessa partita (Norwich-Portsmouth) era nel sistema
con **due `match_id`** e sono partiti due ordini reali sullo stesso
`market_id`/`selection_id`. Qui si verifica la chiave canonica dell'evento e
l'audit che MISURA il problema (sola lettura, prima di toccare lo schema).

Tutti i test sono OFFLINE: SQLite temporaneo, nessuna rete.
"""
import json
import sqlite3
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta, timezone

import pytest

import event_identity as ei


# ---------------------------------------------------------------------------
# 1. Nome canonico e chiave
# ---------------------------------------------------------------------------

class TestChiaveEvento:
    def test_forma_leggibile(self, known=("Norwich", "Portsmouth")):
        key = ei.event_key("Norwich", "Portsmouth", "2026-10-17T14:00:00Z",
                           known=known)
        assert key == "norwich|portsmouth|2026-10-17"

    def test_stessa_partita_stessa_chiave(self):
        known = ("Norwich", "Portsmouth")
        a = ei.event_key("Norwich", "Portsmouth", "2026-10-17T14:00:00Z",
                         known=known)
        b = ei.event_key("Norwich FC", "Portsmouth FC",
                         "2026-10-17T14:00:00Z", known=known)
        assert a == b

    def test_variante_di_nome_risolta_col_pool(self):
        """`Norwich City` e `Norwich` sono lo stesso club quando il pool lo dice."""
        known = ("Norwich", "Portsmouth")
        assert ei.canonical_team("Norwich City", known) == "norwich"

    def test_nome_sconosciuto_resta_se_stesso(self):
        """Senza pool non si inventa nulla: il nome normalizzato e' la chiave."""
        # La normalizzazione ordina i token e toglie i numeri di fondazione:
        # due nomi scritti in ordine diverso danno la stessa chiave.
        assert ei.canonical_team("Team Ignoto 1900", known=()) == "ignoto team"
        assert ei.canonical_team("Ignoto Team", known=()) == "ignoto team"

    def test_drift_di_kickoff_nello_stesso_giorno(self):
        known = ("Norwich", "Portsmouth")
        a = ei.event_key("Norwich", "Portsmouth", "2026-10-17T20:55:00Z", known=known)
        b = ei.event_key("Norwich", "Portsmouth", "2026-10-17T21:05:00Z", known=known)
        assert a == b, "la chiave non deve cambiare per 10 minuti di scarto"

    def test_giorni_diversi_chiavi_diverse(self):
        known = ("Norwich", "Portsmouth")
        a = ei.event_key("Norwich", "Portsmouth", "2026-10-17T20:55:00Z", known=known)
        b = ei.event_key("Norwich", "Portsmouth", "2026-10-24T20:55:00Z", known=known)
        assert a != b

    def test_squadre_invertite_chiavi_diverse_ma_pair(self):
        known = ("Norwich", "Portsmouth")
        k1, k2 = ei.event_key_pair("Norwich", "Portsmouth",
                                   "2026-10-17T14:00:00Z", known=known)
        assert k1 == "norwich|portsmouth|2026-10-17"
        assert k2 == "portsmouth|norwich|2026-10-17"

    def test_bucket_opzionale(self):
        known = ("Norwich", "Portsmouth")
        key = ei.event_key("Norwich", "Portsmouth", "2026-10-17T14:00:00Z",
                           bucket_hours=3, known=known)
        assert key == "norwich|portsmouth|2026-10-17T12"

    def test_default_del_bucket_e_la_data(self):
        assert ei.BUCKET_HOURS == 0, ("il bucket a ore spaccherebbe l'evento "
                                      "al confine dell'ora")

    @pytest.mark.parametrize("bad", [None, "", "2026-10-17T14:00:00",
                                     "non-una-data"])
    def test_kickoff_ambiguo_o_assente_non_da_chiave(self, bad):
        assert ei.event_key("Norwich", "Portsmouth", bad,
                            known=("Norwich", "Portsmouth")) is None

    @pytest.mark.parametrize("home,away", [("", "Portsmouth"),
                                           ("Norwich", ""),
                                           (None, None)])
    def test_nome_mancante_non_da_chiave(self, home, away):
        assert ei.event_key(home, away, "2026-10-17T14:00:00Z",
                            known=("Norwich", "Portsmouth")) is None


# ---------------------------------------------------------------------------
# 2. Audit dei duplicati (sola lettura)
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path, monkeypatch):
    import tracker
    path = tmp_path / "ident.db"
    monkeypatch.setattr(tracker, "DB_PATH", path)
    tracker.init_db()
    return path


def _match(conn, mid, home, away, kickoff, league="Championship"):
    conn.execute("INSERT OR REPLACE INTO matches (id, league, home_team, "
                 "away_team, commence_time, status) VALUES (?,?,?,?,?,?)",
                 (mid, league, home, away, kickoff, "upcoming"))


def _bet(conn, match_id, stake=2.0):
    conn.execute("INSERT INTO bets (match_id, mercato, esito, price, stake, "
                 "mode, status, created_at) VALUES (?,?,?,?,?,?,?,?)",
                 (match_id, "1X2", "1", 1.8, stake, "live", "FULLY_FILLED",
                  datetime.now(timezone.utc).isoformat()))


class TestAuditDuplicati:
    def _seed(self, path):
        conn = sqlite3.connect(str(path))
        kick = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        _match(conn, "sx-L20471322", "Norwich", "Portsmouth", kick)
        _match(conn, "7a8f8a07aa", "Norwich FC", "Portsmouth FC", kick)
        _bet(conn, "sx-L20471322", 4.98)
        _bet(conn, "7a8f8a07aa", 2.79)
        conn.commit()
        conn.close()

    def test_trova_il_doppio_id(self, db):
        self._seed(db)
        res = ei.audit_duplicate_events(str(db), days=7)
        assert res["error"] is None
        assert res["events"] == 1
        assert res["duplicates"] == 1
        cluster = res["clusters"][0]
        assert set(cluster["ids"]) == {"sx-L20471322", "7a8f8a07aa"}
        assert cluster["bets"] == 2
        assert cluster["stake"] == pytest.approx(7.77)
        # La chiave dell'evento e' una sola, benché i nomi differiscano.
        assert cluster["event_key"] == "norwich|portsmouth|%s" % (
            datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat()

    def test_partite_diverse_non_si_fondono(self, db):
        conn = sqlite3.connect(str(db))
        kick = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        _match(conn, "m1", "Manchester United", "Chelsea", kick)
        _match(conn, "m2", "Manchester City", "Arsenal", kick)
        conn.commit(); conn.close()
        res = ei.audit_duplicate_events(str(db), days=7)
        assert res["clusters"] == [] and res["duplicates"] == 0

    def test_andata_e_ritorno_non_si_fondono(self, db):
        conn = sqlite3.connect(str(db))
        kick = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        _match(conn, "a", "Norwich", "Portsmouth", kick)
        _match(conn, "b", "Norwich", "Portsmouth",
               (datetime.now(timezone.utc) + timedelta(days=30)).isoformat())
        conn.commit(); conn.close()
        res = ei.audit_duplicate_events(str(db), days=45)
        assert res["duplicates"] == 0

    def test_db_assente_e_fail_safe(self, tmp_path):
        res = ei.audit_duplicate_events(str(tmp_path / "nope.db"))
        assert res["error"] and res["clusters"] == []

    def test_file_non_sqlite_e_fail_safe(self, tmp_path):
        bad = tmp_path / "bad.db"
        bad.write_text("non sono un database")
        res = ei.audit_duplicate_events(str(bad))
        assert res["error"] is not None
        assert res["clusters"] == []

    def test_idempotente_non_scrive_nulla(self, db):
        self._seed(db)
        before = db.read_bytes()
        ei.audit_duplicate_events(str(db), days=7)
        ei.audit_duplicate_events(str(db), days=7)
        assert db.read_bytes() == before

    def test_report_leggibile(self, db):
        self._seed(db)
        res = ei.audit_duplicate_events(str(db), days=7)
        text = ei.format_report(res)
        assert "Norwich" in text
        assert "sx-L20471322" in text


class TestSolaLettura:
    def test_connessione_rifiuta_le_scritture(self, db):
        conn = ei._connect_ro(str(db))
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("UPDATE matches SET league='x'")
        conn.close()

    def test_il_sorgente_non_scrive(self):
        src = open("event_identity.py", encoding="utf-8").read().upper()
        for verb in ("INSERT INTO", "UPDATE ", "DELETE FROM", "DROP TABLE"):
            assert verb not in src, "il modulo di sola lettura non deve %s" % verb

    def test_import_leggero(self):
        code = textwrap.dedent("""
            import sys
            import event_identity
            heavy = [m for m in ("auto_bet", "bot", "tracker", "odds_api",
                                 "execution_engine") if m in sys.modules]
            print(",".join(heavy))
        """)
        out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                             text=True, timeout=120)
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == ""


class TestCLI:
    def test_json_su_db_temporaneo(self, db, capsys):
        kick = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        conn = sqlite3.connect(str(db))
        _match(conn, "x1", "Norwich", "Portsmouth", kick)
        _match(conn, "x2", "Norwich", "Portsmouth", kick)
        conn.commit(); conn.close()
        rc = ei.main(["--db", str(db), "--days", "7", "--json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["duplicates"] == 1
