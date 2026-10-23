"""Settlement: il LATO di una previsione (23/10/2026).

Tre casi REALI del ledger in cui l'esito e' il nome della squadra scritto in
modo diverso dal nostro DB (`home_team`/`away_team`):

    esito                        partita                        lato
    ---------------------------  -----------------------------  ----
    'Brondby IF'                 Randers vs Brondby             2
    'IK Sirius'                  Sirius vs AIK                  1
    'AC Milan'                   Udinese vs Milan               2

Prima la regola era duplicata: `decision.adapters.canonical_outcome` usava
`team_names.same_team` (tollerante), il `tracker` confrontava solo il nome
normalizzato — quindi le previsioni con queste forme restavano APERTE per
sempre. Ora entrambi usano `team_names.side_of`, e un esito AMBIGUO (che
somiglia a entrambe le squadre) non produce un lato: la riga resta aperta,
mai saldata col risultato di un'altra squadra.
"""
import sqlite3

import pytest

import team_names
import tracker
from decision.adapters import canonical_outcome


# ---------------------------------------------------------------------------
# 1. Helper condiviso
# ---------------------------------------------------------------------------

class TestSideOf:
    @pytest.mark.parametrize("esito,home,away,atteso", [
        ("Brondby IF", "Randers", "Brondby", "2"),
        ("IK Sirius", "Sirius", "AIK", "1"),
        ("AC Milan", "Udinese", "Milan", "2"),
        ("Brondby", "Brondby", "Randers", "1"),
        ("AFC Bournemouth", "Bournemouth", "Arsenal", "1"),
    ])
    def test_lato_riconosciuto(self, esito, home, away, atteso):
        assert team_names.side_of(esito, home, away) == atteso

    @pytest.mark.parametrize("esito,home,away", [
        ("Manchester", "Manchester United", "Manchester City"),
        ("Inter", "Inter", "Inter"),
        ("Milan", "Inter", "Cagliari"),
        ("", "Inter", "Cagliari"),
        ("Sconosciuta", None, None),
    ])
    def test_ambiguo_o_sconosciuto_non_viene_assegnato(self, esito, home, away):
        assert team_names.side_of(esito, home, away) is None

    def test_usa_anche_il_nome_risolto(self):
        resolve = lambda n: {"Wolverhampton Wanderers": "Wolverhampton"}.get(n, n)  # noqa: E731
        assert team_names.side_of("Wolverhampton Wanderers", "Wolves", "Watford",
                                  resolve=resolve) == "1"
        # lo stesso nome su ENTRAMBI i lati resta ambiguo anche col resolver
        assert team_names.side_of("Wolverhampton Wanderers", "Wolves",
                                  "Wolverhampton", resolve=resolve) is None

    def test_resolver_che_esplode_non_solleva(self):
        def boom(_n):
            raise RuntimeError("db non leggibile")

        assert team_names.side_of("Brondby IF", "Randers", "Brondby",
                                  resolve=boom) == "2"


# ---------------------------------------------------------------------------
# 2. La catena di decisione usa la stessa regola
# ---------------------------------------------------------------------------

class TestAdaptersUsaHelper:
    @pytest.mark.parametrize("esito,home,away,atteso", [
        ("Brondby IF", "Randers", "Brondby", "2"),
        ("IK Sirius", "Sirius", "AIK", "1"),
        ("AC Milan", "Udinese", "Milan", "2"),
    ])
    def test_tre_casi_reali(self, esito, home, away, atteso):
        assert canonical_outcome(esito, home, away, resolve=lambda n: n) == atteso

    def test_ambiguo(self):
        assert canonical_outcome("Manchester", "Manchester United",
                                 "Manchester City", resolve=lambda n: n) is None


# ---------------------------------------------------------------------------
# 3. Il tracker (settlement) usa la stessa regola
# ---------------------------------------------------------------------------

class TestTrackerSettlement:

    def test_nome_squadra_vince_o_perde(self):
        assert tracker._prediction_outcome(
            "1X2", "Brondby IF", 2.5, 1, 2, "Randers", "Brondby") == ("won", 1.5)
        assert tracker._prediction_outcome(
            "1X2", "IK Sirius", 1.8, 3, 0, "Sirius", "AIK") == ("won", 0.8)
        assert tracker._prediction_outcome(
            "1X2", "AC Milan", 2.0, 2, 1, "Udinese", "Milan") == ("lost", -1.0)

    def test_nome_ambiguo_non_produce_verdetto(self):
        """Nessun lato assegnato per esclusione: la riga resta APERTA."""
        assert tracker._prediction_outcome(
            "1X2", "Manchester", 2.0, 2, 1,
            "Manchester United", "Manchester City") == (None, None)

    def test_esito_possible_ambiguo_non_e_contraddizione(self):
        assert tracker._esito_possible(
            "1X2", "Manchester", 2, 1,
            "Manchester United", "Manchester City") is None
        # controprova: un lato riconosciuto resta verificabile
        assert tracker._esito_possible(
            "1X2", "Brondby IF", 1, 2, "Randers", "Brondby") is True
        assert tracker._esito_possible(
            "1X2", "Brondby IF", 2, 1, "Randers", "Brondby") is False

    def test_ou_e_esiti_canonici_invariati(self):
        assert tracker._prediction_outcome(
            "OU", "Over 2.5", 2.0, 2, 1, "A", "B") == ("won", 1.0)
        assert tracker._prediction_outcome(
            "1X2", "2", 2.0, 0, 1, "A", "B") == ("won", 1.0)


# ---------------------------------------------------------------------------
# 4. Settlement end-to-end su un ledger temporaneo
# ---------------------------------------------------------------------------

@pytest.fixture()
def temp_db(monkeypatch, tmp_path):
    db_path = tmp_path / "settle.db"
    monkeypatch.setattr(tracker, "DB_PATH", db_path)
    tracker.init_db()
    return db_path


def _settled(db_path, match_id):
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT esito_finale, profit FROM predictions WHERE match_id=?",
            (match_id,)).fetchone()
    finally:
        conn.close()


class TestSettlementEndToEnd:

    def test_brondby_if_viene_saldato(self, temp_db):
        tracker.save_match("m-bro", "Superliga", "Randers", "Brondby",
                           "2026-10-20T18:00:00Z")
        tracker.save_prediction("m-bro", "1X2", "Brondby IF", 2.5, 0.45, 0.20)
        tracker.save_result("m-bro", "Superliga", "Randers", "Brondby", 1, 2,
                            "2026-10-20T20:00:00Z")
        tracker.settle_predictions()
        assert _settled(temp_db, "m-bro") == ("won", 1.5)

    def test_ac_milan_perde(self, temp_db):
        tracker.save_match("m-mil", "Serie A", "Udinese", "Milan",
                           "2026-10-20T18:00:00Z")
        tracker.save_prediction("m-mil", "1X2", "AC Milan", 2.0, 0.45, 0.10)
        tracker.save_result("m-mil", "Serie A", "Udinese", "Milan", 2, 1,
                            "2026-10-20T20:00:00Z")
        tracker.settle_predictions()
        assert _settled(temp_db, "m-mil") == ("lost", -1.0)

    def test_nome_ambiguo_resta_aperto(self, temp_db):
        tracker.save_match("m-amb", "Premier League", "Manchester United",
                           "Manchester City", "2026-10-20T18:00:00Z")
        tracker.save_prediction("m-amb", "1X2", "Manchester", 1.9, 0.50, 0.05)
        tracker.save_result("m-amb", "Premier League", "Manchester United",
                            "Manchester City", 2, 1, "2026-10-20T20:00:00Z")
        assert tracker.settle_predictions() == (0, 0)
        assert _settled(temp_db, "m-amb") == (None, None)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
