"""Catalogo per lo store Hoplix: righe copia-incolla + vetrina per il sito.

PERCHE' UN GENERATORE E NON UN DOCUMENTO SCRITTO A MANO
-------------------------------------------------------
I prodotti dello store Hoplix si inseriscono dall'interfaccia (l'API crea
campagne e ordini, non prodotti dello store). Cinque inserimenti manuali
diventano dieci se un titolo e' lungo 80 caratteri e va accorciato, se il colore
ha un codice diverso, se la taglia 2XL va scritta XXL. Un elenco generato dai
dati veri elimina la ricostruzione a memoria e i refusi che poi si pagano in
resi.

COSA PRODUCE
------------
1. `output/STORE_SETUP.md` — cosa incollare in ogni campo, prodotto per
   prodotto, con il file da caricare e il margine atteso.
2. `webapp/content/shop.ts` — il manifest tipizzato che la pagina `/shop` del
   sito importa. Contiene SOLO dati pubblici: nessun margine, nessun costo.
3. `webapp/public/shop/*.png` — le immagini delle card.

REGOLA DELL'INCHIOSTRO (la trappola di questo catalogo)
-------------------------------------------------------
Ogni design esiste in due versioni: inchiostro scuro per capi chiari
(`tee-*.png`) e inchiostro chiaro per capi scuri (`tee-dark-*.png`). Una
campagna Hoplix ha UNA sola immagine fronte e un elenco di colori: mettere
`white,black` con l'immagine per capi chiari renderebbe invisibile la stampa
sul nero. Per questo ogni variante e' una riga separata con i suoi colori:
    capo chiaro -> solo `white`, immagine chiara
    capo scuro  -> solo `black`,  immagine a inchiostro chiaro

USO
---
    venv/bin/python catalog.py                     # tabella a video
    venv/bin/python catalog.py --write-store --write-webapp
    venv/bin/python catalog.py --include-dark       # aggiunge le righe nere
    venv/bin/python catalog.py --base-cost 7.50     # costo vero dal catalogo API
"""

from __future__ import annotations

import argparse
import json
import shutil
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import hoplix
import platforms
from publish import PublishError, Listing, load_ip_terms, read_listings, validate_listing

ROOT = Path(__file__).resolve().parent
DEFAULT_LISTINGS = ROOT / "LISTINGS.csv"
DEFAULT_IP_TERMS = ROOT.parent / "redbubble-tech-gift-2026-work" / "ip_terms.txt"
STORE_MD = ROOT / "output" / "STORE_SETUP.md"
WEB_MANIFEST = ROOT.parent / "webapp" / "content" / "shop.ts"
WEB_IMAGES = ROOT.parent / "webapp" / "public" / "shop"

PRODUCT_CODE = hoplix.DEFAULT_PRODUCT_CODE
SIZES = ("S", "M", "L", "XL", "2XL")

# Colori per inchiostro: definizione UNICA in hoplix.py, usata anche dalle
# campagne. Due copie divergerebbero sull'errore che costa un reso.
INK_COLORS = hoplix.INK_COLORS

# Larghezza massima accettata per le immagini della vetrina: oltre, la pagina
# pesa piu' di quanto serve a una card da 300 px.
MAX_WEB_IMAGE_BYTES = 600_000


# ---------------------------------------------------------------------------
# lettura PNG senza dipendenze
# ---------------------------------------------------------------------------


def png_size(path: Path) -> tuple[int, int]:
    """Legge larghezza e altezza dall'IHDR. Solleva se il file non e' un PNG.

    Pillow non e' una dipendenza del progetto e per due interi non vale
    installarla: l'IHDR di un PNG e' a offset fisso.
    """
    with open(path, "rb") as fh:
        head = fh.read(24)
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"{path.name}: non e' un PNG")
    width, height = struct.unpack(">II", head[16:24])
    return int(width), int(height)


# ---------------------------------------------------------------------------
# righe di catalogo
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Row:
    """Una scheda prodotto da inserire nello store."""

    listing_id: str
    ink: str
    name: str
    design: str
    price: float
    base_cost: float
    margin: float
    margin_pct: float
    colors: tuple[str, ...]
    sizes: tuple[str, ...]
    image: Path
    width: int
    height: int
    area_warning: str | None
    description: str
    # Disegno per capo scuro: serve a pubblicare la campagna "dark" con il
    # file giusto (Hoplix lo scarica da un URL pubblico col NOME originale).
    image_dark: Path | None = None

    @property
    def key(self) -> str:
        return f"{self.listing_id}-{self.ink}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "listing_id": self.listing_id,
            "ink": self.ink,
            "name": self.name,
            "design": self.design,
            "price": self.price,
            "base_cost": self.base_cost,
            "margin": self.margin,
            "margin_pct": self.margin_pct,
            "colors": list(self.colors),
            "sizes": list(self.sizes),
            "image": str(self.image),
            "px": [self.width, self.height],
            "area_warning": self.area_warning,
            "image_dark": str(self.image_dark) if self.image_dark else None,
        }


def store_name(title: str, ink: str, *, limit: int = 80) -> str:
    """Nome per lo store: accorciato a 80 caratteri SENZA tagliare parole.

    Un nome troncato a meta' parola ("Regalo Tecnologico Divertente | CTRL +
    ALT + REGA") fa sembrare il negozio scritto male, e su un regalo la forma
    conta quanto il disegno.
    """
    suffix = " (capo scuro)" if ink == "dark" else ""
    budget = max(20, limit - len(suffix))
    text = " ".join((title or "").split())
    if len(text) > budget:
        cut = text[:budget].rsplit(" ", 1)[0]
        text = cut.rstrip(" ,;|/-") or text[:budget]
    return f"{text}{suffix}"


def _price_for(listing: Listing, override: float | None) -> float:
    if override and override > 0:
        return round(float(override), 2)
    return round(listing.price_floor_usd, 2)


def build_rows(
    listings: Sequence[Listing],
    *,
    base_cost: float = platforms.HOPLIX.product_cost_eur,
    price: float | None = None,
    include_dark: bool = False,
) -> list[Row]:
    """Una riga per (design, inchiostro). Fail-closed sul file mancante."""
    product = hoplix.Product(code=PRODUCT_CODE, name="Maglietta unisex", printable_area="320x400")
    rows: list[Row] = []
    for listing in listings:
        variants: list[tuple[str, Path | None]] = [("light", listing.image_light)]
        if include_dark:
            variants.append(("dark", listing.image_dark))
        for ink, image in variants:
            if image is None:
                continue
            path = Path(image)
            if not path.exists():
                raise PublishError(f"{listing.listing_id}: immagine mancante {path}", code="image_missing")
            width, height = png_size(path)
            area = hoplix.print_area_check(width, height, product)
            final_price = _price_for(listing, price)
            hoplix.validate_price(final_price, base_cost)
            # Il margine si calcola sul costo base EFFETTIVO della riga (che puo'
            # essere stato passato da --base-cost), non su una costante: due
            # numeri diversi per lo stesso margine sarebbero un errore silenzioso.
            margin_value = round((final_price / (1 + platforms.VAT_IT)) - float(base_cost), 2)
            rows.append(
                Row(
                    listing_id=listing.listing_id,
                    ink=ink,
                    name=store_name(listing.title, ink),
                    design=listing.design,
                    price=final_price,
                    base_cost=round(float(base_cost), 2),
                    margin=margin_value,
                    margin_pct=round(100.0 * margin_value / final_price, 1),
                    colors=INK_COLORS[ink],
                    sizes=SIZES,
                    image=path,
                    width=width,
                    height=height,
                    area_warning=area.get("warning"),
                    description=listing.description,
                    image_dark=Path(listing.image_dark) if listing.image_dark else None,
                )
            )
    return rows


def format_table(rows: Sequence[Row]) -> str:
    if not rows:
        return "nessuna riga di catalogo"
    lines = [
        f"=== CATALOGO STORE ({len(rows)} schede) — prezzo/margine al netto IVA {platforms.VAT_IT*100:.0f}% ===",
        f"{'scheda':34s} {'prezzo':>7s} {'margine':>8s} {'marg.%':>7s}  colori / taglie",
    ]
    for r in rows:
        lines.append(
            f"{r.key:34s} {r.price:7.2f} {r.margin:8.2f} {r.margin_pct:6.1f}%  "
            f"{','.join(r.colors)} / {','.join(r.sizes)}"
        )
        if r.area_warning:
            lines.append(f"{'':34s} avviso: {r.area_warning}")
    total = sum(r.margin for r in rows)
    lines.append("")
    lines.append(f"margine per set completo (1 pezzo per scheda): {total:.2f} EUR")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# documento operativo
# ---------------------------------------------------------------------------


def store_markdown(rows: Sequence[Row], *, shop_url: str = "") -> str:
    """Le istruzioni esatte, con i valori da incollare."""
    out: list[str] = [
        "# Store Hoplix — inserimento schede",
        "",
        "Generato da `catalog.py` sui dati veri del CSV. Non correggere a mano: rigenera.",
        "",
        f"- schede da inserire: **{len(rows)}**",
        f"- prezzo per maglietta: **{rows[0].price:.2f} EUR** (spedizione a carico del cliente, 4,95 EUR in Italia)"
        if rows
        else "- nessuna scheda",
        f"- margine per pezzo: **{rows[0].margin:.2f} EUR** (costo {rows[0].base_cost:.2f} IVA incl., IVA 22% tolta dal prezzo)"
        if rows
        else "",
        "",
        "## Perche' si inseriscono a mano",
        "",
        "L'API Hoplix crea **campagne** e **ordini**, non prodotti dello store: e' scritto",
        "nella documentazione ufficiale. Chi promette il contrario non l'ha letto.",
        f"Lo store gratuito si attiva dal sito Hoplix; qui sotto c'e' ogni valore da copiare.",
        "",
        "## Prima di iniziare (una volta sola)",
        "",
        "1. Crea l'account su hoplix.com e apri lo **Store gratuito** (nessun canone, nessuna carta richiesta).",
        "2. In *Impostazioni > Dati fiscali* inserisci i tuoi dati: Hoplix fattura al cliente finale,",
        "   tu fatturi a Hoplix il profitto. Senza dati fiscali i pagamenti non vengono liquidati.",
        "3. Imposta la spedizione: **Italia 4,95 EUR**, **UE ~9,95 EUR** (verifica nella tua area).",
        "   La spedizione la paga il cliente: non entra nel margine.",
        "4. Collega il dominio (facoltativo): `shop.<tuo-dominio>` — su un negozio gratuito il dominio",
        "   personalizzato si configura dal pannello; se chiede un piano, resta sull'indirizzo Hoplix.",
        "",
        "## Le schede",
        "",
    ]
    for i, r in enumerate(rows, 1):
        out += [
            f"### {i}. {r.design}{' — versione capo scuro' if r.ink == 'dark' else ''}",
            "",
            "| campo | valore |",
            "|---|---|",
            f"| Nome | `{r.name}` |",
            f"| Prezzo | `{r.price:.2f} EUR` |",
            f"| Colore | `{', '.join(r.colors)}` |",
            f"| Taglie | `{', '.join(r.sizes)}` |",
            f"| Prodotto | `{PRODUCT_CODE}` |",
            f"| Immagine fronte | `{r.image}` |",
            f"| Margine atteso | `{r.margin:.2f} EUR` ({r.margin_pct:.0f}% del prezzo) |",
            "",
            "Descrizione (copia il blocco sotto, e' gia' quella del CSV):",
            "",
            "```",
            r.description.strip(),
            "```",
            "",
        ]
        if r.area_warning:
            out += [f"> **Attenzione**: {r.area_warning}", ""]
    out += [
        "## Regola dell'inchiostro (la piu' facile da sbagliare)",
        "",
        "Le schede `light` usano l'immagine **a inchiostro scuro** e vanno su capo **bianco**.",
        "Le schede `dark` usano l'immagine **a inchiostro chiaro** e vanno su capo **nero**.",
        "Non unire i due colori in una scheda sola: la campagna ha una sola immagine fronte e",
        "una stampa nera su maglietta nera non si vede (e un reso per questo si paga tutto).",
        "",
        "## Controllo prima di salvare ogni scheda",
        "",
        "- anteprima del mockup: la stampa deve stare dentro l'area, non sul bordo;",
        "- il prezzo mostrato deve essere quello della tabella (Hoplix rifiuta un prezzo",
        "  sotto il costo base, quindi se lo accetta il margine c'e');",
        "- le taglie visibili devono essere S, M, L, XL, 2XL;",
        "- la descrizione non deve contenere prezzi o promesse di spedizione diverse da quelle reali.",
        "",
    ]
    if shop_url:
        out += [f"Indirizzo dello store: {shop_url}", ""]
    return "\n".join(out)


# ---------------------------------------------------------------------------
# vetrina per il sito
# ---------------------------------------------------------------------------


def _web_image_name(row: Row) -> str:
    return f"{row.key}.png"


def _ts_str(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


# Larghezza massima della card sul sito: 460 px di resa su mobile a 2x, e
# 900 px copre anche il retina senza portarsi dietro i 3600 px del file di
# stampa.
CARD_MAX_WIDTH = 900


def _write_card(row: Row, card_dir: Path, max_width: int = CARD_MAX_WIDTH) -> tuple[str, int, int] | None:
    """Riduce l'immagine a misura di scheda (WebP) e ritorna (percorso, w, h).

    I file di stampa sono 3600x4800: servono al laboratorio, non al telefono.
    Caricarli in una card vuol dire 250 KB a prodotto per mostrare un disegno
    alto 256 px. Qui si genera una versione WebP da ~900 px (qualita' 82), che
    e' il formato supportato da tutti i browser moderni con un peso 3-5 volte
    minore. Fail-safe: se Pillow non c'e' o la conversione fallisce, si torna
    l'immagine originale (una pagina piu' pesante e' meglio di una pagina
    senza foto).
    """
    try:
        from PIL import Image  # import pigro: lo script gira anche senza Pillow
    except Exception:  # pragma: no cover - dipende dall'ambiente
        return None
    target = card_dir / f"{row.key}.webp"
    try:
        with Image.open(row.image) as img:
            img = img.convert("RGB")
            if img.width > max_width:
                height = max(1, round(img.height * max_width / img.width))
                img = img.resize((max_width, height), Image.LANCZOS)
            card_dir.mkdir(parents=True, exist_ok=True)
            img.save(target, "WEBP", quality=82, method=6)
            return f"/shop/card/{target.name}", img.width, img.height
    except OSError:
        return None


def webapp_ts(
    rows: Sequence[Row],
    images: dict[str, str] | None = None,
    sizes: dict[str, tuple[int, int]] | None = None,
) -> str:
    """Manifest tipizzato: SOLO dati pubblici (prezzo e immagine, zero margini)."""
    lines = [
        "// GENERATO da pod-shop/catalog.py — non modificare a mano.",
        "// Rigenera con: venv/bin/python catalog.py --write-webapp",
        "",
        "export type ShopProduct = {",
        "  id: string",
        "  name: string",
        "  price: number",
        "  image: string",
        "  imageW: number",
        "  imageH: number",
        "  colors: string[]",
        "  sizes: string[]",
        "  url?: string",
        "}",
        "",
        'export const SHOP_CURRENCY = "EUR"',
        "",
        "export const SHOP_PRODUCTS: ShopProduct[] = [",
    ]
    for r in rows:
        image_path = (images or {}).get(r.key, "/shop/" + _web_image_name(r))
        width, height = (sizes or {}).get(r.key, (r.width, r.height))
        lines += [
            "  {",
            f"    id: {_ts_str(r.key)},",
            f"    name: {_ts_str(r.design)},",
            f"    price: {r.price:.2f},",
            f"    image: {_ts_str(image_path)},",
            f"    imageW: {width},",
            f"    imageH: {height},",
            f"    colors: [{', '.join(_ts_str(c) for c in r.colors)}],",
            f"    sizes: [{', '.join(_ts_str(s) for s in r.sizes)}],",
            "  },",
        ]
    lines += ["]", ""]
    return "\n".join(lines)


def write_store(path: Path = STORE_MD, rows: Sequence[Row] | None = None, *, shop_url: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(store_markdown(rows or [], shop_url=shop_url), encoding="utf-8")
    return path


def write_webapp(
    rows: Sequence[Row],
    *,
    manifest: Path = WEB_MANIFEST,
    images_dir: Path = WEB_IMAGES,
) -> dict[str, Any]:
    """Scrive manifest e immagini. Le immagini troppo pesanti sono saltate.

    Una card da 300 px non ha bisogno di un PNG da 3600 px: copiare 5 file da
    megabyte renderebbe la pagina lenta su mobile, che e' dove arriva il
    traffico gratuito. Se il file e' grande si salta e la pagina disegna il
    riquadro con il nome del design (meglio un testo che una pagina da 5 MB).
    """
    manifest.parent.mkdir(parents=True, exist_ok=True)
    images_dir.mkdir(parents=True, exist_ok=True)

    copied: list[str] = []
    skipped: list[dict[str, Any]] = []
    for row in rows:
        target = images_dir / _web_image_name(row)
        size = row.image.stat().st_size
        if size > MAX_WEB_IMAGE_BYTES:
            skipped.append({"key": row.key, "bytes": size})
            continue
        try:
            shutil.copyfile(row.image, target)
            copied.append(target.name)
        except OSError as exc:  # pragma: no cover - disco
            skipped.append({"key": row.key, "error": str(exc)})

    # ------------------------------------------------------------------
    # File di STAMPA (non sono asset di pagina)
    # ------------------------------------------------------------------
    # Hoplix stampa partendo da un URL pubblico e usa il NOME del file per
    # ricostruirlo (`publish_hoplix.design_url` -> `<base>/<nome originale>`):
    # quindi i disegni vanno pubblicati col nome con cui sono stati generati,
    # NON con la chiave della scheda. Senza questo passaggio il negozio si
    # crea ma la prima campagna si ferma con `design_base_missing`, cioe' un
    # errore che sembra di configurazione ed e' invece un file mai copiato.
    # I file di stampa superano il limite delle immagini di pagina: qui non si
    # applica (nessuno li scarica per navigare), si controlla solo l'esistenza.
    design_dir = images_dir / "design"
    design_dir.mkdir(parents=True, exist_ok=True)
    designs: list[str] = []
    designs_missing: list[dict[str, Any]] = []
    for row in rows:
        for image in (row.image, getattr(row, "image_dark", None)):
            if image is None:
                continue
            source = Path(image)
            if source.name in designs or source.name in [d["name"] for d in designs_missing]:
                continue  # stesso inchiostro su piu' schede: un file basta
            try:
                shutil.copyfile(source, design_dir / source.name)
                designs.append(source.name)
            except OSError as exc:
                designs_missing.append({"key": row.key, "name": source.name, "error": str(exc)})

    # Card ottimizzate per il web: il manifest punta qui, i PNG originali
    # restano serviti da `/shop/` e da `/shop/design/` (stampa).
    card_dir = images_dir / "card"
    card_paths: dict[str, str] = {}
    card_sizes: dict[str, tuple[int, int]] = {}
    for row in rows:
        made = _write_card(row, card_dir)
        if made:
            card_paths[row.key], w, h = made
            card_sizes[row.key] = (w, h)

    manifest.write_text(webapp_ts(rows, images=card_paths, sizes=card_sizes), encoding="utf-8")

    return {
        "manifest": str(manifest),
        "images_dir": str(images_dir),
        "copied": copied,
        "skipped": skipped,
        "card_dir": str(card_dir),
        "cards": sorted(card_paths.values()),
        "design_dir": str(design_dir),
        "designs": sorted(designs),
        "designs_missing": designs_missing,
        "products": len(rows),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Genera lo store setup Hoplix e il manifest per il sito.")
    parser.add_argument("--listings", default=str(DEFAULT_LISTINGS), help="CSV dei listing")
    parser.add_argument("--ip-terms", default=str(DEFAULT_IP_TERMS), help="termini protetti (0 = disattiva)")
    parser.add_argument("--base-cost", type=float, default=platforms.HOPLIX.product_cost_eur,
                        help="costo base IVA inclusa (default 7.90, il valore peggiore documentato)")
    parser.add_argument("--price", type=float, default=None, help="prezzo EUR (default: price_floor del CSV)")
    parser.add_argument("--include-dark", action="store_true", help="aggiunge le schede per capo scuro")
    parser.add_argument("--shop-url", default="", help="indirizzo dello store (scritto nel documento)")
    parser.add_argument("--write-store", action="store_true", help="scrive output/STORE_SETUP.md")
    parser.add_argument("--write-webapp", action="store_true", help="scrive webapp/content/shop.ts + immagini")
    parser.add_argument("--json", action="store_true", help="output JSON")
    args = parser.parse_args(argv)

    ip_terms = load_ip_terms(Path(args.ip_terms)) if args.ip_terms and args.ip_terms != "0" else ()
    try:
        listings = read_listings(Path(args.listings))
    except PublishError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    # Un listing non valido scriverebbe una scheda sbagliata nello store: si
    # ferma prima, come fa il publisher.
    problems: list[str] = []
    for listing in listings:
        issues = validate_listing(listing, ip_terms=ip_terms)
        problems.extend(f"{listing.listing_id}: {b}" for b in issues.blocking())

    try:
        rows = build_rows(
            listings,
            base_cost=args.base_cost,
            price=args.price,
            include_dark=args.include_dark,
        )
    except (PublishError, ValueError, hoplix.HoplixError) as exc:
        print(f"catalogo non generato: {exc}", file=sys.stderr)
        return 1

    out: dict[str, Any] = {
        "rows": [r.as_dict() for r in rows],
        "problems": problems,
        "price": rows[0].price if rows else None,
        "margin_total": round(sum(r.margin for r in rows), 2),
    }
    if args.write_store:
        out["store_md"] = str(write_store(rows=rows, shop_url=args.shop_url))
    if args.write_webapp:
        out["webapp"] = write_webapp(rows)

    if args.json:
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        print(format_table(rows))
        if problems:
            print("\nLISTING NON VALIDI (nessuna scheda scritta per questi):")
            for p in problems:
                print(f"  - {p}")
        for key in ("store_md", "webapp"):
            if out.get(key):
                print(f"\n{key}: {out[key]}")
    return 0 if rows else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
