"""Test della calibrazione dell'EV (offline, dati costruiti a mano)."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from research_isolated.lab import ev_calibration as ec

SRC = Path(ec.__file__).read_text(encoding="utf-8")


def _row(**kw):
    base = {"match_id": "m1", "mercato": "1X2", "quota": 1.90, "prob": 0.60,
            "ev": 0.05, "market_prob": 0.55, "esito_finale": "won",
            "profit": 0.9, "created_at": "2026-10-01T10:00:00", "league": "L"}
    base.update(kw)
    return base


class TestBuildRows:
    def test_filtra_quota_non_valida(self):
        assert ec.build_rows([_row(quota=1.0)]) == []

    def test_filtra_prob_nan(self):
        assert ec.build_rows([_row(prob=float("nan"))]) == []

    def test_clip(self):
        rows = ec.build_rows([_row(prob=1.5)])
        assert rows[0].p_model <= 1 - 1e-4

    def test_ordine_cronologico(self):
        rows = ec.build_rows([_row(created_at="2026-10-05T10:00:00"),
                              _row(created_at="2026-10-01T10:00:00")])
        assert rows[0].created_at.startswith("2026-10-01")

    def test_decided_esclude_push(self):
        push = ec.build_rows([_row(esito_finale="push")])[0]
        assert push.decided is False and push.y is None


class TestMetriche:
    def test_brier_noto(self):
        assert ec.brier([1.0, 0.0], [1.0, 0.0]) == pytest.approx(0.0)
        assert ec.brier([0.5, 0.5], [1.0, 0.0]) == pytest.approx(0.25)

    def test_logloss_noto(self):
        # Una previsione piu' sicura e corretta ha log-loss minore.
        assert ec.logloss([0.9, 0.1], [1.0, 0.0]) < ec.logloss([0.6, 0.4], [1.0, 0.0])

    def test_tabelle_per_mercato(self):
        rows = ec.build_rows([
            _row(mercato="1X2", esito_finale="won", prob=0.9, market_prob=0.6),
            _row(mercato="OU", esito_finale="lost", prob=0.5, market_prob=0.5),
        ])
        cal = ec.calibration_table(rows)
        assert set(cal["by_market"]) == {"1X2", "OU"}
        assert cal["overall"]["n_decided"] == 2

    def test_push_fuori_dal_brier(self):
        rows = ec.build_rows([_row(esito_finale="push"), _row(esito_finale="won")])
        cal = ec.calibration_table(rows)
        assert cal["overall"]["n_decided"] == 1

    def test_market_better_count(self):
        rows = ec.build_rows([_row(prob=0.90, market_prob=0.50, esito_finale="lost")])
        cal = ec.calibration_table(rows)
        assert cal["overall"]["n_market_better"] == 1


class TestShrink:
    def _rows(self, n=40, market_right=True):
        out = []
        for i in range(n):
            won = i % 2 == 0
            pm = 0.80 if won else 0.30
            model = 0.55 if market_right else (0.80 if won else 0.30)
            out.append(_row(match_id=f"m{i}", prob=model, market_prob=pm,
                            esito_finale="won" if won else "lost",
                            created_at=f"2026-10-{i + 1:02d}T10:00:00"))
        return ec.build_rows(out)

    def test_w_verso_il_mercato_quando_il_modello_e_cieco(self):
        res = ec.fit_shrink(self._rows())
        assert res["available"] is True
        assert res["w"] >= 0.5
        assert res["validation"]["brier_shrunk"] <= res["validation"]["brier_model"]

    def test_campione_piccolo(self):
        assert ec.fit_shrink(ec.build_rows([_row()]))["available"] is False

    def test_market_still_best_flag(self):
        res = ec.fit_shrink(self._rows())
        assert isinstance(res["market_still_best"], bool)

    def test_split_dichiarato(self):
        res = ec.fit_shrink(self._rows())
        assert res["split"]["total"] == 40


class TestEvBias:
    def test_bias(self):
        rows = ec.build_rows([_row(ev=0.12, profit=-1.0), _row(match_id="m2", ev=0.12, profit=-1.0)])
        bias = ec.ev_bias(rows)["overall"]
        assert bias["n"] == 2
        # EV dichiarato +12%, P/L realizzato -100% (per unita' di stake).
        assert bias["bias_pp"] == pytest.approx(112.0)

    def test_vuoto(self):
        assert ec.ev_bias([])["overall"]["n"] == 0


class TestVerdetto:
    def _cal(self, bm, bmk):
        return {"overall": {"brier_model": bm, "brier_market": bmk}}

    def test_model_worse(self):
        v = ec.verdict(self._cal(0.23, 0.22), {})
        assert v["status"] == "model_worse_than_market"

    def test_model_better(self):
        v = ec.verdict(self._cal(0.20, 0.23), {})
        assert v["status"] == "model_better_than_market"

    def test_tie(self):
        v = ec.verdict(self._cal(0.2200, 0.2199), {})
        assert v["status"] == "tie"

    def test_unavailable(self):
        assert ec.verdict({}, {})["status"] == "unavailable"


class TestReport:
    def test_serializzabile(self):
        rows = ec.build_rows([_row(), _row(match_id="m2", esito_finale="lost")])
        data = ec.report(rows)
        json.dumps(data)
        assert data["verdict"]["status"]
        assert "rows" in data

    def test_formato_senza_none(self):
        rows = ec.build_rows([_row(), _row(match_id="m2", esito_finale="lost")])
        txt = ec.format_report(ec.report(rows))
        assert "None" not in txt
        assert "verdetto" in txt

    def test_formato_vuoto(self):
        txt = ec.format_report(ec.report([]))
        assert "verdetto" in txt


class TestTripwire:
    def test_nessuna_rete_ne_scrittura(self):
        tree = ast.parse(SRC)
        code = "\n".join(
            n.value if isinstance(n, ast.Constant) and isinstance(n.value, str) else ""
            for n in ast.walk(tree))
        for tok in ("requests.", "urlopen", "INSERT", "UPDATE ", "DELETE"):
            assert tok not in code, f"riferimento vietato: {tok}"

    def test_nessuna_conversione_ev_quota(self):
        # Le righe con push non si convertono con (EV+1)/quota: la conversione
        # inventerebbe una probabilita' (misurato il 20/10).
        for pattern in ("(ev + 1)", "(ev+1)", "ev + 1) / "):
            assert pattern not in SRC


def test_main_senza_snapshot(tmp_path, monkeypatch):
    from research_isolated.lab import loader

    monkeypatch.setattr(loader, "DB_PATH", tmp_path / "assente.db")
    assert ec.main([]) == 2
