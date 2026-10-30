"""Publisher Etsy tramite Printify — DRY-RUN di default.

PERCHE' PRINTIFY E NON L'API DI ETSY DIRETTAMENTE
-------------------------------------------------
L'API di Etsy (Open API v3) sa creare e pubblicare listing
(`createDraftListing` + `updateListing`), ma richiede un'app registrata e
approvata, un `shop_id`, un `taxonomy_id`, un profilo di spedizione e un
`readiness_state_id` per OGNI listing. Printify, che e' anche il fornitore che
stampa e spedisce, sa fare la stessa cosa con UNA chiamata: crea il prodotto
(titolo, descrizione, tag, aree di stampa) e lo pubblica sul canale Etsy gia'
collegato. Quindi il ciclo completo — immagine -> prodotto -> listing
pubblicato -> ordine -> stampa -> spedizione — e' automatizzabile con un solo
token personale, senza approvazione di un'app.

COSA NON FA QUESTO MODULO, DI PROPOSITO
---------------------------------------
- Non apre negozi, non verifica identita', non collega canali: sono atti del
  proprietario (documento + selfie + IBAN + carta, una volta sola).
- Non salva credenziali su disco: il token si legge SOLO dall'ambiente
  (`PRINTIFY_TOKEN`), come le altre chiavi del progetto.
- Non pubblica niente se un solo listing non passa la validazione: i testi di
  Etsy hanno limiti rigidi (titolo 140 caratteri, 13 tag da massimo 20
  caratteri) e un payload rifiutato a meta' lascerebbe prodotti orfani.

MODALITA'
---------
`--dry-run` (default)  valida testi, file, immagini e costruisce i payload
                       ESATTI che verrebbero inviati. Se c'e' il token legge
                       anche il catalogo vero (blueprint, fornitore, varianti)
                       senza caricare ne' creare nulla.
`--live`               esegue upload immagine -> crea prodotto -> pubblica.
                       Idempotente: il registro `output/published.json` evita
                       di creare due volte lo stesso listing.
`--check`              verifica token, negozio e canale vendite (deve dire Etsy).

Il trasporto HTTP e' iniettabile: i test girano interamente offline.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import json
import math
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

ROOT = Path(__file__).resolve().parent
DEFAULT_LISTINGS = ROOT / "LISTINGS.csv"
DEFAULT_LEDGER = ROOT / "output" / "published.json"
DEFAULT_IP_TERMS = ROOT.parent / "redbubble-tech-gift-2026-work" / "ip_terms.txt"

# --- Regole ETSY (validate PRIMA di spendere un centesimo) -------------------
ETSY_TITLE_MAX = 140
ETSY_TAGS_MAX = 13
ETSY_TAG_LEN_MAX = 20
ETSY_DESC_MIN = 100
ETSY_DESC_MAX = 60000

# --- Regole PRINTIFY --------------------------------------------------------
PRINTIFY_TAGS_MAX = 15
PRINTIFY_API = "https://api.printify.com/v1"
PRINT_PLACEHOLDER = "front"
# Printify: x/y sono relativi all'area di stampa (0..1), scale 1.0 = massima
# dimensione che entra nell'area. 0.85 lascia un margine di sicurezza: le
# magliette hanno cuciture e la stampa a filo bordo si deforma.
DEFAULT_IMAGE_SCALE = 0.85

# --- Prezzi -----------------------------------------------------------------
DEFAULT_PRICE_FLOOR = 24.99
DEFAULT_MARKUP = 2.2
PRICE_ENDING_CENTS = 99

# Colori considerati "scuri": sulla maglietta nera va l'immagine a inchiostro
# chiaro (`image_dark`), non quella per capi chiari.
DARK_COLOR_TOKENS = ("black", "navy", "charcoal", "dark")

# Servizi di stampa in Europa: spedizione piu' corta e nessun dazio per il
# cliente UE. L'ordine della lista e' una preferenza, non un requisito.
EU_PRINT_COUNTRIES = (
    "latvia",
    "germany",
    "netherlands",
    "poland",
    "czech",
    "spain",
    "portugal",
    "italy",
    "france",
    "belgium",
    "lithuania",
    "estonia",
    "united kingdom",
)

DEFAULT_SIZES = ("S", "M", "L", "XL", "2XL")
DEFAULT_COLORS = ("White", "Black")


# ---------------------------------------------------------------------------
# errori
# ---------------------------------------------------------------------------


class PublishError(RuntimeError):
    """Errore di configurazione o di API: il chiamante lo mostra e si ferma."""

    def __init__(self, message: str, *, code: str = "error", detail: Any = None):
        super().__init__(message)
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------------------
# modelli
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Listing:
    """Una riga di LISTINGS.csv, gia' normalizzata."""

    listing_id: str
    design: str
    product_type: str
    title: str
    description: str
    tags: tuple[str, ...]
    image_light: Path
    image_dark: Path | None
    price_floor: float | None = None

    @property
    def price_floor_usd(self) -> float:
        return self.price_floor if self.price_floor and self.price_floor > 0 else DEFAULT_PRICE_FLOOR


@dataclass
class Variant:
    """Una variante di catalogo (taglia/colore) con il suo costo, se noto."""

    id: int
    title: str
    color: str
    size: str
    cost: float | None = None

    @property
    def is_dark(self) -> bool:
        c = self.color.lower()
        return any(tok in c for tok in DARK_COLOR_TOKENS)


@dataclass
class CatalogChoice:
    """Il prodotto scelto nel catalogo Printify per un listing."""

    blueprint_id: int
    blueprint_title: str
    print_provider_id: int
    print_provider_title: str
    provider_country: str
    variants: list[Variant] = field(default_factory=list)


# ---------------------------------------------------------------------------
# configurazione
# ---------------------------------------------------------------------------


def _csv_list(raw: str | None, default: Sequence[str]) -> tuple[str, ...]:
    if raw is None:
        return tuple(default)
    items = tuple(x.strip() for x in raw.split(",") if x.strip())
    return items or tuple(default)


def _float_env(name: str, default: float, source: Mapping[str, str] | None = None) -> float:
    """Legge un float; un valore impossibile NON spegne la regola.

    `source` esiste perche' `Settings.from_env(env)` deve leggere DAVVERO il
    mapping ricevuto: con `os.getenv` diretto i test (e qualunque chiamante che
    inietti un ambiente) vedevano i default e non l'ambiente passato.
    """
    raw = (source if source is not None else os.environ).get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = float(str(raw))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


@dataclass(frozen=True)
class Settings:
    token: str
    shop_id: str
    api_base: str = PRINTIFY_API
    blueprint_id: int | None = None
    blueprint_title: str = "Unisex Heavy Cotton Tee"
    print_provider_id: int | None = None
    sizes: tuple[str, ...] = DEFAULT_SIZES
    colors: tuple[str, ...] = DEFAULT_COLORS
    image_scale: float = DEFAULT_IMAGE_SCALE
    price_floor: float = DEFAULT_PRICE_FLOOR
    markup: float = DEFAULT_MARKUP
    price_mode: str = "fixed"  # fixed | markup
    user_agent: str = "quotaverace-pod-publisher/1.0"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        src = dict(os.environ if env is None else env)
        blueprint_raw = (src.get("PRINTIFY_BLUEPRINT") or "").strip()
        provider_raw = (src.get("PRINTIFY_PRINT_PROVIDER") or "").strip()
        mode = (src.get("PRINTIFY_PRICE_MODE") or "fixed").strip().lower()
        if mode not in ("fixed", "markup"):
            mode = "fixed"
        return cls(
            token=(src.get("PRINTIFY_TOKEN") or "").strip(),
            shop_id=(src.get("PRINTIFY_SHOP_ID") or "").strip(),
            api_base=(src.get("PRINTIFY_API") or PRINTIFY_API).rstrip("/"),
            blueprint_id=int(blueprint_raw) if blueprint_raw.isdigit() else None,
            blueprint_title=(src.get("PRINTIFY_BLUEPRINT_TITLE") or "Unisex Heavy Cotton Tee").strip(),
            print_provider_id=int(provider_raw) if provider_raw.isdigit() else None,
            sizes=_csv_list(src.get("PRINTIFY_SIZES"), DEFAULT_SIZES),
            colors=_csv_list(src.get("PRINTIFY_COLORS"), DEFAULT_COLORS),
            image_scale=_float_env("PRINTIFY_IMAGE_SCALE", DEFAULT_IMAGE_SCALE, src),
            price_floor=_float_env("PRINTIFY_PRICE_FLOOR", DEFAULT_PRICE_FLOOR, src),
            markup=_float_env("PRINTIFY_MARKUP", DEFAULT_MARKUP, src),
            price_mode=mode,
        )


# ---------------------------------------------------------------------------
# trasporto HTTP (iniettabile: i test non toccano la rete)
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


class PrintifyClient:
    """Client minimo: solo gli endpoint che servono, con trasporto iniettabile."""

    def __init__(self, settings: Settings, transport: Transport | None = None):
        if not settings.token:
            raise PublishError(
                "PRINTIFY_TOKEN mancante: senza token nessuna chiamata (fail-closed).",
                code="token_missing",
            )
        self.settings = settings
        self._transport = transport or _requests_transport
        self.calls: list[tuple[str, str]] = []

    # -- internals ---------------------------------------------------------
    def _call(self, method: str, path: str, body: Any = None) -> Any:
        url = f"{self.settings.api_base}{path}"
        headers = {
            "Authorization": f"Bearer {self.settings.token}",
            "Content-Type": "application/json",
            "User-Agent": self.settings.user_agent,
        }
        self.calls.append((method, path))
        status, payload = self._transport(method, url, body, headers)
        if status >= 400:
            raise PublishError(
                f"Printify {method} {path} -> HTTP {status}",
                code="http_error",
                detail=payload,
            )
        return payload

    # -- endpoint ----------------------------------------------------------
    def shops(self) -> list[dict]:
        data = self._call("GET", "/shops.json")
        return data if isinstance(data, list) else []

    def blueprints(self, page_size: int = 100, max_pages: int = 12) -> list[dict]:
        """Elenca i blueprint (paginati con `last_id`).

        Il catalogo Printify ha centinaia di voci e l'endpoint elenco non ha
        una ricerca per titolo: si pagina finche' non si esaurisce, con un
        tetto di pagine per non trasformare un nome sbagliato in cento
        chiamate.
        """
        found: list[dict] = []
        last_id: int | None = None
        for _ in range(max(1, max_pages)):
            query = f"limit={page_size}" if last_id is None else f"limit={page_size}&last_id={last_id}"
            page = self._call("GET", f"/catalog/blueprints.json?{query}")
            rows = page.get("data") if isinstance(page, dict) else page
            rows = rows or []
            if not rows:
                break
            found.extend(rows)
            new_last = rows[-1].get("id")
            if not isinstance(new_last, int) or new_last == last_id:
                break
            last_id = new_last
        return found

    def blueprint(self, blueprint_id: int) -> dict:
        return self._call("GET", f"/catalog/blueprints/{blueprint_id}.json")

    def print_providers(self, blueprint_id: int) -> list[dict]:
        data = self._call("GET", f"/catalog/blueprints/{blueprint_id}/print_providers.json")
        rows = data if isinstance(data, list) else (data or {}).get("data", [])
        return rows or []

    def provider_variants(self, blueprint_id: int, provider_id: int) -> list[dict]:
        data = self._call("GET", f"/catalog/blueprints/{blueprint_id}/print_providers/{provider_id}/variants.json")
        rows = data.get("variants") if isinstance(data, dict) else data
        return rows or []

    def upload_image(self, path: Path, file_name: str | None = None) -> str:
        raw = path.read_bytes()
        body = {
            "file_name": file_name or path.name,
            "contents": base64.b64encode(raw).decode("ascii"),
        }
        data = self._call("POST", "/uploads/images.json", body)
        image_id = (data or {}).get("id")
        if not image_id:
            raise PublishError(
                f"upload di {path.name} senza id nella risposta: non creo un prodotto senza immagine.",
                code="upload_without_id",
                detail=data,
            )
        return str(image_id)

    def create_product(self, payload: Mapping[str, Any]) -> dict:
        data = self._call("POST", f"/shops/{self.settings.shop_id}/products.json", dict(payload))
        if not isinstance(data, dict) or not data.get("id"):
            raise PublishError("creazione prodotto senza id: nessuna pubblicazione.", code="product_without_id", detail=data)
        return data

    def publish_product(self, product_id: str) -> dict:
        body = {
            "title": True,
            "description": True,
            "images": True,
            "variants": True,
            "tags": True,
            "keyFeatures": True,
            "shipping_template": True,
        }
        return self._call("POST", f"/shops/{self.settings.shop_id}/products/{product_id}/publish.json", body)


# ---------------------------------------------------------------------------
# lettura e validazione dei listing
# ---------------------------------------------------------------------------


class ListingIssues(list):
    """Lista di problemi con un `ok` comodo."""

    @property
    def ok(self) -> bool:
        return not self

    def blocking(self) -> list[str]:
        return [i for i in self if not i.startswith("avviso:")]


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def _resolve_path(raw: str, base: Path) -> Path:
    p = Path(raw.strip())
    if p.is_absolute():
        return p
    for candidate in (base / p, ROOT / p, ROOT.parent / p):
        if candidate.exists():
            return candidate
    return base / p


def validate_listing(listing: Listing, *, ip_terms: Sequence[str] = ()) -> ListingIssues:
    """Tutte le regole che, se violate, farebbero rifiutare il listing."""
    issues = ListingIssues()
    title = _norm(listing.title)
    if not title:
        issues.append("titolo vuoto")
    elif len(title) > ETSY_TITLE_MAX:
        issues.append(f"titolo {len(title)} caratteri > {ETSY_TITLE_MAX} (Etsy rifiuta)")
    if not listing.description.strip():
        issues.append("descrizione vuota")
    elif len(listing.description.strip()) < ETSY_DESC_MIN:
        issues.append(f"descrizione troppo corta ({len(listing.description.strip())} < {ETSY_DESC_MIN})")
    if len(listing.description) > ETSY_DESC_MAX:
        issues.append(f"descrizione oltre {ETSY_DESC_MAX} caratteri")
    if not listing.tags:
        issues.append("nessun tag (Etsy ne usa fino a 13)")
    if len(listing.tags) > ETSY_TAGS_MAX:
        issues.append(f"{len(listing.tags)} tag > {ETSY_TAGS_MAX} (Etsy rifiuta)")
    for tag in listing.tags:
        if len(tag) > ETSY_TAG_LEN_MAX:
            issues.append(f"tag '{tag}' di {len(tag)} caratteri > {ETSY_TAG_LEN_MAX} (Etsy rifiuta)")
        if not tag.strip():
            issues.append("tag vuoto")
    seen: set[str] = set()
    for tag in listing.tags:
        key = tag.lower()
        if key in seen:
            issues.append(f"tag duplicato: '{tag}'")
        seen.add(key)
    if len(listing.tags) > PRINTIFY_TAGS_MAX:
        issues.append(f"{len(listing.tags)} tag > {PRINTIFY_TAGS_MAX} (Printify tronca)")

    if not listing.image_light.exists():
        issues.append(f"immagine assente: {listing.image_light}")
    if listing.image_dark is not None and not listing.image_dark.exists():
        issues.append(f"immagine (capi scuri) assente: {listing.image_dark}")
    if listing.image_dark is None:
        issues.append("avviso: nessuna immagine per capi scuri, i colori scuri verranno esclusi")
    if any(tok in c.lower() for c in DEFAULT_COLORS for tok in DARK_COLOR_TOKENS) and listing.image_dark is None:
        # gia' coperto dall'avviso: qui nessun blocco, il filtro colori decide
        pass

    blob = f"{listing.title} {listing.description} {' '.join(listing.tags)}".lower()
    for term in ip_terms:
        t = term.strip().lower()
        if len(t) < 3:
            continue
        if _ip_hit(blob, t):
            issues.append(f"termine IP protetto nel testo: '{term}'")
    return issues


def _ip_hit(text: str, term: str) -> bool:
    """Vero se il termine compare come PAROLA, non come frammento.

    L'apostrofo e' un caso reale, non teorico: il termine 'dell' (marca di PC)
    colpiva l'articolo italiano "dell'ultimo minuto" e bloccava listing
    legittimi. Un apostrofo SUBITO dopo il termine e' un'elisione: non e' un
    riferimento al marchio e viene ignorato. Un marchio seguito da spazio o da
    un'altra parola resta rilevato.
    """
    pattern = re.compile(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])")
    for match in pattern.finditer(text):
        following = text[match.end() : match.end() + 1]
        if following in ("'", "\u2019"):
            continue
        return True
    return False


def read_listings(path: Path = DEFAULT_LISTINGS) -> list[Listing]:
    """Legge il CSV e restituisce i listing; un CSV rotto solleva con dettaglio."""
    if not path.exists():
        raise PublishError(f"file listing non trovato: {path}", code="listings_missing")
    base = path.parent
    out: list[Listing] = []
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        required = {"listing_id", "design", "product_type", "title", "description", "tags", "image_light"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise PublishError(
                f"colonne mancanti nel CSV: {', '.join(sorted(missing))}",
                code="csv_columns",
            )
        for row in reader:
            if not (row.get("listing_id") or "").strip():
                continue
            tags_raw = row.get("tags") or ""
            tags = tuple(_norm(t) for t in re.split(r"[,;\n]", tags_raw) if t.strip())
            floor_raw = (row.get("price_floor") or "").strip()
            try:
                floor = float(floor_raw) if floor_raw else None
            except ValueError:
                floor = None
            image_dark_raw = (row.get("image_dark") or "").strip()
            out.append(
                Listing(
                    listing_id=row["listing_id"].strip(),
                    design=(row.get("design") or "").strip(),
                    product_type=(row.get("product_type") or "tee").strip().lower(),
                    title=_norm(row.get("title") or ""),
                    description=(row.get("description") or "").strip(),
                    tags=tags,
                    image_light=_resolve_path(row.get("image_light") or "", base),
                    image_dark=_resolve_path(image_dark_raw, base) if image_dark_raw else None,
                    price_floor=floor,
                )
            )
    return out


def load_ip_terms(path: Path | None = DEFAULT_IP_TERMS) -> tuple[str, ...]:
    """Termini IP dal file del cantiere Redbubble (se presente)."""
    if path is None or not Path(path).exists():
        return ()
    terms: list[str] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        terms.append(line)
    return tuple(terms)


# ---------------------------------------------------------------------------
# pricing
# ---------------------------------------------------------------------------


def price_for(cost_usd: float | None, *, settings: Settings) -> tuple[float, str]:
    """Prezzo di vendita e motivo.

    Con `price_mode=markup` e un costo noto: costo x markup arrotondato a .99
    per eccesso, mai sotto il pavimento. Senza costo la regola non si inventa
    un margine: si usa il prezzo fisso e lo si dichiara.
    """
    floor = settings.price_floor
    if settings.price_mode != "markup" or cost_usd is None or cost_usd <= 0:
        reason = "prezzo fisso" if settings.price_mode == "fixed" else "costo variante ignoto -> prezzo fisso"
        return round(max(floor, settings.price_floor), 2), reason
    target = cost_usd * settings.markup
    whole = math.floor(target)
    price = whole + PRICE_ENDING_CENTS / 100
    if price < target:
        price += 1.0
    price = max(price, floor)
    return round(price, 2), "costo x markup"


# ---------------------------------------------------------------------------
# catalogo: scelta del prodotto e delle varianti
# ---------------------------------------------------------------------------


def _match_blueprint(blueprints: Sequence[Mapping[str, Any]], wanted: str) -> Mapping[str, Any] | None:
    want = wanted.strip().lower()
    if not want:
        return None
    for bp in blueprints:
        if (bp.get("title") or "").strip().lower() == want:
            return bp
    for bp in blueprints:
        if want in (bp.get("title") or "").strip().lower():
            return bp
    return None


def _country_rank(country: str) -> int:
    c = (country or "").strip().lower()
    for i, eu in enumerate(EU_PRINT_COUNTRIES):
        if eu in c:
            return i
    return len(EU_PRINT_COUNTRIES) + 1


def choose_provider(providers: Sequence[Mapping[str, Any]], preferred: int | None = None) -> Mapping[str, Any] | None:
    """Fornitore di stampa: preferenza esplicita, poi Europa, poi il primo.

    Il rank europeo non e' estetica: un fornitore USA raddoppia i tempi di
    consegna verso l'Italia e puo' aggiungere dazi al cliente.
    """
    if not providers:
        return None
    if preferred is not None:
        for p in providers:
            if p.get("id") == preferred:
                return p
    ranked = sorted(providers, key=lambda p: (_country_rank((p.get("location") or {}).get("country", "")), -(p.get("variant_count") or 0)))
    return ranked[0]


def build_variants(raw_variants: Sequence[Mapping[str, Any]], *, settings: Settings, want_dark: bool) -> list[Variant]:
    """Filtra per taglia/colore e legge il costo quando il catalogo lo espone."""
    out: list[Variant] = []
    for v in raw_variants:
        opts = v.get("options") or {}
        color = str(opts.get("color") or "").strip()
        size = str(opts.get("size") or "").strip()
        if color and color.lower() not in {c.lower() for c in settings.colors}:
            continue
        if size and size.upper() not in {s.upper() for s in settings.sizes}:
            continue
        if not color and not size:
            # varianti senza opzioni (es. adesivi): si tengono solo se uniche
            pass
        cost_raw = v.get("cost") if v.get("cost") is not None else v.get("price")
        cost: float | None = None
        if isinstance(cost_raw, bool):
            cost = None
        elif isinstance(cost_raw, int):
            # Printify espone gli importi come interi in CENTESIMI (1499 = 14,99).
            cost = round(cost_raw / 100.0, 2) if cost_raw > 0 else None
        elif isinstance(cost_raw, float):
            # Un float e' gia' un importo in unita' (raro nei payload reali).
            cost = round(cost_raw, 2) if cost_raw > 0 else None
        variant = Variant(id=int(v["id"]), title=str(v.get("title") or ""), color=color, size=size, cost=cost)
        if variant.is_dark and not want_dark:
            continue
        out.append(variant)
    return out


# ---------------------------------------------------------------------------
# costruzione dei payload (la parte che si puo' verificare senza toccare nulla)
# ---------------------------------------------------------------------------


def build_print_areas(listing: Listing, variants: Sequence[Variant], image_ids: Mapping[str, str], *, settings: Settings) -> list[dict]:
    """Una print_area per gruppo di colore: capi scuri e chiari hanno inchiostri diversi."""
    groups: dict[str, list[int]] = {}
    for v in variants:
        key = "dark" if v.is_dark else "light"
        groups.setdefault(key, []).append(v.id)
    areas: list[dict] = []
    for key, ids in groups.items():
        image_id = image_ids.get(key)
        if not image_id:
            continue
        areas.append(
            {
                "variant_ids": ids,
                "placeholders": [
                    {
                        "position": PRINT_PLACEHOLDER,
                        "images": [
                            {
                                "id": image_id,
                                "x": 0.5,
                                "y": 0.5,
                                "scale": settings.image_scale,
                                "angle": 0,
                            }
                        ],
                    }
                ],
            }
        )
    return areas


def build_product_payload(listing: Listing, choice: CatalogChoice, image_ids: Mapping[str, str], *, settings: Settings) -> dict:
    variants = [v for v in choice.variants]
    if not variants:
        raise PublishError(
            f"[{listing.listing_id}] nessuna variante utilizzabile (taglie {settings.sizes} / colori {settings.colors})",
            code="no_variants",
        )
    priced: list[dict] = []
    for v in variants:
        price, _ = price_for(v.cost, settings=settings)
        priced.append({"id": v.id, "price": int(round(price * 100)), "is_enabled": True})
    areas = build_print_areas(listing, variants, image_ids, settings=settings)
    if not areas:
        raise PublishError(
            f"[{listing.listing_id}] nessuna immagine associata alle varianti",
            code="no_print_areas",
        )
    return {
        "title": listing.title,
        "description": listing.description,
        "tags": list(listing.tags)[:PRINTIFY_TAGS_MAX],
        "blueprint_id": choice.blueprint_id,
        "print_provider_id": choice.print_provider_id,
        "variants": priced,
        "print_areas": areas,
    }


def file_fingerprint(paths: Iterable[Path]) -> str:
    h = hashlib.sha256()
    for p in paths:
        if p and Path(p).exists():
            h.update(Path(p).name.encode())
            h.update(Path(p).read_bytes())
    return h.hexdigest()[:16]


# ---------------------------------------------------------------------------
# registro di pubblicazione (idempotenza)
# ---------------------------------------------------------------------------


def load_ledger(path: Path = DEFAULT_LEDGER) -> dict:
    if not path.exists():
        return {"listings": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"listings": {}, "error": "registro illeggibile, non sovrascritto"}
    if not isinstance(data, dict):
        return {"listings": {}}
    data.setdefault("listings", {})
    return data


def save_ledger(data: Mapping[str, Any], path: Path = DEFAULT_LEDGER) -> bool:
    """Scrittura atomica; un errore non deve far credere che il listing esista."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# piano (dry-run) e pubblicazione
# ---------------------------------------------------------------------------


def resolve_catalog(client: PrintifyClient | None, settings: Settings, *, listing: Listing | None = None) -> CatalogChoice:
    """Sceglie blueprint + fornitore + varianti. Con `client=None` fallisce."""
    if client is None:
        raise PublishError("catalogo non disponibile (serve PRINTIFY_TOKEN)", code="no_catalog")
    if settings.blueprint_id is not None:
        bp_id = int(settings.blueprint_id)
        bp_title = ""
        try:
            bp = client.blueprint(bp_id)
            bp_title = str((bp or {}).get("title") or "")
        except PublishError:
            bp_title = ""
    else:
        bps = client.blueprints(max_pages=8)
        match = _match_blueprint(bps, settings.blueprint_title)
        if match is None:
            raise PublishError(
                f"blueprint '{settings.blueprint_title}' non trovato nel catalogo: imposta PRINTIFY_BLUEPRINT con l'id del prodotto scelto.",
                code="blueprint_missing",
            )
        bp_id = int(match["id"])
        bp_title = str(match.get("title") or "")

    providers = client.print_providers(bp_id)
    provider = choose_provider(providers, settings.print_provider_id)
    if provider is None:
        raise PublishError(f"nessun fornitore di stampa per il blueprint {bp_id}", code="provider_missing")
    raw_variants = client.provider_variants(bp_id, int(provider["id"]))
    variants = build_variants(raw_variants, settings=settings, want_dark=bool(listing and listing.image_dark))
    return CatalogChoice(
        blueprint_id=bp_id,
        blueprint_title=bp_title,
        print_provider_id=int(provider["id"]),
        print_provider_title=str(provider.get("title") or ""),
        provider_country=str((provider.get("location") or {}).get("country") or ""),
        variants=variants,
    )


def plan_listing(
    listing: Listing,
    *,
    settings: Settings,
    client: PrintifyClient | None = None,
    ip_terms: Sequence[str] = (),
    choice: CatalogChoice | None = None,
) -> dict:
    """Piano completo per UN listing: validazione, file, catalogo, payload."""
    issues = validate_listing(listing, ip_terms=ip_terms)
    entry: dict[str, Any] = {
        "listing_id": listing.listing_id,
        "design": listing.design,
        "title": listing.title,
        "title_len": len(listing.title),
        "tags": list(listing.tags),
        "n_tags": len(listing.tags),
        "images": {"light": str(listing.image_light)},
        "ok": False,
        "issues": list(issues),
        "blocking": issues.blocking(),
    }
    if listing.image_dark is not None:
        entry["images"]["dark"] = str(listing.image_dark)
    entry["validation_ok"] = issues.ok
    if issues.blocking():
        entry["error"] = "listing non valido: non verrebbe pubblicato"
        entry["error_code"] = "invalid_listing"
        return entry

    if choice is None:
        try:
            choice = resolve_catalog(client, settings, listing=listing)
        except PublishError as exc:
            entry["error"] = str(exc)
            entry["error_code"] = exc.code
            # Senza token non si puo' leggere il catalogo: e' una condizione
            # diversa da un listing sbagliato e non deve sembrare la stessa.
            entry["needs_token"] = exc.code == "no_catalog"
            return entry

    entry["catalog"] = {
        "blueprint_id": choice.blueprint_id,
        "blueprint": choice.blueprint_title,
        "print_provider_id": choice.print_provider_id,
        "print_provider": choice.print_provider_title,
        "provider_country": choice.provider_country,
        "variants": len(choice.variants),
    }
    if not choice.variants:
        entry["error"] = "nessuna variante utilizzabile (taglie/colori)"
        entry["error_code"] = "no_variants"
        return entry

    costs = [v.cost for v in choice.variants if v.cost]
    price, price_reason = price_for(costs[0] if costs else None, settings=settings)
    entry["price"] = price
    entry["price_reason"] = price_reason
    entry["margin"] = round(price - costs[0], 2) if costs else None

    # I payload si costruiscono con id immagine SEGNAPOSTO in dry-run: la forma
    # e' identica a quella reale, quindi un errore di struttura si vede subito.
    placeholders = {"light": "DRY-RUN-LIGHT", "dark": "DRY-RUN-DARK"} if listing.image_dark else {"light": "DRY-RUN-LIGHT"}
    try:
        entry["product_payload"] = build_product_payload(listing, choice, placeholders, settings=settings)
    except PublishError as exc:
        entry["error"] = str(exc)
        entry["error_code"] = exc.code
        return entry
    entry["ok"] = True
    return entry


def dry_run(
    listings: Sequence[Listing],
    *,
    settings: Settings,
    client: PrintifyClient | None = None,
    ip_terms: Sequence[str] = (),
    ledger: Mapping[str, Any] | None = None,
) -> dict:
    ledger_rows = (ledger or {}).get("listings", {}) if ledger else {}
    # Il catalogo si legge UNA volta per gruppo di colore (con o senza capi
    # scuri), non una volta per listing: le varianti dipendono da quello, e
    # usarne uno solo per tutti mostrerebbe varianti nere anche dove l'immagine
    # scura non esiste (il percorso live ha la stessa regola).
    choices: dict[bool, CatalogChoice] = {}
    catalog_error: str | None = None
    if client is not None:
        for want_dark in sorted({l.image_dark is not None for l in listings}, reverse=True):
            probe = next(l for l in listings if (l.image_dark is not None) == want_dark)
            try:
                choices[want_dark] = resolve_catalog(client, settings, listing=probe)
            except PublishError as exc:
                catalog_error = str(exc)
                break
    entries = []
    for listing in listings:
        already = ledger_rows.get(listing.listing_id)
        choice = choices.get(listing.image_dark is not None)
        entry = plan_listing(listing, settings=settings, client=client, ip_terms=ip_terms, choice=choice)
        entry["already_published"] = bool(already)
        if already:
            entry["product_id"] = (already or {}).get("product_id")
        entries.append(entry)
    blocked = [e for e in entries if not e.get("ok") and not e.get("needs_token")]
    needs_token = [e for e in entries if e.get("needs_token")]
    return {
        "mode": "dry-run",
        "catalog": "letto" if choices else ("non disponibile" if client is None else f"errore: {catalog_error}"),
        "n_listings": len(entries),
        "ok": sum(1 for e in entries if e.get("ok")),
        "blocked": len(blocked),
        "needs_token": len(needs_token),
        "already_published": sum(1 for e in entries if e.get("already_published")),
        "listings": entries,
    }


def publish_listing(
    listing: Listing,
    *,
    settings: Settings,
    client: PrintifyClient,
    choice: CatalogChoice,
    ledger: dict,
    ledger_path: Path = DEFAULT_LEDGER,
) -> dict:
    """Carica, crea e pubblica UN listing. Idempotente sul registro."""
    existing = (ledger.get("listings") or {}).get(listing.listing_id)
    if existing and existing.get("product_id"):
        return {
            "listing_id": listing.listing_id,
            "skipped": True,
            "reason": "gia' pubblicato (registro)",
            "product_id": existing["product_id"],
        }

    light_id = client.upload_image(listing.image_light)
    image_ids = {"light": light_id}
    if listing.image_dark is not None and any(v.is_dark for v in choice.variants):
        image_ids["dark"] = client.upload_image(listing.image_dark)

    payload = build_product_payload(listing, choice, image_ids, settings=settings)
    product = client.create_product(payload)
    product_id = str(product["id"])

    published = False
    publish_error: str | None = None
    try:
        client.publish_product(product_id)
        published = True
    except PublishError as exc:
        publish_error = str(exc)

    ledger.setdefault("listings", {})[listing.listing_id] = {
        "product_id": product_id,
        "title": listing.title,
        "blueprint_id": choice.blueprint_id,
        "print_provider_id": choice.print_provider_id,
        "variants": len(choice.variants),
        "published": published,
        "publish_error": publish_error,
        "fingerprint": file_fingerprint([listing.image_light, listing.image_dark] if listing.image_dark else [listing.image_light]),
    }
    save_ledger(ledger, ledger_path)
    return {
        "listing_id": listing.listing_id,
        "skipped": False,
        "product_id": product_id,
        "published": published,
        "publish_error": publish_error,
        "variants": len(choice.variants),
    }


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


def format_report(report: Mapping[str, Any]) -> str:
    lines: list[str] = []
    mode = report.get("mode", "dry-run")
    lines.append(f"=== PUBLISHER ETSY/PRINTIFY — {mode} ===")
    if mode == "dry-run":
        lines.append(
            f"catalogo: {report.get('catalog')} | listing: {report.get('n_listings')} | "
            f"pronti: {report.get('ok')} | bloccati: {report.get('blocked')} | "
            f"in attesa di token: {report.get('needs_token')} | "
            f"gia' pubblicati: {report.get('already_published')}"
        )
        if report.get("needs_token"):
            lines.append(
                "nota: senza PRINTIFY_TOKEN il catalogo (blueprint, fornitore, varianti) non si legge,"
                " quindi prezzi e varianti non sono calcolati. Testi, file e immagini sono gia' verificati."
            )
    else:
        lines.append(
            f"pubblicati: {report.get('published')} | saltati: {report.get('skipped')} | "
            f"errori: {report.get('errors')}"
        )
    lines.append("")
    for entry in report.get("listings", []):
        lid = entry.get("listing_id", "?")
        if entry.get("ok") or entry.get("published") or entry.get("skipped"):
            status = "OK" if entry.get("ok") else ("PUBBLICATO" if entry.get("published") else "SALTATO")
            extra = []
            if entry.get("price") is not None:
                extra.append(f"{entry['price']:.2f} USD")
            if entry.get("catalog"):
                cat = entry["catalog"]
                extra.append(f"{cat.get('blueprint')} @ {cat.get('print_provider')} ({cat.get('provider_country')})")
                extra.append(f"{cat.get('variants')} varianti")
            if entry.get("already_published"):
                extra.append("gia' a registro")
            lines.append(f"[{status}] {lid}: {' | '.join(str(x) for x in extra if x)}")
            if entry.get("product_id"):
                lines.append(f"         product_id: {entry['product_id']}")
        elif entry.get("needs_token"):
            # Il listing e' valido: manca solo il catalogo. Chiamarlo "bloccato"
            # farebbe cercare un errore nel testo che non c'e'.
            lines.append(f"[ATTESA TOKEN] {lid}: testi e file validati, catalogo non letto")
        else:
            lines.append(f"[BLOCCATO] {lid}: {entry.get('error') or 'non valido'}")
            for issue in entry.get("blocking") or entry.get("issues") or []:
                lines.append(f"         - {issue}")
    for entry in report.get("listings", []):
        warns = [i for i in (entry.get("issues") or []) if i.startswith("avviso:")]
        for w in warns:
            lines.append(f"avviso [{entry.get('listing_id')}]: {w.replace('avviso: ', '')}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def check(settings: Settings, client: PrintifyClient) -> dict:
    shops = client.shops()
    selected = None
    for shop in shops:
        if settings.shop_id and str(shop.get("id")) == str(settings.shop_id):
            selected = shop
            break
    if selected is None and len(shops) == 1:
        selected = shops[0]
    channel = str((selected or {}).get("sales_channel") or "")
    return {
        "shops": [{"id": s.get("id"), "title": s.get("title"), "sales_channel": s.get("sales_channel")} for s in shops],
        "shop_id": (selected or {}).get("id"),
        "sales_channel": channel,
        "etsy_connected": "etsy" in channel.lower(),
        "token_ok": True,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pubblica i listing su Etsy tramite Printify (dry-run di default).")
    parser.add_argument("--listings", default=str(DEFAULT_LISTINGS), help="CSV dei listing")
    parser.add_argument("--limit", type=int, default=0, help="max listing da processare (0 = tutti)")
    parser.add_argument("--only", default="", help="pubblica solo questi listing_id (virgole)")
    parser.add_argument("--live", action="store_true", help="esegue davvero (default: dry-run)")
    parser.add_argument("--dry-run", action="store_true", help="forza il dry-run")
    parser.add_argument("--check", action="store_true", help="verifica token, negozio e canale Etsy")
    parser.add_argument("--json", action="store_true", help="output JSON")
    parser.add_argument("--report", default="", help="scrive il report JSON in questo file")
    parser.add_argument("--ip-terms", default=str(DEFAULT_IP_TERMS), help="file dei termini protetti (0 = disattiva)")
    parser.add_argument("--ledger", default=str(DEFAULT_LEDGER), help="registro di pubblicazione")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    live = args.live and not args.dry_run
    if live and not settings.shop_id:
        print("PRINTIFY_SHOP_ID mancante: senza negozio la pubblicazione non ha destinazione.", file=sys.stderr)
        return 2

    try:
        client = PrintifyClient(settings) if settings.token else None
    except PublishError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.check:
        if client is None:
            print("PRINTIFY_TOKEN mancante: impossibile verificare il negozio.", file=sys.stderr)
            return 2
        try:
            info = check(settings, client)
        except PublishError as exc:
            print(f"{exc}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(info, indent=2, ensure_ascii=False))
        else:
            print("=== VERIFICA PRINTIFY ===")
            for s in info["shops"]:
                print(f"  negozio {s['id']} | {s['title']} | canale: {s['sales_channel']}")
            print(f"canale Etsy collegato: {'SI' if info['etsy_connected'] else 'NO'}")
            if not info["etsy_connected"]:
                print("  -> collega il negozio Etsy in Printify (My Stores -> Connect) prima di pubblicare.")
        return 0 if info["etsy_connected"] else 1

    ip_terms = load_ip_terms(Path(args.ip_terms)) if args.ip_terms and args.ip_terms != "0" else ()
    try:
        listings = read_listings(Path(args.listings))
    except PublishError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    only = {x.strip() for x in args.only.split(",") if x.strip()}
    if only:
        listings = [l for l in listings if l.listing_id in only]
    if args.limit and args.limit > 0:
        listings = listings[: args.limit]
    if not listings:
        print("nessun listing da processare.", file=sys.stderr)
        return 1

    ledger = load_ledger(Path(args.ledger))

    if not live:
        report = dry_run(listings, settings=settings, client=client, ip_terms=ip_terms, ledger=ledger)
    else:
        assert client is not None
        entries: list[dict] = []
        published = skipped = errors = 0
        choice_cache: dict[str, CatalogChoice] = {}
        for listing in listings:
            key = "dark" if listing.image_dark is not None else "light"
            try:
                if key not in choice_cache:
                    choice_cache[key] = resolve_catalog(client, settings, listing=listing)
                result = publish_listing(
                    listing,
                    settings=settings,
                    client=client,
                    choice=choice_cache[key],
                    ledger=ledger,
                    ledger_path=Path(args.ledger),
                )
            except PublishError as exc:
                errors += 1
                entries.append({"listing_id": listing.listing_id, "ok": False, "error": str(exc), "error_code": exc.code})
                continue
            if result.get("skipped"):
                skipped += 1
            elif result.get("published"):
                published += 1
            else:
                errors += 1
            entries.append({**result, "ok": bool(result.get("published") or result.get("skipped"))})
        report = {
            "mode": "live",
            "published": published,
            "skipped": skipped,
            "errors": errors,
            "listings": entries,
        }

    if args.report:
        try:
            Path(args.report).parent.mkdir(parents=True, exist_ok=True)
            Path(args.report).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            print(f"report non scritto: {exc}", file=sys.stderr)

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(format_report(report))

    if report.get("mode") == "dry-run":
        return 0 if not report.get("blocked") else 1
    return 0 if report.get("errors", 0) == 0 else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
