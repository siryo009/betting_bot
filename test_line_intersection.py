"""Test OFFLINE di `line_intersection.py` (strumento di misura, 07/10/2026).

Misura l'intersezione fra la griglia di SX Bet e le linee prezzate
dall'oracolo Pinnacle: e' il KPI della direttiva "i pick non nascono non
ordinabili". Deve restare SOLA LETTURA, OFFLINE e fail-safe: nessuna rete,
nessun credito, nessun ordine, nessuna eccezione al chiamante.

Qui si verifica: I/O in read-only (una `UPDATE` deve essere RIFIUTATA), i
quattro esiti per (fixture, mercato), il conteggio dei pick giocabili con
linea non prezzabile, i default/env della finestra, il report e i tripwire
sul sorgente.
"""

import json
import sqlite3
import time

import pytest

import line_intersection as li
import tracker

FIXTURE = "sx-L20067612"
HOME, AWAY = "Chicago Fire", "Vancouver Whitecaps"
SPORT = "soccer_usa_mls"


@pytest.fixture
def db(monkeypatch, tmp_path):
    """DB temporaneo con lo schema di produzione + quote/pick seminati."""
    path = tmp_path / "ledger.db"
    monkeypatch.setattr(tracker, "DB_PATH", path)
    conn = tracker._get_conn()
    conn.close()
    return path


def _write_oracle(tmp_path, *, totals=(), spreads=(), home=HOME, away=AWAY,
                  sport=SPORT, age_h=0.0, with_pinnacle=True):
    """Cache oracolo finta (`toao_*.json`) con i mercati Pinnacle."""
    markets = []
    if totals:
        outcomes = [{"name": "Over", "price": 1.95, "point": float(l)}
                    for l in totals]
        outcomes += [{"name": "Under", "price": 1.95, "point": float(l)}
                     for l in totals]
        markets.append({"key": "totals", "outcomes": outcomes})
    if spreads:
        outcomes = []
        for l in spreads:
            outcomes.append({"name": home, "price": 1.90, "point": float(l)})
            outcomes.append({"name": away, "price": 1.90, "point": -float(l)})
        markets.append({"key": "spreads", "outcomes": outcomes})
    books = ([{"key": "pinnacle", "title": "Pinnacle", "markets": markets}]
             if with_pinnacle else [])
    payload = [{"home_team": home, "away_team": away, "bookmakers": books}]
    (tmp_path / f"toao_{sport}.json").write_text(json.dumps(
        {"ts": time.time() - age_h * 3600.0, "payload": payload}))


def _seed_quotes(db, lines, *, market_type="OU", fixture=FIXTURE):
    conn = sqlite3.connect(db)
    for ln in lines:
        conn.execute(
            "INSERT OR REPLACE INTO market_quotes "
            "(fixture_id, market_type, line_key, line, selection, price, "
            " home, away, league, kickoff) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (fixture, market_type, str(ln), float(ln), "over", 1.95,
             HOME, AWAY, "MLS", "2030-01-01T00:30:00Z"))
    conn.commit()
    conn.close()


def _seed_prediction(db, *, match_id=FIXTURE, esito="Over 2.5", status="value",
                     market="OU"):
    conn = sqlite3.connect(db)
    conn.execute("INSERT OR REPLACE INTO matches "
                 "(id, home_team, away_team, commence_time, league) "
                 "VALUES (?,?,?,?,?)",
                 (match_id, HOME, AWAY, "2030-01-01T00:30:00Z", "MLS"))
    conn.execute("INSERT OR REPLACE INTO predictions "
                 "(match_id, mercato, esito, status, quota, league) "
                 "VALUES (?,?,?,?,?,?)",
                 (match_id, market, esito, status, 1.6, "MLS"))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# 1. Finestra e connessione
# ---------------------------------------------------------------------------

class TestFinestra:
    def test_default(self, monkeypatch):
        monkeypatch.delenv("LINE_INTERSECTION_DAYS", raising=False)
        assert li.window_days() == 7.0

    def test_env(self, monkeypatch):
        monkeypatch.setenv("LINE_INTERSECTION_DAYS", "3")
        assert li.window_days() == 3.0

    def test_valore_impossibile_ricade_sul_default(self, monkeypatch):
        for raw in ("", "abc", "0", "-2"):
            monkeypatch.setenv("LINE_INTERSECTION_DAYS", raw)
            assert li.window_days() == 7.0, raw


class TestSolaLettura:
    """La misura NON deve poter scrivere: la connessione e' `mode=ro`."""

    def test_connessione_rifiuta_una_update(self, db):
        conn = li._ro_conn(str(db))
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("UPDATE predictions SET status='value'")
        conn.close()

    def test_il_sorgente_non_contiene_scritture(self):
        import inspect
        src = inspect.getsource(li)
        for bad in ("INSERT", "UPDATE ", "DELETE", "DROP", "commit()"):
            assert bad not in src, bad

    def test_nessuna_rete_ne_ordini(self):
        import inspect
        src = inspect.getsource(li)
        for bad in ("requests", "aiohttp", "place_limit_order", "_live_fill",
                    "resolve_market_for", "execution_engine", "fetch_line_odds"):
            assert bad not in src, bad


# ---------------------------------------------------------------------------
# 2. Esiti per (fixture, mercato)
# ---------------------------------------------------------------------------

class TestEsitiFixture:
    def test_oracolo_ignoto(self, db, tmp_path):
        _seed_quotes(db, [3.0, 3.5, 4.0])
        rep = li.measure(days=7, db=str(db))
        assert rep["by_status"] == {"unknown": 1}
        assert rep["totals"]["fixtures_no_intersection"] == 0

    def test_oracolo_vuoto(self, db, tmp_path):
        _write_oracle(tmp_path, totals=(), with_pinnacle=True)
        _seed_quotes(db, [3.0, 3.5])
        rep = li.measure(days=7, db=str(db))
        assert rep["by_status"] == {"oracle_empty": 1}

    def test_intersezione_vuota(self, db, tmp_path):
        """Il caso MLS del 06-07/10: SX a passi di 0,5 vs Pinnacle 3.25."""
        _write_oracle(tmp_path, totals=(3.25,))
        _seed_quotes(db, [3.0, 3.5, 4.0])
        rep = li.measure(days=7, db=str(db))
        assert rep["by_status"] == {"no_intersection": 1}
        f = rep["fixtures"][0]
        assert f["oracle_lines"] == [3.25]
        assert f["priceable_lines"] == []
        assert rep["totals"]["fixtures_no_intersection"] == 1
        assert rep["totals"]["line_coverage_pct"] == 0.0

    def test_prezzabile(self, db, tmp_path):
        _write_oracle(tmp_path, totals=(3.0,))
        _seed_quotes(db, [2.5, 3.0, 3.5])
        rep = li.measure(days=7, db=str(db))
        assert rep["by_status"] == {"priceable": 1}
        assert rep["fixtures"][0]["priceable_lines"] == [3.0]
        assert rep["totals"]["sx_lines"] == 3
        assert rep["totals"]["sx_lines_priceable"] == 1

    def test_cache_stantia_vale_come_ignota(self, db, tmp_path):
        _write_oracle(tmp_path, totals=(3.0,), age_h=100.0)
        _seed_quotes(db, [3.0])
        rep = li.measure(days=7, db=str(db))
        assert rep["by_status"] == {"unknown": 1}

    def test_due_mercati_della_stessa_fixture(self, db, tmp_path):
        _write_oracle(tmp_path, totals=(3.0,), spreads=(0.5,))
        _seed_quotes(db, [3.0], market_type="OU")
        _seed_quotes(db, [0.5, 1.5], market_type="AH")
        rep = li.measure(days=7, db=str(db))
        assert rep["by_status"] == {"priceable": 2}
        assert sorted(rep["by_league"]["MLS"]) == ["priceable"]
        assert rep["by_league"]["MLS"]["priceable"] == 2


# ---------------------------------------------------------------------------
# 3. Pick aperti: il KPI "non nascono non ordinabili"
# ---------------------------------------------------------------------------

class TestPickAperti:
    def test_pick_giocabile_con_linea_non_prezzabile(self, db, tmp_path):
        _write_oracle(tmp_path, totals=(3.25,))
        _seed_quotes(db, [3.0, 3.5, 4.0])
        _seed_prediction(db, esito="Under 4.5", status="strong_value")
        rep = li.measure(days=7, db=str(db))
        t = rep["totals"]
        assert t["picks_open"] == 1
        assert t["picks_playable"] == 1
        assert t["picks_playable_unpriceable"] == 1
        assert t["picks_playable_unknown_oracle"] == 0
        assert rep["picks"][0]["line"] == 4.5

    def test_pick_non_giocabile_non_conta(self, db, tmp_path):
        _write_oracle(tmp_path, totals=(3.25,))
        _seed_quotes(db, [3.0, 3.5, 4.0])
        _seed_prediction(db, esito="Under 4.5", status="rejected")
        rep = li.measure(days=7, db=str(db))
        assert rep["totals"]["picks_playable"] == 0
        assert rep["totals"]["picks_playable_unpriceable"] == 0

    def test_pick_con_oracolo_ignoto_dichiarato_a_parte(self, db, tmp_path):
        _seed_quotes(db, [3.0])
        _seed_prediction(db, esito="Over 3.0", status="value")
        rep = li.measure(days=7, db=str(db))
        t = rep["totals"]
        assert t["picks_playable"] == 1
        assert t["picks_playable_unknown_oracle"] == 1
        assert t["picks_playable_unpriceable"] == 0

    def test_pick_con_linea_prezzabile(self, db, tmp_path):
        _write_oracle(tmp_path, totals=(3.0,))
        _seed_quotes(db, [3.0])
        _seed_prediction(db, esito="Over 3.0", status="value")
        rep = li.measure(days=7, db=str(db))
        assert rep["picks_by_status"] == {"priceable": 1}
        assert rep["totals"]["picks_playable_unpriceable"] == 0


# ---------------------------------------------------------------------------
# 4. Fail-safe, report, CLI
# ---------------------------------------------------------------------------

class TestFailSafeEReport:
    def test_db_assente_dichiara_l_errore(self, tmp_path):
        rep = li.measure(days=7, db=str(tmp_path / "nope.db"))
        assert rep["error"]
        assert rep["fixtures"] == []
        assert "misura non disponibile" in li.format_report(rep)[0]

    def test_db_corrotto_non_solleva(self, tmp_path):
        bad = tmp_path / "bad.db"
        bad.write_text("non sono un db")
        rep = li.measure(days=7, db=str(bad))
        assert isinstance(rep, dict)          # mai un'eccezione

    def test_tabelle_mancanti_zeri_non_eccezioni(self, monkeypatch, tmp_path):
        path = tmp_path / "vuoto.db"
        monkeypatch.setattr(tracker, "DB_PATH", path)
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE x (a)")
        conn.commit()
        conn.close()
        rep = li.measure(days=7, db=str(path))
        assert rep["fixtures"] == [] and rep["picks"] == []

    def test_report_dichiara_i_numeri(self, db, tmp_path):
        _write_oracle(tmp_path, totals=(3.25,))
        _seed_quotes(db, [3.0, 3.5])
        _seed_prediction(db, esito="Under 4.5", status="value")
        rep = li.measure(days=7, db=str(db))
        txt = "\n".join(li.format_report(rep))
        assert "INTERSEZIONE LINEE" in txt
        assert "NON prezzabile" in txt
        assert "MLS" in txt

    def test_cli_json(self, db, tmp_path, capsys):
        _write_oracle(tmp_path, totals=(3.0,))
        _seed_quotes(db, [3.0])
        assert li.main(["--days", "7", "--db", str(db), "--json"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["by_status"] == {"priceable": 1}

    def test_cli_db_assente_exit_1(self, tmp_path):
        assert li.main(["--db", str(tmp_path / "nope.db")]) == 1
