"""Test di `flow_measure.py` (misura del flusso scommesse su una finestra).

Tutto OFFLINE: ledger SQLite temporaneo, nessuna rete, nessun ordine e nessuna
scrittura sul ledger reale (il modulo apre in `mode=ro` e un test prova a
scrivere pretendendo il rifiuto). Le date dei dati sono SEMPRE relative a `now`:
un test che scade col calendario arriva nel momento peggiore (lezione 15/09).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import flow_measure as fm
import tracker
import value_filter as vf


def _iso(hours_ago: float = 1.0) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


@pytest.fixture
def ledger(monkeypatch, tmp_path):
    """Ledger temporaneo con lo SCHEMA di produzione (`tracker.init_db`)."""
    db = tmp_path / "quotaverace.db"
    monkeypatch.setattr(tracker, "DB_PATH", db)
    monkeypatch.setattr(fm, "DATA_DIR", tmp_path)
    tracker.init_db()
    return db


def _insert(db: Path, sql: str, rows) -> None:
    conn = sqlite3.connect(db)
    conn.executemany(sql, rows)
    conn.commit()
    conn.close()


def _match(db: Path, mid: str, league: str = "Premier League", hours_ago: float = 5.0):
    _insert(db, "INSERT OR REPLACE INTO matches VALUES (?,?,?,?,?,?,?)",
            [(mid, league, "Home", "Away", _iso(hours_ago), "upcoming", _iso())])


def _prediction(db: Path, mid: str, *, status: str, esito: str = "1",
                mercato: str = "1X2", quota: float = 1.65, prob: float = 0.62,
                ev: float = 0.05, market_prob: float = 0.60,
                league: str = "Premier League", hours_ago: float = 1.0,
                closed: bool = False):
    _insert(db,
            "INSERT INTO predictions (match_id, mercato, esito, quota, prob, ev, "
            "market_prob, market_edge, status, esito_finale, league, created_at, "
            "settled_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,NULL)",
            [(mid, mercato, esito, quota, prob, ev, market_prob, prob - market_prob,
              status, ("won" if closed else None), league, _iso(hours_ago))])


# ---------------------------------------------------------------------------
# Sola lettura / offline
# ---------------------------------------------------------------------------

class TestSolaLettura:
    def test_nessuna_scrittura_nel_sorgente(self):
        src = Path(fm.__file__).read_text(encoding="utf-8").upper()
        for kw in ("INSERT INTO", "UPDATE ", "DELETE FROM", "DROP TABLE"):
            assert kw not in src, f"il misuratore non deve scrivere: {kw}"

    def test_nessuna_rete_nel_sorgente(self):
        src = Path(fm.__file__).read_text(encoding="utf-8")
        for mod in ("import requests", "odds_api", "urllib", "httpx",
                    "sx_signals", "execution_engine"):
            assert mod not in src, f"la misura e' offline: {mod}"

    def test_connessione_di_sola_lettura(self, ledger):
        conn = sqlite3.connect(f"file:{ledger}?mode=ro", uri=True)
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("UPDATE predictions SET quota = 99")
        conn.close()
        # e il modulo legge davvero in mode=ro
        assert fm.measure(24, path=ledger)["readonly"] is True


# ---------------------------------------------------------------------------
# Finestra e parsing delle date
# ---------------------------------------------------------------------------

class TestFinestra:
    def test_formati_di_data_reali(self):
        """'T', 'Z', offset e spazio devono dare lo stesso istante UTC."""
        naive = fm._parse_ts("2026-09-24 10:00:00")
        zulu = fm._parse_ts("2026-09-24T10:00:00Z")
        off = fm._parse_ts("2026-09-24T12:00:00+02:00")
        assert naive == zulu == off
        assert fm._parse_ts("non-una-data") is None
        assert fm._parse_ts(None) is None

    def test_riga_vecchia_esclusa_dalla_finestra(self, ledger):
        _match(ledger, "m1")
        _prediction(ledger, "m1", status="value", hours_ago=1)      # dentro
        _prediction(ledger, "m1", status="value", esito="X", hours_ago=48)  # fuori
        sig = fm.measure(24, path=ledger)["signals"]
        assert sig["rows"] == 1

    def test_ora_di_default_dall_env(self, monkeypatch):
        monkeypatch.setenv("FLOW_WINDOW_HOURS", "6")
        import importlib
        importlib.reload(fm)
        assert fm.DEFAULT_HOURS == 6.0
        importlib.reload(fm)                                   # ripristina


# ---------------------------------------------------------------------------
# Attribuzione degli scarti (il gate VERO, non una copia)
# ---------------------------------------------------------------------------

class TestAttribuzione:
    def _row(self, **kw):
        base = {"mercato": "1X2", "prob": 0.62, "quota": 1.65, "ev": 0.05,
                "market_prob": 0.60, "league": "Premier League"}
        base.update(kw)
        return base

    def test_lega_vietata(self):
        code, _ = fm.attribute_reject(self._row(league="Serie A"))
        assert code == "league_blocked"

    # Soglie DERIVATE dalle costanti (16/10/2026): dopo l'allargamento della
    # fascia del 14/10 (odds 1.20-2.00, edge 1pp) i valori letterali 1.20 e
    # 0.61/0.60 finivano DENTRO i gate, il gate odierno approvava la riga e
    # l'attribuzione diventava `gate_non_riconciliato`: due test rossi a HEAD
    # per una soglia cambiata, non per un difetto. Derivandoli, il test misura
    # cio' che vuole misurare a qualunque soglia.
    def test_quota_fuori_fascia(self):
        assert fm.attribute_reject(
            self._row(quota=vf.ODDS_MAX + 0.10))[0] == "odds_max"
        assert fm.attribute_reject(
            self._row(quota=vf.ODDS_MIN - 0.10))[0] == "odds_min"

    def test_ev_basso(self):
        code, _ = fm.attribute_reject(self._row(ev=vf.EV_MIN / 2.0))
        assert code == "ev_low"

    def test_edge_basso(self):
        # Edge sotto la soglia di lega ma EV sopra la sua: la riga deve cadere
        # sull'EDGE, non sull'EV (l'ordine delle verifiche e' parte del test).
        sotto = vf.MARKET_EDGE_MIN / 2.0
        code, _ = fm.attribute_reject(self._row(prob=0.60 + sotto,
                                                market_prob=0.60,
                                                ev=vf.EV_MIN * 2))
        assert code == "edge_low"

    def test_non_favorito(self):
        code, _ = fm.attribute_reject(self._row(market_prob=0.45))
        assert code == "not_favourite"

    def test_gate_odierno_approva_ma_la_riga_e_scartata(self):
        """Un rifiuto arrivato fuori da `is_sane` (favourite gate, libro, feed)
        non va attribuito a un motivo inventato: e' dichiarato."""
        code, reason = fm.attribute_reject(self._row())
        assert code == "gate_non_riconciliato" and "approva" in reason

    def test_riga_non_interpretabile_non_solleva(self):
        code, reason = fm.attribute_reject({"mercato": "1X2", "prob": "abc"})
        assert code == "altro" and "non interpretabile" in reason

    def test_usa_is_sane_di_produzione(self):
        """Il motivo di scarto si ricalcola col gate di produzione, mai con
        una copia delle soglie."""
        src = Path(fm.__file__).read_text(encoding="utf-8")
        assert "from value_filter import" in src
        for name in ("is_sane", "PLAYABLE_TIERS", "league_tier",
                     "canonical_league"):
            assert name in src


# ---------------------------------------------------------------------------
# Stadi del funnel
# ---------------------------------------------------------------------------

class TestStadi:
    def test_segnali_per_stato_e_giocabili(self, ledger):
        _match(ledger, "m1")
        _prediction(ledger, "m1", status="value")
        _prediction(ledger, "m1", status="rejected", esito="X", ev=0.0)
        _prediction(ledger, "m1", status="rejected", esito="2", ev=0.0,
                    league="Serie A")
        sig = fm.measure(24, path=ledger)["signals"]
        assert sig["rows"] == 3
        assert sig["playable"] == 1
        assert sig["by_status"] == {"value": 1, "rejected": 2}
        assert sig["rejects"]["rows"] == 2
        # una scartata per EV e una per il gate leghe
        assert sig["rejects"]["by_reason"] == {"ev_low": 1, "league_blocked": 1}
        assert sig["playable_in_allowed_leagues"] == 1

    def test_playable_in_lega_vietata_non_conta(self, ledger):
        _match(ledger, "m1", league="Serie A")
        _prediction(ledger, "m1", status="value", league="Serie A")
        sig = fm.measure(24, path=ledger)["signals"]
        assert sig["playable"] == 1 and sig["playable_in_allowed_leagues"] == 0

    def test_lega_ignota_non_e_un_divieto(self, ledger):
        """Lega vuota = non attribuibile (riga `matches` potata / pre-22/09):
        va in un bucket `unknown`, MAI contata come lega vietata - altrimenti
        la misura produce un falso allarme (verificato sul ledger vero il
        24/09: 13 giocabili con lega persa dal pruning)."""
        _match(ledger, "m1", league="")
        _insert(ledger, "INSERT INTO match_analysis (match_id, status, timestamp, "
                        "best_ev) VALUES (?,?,?,?)",
                [("m1", "value", _iso(1), 0.06)])
        _prediction(ledger, "m1", status="value", league="")
        _prediction(ledger, "m1", status="rejected", esito="X", ev=0.0, league="")
        data = fm.measure(24, path=ledger)
        sig = data["signals"]
        assert sig["playable"] == 1
        assert sig["playable_in_allowed_leagues"] == 0
        assert sig["playable_unknown_league"] == 1
        assert sig["unknown_league_rows"] == 2
        assert data["analysis"]["by_tier"] == {"unknown": 1}
        assert "senza lega attribuibile" in fm.format_report(data)
        assert fm._league_state("") == "unknown"
        assert fm._league_state("Serie A") == "blocked"
        assert fm._league_state("Bundesliga") == "core"

    def test_analisi_e_ordini_e_catena(self, ledger):
        _match(ledger, "m1", league="Bundesliga")
        _insert(ledger, "INSERT INTO match_analysis (match_id, status, timestamp, "
                        "best_ev) VALUES (?,?,?,?)",
                [("m1", "value", _iso(1), 0.06)])
        _insert(ledger, "INSERT INTO bets (match_id, mercato, esito, price, stake, "
                        "mode, status, created_at) VALUES (?,?,?,?,?,?,?,?)",
                [("m1", "1X2", "1", 1.65, 1.0, "live", "FULLY_FILLED", _iso(1)),
                 ("m1", "1X2", "X", 3.0, 1.0, "sim", "FULLY_FILLED", _iso(30))])
        _insert(ledger, "INSERT INTO decisions (record_id, verdict, status, reason, "
                        "mode, created_at) VALUES (?,?,?,?,?,?)",
                [("r1", "reject", "rejected", "LEAGUE_NOT_ALLOWED", "live", _iso(1)),
                 ("r2", "approve", "validated", "", "live", _iso(1))])
        data = fm.measure(24, path=ledger)
        assert data["analysis"]["matches"] == 1
        assert data["analysis"]["by_tier"] == {"core": 1}
        assert data["orders"]["rows"] == 1                    # la sim e' fuori finestra
        assert data["orders"]["by_mode"] == {"live": 1}
        assert data["orders"]["staked"] == 1.0
        assert data["chain"]["by_verdict"] == {"reject": 1, "approve": 1}
        assert data["chain"]["by_reason"] == {"LEAGUE_NOT_ALLOWED": 1}

    def test_quote_per_mercato(self, ledger):
        _insert(ledger, "INSERT INTO market_quotes (fixture_id, market_type, "
                        "line_key, selection, origin, updated_at) "
                        "VALUES (?,?,?,?,?,?)",
                [("f1", "OU", "2.5", "over", "native", _iso(1)),
                 ("f1", "AH", "-0.5", "1", "native", _iso(1)),
                 ("f1", "OU", "3.5", "over", "native", _iso(40))])
        q = fm.measure(24, path=ledger)["quotes"]
        assert q["rows"] == 2 and q["by_market"] == {"OU": 1, "AH": 1}

    def test_liquidita_iniettabile(self, ledger):
        data = fm.measure(24, path=ledger, liquidity_events=[
            {"kind": "scan", "reason": "depth_totale"},
            {"kind": "order", "reason": "depth_vs_stake"},
        ])
        assert data["liquidity"]["events"] == 2
        assert data["liquidity"]["by_kind"] == {"scan": 1, "order": 1}


# ---------------------------------------------------------------------------
# Verdetto: dove si ferma il funnel
# ---------------------------------------------------------------------------

class TestVerdetto:
    def _v(self, analysis, signals, orders):
        return fm._verdict(analysis, signals, orders)

    def test_nessun_segnale_generato(self):
        txt = self._v({"matches": 3}, {"rows": 0, "playable": 0,
                                       "rejects": {"by_reason": {}}}, {"rows": 0})
        assert "NESSUN SEGNALE" in txt and "rotazione" in txt

    def test_segnali_ma_tutti_scartati(self):
        txt = self._v({"matches": 5},
                      {"rows": 9, "playable": 0,
                       "rejects": {"by_reason": {"league_blocked": 9}}},
                      {"rows": 0})
        assert "TUTTI scartati" in txt and "league_blocked" in txt

    def test_giocabili_ma_zero_ordini(self):
        txt = self._v({"matches": 5},
                      {"rows": 2, "playable": 2, "rejects": {"by_reason": {}}},
                      {"rows": 0})
        assert "0 ordini" in txt and "a valle dei gate" in txt

    def test_ordini_presenti(self):
        txt = self._v({"matches": 5}, {"rows": 2, "playable": 2,
                                       "rejects": {"by_reason": {}}},
                      {"rows": 3, "staked": 3.0})
        assert "flusso ATTIVO" in txt and "3 ordini" in txt


# ---------------------------------------------------------------------------
# Fail-safe
# ---------------------------------------------------------------------------

class TestFailSafe:
    def test_db_assente_non_solleva(self, tmp_path, monkeypatch):
        monkeypatch.setattr(fm, "DATA_DIR", tmp_path)
        data = fm.measure(24, path=tmp_path / "assente.db")
        # `mode=ro` su un file inesistente: la misura ritorna o dati vuoti o
        # un errore dichiarato, MAI un'eccezione verso il chiamante.
        assert data["readonly"] is True

    def test_file_corrotto_non_solleva(self, tmp_path):
        bad = tmp_path / "corrotto.db"
        bad.write_bytes(b"non sono un database sqlite")
        data = fm.measure(24, path=bad)
        assert "error" in data or data["signals"]["rows"] == 0

    def test_tabella_mancante_non_solleva(self, tmp_path):
        db = tmp_path / "vuoto.db"
        sqlite3.connect(db).close()               # nessuna tabella
        data = fm.measure(24, path=db)
        assert data["analysis"]["rows"] == 0 and data["signals"]["rows"] == 0
        assert data["verdict"]

    def test_format_report_su_errore(self):
        assert "non disponibile" in fm.format_report({"error": "boom"})
