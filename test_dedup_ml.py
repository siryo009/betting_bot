"""Deduplicazione dataset ML e vincoli UNIQUE sui DB legacy.

Regressione 02/09: l'audit ha trovato una riga duplicata (hash 36aa024f...)
nel dataset di addestramento. Due livelli di difesa, entrambi testati:
1. DB: UNIQUE(match_id, mercato, esito) su predictions / UNIQUE(match_id,
   esito) su bets, applicati con migrazione ANCHE ai DB creati prima che i
   vincoli esistessero (CREATE TABLE IF NOT EXISTS non migra).
2. Dataset: dedup NORMALIZZATO in ml_dataset (stessa scommessa descritta
   da predictions e bets, o con esiti rappresentati diversamente, = 1 riga).
"""
import sqlite3
import tempfile
from pathlib import Path

import pytest

import tracker
import ml_dataset


@pytest.fixture()
def temp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        monkeypatch.setattr(tracker, "DB_PATH", db_path)
        tracker.init_db()
        yield db_path


def _seed_signal(mid="m1", esito="Over 2.5", esito_finale=None):
    from datetime import datetime, timedelta, timezone
    start = (datetime.now(timezone.utc) - timedelta(hours=3)) \
        .isoformat().replace("+00:00", "Z")
    tracker.save_match(mid, "Serie A", "Inter", "Empoli", start)
    tracker.save_analysis(mid, 2.0, 1.0, 0.5, 0.3, 0.2, 0.6, 0.05,
                          "Inter", 1.90, "Book", "value")
    tracker.save_prediction(mid, "OU", esito, 1.90, 0.55, 0.05)
    if esito_finale:
        tracker.save_result(mid, "Serie A", "Inter", "Empoli", 2, 1,
                            datetime.now().isoformat())
        tracker.settle_predictions()


# --- Dedup a livello dataset --------------------------------------------------


def test_dedup_ou_over_25_vs_over(temp_db):
    """'Over 2.5' (ledger) e 'over' (puntata auto) = STESSA scommessa."""
    _seed_signal("m1", "Over 2.5", esito_finale=True)
    tracker.save_bet("m1", "OU", "over", price=1.90, stake=5.0, mode="sim")
    # salda la bet: la partita ha gia' il risultato
    tracker.settle_bets()
    rows = ml_dataset.build_training_rows()
    assert len(rows) == 1
    # mantiene la riga col verdetto definitivo
    assert rows[0]["esito_finale"] in ("won", "lost")


def test_dedup_1x2_nome_squadra_vs_chiave(temp_db):
    """'Inter' (ledger) e '1' (puntata auto) sono lo stesso esito 1X2."""
    _seed_signal("m1", esito_finale=True)
    tracker.save_prediction("m1", "1X2", "Inter", 1.90, 0.55, 0.05)
    tracker.settle_predictions()
    tracker.save_bet("m1", "1X2", "1", price=1.95, stake=5.0, mode="sim")
    tracker.settle_bets()
    rows = ml_dataset.build_training_rows()
    chiavi = [(r["mercato"], r["esito"]) for r in rows]
    assert len(rows) == 2          # OU + 1X2 (non 3: la 1X2 e' una sola)
    assert chiavi.count(("1X2", "Inter")) + chiavi.count(("1X2", "1")) == 1


def test_dedup_mantiene_il_verdetto_definitivo():
    rows = [
        {"match_id": "m", "mercato": "OU", "esito": "over", "esito_finale": "",
         "settled_at": "", "home": "", "away": ""},
        {"match_id": "m", "mercato": "OU", "esito": "Over 2.5", "esito_finale": "won",
         "settled_at": "2026-09-02T10:00:00", "home": "", "away": ""},
    ]
    out = ml_dataset.dedupe_training_rows(rows)
    assert len(out) == 1
    assert out[0]["esito_finale"] == "won"


def test_dedup_idempotente():
    rows = [
        {"match_id": "m", "mercato": "OU", "esito": "over", "esito_finale": "won",
         "settled_at": "2026-09-02T10:00:00", "home": "", "away": ""},
        {"match_id": "m", "mercato": "OU", "esito": "Over 2.5", "esito_finale": "won",
         "settled_at": "2026-09-02T10:00:00", "home": "", "away": ""},
    ]
    once = ml_dataset.dedupe_training_rows(rows)
    assert ml_dataset.dedupe_training_rows(once) == once


def test_dedup_mercati_diversi_non_toccati():
    rows = [
        {"match_id": "m", "mercato": "1X2", "esito": "1", "esito_finale": "won",
         "settled_at": "", "home": "", "away": ""},
        {"match_id": "m", "mercato": "OU", "esito": "over", "esito_finale": "won",
         "settled_at": "", "home": "", "away": ""},
        {"match_id": "m2", "mercato": "OU", "esito": "over", "esito_finale": "lost",
         "settled_at": "", "home": "", "away": ""},
    ]
    assert len(ml_dataset.dedupe_training_rows(rows)) == 3


# --- Vincolo UNIQUE su DB legacy (migrazione) ---------------------------------


def _legacy_db(path: Path) -> None:
    """DB con schema VECCHIO: predictions SENZA UNIQUE (come i DB nati prima)."""
    conn = sqlite3.connect(path)
    conn.execute('''CREATE TABLE predictions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        match_id TEXT, mercato TEXT, esito TEXT,
        quota REAL, prob REAL, ev REAL,
        market_prob REAL, market_edge REAL,
        status TEXT, esito_finale TEXT, profit REAL,
        created_at TEXT, settled_at TEXT)''')
    conn.execute('''CREATE TABLE bets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        match_id TEXT, mercato TEXT, esito TEXT,
        market_id TEXT, selection_id INTEGER,
        price REAL, stake REAL, mode TEXT, status TEXT, bet_id TEXT,
        esito_finale TEXT, profit REAL,
        created_at TEXT, settled_at TEXT)''')
    # Duplicato REALE del bug: stesso match+mercato+esito, una chiusa e una aperta
    conn.execute("INSERT INTO predictions (match_id, mercato, esito, quota, esito_finale, profit) "
                 "VALUES ('m1','OU','Over 2.5',1.90,'won',4.7)")
    conn.execute("INSERT INTO predictions (match_id, mercato, esito, quota, esito_finale, profit) "
                 "VALUES ('m1','OU','Over 2.5',1.85,NULL,NULL)")
    conn.commit()
    conn.close()


def test_migrazione_unique_db_legacy(monkeypatch, tmp_path):
    db = tmp_path / "legacy.db"
    _legacy_db(db)
    monkeypatch.setattr(tracker, "DB_PATH", db)
    tracker.init_db()          # _get_conn esegue la migrazione

    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT esito, esito_finale FROM predictions").fetchall()
    assert len(rows) == 1
    assert rows[0][1] == "won"          # conserva la riga col verdetto
    # il vincolo ora e' ATTIVO: lo stesso insert rifiutato
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO predictions (match_id, mercato, esito) "
                     "VALUES ('m1','OU','Over 2.5')")
    conn.close()
    # nessun backup residuo
    names = [r[0] for r in sqlite3.connect(db).execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    assert "predictions_old" not in names and "bets_old" not in names


def test_migrazione_idempotente(monkeypatch, tmp_path):
    db = tmp_path / "legacy.db"
    _legacy_db(db)
    monkeypatch.setattr(tracker, "DB_PATH", db)
    tracker.init_db()
    tracker.init_db()          # secondo giro: nessuna migrazione, nessun errore
    tracker.init_db()


def test_migrazione_recupera_backup_interrotto(monkeypatch, tmp_path):
    """Migrazione interrotta a meta' (crash): _old con righe non copiate ->
    il prossimo avvio le recupera invece di perderle."""
    db = tmp_path / "legacy.db"
    _legacy_db(db)
    # simula lo stato "a meta'": tabella rinominata, righe in _old
    conn = sqlite3.connect(db)
    conn.execute("DROP TABLE predictions")   # la nuova non e' mai stata creata
    conn.execute("ALTER TABLE bets RENAME TO bets_old")
    conn.execute("INSERT INTO bets_old (match_id, mercato, esito, price, stake) "
                 "VALUES ('mX','1X2','1',2.0,5.0)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(tracker, "DB_PATH", db)
    tracker.init_db()

    conn = sqlite3.connect(db)
    bets = conn.execute("SELECT match_id FROM bets").fetchall()
    assert ("mX",) in bets           # riga recuperata, non persa
    names = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    assert "bets_old" not in names
    conn.close()


def test_recupero_con_colonne_nuove_in_piu(monkeypatch, tmp_path):
    """REGRESSIONE 16/10/2026: il backup ha MENO colonne della tabella nuova.

    Il recupero pretendeva elenchi di colonne IDENTICI: bastava che la tabella
    nuova avesse una colonna in piu' (qui le quattro di identita' evento
    aggiunte a `bets`: `home`/`away`/`league`/`commence`) perche' il recupero
    saltasse IN SILENZIO e le righe restassero in `_old` per sempre. E' il
    caso che si presenta a OGNI cambio di schema con un backup pendente.
    """
    db = tmp_path / "legacy.db"
    _legacy_db(db)
    conn = sqlite3.connect(db)
    conn.execute("DROP TABLE predictions")
    conn.execute("ALTER TABLE bets RENAME TO bets_old")
    conn.execute("INSERT INTO bets_old (match_id, mercato, esito, price, stake) "
                 "VALUES ('mY','1X2','1',2.0,5.0)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(tracker, "DB_PATH", db)
    tracker.init_db()

    conn = sqlite3.connect(db)
    bets = conn.execute("SELECT match_id, home, league FROM bets").fetchall()
    assert ("mY", None, None) in bets          # riga recuperata, colonne nuove vuote
    names = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    assert "bets_old" not in names
    conn.close()


def test_backup_con_colonna_sparita_non_si_distrugge(monkeypatch, tmp_path,
                                                     caplog):
    """Controprova della stessa regola: se il backup ha una colonna che la
    tabella nuova NON ha piu', il backup si CONSERVA (nessun dato toccato) e il
    motivo e' dichiarato. Prima l'alternativa era distruggere in silenzio."""
    db = tmp_path / "legacy.db"
    _legacy_db(db)
    conn = sqlite3.connect(db)
    conn.execute("ALTER TABLE bets RENAME TO bets_old")
    conn.execute("ALTER TABLE bets_old ADD COLUMN colonna_morta TEXT")
    conn.execute("INSERT INTO bets_old (match_id, mercato, esito, price, stake, "
                 "colonna_morta) VALUES ('mZ','1X2','1',2.0,5.0,'x')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(tracker, "DB_PATH", db)
    with caplog.at_level("WARNING"):
        tracker.init_db()

    conn = sqlite3.connect(db)
    names = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    assert "bets_old" in names                 # backup CONSERVATO
    assert conn.execute("SELECT match_id FROM bets_old").fetchall() == [("mZ",)]
    conn.close()
    assert any("colonne assenti" in r.message for r in caplog.records)


def test_save_bet_nessun_duplicato_su_rerun(temp_db):
    """Doppio inserimento identico: una sola riga (ON CONFLICT aggiorna)."""
    from datetime import datetime, timedelta, timezone
    start = (datetime.now(timezone.utc) + timedelta(hours=3)) \
        .isoformat().replace("+00:00", "Z")
    tracker.save_match("m1", "Serie A", "Inter", "Empoli", start)
    tracker.save_analysis("m1", 2.0, 1.0, 0.5, 0.3, 0.2, 0.6, 0.05,
                          "Inter", 1.90, "Book", "value")
    tracker.save_bet("m1", "OU", "over", price=1.90, stake=5.0, mode="sim")
    tracker.save_bet("m1", "OU", "over", price=1.92, stake=5.0, mode="sim")
    assert len(tracker.get_bets()) == 1
