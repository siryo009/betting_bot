"""Client API Hoplix v1.1 — ordini, campagne e catalogo, con TEST MODE.

PERCHE' HOPLIX (e non Printify Pop-Up) — vedi anche platforms.py
-----------------------------------------------------------------
Tre fatti verificati, non preferenze:
1. costa meno: t-shirt unisex 7,90 EUR IVA inclusa contro ~10,50 di un
   fornitore EU Printify (stima);
2. incassa e fattura Hoplix: nessuna verifica Stripe, nessun incasso in casa
   nostra, IVA/OSS non a nostro carico (per un forfettario e' la differenza fra
   "vendo" e "devo aprire una contabilita'");
3. l'API ha un TEST MODE vero: `"test": true` attraversa tutti i controlli,
   calcola totale e spedizione, e NON produce, non addebita, non spedisce.

IL LIMITE CHE VA DETTO SUBITO
-----------------------------
L'API sa creare **campagne** (pagine di vendita) e **ordini**, NON prodotti
dello store: i prodotti dello store si inseriscono dall'interfaccia (cinque
voci, pochi minuti). Chi promette "pubblico lo store via API" non ha letto la
documentazione. `catalog.py` genera la lista esatta da inserire, cosi' il
lavoro manuale e' copia-incolla e non una ricostruzione.

DUE COSE CHE QUESTA API FA IN MODO INSIDIOSO (e qui sono gestite)
----------------------------------------------------------------
1. **Gli errori tornano con HTTP 200.** Il corpo e' `{"status": 201,
   "message": "..."}`: fidarsi del codice HTTP farebbe passare per riuscita
   un'operazione fallita. `_post` controlla il `status` NEL CORPO.
2. **Il prezzo di vendita deve essere maggiore del costo base**, altrimenti
   la campagna viene rifiutata. Qui il controllo si fa PRIMA della chiamata,
   con un messaggio che dice quanto vale il costo.

Il trasporto HTTP e' iniettabile: i test girano interamente offline.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

BASE_URL = "https://api.hoplix.com/v1"
USER_AGENT = "quotaverace-pod-shop/1.0"

# Hoplix restituisce HTTP 200 anche sugli errori: il codice sta nel corpo.
ERROR_STATUS = 201

# Il prodotto che vendiamo. Codice ufficiale del catalogo (`maglietta-unisex`).
DEFAULT_PRODUCT_CODE = "maglietta-unisex"

# Colori del catalogo Hoplix (codici minuscoli, senza spazi) mappati sui nomi
# che usiamo nei listing. Solo colori che ESISTONO per la maglietta unisex: un
# colore inventato fa rifiutare la campagna da Hoplix.
COLOR_CODES: Mapping[str, str] = {
    "white": "white",
    "black": "black",
    "navy": "navy",
    "heathergrey": "heathergrey",
    "darkgrey": "darkgrey",
    "red": "red",
    "bottlegreen": "bottlegreen",
}

# Colori ammessi per ogni inchiostro: una campagna ha UNA immagine fronte,
# quindi i colori devono essere compatibili con quell'inchiostro (una stampa
# nera su capo nero non si vede, e il reso lo paghiamo noi).
INK_COLORS: Mapping[str, tuple[str, ...]] = {
    "light": ("white",),
    "dark": ("black",),
}

# Le taglie del catalogo usano XXL, i nostri listing 2XL.
SIZE_CODES: Mapping[str, str] = {
    "S": "S",
    "M": "M",
    "L": "L",
    "XL": "XL",
    "2XL": "XXL",
    "XXL": "XXL",
    "3XL": "3XL",
}

SHIPPING_MODES = ("economy", "tracked", "express", "fob")
PAYMENT_METHODS = ("credit-card", "wallet")

# Estensioni accettate per i file di stampa (dalla documentazione: "the file
# extension is not allowed" e' un errore reale).
ALLOWED_IMAGE_EXT = (".png", ".jpg", ".jpeg")

# Hoplix rifiuta gli url con caratteri speciali: lo slug si valida PRIMA.
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,58}[a-z0-9]$")
COUNTRY_RE = re.compile(r"^[A-Z]{2}$")


class HoplixError(RuntimeError):
    """Errore dell'API o di validazione: il chiamante lo mostra e si ferma."""

    def __init__(self, message: str, *, code: str = "error", detail: Any = None):
        super().__init__(message)
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------------------
# trasporto (iniettabile)
# ---------------------------------------------------------------------------

Transport = Callable[[str, str, Any, Mapping[str, str]], tuple[int, Any]]


def _requests_transport(method: str, url: str, body: Any, headers: Mapping[str, str]) -> tuple[int, Any]:
    import requests  # import pigro: importare il modulo non carica la rete

    resp = requests.request(method, url, json=body, headers=dict(headers), timeout=60)
    try:
        payload = resp.json()
    except ValueError:
        payload = {"_raw": resp.text[:500]}
    return resp.status_code, payload


# ---------------------------------------------------------------------------
# catalogo
# ---------------------------------------------------------------------------


def _num(value: Any) -> float | None:
    """Hoplix espone i prezzi come STRINGHE ('7.50'): qui diventano numeri."""
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None
    return out if out > 0 else None


def _codes(raw: Any) -> tuple[str, ...]:
    if not raw or str(raw).strip().lower() == "nd":
        return ()
    return tuple(c.strip() for c in str(raw).split(",") if c.strip())


@dataclass(frozen=True)
class Product:
    """Una voce di catalogo Hoplix (base cost, colori, taglie, area di stampa)."""

    code: str
    name: str
    colors: tuple[str, ...] = ()
    sizes: tuple[str, ...] = ()
    base_cost: float | None = None
    base_cost_back: float | None = None
    production_days: int | None = None
    printable_area: str = ""
    weight_g: float | None = None

    @property
    def area_mm(self) -> tuple[int, int] | None:
        """'320x400' -> (320, 400). None se il campo non e' leggibile."""
        m = re.match(r"^\s*(\d{2,4})\s*[xX]\s*(\d{2,4})\s*$", self.printable_area or "")
        if not m:
            return None
        return int(m.group(1)), int(m.group(2))


def parse_product(row: Mapping[str, Any]) -> Product:
    return Product(
        code=str(row.get("product-code") or "").strip(),
        name=str(row.get("name") or "").strip(),
        colors=_codes(row.get("colors")),
        sizes=_codes(row.get("sizes")),
        base_cost=_num(row.get("base-cost")),
        base_cost_back=_num(row.get("base-cost-back")),
        production_days=int(_num(row.get("production days")) or 0) or None,
        printable_area=str(row.get("printable-area") or "").strip(),
        weight_g=_num(row.get("weight")),
    )


def parse_products(payload: Mapping[str, Any]) -> dict[str, Product]:
    rows = payload.get("product-list") if isinstance(payload, dict) else None
    out: dict[str, Product] = {}
    for row in rows or []:
        if not isinstance(row, Mapping):
            continue
        product = parse_product(row)
        if product.code:
            out[product.code] = product
    return out


# ---------------------------------------------------------------------------
# validazioni pure (prima di qualunque chiamata)
# ---------------------------------------------------------------------------


def map_colors(colors: Iterable[str], product: Product) -> list[str]:
    """Traduce i nostri colori nei codici Hoplix; rifiuta quelli inesistenti."""
    out: list[str] = []
    for raw in colors:
        key = str(raw).strip().lower().replace(" ", "")
        code = COLOR_CODES.get(key, key)
        if product.colors and code not in product.colors:
            raise HoplixError(
                f"colore '{raw}' non disponibile per {product.code} "
                f"(disponibili: {', '.join(product.colors)})",
                code="invalid_color",
            )
        if code not in out:
            out.append(code)
    if not out:
        raise HoplixError("nessun colore valido", code="no_colors")
    return out


def map_sizes(sizes: Iterable[str], product: Product) -> list[str]:
    out: list[str] = []
    for raw in sizes:
        key = str(raw).strip().upper()
        code = SIZE_CODES.get(key, key)
        if product.sizes and code not in product.sizes:
            raise HoplixError(
                f"taglia '{raw}' non disponibile per {product.code} "
                f"(disponibili: {', '.join(product.sizes)})",
                code="invalid_size",
            )
        if code not in out:
            out.append(code)
    if not out:
        raise HoplixError("nessuna taglia valida", code="no_sizes")
    return out


def validate_price(price: float, base_cost: float | None) -> float:
    """Il prezzo deve superare il costo base: Hoplix rifiuta la campagna.

    Il controllo si fa qui perche' l'errore di Hoplix ("The price of the product
    ... must be greater than ...") arriva DOPO la chiamata: meglio dirlo con il
    numero sotto gli occhi.
    """
    if not isinstance(price, (int, float)) or price <= 0:
        raise HoplixError("prezzo non valido", code="invalid_price")
    if base_cost is not None and float(price) <= float(base_cost):
        raise HoplixError(
            f"prezzo {float(price):.2f} <= costo base {float(base_cost):.2f}: "
            "Hoplix rifiuterebbe la campagna",
            code="price_below_base_cost",
        )
    return round(float(price), 2)


def validate_design_url(url: str, *, what: str = "front") -> str:
    url = (url or "").strip()
    if not url:
        raise HoplixError(f"immagine '{what}' mancante: Hoplix richiede un URL pubblico", code="design_missing")
    if not re.match(r"^https?://", url, re.I):
        raise HoplixError(
            f"immagine '{what}' non e' un URL http(s): Hoplix scarica il file da remoto "
            "e non legge il disco locale",
            code="design_not_url",
        )
    path = url.split("?")[0].lower()
    if not path.endswith(ALLOWED_IMAGE_EXT):
        raise HoplixError(
            f"estensione di '{what}' non ammessa ({path.rsplit('.', 1)[-1][:8]}): "
            f"servono {', '.join(ALLOWED_IMAGE_EXT)}",
            code="design_extension",
        )
    return url


def validate_slug(slug: str) -> str:
    slug = (slug or "").strip().lower()
    if not SLUG_RE.match(slug):
        raise HoplixError(
            f"slug '{slug}' non valido: minuscole, numeri e trattini (Hoplix rifiuta i caratteri speciali)",
            code="invalid_slug",
        )
    return slug


def print_area_check(width_px: int, height_px: int, product: Product) -> dict[str, Any]:
    """Confronta le proporzioni del file con l'area di stampa.

    Non e' un dettaglio estetico: un file con proporzioni diverse dall'area
    viene adattato, e un disegno che arriva a filo bordo puo' perdere una parte.
    Il risultato dichiara il rapporto e di quanto il file va ridotto.
    """
    area = product.area_mm
    out: dict[str, Any] = {
        "file_px": [width_px, height_px],
        "file_ratio": round(width_px / height_px, 4) if height_px else None,
        "printable_area": product.printable_area,
        "area_ratio": None,
        "fits": None,
        "warning": None,
    }
    if not area or not height_px:
        return out
    aw, ah = area
    out["area_ratio"] = round(aw / ah, 4)
    file_ratio = width_px / height_px
    area_ratio = aw / ah
    # "fits" = il file riempie l'area senza tagli (stesso rapporto o piu' stretto)
    out["fits"] = file_ratio <= area_ratio + 1e-6
    if not out["fits"]:
        cut = round((1.0 - area_ratio / file_ratio) * 100.0, 1)
        out["warning"] = f"file piu' largo dell'area: ~{cut}% della larghezza andrebbe tagliato o ridotto"
    elif abs(file_ratio - area_ratio) > 0.02:
        margin = round((1.0 - file_ratio / area_ratio) * 100.0, 1)
        out["warning"] = f"file piu' stretto dell'area: ~{margin}% di margine laterale (nessun taglio)"
    return out


# ---------------------------------------------------------------------------
# payload
# ---------------------------------------------------------------------------


def build_campaign_payload(
    *,
    name: str,
    description: str,
    slug: str,
    front_url: str,
    product: Product,
    price: float,
    colors: Sequence[str],
    sizes: Sequence[str] | None = None,
    currency: str = "EUR",
    back_url: str = "",
    oneshot: bool = False,
    duration: str = "0",
    private: bool = False,
) -> dict[str, Any]:
    """Corpo esatto di `create-campaign`, validato prima di partire.

    `sizes` non e' un campo della campagna (Hoplix le mostra tutte quelle del
    prodotto): viene accettato per validare le taglie contro il catalogo e
    documentare cosa ci si aspetta di vedere in vendita.
    """
    if not (name or "").strip():
        raise HoplixError("nome campagna mancante", code="name_missing")
    if not (description or "").strip():
        raise HoplixError("descrizione mancante", code="description_missing")
    slug = validate_slug(slug)
    front_url = validate_design_url(front_url, what="front")
    back = validate_design_url(back_url, what="back") if (back_url or "").strip() else ""
    colors = map_colors(colors, product)
    if sizes:
        map_sizes(sizes, product)
    price = validate_price(price, product.base_cost)

    campaign: dict[str, Any] = {
        "name": name.strip()[:80],
        "description": description.strip(),
        "url": slug,
        "private": "y" if private else "n",
        "campaign-duration": duration or "0",
        "front": front_url,
        "currency": (currency or "EUR").upper(),
        "product-code": product.code,
        "product-color": ",".join(colors),
        "product-price": f"{price:.2f}",
    }
    if back:
        campaign["back"] = back
    if oneshot:
        # 'oneshot': la campagna si cancella da sola dopo 90 giorni
        campaign["oneshot"] = "on"
    return {"campaign": campaign}


def build_order_payload(
    *,
    reference: str,
    items: Sequence[Mapping[str, Any]],
    shipping: Mapping[str, Any],
    shipping_mode: str = "economy",
    payment_method: str = "credit-card",
    client_email: str = "",
    test: bool = True,
) -> dict[str, Any]:
    """Corpo di `create-order`, validato (il test mode e' il default).

    Ogni voce richiede `campaign-id`, `product-id`, colore, taglia, quantita':
    sono gli id che Hoplix restituisce in `info-campaign`, non i nostri.
    """
    if not (reference or "").strip():
        raise HoplixError("order_reference mancante", code="reference_missing")
    if not items:
        raise HoplixError("ordine senza prodotti", code="no_items")
    if shipping_mode not in SHIPPING_MODES:
        raise HoplixError(f"spedizione '{shipping_mode}' non ammessa ({', '.join(SHIPPING_MODES)})", code="invalid_shipping")
    if payment_method not in PAYMENT_METHODS:
        raise HoplixError(f"pagamento '{payment_method}' non ammesso ({', '.join(PAYMENT_METHODS)})", code="invalid_payment")

    required = ("name", "surname", "address", "zip-code", "city", "province", "country-code")
    missing = [k for k in required if not str(shipping.get(k) or "").strip()]
    if missing:
        raise HoplixError(f"dati di spedizione incompleti: {', '.join(missing)}", code="shipping_incomplete")
    country = str(shipping.get("country-code") or "").strip().upper()
    if not COUNTRY_RE.match(country):
        raise HoplixError(f"country-code '{country}' non e' ISO 3166-1 alpha-2", code="invalid_country")

    out_items = []
    for it in items:
        missing = [k for k in ("campaign-id", "product-id", "product-color", "product-size") if not str(it.get(k) or "").strip()]
        if missing:
            raise HoplixError(f"voce ordine incompleta: {', '.join(missing)}", code="item_incomplete")
        qty = it.get("quantity", 1)
        try:
            qty = int(qty)
        except (TypeError, ValueError):
            raise HoplixError("quantita' non numerica", code="invalid_quantity") from None
        if qty < 1:
            raise HoplixError("quantita' minima 1", code="invalid_quantity")
        out_items.append(
            {
                "campaign-id": str(it["campaign-id"]),
                "product-id": str(it["product-id"]),
                "product-color": str(it["product-color"]),
                "product-size": str(it["product-size"]),
                "quantity": qty,
            }
        )

    order: dict[str, Any] = {
        "products": out_items,
        "shipping-mode": shipping_mode,
        "order_reference": str(reference),
        "shipping-info": {
            "name": str(shipping.get("name")),
            "surname": str(shipping.get("surname")),
            "address": str(shipping.get("address")),
            "address-more": str(shipping.get("address-more") or ""),
            "zip-code": str(shipping.get("zip-code")),
            "city": str(shipping.get("city")),
            "province": str(shipping.get("province")),
            "country-code": country,
        },
    }
    if payment_method:
        order["payment-method"] = payment_method
    if client_email:
        order["client-email"] = client_email
    payload: dict[str, Any] = {"order": order}
    if test:
        payload["test"] = True
    return payload


# ---------------------------------------------------------------------------
# client
# ---------------------------------------------------------------------------


@dataclass
class HoplixClient:
    """Client minimo: solo gli endpoint che servono, trasporto iniettabile."""

    api_key: str
    secret: str
    base_url: str = BASE_URL
    test_mode: bool = False
    transport: Transport | None = None
    calls: list[tuple[str, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not (self.api_key or "").strip() or not (self.secret or "").strip():
            raise HoplixError(
                "HOPLIX_API_KEY / HOPLIX_SECRET mancanti: senza chiavi nessuna chiamata (fail-closed).",
                code="credentials_missing",
            )
        self._transport = self.transport or _requests_transport

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, **kwargs: Any) -> "HoplixClient":
        src = dict(os.environ if env is None else env)
        test_raw = str(src.get("HOPLIX_TEST_MODE", "1")).strip().lower()
        settings: dict[str, Any] = {
            "api_key": (src.get("HOPLIX_API_KEY") or "").strip(),
            "secret": (src.get("HOPLIX_SECRET") or "").strip(),
            "base_url": (src.get("HOPLIX_API_BASE") or BASE_URL).rstrip("/"),
            "test_mode": test_raw not in ("0", "false", "no", "off"),
        }
        # Chi chiama puo' SOVRASCRIVERE cio' che arriva dall'ambiente (per esempio
        # dai test o da un `--live` esplicito). Prima i kwargs finivano accanto ai
        # valori calcolati e uno stesso campo passato due volte sollevava
        # `TypeError: got multiple values for keyword argument`, cioe' un errore
        # di programmazione al posto di una precedenza dichiarata.
        settings.update(kwargs)
        return cls(**settings)

    # -- internals ---------------------------------------------------------
    def _post(self, path: str, extra: Mapping[str, Any] | None = None) -> Any:
        url = f"{self.base_url}{path}"
        body: dict[str, Any] = {"api": self.api_key, "secret": self.secret}
        if extra:
            body.update(dict(extra))
        headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
        self.calls.append(("POST", path))
        status, payload = self._transport("POST", url, body, headers)
        # Hoplix usa HTTP 200 anche per gli errori: si guarda il corpo.
        inner = payload.get("status") if isinstance(payload, Mapping) else None
        message = payload.get("message") if isinstance(payload, Mapping) else None
        if status >= 400 or inner == ERROR_STATUS or (message and inner != 200):
            raise HoplixError(
                f"Hoplix POST {path} -> {message or f'HTTP {status}'}",
                code="api_error",
                detail=payload,
            )
        return payload

    def _extra(self, extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
        body = dict(extra or {})
        if self.test_mode:
            body.setdefault("test", True)
        return body

    # -- catalogo ----------------------------------------------------------
    def product_list(self) -> dict[str, Product]:
        return parse_products(self._post("/product-list"))

    def product_info(self, code: str) -> tuple[Product, list[Product]]:
        payload = self._post(f"/product-info/{(code or '').strip()}")
        info = payload.get("product-info") if isinstance(payload, Mapping) else None
        if not isinstance(info, Mapping):
            raise HoplixError(f"product-info '{code}' senza dati", code="product_missing", detail=payload)
        related = [parse_product(r) for r in (payload.get("additional-product") or []) if isinstance(r, Mapping)]
        return parse_product(info), related

    def product(self, code: str = DEFAULT_PRODUCT_CODE) -> Product:
        product, _ = self.product_info(code)
        if product.base_cost is None:
            raise HoplixError(
                f"{code}: costo base assente dal catalogo, impossibile verificare il prezzo",
                code="no_base_cost",
            )
        return product

    # -- campagne ----------------------------------------------------------
    def create_campaign(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        extra = {k: v for k, v in payload.items() if k != "campaign"}
        campaign = dict(payload.get("campaign") or {})
        if not campaign:
            raise HoplixError("payload campagna vuoto", code="empty_campaign")
        data = self._post("/create-campaign", {**extra, **self._extra(campaign)})
        campaign_id = (data or {}).get("campaign_id")
        if not campaign_id:
            raise HoplixError("campagna creata senza id: non la registro", code="campaign_without_id", detail=data)
        return data

    def list_campaigns(self) -> list[dict[str, Any]]:
        data = self._post("/list-campaign")
        rows = data if isinstance(data, list) else (data or {}).get("list")
        return [r for r in (rows or []) if isinstance(r, Mapping)]

    def campaign_info(self, campaign_id: str) -> dict[str, Any]:
        data = self._post(f"/info-campaign/{(campaign_id or '').strip()}")
        info = (data or {}).get("campaign")
        if not isinstance(info, Mapping):
            raise HoplixError("campagna non trovata", code="campaign_missing", detail=data)
        return dict(info)

    # -- ordini ------------------------------------------------------------
    def create_order(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        body = {k: v for k, v in payload.items() if k != "order"}
        order = dict(payload.get("order") or {})
        if not order:
            raise HoplixError("payload ordine vuoto", code="empty_order")
        extra = {**body, **order}
        if self.test_mode:
            extra.setdefault("test", True)
        data = self._post("/create-order", extra)
        if not (data or {}).get("order-id"):
            raise HoplixError("ordine senza id nella risposta", code="order_without_id", detail=data)
        return data

    def create_campaign_order(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Le v2: crea il prodotto (campagna) E l'ordine in una chiamata."""
        body = {k: v for k, v in payload.items() if k != "order"}
        order = dict(payload.get("order") or {})
        if not order:
            raise HoplixError("payload ordine vuoto", code="empty_order")
        extra = {**body, **order}
        if self.test_mode:
            extra.setdefault("test", True)
        data = self._post("/create-campaign-order", extra)
        if not (data or {}).get("order-id"):
            raise HoplixError("ordine senza id nella risposta", code="order_without_id", detail=data)
        return data

    def status_order(self, order_id: str) -> dict[str, Any]:
        data = self._post(f"/status-order/{(order_id or '').strip()}")
        return {
            "order_id": order_id,
            "status": (data or {}).get("order-status"),
            "tracking": (data or {}).get("shipping-code"),
            "tracking_url": (data or {}).get("tracking-url"),
            "test": bool((data or {}).get("test")),
        }

    def status_by_reference(self, reference: str) -> dict[str, Any]:
        data = self._post("/status-order-reference", {"order-ref": (reference or "").strip()})
        return {
            "reference": reference,
            "status": (data or {}).get("order-status"),
            "tracking": (data or {}).get("shipping-code"),
            "tracking_url": (data or {}).get("tracking-url"),
        }

    def order_info(self, order_id: str) -> dict[str, Any]:
        data = self._post(f"/order-info/{(order_id or '').strip()}")
        rows = (data or {}).get("order-info") if isinstance(data, Mapping) else None
        row = rows[0] if isinstance(rows, list) and rows else None
        if not isinstance(row, Mapping):
            raise HoplixError(f"order-info '{order_id}' senza dati", code="order_missing", detail=data)
        return dict(row)

    def list_orders(self) -> list[dict[str, Any]]:
        data = self._post("/list-orders")
        rows = (data or {}).get("order-list")
        return [dict(r) for r in (rows or []) if isinstance(r, Mapping)]

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        return dict(self._post(f"/cancel-order/{(order_id or '').strip()}") or {})


# ---------------------------------------------------------------------------
# report degli ordini (monitoraggio: e' il lavoro che resta dopo la vendita)
# ---------------------------------------------------------------------------


def orders_report(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Riepiloga gli ordini per stato, con i tracker da seguire.

    Hoplix usa stati a due lettere (`pe`, `pro`, `ok`, `co`, ...): qui vengono
    letti come sono, senza traduzioni inventate, e i codici di tracking sono
    raccolti perche' sono l'informazione che serve al cliente.
    """
    by_status: dict[str, int] = {}
    with_tracking = 0
    trackers: list[dict[str, Any]] = []
    for row in rows:
        status = str(row.get("order-status") or "?")
        by_status[status] = by_status.get(status, 0) + 1
        code = str(row.get("shipping-code") or "").strip()
        if code and code.lower() not in ("not available", ""):
            with_tracking += 1
            trackers.append(
                {
                    "order_id": row.get("order-id"),
                    "tracking": code,
                    "url": row.get("tracking-url"),
                }
            )
    return {
        "total": len(rows),
        "by_status": by_status,
        "with_tracking": with_tracking,
        "trackers": trackers,
    }


def format_orders(report: Mapping[str, Any]) -> str:
    if not report.get("total"):
        return "nessun ordine (lo store gratuito non ne ha ancora ricevuti)"
    lines = [f"=== ORDINI HOPLIX — {report['total']} totali, {report['with_tracking']} con tracking ==="]
    for status, n in sorted(report["by_status"].items(), key=lambda kv: -kv[1]):
        lines.append(f"  stato {status}: {n}")
    for t in report["trackers"][:20]:
        lines.append(f"  {t['order_id']}: {t['tracking']} {t.get('url') or ''}".rstrip())
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - CLI
    import argparse

    parser = argparse.ArgumentParser(description="Hoplix: catalogo, ordini e tracking (test mode di default).")
    parser.add_argument("--catalog", action="store_true", help="elenca i prodotti e il costo base")
    parser.add_argument("--orders", action="store_true", help="riepiloga gli ordini con i tracking")
    parser.add_argument("--json", action="store_true", help="output JSON")
    parser.add_argument("--live-orders", action="store_true", help="include gli ordini reali (non solo test)")
    args = parser.parse_args(argv)

    try:
        client = HoplixClient.from_env(test_mode=not args.live_orders)
    except HoplixError as exc:
        print(str(exc))
        return 2

    try:
        if args.catalog:
            products = client.product_list()
            if args.json:
                print(json.dumps({k: v.__dict__ for k, v in products.items()}, indent=2, ensure_ascii=False))
            else:
                for code, p in sorted(products.items()):
                    print(f"{code:32s} {p.name:28s} base {p.base_cost or '?':>6} EUR  taglie {len(p.sizes)}  colori {len(p.colors)}")
            return 0
        report = orders_report(client.list_orders() if args.live_orders else [])
        print(json.dumps(report, indent=2, ensure_ascii=False) if args.json else format_orders(report))
        return 0
    except HoplixError as exc:
        print(f"errore Hoplix: {exc}")
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
