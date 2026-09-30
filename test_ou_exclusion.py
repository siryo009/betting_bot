"""Test esclusione DEFINITIVA del mercato OU2.5 (06/09).

Il backtest storico (12.909 partite) ha mostrato un leak sistematico sul
mercato Over/Under (ROI -6.8% su 924 bet, sotto -7.3%): il mercato OU2.5
e' stato ESCLUSO PERMANENTEMENTE dalle selezioni, senza escape hatch via
env. Il sistema elabora SOLO segnali 1X2 (h2h).

Comportamento atteso:
  - _analyze_match NON registra previsioni con mercato "OU" nel ledger
    (neanche con env ENABLE_OU_MARKET=1: la variabile non esiste piu');
  - lo status e il miglior esito dipendono solo da 1X2;
  - OU_ENABLED e' costante False (nessun flag riconfigurabile).
"""

from pathlib import Path

import pytest

import fixture_engine


def _match():
    """Match con h2h 1X2 e totals 2.5 (entrambi i lati) multi-bookmaker."""
    return {
        "id": "match_ou_test",
        "home_team": "Roma", "away_team": "Empoli",
        "commence_time": "2026-09-01T18:45:00Z",
        "bookmakers": [
            {"title": "BookA", "markets": [
                {"key": "h2h", "outcomes": [
                    {"name": "Roma", "price": 1.70},
                    {"name": "Draw", "price": 3.80},
                    {"name": "Empoli", "price": 5.50}]},
                {"key": "totals", "outcomes": [
                    {"name": "Over 2.5", "point": 2.5, "price": 2.00},
                    {"name": "Under 2.5", "point": 2.5, "price": 1.80}]},
            ]},
        ],
    }


def test_odds_request_solo_h2h():
    """La ROTAZIONE DI RICERCA the-odds-api resta `markets="h2h"` (1 credito).

    ⚠️ AGGIORNATO 30/09/2026 (decisione del proprietario: tagliare la
    rotazione per far entrare totals,spreads nel budget). Il veto originale
    del 06/09 nasceva dal doppio addebito `h2h,totals` sulla rotazione: il
    nuovo design NON tocca la rotazione (che resta h2h-only) e porta i
    mercati totals/spreads su una CHIAMATA ORACOLO SEPARATA e budgettizzata
    (`odds_api.fetch_line_odds`, follow-the-money: solo leghe con pick OU/AH
    in gioco, cache `toao_*`, budget `ORACLE_BUDGET_DAY`). Cio' che NON deve
    mai tornare: la rotazione con `h2h,totals` (2 crediti su OGNI chiamata
    di ricerca) e il mercato totals come selezione di `fixture_engine`.
    """
    src = Path(fixture_engine.__file__).parent / "odds_api.py"
    body = src.read_text()
    # la rotazione di ricerca resta h2h (default di `_get_odds`)
    assert '"markets": "h2h",' in body or 'markets: str = "h2h"' in body \
        or 'markets="h2h"' in body
    assert '"markets": "h2h,totals"' not in body
    assert 'markets="h2h,totals",\n            "oddsFormat"' not in body
    # i mercati dell'oracolo a linea vivono SOLO nella chiamata dedicata
    assert 'ORACLE_MARKETS_LIST = "h2h,totals,spreads"' in body
    assert 'def fetch_line_odds' in body
    # il chiamante dell'oracolo passa la lista DEDICATA, non la stringa della
    # rotazione
    assert 'markets=ORACLE_MARKETS_LIST' in body


def test_oracle_a_linea_budget_e_cache_dedicate():
    """L'oracolo a linea ha budget e cache PROPRI (non sfinisce la ricerca)."""
    import odds_api
    assert odds_api.ORACLE_MARKETS_LIST == "h2h,totals,spreads"
    assert odds_api.ORACLE_BUDGET_DAY >= 1
    assert odds_api.ORACLE_CACHE_TTL_S == 86400
    assert odds_api.ORACLE_CACHE_PREFIX == "toao_"
    # la cache oracolo NON e' la cache di ricerca
    assert odds_api.ORACLE_CACHE_PREFIX != ""


def _run(monkeypatch):
    """Esegue _analyze_match intercettando save_prediction e save_analysis.

    is_sane viene forzato a passare (True, "OK") cosi' TUTTI i candidati
    finiscono nel ledger: il test verifica quali mercati vengono registrati,
    non se i singoli segnali superano il filtro EV.
    """
    monkeypatch.setattr("fixture_engine.expected_goals", lambda h, a: (1.9, 1.1))
    monkeypatch.setattr("fixture_engine.save_clv", lambda *a, **k: None)
    monkeypatch.setattr("fixture_engine.get_analysis_for_match", lambda m: None)
    monkeypatch.setattr("fixture_engine.is_sane", lambda *a, **k: (True, "OK"))
    preds = []
    monkeypatch.setattr("fixture_engine.save_prediction",
                        lambda *a, **k: preds.append((a, k)))
    saved = {}
    monkeypatch.setattr("fixture_engine.save_analysis",
                        lambda *a, **k: saved.update({"args": a, "kwargs": k}))
    status = fixture_engine._analyze_match("match_ou_test", _match(),
                                           "Roma", "Empoli", "Serie A")
    return status, preds, saved


class TestOuEsclusoDefinitivo:
    def test_nessuna_previsione_ou_nel_ledger(self, monkeypatch):
        """Nessun candidato OU viene registrato: solo 1X2."""
        status, preds, _ = _run(monkeypatch)
        assert status in ("value", "strong_value", "no_value", "rejected")
        mercati = [a[1] for a, _ in preds]  # save_prediction(match, mercato, ...)
        assert "OU" not in mercati
        assert mercati, "devono esserci candidati 1X2"
        assert all(m == "1X2" for m in mercati)

    def test_esclusione_valida_con_soli_prezzi_h2h(self, monkeypatch):
        """Senza prezzi totals: analisi regolare, nessun crash."""
        m = _match()
        m["bookmakers"][0]["markets"] = [mm for mm in m["bookmakers"][0]["markets"]
                                         if mm["key"] != "totals"]
        monkeypatch.setattr("fixture_engine.expected_goals", lambda h, a: (1.9, 1.1))
        monkeypatch.setattr("fixture_engine.save_clv", lambda *a, **k: None)
        monkeypatch.setattr("fixture_engine.save_analysis", lambda *a, **k: None)
        monkeypatch.setattr("fixture_engine.get_analysis_for_match", lambda m: None)
        preds = []
        monkeypatch.setattr("fixture_engine.save_prediction",
                            lambda *a, **k: preds.append((a, k)))
        status = fixture_engine._analyze_match("m_no_totals", m, "Roma", "Empoli", "Serie A")
        assert status in ("value", "strong_value", "no_value", "rejected")
        assert all(a[1] == "1X2" for a, _ in preds)

    def test_flag_ou_costante_false(self):
        """OU_ENABLED e' una costante False: nessuna riconfigurazione via env."""
        import os
        assert fixture_engine.OU_ENABLED is False
        # la variabile env non esiste piu' nel repo: nessun escape hatch
        assert os.getenv("ENABLE_OU_MARKET", "") == ""

    def test_env_non_riattiva_il_mercato(self, monkeypatch):
        """Anche forzando ENABLE_OU_MARKET=1 il mercato OU resta spento."""
        monkeypatch.setenv("ENABLE_OU_MARKET", "1")
        import importlib
        importlib.reload(fixture_engine)
        try:
            assert fixture_engine.OU_ENABLED is False  # costante, ignora env
            status, preds, _ = _run(monkeypatch)
            assert "OU" not in [a[1] for a, _ in preds]
        finally:
            monkeypatch.delenv("ENABLE_OU_MARKET", raising=False)
            importlib.reload(fixture_engine)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])