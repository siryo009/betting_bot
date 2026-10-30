"""Test del publisher Etsy/Printify — TUTTI offline.

Il trasporto HTTP e' finto: nessuna richiesta di rete, nessun token, nessuna
spesa. I test verificano le cose che, se sbagliate, costano soldi veri:
- i limiti di Etsy (titolo, tag) bloccano PRIMA dell'invio;
- i prezzi non finiscono sotto il costo;
- il registro impedisce di pubblicare due volte lo stesso listing;
- un errore dell'API non viene mai presentato come successo.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import publish as P

ROOT = Path(__file__).resolve().parent
LISTINGS = ROOT / "LISTINGS.csv"


# ---------------------------------------------------------------------------
# finto trasporto HTTP
# ---------------------------------------------------------------------------


class FakeTransport:
    """Risponde come Printify, registrando le chiamate fatte."""

    def __init__(self, *, publish_status: int = 200, upload_without_id: bool = False, fail_status: int | None = None):
        self.calls: list[tuple[str, str, dict]] = []
        self.publish_status = publish_status
        self.upload_without_id = upload_without_id
        self.fail_status = fail_status
        self._upload_seq = 0
        self._product_seq = 0
        self.last_product_payload: dict | None = None

    def __call__(self, method, url, body, headers):
        self.calls.append((method, url, body or {}))
        assert headers["Authorization"].startswith("Bearer ")
        assert headers["User-Agent"]
        if self.fail_status:
            return self.fail_status, {"error": "boom"}
        path = url.split("/v1", 1)[-1]
        if path.startswith("/shops.json"):
            return 200, [{"id": 999, "title": "Tech Shop", "sales_channel": "Etsy"}]
        if path.startswith("/catalog/blueprints.json"):
            return 200, {"data": [{"id": 6, "title": "Unisex Heavy Cotton Tee"}], "last_id": 6}
        if path == "/catalog/blueprints/6.json":
            return 200, {"id": 6, "title": "Unisex Heavy Cotton Tee"}
        if path.endswith("/print_providers.json"):
            return 200, [
                {"id": 1, "title": "US Printer", "location": {"country": "United States"}, "variant_count": 50},
                {"id": 29, "title": "EU Printer", "location": {"country": "Latvia"}, "variant_count": 90},
            ]
        if path.endswith("/variants.json"):
            variants = []
            vid = 1000
            for color, cost in (("White", 1100), ("Black", 1150), ("Radish", 1200)):
                for size in ("S", "M", "L", "XL", "2XL", "3XL"):
                    vid += 1
                    variants.append(
                        {
                            "id": vid,
                            "title": f"{color} / {size}",
                            "options": {"color": color, "size": size},
                            "cost": cost,
                        }
                    )
            return 200, {"variants": variants}
        if path.startswith("/uploads/images.json"):
            self._upload_seq += 1
            if self.upload_without_id:
                return 200, {"preview_url": "x"}
            return 200, {"id": f"img-{self._upload_seq}"}
        if path.startswith("/shops/999/products.json"):
            self._product_seq += 1
            self.last_product_payload = body
            return 200, {"id": f"prod-{self._product_seq}", "title": body.get("title")}
        if path.endswith("/publish.json"):
            return self.publish_status, {"ok": self.publish_status < 400}
        return 404, {"error": f"unexpected {path}"}


def settings(**over) -> P.Settings:
    base = dict(
        token="fake-token",
        shop_id="999",
        blueprint_id=6,
        print_provider_id=None,
        sizes=P.DEFAULT_SIZES,
        colors=P.DEFAULT_COLORS,
        image_scale=0.85,
        price_floor=24.99,
        markup=2.2,
        price_mode="markup",
        user_agent="test-agent",
    )
    base.update(over)
    return P.Settings(**base)


def make_listing(**over) -> P.Listing:
    base = dict(
        listing_id="t1",
        design="TEST",
        product_type="tee",
        title="Regalo tecnologico divertente per programmatori",
        description="Descrizione " * 20,
        tags=tuple(f"tag {i}" for i in range(13)),
        image_light=ROOT / "missing-light.png",
        image_dark=None,
        price_floor=None,
    )
    base.update(over)
    return P.Listing(**base)


@pytest.fixture()
def real_listings():
    return P.read_listings(LISTINGS)


# ---------------------------------------------------------------------------
# il CSV reale: è il test che conta di più
# ---------------------------------------------------------------------------


class TestListingReali:
    def test_cinque_listing(self, real_listings):
        assert len(real_listings) == 5
        assert {l.product_type for l in real_listings} == {"tee"}

    def test_tutte_le_immagini_esistono(self, real_listings):
        for listing in real_listings:
            assert listing.image_light.exists(), listing.image_light
            assert listing.image_dark is not None
            assert listing.image_dark.exists(), listing.image_dark

    def test_tutti_passano_la_validazione_con_i_termini_ip(self, real_listings):
        terms = P.load_ip_terms()
        assert len(terms) > 50  # la lista del cantiere Redbubble è presente
        for listing in real_listings:
            issues = P.validate_listing(listing, ip_terms=terms)
            assert issues.blocking() == [], (listing.listing_id, issues.blocking())

    def test_titoli_e_tag_entro_i_limiti_etsy(self, real_listings):
        for listing in real_listings:
            assert len(listing.title) <= P.ETSY_TITLE_MAX
            assert len(listing.tags) == P.ETSY_TAGS_MAX
            assert all(len(t) <= P.ETSY_TAG_LEN_MAX for t in listing.tags)

    def test_id_univoci(self, real_listings):
        ids = [l.listing_id for l in real_listings]
        assert len(ids) == len(set(ids))


# ---------------------------------------------------------------------------
# validazione
# ---------------------------------------------------------------------------


class TestValidazione:
    def test_titolo_troppo_lungo(self):
        issues = P.validate_listing(make_listing(title="x" * 141))
        assert any("141 caratteri" in i for i in issues)

    def test_titolo_al_limite_passa(self):
        issues = P.validate_listing(make_listing(title="x" * 140))
        assert not any("titolo" in i for i in issues)

    def test_tag_oltre_il_massimo(self):
        issues = P.validate_listing(make_listing(tags=tuple(f"tag {i}" for i in range(14))))
        assert any("14 tag" in i for i in issues)

    def test_tag_lungo(self):
        tags = tuple(f"tag {i}" for i in range(12)) + ("questo-tag-e-troppo-lungo",)
        issues = P.validate_listing(make_listing(tags=tags))
        assert any("troppo-lungo" in i for i in issues)

    def test_tag_duplicato(self):
        tags = tuple(f"tag {i}" for i in range(12)) + ("Tag 0",)
        issues = P.validate_listing(make_listing(tags=tags))
        assert any("duplicato" in i for i in issues)

    def test_descrizione_corta(self):
        issues = P.validate_listing(make_listing(description="corta"))
        assert any("descrizione troppo corta" in i for i in issues)

    def test_immagine_mancante(self):
        issues = P.validate_listing(make_listing())
        assert any("immagine assente" in i for i in issues)

    def test_senza_immagine_scura_e_un_avviso_non_un_blocco(self):
        listing = make_listing(image_light=LISTINGS)  # file che esiste
        issues = P.validate_listing(listing)
        assert issues.blocking() == []  # nessun blocco
        assert any(i.startswith("avviso:") for i in issues)

    def test_termine_ip_bloccato(self):
        listing = make_listing(title="Maglietta Apple per tutti", image_light=LISTINGS)
        issues = P.validate_listing(listing, ip_terms=("apple",))
        assert any("IP protetto" in i for i in issues)

    def test_termine_ip_non_scatta_dentro_una_parola(self):
        listing = make_listing(title="Maglietta pineapple per tutti", image_light=LISTINGS)
        issues = P.validate_listing(listing, ip_terms=("apple",))
        assert not any("IP protetto" in i for i in issues)

    def test_elisione_italiana_non_e_un_marchio(self):
        # Regressione: il marchio "dell" colpiva "dell'ultimo minuto".
        listing = make_listing(title="Idea regalo dell'ultimo minuto", image_light=LISTINGS)
        issues = P.validate_listing(listing, ip_terms=("dell",))
        assert not any("IP protetto" in i for i in issues)

    def test_il_marchio_vero_resta_rilevato(self):
        listing = make_listing(title="Maglietta Dell XPS per tutti", image_light=LISTINGS)
        issues = P.validate_listing(listing, ip_terms=("dell",))
        assert any("IP protetto" in i for i in issues)

    def test_blocking_ignora_gli_avvisi(self, real_listings):
        listing = real_listings[0]
        issues = P.validate_listing(listing)
        assert issues.ok and issues.blocking() == []


class TestLetturaCsv:
    def test_file_mancante(self):
        with pytest.raises(P.PublishError) as exc:
            P.read_listings(ROOT / "nope.csv")
        assert exc.value.code == "listings_missing"

    def test_colonne_mancanti(self, tmp_path):
        bad = tmp_path / "bad.csv"
        bad.write_text("listing_id,title\nx,y\n", encoding="utf-8")
        with pytest.raises(P.PublishError) as exc:
            P.read_listings(bad)
        assert exc.value.code == "csv_columns"

    def test_tag_separati_da_punto_e_virgola(self, tmp_path):
        path = tmp_path / "l.csv"
        path.write_text(
            "listing_id,design,product_type,title,description,tags,image_light,image_dark,price_floor\n"
            'x,D,tee,Titolo,"' + "desc " * 40 + '","uno;due,tre",light.png,,29.5\n',
            encoding="utf-8",
        )
        rows = P.read_listings(path)
        assert rows[0].tags == ("uno", "due", "tre")
        assert rows[0].price_floor == 29.5
        assert rows[0].image_dark is None


# ---------------------------------------------------------------------------
# prezzi
# ---------------------------------------------------------------------------


class TestPrezzi:
    def test_markup_arrotonda_a_99_per_eccesso(self):
        price, reason = P.price_for(13.0, settings=settings())
        assert price == 28.99  # 13 * 2.2 = 28.6 -> 28.99
        assert price > 28.6
        assert reason == "costo x markup"

    def test_mai_sotto_il_pavimento(self):
        price, _ = P.price_for(5.0, settings=settings())
        assert price == 24.99

    def test_costo_ignoto_usa_il_prezzo_fisso(self):
        price, reason = P.price_for(None, settings=settings())
        assert price == 24.99
        assert "ignoto" in reason

    def test_modo_fisso_ignora_il_margine(self):
        price, reason = P.price_for(30.0, settings=settings(price_mode="fixed"))
        assert price == 24.99
        assert reason == "prezzo fisso"

    def test_margine_sempre_positivo(self):
        for cost in (8.0, 11.0, 14.5, 19.0):
            price, _ = P.price_for(cost, settings=settings())
            assert price - cost > 4.0


# ---------------------------------------------------------------------------
# configurazione
# ---------------------------------------------------------------------------


class TestSettings:
    def test_default_senza_env(self):
        s = P.Settings.from_env({})
        assert s.sizes == P.DEFAULT_SIZES
        assert s.colors == P.DEFAULT_COLORS
        assert s.blueprint_id is None
        assert s.price_mode == "fixed"

    def test_env_letta(self):
        s = P.Settings.from_env(
            {
                "PRINTIFY_TOKEN": "abc",
                "PRINTIFY_SHOP_ID": "123",
                "PRINTIFY_BLUEPRINT": "42",
                "PRINTIFY_SIZES": "M,L",
                "PRINTIFY_COLORS": "Black",
                "PRINTIFY_PRICE_MODE": "markup",
                "PRINTIFY_MARKUP": "2.8",
            }
        )
        assert s.token == "abc"
        assert s.blueprint_id == 42
        assert s.sizes == ("M", "L")
        assert s.colors == ("Black",)
        assert s.price_mode == "markup"
        assert s.markup == 2.8

    def test_valori_impossibili_ricadono_sul_default(self):
        s = P.Settings.from_env({"PRINTIFY_IMAGE_SCALE": "abc", "PRINTIFY_MARKUP": "-3", "PRINTIFY_PRICE_MODE": "boh"})
        assert s.image_scale == P.DEFAULT_IMAGE_SCALE
        assert s.markup == P.DEFAULT_MARKUP
        assert s.price_mode == "fixed"

    def test_liste_vuote_ricadono_sul_default(self):
        s = P.Settings.from_env({"PRINTIFY_SIZES": " , ", "PRINTIFY_COLORS": ""})
        assert s.sizes == P.DEFAULT_SIZES
        assert s.colors == P.DEFAULT_COLORS


# ---------------------------------------------------------------------------
# catalogo
# ---------------------------------------------------------------------------


class TestCatalogo:
    def test_fornitore_europeo_preferito(self):
        providers = [
            {"id": 1, "title": "US", "location": {"country": "United States"}},
            {"id": 29, "title": "EU", "location": {"country": "Latvia"}},
        ]
        assert P.choose_provider(providers)["id"] == 29

    def test_preferenza_esplicita_vince(self):
        providers = [
            {"id": 1, "title": "US", "location": {"country": "Mexico"}},
            {"id": 29, "title": "EU", "location": {"country": "Latvia"}},
        ]
        assert P.choose_provider(providers, preferred=1)["id"] == 1

    def test_senza_fornitori(self):
        assert P.choose_provider([]) is None

    def test_filtro_taglie_e_colori(self):
        raw = [
            {"id": 1, "title": "White / S", "options": {"color": "White", "size": "S"}, "cost": 1100},
            {"id": 2, "title": "White / 3XL", "options": {"color": "White", "size": "3XL"}, "cost": 1200},
            {"id": 3, "title": "Radish / S", "options": {"color": "Radish", "size": "S"}, "cost": 1100},
            {"id": 4, "title": "Black / M", "options": {"color": "Black", "size": "M"}, "cost": 1150},
        ]
        kept = P.build_variants(raw, settings=settings(), want_dark=False)
        assert [v.id for v in kept] == [1]

    def test_colori_scuri_tenuti_solo_con_immagine(self):
        raw = [{"id": 4, "title": "Black / M", "options": {"color": "Black", "size": "M"}, "cost": 1150}]
        assert P.build_variants(raw, settings=settings(), want_dark=False) == []
        assert [v.id for v in P.build_variants(raw, settings=settings(), want_dark=True)] == [4]

    def test_costo_in_centesimi(self):
        raw = [{"id": 1, "title": "White / S", "options": {"color": "White", "size": "S"}, "cost": 999}]
        v = P.build_variants(raw, settings=settings(), want_dark=False)[0]
        assert v.cost == 9.99

    def test_costo_float_e_gia_in_unita(self):
        raw = [{"id": 1, "title": "White / S", "options": {"color": "White", "size": "S"}, "cost": 9.99}]
        v = P.build_variants(raw, settings=settings(), want_dark=False)[0]
        assert v.cost == 9.99

    def test_bool_non_e_un_costo(self):
        raw = [{"id": 1, "title": "White / S", "options": {"color": "White", "size": "S"}, "cost": True}]
        assert P.build_variants(raw, settings=settings(), want_dark=False)[0].cost is None

    def test_resolve_catalog_con_blueprint_esplicito(self):
        client = P.PrintifyClient(settings(), transport=FakeTransport())
        choice = P.resolve_catalog(client, settings())
        assert choice.blueprint_id == 6
        assert choice.print_provider_id == 29
        assert choice.provider_country == "Latvia"
        assert len(choice.variants) == 5  # White S..2XL

    def test_resolve_catalog_senza_blueprint_esplicito_cerca_per_titolo(self):
        client = P.PrintifyClient(settings(blueprint_id=None), transport=FakeTransport())
        choice = P.resolve_catalog(client, settings(blueprint_id=None))
        assert choice.blueprint_id == 6

    def test_blueprint_non_trovato(self):
        client = P.PrintifyClient(settings(blueprint_id=None), transport=FakeTransport())
        with pytest.raises(P.PublishError) as exc:
            P.resolve_catalog(client, settings(blueprint_id=None, blueprint_title="Cappello Inesistente"))
        assert exc.value.code == "blueprint_missing"

    def test_senza_client_niente_catalogo(self):
        with pytest.raises(P.PublishError) as exc:
            P.resolve_catalog(None, settings())
        assert exc.value.code == "no_catalog"


# ---------------------------------------------------------------------------
# payload
# ---------------------------------------------------------------------------


class TestPayload:
    def _choice(self, want_dark=True):
        client = P.PrintifyClient(settings(), transport=FakeTransport())
        return client, P.resolve_catalog(client, settings(), listing=make_listing(image_dark=Path("x.png")) if want_dark else None)

    def test_prezzi_in_centesimi(self):
        client, choice = self._choice()
        listing = make_listing(image_light=LISTINGS, image_dark=LISTINGS, tags=tuple(f"tag {i}" for i in range(13)))
        payload = P.build_product_payload(listing, choice, {"light": "a", "dark": "b"}, settings=settings())
        assert all(isinstance(v["price"], int) for v in payload["variants"])
        assert all(v["price"] >= 2499 for v in payload["variants"])

    def test_una_print_area_per_gruppo_di_colore(self):
        client, choice = self._choice()
        listing = make_listing(image_light=LISTINGS, image_dark=LISTINGS)
        payload = P.build_product_payload(listing, choice, {"light": "a", "dark": "b"}, settings=settings())
        assert len(payload["print_areas"]) == 2
        ids = {area["placeholders"][0]["images"][0]["id"] for area in payload["print_areas"]}
        assert ids == {"a", "b"}

    def test_solo_capi_chiari_una_sola_area(self):
        client = P.PrintifyClient(settings(), transport=FakeTransport())
        choice = P.resolve_catalog(client, settings())
        listing = make_listing(image_light=LISTINGS, image_dark=None)
        payload = P.build_product_payload(listing, choice, {"light": "a"}, settings=settings())
        assert len(payload["print_areas"]) == 1
        assert payload["print_areas"][0]["placeholders"][0]["position"] == "front"

    def test_tag_troncati_al_massimo_printify(self):
        client, choice = self._choice()
        listing = make_listing(image_light=LISTINGS, image_dark=LISTINGS, tags=tuple(f"t{i}" for i in range(20)))
        payload = P.build_product_payload(listing, choice, {"light": "a", "dark": "b"}, settings=settings())
        assert len(payload["tags"]) == P.PRINTIFY_TAGS_MAX

    def test_senza_varianti_errore(self):
        client, choice = self._choice()
        choice.variants = []
        with pytest.raises(P.PublishError) as exc:
            P.build_product_payload(make_listing(), choice, {"light": "a"}, settings=settings())
        assert exc.value.code == "no_variants"

    def test_senza_immagini_errore(self):
        client, choice = self._choice()
        with pytest.raises(P.PublishError) as exc:
            P.build_product_payload(make_listing(), choice, {}, settings=settings())
        assert exc.value.code == "no_print_areas"


# ---------------------------------------------------------------------------
# client
# ---------------------------------------------------------------------------


class TestClient:
    def test_senza_token_fail_closed(self):
        with pytest.raises(P.PublishError) as exc:
            P.PrintifyClient(settings(token=""))
        assert exc.value.code == "token_missing"

    def test_errore_http(self):
        client = P.PrintifyClient(settings(), transport=FakeTransport(fail_status=500))
        with pytest.raises(P.PublishError) as exc:
            client.shops()
        assert exc.value.code == "http_error"
        assert "500" in str(exc.value)

    def test_upload_senza_id_non_crea_prodotti(self):
        client = P.PrintifyClient(settings(), transport=FakeTransport(upload_without_id=True))
        with pytest.raises(P.PublishError) as exc:
            client.upload_image(LISTINGS)
        assert exc.value.code == "upload_without_id"

    def test_upload_restituisce_id(self):
        client = P.PrintifyClient(settings(), transport=FakeTransport())
        assert client.upload_image(LISTINGS) == "img-1"

    def test_check_riconosce_etsy(self):
        client = P.PrintifyClient(settings(), transport=FakeTransport())
        info = P.check(settings(), client)
        assert info["etsy_connected"] is True
        assert info["shop_id"] == 999


# ---------------------------------------------------------------------------
# dry-run
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_senza_token_tutto_valido_ma_catalogo_assente(self, real_listings):
        report = P.dry_run(real_listings, settings=settings(token=""), client=None)
        assert report["ok"] == 0
        assert report["blocked"] == 0
        assert report["needs_token"] == 5
        assert report["catalog"] == "non disponibile"
        assert all(e["validation_ok"] for e in report["listings"])

    def test_con_catalogo_pianifica_tutto(self, real_listings):
        client = P.PrintifyClient(settings(), transport=FakeTransport())
        report = P.dry_run(real_listings, settings=settings(), client=client)
        assert report["ok"] == 5
        assert report["blocked"] == 0
        assert report["catalog"] == "letto"
        entry = report["listings"][0]
        assert entry["price"] >= 24.99
        assert entry["margin"] is not None and entry["margin"] > 0
        assert entry["product_payload"]["print_areas"]

    def test_listing_invalido_blocca_solo_se_stesso(self):
        bad = make_listing(listing_id="bad", title="x" * 200)
        good = make_listing(listing_id="good", image_light=LISTINGS)
        report = P.dry_run([bad, good], settings=settings(token=""), client=None)
        assert report["blocked"] == 1
        blocked = [e for e in report["listings"] if not e["ok"] and not e.get("needs_token")]
        assert blocked[0]["listing_id"] == "bad"
        assert blocked[0]["error_code"] == "invalid_listing"

    def test_gia_pubblicato_segnalato(self, real_listings):
        ledger = {"listings": {real_listings[0].listing_id: {"product_id": "prod-x"}}}
        report = P.dry_run(real_listings, settings=settings(token=""), client=None, ledger=ledger)
        assert report["already_published"] == 1
        assert report["listings"][0]["product_id"] == "prod-x"

    def test_nessuna_chiamata_di_scrittura_in_dry_run(self, real_listings):
        transport = FakeTransport()
        client = P.PrintifyClient(settings(), transport=transport)
        P.dry_run(real_listings, settings=settings(), client=client)
        writes = [(m, u) for m, u, _ in transport.calls if m == "POST"]
        assert writes == []


# ---------------------------------------------------------------------------
# pubblicazione
# ---------------------------------------------------------------------------


class TestPubblicazione:
    def test_crea_pubblica_e_registra(self, tmp_path):
        transport = FakeTransport()
        client = P.PrintifyClient(settings(), transport=transport)
        listing = P.read_listings(LISTINGS)[0]
        choice = P.resolve_catalog(client, settings(), listing=listing)
        ledger = {"listings": {}}
        ledger_path = tmp_path / "published.json"

        result = P.publish_listing(
            listing, settings=settings(), client=client, choice=choice, ledger=ledger, ledger_path=ledger_path
        )
        assert result["published"] is True
        assert result["product_id"] == "prod-1"
        saved = json.loads(ledger_path.read_text())
        assert saved["listings"][listing.listing_id]["product_id"] == "prod-1"

        payload = transport.last_product_payload
        assert payload["title"] == listing.title
        assert payload["print_provider_id"] == 29

    def test_idempotente_sul_registro(self, tmp_path):
        transport = FakeTransport()
        client = P.PrintifyClient(settings(), transport=transport)
        listing = P.read_listings(LISTINGS)[0]
        choice = P.resolve_catalog(client, settings(), listing=listing)
        ledger = {"listings": {listing.listing_id: {"product_id": "prod-old"}}}

        result = P.publish_listing(
            listing, settings=settings(), client=client, choice=choice, ledger=ledger, ledger_path=tmp_path / "l.json"
        )
        assert result["skipped"] is True
        assert result["product_id"] == "prod-old"
        assert [m for m, _, _ in transport.calls if m == "POST"] == []

    def test_publish_fallito_non_diventa_successo(self, tmp_path):
        transport = FakeTransport(publish_status=500)
        client = P.PrintifyClient(settings(), transport=transport)
        listing = P.read_listings(LISTINGS)[0]
        choice = P.resolve_catalog(client, settings(), listing=listing)
        ledger = {"listings": {}}
        result = P.publish_listing(
            listing, settings=settings(), client=client, choice=choice, ledger=ledger, ledger_path=tmp_path / "l.json"
        )
        assert result["published"] is False
        assert "HTTP 500" in (result["publish_error"] or "")
        assert ledger["listings"][listing.listing_id]["published"] is False


class TestRegistro:
    def test_registro_assente(self, tmp_path):
        assert P.load_ledger(tmp_path / "nope.json") == {"listings": {}}

    def test_registro_corrotto_non_sovrascritto(self, tmp_path):
        path = tmp_path / "l.json"
        path.write_text("{rotto", encoding="utf-8")
        data = P.load_ledger(path)
        assert data["listings"] == {}
        assert "error" in data

    def test_salvataggio_atomico(self, tmp_path):
        path = tmp_path / "sub" / "l.json"
        assert P.save_ledger({"listings": {"a": {"product_id": "1"}}}, path) is True
        assert json.loads(path.read_text())["listings"]["a"]["product_id"] == "1"


# ---------------------------------------------------------------------------
# report e CLI
# ---------------------------------------------------------------------------


class TestReportECli:
    def test_report_blocchi_e_pronti(self, real_listings):
        report = P.dry_run(real_listings[:2], settings=settings(token=""), client=None)
        text = P.format_report(report)
        assert "PUBLISHER ETSY/PRINTIFY" in text
        assert "in attesa di token: 2" in text
        # Senza token il listing e' valido: non va etichettato come bloccato.
        assert "[ATTESA TOKEN]" in text
        assert "[BLOCCATO]" not in text

    def test_cli_dry_run_senza_token_esce_zero(self, monkeypatch, capsys, tmp_path):
        for key in ("PRINTIFY_TOKEN", "PRINTIFY_SHOP_ID"):
            monkeypatch.delenv(key, raising=False)
        code = P.main(["--dry-run", "--json", "--ledger", str(tmp_path / "l.json")])
        assert code == 0
        out = json.loads(capsys.readouterr().out)
        assert out["mode"] == "dry-run"
        assert out["needs_token"] == 5

    def test_cli_check_senza_token_esce_due(self, monkeypatch, capsys):
        monkeypatch.delenv("PRINTIFY_TOKEN", raising=False)
        assert P.main(["--check"]) == 2

    def test_cli_solo_listing_scelti(self, monkeypatch, capsys, tmp_path):
        monkeypatch.delenv("PRINTIFY_TOKEN", raising=False)
        code = P.main(
            ["--dry-run", "--json", "--only", "tech-tee-404-regalo", "--ledger", str(tmp_path / "l.json")]
        )
        assert code == 0
        out = json.loads(capsys.readouterr().out)
        assert out["n_listings"] == 1
        assert out["listings"][0]["listing_id"] == "tech-tee-404-regalo"

    def test_cli_live_senza_shop_id_esce_due(self, monkeypatch):
        monkeypatch.setenv("PRINTIFY_TOKEN", "x")
        monkeypatch.delenv("PRINTIFY_SHOP_ID", raising=False)
        assert P.main(["--live"]) == 2

    def test_report_scritto_su_file(self, monkeypatch, tmp_path):
        monkeypatch.delenv("PRINTIFY_TOKEN", raising=False)
        target = tmp_path / "out" / "report.json"
        assert P.main(["--dry-run", "--json", "--report", str(target), "--ledger", str(tmp_path / "l.json")]) == 0
        assert json.loads(target.read_text())["mode"] == "dry-run"


# ---------------------------------------------------------------------------
# tripwire
# ---------------------------------------------------------------------------


class TestTripwire:
    def test_importare_il_modulo_non_carica_la_rete(self):
        code = (
            "import sys; import publish; "
            "print('requests' in sys.modules)"
        )
        out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == "False"

    def test_nessuna_credenziale_scritta_nel_sorgente(self):
        source = (ROOT / "publish.py").read_text(encoding="utf-8")
        assert "Bearer " not in source.replace('f"Bearer {', "").replace("Bearer {self", "")
        assert re_search_secret(source) is None

    def test_nessun_percorso_di_ordine_o_di_denaro(self):
        source = (ROOT / "publish.py").read_text(encoding="utf-8")
        for forbidden in ("auto_bet", "execution_engine", "tracker", "place_order"):
            assert forbidden not in source


def re_search_secret(source: str):
    """Cerca token in chiaro (32+ caratteri alfanumerici) nel sorgente."""
    import re

    for line in source.splitlines():
        if line.strip().startswith("#") or line.strip().startswith('"""'):
            continue
        for match in re.finditer(r"[A-Za-z0-9_\-]{32,}", line):
            token = match.group(0)
            if any(ch.isdigit() for ch in token) and any(ch.isalpha() for ch in token):
                return token
    return None
