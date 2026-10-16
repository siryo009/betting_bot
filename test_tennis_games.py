"""Test dei GAME di tennis nel referto — TUTTI OFFLINE (16/10/2026).

Perche' esiste questo pezzo
---------------------------
Il referto SX-native salva per il tennis il punteggio CANONICO (1,0)/(0,1)
preso dal campo `outcome` dell'exchange: e' la scelta corretta per il VERDETTO
(i game possono pareggiare con un vincitore — Giron-Baez 13-13 con `outcome` 2)
ma buttava via i GAME veri che la stessa risposta porta in
`teamOneScore`/`teamTwoScore`. Senza quelli `tennis_markov.ledger_games_stats`
vedeva solo totali da 1 game, rispondeva `semantics="unknown"` e la
calibrazione di `BASE_HOLD` non poteva partire (misurato: plausible_share
0.417).

Le tre proprieta' che questi test difendono
------------------------------------------
1. il dettaglio e' REGISTRATO, non interpretato: non tocca `score_home`/
   `score_away` ne' il campo `result` (il verdetto resta dei punteggi canonici);
2. non si inventa nulla: un dettaglio mancante resta `NULL`, una partita non
   presente nel referto NON riceve una riga di risultato (`no_row`);
3. il campione usato per la calibrazione e' dichiarato: `ledger_detail` quando
   c'e', `ledger` (punteggi canonici) come ripiego.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

import tracker


@pytest.fixture()
def temp_db(monkeypatch, tmp_path):
    db_path = tmp_path / "games.db"
    monkeypatch.setattr(tracker, "DB_PATH", db_path)
    tracker.init_db()
    yield db_path


def _iso():
    return datetime.now(timezone.utc).isoformat()


def _row(db: Path, match_id: str):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    got = conn.execute("SELECT * FROM match_results WHERE match_id=?",
                       (match_id,)).fetchone()
    conn.close()
    return dict(got) if got else None


class _Prov:
    """Provider SX finto: registra le chiamate e risponde a copione."""

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def _get(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        return self.payload


# ---------------------------------------------------------------------------
# 1. TRACKER — colonne del dettaglio, migrazione, COALESCE
# ---------------------------------------------------------------------------

class TestDettaglioNelReferto:
    def test_colonne_presenti_alla_creazione_della_tabella(self, temp_db):
        """`match_results` nasce lazy (al primo scrittore): la creazione include
        le colonne del dettaglio, quindi il DB nuovo e quello migrato hanno lo
        stesso schema."""
        tracker.save_result("sx-0", "ATP", "A", "B", 1, 0, _iso())
        conn = sqlite3.connect(temp_db)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(match_results)")}
        conn.close()
        for name in tracker.RESULT_DETAIL_FIELDS:
            assert name in cols, name

    def test_save_result_con_dettaglio(self, temp_db):
        tracker.save_result("sx-1", "ATP - Shanghai", "Tien", "Svajda", 1, 0,
                            _iso(), detail=(14, 10, "games", "sx"))
        row = _row(temp_db, "sx-1")
        assert (row["score_home"], row["score_away"], row["result"]) == (1, 0, "1")
        assert (row["detail_home"], row["detail_away"]) == (14, 10)
        assert row["detail_kind"] == "games" and row["detail_source"] == "sx"

    def test_riscrittura_senza_dettaglio_non_cancella_i_game(self, temp_db):
        """Il watchdog ri-salva spesso lo stesso risultato: i game restano."""
        tracker.save_result("sx-2", "ATP - Shanghai", "Hanfmann", "Majchrzak",
                            1, 0, _iso(), detail=(20, 19, "games", "sx"))
        tracker.save_result("sx-2", "ATP - Shanghai", "Hanfmann", "Majchrzak",
                            1, 0, _iso())
        row = _row(temp_db, "sx-2")
        assert (row["detail_home"], row["detail_away"]) == (20, 19)
        assert row["detail_kind"] == "games"

    def test_riscrittura_con_dettaglio_aggiorna(self, temp_db):
        tracker.save_result("sx-3", "WTA - Beijing", "Ito", "Birrell", 2, 0,
                            _iso(), detail=(1, 1, "raw", "sx"))
        tracker.save_result("sx-3", "WTA - Beijing", "Ito", "Birrell", 2, 0,
                            _iso(), detail=(2, 12, "games", "sx"))
        row = _row(temp_db, "sx-3")
        assert (row["detail_home"], row["detail_away"]) == (2, 12)
        assert row["detail_kind"] == "games"

    def test_il_verdetto_resta_dei_punteggi_canonici(self, temp_db):
        """Il dettaglio NON e' una via per il verdetto: `result` segue (1,0)."""
        tracker.save_result("sx-4", "ATP - Shanghai", "Giron", "Baez", 0, 1,
                            _iso(), detail=(13, 13, "games", "sx"))
        row = _row(temp_db, "sx-4")
        assert row["result"] == "2"          # Baez (trasferta) ha vinto
        assert (row["detail_home"], row["detail_away"]) == (13, 13)

    def test_dettaglio_malformato_non_impedisce_il_risultato(self, temp_db):
        tracker.save_result("sx-5", "ATP", "A", "B", 1, 0, _iso(), detail="x")
        row = _row(temp_db, "sx-5")
        assert row["result"] == "1" and row["detail_home"] is None

    def test_migrazione_su_tabella_vecchia(self, monkeypatch, tmp_path):
        """DB di un deploy precedente: le colonne si aggiungono, righe intatte."""
        db = tmp_path / "vecchio.db"
        conn = sqlite3.connect(db)
        conn.execute("""CREATE TABLE match_results (
            match_id TEXT PRIMARY KEY, league TEXT, home_team TEXT,
            away_team TEXT, score_home INTEGER, score_away INTEGER,
            result TEXT, settled_at TEXT)""")
        conn.execute("INSERT INTO match_results VALUES ('old','ATP','A','B',2,1,"
                     "'1','2026-10-01T00:00:00Z')")
        conn.commit()
        conn.close()
        monkeypatch.setattr(tracker, "DB_PATH", db)
        tracker.save_result("nuovo", "ATP", "C", "D", 1, 0, _iso(),
                            detail=(12, 6, "games", "sx"))
        conn = sqlite3.connect(db)
        assert conn.execute("SELECT COUNT(*) FROM match_results").fetchone()[0] == 2
        conn.close()
        assert _row(db, "old")["detail_home"] is None      # nessun dato inventato
        assert _row(db, "nuovo")["detail_home"] == 12

    def test_save_result_detail_su_riga_assente_non_crea_nulla(self, temp_db):
        assert tracker.save_result_detail("manca", 12, 6, "games", "sx") is False
        assert _row(temp_db, "manca") is None

    def test_save_result_detail_aggiorna_solo_il_dettaglio(self, temp_db):
        tracker.save_result("sx-6", "ATP", "A", "B", 1, 0, _iso())
        assert tracker.save_result_detail("sx-6", 12, 4, "games", "sx-backfill")
        row = _row(temp_db, "sx-6")
        assert (row["score_home"], row["score_away"], row["result"]) == (1, 0, "1")
        assert (row["detail_home"], row["detail_away"]) == (12, 4)
        assert row["detail_source"] == "sx-backfill"

    def test_save_result_detail_non_solleva_mai(self, temp_db):
        assert tracker.save_result_detail(None, 1, 2) is False
        assert tracker.save_result_detail("x", None, None) is False


# ---------------------------------------------------------------------------
# 2. SX_SIGNALS — lettura del dettaglio dalla risposta dell'exchange
# ---------------------------------------------------------------------------

class TestDettaglioDallaRispostaSx:
    def test_tennis_con_game(self):
        import sx_signals as sx
        got = sx._result_detail({"sportId": 6, "teamOneScore": 12,
                                 "teamTwoScore": 4}, sx.SX_MONEYLINE_TYPE)
        assert got == (12, 4, "games", "sx")

    def test_tennis_senza_punteggi(self):
        """Mannarino-Basilashvili (mercato ritirato): nessun dettaglio."""
        import sx_signals as sx
        assert sx._result_detail({"sportId": 6}, sx.SX_MONEYLINE_TYPE) is None

    def test_type_52_non_tennis_registrato_come_raw(self):
        """eSports: i numeri ci sono, la semantica non e' verificata."""
        import sx_signals as sx
        got = sx._result_detail({"sportId": 9, "teamOneScore": 2,
                                 "teamTwoScore": 1}, sx.SX_MONEYLINE_TYPE)
        assert got[2] == "raw"

    def test_mercato_1x2_non_produce_dettaglio(self):
        """Sul calcio i punteggi SONO i gol: gia' salvati, nessun doppione."""
        import sx_signals as sx
        assert sx._result_detail({"sportId": 5, "teamOneScore": 2,
                                  "teamTwoScore": 1}, 1) is None

    def test_punteggi_non_interi_rifiutati(self):
        import sx_signals as sx
        for bad in ({"teamOneScore": "12", "teamTwoScore": 4},
                    {"teamOneScore": True, "teamTwoScore": 4},
                    {"teamOneScore": 12}):
            assert sx._result_detail(bad, sx.SX_MONEYLINE_TYPE) is None

    def test_hash_dentro_il_match_id(self):
        import sx_signals as sx
        h = "0x" + "ab" * 32
        assert sx._hash_from_match_id(f"sx-tennis-{h}") == h
        assert sx._hash_from_match_id("sx-L20532293") is None
        assert sx._hash_from_match_id("") is None


# ---------------------------------------------------------------------------
# 3. BACKFILL — i game delle partite gia' concluse
# ---------------------------------------------------------------------------

class TestBackfill:
    def _seed(self, temp_db, mid, home, away, detail=None):
        tracker.save_result(mid, "ATP - Shanghai", home, away, 1, 0, _iso(),
                            detail=detail)

    def test_scrive_i_game_delle_partite_gia_nel_referto(self, temp_db):
        import sx_signals as sx
        h = "0x" + "cd" * 32
        self._seed(temp_db, f"sx-tennis-{h}", "Tien", "Svajda")
        prov = _Prov({"data": [{"marketHash": h, "type": 52, "sportId": 6,
                                "teamOneScore": 14, "teamTwoScore": 10}]})
        res = sx.backfill_tennis_games(provider=prov)
        assert res["saved"] == 1 and res["calls"] == 1
        row = _row(temp_db, f"sx-tennis-{h}")
        assert (row["detail_home"], row["detail_away"]) == (14, 10)
        assert row["detail_kind"] == "games"

    def test_usa_il_market_id_delle_puntate(self, temp_db):
        """Le bet portano l'hash: e' la fonte piu' diretta del denaro."""
        import sx_signals as sx
        h = "0x" + "ef" * 32
        conn = tracker._get_conn()
        conn.execute("INSERT INTO bets (match_id, mercato, esito, market_id, "
                     "selection_id, price, stake, mode, status) "
                     "VALUES ('sx-L9','1X2','1',?,1,1.9,1.5,'live','FULLY_FILLED')",
                     (h,))
        conn.commit()
        conn.close()
        self._seed(temp_db, "sx-L9", "Paolini", "Kartal")
        prov = _Prov({"data": [{"marketHash": h, "type": 52, "sportId": 6,
                                "teamOneScore": 2, "teamTwoScore": 12}]})
        res = sx.backfill_tennis_games(provider=prov)
        assert res["saved"] == 1
        assert _row(temp_db, "sx-L9")["detail_home"] == 2

    def test_partita_fuori_referto_conta_no_row(self, temp_db):
        import sx_signals as sx
        h = "0x" + "11" * 32
        conn = tracker._get_conn()
        conn.execute("INSERT INTO bets (match_id, mercato, esito, market_id, "
                     "selection_id, price, stake, mode, status) "
                     "VALUES ('sx-L10','1X2','1',?,1,2.0,1.0,'live','FULLY_FILLED')",
                     (h,))
        conn.commit()
        conn.close()
        prov = _Prov({"data": [{"marketHash": h, "type": 52, "sportId": 6,
                                "teamOneScore": 12, "teamTwoScore": 5}]})
        res = sx.backfill_tennis_games(provider=prov)
        assert res["saved"] == 0 and res["no_row"] == 1

    def test_mercato_non_tennis_ignorato(self, temp_db):
        import sx_signals as sx
        h = "0x" + "22" * 32
        self._seed(temp_db, f"sx-tennis-{h}", "A", "B")
        prov = _Prov({"data": [{"marketHash": h, "type": 52, "sportId": 9,
                                "teamOneScore": 2, "teamTwoScore": 1}]})
        res = sx.backfill_tennis_games(provider=prov)
        assert res["saved"] == 0 and res["tennis"] == 0

    def test_errore_di_rete_non_solleva(self, temp_db):
        import sx_signals as sx

        class _Boom:
            def _get(self, path, params=None):
                raise RuntimeError("rete giu'")

        h = "0x" + "33" * 32
        self._seed(temp_db, f"sx-tennis-{h}", "A", "B")
        res = sx.backfill_tennis_games(provider=_Boom())
        assert res["saved"] == 0 and res["error"].startswith("find:")
        assert _row(temp_db, f"sx-tennis-{h}")["detail_home"] is None

    def test_nessun_candidato(self, temp_db):
        import sx_signals as sx
        res = sx.backfill_tennis_games(provider=_Prov({"data": []}))
        assert res["hashes"] == 0 and res["saved"] == 0

    def test_nessuna_scrittura_su_dettaglio_assente(self, temp_db):
        import sx_signals as sx
        h = "0x" + "44" * 32
        self._seed(temp_db, f"sx-tennis-{h}", "A", "B")
        prov = _Prov({"data": [{"marketHash": h, "type": 52, "sportId": 6}]})
        res = sx.backfill_tennis_games(provider=prov)
        assert res["no_scores"] == 1 and res["saved"] == 0

    def test_cli_espone_il_comando(self):
        from pathlib import Path as _P
        src = _P("sx_signals.py").read_text(encoding="utf-8")
        assert 'backfill-games' in src
        assert "backfill_tennis_games()" in src


# ---------------------------------------------------------------------------
# 4. TENNIS_MARKOV — il campione dei game e la calibrazione
# ---------------------------------------------------------------------------

class TestLetturaGamePerLaCalibrazione:
    def _seed_games(self, temp_db, rows):
        for i, (mid, sh, sa) in enumerate(rows):
            tracker.save_result(mid, "ATP - Shanghai", f"P{i}", f"Q{i}", 1, 0,
                                _iso(), detail=(sh, sa, "games", "sx"))

    def test_preferisce_il_dettaglio(self, temp_db):
        import tennis_markov as tm
        self._seed_games(temp_db, [("m1", 14, 10), ("m2", 7, 13)])
        stats = tm.ledger_games_stats(db=str(temp_db))
        assert stats["source"] == "ledger_detail"
        assert stats["semantics"] == "games"
        assert stats["ok"] is True
        assert stats["observed_mean"] == pytest.approx((24 + 20) / 2)

    def test_ripiego_sui_punteggi_canonici(self, temp_db):
        """Senza dettaglio si legge il referto come prima (e lo si dichiara)."""
        import tennis_markov as tm
        tracker.save_result("m1", "ATP - Shanghai", "A", "B", 1, 0, _iso())
        stats = tm.ledger_games_stats(db=str(temp_db))
        assert stats["source"] == "ledger"
        assert stats["semantics"] == "unknown"
        assert stats["ok"] is False

    def test_il_kind_raw_non_entra_nel_campione(self, temp_db):
        import tennis_markov as tm
        tracker.save_result("m1", "ATP - Shanghai", "A", "B", 1, 0, _iso(),
                            detail=(2, 1, "raw", "sx"))
        stats = tm.ledger_games_stats(db=str(temp_db))
        assert stats["source"] == "ledger"
        assert stats["semantics"] == "unknown"

    def test_soglia_fascia_tennis(self, temp_db):
        """Il filtro di lega resta: il calcio non entra nei game."""
        import tennis_markov as tm
        tracker.save_result("c1", "Serie A", "Inter", "Napoli", 3, 2, _iso(),
                            detail=(3, 2, "games", "sx"))
        stats = tm.ledger_games_stats(db=str(temp_db))
        assert stats["sample"] == 0

    def test_calibrazione_parte_col_dettaglio(self, temp_db):
        import tennis_markov as tm
        self._seed_games(temp_db, [(f"m{i}", 13, 9) for i in range(10)])
        stats = tm.ledger_games_stats(db=str(temp_db))
        assert stats["semantics"] == "games"
        cal = tm.calibrate_base_hold(rows=[{"sh": 13, "sa": 9}] * 10,
                                     grid=(0.60, 0.70, 0.75, 0.80, 0.85, 0.90))
        assert cal["ok"] is True
        assert 0.6 <= cal["base_suggested"] <= 0.9

    def test_sources_tried_dichiarate(self, temp_db):
        import tennis_markov as tm
        self._seed_games(temp_db, [("m1", 12, 6), ("m2", 12, 7)])
        stats = tm.ledger_games_stats(db=str(temp_db))
        assert "ledger_detail" in stats["sources_tried"]


class TestNessunaRegressioneNelPercorsoDelDenaro:
    def test_settlement_deriva_il_verdetto_dai_punteggi_canonici(self, temp_db):
        """Un mercato tennis risolto: il dettaglio non cambia il P/L."""
        tracker.save_result("sx-tennis-x", "ATP", "A", "B", 0, 1, _iso(),
                            detail=(13, 13, "games", "sx"))
        conn = tracker._get_conn()
        conn.execute("INSERT INTO bets (match_id, mercato, esito, price, stake, "
                     "mode, status) VALUES ('sx-tennis-x','1X2','2',2.0,1.5,"
                     "'live','FULLY_FILLED')")
        conn.commit()
        conn.close()
        settled, _pushes = tracker.settle_bets()
        assert settled == 1
        conn = tracker._get_conn()
        row = conn.execute("SELECT esito_finale, profit FROM bets WHERE "
                           "match_id='sx-tennis-x'").fetchone()
        conn.close()
        assert row[0] == "won" and row[1] == pytest.approx(1.5)


# --------------------------------------------------------------------------
# IaC: la catena di Markov legge env -> devono essere dichiarate `preserve()`
# --------------------------------------------------------------------------
class TestIaCMarkov:
    def test_env_dichiarate_nella_iac(self):
        import re
        root = Path(__file__).resolve().parent
        iac = (root / ".railway" / "railway.ts").read_text(encoding="utf-8")
        src = (root / "tennis_markov.py").read_text(encoding="utf-8")
        names = set(re.findall(r"\"(TENNIS_MARKOV_[A-Z0-9_]+)\"", src))
        assert names, "nessuna env TENNIS_MARKOV_ trovata nel sorgente"
        missing = {n for n in names if f"{n}: preserve()" not in iac}
        assert not missing, missing
