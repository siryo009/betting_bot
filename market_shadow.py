"""market_shadow.py — Telemetria OMbra su `market_quotes` (01/10/2026).

Perche' esiste (direttiva del proprietario): SX Bet pubblica anche i mercati
NON calcistici (Basketball `sportId` 1, American Football `sportId` 8) e i tipi
osservati VIVI sono i tre della famiglia "including overtime" — 28
(Under/Over), 342 (Asian Handicap), 226 (12) — modellati nel contratto il
01/10/2026 (`decision.market.MarketType.OVER_UNDER_OT` / `ASIAN_HANDICAP_OT` /
`MONEYLINE_OT`). Il modello (Poisson) e' calcistico: non abbiamo un motore per
questi sport, quindi NON si analizza nulla. Si RACCOGLIE il mercato per
poterlo misurare piu' avanti.

REGOLA TASSATIVA DEL PROPRIETARIO
---------------------------------
Questo modulo scrive **ESCLUSIVAMENTE su `market_quotes`**.

Mai `predictions`, mai una riga a stake 0: una "previsione" senza denaro non e'
un esperimento, e' un dato falso che inquina il ROI e la calibrazione per
mercato (`market_diagnose`, `significance`, `multi_market.shadow_report`).
Il modulo non importa `save_prediction` e un tripwire lo verifica sul sorgente.

Cosa fa, in tre passi:

1. **DISCOVERY** dei mercati SX dei tipi richiesti per gli sport richiesti,
   con la STESSA paginazione della corsia multi-mercato
   (`multi_market._discover_type` con `sport_ids` parametrico: nessuna copia).
2. **ORDER BOOK** pubblico (riuso di `sx_signals._books_parallel`: 10 thread,
   zero chiavi, zero crediti, zero ordini).
3. **CONTRATTO + LEDGER**: ogni quota passa da `decision.market.parse_quote`
   (unica porta di validazione del progetto) e le righe conformi finiscono in
   `tracker.market_quotes` con l'upsert di `tracker.save_market_quotes`.

Fail-safe totale: come tutta la telemetria del progetto, `run()` non solleva
mai — un exchange lento non deve fermare il giro.

Comandi:

    venv/bin/python market_shadow.py [--json] [--no-save]

Interruttore: `SHADOW_MARKET_ENABLED` (default **0** = spento; l'ingestione e'
a costo zero ma nessuna attivita' parte in produzione senza una scelta
esplicita dell'operatore).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("market_shadow")

#: Identita' del feed nel contratto (deve essere non vuota).
GATEWAY_ID = os.getenv("SHADOW_GATEWAY_ID", "sxbet-shadow")
SOURCE = "sxbet"

#: Sport SX NON calcistici da leggere (5 = calcio, volutamente escluso: il
#: calcio ha gia' la sua corsia completa in `multi_market`).
DEFAULT_SPORTS: Tuple[str, ...] = ("1", "8")

#: Tipi SX della famiglia "including overtime" (decision.market.SX_TYPE_IDS).
DEFAULT_TYPES: Tuple[str, ...] = ("28", "342", "226")

#: Finestra dei mercati (una partita di basket/NFL si gioca nelle prossime ore).
HOURS_AHEAD = float(os.getenv("SHADOW_HOURS_AHEAD", "24") or 24)
#: Mercati grezzi per tipo (paginazione).
MAX_MARKETS = int(float(os.getenv("SHADOW_MAX_MARKETS", "200") or 200))

#: Limite di plausibilita' per una linea presa da un CAMPO della fonte.
#: ⚠️ Diverso da `multi_market._LINE_FIELD_MAX` (12 = gol): qui gli sport sono
#: a punteggio alto (basket ~150-260). I nomi degli esiti restano la fonte
#: primaria (come nel calcio), il campo si usa solo come ripiego.
_LINE_FIELD_MAX = 500.0


def enabled() -> bool:
    """La telemetria ombra e' accesa? (default OFF: nessuna attivita' implicita)."""
    return str(os.getenv("SHADOW_MARKET_ENABLED", "0")).strip().lower() \
        not in ("0", "false", "no", "off", "")


def _sports() -> Tuple[str, ...]:
    raw = os.getenv("SHADOW_SPORTS")
    if raw is None or not str(raw).strip():
        return DEFAULT_SPORTS
    return tuple(part.strip() for part in str(raw).split(",") if part.strip())


def _types() -> Tuple[str, ...]:
    raw = os.getenv("SHADOW_TYPES")
    if raw is None or not str(raw).strip():
        return DEFAULT_TYPES
    return tuple(part.strip() for part in str(raw).split(",") if part.strip())


def shadow_markets(types: Optional[Sequence[str]] = None) -> Tuple[str, ...]:
    """Mercati canonici coperti (derivati dal CONTRATTO, mai riscritti a mano).

    Un type id senza corrispondenza nel registro viene ignorato: meglio non
    leggere un tipo che registrarlo sotto un mercato inventato.
    """
    from decision.market import SX_TYPE_IDS            # import pigro

    out: List[str] = []
    for type_id in (types or _types()):
        try:
            market = SX_TYPE_IDS.get(int(type_id))
        except (TypeError, ValueError):
            market = None
        if market is not None and market.value not in out:
            out.append(market.value)
    return tuple(out)


def _line_for(m: Dict[str, Any], *, has_lines: bool) -> Optional[float]:
    """Linea di un mercato SX: PRIMA dai nomi degli esiti, poi dal campo.

    Come nel calcio i nomi sono la fonte piu' affidabile ('Over 220.5',
    'Lakers -3.5'). Il vincolo sul campo e' largo perche' gli sport qui non
    sono calcistici.
    """
    if not has_lines:
        return None
    from multi_market import parse_line                # import pigro (riuso)

    for key in ("outcomeOneName", "outcomeTwoName"):
        value = parse_line(m.get(key))
        if value is not None:
            return value
    for key in ("line", "lineValue", "line_value", "handicap", "overUnderLine"):
        raw = m.get(key)
        if raw is None:
            continue
        value = parse_line(raw)
        if value is not None and abs(value) <= _LINE_FIELD_MAX:
            return value
    return None


def discover(provider: Any, *, sports: Optional[Sequence[str]] = None,
             types: Optional[Sequence[str]] = None,
             max_markets: Optional[int] = None,
             now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Mercati SX non calcistici nella finestra, normalizzati per il contratto.

    Ritorna una voce per MERCATO con: evento, kickoff, squadre, lega, mercato
    canonico, LINEA (None sui mercati senza linea), market hash ed esiti grezzi.
    Fail-soft: un record malformato viene contato e saltato, il giro continua.
    """
    from multi_market import _discover_type          # import pigro (riuso)
    from sx_signals import _kickoff_utc_ms           # import pigro (riuso)
    from decision.market import MARKET_SPECS, SX_TYPE_IDS

    wanted = types or _types()
    sport_list = tuple(sports or _sports())
    limit = int(max_markets or MAX_MARKETS)
    now_ms = int((now or datetime.now(timezone.utc)).timestamp() * 1000)
    lo = now_ms - 60 * 60 * 1000                      # -1h: live appena iniziati
    hi = now_ms + HOURS_AHEAD * 3600 * 1000
    records: List[Dict[str, Any]] = []
    skipped = 0
    for sport_id in sport_list:
        for type_id in wanted:
            try:
                market = SX_TYPE_IDS.get(int(type_id))
            except (TypeError, ValueError):
                market = None
            if market is None:
                continue
            spec = MARKET_SPECS.get(market)
            has_lines = bool(spec and spec.has_lines)
            for m in _discover_type(provider, type_id, limit,
                                    sport_ids=str(sport_id)):
                event_id = m.get("sportXeventId")
                kickoff_ms = _kickoff_utc_ms(m.get("gameTime"))
                home = str(m.get("teamOneName") or "").strip()
                away = str(m.get("teamTwoName") or "").strip()
                market_hash = m.get("marketHash")
                line = _line_for(m, has_lines=has_lines)
                if not (event_id and market_hash and home and away) \
                        or kickoff_ms is None \
                        or not (lo <= kickoff_ms <= hi) \
                        or (has_lines and line is None):
                    skipped += 1
                    continue
                records.append({
                    "event_id": str(event_id),
                    "sport_id": str(sport_id),
                    "league_label": str(m.get("leagueLabel") or "").strip(),
                    "kickoff_ms": kickoff_ms,
                    "home": home, "away": away,
                    "market_type": market.value,
                    "line": line,
                    "main_line": bool(m.get("mainLine")) if has_lines else None,
                    "market_hash": str(market_hash),
                    "outcome_one": m.get("outcomeOneName"),
                    "outcome_two": m.get("outcomeTwoName"),
                })
    if skipped:
        logger.info("market_shadow: %d mercati scartati in discovery "
                    "(linea/squadre/kickoff non utilizzabili)", skipped)
    records.sort(key=lambda r: (r["kickoff_ms"], r["market_type"]))
    return records


def build_rows(records: Sequence[Dict[str, Any]], books: Dict[str, Any], *,
               observed: Optional[datetime] = None,
               gateway_id: str = GATEWAY_ID
               ) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Record discovery + order book -> righe validate dal CONTRATTO 2.0.

    Una riga per ESITO (2 per mercato binario). `parse_quote` e' l'unica porta
    di validazione: una riga non conforme non entra nel ledger. Non solleva mai.
    """
    from decision.market import MARKET_SCHEMA_VERSION, parse_quote, spec_for
    from sx_signals import _kickoff_iso
    from multi_market import outcome_sides

    when = observed or datetime.now(timezone.utc)
    rows: List[Dict[str, Any]] = []
    stats = {"built": 0, "rejected": 0, "no_book": 0, "no_side": 0,
             "incoherent": 0}
    for rec in records:
        sides = outcome_sides(rec["market_type"], rec.get("outcome_one"),
                              rec["home"], rec["away"])
        if sides is None:
            stats["no_side"] += 1
            continue
        book = books.get(rec["market_hash"]) or {}
        if book.get("error"):
            stats["no_book"] += 1
            continue
        sel_one, sel_two = sides
        kickoff = _kickoff_iso(rec["kickoff_ms"])
        prices: Dict[str, float] = {}
        for selection, index in ((sel_one, 1), (sel_two, 2)):
            info = book.get(index) or {}
            best = info.get("best") or {}
            price = best.get("price")
            if not price or float(price) <= 1.0:
                continue
            prices[selection] = float(price)
            row: Dict[str, Any] = {
                "schema_version": MARKET_SCHEMA_VERSION,
                "event_id": f"sx-{rec['event_id']}",
                "market": rec["market_type"],
                "market_type": rec["market_type"],
                "selection": selection,
                "odds": float(price),
                "timestamp": when.isoformat(),
                "source": SOURCE,
                "gateway_id": gateway_id,
                "line": rec.get("line"),
                "origin": "native",
                "event_name": f"{rec['home']} - {rec['away']}",
                "league": rec.get("league_label") or "",
                "home": rec["home"], "away": rec["away"],
                "kickoff": kickoff,
                "depth_usdc": float(info.get("depth") or 0.0),
                # Campi extra (ammessi dal contratto, non lo allargano):
                # servono alla diagnosi e al percorso d'ordine futuro.
                "market_hash": rec["market_hash"],
                "sport_x_event_id": rec["event_id"],
                "sport_id": rec.get("sport_id"),
            }
            _spec = spec_for(rec["market_type"])
            if _spec is not None and _spec.has_lines:
                # Solo sui mercati CON linea: il contratto VIETA `main_line`
                # sui mercati senza (una riga respinta sarebbe un dato perso
                # per un campo che non ha senso).
                row["main_line"] = bool(rec.get("main_line"))
            if len(prices) == 2:
                row["inv_sum"] = round(
                    sum(1.0 / p for p in prices.values()), 4)
                row["total_depth_usdc"] = round(
                    float((book.get(1) or {}).get("depth") or 0.0)
                    + float((book.get(2) or {}).get("depth") or 0.0), 2)
            try:
                quote = parse_quote(row, gateway_id=gateway_id)
            except Exception as exc:                   # riga non conforme
                stats["rejected"] += 1
                logger.debug("market_shadow: quota respinta dal contratto "
                             "(%s %s %s): %s", rec["market_type"],
                             rec.get("line"), selection, exc)
                continue
            rows.append(quote.as_row())
            stats["built"] += 1
        if len(prices) == 2:
            inv = sum(1.0 / p for p in prices.values())
            if not (0.98 <= inv <= 1.08):
                # Mercato non coerente (book sporco o in movimento): le due
                # righe appena costruite restano fuori dal ledger.
                stats["incoherent"] += 1
                rows = rows[:-2]
                stats["built"] -= 2
    return rows, stats


def run(provider: Any = None, *, save: bool = True,
        sports: Optional[Sequence[str]] = None,
        types: Optional[Sequence[str]] = None,
        observed: Optional[datetime] = None) -> Dict[str, Any]:
    """Un ciclo di telemetria ombra: discovery + book + contratto + ledger.

    ⚠️ Scrive SOLO `market_quotes` (`tracker.save_market_quotes`). Nessuna
    riga su `predictions`, nessun ordine, nessuna chiave, nessun credito.
    Non solleva mai: un mercato sporco (o l'exchange giu') non ferma il giro.
    """
    from sx_signals import _books_parallel            # import pigro (riuso)

    summary: Dict[str, Any] = {
        "enabled": enabled(), "markets": list(shadow_markets(types)),
        "sports": list(sports or _sports()),
        "records": 0, "quotes": 0, "saved": 0, "skipped": 0,
        "fixtures": 0, "by_market": {}, "error": None,
    }
    try:
        if provider is None:
            from execution_engine import SxBetProvider
            provider = SxBetProvider()
        records = discover(provider, sports=sports, types=types,
                           now=observed)
        summary["records"] = len(records)
        by_market: Dict[str, int] = {}
        for rec in records:
            key = rec["market_type"]
            by_market[key] = by_market.get(key, 0) + 1
        summary["by_market"] = by_market
        if not records:
            logger.info("market_shadow: nessun mercato non calcistico "
                        "nella finestra (sports=%s, tipi=%s)",
                        ",".join(summary["sports"]),
                        ",".join(summary["markets"]))
            return summary
        hashes = [r["market_hash"] for r in records]
        books = _books_parallel(provider, hashes)
        rows, stats = build_rows(records, books, observed=observed)
        summary["quotes"] = stats["built"]
        summary["skipped"] = (stats["rejected"] + stats["no_book"]
                              + stats["no_side"] + stats["incoherent"])
        if save and rows:
            from tracker import save_market_quotes
            result = save_market_quotes(rows) or {}
            summary["saved"] = int(result.get("saved") or 0)
            summary["fixtures"] = int(result.get("fixtures") or 0)
            summary["error"] = result.get("error")
        logger.info("market_shadow: %d mercati -> %d quote salvate "
                    "(%d scartate, %d fixture)", summary["records"],
                    summary["saved"], summary["skipped"], summary["fixtures"])
    except Exception as exc:                           # fail-safe totale
        summary["error"] = str(exc)
        logger.warning("market_shadow: ciclo fallito (%s)", exc)
    return summary


def format_report(summary: Dict[str, Any]) -> str:
    """Riepilogo leggibile (CLI/report): mai un giudizio, solo cio' che e' stato letto."""
    lines = [
        "🧪 Telemetria ombra mercati SX (solo `market_quotes`)",
        f"   sport SX: {', '.join(summary.get('sports') or []) or '-'}",
        f"   mercati: {', '.join(summary.get('markets') or []) or '-'}",
        f"   discovery: {summary.get('records', 0)} mercati, "
        f"{summary.get('quotes', 0)} quote conformi al contratto",
        f"   ledger: {summary.get('saved', 0)} righe salvate, "
        f"{summary.get('skipped', 0)} scartate, "
        f"{summary.get('fixtures', 0)} fixture",
    ]
    by_market = summary.get("by_market") or {}
    if by_market:
        lines.append("   per mercato: " + ", ".join(
            f"{k} {v}" for k, v in sorted(by_market.items())))
    if summary.get("error"):
        lines.append(f"   ⚠️ error: {summary['error']}")
    lines.append("   (nessuna riga su `predictions`: la telemetria non "
                 "inquina ROI/calibrazione)")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Telemetria ombra mercati SX non calcistici su market_quotes")
    parser.add_argument("--json", action="store_true",
                        help="stampa il riepilogo in JSON")
    parser.add_argument("--no-save", action="store_true",
                        help="non scrive sul ledger (solo lettura/diagnosi)")
    args = parser.parse_args(argv)
    summary = run(save=not args.no_save)
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(format_report(summary))
    return 0 if not summary.get("error") else 1


if __name__ == "__main__":                              # pragma: no cover
    raise SystemExit(main())
