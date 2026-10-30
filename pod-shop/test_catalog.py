"""Test del generatore di catalogo — offline.

Il catalogo e' il ponte fra i file veri e due destinazioni che costano soldi se
sbagliate: le schede da incollare nello store e la vetrina del sito. I test
fissano che i numeri vengano dai file (non da costanti scritte a mano) e che il
manifest pubblico NON contenga dati interni come il margine.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import catalog as C
import hoplix
import platforms as PL
from publish import PublishError, read_listings

ROOT = Path(__file__).resolve().parent
LISTINGS = ROOT / "LISTINGS.csv"
DESIGN_DIR = ROOT.parent / "redbubble-tech-gift-2026-work" / "output" / "upload" / "design"
SAMPLE = DESIGN_DIR / "ctrl-alt-regala" / "tee-ctrl-alt-regala.png"


def rows(**over):
    kwargs = dict(base_cost=PL.HOPLIX.product_cost_eur)
    kwargs.update(over)
    return C.build_rows(read_listings(LISTINGS), **kwargs)


class TestPng:
    def test_dimensioni_reali(self):
        assert C.png_size(SAMPLE) == (3600, 4800)

    def test_file_che_non_e_png(self, tmp_path):
        fake = tmp_path / "x.png"
        fake.write_bytes(b"non sono un png")
        with pytest.raises(ValueError):
            C.png_size(fake)


class TestRighe:
    def test_cinque_schede_chiare(self):
        assert len(rows()) == 5

    def test_dieci_con_capo_scuro(self):
        dark = rows(include_dark=True)
        assert len(dark) == 10
        assert {r.ink for r in dark} == {"light", "dark"}

    def test_inchiostro_su_colore_compatibile(self):
        for row in rows(include_dark=True):
            assert row.colors == (("white",) if row.ink == "light" else ("black",))

    def test_prezzo_dal_csv(self):
        assert {r.price for r in rows()} == {24.99}

    def test_margine_coerente_col_prezzo(self):
        for row in rows():
            expected = row.price / (1 + PL.VAT_IT) - row.base_cost
            assert row.margin == pytest.approx(expected, abs=0.01)
            assert row.margin > 0

    def test_override_di_prezzo(self):
        assert {r.price for r in rows(price=29.9)} == {29.9}

    def test_costo_alto_abbassa_il_margine(self):
        cheap = rows()[0].margin
        pricey = rows(base_cost=12.0)[0].margin
        assert pricey < cheap

    def test_taglie_e_area(self):
        row = rows()[0]
        assert row.sizes == ("S", "M", "L", "XL", "2XL")
        assert row.width == 3600 and row.height == 4800
        # 3:4 dentro 4:5 -> entra, con margine laterale
        assert "margine laterale" in (row.area_warning or "")

    def test_immagine_mancante_blocca(self, tmp_path):
        listing = read_listings(LISTINGS)[0].__class__(
            **{
                **read_listings(LISTINGS)[0].__dict__,
                "image_light": tmp_path / "assente.png",
            }
        )
        with pytest.raises(PublishError):
            C.build_rows([listing])

    def test_nome_non_taglia_parole(self):
        name = C.store_name("Regalo Tecnologico Divertente " * 5, "light")
        assert len(name) <= 80
        assert not name.endswith(" ")
        assert "  " not in name

    def test_nome_scuro_ha_il_suffisso(self):
        assert C.store_name("SISTEMA AGGIORNATO", "dark").endswith("(capo scuro)")


class TestDocumento:
    def test_contiene_ogni_design(self):
        text = C.store_markdown(rows())
        for listing in read_listings(LISTINGS):
            assert listing.design in text

    def test_contiene_i_valori_da_incollare(self):
        text = C.store_markdown(rows())
        assert "24.99 EUR" in text
        assert "maglietta-unisex" in text
        assert "S, M, L, XL, 2XL" in text

    def test_spiega_perche_e_manuale(self):
        assert "non prodotti dello store" in C.store_markdown(rows())

    def test_avverte_sulla_regola_dell_inchiostro(self):
        text = C.store_markdown(rows(include_dark=True))
        assert "inchiostro" in text
        assert "capo **bianco**" in text and "capo **nero**" in text

    def test_store_url_se_fornito(self):
        assert "https://negozio.example" in C.store_markdown(rows(), shop_url="https://negozio.example")


class TestManifest:
    def test_struttura_ts(self):
        ts = C.webapp_ts(rows())
        assert ts.startswith("// GENERATO")
        assert "export const SHOP_PRODUCTS: ShopProduct[]" in ts
        # Si contano le VOCI (4 spazi + virgolette), non la parola `id` nel
        # blocco dei tipi o nel nome della costante di valuta.
        assert ts.count('    id: "') == 5

    def test_solo_dati_pubblici(self):
        ts = C.webapp_ts(rows())
        for secret in ("margin", "base_cost", "7.9"):
            assert secret not in ts

    def test_immagine_e_prezzo(self):
        ts = C.webapp_ts(rows())
        assert "/shop/tech-tee-ctrl-alt-regala-light.png" in ts
        assert "price: 24.99" in ts

    def test_scrittura_su_disco(self, tmp_path):
        out = C.write_webapp(rows(), manifest=tmp_path / "shop.ts", images_dir=tmp_path / "img")
        assert out["products"] == 5
        assert len(out["copied"]) == 5
        assert (tmp_path / "shop.ts").exists()
        assert all((tmp_path / "img" / n).exists() for n in out["copied"])

    def test_file_di_stampa_copiati_col_nome_originale(self, tmp_path):
        """Hoplix ricostruisce il disegno dal NOME del file, non dalla chiave."""
        out = C.write_webapp(rows(), manifest=tmp_path / "shop.ts", images_dir=tmp_path / "img")
        design_dir = tmp_path / "img" / "design"
        names = sorted(p.name for p in design_dir.iterdir())
        assert names == out["designs"]
        assert out["designs_missing"] == []
        # `tee-*.png` e' il nome con cui il disegno e' stato generato: la chiave
        # della scheda (tech-tee-...-light) non esiste come file di stampa.
        assert all(n.startswith("tee-") for n in names)

    def test_anche_il_capo_scuro_viene_pubblicato(self, tmp_path):
        dark = tmp_path / "tee-dark-prova.png"
        dark.write_bytes(b"\x89PNG\r\n\x1a\n")
        row = rows()[0].__class__(**{**rows()[0].__dict__, "image_dark": dark})
        out = C.write_webapp([row], manifest=tmp_path / "s.ts", images_dir=tmp_path / "img")
        assert dark.name in out["designs"]
        assert (tmp_path / "img" / "design" / dark.name).exists()

    def test_immagine_troppo_pesante_saltata(self, tmp_path):
        big = tmp_path / "grande.png"
        big.write_bytes(b"\x00" * (C.MAX_WEB_IMAGE_BYTES + 1))
        row = rows()[0].__class__(**{**rows()[0].__dict__, "image": big})
        out = C.write_webapp([row], manifest=tmp_path / "s.ts", images_dir=tmp_path / "img")
        assert out["copied"] == []
        assert out["skipped"][0]["key"] == row.key


class TestCli:
    def test_json(self):
        out = subprocess.run(
            [sys.executable, str(ROOT / "catalog.py"), "--json"],
            capture_output=True,
            text=True,
            cwd=ROOT,
            check=False,
        )
        assert out.returncode == 0, out.stderr
        data = json.loads(out.stdout)
        assert len(data["rows"]) == 5
        assert data["margin_total"] > 0


class TestTripwire:
    def test_nessuna_rete(self):
        source = (ROOT / "catalog.py").read_text(encoding="utf-8")
        for token in ("import requests", "httpx", "urllib.request"):
            assert token not in source

    def test_colori_da_una_sola_fonte(self):
        """La mappa inchiostro->colori vive in hoplix.py, non duplicata qui."""
        assert C.INK_COLORS is hoplix.INK_COLORS
