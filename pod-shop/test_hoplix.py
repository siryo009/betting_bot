"""Test del client Hoplix — TUTTI offline (trasporto finto).

Le cose che, se sbagliate, costano soldi veri o producono un ordine fantasma:
- Hoplix risponde HTTP 200 anche sugli errori: va letto il `status` nel corpo;
- un prezzo sotto il costo base va fermato PRIMA (la campagna viene rifiutata);
- una campagna senza id non deve essere registrata come creata;
- il test mode deve essere attivo quando non lo si disattiva esplicitamente.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import hoplix

ROOT = Path(__file__).resolve().parent


class FakeTransport:
    """Risponde come l'API Hoplix, registrando corpi e header."""

    def __init__(self, *, status: int = 200, inner_status: int = 200, message: str = "", payload=None):
        self.calls: list[tuple[str, str, dict]] = []
        self.status = status
        self.inner_status = inner_status
        self.message = message
        self.payload = payload
        self.headers: list[dict] = []

    def __call__(self, method, url, body, headers):
        self.calls.append((method, url, dict(body or {})))
        self.headers.append(dict(headers))
        if self.payload is not None:
            return self.status, self.payload
        path = url.split("/v1", 1)[-1]
        if path == "/product-list":
            return self.status, {
                "status": self.inner_status,
                "message": self.message,
                "product-list": [
                    {
                        "product-code": "maglietta-unisex",
                        "name": "Maglietta unisex",
                        "colors": "white,black,navy",
                        "sizes": "S,M,L,XL,XXL",
                        "base-cost": "7.50",
                        "printable-area": "320x400",
                        "production days": "2",
                    }
                ],
            }
        if path.startswith("/product-info/"):
            return self.status, {
                "status": self.inner_status,
                "message": self.message,
                "product-info": {
                    "product-code": "maglietta-unisex",
                    "name": "Maglietta unisex",
                    "colors": "white,black",
                    "sizes": "S,M,L,XL,XXL",
                    "base-cost": "7.50",
                    "base-cost-back": "9.50",
                    "printable-area": "320x400",
                },
                "additional-product": [{"product-code": "felpa", "base-cost": "15.00"}],
            }
        if path == "/create-campaign":
            return self.status, {
                "status": self.inner_status,
                "message": self.message,
                "campaign_id": "" if self.inner_status != 200 else "camp-1",
                "link": "https://hoplix.shop/camp-1",
            }
        if path == "/create-order":
            return self.status, {"status": self.inner_status, "message": self.message, "order-id": "ord-1", "total": "12.85"}
        if path == "/list-orders":
            return self.status, {
                "status": self.inner_status,
                "order-list": [
                    {"order-id": "o1", "order-status": "ok", "shipping-code": "TRACK1", "tracking-url": "https://t/1"},
                    {"order-id": "o2", "order-status": "pro", "shipping-code": "not available"},
                ],
            }
        if path.startswith("/status-order/"):
            return self.status, {"status": 200, "order-status": "sp", "shipping-code": "TRACK9", "test": False}
        return 404, {"error": f"unexpected {path}"}


def client(**kwargs) -> hoplix.HoplixClient:
    kwargs.setdefault("api_key", "fake-key")
    kwargs.setdefault("secret", "fake-secret")
    kwargs.setdefault("transport", FakeTransport())
    return hoplix.HoplixClient(**kwargs)


PRODUCT = hoplix.Product(
    code="maglietta-unisex",
    name="Maglietta unisex",
    colors=("white", "black"),
    sizes=("S", "M", "L", "XL", "XXL"),
    base_cost=7.5,
    printable_area="320x400",
)


class TestParsing:
    def test_prezzi_da_stringa(self):
        assert hoplix._num("7.50") == 7.5
        assert hoplix._num("7,50") == 7.5
        assert hoplix._num("") is None
        assert hoplix._num(None) is None
        assert hoplix._num("abc") is None
        assert hoplix._num(0) is None

    def test_colori_e_taglie(self):
        assert hoplix._codes("white,black") == ("white", "black")
        assert hoplix._codes("nd") == ()
        assert hoplix._codes(None) == ()

    def test_parse_product(self):
        p = hoplix.parse_product(
            {"product-code": "m", "base-cost": "7.50", "colors": "white,black", "printable-area": "320x400"}
        )
        assert p.base_cost == 7.5
        assert p.area_mm == (320, 400)

    def test_area_illeggibile(self):
        assert hoplix.Product(code="x", name="x", printable_area="boh").area_mm is None

    def test_parse_products_ignora_righe_rotte(self):
        payload = {"product-list": [{"product-code": "a"}, "rotto", {"name": "senza codice"}]}
        assert list(hoplix.parse_products(payload)) == ["a"]


class TestValidazioni:
    def test_colore_inesistente_rifiutato(self):
        with pytest.raises(hoplix.HoplixError) as exc:
            hoplix.map_colors(["purple"], PRODUCT)
        assert exc.value.code == "invalid_color"

    def test_colore_valido_normalizzato(self):
        assert hoplix.map_colors(["White"], PRODUCT) == ["white"]

    def test_taglia_2xl_diventa_xxl(self):
        assert hoplix.map_sizes(["S", "2XL"], PRODUCT) == ["S", "XXL"]

    def test_taglia_inesistente(self):
        with pytest.raises(hoplix.HoplixError) as exc:
            hoplix.map_sizes(["8XL"], PRODUCT)
        assert exc.value.code == "invalid_size"

    def test_senza_colori_o_taglie(self):
        with pytest.raises(hoplix.HoplixError):
            hoplix.map_colors([], PRODUCT)
        with pytest.raises(hoplix.HoplixError):
            hoplix.map_sizes([], PRODUCT)

    def test_prezzo_sotto_il_costo(self):
        with pytest.raises(hoplix.HoplixError) as exc:
            hoplix.validate_price(7.0, 7.5)
        assert exc.value.code == "price_below_base_cost"
        assert "7.50" in str(exc.value)

    def test_prezzo_uguale_al_costo_rifiutato(self):
        with pytest.raises(hoplix.HoplixError):
            hoplix.validate_price(7.5, 7.5)

    def test_prezzo_valido(self):
        assert hoplix.validate_price(24.99, 7.5) == 24.99

    def test_url_mancante_o_non_http(self):
        for bad in ("", "/tmp/x.png", "file:///x.png"):
            with pytest.raises(hoplix.HoplixError):
                hoplix.validate_design_url(bad)

    def test_estensione_non_ammessa(self):
        with pytest.raises(hoplix.HoplixError) as exc:
            hoplix.validate_design_url("https://x/y.pdf")
        assert exc.value.code == "design_extension"

    def test_url_valido(self):
        assert hoplix.validate_design_url("https://x/y.png") == "https://x/y.png"

    def test_slug(self):
        assert hoplix.validate_slug("tech-tee-404-light") == "tech-tee-404-light"
        # Le maiuscole sono NORMALIZZATE (uno slug e' case-insensitive per
        # costruzione): cio' che deve fermare il flusso sono i separatori non
        # ammessi e le forme che Hoplix rifiuta, non la tastiera dell'operatore.
        assert hoplix.validate_slug("Tech-Tee") == "tech-tee"
        for bad in ("", "con spazio", "con_underscore", "-inizio", "a"):
            with pytest.raises(hoplix.HoplixError):
                hoplix.validate_slug(bad)

    def test_area_di_stampa_file_stretto_avvisa_senza_tagliare(self):
        out = hoplix.print_area_check(3600, 4800, PRODUCT)
        assert out["fits"] is True
        assert "margine laterale" in (out["warning"] or "")

    def test_area_di_stampa_file_largo_avvisa_del_taglio(self):
        out = hoplix.print_area_check(5000, 4000, PRODUCT)
        assert out["fits"] is False
        assert "tagliato" in (out["warning"] or "")

    def test_area_sconosciuta_non_inventa(self):
        out = hoplix.print_area_check(3600, 4800, hoplix.Product(code="x", name="x"))
        assert out["fits"] is None and out["warning"] is None


class TestPayloadCampagna:
    def base(self, **over):
        kwargs = dict(
            name="404 REGALO NON TROVATO",
            description="Maglietta unisex con stampa originale.",
            slug="tech-tee-404-light",
            front_url="https://sito.it/shop/tee.png",
            product=PRODUCT,
            price=24.99,
            colors=("white",),
            sizes=("S", "M", "2XL"),
        )
        kwargs.update(over)
        return hoplix.build_campaign_payload(**kwargs)

    def test_struttura(self):
        camp = self.base()["campaign"]
        assert camp["product-code"] == "maglietta-unisex"
        assert camp["product-color"] == "white"
        assert camp["product-price"] == "24.99"  # stringa: Hoplix la vuole cosi'
        assert camp["currency"] == "EUR"
        assert camp["campaign-duration"] == "0"
        assert camp["private"] == "n"
        assert "back" not in camp

    def test_descrizione_e_nome_obbligatori(self):
        with pytest.raises(hoplix.HoplixError):
            self.base(name="")
        with pytest.raises(hoplix.HoplixError):
            self.base(description="")

    def test_prezzo_sotto_costo_blocca(self):
        with pytest.raises(hoplix.HoplixError):
            self.base(price=5.0)

    def test_retro_e_oneshot(self):
        camp = self.base(back_url="https://sito.it/shop/back.png", oneshot=True)["campaign"]
        assert camp["back"].endswith("back.png")
        assert camp["oneshot"] == "on"

    def test_nome_troncato_a_80(self):
        camp = self.base(name="x" * 200)["campaign"]
        assert len(camp["name"]) <= 80


class TestPayloadOrdine:
    def shipping(self, **over):
        base = {
            "name": "Mario",
            "surname": "Rossi",
            "address": "Via Roma 1",
            "zip-code": "00100",
            "city": "Roma",
            "province": "RM",
            "country-code": "IT",
        }
        base.update(over)
        return base

    def item(self, **over):
        base = {"campaign-id": "c1", "product-id": "p1", "product-color": "white", "product-size": "M", "quantity": 1}
        base.update(over)
        return base

    def test_test_mode_di_default(self):
        payload = hoplix.build_order_payload(reference="r1", items=[self.item()], shipping=self.shipping())
        assert payload["test"] is True

    def test_test_mode_disattivabile_solo_esplicitamente(self):
        payload = hoplix.build_order_payload(
            reference="r1", items=[self.item()], shipping=self.shipping(), test=False
        )
        assert "test" not in payload

    def test_dati_mancanti(self):
        with pytest.raises(hoplix.HoplixError) as exc:
            hoplix.build_order_payload(reference="r1", items=[self.item()], shipping=self.shipping(city=""))
        assert "city" in str(exc.value)

    def test_paese_non_iso(self):
        with pytest.raises(hoplix.HoplixError):
            hoplix.build_order_payload(reference="r1", items=[self.item()], shipping=self.shipping(**{"country-code": "Italia"}))

    def test_quantita_non_valida(self):
        for qty in (0, -1, "due"):
            with pytest.raises(hoplix.HoplixError):
                hoplix.build_order_payload(reference="r1", items=[self.item(quantity=qty)], shipping=self.shipping())

    def test_voce_incompleta(self):
        with pytest.raises(hoplix.HoplixError):
            hoplix.build_order_payload(reference="r1", items=[self.item(**{"product-size": ""})], shipping=self.shipping())

    def test_ordine_vuoto_e_riferimento_mancante(self):
        with pytest.raises(hoplix.HoplixError):
            hoplix.build_order_payload(reference="r1", items=[], shipping=self.shipping())
        with pytest.raises(hoplix.HoplixError):
            hoplix.build_order_payload(reference="", items=[self.item()], shipping=self.shipping())

    def test_spedizione_non_ammessa(self):
        with pytest.raises(hoplix.HoplixError):
            hoplix.build_order_payload(
                reference="r1", items=[self.item()], shipping=self.shipping(), shipping_mode="teletrasporto"
            )


class TestClient:
    def test_senza_credenziali_non_parte(self):
        with pytest.raises(hoplix.HoplixError) as exc:
            hoplix.HoplixClient(api_key="", secret="x")
        assert exc.value.code == "credentials_missing"

    def test_chiavi_e_segreto_nel_corpo(self):
        t = FakeTransport()
        hoplix.HoplixClient(api_key="k", secret="s", transport=t).product_list()
        assert t.calls[0][2]["api"] == "k"
        assert t.calls[0][2]["secret"] == "s"

    def test_test_mode_aggiunto(self):
        """Il flag va sugli ORDINI (dove Hoplix lo documenta), non sulle letture."""
        t = FakeTransport()
        client = hoplix.HoplixClient(api_key="k", secret="s", transport=t, test_mode=True)
        client.create_order({"order": {"reference": "r1", "items": []}})
        assert t.calls[-1][2]["test"] is True

    def test_test_mode_spento_non_aggiunge(self):
        t = FakeTransport()
        client = hoplix.HoplixClient(api_key="k", secret="s", transport=t, test_mode=False)
        client.create_order({"order": {"reference": "r1", "items": []}})
        assert "test" not in t.calls[-1][2]

    def test_errore_con_http_200_viene_letto(self):
        """Il caso insidioso: successo HTTP, errore nel corpo."""
        t = FakeTransport(inner_status=201, message="Invalid API key")
        with pytest.raises(hoplix.HoplixError) as exc:
            hoplix.HoplixClient(api_key="k", secret="s", transport=t).product_list()
        assert "Invalid API key" in str(exc.value)

    def test_http_500(self):
        t = FakeTransport(status=500, payload={"error": "boom"})
        with pytest.raises(hoplix.HoplixError):
            hoplix.HoplixClient(api_key="k", secret="s", transport=t).product_list()

    def test_catalogo(self):
        products = client().product_list()
        assert products["maglietta-unisex"].base_cost == 7.5

    def test_product_senza_costo_base(self):
        t = FakeTransport(payload={"status": 200, "product-info": {"product-code": "x", "name": "x"}})
        with pytest.raises(hoplix.HoplixError) as exc:
            hoplix.HoplixClient(api_key="k", secret="s", transport=t).product()
        assert exc.value.code == "no_base_cost"

    def test_campagna_senza_id_non_registrata(self):
        t = FakeTransport(inner_status=201, message="price too low")
        with pytest.raises(hoplix.HoplixError):
            client(transport=t).create_campaign({"campaign": {"name": "x"}})

    def test_campagna_ok(self):
        data = client().create_campaign({"campaign": {"name": "x"}})
        assert data["campaign_id"] == "camp-1"

    def test_payload_campagna_vuoto(self):
        with pytest.raises(hoplix.HoplixError):
            client().create_campaign({"campaign": {}})

    def test_ordine_senza_id(self):
        t = FakeTransport(payload={"status": 200})
        with pytest.raises(hoplix.HoplixError):
            client(transport=t).create_order({"order": {"x": 1}})

    def test_status_order(self):
        out = client().status_order("o1")
        assert out["status"] == "sp" and out["tracking"] == "TRACK9"


class TestOrdini:
    def test_riepilogo(self):
        report = hoplix.orders_report(
            [
                {"order-id": "o1", "order-status": "ok", "shipping-code": "T1", "tracking-url": "https://t"},
                {"order-id": "o2", "order-status": "pro", "shipping-code": "not available"},
                {"order-id": "o3", "order-status": "ok"},
            ]
        )
        assert report["total"] == 3
        assert report["by_status"] == {"ok": 2, "pro": 1}
        assert report["with_tracking"] == 1

    def test_vuoto(self):
        assert "nessun ordine" in hoplix.format_orders(hoplix.orders_report([]))

    def test_formato_con_tracking(self):
        text = hoplix.format_orders(hoplix.orders_report([{"order-id": "o1", "order-status": "ok", "shipping-code": "T1"}]))
        assert "T1" in text and "ok: 1" in text


class TestTripwire:
    def test_import_non_carica_rete(self):
        code = (
            "import sys, hoplix;"
            "assert 'requests' not in sys.modules, 'import ha caricato requests';"
            "assert 'tracker' not in sys.modules"
        )
        subprocess.run([sys.executable, "-c", code], cwd=ROOT, check=True)

    def test_nessuna_scrittura_su_disco(self):
        source = (ROOT / "hoplix.py").read_text(encoding="utf-8")
        for token in ("open(", "write_text", "save_bet", "tracker."):
            assert token not in source

    def test_cli_senza_chiavi_esce_con_2(self):
        out = subprocess.run(
            [sys.executable, str(ROOT / "hoplix.py"), "--catalog"],
            capture_output=True,
            text=True,
            cwd=ROOT,
            env={"PATH": "/usr/bin:/bin", "HOME": "/tmp"},
            check=False,
        )
        assert out.returncode == 2
        assert "mancanti" in out.stdout
