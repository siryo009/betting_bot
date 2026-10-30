"""Campagne Hoplix via API — pagine di vendita con link condivisibile.

PERCHE' LE CAMPAGNE, SE I PRODOTTI DELLO STORE SI FANNO A MANO
--------------------------------------------------------------
L'API Hoplix non crea prodotti dello store, ma crea **campagne**: una pagina di
vendita pubblica, con pagamento, per UN design. Per il lancio senza pubblicita'
a pagamento la campagna e' esattamente cio' che serve: un link da incollare in
un gruppo, in un commento, in una bio — e chi lo apre compra senza passare dal
negozio.

COSA SERVE (e perche' e' l'unico blocco)
----------------------------------------
Hoplix scarica il disegno da un **URL pubblico**: non legge il disco locale.
Quindi le immagini vanno pubblicate prima (le mette il sito: `catalog.py
--write-webapp` copia i file in `webapp/public/shop/`, il deploy su Vercel li
espone su `https://<sito>/shop/<file>.png`). `HOPLIX_DESIGN_BASE` dice al
publisher quale base usare; senza, il dry-run lo dice invece di fallire a meta'.

PERCHE' UNA CAMPAGNA PER INCHIOSTRO
------------------------------------
La campagna ha UNA immagine fronte. Con `white,black` e l'immagine per capi
chiari, la stampa sul nero sarebbe invisibile. Quindi: campagna `light` ->
colore bianco, immagine scura; campagna `dark` -> colore nero, immagine chiara.
E' la stessa regola di `catalog.py`, applicata al posto dove l'errore costa un
reso.

MODALITA'
---------
`--dry-run` (default)  valida testi, file, proporzioni, prezzo e slug, e
                       costruisce il payload ESATTO della campagna. Se ci sono
                       chiavi legge anche il catalogo vero (costo base reale).
`--live`               crea le campagne. Idempotente sul registro
                       `output/published.json` (chiave `hoplix:<id>:<ink>`).
`--check`              chiavi, costo base e margine al prezzo di listino.

Il trasporto HTTP e' iniettato nei test: nessuna rete, nessuna spesa.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import hoplix
import platforms
from publish import (
    DEFAULT_IP_TERMS,
    DEFAULT_LEDGER,
    DEFAULT_LISTINGS,
    Listing,
    PublishError,
    load_ip_terms,
    load_ledger,
    read_listings,
    save_ledger,
    validate_listing,
)

ROOT = Path(__file__).resolve().parent
DEFAULT_PRODUCT_CODE = hoplix.DEFAULT_PRODUCT_CODE
INKS = ("light", "dark")


@dataclass(frozen=True)
class Settings:
    """Configurazione delle campagne (solo env, nessun segreto su disco)."""

    api_key: str = ""
    secret: str = ""
    design_base: str = ""
    price_floor: float = platforms.DEFAULT_PRICE_EUR
    ink_mode: str = "light"
    test_mode: bool = True
    user_agent: str = hoplix.USER_AGENT

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        src = dict(os.environ if env is None else env)

        def _float(name: str, default: float) -> float:
            raw = (src.get(name) or "").strip()
            if not raw:
                return default
            try:
                value = float(raw)
            except ValueError:
                return default
            return value if value > 0 else default

        ink = (src.get("HOPLIX_INK") or "light").strip().lower()
        if ink not in ("light", "both"):
            ink = "light"
        return cls(
            api_key=(src.get("HOPLIX_API_KEY") or "").strip(),
            secret=(src.get("HOPLIX_SECRET") or "").strip(),
            design_base=(src.get("HOPLIX_DESIGN_BASE") or "").strip(),
            price_floor=_float("HOPLIX_PRICE_FLOOR", platforms.DEFAULT_PRICE_EUR),
            ink_mode=ink,
            test_mode=str(src.get("HOPLIX_TEST_MODE", "1")).strip().lower()
            not in ("0", "false", "no", "off"),
        )


def design_url(listing: Listing, ink: str, settings: Settings) -> str:
    """URL pubblico del disegno giusto per l'inchiostro della campagna."""
    image = listing.image_dark if ink == "dark" else listing.image_light
    if image is None:
        raise PublishError(f"{listing.listing_id}: manca l'immagine per l'inchiostro {ink}", code="image_missing")
    path = Path(image)
    base = (settings.design_base or "").strip().rstrip("/")
    if not base:
        raise PublishError(
            "HOPLIX_DESIGN_BASE mancante: Hoplix scarica il disegno da un URL pubblico "
            "(es. https://quotaverace.vercel.app/shop). Pubblica prima le immagini col "
            "sito o con `catalog.py --write-webapp`.",
            code="design_base_missing",
        )
    return hoplix.validate_design_url(f"{base}/{path.name}")


def plan_listing(
    listing: Listing,
    *,
    settings: Settings,
    product: hoplix.Product,
    ink: str = "light",
    ip_terms: Sequence[str] = (),
    price: float | None = None,
) -> dict[str, Any]:
    """Piano completo per UNA campagna: validazione, prezzo, payload."""
    issues = validate_listing(listing, ip_terms=ip_terms)
    entry: dict[str, Any] = {
        "listing_id": listing.listing_id,
        "ink": ink,
        "design": listing.design,
        "ok": False,
        "issues": list(issues),
        "blocking": issues.blocking(),
    }
    if issues.blocking():
        entry["error"] = "listing non valido: non pubblicherei una campagna con questo testo"
        entry["error_code"] = "invalid_listing"
        return entry

    final_price = round(float(price if price and price > 0 else (listing.price_floor or settings.price_floor)), 2)
    entry["price"] = final_price
    try:
        hoplix.validate_price(final_price, product.base_cost)
    except hoplix.HoplixError as exc:
        entry["error"] = str(exc)
        entry["error_code"] = exc.code
        return entry

    try:
        url = design_url(listing, ink, settings)
    except PublishError as exc:
        entry["error"] = str(exc)
        entry["error_code"] = exc.code
        entry["needs_design_base"] = exc.code == "design_base_missing"
        return entry

    slug = f"{listing.listing_id}-{ink}".lower().replace("_", "-").replace(" ", "-")
    try:
        payload = hoplix.build_campaign_payload(
            name=(listing.design or listing.title)[:80],
            description=listing.description,
            slug=slug,
            front_url=url,
            product=product,
            price=final_price,
            colors=hoplix.INK_COLORS[ink],
            sizes=("S", "M", "L", "XL", "2XL"),
            currency="EUR",
        )
    except hoplix.HoplixError as exc:
        entry["error"] = str(exc)
        entry["error_code"] = exc.code
        return entry

    margin = platforms.net_margin(final_price, platforms.HOPLIX)
    entry.update(
        {
            "ok": True,
            "slug": slug,
            "design_url": url,
            "margin": margin["margin"],
            "margin_pct": margin["margin_pct_of_price"],
            "base_cost": product.base_cost,
            "campaign": payload["campaign"],
        }
    )
    return entry


def dry_run(
    listings: Sequence[Listing],
    *,
    settings: Settings,
    product: hoplix.Product | None = None,
    ip_terms: Sequence[str] = (),
    ledger: Mapping[str, Any] | None = None,
    price: float | None = None,
) -> dict[str, Any]:
    """Piano di tutte le campagne. Nessuna chiamata di scrittura."""
    if product is None:
        # Senza catalogo vero il costo base e' quello documentato: il prezzo si
        # valida comunque, e il margine e' dichiarato come stimato.
        product = hoplix.Product(
            code=DEFAULT_PRODUCT_CODE,
            name="Maglietta unisex",
            colors=("white", "black"),
            sizes=("S", "M", "L", "XL", "XXL"),
            base_cost=platforms.HOPLIX.product_cost_eur,
            printable_area="320x400",
        )
        catalog = "stimato (7,90 EUR IVA incl.)"
    else:
        catalog = f"letto (base {product.base_cost})"

    rows = (ledger or {}).get("listings", {}) if ledger else {}
    inks = INKS if settings.ink_mode == "both" else ("light",)
    entries: list[dict[str, Any]] = []
    for listing in listings:
        for ink in inks:
            key = f"hoplix:{listing.listing_id}:{ink}"
            entry = plan_listing(listing, settings=settings, product=product, ink=ink, ip_terms=ip_terms, price=price)
            already = rows.get(key)
            entry["already_created"] = bool(already)
            if already:
                entry["campaign_id"] = (already or {}).get("campaign_id")
            entries.append(entry)

    blocked = [e for e in entries if not e.get("ok") and not e.get("needs_design_base")]
    return {
        "mode": "dry-run",
        "platform": "hoplix",
        "catalog": catalog,
        "product": product.code,
        "base_cost": product.base_cost,
        "n_campaigns": len(entries),
        "ok": sum(1 for e in entries if e.get("ok")),
        "blocked": len(blocked),
        "needs_design_base": sum(1 for e in entries if e.get("needs_design_base")),
        "already_created": sum(1 for e in entries if e.get("already_created")),
        "campaigns": entries,
    }


def publish_listing(
    entry: Mapping[str, Any],
    *,
    client: hoplix.HoplixClient,
    ledger: dict,
    ledger_path: Path = DEFAULT_LEDGER,
) -> dict[str, Any]:
    """Crea la campagna e la registra. Idempotente sulla chiave del registro."""
    key = f"hoplix:{entry['listing_id']}:{entry['ink']}"
    existing = (ledger.get("listings") or {}).get(key)
    if existing and existing.get("campaign_id"):
        return {**entry, "created": False, "skipped": True, "campaign_id": existing["campaign_id"]}

    payload = {"campaign": entry["campaign"]}
    data = client.create_campaign(payload)
    campaign_id = data.get("campaign_id")
    record = {
        "campaign_id": campaign_id,
        "link": data.get("link"),
        "product_code": payload["campaign"].get("product-code"),
        "price": entry.get("price"),
        "ink": entry.get("ink"),
        "created_at": _now_iso(),
    }
    ledger.setdefault("listings", {})[key] = record
    saved = save_ledger(ledger, ledger_path)
    return {
        **entry,
        "created": True,
        "skipped": False,
        "campaign_id": campaign_id,
        "link": data.get("link"),
        "ledger_saved": saved,
    }


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def format_report(report: Mapping[str, Any]) -> str:
    mode = report.get("mode", "dry-run")
    lines = [f"=== CAMPAGNE HOPLIX — {mode} ==="]
    if mode == "dry-run":
        lines.append(
            f"catalogo: {report.get('catalog')} | campagne da creare: {report.get('n_campaigns')} | "
            f"pronte: {report.get('ok')} | bloccate: {report.get('blocked')} | "
            f"in attesa di URL pubblico: {report.get('needs_design_base')} | "
            f"gia' create: {report.get('already_created')}"
        )
        if report.get("needs_design_base"):
            lines.append(
                "nota: le immagini devono essere online. Pubblica il sito (`catalog.py --write-webapp`), "
                "poi esporta HOPLIX_DESIGN_BASE=https://<sito>/shop"
            )
    else:
        lines.append(
            f"create: {report.get('created')} | saltate: {report.get('skipped')} | errori: {report.get('errors')}"
        )
    lines.append("")
    for entry in report.get("campaigns", []):
        lid = f"{entry.get('listing_id')}[{entry.get('ink')}]"
        if entry.get("skipped"):
            lines.append(f"[SALTATA] {lid}: gia' a registro ({entry.get('campaign_id')})")
        elif entry.get("ok") or entry.get("created"):
            extra = [f"{entry['price']:.2f} EUR"]
            if entry.get("margin") is not None:
                extra.append(f"margine {entry['margin']:.2f} ({entry['margin_pct']:.0f}%)")
            extra.append(str(entry.get("slug")))
            lines.append(f"[OK] {lid}: {' | '.join(extra)}")
            if entry.get("link"):
                lines.append(f"     link: {entry['link']}")
            if entry.get("campaign_id"):
                lines.append(f"     campaign_id: {entry['campaign_id']}")
        elif entry.get("needs_design_base"):
            lines.append(f"[ATTESA URL] {lid}: testi e file validati, manca HOPLIX_DESIGN_BASE")
        else:
            lines.append(f"[BLOCCATA] {lid}: {entry.get('error') or 'non valida'}")
            for issue in entry.get("blocking") or []:
                lines.append(f"     - {issue}")
    return "\n".join(lines)


def check(settings: Settings, client: hoplix.HoplixClient) -> dict[str, Any]:
    product = client.product(DEFAULT_PRODUCT_CODE)
    price = settings.price_floor
    margin = platforms.net_margin(price, platforms.HOPLIX)
    return {
        "product": product.code,
        "base_cost": product.base_cost,
        "sizes": len(product.sizes),
        "colors": len(product.colors),
        "printable_area": product.printable_area,
        "test_mode": settings.test_mode,
        "price_floor": price,
        "margin_at_floor": margin["margin"],
        "margin_pct": margin["margin_pct_of_price"],
        "price_ok": product.base_cost is not None and price > product.base_cost,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Crea le campagne Hoplix (dry-run di default).")
    parser.add_argument("--listings", default=str(DEFAULT_LISTINGS), help="CSV dei listing")
    parser.add_argument("--limit", type=int, default=0, help="max listing (0 = tutti)")
    parser.add_argument("--only", default="", help="solo questi listing_id (virgole)")
    parser.add_argument("--ink", default="", choices=["", "light", "dark", "both"], help="quali campagne")
    parser.add_argument("--price", type=float, default=None, help="prezzo EUR (default: price_floor)")
    parser.add_argument("--live", action="store_true", help="crea davvero le campagne")
    parser.add_argument("--dry-run", action="store_true", help="forza il dry-run")
    parser.add_argument("--check", action="store_true", help="verifica chiavi, costo base e margine")
    parser.add_argument("--json", action="store_true", help="output JSON")
    parser.add_argument("--report", default="", help="scrive il report JSON qui")
    parser.add_argument("--ip-terms", default=str(DEFAULT_IP_TERMS), help="termini protetti (0 = disattiva)")
    parser.add_argument("--ledger", default=str(DEFAULT_LEDGER), help="registro di pubblicazione")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    if args.ink:
        settings = Settings(**{**settings.__dict__, "ink_mode": args.ink})
    live = args.live and not args.dry_run

    client: hoplix.HoplixClient | None = None
    if settings.api_key and settings.secret:
        try:
            client = hoplix.HoplixClient(
                api_key=settings.api_key,
                secret=settings.secret,
                test_mode=settings.test_mode,
            )
        except hoplix.HoplixError as exc:
            print(str(exc), file=sys.stderr)
            return 2

    if args.check:
        if client is None:
            print("HOPLIX_API_KEY / HOPLIX_SECRET mancanti: impossibile leggere il catalogo.", file=sys.stderr)
            return 2
        try:
            info = check(settings, client)
        except hoplix.HoplixError as exc:
            print(f"errore Hoplix: {exc}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(info, indent=2, ensure_ascii=False))
        else:
            print("=== VERIFICA HOPLIX ===")
            print(f"  prodotto: {info['product']} | costo base: {info['base_cost']} EUR")
            print(f"  area di stampa: {info['printable_area']} | taglie: {info['sizes']} | colori: {info['colors']}")
            print(f"  test mode: {'SI' if info['test_mode'] else 'NO (attenzione: ordini reali)'}")
            print(f"  prezzo {info['price_floor']:.2f} -> margine {info['margin_at_floor']:.2f} EUR ({info['margin_pct']:.0f}%)")
        return 0 if info["price_ok"] else 1

    if live and client is None:
        print("HOPLIX_API_KEY / HOPLIX_SECRET mancanti: nessuna campagna creata.", file=sys.stderr)
        return 2

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

    product: hoplix.Product | None = None
    if client is not None:
        try:
            product = client.product(DEFAULT_PRODUCT_CODE)
        except hoplix.HoplixError as exc:
            # Catalogo non leggibile: in dry-run si continua con il costo
            # documentato; in live NO, perche' il prezzo andrebbe validato
            # contro il costo vero.
            if live:
                print(f"catalogo non leggibile: {exc}", file=sys.stderr)
                return 1
            product = None

    ledger_path = Path(args.ledger)
    ledger = load_ledger(ledger_path)
    dry = dry_run(
        listings,
        settings=settings,
        product=product,
        ip_terms=ip_terms,
        ledger=ledger,
        price=args.price,
    )

    if not live:
        report = dry
    else:
        assert client is not None
        created = skipped = errors = 0
        entries: list[dict[str, Any]] = []
        for entry in dry["campaigns"]:
            if not entry.get("ok"):
                errors += 1
                entries.append(entry)
                continue
            try:
                result = publish_listing(entry, client=client, ledger=ledger, ledger_path=ledger_path)
            except (hoplix.HoplixError, PublishError) as exc:
                errors += 1
                entries.append({**entry, "ok": False, "error": str(exc), "error_code": getattr(exc, "code", "error")})
                continue
            if result.get("created"):
                created += 1
            else:
                skipped += 1
            entries.append(result)
        report = {
            "mode": "live",
            "platform": "hoplix",
            "created": created,
            "skipped": skipped,
            "errors": errors,
            "campaigns": entries,
        }

    if args.report:
        try:
            Path(args.report).parent.mkdir(parents=True, exist_ok=True)
            Path(args.report).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            print(f"report non scritto: {exc}", file=sys.stderr)

    print(json.dumps(report, indent=2, ensure_ascii=False) if args.json else format_report(report))
    if report.get("mode") == "dry-run":
        return 0 if not report.get("blocked") else 1
    return 0 if report.get("errors", 0) == 0 else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
