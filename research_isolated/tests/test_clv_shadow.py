"""Test del registro prospettico (`clv_shadow`) — OFFLINE, ledger temporaneo.

Il registro e' l'unico pezzo che puo' rendere il CLV una verifica invece di
un'auto-conferma. Cosa deve restare vero:

* registra OGNI segnale 1X2 aperto, anche quelli che la corsia non avrebbe
  giocato (altrimenti la selezione del campione non e' controllabile);
* non registra partite gia' iniziate (la loro chiusura appartiene al passato:
  sarebbe riusare il vecchio holdout come se fosse nuovo);
* dedup per `signal_id` (il job gira ogni minuto: senza, `n` esplode);
* non scrive nulla sul ledger e non piazza ordini;
* la finestra si dichiara NON pronta finche' non copre 7 giorni CON campione.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from research_isolated.lab import clv_shadow as shadow
from research_isolated.lab import clv_validation as cv


DDL = """
CREATE TABLE matches (id TEXT PRIMARY KEY, home_team TEXT, away_team TEXT,
                      commence_time TEXT, league TEXT);
CREATE TABLE predictions (id INTEGER PRIMARY KEY, match_id TEXT, mercato TEXT,
                          esito TEXT, status TEXT, prob REAL, quota REAL,
                          market_prob REAL, ev REAL, market_edge REAL,
                          league TEXT, created_at TEXT, esito_finale TEXT);
CREATE TABLE clv_history (match_id TEXT, esito TEXT, signal_quota REAL,
                          closing_quota REAL, pinnacle_quota REAL,
                          closing_odds REAL, closing_source TEXT,
                          betfair_odds REAL, updated_at TEXT);
"""

#: Il confine che rende un segnale "prospettico" NON e' una data scelta qui:
#: e' quella del protocollo congelato. I test la DERIVANO, cosi' non scadono e
#: non possono fissare una finestra diversa da quella dichiarata.
FREEZE = cv.parse_ts(cv.load_prereg()["frozen_at"])
#: Dieci giorni dopo il congelamento: cosi' i test della finestra possono
#: usare date "vecchie di 3/8 giorni" che restano COMUNQUE prospettiche.
NOW = FREEZE + timedelta(days=10)         # istante della registrazione
BORN = FREEZE + timedelta(hours=1)        # segnale nato DOPO il congelamento


def iso(dt: datetime) -> str:
    return dt.isoformat()


@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "snap.db"
    conn = sqlite3.connect(p)
    conn.executescript(DDL)
    rows = [
        # (id, match_id, mercato, esito, status, quota, kickoff-offset)
        (1, "m1", "1X2", "1", "strong_value", 1.70, +30),   # selezionato
        (2, "m2", "1X2", "2", "rejected", 1.55, +12),       # NON selezionato
        (3, "m3", "1X2", "1", "value", 1.80, -5),           # gia' iniziata
        (4, "m4", "OU", "Over 2.5", "value", 1.90, +20),    # mercato fuori universo
    ]
    for pid, match_id, market, esito, status, quota, off in rows:
        conn.execute("INSERT INTO matches VALUES (?,?,?,?,?)",
                     (match_id, f"Home {match_id}", f"Away {match_id}",
                      iso(NOW + timedelta(hours=off)), "Test League"))
        conn.execute(
            "INSERT INTO predictions (id, match_id, mercato, esito, status, prob,"
            " quota, market_prob, ev, market_edge, league, created_at,"
            " esito_finale) VALUES (?,?,?,?,?,0.6,?,0.55,0.05,0.02,'Test"
            " League',?,NULL)",
            (pid, match_id, market, esito, status, quota, iso(BORN)))
    conn.commit()
    conn.close()
    return p


def connect(path):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Registrazione
# ---------------------------------------------------------------------------

class TestRecord:
    def test_registra_solo_1x2_non_iniziate(self, db, tmp_path):
        log = tmp_path / "signals.jsonl"
        conn = connect(db)
        try:
            out = shadow.record(conn=conn, path=log, now=NOW)
        finally:
            conn.close()
        assert out["added"] == 2            # m1 (selezionato) + m2 (scartato)
        assert out["skipped_past"] == 1     # m3 era gia' iniziata
        assert out["errors"] == []
        records = shadow.load_log(log)
        assert {r["match_id"] for r in records} == {"m1", "m2"}

    def test_registra_anche_i_non_selezionati(self, db, tmp_path):
        log = tmp_path / "signals.jsonl"
        conn = connect(db)
        try:
            shadow.record(conn=conn, path=log, now=NOW)
        finally:
            conn.close()
        records = {r["match_id"]: r for r in shadow.load_log(log)}
        assert records["m1"]["selected"] is True
        assert records["m2"]["selected"] is False
        assert records["m2"]["status"] == "rejected"

    def test_registra_tutto_il_segnale(self, db, tmp_path):
        log = tmp_path / "signals.jsonl"
        conn = connect(db)
        try:
            shadow.record(conn=conn, path=log, now=NOW)
        finally:
            conn.close()
        rec = {r["match_id"]: r for r in shadow.load_log(log)}["m1"]
        for key in ("ts", "day", "signal_id", "market", "selection", "quota",
                    "prob", "market_prob", "ev", "edge", "league", "kickoff",
                    "created_at", "hypothesis_id"):
            assert key in rec, key
        assert rec["quota"] == pytest.approx(1.70)
        assert rec["hypothesis_id"] == shadow.hypothesis()["id"]

    def test_dedup_per_signal_id(self, db, tmp_path):
        log = tmp_path / "signals.jsonl"
        conn = connect(db)
        try:
            first = shadow.record(conn=conn, path=log, now=NOW)
            second = shadow.record(conn=conn, path=log,
                                   now=NOW + timedelta(minutes=1))
        finally:
            conn.close()
        assert first["added"] == 2
        assert second["added"] == 0
        assert second["skipped"] == 2
        assert len(shadow.load_log(log)) == 2

    def test_il_ts_e_la_prima_occasione(self, db, tmp_path):
        """Il prezzo registrato e' quello della PRIMA occorrenza."""
        log = tmp_path / "signals.jsonl"
        conn = connect(db)
        try:
            shadow.record(conn=conn, path=log, now=NOW)
            shadow.record(conn=conn, path=log, now=NOW + timedelta(hours=6))
        finally:
            conn.close()
        records = shadow.load_log(log)
        assert all(r["ts"] == iso(NOW) for r in records)

    def test_ledger_non_leggibile_non_solleva(self, tmp_path):
        out = shadow.record(db=tmp_path / "assente.db",
                            path=tmp_path / "s.jsonl", now=NOW)
        assert out["added"] == 0
        assert out["errors"]

    def test_righe_corrotte_si_saltano(self, db, tmp_path):
        log = tmp_path / "signals.jsonl"
        log.write_text("{non json}\n\n" + json.dumps(
            {"signal_id": "x|1", "ts": iso(NOW), "day": "2026-10-20"}) + "\n")
        records = shadow.load_log(log)
        assert len(records) == 1


# ---------------------------------------------------------------------------
# Finestra
# ---------------------------------------------------------------------------

class TestWindow:
    def test_registro_vuoto_non_e_pronto(self, tmp_path):
        block = shadow.status(tmp_path / "nope.jsonl", now=NOW)
        assert block["ready"] is False
        assert block["days"] == 0
        assert "NON iniziata" in block["note"]

    def test_finestra_di_meno_di_7_giorni_non_e_pronta(self, tmp_path):
        log = tmp_path / "signals.jsonl"
        start = NOW - timedelta(days=3)
        _write_log(log, [{"signal_id": f"s{i}|1", "ts": iso(start),
                          "day": start.date().isoformat(), "selected": True,
                          "created_at": iso(start)} for i in range(40)])
        block = shadow.status(log, now=NOW)
        assert block["days"] == 3
        assert block["signals"] == 40
        assert block["ready"] is False
        assert "3/7" in block["note"]

    def test_7_giorni_con_campione(self, tmp_path):
        log = tmp_path / "signals.jsonl"
        start = NOW - timedelta(days=8)
        _write_log(log, [{"signal_id": f"s{i}|1", "ts": iso(start),
                          "day": start.date().isoformat(), "selected": True,
                          "created_at": iso(start)} for i in range(cv.MIN_SAMPLES)])
        block = shadow.status(log, now=NOW)
        assert block["ready"] is True
        assert block["days"] == 8

    def test_7_giorni_senza_campione_non_bastano(self, tmp_path):
        log = tmp_path / "signals.jsonl"
        start = NOW - timedelta(days=8)
        with log.open("w") as fh:
            fh.write(json.dumps({"signal_id": "solo|1", "ts": iso(start),
                                 "day": start.date().isoformat()}) + "\n")
        assert shadow.status(log, now=NOW)["ready"] is False


# ---------------------------------------------------------------------------
# Analisi
# ---------------------------------------------------------------------------

def _write_log(path, rows):
    """Scrive un registro di test. `created_at` di default = DOPO il freeze.

    Da quando il confine del congelamento filtra le righe, un log di test senza
    `created_at` sarebbe (correttamente) escluso: lo si aggiunge qui, cosi' i
    test misurano cio' che vogliono misurare.
    """
    with path.open("w") as fh:
        for row in rows:
            row = {"created_at": iso(BORN), **row}
            fh.write(json.dumps(row) + "\n")


class TestAnalyse:
    def test_solo_chiusure_successive_al_segnale(self, tmp_path):
        """Una chiusura anteriore al segnale e' storia, non osservazione."""
        db = tmp_path / "snap.db"
        conn = sqlite3.connect(db)
        conn.executescript(DDL)
        conn.execute("INSERT INTO clv_history VALUES ('m1','1',1.70,NULL,NULL,"
                     "1.65,'pinnacle_t0',NULL,?)",
                     (iso(NOW - timedelta(hours=1)),))     # PRIMA del segnale
        conn.commit()
        conn.close()
        log = tmp_path / "signals.jsonl"
        _write_log(log, [{"signal_id": "m1|1", "match_id": "m1",
                          "selection": "1", "quota": 1.70, "selected": True,
                          "status": "value", "ts": iso(NOW),
                          "day": NOW.date().isoformat()}])
        out = shadow.analyse(db=db, path=log)
        assert out["n"] == 0
        assert out["not_prospective"] == 1

    def test_misura_selezionati_e_non(self, tmp_path):
        db = tmp_path / "snap.db"
        conn = sqlite3.connect(db)
        conn.executescript(DDL)
        conn.execute("INSERT INTO clv_history VALUES ('m1','1',1.70,NULL,NULL,"
                     "1.65,'pinnacle_t0',NULL,?)",
                     (iso(NOW + timedelta(hours=5)),))
        conn.execute("INSERT INTO clv_history VALUES ('m2','2',1.55,NULL,NULL,"
                     "1.60,'pinnacle_t0',NULL,?)",
                     (iso(NOW + timedelta(hours=5)),))
        conn.commit()
        conn.close()
        log = tmp_path / "signals.jsonl"
        _write_log(log, [
            {"signal_id": "m1|1", "match_id": "m1", "selection": "1",
             "quota": 1.70, "selected": True, "status": "value", "ts": iso(NOW),
             "day": NOW.date().isoformat()},
            {"signal_id": "m2|2", "match_id": "m2", "selection": "2",
             "quota": 1.55, "selected": False, "status": "rejected",
             "ts": iso(NOW), "day": NOW.date().isoformat()},
        ])
        out = shadow.analyse(db=db, path=log)
        assert out["n"] == 2
        assert out["selected"]["n"] == 1
        assert out["selected"]["mean"] > 0        # 1.70/1.65 - 1
        assert out["unselected"]["mean"] < 0      # 1.55/1.60 - 1
        assert out["by_status"]["rejected"]["n"] == 1

    def test_eco_esclusa(self, tmp_path):
        db = tmp_path / "snap.db"
        conn = sqlite3.connect(db)
        conn.executescript(DDL)
        conn.execute("INSERT INTO clv_history VALUES ('m1','1',1.70,NULL,NULL,"
                     "1.70,'pinnacle_t0',NULL,?)",
                     (iso(NOW + timedelta(hours=5)),))
        conn.commit()
        conn.close()
        log = tmp_path / "signals.jsonl"
        _write_log(log, [{"signal_id": "m1|1", "match_id": "m1",
                          "selection": "1", "quota": 1.70, "selected": True,
                          "ts": iso(NOW), "day": NOW.date().isoformat()}])
        out = shadow.analyse(db=db, path=log)
        assert out["echo"] == 1
        assert out["n"] == 0

    def test_senza_chiusura_si_conta(self, tmp_path):
        db = tmp_path / "snap.db"
        conn = sqlite3.connect(db)
        conn.executescript(DDL)
        conn.commit()
        conn.close()
        log = tmp_path / "signals.jsonl"
        _write_log(log, [{"signal_id": "m1|1", "match_id": "m1",
                          "selection": "1", "quota": 1.70, "selected": True,
                          "ts": iso(NOW), "day": NOW.date().isoformat()}])
        out = shadow.analyse(db=db, path=log)
        assert out["no_closing"] == 1
        assert out["error"] == "nessun segnale con chiusura misurabile"

    def test_registro_vuoto(self, tmp_path):
        out = shadow.analyse(db=tmp_path / "x.db", path=tmp_path / "none.jsonl")
        assert out["error"] == "registro vuoto: nessuna osservazione prospettica"


# ---------------------------------------------------------------------------
# Confine del CONGELAMENTO: il vecchio campione non e' dato prospettico
# ---------------------------------------------------------------------------

class TestConfineDelCongelamento:
    """Il 20/10 il registro conteneva 45 righe NATEE PRIMA del congelamento.

    Contarle come prospettiche sarebbe riusare l'holdout che ha generato
    l'ipotesi (direttiva: "non riutilizzare il vecchio holdout come se fosse
    nuovo"). Qui si blinda il contrario: solo cio' che nasce DOPO entra.
    """

    def _seed(self, db, created_at):
        conn = sqlite3.connect(db)
        conn.executescript(DDL)
        conn.execute("INSERT INTO matches VALUES ('m9','H','A',?, 'L')",
                     (iso(NOW + timedelta(hours=5)),))
        conn.execute(
            "INSERT INTO predictions (id, match_id, mercato, esito, status, prob,"
            " quota, market_prob, ev, market_edge, league, created_at,"
            " esito_finale) VALUES (9,'m9','1X2','1','value',0.6,1.70,0.55,0.05,"
            " 0.02,'L',?,NULL)", (created_at,))
        conn.commit()
        conn.close()

    def test_un_segnale_nato_prima_del_congelamento_non_entra(self, tmp_path):
        db = tmp_path / "snap.db"
        self._seed(db, iso(FREEZE - timedelta(hours=1)))
        log = tmp_path / "signals.jsonl"
        conn = connect(db)
        try:
            out = shadow.record(conn=conn, path=log, now=NOW)
        finally:
            conn.close()
        assert out["added"] == 0
        assert out["skipped_pre_freeze"] == 1
        assert shadow.load_log(log) == []
        assert "congelamento" in out["note"]

    def test_un_segnale_nato_dopo_entra(self, tmp_path):
        db = tmp_path / "snap.db"
        self._seed(db, iso(FREEZE + timedelta(minutes=1)))
        log = tmp_path / "signals.jsonl"
        conn = connect(db)
        try:
            out = shadow.record(conn=conn, path=log, now=NOW)
        finally:
            conn.close()
        assert out["added"] == 1
        assert out["skipped_pre_freeze"] == 0

    def test_senza_data_di_nascita_non_e_prospettico(self):
        assert shadow.is_prospective({"created_at": None}) is False
        assert shadow.is_prospective({}) is False

    def test_status_esclude_le_righe_pre_congelamento(self, tmp_path):
        log = tmp_path / "signals.jsonl"
        _write_log(log, [
            {"signal_id": "old|1", "ts": iso(FREEZE - timedelta(days=30)),
             "day": "2026-09-20", "created_at": iso(FREEZE - timedelta(days=30))},
            {"signal_id": "new|1", "ts": iso(NOW), "day": NOW.date().isoformat(),
             "created_at": iso(BORN)},
        ])
        block = shadow.status(log, now=NOW)
        assert block["rows"] == 1
        assert block["pre_freeze"] == 1
        assert block["signals"] == 1
        assert block["frozen_at"] == FREEZE.isoformat()

    def test_status_spiega_perche_non_e_iniziata(self, tmp_path):
        log = tmp_path / "signals.jsonl"
        _write_log(log, [{"signal_id": "old|1", "ts": iso(FREEZE),
                          "day": FREEZE.date().isoformat(),
                          "created_at": iso(FREEZE - timedelta(days=2))}])
        block = shadow.status(log, now=NOW)
        assert block["ready"] is False
        assert block["rows"] == 0
        assert "pre-congelamento" in block["note"]

    def test_analyse_non_usa_il_campione_storico(self, tmp_path):
        db = tmp_path / "snap.db"
        conn = sqlite3.connect(db)
        conn.executescript(DDL)
        conn.execute("INSERT INTO clv_history VALUES ('m1','1',1.70,NULL,NULL,"
                     "1.60,'pinnacle_t0',NULL,?)", (iso(NOW + timedelta(hours=1)),))
        conn.commit()
        conn.close()
        log = tmp_path / "signals.jsonl"
        _write_log(log, [{"signal_id": "m1|1", "match_id": "m1",
                          "selection": "1", "quota": 1.70, "selected": True,
                          "status": "value", "ts": iso(FREEZE),
                          "day": FREEZE.date().isoformat(),
                          "created_at": iso(FREEZE - timedelta(days=5))}])
        out = shadow.analyse(db=db, path=log)
        assert out["n"] == 0
        assert out["pre_freeze_excluded"] == 1
        assert "PRIMA del congelamento" in out["error"]


# ---------------------------------------------------------------------------
# Confini
# ---------------------------------------------------------------------------

def test_connessione_usata_e_read_only(db):
    conn = connect(db)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("UPDATE predictions SET status = 'value'")
    finally:
        conn.close()


def test_nessun_ordine_nel_sorgente():
    from pathlib import Path
    src = Path(shadow.__file__).read_text(encoding="utf-8")
    for token in ("place_limit_order", "_live_fill", "save_bet(", "auto_bet"):
        assert token not in src, token


def test_il_registro_vive_fuori_dal_ledger():
    path = shadow.log_path(None)
    assert "research_isolated" in str(path)
    assert "quotaverace.db" not in str(path)


def test_ipotesi_viene_dal_protocollo():
    hyp = shadow.hypothesis()
    assert hyp["id"] == "H-CLV-1"
    assert hyp["orders_allowed"] is False
    assert hyp["prereg_sha256"] == cv.load_prereg()["sha256"]


def test_cli_status_non_scrive(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("QV_CLV_SHADOW_LOG", str(tmp_path / "none.jsonl"))
    assert shadow.main(["status"]) == 0
    assert "finestra prospettica" in capsys.readouterr().out
