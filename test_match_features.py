"""Test OFFLINE di `match_features.py` (modulo 1 del programma quantitativo).

Zero rete, zero crediti, zero ordini: DB SQLite temporaneo con lo schema di
produzione, `xg_fn` iniettato, date SEMPRE relative (o esplicite) — un test che
scade col calendario e' un falso allarme che arriva nel momento peggiore.

Le due proprieta' che contano piu' di tutte:
1. **NESSUN LOOK-AHEAD**: aggiungere un risultato SUCCESSIVO non cambia le
   feature di una partita precedente (verificato sia sull'indice sia end-to-end
   sul percorso storico).
2. **SOLA LETTURA**: la connessione rifiuta una scrittura.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import match_features as mf


# ---------------------------------------------------------------------------
# Fixture: DB temporaneo con lo schema di produzione
# ---------------------------------------------------------------------------

def _make_db(path: Path, rows) -> Path:
    """`rows` = tuple (match_id, home, away, sh, sa, settled_at, league)."""
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE match_results (
            match_id TEXT PRIMARY KEY,
            league TEXT,
            home_team TEXT,
            away_team TEXT,
            score_home INTEGER,
            score_away INTEGER,
            result TEXT,
            settled_at TEXT
        )""")
    for row in rows:
        mid, home, away, sh, sa, ts, league = row
        result = "1" if sh > sa else ("2" if sh < sa else "X")
        conn.execute(
            "INSERT INTO match_results (match_id, league, home_team, away_team,"
            " score_home, score_away, result, settled_at) VALUES (?,?,?,?,?,?,?,?)",
            (mid, league, home, away, sh, sa, result, ts))
    conn.commit()
    conn.close()
    return path


BASE = datetime(2026, 3, 1, 18, 0, tzinfo=timezone.utc)


def _iso(days: float) -> str:
    return (BASE + timedelta(days=days)).isoformat()


@pytest.fixture
def small_db(tmp_path):
    """Storia minima: Inter e Napoli con un paio di partite a testa."""
    rows = [
        ("m1", "Inter", "Roma", 2, 0, _iso(0), "Serie A"),
        ("m2", "Napoli", "Lazio", 1, 1, _iso(1), "Serie A"),
        ("m3", "Inter", "Milan", 1, 2, _iso(7), "Serie A"),
        ("m4", "Napoli", "Juventus", 3, 1, _iso(9), "Serie A"),
        ("m5", "Roma", "Napoli", 0, 2, _iso(20), "Serie A"),
    ]
    return _make_db(tmp_path / "small.db", rows)


@pytest.fixture
def conn(small_db):
    c = mf.ro_conn(small_db)
    yield c
    c.close()


# ---------------------------------------------------------------------------
# Parsing / chiavi
# ---------------------------------------------------------------------------

class TestParseTs:
    def test_iso_con_Z(self):
        assert mf.parse_ts("2026-10-18T18:00:00Z") == datetime(
            2026, 10, 18, 18, 0, tzinfo=timezone.utc)

    def test_iso_con_offset(self):
        d = mf.parse_ts("2026-10-18T20:00:00+02:00")
        assert d == datetime(2026, 10, 18, 18, 0, tzinfo=timezone.utc)

    def test_spazio_e_T_equivalenti(self):
        assert mf.parse_ts("2026-10-18 18:00:00Z") == mf.parse_ts("2026-10-18T18:00:00Z")

    def test_naive_rifiutato_di_default(self):
        assert mf.parse_ts("2026-10-18T18:00:00") is None

    def test_naive_accettato_se_dichiarato(self):
        d = mf.parse_ts("2026-10-18T18:00:00", assume_utc=True)
        assert d == datetime(2026, 10, 18, 18, 0, tzinfo=timezone.utc)

    @pytest.mark.parametrize("bad", [None, "", "   ", "non-una-data", "2026-13-45"])
    def test_illeggibile(self, bad):
        assert mf.parse_ts(bad) is None


class TestNormKey:
    @pytest.mark.parametrize("a,b", [
        ("Inter", "inter"),
        ("Wrexham AFC", "wrexhamafc"),
        ("FC København", "fckbenhavn"),
        ("Paris Saint-Germain", "parissaintgermain"),
    ])
    def test_forma(self, a, b):
        assert mf._norm_key(a) == b

    def test_vuoto(self):
        assert mf._norm_key(None) == ""


# ---------------------------------------------------------------------------
# Indice della storia
# ---------------------------------------------------------------------------

class TestTeamIndex:
    def test_risoluzione_esatta(self, conn):
        index, stats = mf.build_index(conn)
        assert index.resolve("Inter") is not None
        assert stats["rows_used"] == 5

    def test_squadra_sconosciuta(self, conn):
        index, _ = mf.build_index(conn)
        assert index.resolve("Squadra Inventata") is None

    def test_nome_vuoto_o_ignoto_non_solleva(self, conn):
        index, _ = mf.build_index(conn)
        assert index.resolve("") is None
        assert index.resolve(None) is None
        assert index.resolve("Squadra Che Non Esiste") is None

    def test_alias_su_nome_con_suffisso_di_club(self, tmp_path):
        db = _make_db(tmp_path / "a.db", [
            ("m1", "Wrexham AFC", "Roma", 1, 0, _iso(0), "League One"),
        ])
        c = mf.ro_conn(db)
        try:
            index, _ = mf.build_index(c)
            assert index.resolve("Wrexham") is not None
        finally:
            c.close()

    def test_alias_disattivabile(self, tmp_path):
        db = _make_db(tmp_path / "b.db", [
            ("m1", "Wrexham AFC", "Roma", 1, 0, _iso(0), "League One"),
        ])
        c = mf.ro_conn(db)
        try:
            index, _ = mf.build_index(c, allow_alias=False)
            assert index.resolve("Wrexham") is None
            assert index.resolve("Wrexham AFC") is not None
        finally:
            c.close()

    def test_matches_before_strettamente_prima(self, conn):
        index, _ = mf.build_index(conn)
        assert index.matches_before("Inter", BASE.timestamp()) == []
        # a +10 giorni le partite di Inter precedenti sono m1 (0) e m3 (7)
        assert len(index.matches_before(
            "Inter", (BASE + timedelta(days=10)).timestamp())) == 2

    def test_matches_before_esclude_il_kickoff_esatto(self, conn):
        index, _ = mf.build_index(conn)
        entries = index.matches_before(
            "Inter", (BASE + timedelta(days=7)).timestamp())
        assert len(entries) == 1                  # solo m1
        assert entries[0][1] == "m1"

    def test_squadra_sconosciuta_nessuna_partita(self, conn):
        index, _ = mf.build_index(conn)
        assert index.matches_before("Nessuno", 9e9) == []


# ---------------------------------------------------------------------------
# Le feature: riposo, densita', forma
# ---------------------------------------------------------------------------

class TestTeamBlock:
    def test_riposo_e_finestre(self, conn):
        index, _ = mf.build_index(conn)
        block = mf._team_block(index, "Inter", (BASE + timedelta(days=10)).timestamp())
        # ultima partita di Inter = m3 a +7 giorni
        assert block["rest_days"] == pytest.approx(3.0, abs=0.01)
        assert block["hist_matches"] == 2
        assert block["matches_7d"] == 1           # solo m3 (m1 a -10 giorni)
        assert block["matches_14d"] == 2
        assert block["matches_28d"] == 2

    def test_riposo_zero_doppio_turno(self, tmp_path):
        db = _make_db(tmp_path / "d.db", [
            ("m1", "Inter", "Roma", 1, 0, BASE.isoformat(), "Serie A"),
        ])
        c = mf.ro_conn(db)
        try:
            index, _ = mf.build_index(c)
            block = mf._team_block(index, "Inter", BASE.timestamp() + 3600)
            assert block["rest_days"] == pytest.approx(1 / 24, abs=0.001)
        finally:
            c.close()

    def test_senza_storia_tutto_assente_non_zero(self, conn):
        index, _ = mf.build_index(conn)
        block = mf._team_block(index, "Sconosciuta", 9e9)
        assert block["rest_days"] is None
        assert block["pts5"] is None
        assert block["hist_matches"] == 0
        # le finestre sono 0 ma la copertura e' dichiarata da hist_matches
        assert block["matches_7d"] == 0

    def test_congestionato_e_riposo_corto(self, tmp_path):
        rows = [("m%d" % i, "Inter", "Roma", 1, 0, _iso(i * 2.0), "Serie A")
                for i in range(4)]
        db = _make_db(tmp_path / "e.db", rows)
        c = mf.ro_conn(db)
        try:
            index, _ = mf.build_index(c)
            block = mf._team_block(index, "Inter", (BASE + timedelta(days=8)).timestamp())
            # ultimi 7 giorni (dal giorno 1): giorni 2, 4, 6 -> 3 partite
            assert block["matches_7d"] == 3
            assert block["congested"] == 1
            assert block["short_rest"] == 1
        finally:
            c.close()

    def test_forma_media_punti_gol(self, conn):
        index, _ = mf.build_index(conn)
        block = mf._team_block(index, "Napoli", (BASE + timedelta(days=21)).timestamp())
        # Napoli: m2 (1-1 -> 1), m4 (3-1 -> 3), m5 in trasferta 0-2 -> 3 punti
        assert block["pts5"] == pytest.approx(7 / 3, abs=1e-4)
        assert block["gf5"] == pytest.approx(2.0)        # (1+3+2)/3
        assert block["ga5"] == pytest.approx(2 / 3, abs=1e-4)   # (1+1+0)/3

    def test_gol_leggeri_dal_punto_di_vista_della_squadra(self, tmp_path):
        """In trasferta i gol della squadra sono quelli del lato ospite."""
        db = _make_db(tmp_path / "z.db", [
            ("m1", "Roma", "Inter", 0, 3, _iso(0), "Serie A"),
        ])
        c = mf.ro_conn(db)
        try:
            index, _ = mf.build_index(c)
            block = mf._team_block(index, "Inter", (BASE + timedelta(days=2)).timestamp())
            assert block["gf5"] == pytest.approx(3.0)
            assert block["ga5"] == pytest.approx(0.0)
            assert block["pts5"] == pytest.approx(3.0)
        finally:
            c.close()

    def test_forma_usata_solo_su_form_n_partite(self, tmp_path):
        rows = [("m%d" % i, "Inter", "Roma", 1, 1, _iso(i * 5.0), "Serie A")
                for i in range(8)]
        db = _make_db(tmp_path / "f.db", rows)
        c = mf.ro_conn(db)
        try:
            index, _ = mf.build_index(c)
            block = mf._team_block(index, "Inter", (BASE + timedelta(days=40)).timestamp())
            # ultime 5 = pareggi -> 1 punto ciascuno
            assert block["pts5"] == pytest.approx(1.0)
            assert block["hist_matches"] == 8
        finally:
            c.close()


# ---------------------------------------------------------------------------
# features_for: la riga completa
# ---------------------------------------------------------------------------

class TestFeaturesFor:
    def test_tutte_le_feature_presenti_nell_ordine(self, conn):
        index, _ = mf.build_index(conn)
        row = mf.features_for("Inter", "Napoli", _iso(10), index=index)
        assert set(mf.FEATURE_NAMES) <= set(row)
        assert list(mf.feature_vector(row)) == [row[n] for n in mf.FEATURE_NAMES]

    def test_contesto_settimana_ora_mese(self, conn):
        index, _ = mf.build_index(conn)
        # 2026-03-01 e' una domenica
        row = mf.features_for("Inter", "Napoli", "2026-03-01T18:00:00Z", index=index)
        assert row["is_weekend"] == 1
        assert row["kickoff_hour"] == 18
        assert row["month"] == 3
        row = mf.features_for("Inter", "Napoli", "2026-03-04T12:00:00Z", index=index)
        assert row["is_weekend"] == 0

    def test_kickoff_illeggibile_tutto_assente(self, conn):
        index, _ = mf.build_index(conn)
        row = mf.features_for("Inter", "Napoli", "non-una-data", index=index)
        assert all(row[n] is None for n in mf.FEATURE_NAMES)

    def test_il_proprio_kickoff_non_entra_nella_storia(self, conn):
        """All'istante esatto di m5 (Roma-Napoli) il match non e' ancora storia."""
        index, _ = mf.build_index(conn)
        row = mf.features_for("Roma", "Napoli", _iso(20), index=index)
        assert row["hist_matches_away"] == 2      # Napoli: m2, m4 (non m5)
        assert row["hist_matches_home"] == 1      # Roma: m1 (non m5)

    def test_segno_xg_assente_di_default(self, conn):
        index, _ = mf.build_index(conn)
        row = mf.features_for("Inter", "Napoli", _iso(10), index=index)
        assert row["xg_present"] == 0            # flag noto, xG non richiesto
        assert row["xg_for_home"] is None        # la misura resta assente

    def test_kickoff_epoch_numerico(self, conn):
        """Un epoch gia' numerico e' accettato (stessa strada dell'ISO)."""
        index, _ = mf.build_index(conn)
        ts = (BASE + timedelta(days=10)).timestamp()
        row = mf.features_for("Inter", "Napoli", ts, index=index)
        assert row["rest_days_home"] is not None
        assert row["month"] == 3

    def test_xg_iniettato(self, conn):
        index, _ = mf.build_index(conn)

        class S:
            xg_for = 1.8
            xg_against = 0.9

        row = mf.features_for("Inter", "Napoli", _iso(10), index=index,
                              xg_fn=lambda team, league: S())
        assert row["xg_present"] == 1
        assert row["xg_for_home"] == 1.8
        assert row["xg_against_away"] == 0.9

    def test_xg_rotto_non_nega_il_resto(self, conn):
        index, _ = mf.build_index(conn)

        def boom(team, league):
            raise RuntimeError("provider giu'")

        row = mf.features_for("Inter", "Napoli", _iso(10), index=index, xg_fn=boom)
        assert row["xg_present"] == 0
        assert row["pts5_home"] is not None       # le altre feature restano

    def test_xg_none_resta_assente(self, conn):
        index, _ = mf.build_index(conn)
        row = mf.features_for("Inter", "Napoli", _iso(10), index=index,
                              xg_fn=lambda team, league: None)
        assert row["xg_present"] == 0


class TestNoLookAhead:
    """La proprieta' piu' importante del modulo."""

    def test_risultato_successivo_non_cambia_le_feature_precedenti(self, tmp_path):
        early = [
            ("m1", "Inter", "Roma", 2, 0, _iso(0), "Serie A"),
            ("m2", "Napoli", "Lazio", 1, 1, _iso(1), "Serie A"),
        ]
        later = early + [
            ("m3", "Inter", "Milan", 5, 0, _iso(30), "Serie A"),
            ("m4", "Napoli", "Juventus", 0, 4, _iso(31), "Serie A"),
        ]
        db_a = _make_db(tmp_path / "a.db", early)
        db_b = _make_db(tmp_path / "b.db", later)
        ca, cb = mf.ro_conn(db_a), mf.ro_conn(db_b)
        try:
            ia, _ = mf.build_index(ca)
            ib, _ = mf.build_index(cb)
            kickoff = (BASE + timedelta(days=10)).isoformat()
            fa = mf.features_for("Inter", "Napoli", kickoff, index=ia)
            fb = mf.features_for("Inter", "Napoli", kickoff, index=ib)
            assert fa == fb
        finally:
            ca.close()
            cb.close()

    def test_end_to_end_sul_percorso_storico(self, tmp_path):
        early = [
            ("m1", "Inter", "Roma", 2, 0, _iso(0), "Serie A"),
            ("m2", "Napoli", "Lazio", 1, 1, _iso(1), "Serie A"),
            ("m3", "Inter", "Napoli", 1, 1, _iso(6), "Serie A"),
        ]
        later = early + [("m9", "Inter", "Roma", 9, 9, _iso(40), "Serie A")]
        ca = mf.ro_conn(_make_db(tmp_path / "c.db", early))
        cb = mf.ro_conn(_make_db(tmp_path / "d.db", later))
        try:
            ra = mf.history_feature_rows(ca)
            rb = mf.history_feature_rows(cb)
            assert [r["match_id"] for r in ra["rows"]] == ["m3"]
            row_a = next(r for r in ra["rows"] if r["match_id"] == "m3")
            row_b = next(r for r in rb["rows"] if r["match_id"] == "m3")
            assert row_a == row_b
            # CONTROPROVA: dopo m9 la storia di Inter E' cambiata, quindi una
            # partita successiva vede un valore diverso (l'indice non e'
            # semplicemente "congelato").
            ia, _ = mf.build_index(ca)
            ib, _ = mf.build_index(cb)
            fa = mf.features_for("Inter", "Napoli", _iso(45), index=ia)
            fb = mf.features_for("Inter", "Napoli", _iso(45), index=ib)
            assert fa["hist_matches_home"] != fb["hist_matches_home"]
        finally:
            ca.close()
            cb.close()

    def test_la_riga_non_usa_il_proprio_risultato(self, tmp_path):
        """m3 e' Inter-Napoli: a quel kickoff Inter ha 1 partita, Napoli 2."""
        db = _make_db(tmp_path / "e.db", [
            ("m1", "Inter", "Roma", 2, 0, _iso(0), "Serie A"),
            ("m2", "Napoli", "Lazio", 1, 1, _iso(1), "Serie A"),
            ("m5", "Roma", "Napoli", 0, 2, _iso(3), "Serie A"),
            ("m3", "Inter", "Napoli", 1, 1, _iso(6), "Serie A"),
        ])
        c = mf.ro_conn(db)
        try:
            index, _ = mf.build_index(c)
            row = mf.features_for("Inter", "Napoli", _iso(6), index=index)
            assert row["hist_matches_home"] == 1   # solo m1
            assert row["hist_matches_away"] == 2   # m2 e m5
        finally:
            c.close()


# ---------------------------------------------------------------------------
# Vettore e label
# ---------------------------------------------------------------------------

class TestFeatureVector:
    def test_ordine_e_buchi_preservati(self, conn):
        index, _ = mf.build_index(conn)
        row = mf.features_for("Inter", "Napoli", _iso(10), index=index)
        vec = mf.feature_vector(row, ("pts5_home", "xg_for_home", "month"))
        assert vec[0] == row["pts5_home"]
        assert vec[1] is None
        assert vec[2] == 3

    def test_nome_ignoto_diventa_none(self):
        assert mf.feature_vector({"x": 1}, ("nope",)) == [None]

    def test_valore_non_numerico_diventa_none(self):
        assert mf.feature_vector({"a": "abc"}, ("a",)) == [None]

    def test_ordine_di_default_e_quello_dichiarato(self):
        row = {n: i for i, n in enumerate(mf.FEATURE_NAMES)}
        assert mf.feature_vector(row) == [float(i) for i in range(len(mf.FEATURE_NAMES))]


class TestLabel:
    @pytest.mark.parametrize("sh,sa,target,expected", [
        (2, 0, "home_win", 1), (1, 1, "home_win", 0), (0, 2, "home_win", 0),
        (1, 1, "home_or_draw", 1), (0, 2, "home_or_draw", 0),
        (0, 2, "away_win", 1), (2, 0, "away_win", 0),
        (2, 1, "over25", 1), (1, 1, "over25", 0), (2, 0, "over25", 0),
    ])
    def test_target(self, sh, sa, target, expected):
        assert mf._label(sh, sa, target) == expected

    def test_target_ignoto(self):
        assert mf._label(1, 0, "boh") is None


# ---------------------------------------------------------------------------
# Percorso storico
# ---------------------------------------------------------------------------

class TestHistoryFeatureRows:
    def test_righe_con_label(self, conn):
        data = mf.history_feature_rows(conn)
        assert data["error"] is None
        assert data["rows"]
        for row in data["rows"]:
            assert row["label"] in (0, 1)
            assert row["result"] in ("1", "X", "2")

    def test_righe_senza_storia_di_entrambe_sono_scartate(self, tmp_path):
        db = _make_db(tmp_path / "g.db", [
            ("m1", "Inter", "Roma", 2, 0, _iso(0), "Serie A"),
            ("m2", "SquadraA", "SquadraB", 1, 0, _iso(1), "Serie A"),
        ])
        c = mf.ro_conn(db)
        try:
            data = mf.history_feature_rows(c)
            ids = {r["match_id"] for r in data["rows"]}
            assert "m1" not in ids               # m1 e' la prima: nessuna storia
            assert data["stats"]["skipped"]["unknown_team"] >= 1
        finally:
            c.close()

    def test_righe_bad_ts_e_bad_score_scartate(self, tmp_path):
        db = _make_db(tmp_path / "h.db", [
            ("m1", "Inter", "Roma", 2, 0, _iso(0), "Serie A"),
            ("m2", "Inter", "Napoli", 1, 1, "non-una-data", "Serie A"),
        ])
        c = mf.ro_conn(db)
        try:
            data = mf.history_feature_rows(c)
            assert data["stats"]["skipped"]["bad_ts"] == 1
        finally:
            c.close()

    def test_limit(self, conn):
        data = mf.history_feature_rows(conn, limit=1)
        assert len(data["rows"]) <= 1

    def test_target_invalido(self, conn):
        data = mf.history_feature_rows(conn, target="boh")
        assert data["error"] == "target_non_valido:boh"

    def test_target_diverso_cambia_le_label(self, tmp_path):
        """m1 = 2-0 (home_win 1, over25 0), m2 = 2-1 (home_win 1, over25 1)."""
        db = _make_db(tmp_path / "i.db", [
            ("m1", "Inter", "Roma", 2, 0, _iso(0), "Serie A"),
            ("m2", "Inter", "Roma", 2, 1, _iso(20), "Serie A"),
        ])
        c = mf.ro_conn(db)
        try:
            hw = mf.history_feature_rows(c, target="home_win")
            ov = mf.history_feature_rows(c, target="over25")
            # m1 e' saltata (nessuna squadra aveva storia): resta m2
            assert [r["match_id"] for r in hw["rows"]] == ["m2"]
            assert hw["rows"][0]["label"] == 1
            assert ov["rows"][0]["label"] == 1
        finally:
            c.close()

    def test_db_senza_tabella(self, tmp_path):
        p = tmp_path / "vuoto.db"
        sqlite3.connect(p).close()
        c = mf.ro_conn(p)
        try:
            data = mf.history_feature_rows(c)
            assert data["error"] or data["rows"] == []
        finally:
            c.close()

    def test_db_assente_solleva_all_apertura(self, tmp_path):
        """Un DB che non esiste non si apre in read-only: l'errore e' esplicito."""
        with pytest.raises(sqlite3.OperationalError):
            mf.ro_conn(tmp_path / "nope.db")

    def test_db_senza_tabella_non_solleva(self, tmp_path):
        """Un volume di un deploy precedente non ha `match_results`: 0 righe."""
        p = tmp_path / "vecchio.db"
        conn_v = sqlite3.connect(p)
        conn_v.execute("CREATE TABLE altro (x INTEGER)")
        conn_v.commit()
        conn_v.close()
        c = mf.ro_conn(p)
        try:
            data = mf.history_feature_rows(c)
            assert data["rows"] == []
            index, stats = mf.build_index(c)
            assert index.known_keys() == []
            assert stats["rows"] == 0
        finally:
            c.close()

    def test_connessione_esterna_non_viene_chiusa(self, conn):
        mf.history_feature_rows(conn)
        assert conn.execute("SELECT 1").fetchone()[0] == 1


# ---------------------------------------------------------------------------
# Statistica: AUC, p-value, scrematura
# ---------------------------------------------------------------------------

class TestAverageRanks:
    def test_pareggi(self):
        assert mf._average_ranks([1.0, 1.0, 2.0]) == [1.5, 1.5, 3.0]

    def test_ordinamento(self):
        assert mf._average_ranks([3.0, 1.0, 2.0]) == [3.0, 1.0, 2.0]

    def test_tutti_uguali(self):
        assert mf._average_ranks([5.0, 5.0]) == [1.5, 1.5]


class TestFeatureAuc:
    def test_separazione_perfetta(self):
        rows = [{"x": 1.0, "label": 1} for _ in range(10)] + \
               [{"x": 0.0, "label": 0} for _ in range(10)]
        r = mf.feature_auc(rows, "x")
        assert r["auc"] == pytest.approx(1.0)
        assert r["useful"] is True

    def test_separazione_inversa(self):
        rows = [{"x": 0.0, "label": 1} for _ in range(10)] + \
               [{"x": 1.0, "label": 0} for _ in range(10)]
        r = mf.feature_auc(rows, "x")
        assert r["auc"] == pytest.approx(0.0)
        assert r["useful"] is True

    def test_pareggi_danno_meta(self):
        rows = [{"x": 1.0, "label": l} for l in (0, 1)] * 10
        assert mf.feature_auc(rows, "x")["auc"] == pytest.approx(0.5)

    def test_una_sola_classe(self):
        r = mf.feature_auc([{"x": 1.0, "label": 1}] * 5, "x")
        assert r["auc"] is None
        assert r["reason"] == "una_sola_classe"

    def test_nessun_dato_non_e_sbilanciamento(self):
        """Campione vuoto: il motivo e' `nessun_dato`, non `una_sola_classe`."""
        r = mf.feature_auc([], "x")
        assert r["n"] == 0 and r["reason"] == "nessun_dato"

    def test_valori_assenti_scartati(self):
        rows = [{"x": 1.0, "label": 1}, {"x": None, "label": 0},
                {"x": 0.0, "label": 0}]
        r = mf.feature_auc(rows, "x")
        assert r["n"] == 2

    def test_effetto_trascurabile_non_e_utile(self):
        """AUC 0.51 con n grande: significativa ma inutile da addestrare."""
        rows = []
        for i in range(500):
            rows.append({"x": 1.0 if i < 255 else 0.0, "label": 1})
        for i in range(500):
            rows.append({"x": 1.0 if i < 245 else 0.0, "label": 0})
        r = mf.feature_auc(rows, "x")
        assert r["auc"] == pytest.approx(0.51)
        assert r["useful"] is False
        assert r["reason"] == "effetto_trascurabile"

    def test_p_value_finito_e_nel_range(self):
        rows = [{"x": 1.0, "label": 1} for _ in range(30)] + \
               [{"x": 0.0, "label": 0} for _ in range(30)]
        r = mf.feature_auc(rows, "x")
        assert 0.0 <= r["p"] <= 1.0
        assert r["p"] < 0.05

    def test_feature_inesistente(self):
        r = mf.feature_auc([{"label": 1}, {"label": 0}], "nope")
        assert r["n"] == 0 and r["auc"] is None

    def test_label_assente_scartata(self):
        r = mf.feature_auc([{"x": 1.0, "label": None}], "x")
        assert r["n"] == 0


def _synthetic(n: int = 240):
    rows = []
    for i in range(n):
        strong = i % 2 == 0
        rows.append({"pts5_home": 3.0 if strong else 0.2,
                     "ga5_home": 0.4 if strong else 2.4,
                     "month": 6,
                     "label": 1 if strong else 0})
    return rows


class TestScreening:
    def test_trova_la_feature_forte(self):
        data = mf.screening(_synthetic(), names=("pts5_home", "ga5_home", "month"))
        assert data["sufficient"] is True
        assert data["useful"] == ["pts5_home", "ga5_home"]
        assert data["error"] is None

    def test_ordina_per_effetto(self):
        data = mf.screening(_synthetic(), names=("month", "pts5_home", "ga5_home"))
        assert data["results"][0]["name"] == "pts5_home"

    def test_campione_insufficiente_dichiarato(self):
        data = mf.screening(_synthetic(20)[:20], names=("pts5_home",))
        assert data["sufficient"] is False
        assert "rumore" in data["note"]

    def test_esclude_le_feature_in_look_ahead(self):
        data = mf.screening(_synthetic(), names=None)
        names = {r["name"] for r in data["results"]}
        for unsafe in mf.LOOKAHEAD_UNSAFE:
            assert unsafe not in names
        assert set(data["excluded_lookahead"]) == set(mf.LOOKAHEAD_UNSAFE)

    def test_target_invalido(self):
        assert mf.screening(_synthetic(), target="boh")["error"]

    def test_history_features_non_contiene_look_ahead(self):
        for unsafe in mf.LOOKAHEAD_UNSAFE:
            assert unsafe not in mf.HISTORY_FEATURES


class TestCoverage:
    def test_quota_di_presenza(self):
        rows = [{"a": 1, "b": None}, {"a": 2, "b": 3}]
        cov = mf.coverage(rows, ("a", "b"))
        assert cov["a"] == 1.0
        assert cov["b"] == pytest.approx(0.5)

    def test_vuoto(self):
        assert mf.coverage([], ("a",)) == {"a": 0.0}

    def test_zero_non_e_copertura(self):
        """Uno 0 VERO e' un valore presente: conta come coperto."""
        assert mf.coverage([{"a": 0}], ("a",))["a"] == 1.0


# ---------------------------------------------------------------------------
# Report / CLI
# ---------------------------------------------------------------------------

class TestReport:
    def test_report_con_errori(self):
        assert "❌" in mf.format_report({"error": "boom"})

    def test_report_dichiara_l_insufficienza(self):
        data = mf.screening(_synthetic(10), names=("pts5_home",))
        data["stats"] = {"feature_rows": 10, "skipped": {}}
        text = mf.format_report(data)
        assert "rumore" in text

    def test_report_mostra_le_utili(self):
        data = mf.screening(_synthetic(), names=("pts5_home",))
        data["stats"] = {"feature_rows": 240, "skipped": {}}
        text = mf.format_report(data)
        assert "pts5_home" in text and "✅" in text

    def test_report_riga_finale_sui_limiti(self):
        data = mf.screening(_synthetic(), names=("pts5_home",))
        data["stats"] = {"feature_rows": 240, "skipped": {}}
        assert "Nessuna decisione" in mf.format_report(data)


class TestCli:
    def test_history_json(self, small_db, capsys):
        rc = mf.main(["--db", str(small_db), "--history", "--json"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["mode"] == "history"
        assert "coverage" in payload

    def test_screening_ha_i_risultati(self, small_db, capsys):
        rc = mf.main(["--db", str(small_db), "--history", "--screening", "--json"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["results"]
        assert payload["excluded_lookahead"]
        assert payload["n"] == len(payload["rows"])

    def test_report_dichiara_il_campione(self, small_db, capsys):
        """Il report non deve stampare `Campione: None`."""
        rc = mf.main(["--db", str(small_db), "--history", "--screening"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Campione: None" not in out
        assert "Campione: " in out

    def test_match_singolo(self, small_db, capsys):
        rc = mf.main(["--db", str(small_db), "--match", "Inter", "Napoli",
                      "--kickoff", _iso(10)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "FEATURE" in out and "pts5_home" in out

    def test_match_json(self, small_db, capsys):
        rc = mf.main(["--db", str(small_db), "--match", "Inter", "Napoli",
                      "--kickoff", _iso(10), "--json"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["features"]["month"] == 3

    def test_db_assente_esce_con_errore(self, tmp_path, capsys):
        with pytest.raises(sqlite3.OperationalError):
            mf.main(["--db", str(tmp_path / "nope.db"), "--history"])


# ---------------------------------------------------------------------------
# Tripwire: sola lettura, nessuna rete, nessun ordine, import leggero
# ---------------------------------------------------------------------------

class TestTripwire:
    def test_la_connessione_rifiuta_una_scrittura(self, small_db):
        c = mf.ro_conn(small_db)
        try:
            with pytest.raises(sqlite3.OperationalError):
                c.execute("UPDATE match_results SET home_team = 'X'")
        finally:
            c.close()

    def test_sorgente_senza_scritture(self):
        src = Path(mf.__file__).read_text(encoding="utf-8")
        for bad in ("INSERT INTO", "UPDATE ", "DELETE FROM", "commit()"):
            assert bad not in src, f"scrittura nel sorgente: {bad}"

    def test_connessione_solo_read_only(self):
        src = Path(mf.__file__).read_text(encoding="utf-8")
        assert "mode=ro" in src

    def test_nessuna_rete_o_ordine_nel_sorgente(self):
        src = Path(mf.__file__).read_text(encoding="utf-8")
        for bad in ("requests.", "httpx.", "urllib", "place_limit_order",
                    "_live_fill", "execution_engine", "save_bet",
                    "save_prediction", "resolve_market_for"):
            assert bad not in src, f"riferimento vietato: {bad}"

    def test_import_leggero_in_sottoprocesso(self):
        code = ("import sys, match_features as mf; "
                "bad=[m for m in ('tracker','auto_bet','bot','odds_api','sx_signals') "
                "if m in sys.modules]; "
                "print('OK' if not bad else 'BAD:'+','.join(bad))")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                             text=True, cwd=str(Path(mf.__file__).parent))
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == "OK"

    def test_nessuna_soglia_di_strategia(self):
        src = Path(mf.__file__).read_text(encoding="utf-8")
        for bad in ("EV_MIN", "MARKET_EDGE", "ODDS_MAX", "KELLY"):
            assert bad not in src, f"soglia di strategia nel modulo: {bad}"
