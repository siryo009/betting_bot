"""Test del modulo `clv_validation` — tutti OFFLINE, DB SQLite temporaneo.

Cosa difendono questi test (ognuno nasce da un modo reale di sbagliare):

* la formula del CLV e' quella di `market_calib` su OGNI riga (una seconda
  definizione divergerebbe in silenzio);
* `closing_quota` e `pinnacle_quota` NON sono chiusure: usarle produrrebbe un
  CLV che misura un'altra cosa e con il segno sbagliato;
* l'eco del segnale (chiusura == quota d'acquisto) e' un artefatto a CLV 0 e
  non un campione;
* duplicati e righe non-live si CONTANO, non spariscono dal denominatore;
* le coorti si applicano tutte, in ordine, senza scegliere quelle vincenti;
* il verdetto non promuove nulla senza campione prospettico;
* i tre artefatti (MD/JSON/CSV) contengono le colonne della richiesta.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest

from research_isolated.lab import clv_validation as cv


# ---------------------------------------------------------------------------
# Fixture: uno snapshot sintetico con lo schema che il modulo legge
# ---------------------------------------------------------------------------

DDL = """
CREATE TABLE matches (id TEXT PRIMARY KEY, home_team TEXT, away_team TEXT,
                      commence_time TEXT, league TEXT);
CREATE TABLE bets (id INTEGER PRIMARY KEY, match_id TEXT, esito TEXT, price REAL,
                   stake REAL, mode TEXT, status TEXT, market_id TEXT,
                   selection_id TEXT, esito_finale TEXT, profit REAL,
                   created_at TEXT, settled_at TEXT, home TEXT, away TEXT,
                   league TEXT, commence TEXT, mercato TEXT);
CREATE TABLE clv_history (match_id TEXT, esito TEXT, signal_quota REAL,
                          closing_quota REAL, pinnacle_quota REAL,
                          closing_odds REAL, closing_source TEXT,
                          betfair_odds REAL, updated_at TEXT);
CREATE TABLE market_quotes (fixture_id TEXT, market_type TEXT, line REAL,
                            line_key TEXT, selection TEXT, price REAL,
                            liquidity REAL);
CREATE TABLE match_results (match_id TEXT, home_team TEXT, away_team TEXT,
                            score_home INTEGER, score_away INTEGER,
                            settled_at TEXT);
"""


@contextmanager
def ledger(path: Path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(DDL)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def add_bet(conn, *, bet_id=1, match_id="m1", esito="1", price=1.90,
            mode="live", created_at="2026-10-14T10:00:00+00:00",
            commence="2026-10-14T18:00:00+00:00", home="Alpha", away="Beta",
            mercato="1X2", stake=2.0, profit=None, esito_finale=None):
    conn.execute(
        "INSERT INTO bets (id, match_id, esito, price, stake, mode, status, "
        "market_id, selection_id, esito_finale, profit, created_at, settled_at, "
        "home, away, league, commence, mercato) VALUES "
        "(?,?,?,?,?,?,'FULLY_FILLED','0xabc','1',?,?,?,NULL,?,?,'Test League',?,?)",
        (bet_id, match_id, esito, price, stake, mode, esito_finale, profit,
         created_at, home, away, commence, mercato))


def add_match(conn, match_id="m1", home="Alpha", away="Beta",
              commence="2026-10-14T18:00:00+00:00", league="Test League"):
    conn.execute("INSERT INTO matches VALUES (?,?,?,?,?)",
                 (match_id, home, away, commence, league))


def add_clv(conn, *, match_id="m1", esito="1", signal_quota=1.90,
            closing_odds=1.80, closing_source="pinnacle_t0",
            pinnacle_quota=None, closing_quota=None,
            updated_at="2026-10-14T17:55:00+00:00", betfair_odds=None):
    conn.execute(
        "INSERT INTO clv_history (match_id, esito, signal_quota, closing_quota, "
        "pinnacle_quota, closing_odds, closing_source, betfair_odds, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (match_id, esito, signal_quota, closing_quota, pinnacle_quota,
         closing_odds, closing_source, betfair_odds, updated_at))


@pytest.fixture()
def db(tmp_path):
    """Uno snapshot con 3 puntate misurabili, 1 eco, 1 senza chiusura, 1 sim."""
    p = tmp_path / "snap.db"
    with ledger(p) as conn:
        for i in (1, 2, 3, 4, 5, 6):
            add_match(conn, match_id=f"m{i}", home=f"Home{i}", away=f"Away{i}")
        # 1: CLV positivo (+5.56%)
        add_bet(conn, bet_id=1, match_id="m1", price=1.90)
        add_clv(conn, match_id="m1", signal_quota=1.90, closing_odds=1.80)
        # 2: CLV negativo (-2.70%)
        add_bet(conn, bet_id=2, match_id="m2", price=1.80)
        add_clv(conn, match_id="m2", signal_quota=1.80, closing_odds=1.85)
        # 3: CLV positivo (+3.03%)
        add_bet(conn, bet_id=3, match_id="m3", price=1.70)
        add_clv(conn, match_id="m3", signal_quota=1.70, closing_odds=1.65)
        # 4: eco del segnale (chiusura == quota d'acquisto) -> escluso
        add_bet(conn, bet_id=4, match_id="m4", price=1.60)
        add_clv(conn, match_id="m4", signal_quota=1.60, closing_odds=1.60)
        # 5: solo il riferimento sharp, nessuna chiusura -> escluso
        add_bet(conn, bet_id=5, match_id="m5", price=2.10)
        add_clv(conn, match_id="m5", signal_quota=2.10, closing_odds=None,
                pinnacle_quota=2.05)
        # 6: simulata -> mai nel campione del denaro reale
        add_bet(conn, bet_id=6, match_id="m6", price=1.95, mode="sim")
        add_clv(conn, match_id="m6", signal_quota=1.95, closing_odds=1.80)
    return p


def observations(db_path, **kw):
    return cv.build_observations(db=db_path, **kw)


# ---------------------------------------------------------------------------
# 1. La formula
# ---------------------------------------------------------------------------

class TestFormula:
    def test_definizione_e_segno(self):
        """CLV = acquisto/chiusura - 1: positivo = prezzo migliore del nostro."""
        assert cv.clv_gross(1.90, 1.80) == pytest.approx(1 / 1.8 * 1.9 - 1)
        assert cv.clv_gross(1.80, 1.90) < 0

    def test_coincide_con_market_calib(self):
        from market_calib import clv_raw
        for buy, close in ((1.3, 1.25), (2.5, 2.4), (1.9, 1.95)):
            assert cv.clv_gross(buy, close) == pytest.approx(clv_raw(buy, close))

    @pytest.mark.parametrize("buy,close", [(None, 1.8), (1.8, None), (1.0, 1.8),
                                           (1.8, 1.0), (0.5, 1.8)])
    def test_input_impossibili_danno_none(self, buy, close):
        assert cv.clv_gross(buy, close) is None

    def test_le_due_orientazioni_non_coincidono(self):
        """Il reperto di `clv_execution`: scambiare gli operandi cambia il segno."""
        block = cv.sign_sanity(1.90, 1.80)
        assert block["forward"] > 0 > block["inverted"]
        assert abs(block["forward"] - block["inverted"]) > 0

    def test_formula_verificata_riga_per_riga(self, db):
        ds = observations(db)
        check = cv.crosscheck_formula(ds["observations"])
        assert check["ok"] is True
        assert check["checked"] == 3
        assert check["mismatches"] == []


# ---------------------------------------------------------------------------
# 2. Quali colonne sono davvero una chiusura
# ---------------------------------------------------------------------------

class TestChiusura:
    def test_solo_closing_odds_e_una_chiusura(self):
        row = {"closing_quota": 1.55, "pinnacle_quota": 1.50}
        assert cv.closing_of(row)[0] is None
        assert cv.reference_of(row)[0] == 1.50

    def test_closing_odds_con_fonte_dichiarata(self):
        row = {"closing_odds": 1.72, "closing_source": "pinnacle_t0"}
        value, label = cv.closing_of(row)
        assert value == pytest.approx(1.72)
        assert label == "pinnacle_t0"
        assert cv.classify_source(label, None) == cv.CLASS_SHARP_DECLARED

    def test_fonte_non_dichiarata_resta_legacy(self):
        value, label = cv.closing_of({"closing_odds": 1.72})
        assert value == pytest.approx(1.72)
        assert label == "closing_odds?"
        assert cv.classify_source(label, None) == cv.CLASS_SHARP_LEGACY

    def test_chiusura_dell_exchange(self):
        row = {"closing_odds": 1.80, "closing_source": "sx"}
        assert cv.classify_source(cv.closing_of(row)[1], None) == cv.CLASS_VENUE

    def test_riga_con_solo_riferimento_finisce_in_reference_only(self, db):
        ds = observations(db)
        assert ds["excluded"].get(cv.EXCL_REFERENCE_ONLY) == 1

    def test_eco_esclusa_e_contata(self, db):
        ds = observations(db)
        assert ds["excluded"].get(cv.EXCL_ECHO) == 1

    def test_eco_includibile_solo_su_richiesta(self, db):
        ds = observations(db, include_echo=True)
        assert ds["excluded"].get(cv.EXCL_ECHO) is None
        assert len(ds["observations"]) == 4

    def test_clv_vig_free_solo_con_mercato_completo(self):
        """Con meno di due esiti il margine verrebbe attribuito in silenzio."""
        assert cv.fair_closing_odds([1.80], 0) is None
        assert cv.fair_closing_odds([1.80, 2.20, 3.50], 0) is not None
        assert cv.fair_closing_odds([1.80, None, 3.50], 0) is None


# ---------------------------------------------------------------------------
# 3. Denominatore esplicito: niente esclusioni silenziose
# ---------------------------------------------------------------------------

class TestDenominatore:
    def test_dataset(self, db):
        ds = observations(db)
        assert len(ds["observations"]) == 3
        assert ds["excluded"][cv.EXCL_ECHO] == 1
        assert ds["excluded"][cv.EXCL_REFERENCE_ONLY] == 1
        assert ds["error"] is None

    def test_le_simulate_restano_fuori_dal_campione(self, db):
        """La corsia SIM alimenta ML/CLV ma non e' denaro: non entra qui."""
        ds = observations(db)
        assert ds["totals"]["live_bets"] == 5
        assert all(o.bet_id != 6 for o in ds["observations"])
        assert cv.EXCL_NOT_LIVE not in ds["totals"]["excluded"]

    def test_scansione_totale_coerente(self, db):
        ds = observations(db)
        excluded = sum(ds["excluded"].values())
        assert ds["totals"]["live_bets"] == 5
        assert len(ds["observations"]) + excluded == ds["totals"]["live_bets"]

    def test_clv_positivo_calcolato(self, db):
        ds = observations(db)
        vals = sorted(o.clv_gross for o in ds["observations"])
        assert vals[0] == pytest.approx(1.80 / 1.85 - 1, abs=1e-9)
        assert vals[-1] == pytest.approx(1.90 / 1.80 - 1, abs=1e-9)

    def test_duplicato_stesso_evento_esito_non_raddoppia(self, tmp_path):
        p = tmp_path / "dup.db"
        with ledger(p) as conn:
            add_match(conn, match_id="mx")
            add_bet(conn, bet_id=1, match_id="mx", price=1.90)
            add_bet(conn, bet_id=2, match_id="mx", price=1.88)
            add_clv(conn, match_id="mx", signal_quota=1.90, closing_odds=1.80)
        ds = observations(p)
        assert len(ds["observations"]) == 1
        assert ds["excluded"][cv.EXCL_DUPLICATE] == 1

    def test_db_assente_non_solleva(self, tmp_path):
        ds = cv.build_observations(db=tmp_path / "nope.db")
        assert ds["error"] is not None
        assert ds["observations"] == []

    def test_schema_vecchio_senza_colonne_evento_dichiara_degrado(self, tmp_path):
        p = tmp_path / "old.db"
        conn = sqlite3.connect(p)
        conn.executescript(
            "CREATE TABLE bets (id INTEGER PRIMARY KEY, match_id TEXT, esito TEXT,"
            " price REAL, stake REAL, mode TEXT, created_at TEXT, profit REAL,"
            " esito_finale TEXT, mercato TEXT);"
            "CREATE TABLE clv_history (match_id TEXT, esito TEXT,"
            " signal_quota REAL, closing_odds REAL, updated_at TEXT);"
            "CREATE TABLE matches (id TEXT, home_team TEXT, away_team TEXT,"
            " commence_time TEXT, league TEXT);")
        conn.execute("INSERT INTO matches VALUES ('m1','A','B',"
                     "'2026-10-14T18:00:00+00:00','L')")
        conn.execute("INSERT INTO bets VALUES (1,'m1','1',1.90,2.0,'live',"
                     "'2026-10-14T10:00:00+00:00',NULL,NULL,'1X2')")
        conn.execute("INSERT INTO clv_history VALUES ('m1','1',1.90,1.80,"
                     "'2026-10-14T17:55:00+00:00')")
        conn.commit()
        conn.close()
        ds = observations(p)
        assert len(ds["observations"]) == 1
        assert any("home" in d for d in ds["degraded"])
        assert any("closing_source" in d for d in ds["degraded"])


# ---------------------------------------------------------------------------
# 4. Coorti preregistrate
# ---------------------------------------------------------------------------

class TestCoorti:
    def test_protocollo_congelato_leggibile(self):
        prereg = cv.load_prereg()
        assert prereg["error"] is None
        assert prereg["sha256"] and len(prereg["sha256"]) == 64
        assert prereg["errors"] == []
        assert prereg["hypothesis"]["id"]
        assert prereg["hypothesis"]["orders_allowed"] is False

    def test_una_sola_coorte_primaria(self):
        prereg = cv.load_prereg()
        primary = [c for c in prereg["cohorts"] if c.get("primary")]
        assert len(primary) == 1

    @pytest.mark.parametrize("field", ["result", "profit", "clv_gross"])
    def test_mai_predicati_sul_risultato(self, field):
        """Un predicato che usa l'esito sarebbe una selezione a posteriori."""
        bad = cv.validate_cohorts([{"id": "X", "where": [
            {"field": field, "eq": "won"}]}])
        assert bad and field in bad[0]

    def test_coorte_senza_predicato_rifiutata(self):
        assert cv.validate_cohorts([{"id": "Y"}])

    def test_tutte_le_coorti_valutate_in_ordine(self, db):
        ds = observations(db)
        prereg = cv.load_prereg()
        cohorts = cv.evaluate_cohorts(ds["observations"], prereg["cohorts"])
        ids = [c["id"] for c in cohorts]
        assert ids == [c["id"] for c in prereg["cohorts"]]
        for c in cohorts:
            assert "n" in c and "status" in c and "roi" in c

    def test_coorte_primaria_e_l_universo(self, db):
        ds = observations(db)
        prereg = cv.load_prereg()
        cohorts = {c["id"]: c for c in cv.evaluate_cohorts(
            ds["observations"], prereg["cohorts"])}
        assert cohorts["C1"]["n"] == 3

    def test_campione_piccolo_non_conclude(self, db):
        ds = observations(db)
        prereg = cv.load_prereg()
        cohorts = cv.evaluate_cohorts(ds["observations"], prereg["cohorts"])
        assert all(c["status"] != cv.STATUS_VERIFIED for c in cohorts)


# ---------------------------------------------------------------------------
# 5. Trappole misurate
# ---------------------------------------------------------------------------

class TestTrappole:
    def test_echo_impact_distingue_echo_e_seme(self, tmp_path):
        """Due trappole diverse: il seme (closing_quota copiata) e l'eco vera."""
        p = tmp_path / "echo.db"
        with ledger(p) as conn:
            # seme: closing_quota == signal_quota, ma la chiusura vera e' diversa
            add_clv(conn, match_id="m1", signal_quota=1.90, closing_quota=1.90,
                    closing_odds=1.80)
            # eco vera: la chiusura (closing_odds) coincide con l'acquisto
            add_clv(conn, match_id="m2", signal_quota=1.60, closing_quota=1.60,
                    closing_odds=1.60)
        conn = sqlite3.connect(p)
        conn.row_factory = sqlite3.Row
        try:
            block = cv.echo_impact(conn)
            assert block["available"] is True
            assert block["rows"] == 2
            assert block["echo"] == 1          # solo la seconda e' un'eco
            assert block["seme_copy"] == 2     # entrambe hanno il seme copiato
            assert block["share"] == pytest.approx(0.5)
        finally:
            conn.close()

    def test_provenance_distingue_chiusure_e_riferimenti(self, db):
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        try:
            block = cv.provenance(conn)
            assert block.get("closing:sharp_declared", 0) >= 3
            assert sum(block.values()) == 6
        finally:
            conn.close()

    def test_selection_bias_misura_la_copertura(self, db):
        """Confronta chi ha la chiusura con chi non ce l'ha: se il ROI diverge,
        il campione non descrive la strategia."""
        ds = observations(db)
        all_bets = [
            {"id": 1, "profit": 1.0, "stake": 2.0},
            {"id": 2, "profit": -2.0, "stake": 2.0},
            {"id": 3, "profit": 0.5, "stake": 2.0},
            {"id": 5, "profit": 6.0, "stake": 2.0},
        ]
        bias = cv.selection_bias(ds["observations"], all_bets)
        assert bias["measured"]["n"] == 3
        assert bias["missing"]["n"] == 1
        assert bias["coverage"] == pytest.approx(0.75)

    def test_gradiente_per_quota(self, db):
        ds = observations(db)
        grad = cv.odds_gradient(ds["observations"])
        assert grad["n"] == 3
        assert grad["note"]

    def test_shopping_e_il_prezzo_pagato_vs_segnale(self, db):
        ds = observations(db)
        block = cv.shopping_block(ds["observations"])
        assert block["n"] >= 1
        assert -1 < block["mean"] < 1


# ---------------------------------------------------------------------------
# 4-bis. Provenienza del prezzo di riferimento e distanza della chiusura
# ---------------------------------------------------------------------------

class TestRiferimentoEProssimita:
    """Ogni quota deve dichiarare DA DOVE viene e QUANTO e' vicina al fischio.

    Due modi di sbagliare che questi test bloccano: (a) chiamare "riferimento"
    una `closing_quota` che e' una COPIA della quota d'acquisto (CLV ~0 per
    costruzione); (b) leggere un `capture_lag_min` negativo come "chiusura
    catturata 8 minuti DOPO il fischio" quando significa 8 minuti PRIMA, cioe'
    proprio cio' che rende quella quota una closing line.
    """

    def test_la_colonna_di_riferimento_viaggia_con_la_riga(self, tmp_path):
        p = tmp_path / "ref.db"
        with ledger(p) as conn:
            add_match(conn, match_id="m1")
            add_bet(conn, bet_id=1, match_id="m1", price=1.90)
            add_clv(conn, match_id="m1", signal_quota=1.90, closing_odds=1.80,
                    pinnacle_quota=1.85, closing_quota=1.90)
            add_match(conn, match_id="m2")
            add_bet(conn, bet_id=2, match_id="m2", price=1.70)
            # solo il seme: nessun riferimento sharp a ledger
            add_clv(conn, match_id="m2", signal_quota=1.70, closing_odds=1.65,
                    closing_quota=1.70)
        obs = {o.match_id: o for o in observations(p)["observations"]}
        assert obs["m1"].reference_source == "pinnacle_quota"
        assert obs["m1"].reference_odds == pytest.approx(1.85)
        # il seme e' riconoscibile come tale, non indistinguibile da un book
        assert obs["m2"].reference_source == "closing_quota"

    def test_il_seme_non_passa_per_un_benchmark(self, tmp_path):
        p = tmp_path / "ref2.db"
        with ledger(p) as conn:
            add_match(conn, match_id="m1")
            add_bet(conn, bet_id=1, match_id="m1", price=1.70)
            add_clv(conn, match_id="m1", signal_quota=1.70, closing_odds=1.65,
                    closing_quota=1.70)
        obs = observations(p)["observations"]
        block = cv.reference_quality(obs)
        assert block["by_source"] == {"closing_quota": 1}
        assert block["seme_copy"] == 1
        # il ledger NON ha un timestamp per il riferimento: si dichiara
        assert block["timestamp_available"] is False
        assert block["note"]

    def test_il_csv_espone_la_fonte_del_riferimento(self, db):
        assert "reference_source" in cv.CSV_FIELDS
        row = observations(db)["observations"][0].as_row()
        assert "reference_source" in row

    def test_distanza_dal_fischio_e_in_minuti_PRIMA(self):
        a = _obs(1.90, 0.05, "2026-10-14")
        a.capture_lag_min = -8.0
        b = _obs(1.90, 0.04, "2026-10-15", match_id="m2")
        b.capture_lag_min = -6.0
        late = _obs(1.90, 0.01, "2026-10-16", match_id="m3")
        late.capture_lag_min = 3.0            # dopo il fischio: prezzo in-play
        block = cv.closing_quality([a, b, late])
        assert block["n"] == 3
        # la mediana e' il valore centrale di [-8, -6, 3]: 6 minuti PRIMA
        assert block["median_min_before"] == pytest.approx(6.0)
        assert block["share_after_kickoff"] == pytest.approx(1 / 3)
        solo_early = cv.closing_quality([a])
        assert solo_early["median_min_before"] == pytest.approx(8.0)
        assert solo_early["share_after_kickoff"] == 0.0

    def test_senza_kickoff_la_distanza_non_si_inventa(self):
        o = _obs(1.90, 0.05, "2026-10-14")
        o.capture_lag_min = None
        block = cv.closing_quality([o])
        assert block["n"] == 0
        assert block["median_min_before"] is None
        assert "non misurabile" in block["note"]


# ---------------------------------------------------------------------------
# 5-bis. Definizioni alternative e stabilita' del campione
# ---------------------------------------------------------------------------

def _obs(odds_buy, clv, day, match_id="m1", vig_free=None):
    return cv.Observation(
        obs_id=f"{match_id}:{day}", bet_id=1, event_id="e", match_id=match_id,
        sport="football", market="1X2", selection="1", odds_buy=odds_buy,
        buy_ts=f"{day}T10:00:00+00:00", odds_closing=odds_buy / (1 + clv),
        closing_ts=f"{day}T17:00:00+00:00", closing_column="closing_odds",
        closing_source="pinnacle_t0", closing_source_class=cv.CLASS_SHARP_DECLARED,
        clv_gross=clv, clv_vig_free=vig_free, vig_method=None, signal_quota=odds_buy,
        reference_odds=None, reference_source=None, liquidity_usdc=None,
        liquidity_source="n/d",
        result="won", profit=1.0, stake=2.0, kickoff=f"{day}T18:00:00+00:00",
        lead_h=8.0, capture_lag_min=5.0)


class TestDefinizioniAlternative:
    def test_le_tre_definizioni_danno_numeri_diversi(self, tmp_path):
        p = tmp_path / "alt.db"
        with ledger(p) as conn:
            add_match(conn, match_id="m1")
            add_bet(conn, bet_id=1, match_id="m1", price=1.90)
            add_clv(conn, match_id="m1", signal_quota=1.90, closing_odds=1.80,
                    pinnacle_quota=1.85, closing_quota=1.90)
            obs = [cv.Observation(
                obs_id="1", bet_id=1, event_id="m1", match_id="m1",
                sport="football", market="1X2", selection="1", odds_buy=1.90,
                buy_ts="2026-10-14T10:00:00+00:00", odds_closing=1.80,
                closing_ts="2026-10-14T17:00:00+00:00",
                closing_column="closing_odds", closing_source="pinnacle_t0",
                closing_source_class=cv.CLASS_SHARP_DECLARED,
                clv_gross=cv.clv_gross(1.90, 1.80), clv_vig_free=None,
                vig_method=None, signal_quota=1.90, reference_odds=1.85,
                reference_source="pinnacle_quota", liquidity_usdc=None,
                liquidity_source="n/d", result=None,
                profit=None, stake=None, kickoff=None, lead_h=None,
                capture_lag_min=None)]
            conn.row_factory = sqlite3.Row
            block = cv.definition_variants(obs, conn)
        assert block["closing_odds"]["mean"] == pytest.approx(1.90 / 1.80 - 1)
        assert block["reference_as_closing"]["mean"] == pytest.approx(1.90 / 1.85 - 1)
        # il seme e' la quota d'acquisto: CLV ~0, non un campione
        assert abs(block["seme_as_closing"]["mean"]) < 1e-9
        assert block["closing_odds"]["n"] == 1

    def test_senza_righe_di_clv_resta_vuoto(self, tmp_path):
        p = tmp_path / "vuoto.db"
        with ledger(p) as conn:
            conn.row_factory = sqlite3.Row
            block = cv.definition_variants([], conn)
        assert block["closing_odds"]["n"] == 0
        assert block["reference_as_closing"]["mean"] is None


class TestCumulata:
    def test_serie_vuota(self):
        block = cv.cumulative_series([])
        assert block["available"] is False
        assert block["points"] == []

    def test_cumulata_cresce_in_n(self):
        obs = [_obs(1.90, 0.03, "2026-10-0%d" % (i + 1)) for i in range(3)]
        block = cv.cumulative_series(obs)
        assert block["available"] is True
        assert [p["n"] for p in block["points"]] == [1, 2, 3]

    def test_campione_che_degrada(self):
        """Prima meta' positiva, seconda meta' negativa: la cumulata scende."""
        obs = [_obs(1.90, 0.05, "2026-10-%02d" % (i + 1)) for i in range(15)]
        obs += [_obs(1.90, -0.02, "2026-10-%02d" % (i + 16)) for i in range(15)]
        block = cv.cumulative_series(obs)
        assert block["stability"]["delta"] < 0
        assert "scende" in block["note"]
        assert block["crossing"]["n"] >= cv.MIN_SAMPLES

    def test_campione_stabile(self):
        obs = [_obs(1.90, 0.03, "2026-10-%02d" % (i + 1)) for i in range(30)]
        block = cv.cumulative_series(obs)
        assert abs(block["stability"]["delta"]) < 1e-9
        assert "stesso numero" in block["note"]


class TestNumeroCitato:
    """Il +4,54% citato deve essere RITROVATO nella serie, non riscritto.

    La domanda della verifica indipendente e' "quel numero e' vero?": se le
    prime N osservazioni non danno il valore citato (entro la tolleranza
    dichiarata) il numero e' un errore di calcolo, non una fotografia.
    """

    def test_riproduce_il_valore_al_campione_citato(self):
        # 45 osservazioni tutte a +4,54%: il citato deve risultare riprodotto.
        obs = [_obs(1.90, cv.CITED_CLV, "2026-10-01") for _ in range(cv.CITED_SAMPLE_N)]
        block = cv.cited_reproduction(obs)
        assert block["available"] is True
        assert block["n"] == cv.CITED_SAMPLE_N
        assert block["mean"] == pytest.approx(cv.CITED_CLV)
        assert block["reproduced"] is True

    def test_un_campione_diverso_non_riproduce(self):
        obs = [_obs(1.90, 0.02, "2026-10-01") for _ in range(cv.CITED_SAMPLE_N)]
        block = cv.cited_reproduction(obs)
        assert block["reproduced"] is False
        assert block["delta_pp"] < 0

    def test_usa_le_PRIME_osservazioni_per_data_di_acquisto(self):
        """Ordine per `buy_ts`, non per id: le prime N sono le piu' vecchie."""
        early = [_obs(1.90, 0.05, "2026-10-01", match_id=f"a{i}")
                 for i in range(cv.CITED_SAMPLE_N)]
        late = [_obs(1.90, -0.05, "2026-10-19", match_id=f"b{i}") for i in range(5)]
        block = cv.cited_reproduction(late + early)   # ordine invertito
        assert block["mean"] == pytest.approx(0.05)
        assert block["current"] < 0.05                 # la serie intera e' piu' bassa

    def test_serie_troppo_corta_e_dichiarata(self):
        obs = [_obs(1.90, 0.04, "2026-10-01") for _ in range(5)]
        block = cv.cited_reproduction(obs)
        assert block["available"] is False
        assert block["n"] == 5
        assert "parziale" in block["note"]

    def test_serie_vuota_non_inventa(self):
        block = cv.cited_reproduction([])
        assert block["mean"] is None and block["current"] is None
        assert block["reproduced"] is None


class TestVigFreeAppaiato:
    """Vig-free e grezzo vivono su campioni diversi: vanno APPAIATI."""

    def test_il_grezzo_si_isola_sugli_stessi_campioni(self):
        obs = [_obs(1.90, 0.10, "2026-10-01", match_id="m1", vig_free=0.02),
               _obs(1.90, 0.02, "2026-10-02", match_id="m2")]
        cohorts = [{"id": "C1", "primary": True, "all": True, "where": []}]
        block = cv.evaluate_cohorts(obs, cohorts)[0]
        assert block["vig_free"]["n"] == 1
        assert block["vig_free"]["mean"] == pytest.approx(0.02)
        # il grezzo sugli stessi campioni e' 10%, non la media del 6%
        assert block["vig_free"]["gross_on_same_n"] == 1
        assert block["vig_free"]["gross_on_same"] == pytest.approx(0.10)
        assert block["mean"] == pytest.approx(0.06)

    def test_senza_vig_free_il_confronto_non_esiste(self):
        obs = [_obs(1.90, 0.03, "2026-10-01")]
        cohorts = [{"id": "C1", "all": True, "where": []}]
        block = cv.evaluate_cohorts(obs, cohorts)[0]
        assert block["vig_free"]["n"] == 0
        assert block["vig_free"]["mean"] is None
        assert block["vig_free"]["gross_on_same"] is None


# ---------------------------------------------------------------------------
# 6. Verdetto: il calcolo puo' essere verificato, la strategia no
# ---------------------------------------------------------------------------

def _prereg_cohorts(status=None, n=40):
    status = status or cv.STATUS_VERIFIED
    return [{"id": "C1", "primary": True, "n": n, "status": status,
             "mean": 0.03, "ci95": [0.01, 0.05], "roi": {}, "vig_free": {}},
            {"id": "C11", "n": 10, "status": cv.STATUS_INSUFFICIENT,
             "mean": -0.005, "ci95": [-0.03, 0.02], "roi": {}, "vig_free": {}}]


def _checks(prospective_ready=False, second=True, monotone=False, formula_ok=True):
    return {"formula": {"ok": formula_ok, "checked": 50, "mismatches": []},
            "crosscheck": {"match": formula_ok},
            "prospective": {"ready": prospective_ready, "days": 0},
            "second_benchmark": {"available": second, "n": 5 if second else 0},
            "gradient": {"monotone": monotone}}


def test_verdetto_senza_prospettiva_non_promuove():
    """La direttiva: nessuna promozione sulla sola base del CLV storico."""
    block = cv.verdict({}, _prereg_cohorts(), _checks(prospective_ready=False),
                       {"coverage": 0.95})
    assert block["calculation_verified"] is True
    assert block["replicable"] is False
    assert block["promotion_allowed"] is False
    assert block["overall"] in ("dati_insufficienti", "non_verificato")
    assert any("PROSPETTICO" in r for r in block["reasons"])


def test_verdetto_calcolo_non_riproducibile():
    block = cv.verdict({}, _prereg_cohorts(),
                       _checks(formula_ok=False), {"coverage": 0.95})
    assert block["calculation_verified"] is False
    assert block["overall"] == "non_verificato"


def test_verdetto_replicabile_solo_con_tutti_i_requisiti():
    block = cv.verdict({}, _prereg_cohorts(),
                       _checks(prospective_ready=True), {"coverage": 0.95})
    assert block["replicable"] is True
    assert block["overall"] == "verificato"
    assert block["promotion_allowed"] is False   # comunque mai "solo CLV"


def test_verdetto_copertura_bassa_boccia_la_replica():
    block = cv.verdict({}, _prereg_cohorts(),
                       _checks(prospective_ready=True), {"coverage": 0.60})
    assert block["replicable"] is False
    assert any("copertura" in r for r in block["reasons"])


def test_verdetto_gradiente_monotono_e_un_avviso():
    block = cv.verdict({}, _prereg_cohorts(),
                       _checks(prospective_ready=True, monotone=True),
                       {"coverage": 0.95})
    assert any("MONOTONO" in r for r in block["reasons"])


def test_verdetto_usa_i_dati_veri(db, tmp_path, monkeypatch):
    """Sul dataset sintetico il calcolo e' verificato ma la replica non c'e'."""
    monkeypatch.setenv("QV_CLV_SHADOW_LOG", str(tmp_path / "none.jsonl"))
    res = cv.run(db=db, out_dir=tmp_path / "out", write=False)
    assert res["verdict"]["calculation_verified"] is True
    assert res["verdict"]["replicable"] is False
    assert res["verdict"]["promotion_allowed"] is False
    assert res["verdict"]["overall"] in ("dati_insufficienti", "non_verificato")


# ---------------------------------------------------------------------------
# 7. Artefatti
# ---------------------------------------------------------------------------

REQUIRED_COLUMNS = ("event_id", "sport", "market", "selection", "odds_buy",
                    "buy_ts", "odds_closing", "closing_ts", "closing_source",
                    "reference_odds", "reference_source", "clv_gross",
                    "liquidity_usdc", "result", "kickoff", "capture_lag_min")


def test_csv_ha_le_colonne_richieste():
    for column in REQUIRED_COLUMNS:
        assert column in cv.CSV_FIELDS, column


def test_output_scritti_e_leggibili(db, tmp_path, monkeypatch):
    monkeypatch.setenv("QV_CLV_SHADOW_LOG", str(tmp_path / "none.jsonl"))
    out = tmp_path / "out"
    res = cv.run(db=db, out_dir=out, write=True)
    for name in ("CLV_VALIDATION.md", "CLV_VALIDATION.json",
                 "CLV_OBSERVATIONS.csv"):
        assert (out / name).exists(), name
    payload = json.loads((out / "CLV_VALIDATION.json").read_text())
    assert payload["verdict"]["promotion_allowed"] is False
    assert payload["dataset"]["measured"] == 3
    assert "observations" not in payload
    # il riepilogo DICHIARA dove stanno le righe: un JSON senza osservazioni
    # non deve sembrare un campione perso (ne' far cercare il file sbagliato)
    assert payload["artifacts"]["observations_csv"] == "CLV_OBSERVATIONS.csv"
    assert payload["artifacts"]["observations_n"] == 3
    assert set(REQUIRED_COLUMNS) <= set(payload["artifacts"]["csv_fields"])
    with (out / "CLV_OBSERVATIONS.csv").open() as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == res["dataset"]["measured"]
    assert set(REQUIRED_COLUMNS) <= set(rows[0].keys())
    assert rows[0]["event_id"]
    assert rows[0]["sport"]


def test_report_dichiara_i_limiti_e_il_verdetto(db, tmp_path, monkeypatch):
    monkeypatch.setenv("QV_CLV_SHADOW_LOG", str(tmp_path / "none.jsonl"))
    res = cv.run(db=db, out_dir=tmp_path / "o", write=False)
    text = cv.format_report(res)
    assert "Limiti" in text
    assert "Raccolta prospettica" in text
    assert "nessuna promozione" in text
    # il numero di osservazioni e' quello dei dati, non una costante scritta
    assert f"Le {res['dataset']['measured']} osservazioni" in text
    assert "58 osservazioni" not in text


def test_report_senza_dati_non_mente(db, tmp_path, monkeypatch):
    monkeypatch.setenv("QV_CLV_SHADOW_LOG", str(tmp_path / "none.jsonl"))
    res = cv.run(db=db, out_dir=tmp_path / "o", write=False)
    text = cv.format_report(res)
    overall = str(res["verdict"]["overall"]).upper().replace("_", " ")
    assert overall in text
    assert "calcolo riproducibile dalle colonne grezze: **sì**" in text
    assert "replicabile: **no**" in text


# ---------------------------------------------------------------------------
# 8. Confini: sola lettura, nessuna rete, nessun ordine
# ---------------------------------------------------------------------------

def test_connessione_e_read_only(db):
    from research_isolated.lab import loader
    conn = loader.connect(db)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("UPDATE bets SET stake = 999")
    finally:
        conn.close()


def _sql_statements(path: Path):
    """Le stringhe SQL ESEGUITE (non quelle citate nella prosa).

    Lezione del 30/09: un tripwire che cerca testo nel sorgente boccia la
    docstring. Qui si guarda solo il primo argomento di `execute*`.
    """
    import ast
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (func.attr if isinstance(func, ast.Attribute)
                else getattr(func, "id", ""))
        if name not in ("execute", "executescript", "executemany", "_safe_rows"):
            continue
        args = node.args[1:] if name == "_safe_rows" else node.args
        for arg in args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                if arg.value.strip().split()[:1]:
                    out.append(arg.value.strip())
                break
    return out


def test_nessuna_scrittura_nel_sorgente():
    statements = _sql_statements(Path(cv.__file__))
    assert statements, "nessuna query trovata: il tripwire non sta guardando nulla"
    for sql in statements:
        head = sql.split()[0].upper() if sql.split() else ""
        assert head in ("SELECT", "PRAGMA"), sql[:80]


def test_nessun_ordine_ne_rete_nel_sorgente():
    src = Path(cv.__file__).read_text(encoding="utf-8")
    for token in ("place_limit_order", "_live_fill", "requests.get",
                  "urlopen", "execution_engine"):
        assert token not in src, token


def test_import_leggero():
    """Importare il modulo non deve tirare dentro il percorso del denaro."""
    import subprocess
    import sys
    code = ("import sys; import research_isolated.lab.clv_validation;"
            "bad=[m for m in ('auto_bet','bot','tracker','odds_api','sx_signals')"
            " if m in sys.modules]; print(','.join(bad))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == ""
