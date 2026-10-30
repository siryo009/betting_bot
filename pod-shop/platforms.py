"""Confronto Printify Pop-Up Store vs Hoplix — numeri e fonti, non impressioni.

PERCHE' QUESTO MODULO ESISTE
----------------------------
La scelta della piattaforma cambia tre cose che non si vedono nel prezzo di
vetrina: chi incassa (e quindi chi ha le commissioni), chi fattura (e quindi chi
ha gli adempimenti IVA), e cosa l'API sa fare da sola. Un confronto a "questa
costa meno" senza queste tre voci produce una decisione che sembra ragionata e
non lo e'.

LE FONTI, UNA PER CAMPO
-----------------------
Ogni numero porta con se' la sua provenienza (`source`) e se e' un DATO o una
STIMA (`estimate`). Un margine costruito su una stima non e' un margine
verificato: il report lo dice, invece di mescolare le due cose.

MARGINE: COME VIENE CALCOLATO (e perche' cosi')
----------------------------------------------
    ricavo_netto = prezzo / (1 + IVA)
    costo        = costo_prodotto (IVA inclusa, non detraibile nel regime
                   forfettario: e' la cifra che esce davvero)
    commissioni  = solo se incassiamo NOI (Printify: Stripe)
    margine      = ricavo_netto - costo - commissioni

L'IVA viene tolta dal ricavo in ENTRAMBI i casi, anche quando e' la piattaforma
a fatturare: cosi' il confronto non attribuisce a Hoplix un vantaggio di
MARGINE che in realta' e' un vantaggio di ADEMPIMENTI (e di cassa). Il vantaggio
c'e', ma e' di un'altra natura e viene dichiarato separatamente.

USO
---
    venv/bin/python platforms.py [--price 24.90] [--json]

Sola lettura: nessuna rete, nessuna credenziale, nessuna spesa.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

# --- Aliquote e default -----------------------------------------------------
VAT_IT = 0.22
DEFAULT_PRICE_EUR = 24.90
SHIPPING_IT_HOPLIX = 4.95
SHIPPING_EU_PRINTIFY_FROM = 4.12

# Stripe Italia (carte SEE): 1,5% + 0,25 EUR per transazione. Documentato qui
# perche' il modello di costo di Printify lo richiede: la' incassa il venditore.
STRIPE_PCT = 0.015
STRIPE_FIXED_EUR = 0.25


@dataclass(frozen=True)
class Platform:
    """Una piattaforma con i suoi costi e le sue capacita' REALI.

    I campi booleani non sono opinioni: descrivono cosa la documentazione
    ufficiale dichiara possibile. `notes` serve a non dimenticare i limiti
    quando fra sei mesi si rilegge il confronto.
    """

    key: str
    label: str
    # costi (EUR)
    product_cost_eur: float
    product_cost_source: str
    product_cost_estimate: bool
    # pagamenti e fisco
    collects_payment: bool          # incassa e fattura la piattaforma
    seller_handles_vat: bool        # IVA/OSS a carico nostro
    payment_fee_pct: float
    payment_fee_fixed_eur: float
    # automazione (documentazione ufficiale)
    api_orders: bool
    api_publishes_to_store: bool
    api_test_mode: bool
    # tempi e catalogo
    production_days: tuple[int, int]
    delivery_it_days: tuple[int, int]
    products: int
    # struttura del negozio
    monthly_fee_eur: float
    listing_fee_eur: float
    custom_domain: bool
    sources: tuple[str, ...]
    notes: tuple[str, ...]

    # -- derivati ----------------------------------------------------------
    def total_days(self) -> int:
        return max(self.production_days) + max(self.delivery_it_days)


# ---------------------------------------------------------------------------
# Le due piattaforme, come sono state MISURATE il 28/10/2026
# ---------------------------------------------------------------------------

HOPLIX = Platform(
    key="hoplix",
    label="Hoplix (store gratuito, Italia)",
    # API v1: maglietta-unisex -> "base-cost": "7.50" (stampa fronte inclusa).
    # La pagina di confronto ufficiale cita 7,90 IVA inclusa: si usa la cifra
    # PEGGIORE (7,90) per non costruire un margine ottimista.
    product_cost_eur=7.90,
    product_cost_source="hoplix.com/it/hoplix-vs-printify (7,90 EUR IVA incl.) + API product-list base-cost 7.50",
    product_cost_estimate=False,
    collects_payment=True,
    seller_handles_vat=False,
    payment_fee_pct=0.0,
    payment_fee_fixed_eur=0.0,
    api_orders=True,
    api_publishes_to_store=False,   # l'API crea CAMPAGNE, non prodotti dello store
    api_test_mode=True,
    production_days=(2, 5),
    delivery_it_days=(1, 3),
    products=150,
    monthly_fee_eur=0.0,
    listing_fee_eur=0.0,
    custom_domain=True,
    sources=(
        "hoplix.com/it/store-creation (store gratuito, pagamenti e fatture gestiti da Hoplix)",
        "hoplix.shop/api/v1/documentation (API v1.1, test mode ottobre 2026)",
        "hoplix.com/en/shipping (Italia 4,95 EUR, 1-3 giorni)",
        "hoplix.com/it/hoplix-vs-printify (confronto ufficiale, 5 ottobre 2026)",
    ),
    notes=(
        "L'API crea campagne (pagine di vendita) e ordini: i prodotti dello STORE "
        "si inseriscono dall'interfaccia (5 voci, pochi minuti).",
        "Test mode su ogni endpoint ordini: 'test': true non produce, non addebita e "
        "non spedisce — verificabile senza spendere.",
        "Hoplix incassa e fattura le vendite; noi fatturiamo solo il profitto.",
    ),
)

PRINTIFY = Platform(
    key="printify",
    label="Printify Pop-Up Store",
    # I fornitori EU della t-shirt stanno fra 9 e 11 EUR: valore centrale,
    # dichiarato come STIMA finche' il catalogo vero non lo conferma.
    product_cost_eur=10.50,
    product_cost_source="fornitori EU stampa t-shirt (Francia/Germania/Rep. Ceca): stima 9-11 EUR",
    product_cost_estimate=True,
    collects_payment=False,
    seller_handles_vat=True,
    payment_fee_pct=STRIPE_PCT,
    payment_fee_fixed_eur=STRIPE_FIXED_EUR,
    api_orders=True,
    api_publishes_to_store=False,   # publish non agisce senza canale supportato
    api_test_mode=False,
    production_days=(2, 7),
    delivery_it_days=(2, 5),
    products=2000,
    monthly_fee_eur=0.0,
    listing_fee_eur=0.0,
    custom_domain=True,
    sources=(
        "printify.com/pop-up-store (store gratuito, nessuna fee di listing o transazione)",
        "printify.com/blog/printify-pop-up-store (verifica Stripe una tantum)",
        "developers.printify.com (publish: 'does not implement any publishing action "
        "unless the Printify store is connected to one of our other supported sales "
        "channel integrations')",
        "hoplix.com/it/hoplix-vs-printify (spedizioni e tempi Printify EU, 4,12 EUR da)",
    ),
    notes=(
        "E' il venditore a incassare (Stripe) e a gestire IVA/OSS: piu' adempimenti, "
        "non meno margine.",
        "La pubblicazione via API richiede un canale di vendita supportato: su un "
        "Pop-Up Store non e' garantita (segnalazioni di errore 6005 per i prodotti "
        "'Printify Express'). La creazione prodotti funziona, la pubblicazione no.",
        "Catalogo molto piu' ampio (2000+ prodotti) e dominio personalizzato.",
    ),
)

PLATFORMS: tuple[Platform, ...] = (HOPLIX, PRINTIFY)
BY_KEY: dict[str, Platform] = {p.key: p for p in PLATFORMS}

# Pesi dei criteri: la somma e' 1. Il costo pesa piu' di tutto perche' e' l'unica
# voce che si moltiplica per ogni ordine; l'automazione viene subito dopo
# perche' e' il motivo per cui questo progetto esiste.
WEIGHTS: Mapping[str, float] = {
    "cost": 0.30,
    "compliance": 0.20,
    "automation": 0.20,
    "speed": 0.10,
    "catalog": 0.10,
    "brand": 0.05,
    "risk": 0.05,
}


# ---------------------------------------------------------------------------
# margini
# ---------------------------------------------------------------------------


def net_margin(price: float, platform: Platform, *, vat: float = VAT_IT) -> dict[str, Any]:
    """Margine per singolo pezzo, con tutte le voci dichiarate.

    `vat` e' l'aliquota applicata al prezzo di vendita: il ricavo netto e' il
    prezzo meno l'IVA, in entrambi i casi (vedi docstring del modulo).
    """
    if not isinstance(price, (int, float)) or price <= 0:
        raise ValueError("prezzo non valido")
    gross = float(price)
    net = gross / (1.0 + vat)

    # Il costo e' quello che esce: se la piattaforma lo espone IVA inclusa resta
    # cosi', altrimenti va portato IVA inclusa per essere confrontabile.
    cost = platform.product_cost_eur
    fees = 0.0
    if not platform.collects_payment:
        fees = round(gross * platform.payment_fee_pct + platform.payment_fee_fixed_eur, 2)

    margin = round(net - cost - fees, 2)
    return {
        "price": round(gross, 2),
        "vat_eur": round(gross - net, 2),
        "net_revenue": round(net, 2),
        "product_cost": round(cost, 2),
        "product_cost_estimate": platform.product_cost_estimate,
        "payment_fees": fees,
        "margin": margin,
        "margin_pct_of_price": round(100.0 * margin / gross, 1),
        "margin_pct_of_net": round(100.0 * margin / net, 1) if net else 0.0,
        "roi_on_cost": round(100.0 * margin / cost, 1) if cost else 0.0,
        "positive": margin > 0,
    }


def shipping_note(platform: Platform) -> str:
    """La spedizione la paga il cliente: e' un dato, non un costo nostro."""
    if platform.key == "hoplix":
        return f"spedizione Italia {SHIPPING_IT_HOPLIX:.2f} EUR a carico del cliente"
    return f"spedizione EU da {SHIPPING_EU_PRINTIFY_FROM:.2f} EUR a carico del cliente"


# ---------------------------------------------------------------------------
# punteggi (dichiarati, non magici)
# ---------------------------------------------------------------------------


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def criterion_scores(platform: Platform, price: float, *, vat: float = VAT_IT) -> dict[str, dict[str, Any]]:
    """Punteggio 0..1 per criterio, con la formula scritta accanto.

    Ogni voce restituisce `value` (0..1) e `why`: cosi' un punteggio piu' basso
    si puo' contestare leggendo il motivo, invece di discutere il totale.
    """
    m = net_margin(price, platform, vat=vat)

    # costo: quanta parte del ricavo netto resta dopo il costo prodotto
    cost_score = _clamp01(m["margin_pct_of_net"] / 100.0)

    # adempimenti: la piattaforma incassa/fattura, nessuna commissione nostra,
    # IVA non a carico nostro
    c = 0.0
    if not platform.seller_handles_vat:
        c += 0.40
    if platform.collects_payment:
        c += 0.30
    if platform.payment_fee_pct == 0.0 and platform.payment_fee_fixed_eur == 0.0:
        c += 0.30
    compliance = _clamp01(c)

    # automazione: pubblicare nello store (peso doppio), ordini, test mode
    a = 0.0
    if platform.api_publishes_to_store:
        a += 0.50
    if platform.api_orders:
        a += 0.30
    if platform.api_test_mode:
        a += 0.20
    automation = _clamp01(a)

    # tempi: produzione + consegna in Italia (20 giorni = 0)
    speed = _clamp01(1.0 - platform.total_days() / 20.0)

    # catalogo: scala logaritmica (200 prodotti non valgono 1/10 di 2000)
    catalog = _clamp01(math.log10(max(1, platform.products)) / 3.5)

    # brand: dominio personalizzato senza abbonamento
    brand = 1.0 if platform.custom_domain and platform.monthly_fee_eur == 0 else 0.4

    # rischio: incertezze dichiarate (stime di costo e automazione non garantita)
    risk = 1.0
    if platform.product_cost_estimate:
        risk -= 0.30
    if not platform.api_publishes_to_store:
        risk -= 0.40
    if not platform.api_test_mode:
        risk -= 0.10
    risk = _clamp01(risk)

    return {
        "cost": {"value": round(cost_score, 3), "why": f"margine {m['margin_pct_of_net']}% del ricavo netto"},
        "compliance": {"value": round(compliance, 3), "why": _compliance_why(platform)},
        "automation": {"value": round(automation, 3), "why": _automation_why(platform)},
        "speed": {"value": round(speed, 3), "why": f"produzione {platform.production_days[0]}-{platform.production_days[1]}g + consegna IT {platform.delivery_it_days[0]}-{platform.delivery_it_days[1]}g"},
        "catalog": {"value": round(catalog, 3), "why": f"{platform.products} prodotti"},
        "brand": {"value": round(brand, 3), "why": "dominio proprio, nessun canone" if brand == 1.0 else "dominio/canone da verificare"},
        "risk": {"value": round(risk, 3), "why": _risk_why(platform)},
    }


def _compliance_why(p: Platform) -> str:
    if not p.seller_handles_vat and p.collects_payment:
        return "incassa e fattura la piattaforma: nessuna commissione nostra, IVA non a carico nostro"
    if p.collects_payment:
        return "incassa la piattaforma ma l'IVA resta da verificare"
    return "incassiamo noi (Stripe) e gestiamo IVA/OSS"


def _automation_why(p: Platform) -> str:
    bits = []
    bits.append("API pubblica nello store" if p.api_publishes_to_store else "API NON pubblica nello store")
    bits.append("ordini via API" if p.api_orders else "nessun ordine via API")
    bits.append("test mode" if p.api_test_mode else "nessun test mode")
    return ", ".join(bits)


def _risk_why(p: Platform) -> str:
    risks = []
    if p.product_cost_estimate:
        risks.append("costo prodotto STIMATO")
    if not p.api_publishes_to_store:
        risks.append("pubblicazione manuale")
    if not p.api_test_mode:
        risks.append("verifica solo a spese fatte")
    return "; ".join(risks) if risks else "nessuna incertezza dichiarata"


def score(platform: Platform, price: float, *, vat: float = VAT_IT) -> float:
    parts = criterion_scores(platform, price, vat=vat)
    return round(sum(WEIGHTS[k] * parts[k]["value"] for k in WEIGHTS), 4)


# ---------------------------------------------------------------------------
# confronto
# ---------------------------------------------------------------------------


def compare(price: float = DEFAULT_PRICE_EUR, *, vat: float = VAT_IT) -> dict[str, Any]:
    """Tabella di confronto + scelta, con i motivi in chiaro."""
    rows = []
    for p in PLATFORMS:
        rows.append(
            {
                "key": p.key,
                "label": p.label,
                "margin": net_margin(price, p, vat=vat),
                "score": score(p, price, vat=vat),
                "criteria": criterion_scores(p, price, vat=vat),
                "shipping": shipping_note(p),
                "production_days": p.production_days,
                "delivery_it_days": p.delivery_it_days,
                "monthly_fee_eur": p.monthly_fee_eur,
                "listing_fee_eur": p.listing_fee_eur,
                "sources": list(p.sources),
                "notes": list(p.notes),
            }
        )
    winner = max(rows, key=lambda r: r["score"])
    loser = min(rows, key=lambda r: r["score"])
    return {
        "price": price,
        "vat": vat,
        "rows": rows,
        "chosen": winner["key"],
        "chosen_label": winner["label"],
        "score_gap": round(winner["score"] - loser["score"], 4),
        "margin_gap": round(winner["margin"]["margin"] - loser["margin"]["margin"], 2),
        "why": _why(winner, loser),
    }


def _why(winner: Mapping[str, Any], loser: Mapping[str, Any]) -> list[str]:
    """I motivi che decidono, nell'ordine in cui pesano."""
    out: list[str] = []
    if winner["margin"]["margin"] > loser["margin"]["margin"]:
        out.append(
            f"margine per pezzo piu' alto di {winner['margin']['margin'] - loser['margin']['margin']:.2f} EUR "
            f"({winner['margin']['margin']:.2f} contro {loser['margin']['margin']:.2f})"
        )
    for key in ("compliance", "automation", "speed", "catalog", "brand", "risk"):
        w = winner["criteria"][key]
        l = loser["criteria"][key]
        if abs(w["value"] - l["value"]) >= 0.15:
            out.append(f"{key}: {w['value']:.2f} contro {l['value']:.2f} — {w['why']}")
    return out


def format_compare(report: Mapping[str, Any]) -> str:
    lines = [
        f"=== CONFRONTO PIATTAFORME — prezzo {report['price']:.2f} EUR, IVA {report['vat']*100:.0f}% ===",
        "",
    ]
    for row in report["rows"]:
        m = row["margin"]
        lines.append(
            f"[{row['key']}] {row['label']} — punteggio {row['score']:.3f}"
            + ("  <== SCELTA" if row["key"] == report["chosen"] else "")
        )
        lines.append(
            f"   margine {m['margin']:.2f} EUR/pezzo ({m['margin_pct_of_price']:.1f}% del prezzo, "
            f"{m['margin_pct_of_net']:.1f}% del ricavo netto)"
        )
        lines.append(
            f"   ricavo netto {m['net_revenue']:.2f} - costo {m['product_cost']:.2f}"
            f"{' (STIMA)' if m['product_cost_estimate'] else ''} - commissioni {m['payment_fees']:.2f}"
        )
        lines.append(f"   tempi: produzione {row['production_days'][0]}-{row['production_days'][1]}g, "
                     f"consegna IT {row['delivery_it_days'][0]}-{row['delivery_it_days'][1]}g | {row['shipping']}")
        for key in ("cost", "compliance", "automation", "speed", "catalog", "brand", "risk"):
            c = row["criteria"][key]
            lines.append(f"     - {key:11s} {c['value']:.2f}  ({c['why']})")
        for note in row["notes"]:
            lines.append(f"   nota: {note}")
        lines.append("")
    lines.append("PERCHE' QUESTA SCELTA:")
    for reason in report["why"]:
        lines.append(f"  • {reason}")
    lines.append("")
    lines.append(
        "Il margine e' calcolato al netto dell'IVA su ENTRAMBE: il vantaggio di "
        "Hoplix su fatture e adempimenti e' reale ma non e' margine."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Confronta Printify Pop-Up Store e Hoplix (sola lettura).")
    parser.add_argument("--price", type=float, default=DEFAULT_PRICE_EUR, help="prezzo di vendita EUR (default 24.90)")
    parser.add_argument("--json", action="store_true", help="output JSON")
    args = parser.parse_args(argv)

    try:
        report = compare(args.price)
    except ValueError as exc:
        print(f"prezzo non valido: {exc}")
        return 2
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(format_compare(report))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
