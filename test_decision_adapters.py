"""Test dell'adapter Signal (`decision/adapters.py`) — OFFLINE.

L'adapter legge il ledger: qui il ledger e' un SQLite TEMPORANEO con le stesse
tabelle della produzione (`matches`, `predictions`, `match_analysis`,
`team_ratings`), cosi' il test copre anche la query vera senza toccare il DB di
produzione. Nessuna rete, nessun provider, nessuna scrittura.
"""

import sqlite3
from decimal import Decimal

import pytest

import value_filter as vf
from decision import KillSwitchStatus, ReasonCode, RiskLimits, decide
from decision.adapters import (
    FULL_SAMPLE, calibration_active, canonical_outcome, compute_confidence,
    iter_signals, model_coverage, signal_from_row,
)

ALLOWED_LEAGUE = "Premier League"


# ---------------------------------------------------------------------------
# Database temporaneo con lo schema di produzione
# ---------------------------------------------------------------------------

@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.execute("""CREATE TABLE matches (id TEXT PRIMARY KEY, league TEXT, home_team TEXT,
                 away_team TEXT, commence_time TEXT, status TEXT, last_updated TEXT)""")
    c.execute("""CREATE TABLE predictions (id INTEGER PRIMARY KEY AUTOINCREMENT, match_id TEXT,
                 mercato TEXT, esito TEXT, quota REAL, prob REAL, ev REAL, market_prob REAL,
                 market_edge REAL, status TEXT, esito_finale TEXT, profit REAL,
                 created_at TEXT, settled_at TEXT)""")
    c.execute("""CREATE TABLE match_analysis (id INTEGER PRIMARY KEY AUTOINCREMENT,
                 match_id TEXT, lam_h REAL, lam_a REAL, prob_1 REAL, prob_X REAL, prob_2 REAL,
                 prob_over REAL, best_ev REAL, best_esito TEXT, best_quota REAL,
                 best_bookmaker TEXT, status TEXT, timestamp TEXT, market_prob REAL,
                 market_edge REAL)""")
    c.execute("""CREATE TABLE team_ratings (team TEXT PRIMARY KEY, attack_home REAL,
                 defense_home REAL, attack_away REAL, defense_away REAL,
                 n_home INTEGER, n_away INTEGER)""")
    yield c
    c.close()


def add_match(conn, match_id="sx-1", home="Inter", away="Cagliari", league=ALLOWED_LEAGUE,
              kickoff="2026-09-14T18:45:00Z"):
    conn.execute("INSERT INTO matches (id, league, home_team, away_team, commence_time, status) "
                 "VALUES (?,?,?,?,?,'scheduled')", (match_id, league, home, away, kickoff))
    conn.commit()


def add_prediction(conn, match_id="sx-1", esito="1", quota=1.60, prob=0.65, ev=0.04,
                   market_prob=0.58, market_edge=0.07, status="strong_value",
                   esito_finale=None, mercato="1X2"):
    conn.execute("""INSERT INTO predictions (match_id, mercato, esito, quota, prob, ev,
                    market_prob, market_edge, status, esito_finale)
                    VALUES (?,?,?,?,?,?,?,?,?,?)""",
                 (match_id, mercato, esito, quota, prob, ev, market_prob, market_edge,
                  status, esito_finale))
    conn.commit()


def add_analysis(conn, match_id="sx-1", prob_1=0.62, prob_X=0.22, prob_2=0.16):
    conn.execute("""INSERT INTO match_analysis (match_id, lam_h, lam_a, prob_1, prob_X,
                    prob_2, status) VALUES (?,?,?,?,?,?,'analyzed')""",
                 (match_id, 1.6, 0.9, prob_1, prob_X, prob_2))
    conn.commit()


def add_rating(conn, team, n=12):
    conn.execute("INSERT INTO team_ratings (team, attack_home, defense_home, attack_away, "
                 "defense_away, n_home, n_away) VALUES (?,?,?,?,?,?,?)",
                 (team, 1.2, 0.9, 1.1, 1.0, n, max(n - 2, 0)))
    conn.commit()


def row(**kwargs) -> dict:
    """Riga di ledger come la produce l'adapter (dict con chiavi reali)."""
    base = {"id": "sx-1", "home_team": "Inter", "away_team": "Cagliari",
            "commence_time": "2026-09-14T18:45:00Z", "league": ALLOWED_LEAGUE,
            "esito": "1", "quota": 1.60, "prob": 0.65, "ev": 0.04,
            "market_prob": 0.58, "market_edge": 0.07, "status": "strong_value",
            "prob_1": 0.62, "prob_X": 0.22, "prob_2": 0.16}
    base.update(kwargs)
    return base


# ---------------------------------------------------------------------------
# 1. Mapping riga -> Signal
# ---------------------------------------------------------------------------

class TestSignalDaRiga:
    def test_mapping_completo(self):
        signal = signal_from_row(row(), coverage=1.0, calibrated=True)
        assert signal.match_id == "sx-1"
        # La quota e' `Money` (Decimal): la riga 1.60 del ledger deve arrivare
        # come `Decimal('1.6')`, non come il float binario 1.6000000000000001.
        assert signal.price == Decimal("1.6")
        assert isinstance(signal.price, Decimal)
        assert signal.blended_prob == pytest.approx(0.65)
        assert signal.model_prob == pytest.approx(0.62)      # da match_analysis
        assert signal.market_prob == pytest.approx(0.58)
        assert signal.edge == pytest.approx(0.07)
        assert signal.ev == pytest.approx(0.04)
        assert signal.tier == "strong_value"                 # dallo status
        assert signal.league == ALLOWED_LEAGUE
        assert signal.selection_label == "Inter (1)"
        assert signal.kickoff.year == 2026

    def test_tier_coerente_con_get_signal_tier(self):
        signal = signal_from_row(row(status="value", market_edge=0.03, ev=0.03))
        assert signal.tier == "value" == vf.get_signal_tier(0.03, 0.03)

    def test_tier_derivato_se_lo_status_e_ignoto(self):
        signal = signal_from_row(row(status="rejected"))
        assert signal.tier in ("value", "strong_value", "moderate")

    def test_model_prob_assente_avvisa_e_ricade_sul_blend(self):
        signal = signal_from_row(row(prob_1=None))
        assert signal.model_prob == pytest.approx(signal.blended_prob)
        assert "model_prob_assente" in signal.warnings

    def test_edge_calcolato_se_assente(self):
        signal = signal_from_row(row(market_edge=None))
        assert signal.edge == pytest.approx(0.65 - 0.58)
        assert "edge_calcolato" in signal.warnings

    def test_righe_inutilizzabili(self):
        assert signal_from_row(row(quota=None)) is None
        assert signal_from_row(row(quota=1.0)) is None
        assert signal_from_row(row(prob=None)) is None
        assert signal_from_row(row(market_prob=None)) is None
        assert signal_from_row(row(prob=1.4)) is None
        assert signal_from_row(row(esito="1X")) is None
        assert signal_from_row(row(id="")) is None

    def test_etichette_per_ogni_esito(self):
        assert signal_from_row(row(esito="1")).selection_label == "Inter (1)"
        assert signal_from_row(row(esito="2")).selection_label == "Cagliari (2)"
        assert "Pareggio" in signal_from_row(row(esito="X")).selection_label


class TestEsitoCanonico:
    """Il ledger NON e' omogeneo: `sx_signals` scrive '1'/'X'/'2',
    `fixture_engine` scrive il NOME DELLA SQUADRA giocata.

    Senza la normalizzazione l'adapter scartava i secondi come "esito non
    valido" — visto in PRODUZIONE il 15/09 (Atlético Madrid vs Osasuna: la
    riga scelta dalla catena era 'Atlético Madrid'; il vecchio codice la
    rifiutava e il confronto shadow misurava un insieme diverso da quello su
    cui la produzione scommette).
    """

    def test_forme_canoniche(self):
        for esito in ("1", "X", "2"):
            assert canonical_outcome(esito, "Inter", "Cagliari") == esito
        assert canonical_outcome("x", "Inter", "Cagliari") == "X"
        assert canonical_outcome("Pareggio", "Inter", "Cagliari") == "X"
        assert canonical_outcome("draw", "Inter", "Cagliari") == "X"

    def test_nome_squadra_diventa_esito(self):
        assert canonical_outcome("Inter", "Inter", "Cagliari") == "1"
        assert canonical_outcome("Cagliari", "Inter", "Cagliari") == "2"
        # caso reale della produzione (15/09)
        assert canonical_outcome("Atlético Madrid", "Atlético Madrid",
                                 "Osasuna") == "1"

    def test_nomi_risolti_e_codici_di_stato(self):
        """Confronto sul nome RISOLTO e tollerante (codici di stato, sigle)."""
        resolve = lambda n: {"Vila Nova": "Vila Nova GO"}.get(n, n)  # noqa: E731
        assert canonical_outcome("Vila Nova GO", "Vila Nova", "Ponte Preta",
                                 resolve=resolve) == "1"
        # resolver iniettabile che normalizza l'alias societario
        assert canonical_outcome("Spurs", "Tottenham", "Arsenal",
                                 resolve=lambda n: {"Spurs": "Tottenham"}.get(n, n)) == "1"

    def test_mai_indovinare(self):
        """Nome di una terza squadra, dato incoerente o nome vuoto: None
        (la riga si scarta come prima, nessun esito inventato)."""
        assert canonical_outcome("Milan", "Inter", "Cagliari") is None
        assert canonical_outcome("Inter", "Inter", "Inter") is None   # ambiguo
        assert canonical_outcome("", "Inter", "Cagliari") is None
        assert canonical_outcome("Inter", "", "") is None
        assert canonical_outcome("1X", "Inter", "Cagliari") is None

    def test_riga_con_nome_squadra_diventa_signal(self):
        """Regressione end-to-end: la riga con esito = nome squadra produce
        un Signal con l'esito canonico e la probabilita' del MODELLO giusta
        (`prob_1`, non `prob_X`)."""
        signal = signal_from_row(row(esito="Inter"), coverage=1.0,
                                 calibrated=True)
        assert signal is not None
        assert signal.outcome == "1"
        assert signal.selection_label == "Inter (1)"
        assert signal.model_prob == pytest.approx(0.62)
        assert signal_from_row(row(esito="Cagliari")).outcome == "2"


# ---------------------------------------------------------------------------
# 2. Copertura ratings e confidenza
# ---------------------------------------------------------------------------

class TestConfidence:
    def test_copertura(self):
        assert model_coverage(0, 0) == 0.0
        assert model_coverage(FULL_SAMPLE, FULL_SAMPLE) == 1.0
        assert model_coverage(FULL_SAMPLE, 0) == pytest.approx(0.5)
        assert model_coverage(FULL_SAMPLE * 3, FULL_SAMPLE * 3) == 1.0   # clamp
        assert model_coverage(None, None) == 0.0

    def test_crescere_con_i_segnali_di_qualita(self):
        limits = RiskLimits.from_env()
        blind = compute_confidence(model_coverage=0.0, calibrated=False, edge=0.07,
                                   ev=0.04, limits=limits)
        covered = compute_confidence(model_coverage=1.0, calibrated=False, edge=0.07,
                                     ev=0.04, limits=limits)
        calibrated = compute_confidence(model_coverage=1.0, calibrated=True, edge=0.07,
                                        ev=0.04, limits=limits)
        deep = compute_confidence(model_coverage=1.0, calibrated=True, edge=0.07,
                                  ev=0.04, depth_usdc=100.0, limits=limits)
        assert blind < covered < calibrated < deep
        assert deep <= 1.0

    def test_clv_negativo_abbassa(self):
        limits = RiskLimits.from_env()
        base = compute_confidence(model_coverage=1.0, calibrated=True, edge=0.03,
                                  ev=0.03, limits=limits)
        without = compute_confidence(model_coverage=1.0, calibrated=True, edge=0.03,
                                     ev=0.03, has_clv_positive=False, limits=limits)
        with_clv = compute_confidence(model_coverage=1.0, calibrated=True, edge=0.03,
                                      ev=0.03, has_clv_positive=True, limits=limits)
        assert without < base < with_clv

    def test_mai_fuori_range(self):
        limits = RiskLimits.from_env()
        assert compute_confidence(model_coverage=0.0, calibrated=False, edge=-1.0,
                                  ev=-1.0, limits=limits) == pytest.approx(0.35)
        assert compute_confidence(model_coverage=5.0, calibrated=True, edge=1.0, ev=1.0,
                                  depth_usdc=10_000, has_clv_positive=True,
                                  limits=limits) == 1.0

    def test_calibration_active_fail_safe_e_con_fake(self, monkeypatch):
        import ml_ensemble

        class Calibrator:
            fitted_ = True

        class FakeModel:
            calibrator = Calibrator()

        monkeypatch.setattr(ml_ensemble, "get_ensemble", lambda: FakeModel())
        assert calibration_active() is True

        monkeypatch.setattr(ml_ensemble, "get_ensemble",
                            lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        assert calibration_active() is False


# ---------------------------------------------------------------------------
# 3. Lettura del ledger (query vera su DB temporaneo)
# ---------------------------------------------------------------------------

class TestLetturaLedger:
    def test_solo_segnali_aperti_in_finestra(self, conn):
        add_match(conn, "sx-1")
        add_prediction(conn, "sx-1", status="strong_value")
        add_match(conn, "sx-2", home="Mainz", away="Union")
        add_prediction(conn, "sx-2", esito="1", quota=1.70, prob=0.62, ev=0.05,
                       market_prob=0.56, market_edge=0.06, status="value")
        # esclusi: rejected, mercato OU, gia' saldata
        add_prediction(conn, "sx-2", esito="X", quota=3.2, prob=0.30, ev=0.0,
                       market_prob=0.29, market_edge=0.01, status="rejected")
        add_match(conn, "sx-3", home="Roma", away="Lazio")
        add_prediction(conn, "sx-3", esito="1", mercato="Over/Under", status="value")
        add_match(conn, "sx-4", home="A", away="B")
        add_prediction(conn, "sx-4", esito="1", status="value", esito_finale="won")
        # fuori finestra
        add_match(conn, "sx-5", home="C", away="D", kickoff="2027-01-01T12:00:00Z")
        add_prediction(conn, "sx-5", esito="1", status="value")

        signals = iter_signals(conn=conn, calibrated=False,
                               now=__import__("datetime").datetime(2026, 9, 14, 12, 0,
                                                                   tzinfo=__import__("datetime").timezone.utc),
                               resolve=lambda name: name)
        # ev 0.05 (sx-2) sopra 0.04 (sx-1): ORDER BY ev DESC
        assert [s.match_id for s in signals] == ["sx-2", "sx-1"]
        assert signals[0].ev >= signals[1].ev

    def test_ledger_misto_nome_squadra_e_codice(self, conn):
        """Regressione della produzione (15/09): 4 righe aperte di cui UNA
        con esito = nome squadra non devono piu' diventare 3 segnali —
        quel segnale c'e' e la catena lo valuta come gli altri."""
        add_match(conn, "sx-1", home="Atlético Madrid", away="Osasuna")
        add_prediction(conn, "sx-1", esito="Atlético Madrid", quota=1.54,
                       prob=0.764, ev=0.177, market_prob=0.649, market_edge=0.134,
                       status="strong_value")
        add_match(conn, "sx-2", home="Mainz", away="Union")
        add_prediction(conn, "sx-2", esito="1", quota=1.70, prob=0.62, ev=0.05,
                       market_prob=0.56, market_edge=0.06, status="value")
        signals = iter_signals(conn=conn, calibrated=False,
                               now=_utc(2026, 9, 14, 12), hours=72,
                               resolve=lambda name: name)
        assert len(signals) == 2
        by_id = {s.match_id: s for s in signals}
        assert by_id["sx-1"].outcome == "1"
        assert by_id["sx-1"].tier == "strong_value"

    def test_copertura_dal_db(self, conn):
        add_match(conn, "sx-1")
        add_prediction(conn, "sx-1")
        add_rating(conn, "Inter", n=FULL_SAMPLE)
        add_rating(conn, "Cagliari", n=FULL_SAMPLE)
        signal = iter_signals(conn=conn, calibrated=True,
                              now=_utc(2026, 9, 14, 12),
                              resolve=lambda name: name)[0]
        assert signal.data_quality.model_coverage == 1.0
        # campione osservato = n_home + n_away (add_rating: n + max(n-2, 0))
        assert signal.data_quality.ratings_home_n == 2 * FULL_SAMPLE - 2
        assert signal.data_quality.ratings_away_n == 2 * FULL_SAMPLE - 2
        assert signal.confidence >= 0.80

    def test_squadre_senza_rating_abbassano_la_confidenza(self, conn):
        add_match(conn, "sx-1")
        add_prediction(conn, "sx-1")
        signal = iter_signals(conn=conn, calibrated=True,
                              now=_utc(2026, 9, 14, 12),
                              resolve=lambda name: name)[0]
        assert signal.data_quality.model_coverage == 0.0
        assert signal.data_quality.is_blind is True
        assert "ratings_assenti" in signal.warnings
        # la confidenza resta alta (il gate 1.60/58%/65% e' solido), ma la
        # copertura e' un gate a parte del Risk Engine: vedi il test e2e sotto
        covered = signal_from_row(row(), coverage=1.0, calibrated=True).confidence
        assert signal.confidence < covered

    def test_adapter_e_sola_lettura(self, conn):
        add_match(conn, "sx-1")
        add_prediction(conn, "sx-1")
        before = conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
        iter_signals(conn=conn, calibrated=False, now=_utc(2026, 9, 14, 12),
                     resolve=lambda name: name)
        assert conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0] == before
        source = open("decision/adapters.py", encoding="utf-8").read().upper()
        for verb in ("INSERT INTO", "UPDATE ", "DELETE FROM", "COMMIT("):
            assert verb not in source

    def test_team_ratings_illeggibile_non_esplode(self, conn):
        add_match(conn, "sx-1")
        add_prediction(conn, "sx-1")
        conn.execute("DROP TABLE team_ratings")
        signal = iter_signals(conn=conn, calibrated=False, now=_utc(2026, 9, 14, 12),
                              resolve=lambda name: name)[0]
        assert signal.data_quality.model_coverage == 0.0

    def test_ledger_vuoto(self, conn):
        assert iter_signals(conn=conn, calibrated=False, now=_utc(2026, 9, 14, 12),
                            resolve=lambda name: name) == []


def _utc(y, m, d, h):
    from datetime import datetime, timezone
    return datetime(y, m, d, h, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 4. Catena completa: ledger -> Signal -> Risk -> Stake
# ---------------------------------------------------------------------------

class TestCatenaSuDatiReali:
    def test_approve_e_review_dallo_stesso_ledger(self, conn):
        limits = RiskLimits.from_env()
        # 1) favorito forte con ratings pieni -> giocabile in automatico
        add_match(conn, "sx-1", home="Inter", away="Cagliari")
        add_prediction(conn, "sx-1", status="strong_value")
        add_rating(conn, "Inter", n=FULL_SAMPLE)
        add_rating(conn, "Cagliari", n=FULL_SAMPLE)
        # 2) stesso tipo di segnale ma squadre SENZA rating -> in coda umana
        add_match(conn, "sx-2", home="Volendam", away="Telstar")
        add_prediction(conn, "sx-2", esito="1", quota=1.65, prob=0.64, ev=0.056,
                       market_prob=0.58, market_edge=0.06, status="value")
        add_match(conn, "sx-3", home="Malaga", away="Eldense")
        add_prediction(conn, "sx-3", esito="1", quota=2.40, prob=0.48, ev=0.15,
                       market_prob=0.40, market_edge=0.08, status="value")

        signals = iter_signals(conn=conn, calibrated=True, now=_utc(2026, 9, 14, 12),
                               resolve=lambda name: name, limits=limits)
        assert len(signals) == 3

        kills = KillSwitchStatus(mode="live", provider_ready=True)
        records = [decide(s, kills=kills, limits=limits, bankroll=1000.0) for s in signals]
        by_id = {r.signal.match_id: r for r in records}

        assert by_id["sx-1"].risk.verdict == "approve"
        assert by_id["sx-1"].stake.executable is True
        assert by_id["sx-1"].stake.stake > 0

        assert by_id["sx-2"].risk.verdict == "review"
        assert by_id["sx-2"].risk.reason is ReasonCode.DATA_QUALITY_LOW
        assert by_id["sx-2"].stake is None

        assert by_id["sx-3"].risk.verdict == "reject"
        assert by_id["sx-3"].risk.reason is ReasonCode.ODDS_TOO_HIGH

    def test_righe_storiche_fuori_fascia_non_diventano_ordini(self, conn):
        """Difesa in profondita': una riga storica a quota alta viene MOTIVATA.

        In `auto_bet` la riga veniva scartata in silenzio; nella catena il
        motivo e' un `ReasonCode`, quindi resta contabilizzabile.
        """
        limits = RiskLimits.from_env()
        add_match(conn, "sx-1")
        add_prediction(conn, "sx-1", quota=2.10, prob=0.55, ev=0.15, market_prob=0.48,
                       market_edge=0.07, status="value")
        signal = iter_signals(conn=conn, calibrated=True, now=_utc(2026, 9, 14, 12),
                              resolve=lambda name: name)[0]
        record = decide(signal, kills=KillSwitchStatus(mode="live"), limits=limits,
                        bankroll=1000.0)
        assert record.risk.verdict == "reject"
        assert record.risk.reason in (ReasonCode.ODDS_TOO_HIGH, ReasonCode.NOT_FAVOURITE)
        assert record.stake is None
