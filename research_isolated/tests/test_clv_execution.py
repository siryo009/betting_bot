"""Test del CLV sul prezzo ESEGUIBILE (offline, nessuna rete/DB reale)."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from research_isolated.lab import clv_execution as ce

SRC = Path(ce.__file__).read_text(encoding="utf-8")


def _clv(**kw):
    base = {"match_id": "m1", "esito": "1", "signal_quota": 1.90,
            "closing_quota": None, "pinnacle_quota": None, "closing_odds": None}
    base.update(kw)
    return base


def _bet(**kw):
    base = {"match_id": "m1", "esito": "1", "mercato": "1X2", "price": 1.90,
            "stake": 2.0, "profit": 0.9, "created_at": "2026-10-01T10:00:00"}
    base.update(kw)
    return base


class TestEcho:
    def test_echo_escluso(self):
        rows = ce.build_samples([_clv(closing_quota=1.90)], [_bet(price=1.90)])
        assert rows == []

    def test_echo_tolleranza_relativa(self):
        assert ce.is_echo(2.0, 2.00005) is True
        assert ce.is_echo(2.0, 2.01) is False


class TestFontiChiusura:
    def test_priorita_sharp(self):
        row = _clv(closing_odds=2.00, pinnacle_quota=1.95, closing_quota=1.90)
        s = ce.build_samples([row], [_bet(price=1.80)])[0]
        assert s.source == "closing_odds"
        assert s.closing_price == pytest.approx(2.00)

    def test_fallback_a_pinnacle(self):
        row = _clv(pinnacle_quota=1.95, closing_quota=1.50)
        s = ce.build_samples([row], [_bet(price=1.80)])[0]
        assert s.source == "pinnacle_quota"

    def test_fonte_forzata(self):
        row = _clv(closing_odds=2.00, closing_quota=1.50)
        s = ce.build_samples([row], [_bet(price=1.80)], source="closing_quota")[0]
        assert s.source == "closing_quota"
        assert s.closing_price == pytest.approx(1.50)

    def test_nessuna_fonte_utile(self):
        assert ce.build_samples([_clv()], [_bet()]) == []


class TestCalcoli:
    def test_clv_e_segnale(self):
        s = ce.build_samples([_clv(closing_odds=1.80, signal_quota=2.00)],
                             [_bet(price=1.90)])[0]
        assert s.clv == pytest.approx(1.80 / 1.90 - 1)
        assert s.clv_signal == pytest.approx(1.80 / 2.00 - 1)

    def test_shopping(self):
        s = ce.build_samples([_clv(closing_odds=2.00, signal_quota=2.00)],
                             [_bet(price=1.90)])[0]
        assert s.shopping == pytest.approx(-0.05)
        assert s.clv_signal is not None

    def test_prezzo_non_valido_scartato(self):
        assert ce.build_samples([_clv(closing_odds=2.0)], [_bet(price=1.0)]) == []

    def test_match_assente(self):
        assert ce.build_samples([_clv(closing_odds=2.0, match_id="altro")], [_bet()]) == []

    def test_push_conserva_lo_zero(self):
        s = ce.build_samples([_clv(closing_odds=2.0)], [_bet(profit=0.0)])[0]
        assert s.profit == 0.0


class TestDecomposizione:
    def _samples(self):
        clv = [_clv(match_id=f"m{i}", closing_odds=1.80 + 0.01 * i,
                    signal_quota=1.90) for i in range(10)]
        bets = [_bet(match_id=f"m{i}", price=1.90,
                     created_at=f"2026-10-{i + 1:02d}T10:00:00") for i in range(10)]
        return ce.build_samples(clv, bets)

    def test_conteggi(self):
        dec = ce.decompose(self._samples())
        assert dec["n"] == 10
        assert dec["signal"]["n"] == 10
        assert dec["roi"]["n"] == 10
        assert dec["roi"]["staked"] == pytest.approx(20.0)

    def test_per_fonte(self):
        dec = ce.decompose(self._samples())
        assert "closing_odds" in dec["by_source"]
        assert dec["by_source"]["closing_odds"]["n"] == 10

    def test_vuoto_non_esplode(self):
        dec = ce.decompose([])
        assert dec["n"] == 0
        assert dec["executable"]["n"] == 0

    def test_drawdown_e_peggior_esito(self):
        clv, bets = [], []
        # Serie cronologica: +0,5 poi -1,0 -> drawdown 1,0.
        for match, profit, day in (("m1", 0.5, 1), ("m2", -1.0, 2)):
            clv.append(_clv(match_id=match, closing_odds=2.00))
            bets.append(_bet(match_id=match, price=1.90, profit=profit,
                             created_at=f"2026-10-0{day}T10:00:00"))
        roi = ce.decompose(ce.build_samples(clv, bets))["roi"]
        assert roi["max_drawdown"] == pytest.approx(1.0)
        assert roi["worst"] == pytest.approx(-1.0)
        assert roi["roi"] == pytest.approx((0.5 - 1.0) / 4.0)


class TestVerdetto:
    def test_lagging(self):
        v = ce.verdict({"n": 50, "mean": -0.03, "lo": -0.045, "hi": -0.02})
        assert v["status"] == ce.STATUS_LAGGING

    def test_beating(self):
        v = ce.verdict({"n": 50, "mean": 0.03, "lo": 0.01, "hi": 0.05})
        assert v["status"] == ce.STATUS_BEATING

    def test_flat(self):
        v = ce.verdict({"n": 50, "mean": 0.001, "lo": -0.02, "hi": 0.02})
        assert v["status"] == ce.STATUS_FLAT

    def test_campione_insufficiente(self):
        v = ce.verdict({"n": 5, "mean": 0.5, "lo": 0.4, "hi": 0.6})
        assert v["status"] == ce.STATUS_INSUFFICIENT

    def test_unavailable(self):
        assert ce.verdict({"n": 0})["status"] == ce.STATUS_UNAVAILABLE


class TestStabilita:
    def test_due_finestre_ordinate(self):
        clv, bets = [], []
        for i in range(6):
            clv.append(_clv(match_id=f"m{i}", closing_odds=1.80 if i < 3 else 2.10))
            bets.append(_bet(match_id=f"m{i}", price=1.90,
                             created_at=f"2026-10-0{i + 1}T10:00:00"))
        parts = ce.stability(ce.build_samples(clv, bets), parts=2)
        assert [p["n"] for p in parts] == [3, 3]
        assert parts[0]["mean"] < parts[1]["mean"]

    def test_vuoto(self):
        assert ce.stability([]) == []


class TestReport:
    def test_serializzabile(self):
        row = _clv(closing_odds=1.80)
        data = ce.report(ce.build_samples([row], [_bet()]))
        json.dumps(data)  # non deve sollevare
        assert data["decomposition"]["n"] == 1
        assert data["verdict"]["status"] in {
            ce.STATUS_BEATING, ce.STATUS_LAGGING, ce.STATUS_FLAT,
            ce.STATUS_INSUFFICIENT, ce.STATUS_UNAVAILABLE}

    def test_formato_senza_none(self):
        row = _clv(closing_odds=1.80)
        txt = ce.format_report(ce.report(ce.build_samples([row], [_bet()])))
        assert "None" not in txt
        assert "CLV" in txt

    def test_formato_vuoto(self):
        txt = ce.format_report(ce.report([]))
        assert "nessun campione" in txt


class TestTripwire:
    def test_nessuna_scrittura_ne_rete_nel_codice(self):
        tree = ast.parse(SRC)
        code = "\n".join(
            n.value if isinstance(n, ast.Constant) and isinstance(n.value, str) else ""
            for n in ast.walk(tree))
        for tok in ("requests.", "urlopen", "INSERT", "UPDATE ", "DELETE", "api.sx.bet"):
            assert tok not in code, f"riferimento vietato nel codice: {tok}"

    def test_import_pigro_del_loader(self):
        # `loader` non deve essere importato a livello di modulo (il modulo resta
        # importabile senza snapshot).
        assert "from . import loader" in SRC
        assert "\nfrom . import loader" not in SRC.split("def _load_rows")[0]


def test_main_senza_snapshot(tmp_path, monkeypatch):
    from research_isolated.lab import loader

    monkeypatch.setattr(loader, "DB_PATH", tmp_path / "assente.db")
    assert ce.main([]) == 2
