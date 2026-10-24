"""Test di `live_decomp` (scomposizione del P/L delle puntate LIVE chiuse).

Tutti OFFLINE: ledger SQLite temporaneo, nessuna rete, nessun ordine, nessuna
scrittura (la connessione e' `mode=ro` e il test lo pretende).

Cosa difendono, in ordine di importanza:
  1. la lettura e' di SOLA LETTURA (una `UPDATE` deve essere RIFIUTATA);
  2. le tre classificazioni (corsia, fascia quota, era) sono deterministiche e
     dichiarate — se cambiano in silenzio, la scomposizione del P/L racconta
     una storia diversa senza che nessuno se ne accorga;
  3. si contano SOLO le puntate live CHIUSE (un push e' un esito, un'apertura
     no): includerne una cambierebbe il denominatore del ROI;
  4. fail-safe su DB assente/corrotto e su date illeggibili.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import live_decomp as ld


# ---------------------------------------------------------------------------
# fixture: ledger minimo con lo schema di produzione (solo le colonne usate)
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "bets.db"
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE bets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id TEXT, mercato TEXT, esito TEXT, market_id TEXT,
            selection_id INTEGER, price REAL, stake REAL, mode TEXT,
            status TEXT, bet_id TEXT, esito_finale TEXT, profit REAL,
            created_at TEXT, settled_at TEXT, home TEXT, away TEXT,
            league TEXT, commence TEXT
        )""")
    conn.commit()
    conn.close()
    return path


def _bet(db_path, *, mid="sx-L1", mercato="1X2", price=1.70, stake=1.0,
         profit=None, finale="won", mode="live", league="", created="2026-10-16T10:00:00"):
    """Riga di ledger come la scrive il bot (profit dal verdetto se non dato)."""
    if profit is None:
        profit = round(stake * (price - 1.0), 4) if finale == "won" else (
            0.0 if finale == "push" else -stake)
    conn = sqlite3.connect(db_path)
    conn.execute("""INSERT INTO bets (match_id, mercato, esito, price, stake, mode,
                    status, esito_finale, profit, created_at, league)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                 (mid, mercato, "1", price, stake, mode, "FULLY_FILLED",
                  finale, profit, created, league))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# 1. Lettura: solo live CHIUSE, sola lettura
# ---------------------------------------------------------------------------

class TestLettura:
    def test_conta_solo_le_live_chiuse(self, db):
        _bet(db, mid="a")                                    # live chiusa
        _bet(db, mid="b", mode="sim")                        # simulata
        _bet(db, mid="c", finale=None)                       # ancora aperta
        _bet(db, mid="d", finale="")                         # esito vuoto
        data = ld.analyze(db)
        assert data["ok"] is True
        assert data["total"]["n"] == 1

    def test_il_push_e_un_esito_non_un_dato_mancante(self, db):
        _bet(db, mid="a", finale="push", price=2.0, stake=1.5)
        total = ld.analyze(db)["total"]
        assert total["n"] == 1 and total["push"] == 1
        assert total["profit"] == pytest.approx(0.0)
        assert total["roi_pct"] == pytest.approx(0.0)
        assert total["hit_pct"] is None        # nessun vinto/perso: hit n/d

    def test_roi_e_sul_denaro_reale_non_media_di_medie(self, db):
        _bet(db, mid="a", price=2.0, stake=10.0, finale="won")    # +10
        _bet(db, mid="b", price=2.0, stake=1.0, finale="lost")    # -1
        total = ld.analyze(db)["total"]
        assert total["stake"] == pytest.approx(11.0)
        assert total["profit"] == pytest.approx(9.0)
        assert total["roi_pct"] == pytest.approx(round(100 * 9.0 / 11.0, 2))

    def test_la_connessione_e_di_sola_lettura(self, db):
        """Senza `mode=ro` una svista potrebbe scrivere sul volume vero."""
        conn = ld._connect(db)
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("UPDATE bets SET profit = 99")
        conn.close()

    def test_db_assente_non_solleva(self, tmp_path):
        data = ld.analyze(tmp_path / "nope.db")
        assert data["ok"] is False and "db assente" in data["error"]

    def test_db_corrotto_non_solleva(self, tmp_path):
        bad = tmp_path / "bad.db"
        bad.write_text("non sono un database")
        data = ld.analyze(bad)
        assert data["ok"] is False
        assert "lettura fallita" in data["error"]

    def test_report_su_dati_non_disponibili(self, tmp_path):
        out = ld.format_report(ld.analyze(tmp_path / "nope.db"))
        assert "non disponibile" in out


# ---------------------------------------------------------------------------
# 2. Classificazione della CORSIA (prefisso + mercato + lega)
# ---------------------------------------------------------------------------

class TestCorsia:
    @pytest.mark.parametrize("row,atteso", [
        ({"match_id": "sx-tennis-0xabc", "mercato": "TENNIS", "league": ""},
         "TENNIS"),
        ({"match_id": "sx-1", "mercato": "TENNIS", "league": ""}, "TENNIS"),
        ({"match_id": "sx-esports-0xabc", "mercato": "1X2", "league": ""},
         "ESPORTS"),
        ({"match_id": "sx-1", "mercato": "ML", "league": ""}, "ESPORTS"),
        ({"match_id": "sx-L1", "mercato": "1X2", "league": "NFL"}, "FOOTBALL USA"),
        ({"match_id": "sx-L1", "mercato": "1X2", "league": "NCAAF"},
         "FOOTBALL USA"),
        ({"match_id": "sx-L1", "mercato": "1X2", "league": "CFL"},
         "FOOTBALL USA"),
        ({"match_id": "sx-L1", "mercato": "OU", "league": "MLS"}, "CALCIO OU"),
        ({"match_id": "sx-L1", "mercato": "AH", "league": "Serie A"}, "CALCIO AH"),
        ({"match_id": "sx-L1", "mercato": "1X2", "league": "Serie A"},
         "CALCIO 1X2"),
        ({"match_id": "sx-L1", "mercato": "BTTS", "league": "Serie A"}, "ALTRO"),
    ])
    def test_corsie_riconosciute(self, row, atteso):
        assert ld.lane_of(row) == atteso

    def test_tennis_ha_la_precedenza_sulla_lega(self):
        """Un tennis con lega NFL resta tennis: il prefisso vince."""
        assert ld.lane_of({"match_id": "sx-tennis-x", "mercato": "TENNIS",
                           "league": "NFL"}) == "TENNIS"

    def test_lega_in_minuscolo_non_sfugge(self):
        assert ld.lane_of({"match_id": "sx-1", "mercato": "1X2",
                           "league": "nfl"}) == "FOOTBALL USA"

    def test_lega_con_spazi_non_diventa_football(self):
        assert ld.lane_of({"match_id": "sx-1", "mercato": "1X2",
                           "league": "NFL Europe"}) == "CALCIO 1X2"


# ---------------------------------------------------------------------------
# 3. Fascia quota (bordi: [min, max), ultima aperta)
# ---------------------------------------------------------------------------

class TestFasciaQuota:
    @pytest.mark.parametrize("price,atteso", [
        (1.10, "< 1,20"),
        (1.20, "1.20-1.50"),
        (1.4999, "1.20-1.50"),
        (1.50, "1.50-1.80"),
        (1.80, "1.80-2.00"),
        (2.00, "2.00-2.30"),
        (2.30, "2.30-2.50"),
        (2.50, ">= 2,50"),
        (17.02, ">= 2,50"),
    ])
    def test_bordi(self, price, atteso):
        assert ld.band_of(price) == atteso

    def test_ogni_fascia_ha_una_etichetta(self):
        assert [ld.band_of(p) for p in (1.0, 1.3, 1.6, 1.9, 2.1, 2.4, 3.0)] == [
            "< 1,20", "1.20-1.50", "1.50-1.80", "1.80-2.00", "2.00-2.30",
            "2.30-2.50", ">= 2,50"]


# ---------------------------------------------------------------------------
# 4. Era (data di apertura, date ISO o con spazio)
# ---------------------------------------------------------------------------

class TestEra:
    @pytest.mark.parametrize("ts,atteso", [
        ("2026-09-01T10:00:00", "< 11/09 quota 1.30-1.80"),
        ("2026-09-11T00:00:00", "11/09-13/10 favoriti netti"),
        ("2026-10-13T23:59:59", "11/09-13/10 favoriti netti"),
        ("2026-10-14T00:00:00", "14/10-20/10 bot aggressivo"),
        ("2026-10-20T22:53:00", "14/10-20/10 bot aggressivo"),
        ("2026-10-21T00:00:00", "dal 21/10 cap 1,00 USDC"),
        ("2026-10-24T09:00:00", "dal 21/10 cap 1,00 USDC"),
    ])
    def test_soglie(self, ts, atteso):
        assert ld.era_of(ts) == atteso

    def test_data_con_spazio_e_letta_come_iso(self):
        assert ld.era_of("2026-10-14 08:00:00") == "14/10-20/10 bot aggressivo"

    def test_data_mancante_ha_un_bucket_suo(self):
        """Senza data non si inventa un'era.

        Il primo giro del 24/10/2026 classificava `""` come `< 11/09`
        (confronto fra stringhe): una riga non databile finiva nella strategia
        piu' vecchia. Ora ha il bucket dichiarato `(data assente)`.
        """
        assert ld.era_of("") == ld.ERA_MISSING
        assert ld.era_of("   ") == ld.ERA_MISSING
        assert ld.era_of("boh") == ld.ERA_MISSING
        # e la somma dei bucket resta completa: nessuna riga sparisce
    def test_riga_senza_data_non_sparisce_dai_conti(self, db):
        _bet(db, mid="a", created="")
        data = ld.analyze(db)
        assert [b["label"] for b in data["by_era"]] == [ld.ERA_MISSING]
        assert sum(b["n"] for b in data["by_era"]) == data["total"]["n"]


# ---------------------------------------------------------------------------
# 5. Raggruppamenti e report
# ---------------------------------------------------------------------------

class TestRaggruppamenti:
    def test_by_lane_somma_al_totale(self, db):
        _bet(db, mid="sx-tennis-a", mercato="TENNIS", price=2.0, stake=2.0)
        _bet(db, mid="sx-L1", mercato="1X2", price=1.6, stake=1.0, finale="lost")
        data = ld.analyze(db)
        assert sum(b["n"] for b in data["by_lane"]) == data["total"]["n"]
        assert sum(b["stake"] for b in data["by_lane"]) == pytest.approx(
            data["total"]["stake"])
        assert sum(b["profit"] for b in data["by_lane"]) == pytest.approx(
            data["total"]["profit"])

    def test_by_lane_ordina_per_perdita(self, db):
        _bet(db, mid="sx-tennis-a", mercato="TENNIS", price=1.5, stake=5.0,
             finale="lost")
        _bet(db, mid="sx-L1", mercato="1X2", price=2.0, stake=1.0)
        lanes = [b["label"] for b in ld.analyze(db)["by_lane"]]
        assert lanes[0] == "TENNIS"

    def test_by_band_ordina_per_quota(self, db):
        _bet(db, mid="a", price=2.6, stake=1.0, finale="lost")
        _bet(db, mid="b", price=1.3, stake=1.0)
        bands = [b["label"] for b in ld.analyze(db)["by_band"]]
        assert bands == ["1.20-1.50", ">= 2,50"]

    def test_by_league_scarta_i_campioni_di_uno(self, db):
        _bet(db, mid="a", league="Serie A")
        _bet(db, mid="b", league="Liga MX")
        _bet(db, mid="c", league="Liga MX")
        labels = [b["label"] for b in ld.analyze(db)["by_league"]]
        assert labels == ["Liga MX"]

    def test_lega_assente_e_dichiarata_non_nascosta(self, db):
        _bet(db, mid="a", league="")
        _bet(db, mid="b", league="")
        labels = [b["label"] for b in ld.analyze(db)["by_league"]]
        assert labels == ["(lega assente)"]

    def test_cross_lane_band_solo_con_almeno_tre(self, db):
        _bet(db, mid="a", price=1.6, stake=1.0)
        _bet(db, mid="b", price=1.6, stake=1.0)
        assert ld.analyze(db)["cross_lane_band"] == []
        _bet(db, mid="c", price=1.6, stake=1.0)
        cross = ld.analyze(db)["cross_lane_band"]
        assert len(cross) == 1 and cross[0]["n"] == 3

    def test_report_contiene_le_sezioni_e_i_numeri(self, db):
        _bet(db, mid="sx-tennis-a", mercato="TENNIS", price=1.4, stake=2.0)
        _bet(db, mid="sx-L1", mercato="1X2", price=2.6, stake=1.0, finale="lost")
        out = ld.format_report(ld.analyze(db))
        for sezione in ("PER CORSIA", "PER FASCIA QUOTA", "PER LEGA",
                        "PER ERA", "PUNTATE LIVE CHIUSE"):
            assert sezione in out
        assert "TENNIS" in out and "1.20-1.50" in out

    def test_json_serializzabile(self, db):
        import json
        _bet(db, mid="a")
        blob = json.dumps(ld.analyze(db), ensure_ascii=False)
        assert "\"by_lane\"" in blob


# ---------------------------------------------------------------------------
# 6. Tripwire: il modulo e' di sola LETTURA e non tocca il percorso ordini
# ---------------------------------------------------------------------------

class TestTripwire:
    def test_nessuna_scrittura_nel_sorgente(self):
        src = Path("live_decomp.py").read_text(encoding="utf-8")
        for token in ("INSERT ", "UPDATE bets", "DELETE FROM", "save_bet(",
                      "save_prediction("):
            assert token not in src, token

    def test_nessun_percorso_d_ordine_o_rete(self):
        src = Path("live_decomp.py").read_text(encoding="utf-8")
        for token in ("import requests", "_live_fill", "place_order",
                      "execution_engine", "urllib"):
            assert token not in src, token

    def test_import_leggero(self):
        """Importare il modulo non deve caricare la produzione."""
        import subprocess
        import sys
        code = ("import sys, live_decomp; "
                "print(','.join(m for m in ('auto_bet','tracker','bot','odds_api') "
                "if m in sys.modules))")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                             text=True, timeout=60)
        assert out.returncode == 0
        assert out.stdout.strip() == ""
