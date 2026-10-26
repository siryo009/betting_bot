"""Test di `liquidity_impact.py` (diagnostica impatto soglie liquidita').

Tutto OFFLINE e deterministico: nessuna rete, nessun ordine, nessuna
scrittura sul ledger. Le righe di campione sono sintetiche.
"""
from __future__ import annotations

import time

import pytest

import liquidity_impact as li


def _row(*, home="Alpha", away="Beta", league="Serie A",
         depths=None, at_floor=None, favorite="1", odds=None,
         coherent=True, value=False, has_ratings=False, fav_edge=None,
         best_ev=-0.05, best_edge=-0.04) -> dict:
    depths = depths or {"1": 100.0, "X": 100.0, "2": 100.0}
    at_floor = at_floor or dict(depths)
    odds = odds or {"1": 1.5, "X": 4.0, "2": 6.0}
    total = round(sum(depths.values()), 2)
    return {
        "match_id": f"sx-{home}{away}", "home": home, "away": away,
        "league": league, "league_sports": league,
        "kickoff": "2026-09-12T18:00:00+00:00",
        "odds": odds, "depths": depths, "at_floor": at_floor,
        "total_depth": total, "min_leg_depth": min(depths.values()),
        "inv_sum": 1.02, "coherent": coherent,
        "market_probs": {"1": 0.60, "X": 0.24, "2": 0.16},
        "has_ratings": has_ratings,
        "market_fav": "1", "market_fav_odds": odds["1"],
        "market_fav_prob": 0.60, "model_fav_prob": 0.55,
        "fav_edge": fav_edge if fav_edge is not None else (best_edge or 0.0),
        "is_favorite_candidate": favorite is not None,
        "favorite": favorite,
        "favorite_depth": depths.get(favorite) if favorite else None,
        "favorite_floor": at_floor.get(favorite) if favorite else None,
        "favorite_odds": odds.get(favorite) if favorite else None,
        "favorite_market_prob": 0.60 if favorite else None,
        "value": value, "value_reason": "test",
        "value_ev": best_ev, "value_esito": favorite, "value_odds": 1.5,
        "best_prob": 0.62, "best_ev": best_ev, "best_edge": best_edge,
        "best_market_prob": 0.60, "best_esito": favorite, "best_odds": 1.5,
    }


# ---------------------------------------------------------------------------
# Filtro di mercato
# ---------------------------------------------------------------------------

class TestFiltroMercato:
    def test_vecchie_soglie_vs_attuali(self):
        """Le soglie VECCHIE (15/5) sono il riferimento before/after del
        misuratore; l'attuale taratura e' 20/4/20 (21/09/2026).

        Sul TOTALE le vecchie restano meno severe (15 < 20), quindi il
        confronto "prima passavano piu' partite" e' ancora sensato; sul
        SINGOLO esito invece la soglia si e' allentata di 1 USDC (5 -> 4)
        perche' il taglio del 20% si applica a tutti e tre i valori: le due
        grandezze non sono piu' ordinate allo stesso modo (voluto).
        """
        import sx_signals
        assert li.OLD_DEPTH_USDC < sx_signals.MIN_DEPTH_USDC
        assert li.OLD_LEG_DEPTH_USDC >= sx_signals.MIN_LEG_DEPTH_USDC

    def test_totale_sotto_soglia_bloccato(self):
        r = _row(depths={"1": 5.0, "X": 3.0, "2": 2.0})   # totale 10 < 25
        assert li._market_ok(r, 25.0, 5.0) is False
        assert li._depth_block_reason(r, 25.0, 5.0) == "depth_totale"
        # ma passerebbe le VECCHIE soglie (10 < 15? no: 10 < 15 -> bloccata)
        r2 = _row(depths={"1": 6.0, "X": 5.0, "2": 5.0})   # totale 16
        assert li._market_ok(r2, li.OLD_DEPTH_USDC, li.OLD_LEG_DEPTH_USDC)
        assert not li._market_ok(r2, 25.0, 5.0)

    def test_esito_sotto_soglia_bloccato(self):
        r = _row(depths={"1": 60.0, "X": 40.0, "2": 2.0})  # totale 102
        assert li._market_ok(r, 25.0, 5.0) is False
        assert li._depth_block_reason(r, 25.0, 5.0) == "depth_esito"

    def test_leg_giocata_usa_la_size_al_floor(self):
        # Profondita' di mercato abbondante ma size al floor sottile.
        r = _row(depths={"1": 900.0, "X": 500.0, "2": 400.0},
                 at_floor={"1": 4.0, "X": 200.0, "2": 150.0})
        assert li._market_ok(r, 25.0, 5.0) is True
        assert li._played_ok(r, 10.0) is False
        r["favorite_floor"] = 12.0
        assert li._played_ok(r, 10.0) is True

    def test_senza_favorito_niente_leg_giocata(self):
        r = _row(favorite=None)
        assert li._played_ok(r, 10.0) is False


# ---------------------------------------------------------------------------
# Aggregazione
# ---------------------------------------------------------------------------

class TestAnalyse:
    def test_imbuto_conta_blocchi_e_favoriti(self):
        rows = [
            _row(home="A"),                                   # ok, favorito
            _row(home="B", favorite=None),                    # ok, no favorito
            _row(home="C", depths={"1": 8.0, "X": 4.0, "2": 3.0}),   # thin
            _row(home="D", coherent=False),                   # incoerente
        ]
        data = li.analyse(rows)
        f = data["funnel"]
        assert f["events"] == 4 and f["coherent"] == 3
        assert f["new_market_ok"] == 2 and f["depth_blocked"] == 1
        # A e C hanno un favorito; solo A e' eseguibile (C e' thin: filtro di
        # mercato E size al floor).
        assert f["favorites"] == 2
        assert f["favorites_market_ok"] == 1
        assert f["favorites_playable"] == 1
        assert f["favorites_playable_pct"] == 50.0
        assert data["block_reasons"] == {"depth_totale": 1}

    def test_sensibilita_allo_stake(self):
        # Size al floor 30 USDC: passa 10 e 20, non 40 (stake 20 x 2).
        rows = [_row(at_floor={"1": 30.0, "X": 30.0, "2": 30.0})]
        data = li.analyse(rows)
        grid = {g["stake"]: g for g in data["stake_sensitivity"]}
        assert grid[1.0]["passed"] == 1 and grid[10.0]["passed"] == 1
        assert grid[20.0]["passed"] == 0
        # Taratura 21/09: richiesto = max(20 x 1.6, 20) = 32 (era 40 con x2.0).
        assert grid[20.0]["required_depth"] == pytest.approx(32.0)

    def test_sensibilita_se_irrigidissimo_la_soglia(self):
        # due favoriti: floor 40 e 400 -> a soglia 25 passano entrambi,
        # a soglia 100 solo il secondo.
        rows = [_row(home="A", at_floor={"1": 40.0, "X": 40.0, "2": 40.0}),
                _row(home="B", at_floor={"1": 400.0, "X": 400.0, "2": 400.0})]
        grid = {g["floor_min"]: g for g in
                li.analyse(rows)["exec_sensitivity"]}
        assert grid[10.0]["passed"] == 2
        assert grid[25.0]["passed"] == 2
        assert grid[50.0]["passed"] == 1
        assert grid[100.0]["passed"] == 1

    def test_gate_modello_non_misurabile_senza_ratings(self):
        data = li.analyse([_row(has_ratings=False)], with_model=True)
        mg = data["model_gate"]
        assert mg["with_ratings"] == 0 and mg["measurable"] is False
        out = "\n".join(li.verdict(data))
        assert "NON MISURABILE" in out and "CONTAINER" in out

    def test_gate_modello_misurabile_con_ratings(self):
        data = li.analyse([_row(has_ratings=True, fav_edge=-0.02)],
                          with_model=True)
        mg = data["model_gate"]
        assert mg["measurable"] is True and mg["with_ratings"] == 1
        assert mg["fav_edge_rated"]["p50"] == pytest.approx(-0.02)

    # --- ISOLAMENTO DELLE VARIABILI (direttiva 11/09): la misura di
    # --- liquidita' non deve contenere/persino calcolare il modello.
    def test_default_solo_liquidita_nessun_campo_modello(self):
        data = li.analyse([_row()])
        for key in ("model_gate", "gate_sensitivity", "favorite_ev",
                    "favorite_edge"):
            assert key not in data
        assert "value_signals" not in data["funnel"]
        assert data["misc"]["with_model"] is False

    def test_verdetto_default_non_parla_di_modello(self):
        out = "\n".join(li.verdict(li.analyse([_row()])))
        assert "SOLO-LIQUIDITA'" in out
        # Nessun verdetto sul gate modello: la variabile resta isolata.
        assert "segnali value" not in out
        assert "GATE MODELLO" not in out
        assert "PERDITA PER LIQUIDITA'" in out

    def test_verdetto_equilibrato(self):
        data = li.analyse([_row() for _ in range(10)])
        out = "\n".join(li.verdict(data))
        assert "100.0%" in out and "NON e' il collo di bottiglia" in out

    def test_verdetto_severo(self):
        rows = [_row(at_floor={"1": 1.0, "X": 1.0, "2": 1.0}) for _ in range(10)]
        out = "\n".join(li.verdict(li.analyse(rows)))
        assert "0.0%" in out and "rivedere" in out

    def test_confronto_vecchie_nuove_soglie(self):
        # Totale 16: passa le VECCHIE (15/5), non le NUOVE (25).
        rows = [_row(depths={"1": 6.0, "X": 5.0, "2": 5.0})]
        f = li.analyse(rows)["funnel"]
        assert f["old_market_pct"] == 100.0 and f["new_market_pct"] == 0.0


# ---------------------------------------------------------------------------
# Lettura dei book -> riga di campione (nessuna rete)
# ---------------------------------------------------------------------------

def _ev() -> dict:
    return {"event_id": "LTEST1", "league_label": "Italy Serie A",
            "kickoff_ms": int(time.time() * 1000) + 3_600_000,
            "teams": ("Alpha", "Beta"),
            "legs": [{"esito": "1", "market_hash": "m1"},
                     {"esito": "X", "market_hash": "mX"},
                     {"esito": "2", "market_hash": "m2"}]}


class TestRowForEvent:
    def _patch(self, monkeypatch):
        import poisson_engine
        monkeypatch.setattr(poisson_engine, "expected_goals",
                            lambda h, a: (1.9, 0.8))
        monkeypatch.setattr(poisson_engine, "prob_1x2",
                            lambda lh, la: (0.62, 0.23, 0.15))
        import value_filter
        monkeypatch.setattr(value_filter, "adjusted_probability",
                            lambda mp, mkt, price, league=None: mp)
        import rating_engine
        monkeypatch.setattr(rating_engine, "get_rating", lambda t: None)

    def test_depths_e_size_al_floor(self, monkeypatch):
        self._patch(monkeypatch)
        books = {"m1": [(1.70, 50.0), (1.68, 10.0)],
                 "mX": [(3.90, 12.0)],
                 "m2": [(5.80, 8.0)]}
        row = li._row_for_event(_ev(), books)
        assert row is not None
        assert row["depths"]["1"] == pytest.approx(60.0)      # tutti i livelli
        assert row["at_floor"]["1"] == pytest.approx(50.0)    # solo al best
        assert row["favorite"] == "1"
        assert row["favorite_floor"] == pytest.approx(50.0)
        assert row["league_sports"] == "Serie A"
        # Default = solo liquidita': il modello non e' nemmeno interrogato.
        assert row["has_ratings"] is None

    def test_book_mancante_riga_none(self, monkeypatch):
        self._patch(monkeypatch)
        assert li._row_for_event(_ev(), {"m1": [(1.70, 10.0)]}) is None

    def test_favorito_fuori_fascia_non_e_candidato(self, monkeypatch):
        self._patch(monkeypatch)
        # Quota del favorito 2.50 (> ODDS_MAX 1.80): nessun candidato.
        books = {"m1": [(2.50, 500.0)], "mX": [(3.90, 100.0)],
                 "m2": [(5.80, 100.0)]}
        row = li._row_for_event(_ev(), books)
        assert row["favorite"] is None
        assert row["is_favorite_candidate"] is False

    def test_default_non_interroga_il_modello(self, monkeypatch):
        """Isolamento: con with_model=False il modello non viene chiamato."""
        import poisson_engine

        def _boom(*a, **k):
            raise AssertionError("il modello non deve essere interrogato")

        monkeypatch.setattr(poisson_engine, "expected_goals", _boom)
        books = {"m1": [(1.70, 50.0)], "mX": [(3.90, 50.0)],
                 "m2": [(5.80, 50.0)]}
        row = li._row_for_event(_ev(), books)   # default: solo liquidita'
        assert row is not None and row["favorite"] == "1"
        assert row["favorite_floor"] == pytest.approx(50.0)
        assert row["value"] is False and row["has_ratings"] is None


# ---------------------------------------------------------------------------
# Tripwire: la diagnostica non tocca il ledger ne' piazza ordini
# ---------------------------------------------------------------------------

class TestIndipendenza:
    def test_nessuna_scrittura_sul_ledger(self):
        from pathlib import Path
        src = Path("liquidity_impact.py").read_text(encoding="utf-8")
        for forbidden in ("import tracker", "save_match", "save_prediction",
                          "save_bet", "place_limit_order", "import bot"):
            assert forbidden not in src, f"liquidity_impact non deve usare {forbidden}"

    def test_default_env_documentate(self):
        import liquidity_monitor
        assert liquidity_monitor.DEFAULT_EXEC_DEPTH_USDC >= 20.0
        assert liquidity_monitor.DEFAULT_DEPTH_MULTIPLIER >= 1.6
