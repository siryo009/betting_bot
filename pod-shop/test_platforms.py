"""Test del confronto piattaforme — offline, nessuna rete.

I numeri qui sono l'unica cosa che puo' rendere sbagliata la scelta: se il
margine di Hoplix fosse piu' basso di quello di Printify, la decisione andrebbe
rifatta. I test fissano i valori MISURATI il 28/10/2026 e, soprattutto,
verificano che la scelta sia calcolata e non scritta a mano.
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
import sys
from pathlib import Path

import pytest

import platforms as PL

ROOT = Path(__file__).resolve().parent


class TestMargini:
    def test_hoplix_margine_al_prezzo_di_listino(self):
        m = PL.net_margin(24.90, PL.HOPLIX)
        # 24,90 / 1,22 = 20,41 netti; meno 7,90 di costo
        assert m["net_revenue"] == pytest.approx(20.41, abs=0.01)
        assert m["payment_fees"] == 0.0
        assert m["margin"] == pytest.approx(12.51, abs=0.02)
        assert m["margin_pct_of_price"] == pytest.approx(50.2, abs=0.2)

    def test_printify_paga_stripe(self):
        m = PL.net_margin(24.90, PL.PRINTIFY)
        # 1,5% di 24,90 + 0,25
        assert m["payment_fees"] == pytest.approx(0.62, abs=0.01)
        assert m["margin"] == pytest.approx(9.29, abs=0.05)

    def test_hoplix_vince_di_circa_tre_euro(self):
        diff = PL.net_margin(24.90, PL.HOPLIX)["margin"] - PL.net_margin(24.90, PL.PRINTIFY)["margin"]
        assert diff == pytest.approx(3.22, abs=0.05)

    def test_iva_tolta_in_entrambi(self):
        for platform in PL.PLATFORMS:
            m = PL.net_margin(24.90, platform)
            assert m["vat_eur"] == pytest.approx(24.90 - 24.90 / 1.22, abs=0.01)

    def test_prezzo_non_valido_solleva(self):
        for bad in (0, -5, "x"):
            with pytest.raises(ValueError):
                PL.net_margin(bad, PL.HOPLIX)  # type: ignore[arg-type]

    def test_margine_negativo_segnato(self):
        cheap = dataclasses.replace(PL.HOPLIX, product_cost_eur=25.0)
        m = PL.net_margin(20.0, cheap)
        assert m["margin"] < 0
        assert m["positive"] is False

    def test_stima_dichiarata_su_printify(self):
        assert PL.PRINTIFY.product_cost_estimate is True
        assert PL.net_margin(24.90, PL.PRINTIFY)["product_cost_estimate"] is True
        assert PL.HOPLIX.product_cost_estimate is False


class TestPunteggi:
    def test_pesi_sommano_a_uno(self):
        assert sum(PL.WEIGHTS.values()) == pytest.approx(1.0)

    def test_ogni_criterio_tra_zero_e_uno(self):
        for platform in PL.PLATFORMS:
            parts = PL.criterion_scores(platform, 24.90)
            assert set(parts) == set(PL.WEIGHTS)
            for key, part in parts.items():
                assert 0.0 <= part["value"] <= 1.0, key
                assert part["why"]

    def test_adempimenti_hoplix_al_massimo(self):
        parts = PL.criterion_scores(PL.HOPLIX, 24.90)
        assert parts["compliance"]["value"] == 1.0
        # Printify: incassiamo noi, IVA nostra, commissioni nostre
        assert PL.criterion_scores(PL.PRINTIFY, 24.90)["compliance"]["value"] == 0.0

    def test_automazione_penalizza_chi_non_pubblica(self):
        for platform in PL.PLATFORMS:
            assert PL.criterion_scores(platform, 24.90)["automation"]["value"] <= 0.7

    def test_rischio_cresce_con_le_incognite(self):
        assert PL.criterion_scores(PL.PRINTIFY, 24.90)["risk"]["value"] < PL.criterion_scores(
            PL.HOPLIX, 24.90
        )["risk"]["value"]

    def test_tempi_penalizzano_la_produzione_lunga(self):
        slow = dataclasses.replace(PL.HOPLIX, production_days=(10, 15), delivery_it_days=(5, 8))
        assert PL.score(slow, 24.90) < PL.score(PL.HOPLIX, 24.90)


class TestScelta:
    def test_hoplix_scelto(self):
        report = PL.compare(24.90)
        assert report["chosen"] == "hoplix"
        assert report["score_gap"] > 0
        assert report["margin_gap"] > 0

    def test_motivi_non_vuoti(self):
        report = PL.compare(24.90)
        assert report["why"]
        assert any("margine" in w for w in report["why"])

    def test_la_scelta_e_calcolata_non_scritta(self, monkeypatch):
        """Con Printify piu' economico E automatizzabile, deve vincere lui.

        Se il codice restituisse sempre "hoplix", questo test lo scoprirebbe:
        e' il motivo per cui esiste.
        """
        cheap = dataclasses.replace(
            PL.PRINTIFY,
            product_cost_eur=5.0,
            product_cost_estimate=False,
            collects_payment=True,
            seller_handles_vat=False,
            payment_fee_pct=0.0,
            payment_fee_fixed_eur=0.0,
            api_publishes_to_store=True,
            api_test_mode=True,
        )
        assert PL.score(cheap, 24.90) > PL.score(PL.HOPLIX, 24.90)
        monkeypatch.setattr(PL, "PLATFORMS", (PL.HOPLIX, cheap))
        assert PL.compare(24.90)["chosen"] == "printify"

    def test_prezzo_alto_aumenta_il_margine_di_entrambe(self):
        low = PL.compare(20.0)
        high = PL.compare(35.0)
        margins = {
            r["key"]: (low_m["margin"]["margin"], r["margin"]["margin"])
            for r, low_m in zip(high["rows"], low["rows"])
        }
        for before, after in margins.values():
            assert after > before

    def test_campioni_e_note_presenti(self):
        for row in PL.compare()["rows"]:
            assert row["sources"]
            assert row["notes"]
            assert row["production_days"]


class TestReport:
    def test_riporta_entrambe_e_la_scelta(self):
        text = PL.format_compare(PL.compare())
        assert "Printify" in text and "Hoplix" in text
        assert "SCELTA" in text
        assert "SCELTA" in [line for line in text.splitlines() if "Hoplix" in line][0]

    def test_dichiara_che_il_vantaggio_non_e_margine(self):
        assert "non e' margine" in PL.format_compare(PL.compare())


class TestCli:
    def test_json_valido(self):
        out = subprocess.run(
            [sys.executable, str(ROOT / "platforms.py"), "--json"],
            capture_output=True,
            text=True,
            cwd=ROOT,
            check=False,
        )
        assert out.returncode == 0, out.stderr
        data = json.loads(out.stdout)
        assert data["chosen"] == "hoplix"
        assert len(data["rows"]) == 2

    def test_prezzo_impossibile_esce_con_2(self):
        out = subprocess.run(
            [sys.executable, str(ROOT / "platforms.py"), "--price", "0"],
            capture_output=True,
            text=True,
            cwd=ROOT,
            check=False,
        )
        assert out.returncode == 2


class TestTripwire:
    def test_nessuna_rete(self):
        source = (ROOT / "platforms.py").read_text(encoding="utf-8")
        for token in ("import requests", "httpx", "urllib.request", "socket"):
            assert token not in source

    def test_import_leggero(self):
        code = "import platforms, sys; assert 'tracker' not in sys.modules"
        subprocess.run([sys.executable, "-c", code], cwd=ROOT, check=True)
