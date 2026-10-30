"""Doctor — controllo pre-volo del negozio, prima di pubblicare o spendere.

PERCHE' UN COMANDO E NON UNA CHECKLIST
--------------------------------------
Prima di aprire un negozio e incollare cinque schede ci sono una ventina di
cose che possono essere sbagliate in silenzio: un PNG senza trasparenza, un
disegno che sparisce sul capo nero, un termine protetto nel titolo, un prezzo
che lascia meno margine del previsto, un'immagine del sito da 300 KB. Nessuna
di queste produce un errore evidente: si scoprono con un reso, una
penalizzazione o un margine che non c'e'.

Questo modulo le controlla TUTTE, in un comando, e dice per ognuna se e'
`ok`, `warn`, `error` o `pending` (bloccata su un passo del proprietario:
credenziali o negozio).

COSA NON FA
-----------
- Non chiama l'API Hoplix senza chiavi e non crea nulla: senza credenziali le
  sezioni che ne hanno bisogno risultano `pending`, non `error`.
- Non spende: nessun ordine, nessuna campagna, nessun annuncio.
- Non inventa numeri: il margine esce dalle funzioni di produzione
  (`catalog.build_rows` -> `platforms`), non da costanti ricopiate qui.

USO
---
    ../venv/bin/python doctor.py                 # tutto offline
    ../venv/bin/python doctor.py --json          # per script
    ../venv/bin/python doctor.py --base https://sito.example   # controlla le URL pubbliche
    ../venv/bin/python doctor.py --base-cost 7.50              # margine col costo vero

Exit code: 0 = nessun errore (avvisi e pending ammessi), 1 = almeno un errore.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Iterable, Sequence

ROOT = Path(__file__).resolve().parent
WEBAPP = ROOT.parent / "webapp"
MANIFEST = WEBAPP / "content" / "shop.ts"
CARDS_DIR = WEBAPP / "public" / "shop" / "card"
SVG_DIR = ROOT.parent / "redbubble-tech-gift-2026-work" / "output" / "svg"

# Soglie dichiarate: se cambiano, cambiano qui e i test le leggono da qui.
MIN_MARGIN_EUR = 8.0
MIN_IMAGE_HEIGHT = 4500  # 3600x4800 e' lo standard del pacchetto; sotto, DPI bassi
MAX_CARD_BYTES = 120_000  # una card oltre 120 KB pesa su 4G
LEGIBLE_CONTRAST = 4.5  # WCAG AA: il contrasto sotto cui un testo non si legge
MIN_LEGIBLE_SHARE = 0.05  # sotto, quasi nulla del disegno si legge sul capo
RISK_LEGIBLE_SHARE = 0.12
MIN_MEAN_CONTRAST = 1.35  # disegno tonalmente identico al tessuto


# ---------------------------------------------------------------------------
# colore
# ---------------------------------------------------------------------------


def _srgb(u: float) -> float:
    return u / 12.92 if u <= 0.03928 else ((u + 0.055) / 1.055) ** 2.4


def luminance(rgb: Sequence[int]) -> float:
    r, g, b = (_srgb(c / 255.0) for c in rgb[:3])
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: Sequence[int], b: Sequence[int]) -> float:
    """Rapporto di contrasto WCAG fra due colori (1.0 = identici)."""
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


# Un PNG di stampa 3600x4800 costa ~1,5-2s da decodificare: la stessa immagine
# viene misurata piu' volte nella vita di un processo (il doctor gira piu' volte
# nei test, e in produzione una passata per capo). La memo e' sulla FIRMA del
# file (percorso + mtime + dimensione), quindi un'immagine rigenerata NON viene
# misurata sul valore vecchio.
_INK_CACHE: dict[tuple[Any, ...], dict[str, float]] = {}
_INK_CACHE_MAX = 64

# La memo in memoria non basta per chi lancia il doctor come COMANDO: ogni
# processo ripartirebbe da zero e la prima passata costerebbe ~30s ogni volta
# (prima di ogni pubblicazione). La stessa firma viene quindi salvata su disco
# in un file piccolo accanto al progetto (`DOCTOR_CACHE`, default
# `.doctor_cache.json`). Il valore in cache e' solo una MISURA gia' fatta su
# quel preciso file: se la cache e' corrotta o il file e' cambiato si ricalcola,
# quindi il peggio che puo' capitare e' perdere tempo, mai leggere un numero
# sbagliato.
_DISK_CACHE: dict[str, dict[str, float]] | None = None
_DISK_CACHE_MAX = 200
_DISK_CACHE_VERSION = 1


def cache_path() -> Path | None:
    """Dove vive la cache su disco. `DOCTOR_CACHE=0` la spegne."""
    raw = (os.getenv("DOCTOR_CACHE") or "").strip()
    if raw.lower() in ("0", "off", "no", "false", "disabled"):
        return None
    return Path(raw) if raw else ROOT / ".doctor_cache.json"


def _disk_load() -> dict[str, dict[str, float]]:
    """Legge la cache (una volta per processo). File rotto = cache vuota."""
    global _DISK_CACHE
    if _DISK_CACHE is not None:
        return _DISK_CACHE
    _DISK_CACHE = {}
    path = cache_path()
    if path is None or not path.exists():
        return _DISK_CACHE
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        entries = payload.get("entries") if isinstance(payload, dict) else None
        if isinstance(entries, dict):
            _DISK_CACHE = {
                str(k): dict(v)
                for k, v in entries.items()
                if isinstance(v, dict) and "legible_share" in v
            }
    except Exception:
        _DISK_CACHE = {}  # ricalcolare costa secondi, un numero sbagliato costa un cliente
    return _DISK_CACHE


def _disk_store(key: str, metrics: dict[str, float]) -> None:
    """Salva una misura (scrittura atomica, fail-safe: mai un'eccezione)."""
    path = cache_path()
    if path is None:
        return
    entries = _disk_load()
    entries[key] = dict(metrics)
    if len(entries) > _DISK_CACHE_MAX:
        entries = dict(list(entries.items())[-_DISK_CACHE_MAX:])
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps({"version": _DISK_CACHE_VERSION, "entries": entries}, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(tmp, path)
    except Exception:
        return


def _signature_key(key: tuple[Any, ...]) -> str:
    return hashlib.sha1(repr(key).encode("utf-8")).hexdigest()


def reset_ink_cache(*, drop_disk: bool = False) -> None:
    """Svuota la memo in memoria e, se richiesto, la cache su disco."""
    global _DISK_CACHE
    _INK_CACHE.clear()
    _DISK_CACHE = None
    if drop_disk:
        path = cache_path()
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass


def ink_metrics(path: Path, garment: Sequence[int], *, step: int = 5) -> dict[str, float]:
    """Come si comporta il disegno sul capo indicato.

    Due misure, perche' una sola non basta su un disegno illustrato:

    * `legible_share` — quota di pixel con contrasto >= 4.5:1 (WCAG AA). E' la
      parte che si legge davvero: se e' sotto il 5% il disegno e' svanito sul
      tessuto, e il cliente riceve una maglietta quasi vuota.
    * `mean_contrast` — contrasto medio. Distingue "pochi elementi ad alto
      contrasto su fondo simile" (una grafica sobria, legittima) da "tutto
      dello stesso tono" (errore di abbinamento inchiostro/capo).

    NON si usa il colore mediano: su un disegno con blocchi colorati e testo
    scuro la mediana descrive il blocco, non la leggibilita'.
    """
    import numpy as np
    from PIL import Image

    file = Path(path)
    stat = file.stat()
    key = (str(file.resolve()), stat.st_mtime_ns, stat.st_size, tuple(garment[:3]), step)
    cached = _INK_CACHE.get(key)
    if cached is not None:
        return dict(cached)
    disk_key = _signature_key(key)
    on_disk = _disk_load().get(disk_key)
    if on_disk is not None:
        _INK_CACHE[key] = dict(on_disk)
        return dict(on_disk)

    with Image.open(file) as im:
        # Un file di stampa e' 3600x4800: misurarne OGNI pixel costa secondi per
        # immagine (dieci immagini x ogni `doctor.py` = un minuto di attesa prima
        # di pubblicare). La quota di pixel leggibili e' una MEDIA, quindi un
        # rimpicciolimento a 480px la stima con errore ~1 punto percentuale —
        # il test sul disegno misto ritrova il 30% esatto.
        scaled = im.convert("RGBA")
        scaled.thumbnail((480, 640))
        rgba = np.asarray(scaled)
    pixels = rgba[rgba[:, :, 3] >= 250][::step, :3]
    if not len(pixels):
        return {"legible_share": 0.0, "mean_contrast": 1.0, "max_contrast": 1.0}
    sample = pixels[:: max(1, len(pixels) // 4000)]
    lg = luminance(garment)
    ratios = []
    for px in sample:
        top, bottom = sorted((luminance(tuple(int(v) for v in px)), lg), reverse=True)
        ratios.append((top + 0.05) / (bottom + 0.05))
    ratios.sort()
    legible = sum(1 for r in ratios if r >= LEGIBLE_CONTRAST) / len(ratios)
    metrics = {
        "legible_share": legible,
        "mean_contrast": sum(ratios) / len(ratios),
        "max_contrast": ratios[-1],
    }
    if len(_INK_CACHE) >= _INK_CACHE_MAX:
        _INK_CACHE.clear()
    _INK_CACHE[key] = metrics
    _disk_store(disk_key, metrics)
    return dict(metrics)


# ---------------------------------------------------------------------------
# sezioni di controllo
# ---------------------------------------------------------------------------


def _section(name: str, level: str, detail: str, **data: Any) -> dict[str, Any]:
    return {"name": name, "level": level, "detail": detail, "data": data}


def check_files(listings: Sequence[Any], rows: Sequence[Any]) -> list[dict[str, Any]]:
    """Listings, immagini, dimensioni, area di stampa."""
    from PIL import Image

    out: list[dict[str, Any]] = []
    out.append(
        _section(
            "schede",
            "ok" if len(listings) == 5 else "warn",
            f"{len(listings)} schede nel CSV (attese 5 per il primo lancio)",
            count=len(listings),
        )
    )
    problems: list[str] = []
    for listing in listings:
        for label, path in (("chiaro", listing.image_light), ("scuro", listing.image_dark)):
            if path is None:
                problems.append(f"{listing.listing_id}: manca l'immagine per capo {label}")
                continue
            if not Path(path).exists():
                problems.append(f"{listing.listing_id}: file assente {Path(path).name}")
                continue
            with Image.open(path) as im:
                mode, size = im.mode, im.size
            if "A" not in mode:
                problems.append(f"{Path(path).name}: nessun canale alpha (fondo non trasparente)")
            if size[1] < MIN_IMAGE_HEIGHT:
                problems.append(f"{Path(path).name}: {size[0]}x{size[1]} sotto {MIN_IMAGE_HEIGHT}px di altezza")
    out.append(
        _section(
            "immagini",
            "error" if problems else "ok",
            "; ".join(problems) if problems else f"10 file validi (alpha + altezza >= {MIN_IMAGE_HEIGHT}px)",
            problems=problems,
        )
    )

    warnings = [f"{getattr(r, 'listing_id', '?')}/{getattr(r, 'ink', '?')}: {r.area_warning}" for r in rows if getattr(r, "area_warning", None)]
    out.append(
        _section(
            "area_di_stampa",
            "warn" if warnings else "ok",
            f"{len(warnings)} schede con avviso di proporzione" if warnings else "tutte le immagini entrano nell'area 320x400 mm",
            warnings=warnings,
        )
    )
    return out


def check_garments(listings: Sequence[Any]) -> list[dict[str, Any]]:
    """Il disegno si vede sul capo su cui verra' stampato?"""
    results: list[dict[str, Any]] = []
    bad: list[str] = []
    risky: list[str] = []
    for listing in listings:
        for ink, path, garment, garment_name in (
            ("light", listing.image_light, (255, 255, 255), "bianco"),
            ("dark", listing.image_dark, (0, 0, 0), "nero"),
        ):
            if path is None or not Path(path).exists():
                continue
            metrics = ink_metrics(Path(path), garment)
            entry = {
                "listing_id": listing.listing_id,
                "ink": ink,
                "file": Path(path).name,
                "garment": garment_name,
                "legible_pct": round(metrics["legible_share"] * 100, 1),
                "mean_contrast": round(metrics["mean_contrast"], 2),
            }
            results.append(entry)
            label = (
                f"{listing.listing_id}/{ink} ({Path(path).name} su {garment_name}): "
                f"solo {entry['legible_pct']}% leggibile a {LEGIBLE_CONTRAST}:1"
            )
            if metrics["legible_share"] < MIN_LEGIBLE_SHARE or metrics["mean_contrast"] < MIN_MEAN_CONTRAST:
                bad.append(label)
            elif metrics["legible_share"] < RISK_LEGIBLE_SHARE:
                risky.append(label)
    level = "error" if bad else ("warn" if risky else "ok")
    detail = (
        "disegno quasi invisibile sul capo abbinato: " + "; ".join(bad)
        if bad
        else ("leggibilita' al limite: " + "; ".join(risky) if risky else "ogni disegno resta leggibile sul capo a cui e' abbinato")
    )
    return [_section("abbinamento_capo", level, detail, rows=results)]


def check_texts(listings: Sequence[Any]) -> list[dict[str, Any]]:
    """Titoli, tag, descrizioni e termini protetti."""
    import publish as P

    terms = P.load_ip_terms()
    blocking: list[str] = []
    soft: list[str] = []
    for listing in listings:
        issues = P.validate_listing(listing, ip_terms=terms)
        blocking.extend(f"{listing.listing_id}: {i}" for i in issues.blocking())
        soft.extend(f"{listing.listing_id}: {i}" for i in getattr(issues, "warnings", []) or [])
        if len(listing.title) > P.ETSY_TITLE_MAX:
            blocking.append(f"{listing.listing_id}: titolo oltre {P.ETSY_TITLE_MAX} caratteri")
        if len(listing.tags) != P.ETSY_TAGS_MAX:
            blocking.append(f"{listing.listing_id}: {len(listing.tags)} tag (attesi {P.ETSY_TAGS_MAX})")
        if len(listing.description) < 200:
            soft.append(f"{listing.listing_id}: descrizione corta ({len(listing.description)} caratteri)")
    out = [
        _section(
            "testi",
            "error" if blocking else ("warn" if soft else "ok"),
            "; ".join(blocking or soft) if (blocking or soft) else f"titoli, {P.ETSY_TAGS_MAX} tag, descrizioni e limiti rispettati",
            blocking=blocking,
            warnings=soft,
        )
    ]
    out.append(
        _section(
            "marchi",
            "ok" if terms else "warn",
            f"{len(terms)} termini protetti controllati su titolo, descrizione e tag"
            if terms
            else "lista dei termini protetti non trovata: il controllo marchi e' DISATTIVATO",
            terms=len(terms),
        )
    )
    return out


def _design_slug(image: Path) -> str:
    """`tee-dark-404-regalo-non-trovato.png` -> `404-regalo-non-trovato`."""
    stem = Path(image).stem
    for prefix in ("tee-dark-", "tee-", "sticker-", "preview-"):
        if stem.startswith(prefix):
            return stem[len(prefix) :]
    return stem


def check_origin(listings: Sequence[Any]) -> list[dict[str, Any]]:
    """I disegni nascono da codice (vettoriale) o sono file di terzi?

    La prova di originalita' non e' una dichiarazione: e' la presenza del
    sorgente SVG accanto al PNG esportato. Un design senza vettoriale e' un
    bitmap di provenienza ignota, e quello si controlla prima di vendere.
    """
    missing: list[str] = []
    vectors = 0
    for listing in listings:
        slug = _design_slug(Path(listing.image_light))
        svgs = sorted(SVG_DIR.glob(f"{slug}-*.svg")) if SVG_DIR.exists() else []
        if not svgs:
            missing.append(listing.listing_id)
        vectors += len(svgs)
    level = "ok" if not missing else "error"
    detail = (
        f"{vectors} sorgenti vettoriali per 5 design: ogni grafica nasce da codice, nessun bitmap di terzi"
        if not missing
        else "schede senza sorgente vettoriale (provenienza non dimostrabile): " + ", ".join(missing)
    )
    return [_section("originalita", level, detail, vectors=vectors, missing=missing)]


def check_economics(rows: Sequence[Any], *, base_cost: float | None = None) -> list[dict[str, Any]]:
    """Prezzo, costo e margine — dalle funzioni di produzione, non da costanti."""
    import platforms

    if not rows:
        return [_section("economia", "error", "nessuna riga di catalogo: impossibile calcolare il margine")]
    prices = sorted({round(float(getattr(r, "price", 0) or 0), 2) for r in rows})
    margins = sorted({round(float(getattr(r, "margin", 0) or 0), 2) for r in rows})
    cost = base_cost if base_cost is not None else float(getattr(rows[0], "base_cost", 0) or 0)
    price = prices[0]
    breakdown = platforms.net_margin(price, platforms.HOPLIX)
    worst = min(margins)
    level = "ok" if worst >= MIN_MARGIN_EUR else "error"
    detail = (
        f"prezzo {price:.2f} EUR, costo {cost:.2f} (IVA incl.), margine {worst:.2f} EUR "
        f"({breakdown['margin_pct_of_price']:.0f}% del prezzo)"
    )
    if worst < MIN_MARGIN_EUR:
        detail += f" — SOTTO la soglia di {MIN_MARGIN_EUR:.2f} EUR"
    return [
        _section(
            "economia",
            level,
            detail,
            price=price,
            base_cost=round(cost, 2),
            margins=margins,
            min_margin=MIN_MARGIN_EUR,
            breakdown=breakdown,
        )
    ]


def check_site(listings: Sequence[Any], *, base_url: str = "") -> list[dict[str, Any]]:
    """Manifest, card ottimizzate e (se richiesto) le URL pubbliche."""
    out: list[dict[str, Any]] = []
    manifest_ok = MANIFEST.exists()
    ids: list[str] = []
    if manifest_ok:
        text = MANIFEST.read_text(encoding="utf-8")
        ids = [listing.listing_id for listing in listings if listing.listing_id in text]
    out.append(
        _section(
            "manifest_sito",
            "ok" if manifest_ok and len(ids) == len(listings) else "warn",
            f"{len(ids)}/{len(listings)} schede presenti in {MANIFEST.name}"
            if manifest_ok
            else f"manifest assente: esegui `python catalog.py --write-webapp`",
            ids=ids,
        )
    )

    heavy: list[str] = []
    missing: list[str] = []
    if CARDS_DIR.exists():
        for path in sorted(CARDS_DIR.glob("*.webp")) + sorted(CARDS_DIR.glob("*.png")):
            size = path.stat().st_size
            if size > MAX_CARD_BYTES:
                heavy.append(f"{path.name} {size // 1024} KB")
        if not heavy and not any(CARDS_DIR.glob("*")):
            missing.append("cartella immagini vuota")
    else:
        missing.append(f"cartella assente: {CARDS_DIR}")
    out.append(
        _section(
            "peso_immagini",
            "warn" if (heavy or missing) else "ok",
            "; ".join(heavy + missing)
            if (heavy or missing)
            else f"nessuna card sopra {MAX_CARD_BYTES // 1000} KB (le WebP sono 16-19 KB)",
            heavy=heavy,
            missing=missing,
        )
    )

    if not base_url:
        out.append(_section("url_pubbliche", "pending", "non controllate: passa --base https://... per verificarle"))
        return out

    ok, bad = [], []
    for name in ["/shop"] + [f"/shop/card/{item.listing_id}-light.webp" for item in listings]:
        url = base_url.rstrip("/") + name
        try:
            import urllib.request

            request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "doctor/1.0"})
            with urllib.request.urlopen(request, timeout=10) as resp:  # noqa: S310 - URL fornita a mano
                status = getattr(resp, "status", 200)
            (ok if 200 <= status < 400 else bad).append(f"{name} {status}")
        except Exception as exc:  # pragma: no cover - dipende dalla rete
            bad.append(f"{name} ({type(exc).__name__})")
    out.append(
        _section(
            "url_pubbliche",
            "error" if bad else "ok",
            "; ".join(bad) if bad else f"{len(ok)} URL raggiungibili",
            ok=ok,
            bad=bad,
        )
    )
    return out


def check_api(*, offline: bool = False) -> list[dict[str, Any]]:
    """Credenziali Hoplix e costo base VERO (sola lettura)."""
    import hoplix

    key = (os.getenv("HOPLIX_API_KEY") or "").strip()
    secret = (os.getenv("HOPLIX_SECRET") or "").strip()
    if offline:
        return [_section("api_hoplix", "pending", "controllo API saltato (--offline)")]
    if not (key and secret):
        return [
            _section(
                "api_hoplix",
                "pending",
                "credenziali assenti: e' il Passo 3 di GO_LIVE.md (HOPLIX_API_KEY + HOPLIX_SECRET nel .env)",
                has_key=False,
            )
        ]
    try:
        client = hoplix.HoplixClient.from_env()
        products = client.product_list()
        first = next(iter(products.values()), None)
        cost = float(getattr(first, "base_cost", 0) or 0)
    except Exception as exc:  # pragma: no cover - dipende dalla rete
        return [_section("api_hoplix", "error", f"chiamata fallita: {type(exc).__name__}: {exc}")]
    return [
        _section(
            "api_hoplix",
            "ok" if cost > 0 else "warn",
            f"{len(products)} prodotti a catalogo, costo base maglietta {cost:.2f} EUR",
            products=len(products),
            base_cost=cost,
            has_key=True,
        )
    ]


def check_orders() -> list[dict[str, Any]]:
    """Ordini e tracking (sola lettura)."""
    import hoplix

    if not ((os.getenv("HOPLIX_API_KEY") or "").strip() and (os.getenv("HOPLIX_SECRET") or "").strip()):
        return [_section("ordini", "pending", "credenziali assenti: nessun ordine da leggere finche' lo store non e' attivo")]
    try:
        client = hoplix.HoplixClient.from_env()
        rows = client.list_orders()
        report = hoplix.orders_report(rows)
    except Exception as exc:  # pragma: no cover - dipende dalla rete
        return [_section("ordini", "error", f"lettura ordini fallita: {type(exc).__name__}: {exc}")]
    return [
        _section(
            "ordini",
            "ok",
            f"{report.get('total', 0)} ordini, {report.get('with_tracking', 0)} con tracking",
            report=report,
        )
    ]


# ---------------------------------------------------------------------------
# orchestrazione
# ---------------------------------------------------------------------------


def run(
    *,
    listings_path: Path | None = None,
    base_url: str = "",
    base_cost: float | None = None,
    offline: bool = False,
) -> dict[str, Any]:
    """Esegue tutti i controlli. Non solleva: un errore diventa una sezione."""
    import catalog as C
    import platforms
    import publish as P

    sections: list[dict[str, Any]] = []
    try:
        listings = P.read_listings(listings_path or P.DEFAULT_LISTINGS)
    except Exception as exc:
        return {
            "sections": [_section("csv", "error", f"lettura del CSV fallita: {exc}")],
            "errors": 1,
            "warnings": 0,
            "pending": 0,
            "ok": False,
        }

    api_sections = check_api(offline=offline)
    real_cost = base_cost
    for section in api_sections:
        value = section.get("data", {}).get("base_cost")
        if section["level"] == "ok" and value:
            real_cost = float(value)

    try:
        rows = C.build_rows(
            listings,
            base_cost=real_cost if real_cost else platforms.HOPLIX.product_cost_eur,
            include_dark=True,
        )
    except Exception as exc:
        rows = []
        sections.append(_section("catalogo", "error", f"costruzione del catalogo fallita: {exc}"))

    sections.extend(check_files(listings, rows))
    sections.extend(check_garments(listings))
    sections.extend(check_texts(listings))
    sections.extend(check_origin(listings))
    sections.extend(check_economics(rows, base_cost=real_cost))
    sections.extend(check_site(listings, base_url=base_url))
    sections.extend(api_sections)
    sections.extend(check_orders())

    errors = sum(1 for s in sections if s["level"] == "error")
    warnings = sum(1 for s in sections if s["level"] == "warn")
    pending = sum(1 for s in sections if s["level"] == "pending")
    return {"sections": sections, "errors": errors, "warnings": warnings, "pending": pending, "ok": errors == 0}


MARKS = {"ok": "[OK]", "warn": "[AVVISO]", "error": "[ERRORE]", "pending": "[ATTESA]"}


def format_report(report: dict[str, Any]) -> str:
    lines = ["=== DOCTOR — controllo pre-volo del negozio ==="]
    for section in report["sections"]:
        lines.append(f"{MARKS.get(section['level'], '[?]'):9s} {section['name']}: {section['detail']}")
    verdict = "PRONTO" if report["ok"] and not report["pending"] else ("PRONTO (con attese)" if report["ok"] else "NON PRONTO")
    lines.append("")
    lines.append(f"Esito: {verdict} — {report['errors']} errori, {report['warnings']} avvisi, {report['pending']} attese")
    if report["pending"]:
        lines.append("Le righe [ATTESA] non sono errori: dipendono da un passo che spetta al proprietario (vedi GO_LIVE.md).")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Controllo pre-volo del negozio (offline di default).")
    parser.add_argument("--listings", default="", help="CSV dei listing (default: quello del progetto)")
    parser.add_argument("--base", default="", help="base URL pubblica del sito, per verificare /shop e le immagini")
    parser.add_argument("--base-cost", type=float, default=None, help="costo base reale (EUR, IVA incl.)")
    parser.add_argument("--offline", action="store_true", help="salta anche i controlli che usano l'API")
    parser.add_argument("--json", action="store_true", help="output JSON")
    args = parser.parse_args(argv)

    report = run(
        listings_path=Path(args.listings) if args.listings else None,
        base_url=args.base,
        base_cost=args.base_cost,
        offline=args.offline,
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        print(format_report(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
