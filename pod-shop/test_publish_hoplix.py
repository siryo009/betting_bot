"""Test delle campagne Hoplix — TUTTI offline (trasporto finto).

Le campagne sono l'unico pezzo che l'API sa creare e che possiamo condividere
come link: se il payload e' sbagliato la campagna non nasce, se il prezzo e'
sotto il costo Hoplix la rifiuta, e se il registro non e' idempotente si
creerebbero doppioni a ogni esecuzione.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import hoplix
import platforms as PL
import publish_hoplix as PH
from publish import read_listings

ROOT = Path(__file__).resolve().parent
LISTINGS = ROOT / "LISTINGS.csv"

PRODUCT = hoplix.Product(
    code="maglietta-unisex",
    name="Maglietta unisex",
    colors=("white", "black"),
    sizes=("S", "M", "L", "XL", "XXL"),
    base_cost=7.5,
    printable_area="320x400",
)


def listings():
    return read_listings(LISTINGS)


def settings(**over) -> PH.Settings:
    base = dict(
        api_key="",
        secret="",
        design_base="https://sito.example/shop",
        price_floor=24.90,
        ink_mode="light",
        test_mode=True,
    )
    base.update(over)
    return PH.Settings(**base)


class FakeTransport:
    def __init__(self, *, campaign_ok: bool = True):
        self.calls: list[tuple[str, dict]] = []
        self.campaign_ok = campaign_ok
        self._seq = 0

    def __call__(self, method, url, body, headers):
        path = url.split("/v1", 1)[-1]
        self.calls.append((path, dict(body or {})))
        if path.startswith("/product-info/"):
            return 200, {
                "status": 200,
                "product-info": {
                    "product-code": "maglietta-unisex",
                    "name": "Maglietta unisex",
                    "colors": "white,black",
                    "sizes": "S,M,L,XL,XXL",
                    "base-cost": "7.50",
                    "printable-area": "320x400",
                },
            }
        if path == "/create-campaign":
            self._seq += 1
            if not self.campaign_ok:
                return 200, {"status": 201, "message": "The price must be greater than the base cost"}
            return 200, {"status": 200, "campaign_id": f"camp-{self._seq}", "link": f"https://hoplix.shop/camp-{self._seq}"}
        return 404, {"error": f"unexpected {path}"}


def client(**over) -> hoplix.HoplixClient:
    kwargs = dict(api_key="k", secret="s", transport=FakeTransport())
    kwargs.update(over)
    return hoplix.HoplixClient(**kwargs)


class TestSettings:
    def test_default_prudente(self):
        s = PH.Settings.from_env({})
        assert s.price_floor == pytest.approx(PL.DEFAULT_PRICE_EUR)
        assert s.ink_mode == "light"
        assert s.test_mode is True
        assert s.design_base == ""

    def test_ink_invalido_ricade_su_light(self):
        assert PH.Settings.from_env({"HOPLIX_INK": "arcobaleno"}).ink_mode == "light"
        assert PH.Settings.from_env({"HOPLIX_INK": "both"}).ink_mode == "both"

    def test_prezzo_impossibile_ricade_sul_default(self):
        assert PH.Settings.from_env({"HOPLIX_PRICE_FLOOR": "0"}).price_floor == pytest.approx(PL.DEFAULT_PRICE_EUR)
        assert PH.Settings.from_env({"HOPLIX_PRICE_FLOOR": "abc"}).price_floor == pytest.approx(PL.DEFAULT_PRICE_EUR)

    def test_test_mode_si_spegne_solo_esplicitamente(self):
        for raw in ("0", "false", "no", "off"):
            assert PH.Settings.from_env({"HOPLIX_TEST_MODE": raw}).test_mode is False
        assert PH.Settings.from_env({"HOPLIX_TEST_MODE": "qualcosa"}).test_mode is True


class TestDesignUrl:
    def test_usa_l_immagine_giusta(self):
        listing = listings()[0]
        assert PH.design_url(listing, "light", settings()).endswith(listing.image_light.name)
        assert PH.design_url(listing, "dark", settings()).endswith(listing.image_dark.name)

    def test_senza_base_pubblica_non_inventa(self):
        with pytest.raises(Exception) as exc:
            PH.design_url(listings()[0], "light", settings(design_base=""))
        assert getattr(exc.value, "code", "") == "design_base_missing"

    def test_base_con_slash_finale(self):
        url = PH.design_url(listings()[0], "light", settings(design_base="https://x.example/shop/"))
        assert "//shop//" not in url


class TestPiano:
    def test_piano_valido(self):
        entry = PH.plan_listing(listings()[0], settings=settings(), product=PRODUCT)
        assert entry["ok"] is True
        camp = entry["campaign"]
        assert camp["product-code"] == "maglietta-unisex"
        assert camp["product-color"] == "white"
        assert camp["product-price"] == "24.99"
        assert camp["currency"] == "EUR"
        assert entry["margin"] > 0

    def test_capo_scuro_usa_il_nero(self):
        entry = PH.plan_listing(listings()[0], settings=settings(), product=PRODUCT, ink="dark")
        assert entry["ok"] is True
        assert entry["campaign"]["product-color"] == "black"
        assert "tee-dark" in entry["design_url"]

    def test_prezzo_sotto_il_costo_blocca(self):
        entry = PH.plan_listing(
            listings()[0], settings=settings(), product=hoplix.Product(**{**PRODUCT.__dict__, "base_cost": 40.0})
        )
        assert entry["ok"] is False
        assert entry["error_code"] == "price_below_base_cost"

    def test_listing_non_valido_blocca(self):
        listing = listings()[0].__class__(**{**listings()[0].__dict__, "title": "", "description": "corta"})
        entry = PH.plan_listing(listing, settings=settings(), product=PRODUCT)
        assert entry["ok"] is False
        assert entry["error_code"] == "invalid_listing"

    def test_senza_url_pubblica_segnala_attesa_non_errore(self):
        entry = PH.plan_listing(listings()[0], settings=settings(design_base=""), product=PRODUCT)
        assert entry["ok"] is False
        assert entry["needs_design_base"] is True


class TestDryRun:
    def test_conta_le_campagne(self):
        report = PH.dry_run(listings(), settings=settings(), product=PRODUCT)
        assert report["n_campaigns"] == 5
        assert report["ok"] == 5
        assert report["blocked"] == 0

    def test_ink_both_raddoppia(self):
        report = PH.dry_run(listings(), settings=settings(ink_mode="both"), product=PRODUCT)
        assert report["n_campaigns"] == 10

    def test_senza_base_pubblica_tutte_in_attesa(self):
        report = PH.dry_run(listings(), settings=settings(design_base=""), product=PRODUCT)
        assert report["needs_design_base"] == 5
        assert report["blocked"] == 0
        assert "HOPLIX_DESIGN_BASE" in PH.format_report(report)

    def test_catalogo_stimato_senza_chiavi(self):
        report = PH.dry_run(listings(), settings=settings())
        assert "stimato" in report["catalog"]
        assert report["ok"] == 5

    def test_registro_mostra_le_gia_create(self):
        ledger = {"listings": {"hoplix:tech-tee-404-regalo:light": {"campaign_id": "camp-x"}}}
        report = PH.dry_run(listings(), settings=settings(), product=PRODUCT, ledger=ledger)
        done = [c for c in report["campaigns"] if c["already_created"]]
        assert len(done) == 1 and done[0]["campaign_id"] == "camp-x"


class TestPubblicazione:
    def entry(self, **over):
        base = PH.plan_listing(listings()[0], settings=settings(), product=PRODUCT)
        base.update(over)
        return base

    def test_crea_e_registra(self, tmp_path):
        ledger_path = tmp_path / "published.json"
        ledger = {"listings": {}}
        result = PH.publish_listing(self.entry(), client=client(), ledger=ledger, ledger_path=ledger_path)
        assert result["created"] is True
        assert result["campaign_id"] == "camp-1"
        saved = json.loads(ledger_path.read_text(encoding="utf-8"))
        assert saved["listings"]["hoplix:tech-tee-ctrl-alt-regala:light"]["campaign_id"] == "camp-1"

    def test_idempotente(self, tmp_path):
        ledger_path = tmp_path / "published.json"
        ledger = {"listings": {}}
        PH.publish_listing(self.entry(), client=client(), ledger=ledger, ledger_path=ledger_path)
        again = PH.publish_listing(self.entry(), client=client(), ledger=ledger, ledger_path=ledger_path)
        assert again["skipped"] is True
        assert again["created"] is False

    def test_errore_api_non_registra(self, tmp_path):
        ledger = {"listings": {}}
        with pytest.raises(hoplix.HoplixError):
            PH.publish_listing(
                self.entry(),
                client=client(transport=FakeTransport(campaign_ok=False)),
                ledger=ledger,
                ledger_path=tmp_path / "p.json",
            )
        assert ledger["listings"] == {}


class TestCheck:
    def test_legged_catalogo_e_margine(self):
        info = PH.check(settings(price_floor=24.99), client())
        assert info["product"] == "maglietta-unisex"
        assert info["base_cost"] == 7.5
        assert info["price_ok"] is True
        assert info["margin_at_floor"] > 0

    def test_prezzo_sotto_costo_non_e_ok(self):
        info = PH.check(settings(price_floor=5.0), client())
        assert info["price_ok"] is False


class TestCli:
    def test_dry_run_senza_chiavi(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv("HOPLIX_DESIGN_BASE", "https://sito.example/shop")
        monkeypatch.delenv("HOPLIX_API_KEY", raising=False)
        rc = PH.main(["--ledger", str(tmp_path / "p.json")])
        out = capsys.readouterr().out
        assert rc == 0
        assert "CAMPAGNE HOPLIX" in out
        assert "pronte: 5" in out

    def test_live_crea_tutte_e_poi_salta(self, monkeypatch, tmp_path, capsys):
        fake = FakeTransport()
        monkeypatch.setattr(hoplix, "_requests_transport", fake)
        monkeypatch.setenv("HOPLIX_API_KEY", "k")
        monkeypatch.setenv("HOPLIX_SECRET", "s")
        monkeypatch.setenv("HOPLIX_DESIGN_BASE", "https://sito.example/shop")
        ledger = str(tmp_path / "p.json")

        rc = PH.main(["--live", "--ledger", ledger, "--json"])
        assert rc == 0
        first = json.loads(capsys.readouterr().out)
        assert first["created"] == 5 and first["errors"] == 0

        rc = PH.main(["--live", "--ledger", ledger, "--json"])
        assert rc == 0
        second = json.loads(capsys.readouterr().out)
        assert second["created"] == 0 and second["skipped"] == 5
        # una sola chiamata di creazione per campagna, non due
        creations = [c for c in fake.calls if c[0] == "/create-campaign"]
        assert len(creations) == 5

    def test_live_senza_chiavi_non_crea(self, monkeypatch, tmp_path):
        monkeypatch.delenv("HOPLIX_API_KEY", raising=False)
        monkeypatch.delenv("HOPLIX_SECRET", raising=False)
        assert PH.main(["--live", "--ledger", str(tmp_path / "p.json")]) == 2


class TestReport:
    def test_contiene_margine_e_link(self):
        report = {
            "mode": "live",
            "created": 1,
            "skipped": 0,
            "errors": 0,
            "campaigns": [
                {"listing_id": "x", "ink": "light", "ok": True, "created": True, "price": 24.99, "margin": 12.5, "margin_pct": 50.0, "slug": "x-light", "link": "https://h/x", "campaign_id": "c1"}
            ],
        }
        text = PH.format_report(report)
        assert "create: 1" in text
        assert "https://h/x" in text
        assert "margine 12.50" in text


class TestTripwire:
    def test_import_non_carica_rete(self):
        code = (
            "import sys, publish_hoplix;"
            "assert 'requests' not in sys.modules;"
            "assert 'tracker' not in sys.modules"
        )
        subprocess.run([sys.executable, "-c", code], cwd=ROOT, check=True)

    def test_nessun_ordine_reale_nel_sorgente(self):
        source = (ROOT / "publish_hoplix.py").read_text(encoding="utf-8")
        for token in ("create_order(", "save_bet(", "auto_bet", "_live_fill"):
            assert token not in source

    def test_iac_documentata_nel_readme(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        assert "HOPLIX_DESIGN_BASE" in readme
