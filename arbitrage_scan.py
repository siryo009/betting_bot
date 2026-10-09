"""arbitrage_scan.py — Misura l'arbitraggio SX Bet <-> Smarkets. SOLO LETTURA.

PERCHE' ESISTE (10/10/2026). SX Bet e' **un singolo exchange P2P**: al suo
interno non esiste arbitraggio (i due lati del book SONO il prezzo). L'unico
arbitraggio reale e' **cross-venue** — e il progetto ha gia' DUE provider di
exchange scritti in `execution_engine.py` (`SxBetProvider` e
`SmarketsProvider`), entrambi con discovery 1X2 calcio e lettura book.

Il container e' in **Amsterdam** dal 01/10 (cutover per il geo-blocco SX):
`api.smarkets.com`, che dall'Italia e' inibito dall'ADM, e' raggiungibile da
li'. Quella porta si e' aperta e nessuno l'ha ancora bussata.

Questo strumento risponde a UNA domanda, con i numeri: **quante opportunita'
di arbitraggio esistono davvero fra i due exchange, con che margine, a che
profondita' e con quanto capitale?** Non esegue nulla: nessun ordine, nessuna
scrittura, **zero crediti the-odds-api** (SX e Smarkets hanno API proprie).

Scelta di progetto: come `line_intersection.py` / `liquidity_impact.py` /
`gate_audit.py`, e' un misuratore. Decide dopo, il proprietario, coi dati.

⚠️ VINCOLO DI CAPITALE (il motivo per cui si misura PRIMA di costruire).
L'arbitraggio e' DUE gambe su DUE venue: servono fondi su **entrambe**. Con
~30 USDC tutti su SX la gamba Smarkets non e' coperta. Il report calcola per
ogni opportunita' la cassa richiesta per venue, la profondita' massima
eseguibile e il rispetto dello stake minimo — cioe' dice se l'arbitraggio e'
*realizzabile* col capitale disponibile, non solo se esiste.

⚠️ NON e' collegato alla produzione: `auto_bet` non lo importa, nessuna
pipeline lo chiama. Sta a monte di qualunque decisione.

CLI:
    venv/bin/python arbitrage_scan.py [--json] [--min-margin 0.005]
        [--max-events 60] [--budget 30] [--window-min 90]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("arbitrage_scan")

# ---------------------------------------------------------------------------
# Config (env, con default prudenti)
# ---------------------------------------------------------------------------

#: Esiti del 1X2, nell'ordine canonico del progetto.
OUTCOMES: Tuple[str, str, str] = ("1", "X", "2")

#: Etichette venus -> outcome "X" (il pareggio ha nomi diversi per venue).
_DRAW_TOKENS = ("draw", "pareggio", "x")


def _num_env(name: str, default: float, *, minimum: Optional[float] = None) -> float:
    """Legge un env numerico a RUNTIME (clamp + warning, mai un eccezione)."""
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        val = float(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning("arbitrage_scan: %s=%r non numerico, uso %s", name, raw, default)
        return default
    if minimum is not None and val < minimum:
        logger.warning("arbitrage_scan: %s=%s sotto il minimo, uso %s", name, val, minimum)
        return minimum
    return val


def min_margin() -> float:
    """Margine minimo d'arbitraggio (`ARB_MIN_MARGIN`, default 0.005 = 0.5%)."""
    return _num_env("ARB_MIN_MARGIN", 0.005, minimum=0.0)


def match_window_min() -> float:
    """Tolleranza kickoff nel matching (`ARB_MATCH_WINDOW_MIN`, default 90')."""
    return _num_env("ARB_MATCH_WINDOW_MIN", 90.0, minimum=0.0)


def max_events() -> int:
    """Tetto eventi per venue (`ARB_MAX_EVENTS`, default 60)."""
    return int(_num_env("ARB_MAX_EVENTS", 60.0, minimum=1.0))


def default_budget() -> float:
    """Capitale totale da allocare nel piano (`ARB_BUDGET`, default 30)."""
    return _num_env("ARB_BUDGET", 30.0, minimum=0.0)


def min_stake_by_venue() -> Dict[str, float]:
    """Stake minimo per venue: SX e' 1 USDC (floor dell'exchange, misurato)."""
    return {
        "sx": _num_env("ARB_MIN_STAKE_SX", 1.0, minimum=0.0),
        "smarkets": _num_env("ARB_MIN_STAKE_SMARKETS", 1.0, minimum=0.0),
    }


# ---------------------------------------------------------------------------
# Provider (import PIGRO: `import arbitrage_scan` resta leggero)
# ---------------------------------------------------------------------------

def smarkets_configured() -> bool:
    """True se le credenziali Smarkets esistono (solo per la discovery)."""
    try:
        import execution_engine as ee
        return bool(ee.SMARKETS_USERNAME and ee.SMARKETS_PASSWORD)
    except Exception:
        return False


def build_sx_provider():
    """Provider SX Bet (letture pubbliche: nessuna chiave necessaria)."""
    import execution_engine as ee
    return ee.SxBetProvider(ee.SX_API_KEY, ee.SX_PRIVATE_KEY)


def build_smarkets_provider():
    """Provider Smarkets (richiede SMARKETS_USERNAME/PASSWORD)."""
    import execution_engine as ee
    return ee.SmarketsProvider(ee.SMARKETS_USERNAME, ee.SMARKETS_PASSWORD)


# ---------------------------------------------------------------------------
# Helper di parsing (delegano ai convertitori di execution_engine, mai copiati)
# ---------------------------------------------------------------------------

def _parse_ts(txt: Any) -> Optional[datetime]:
    """ISO (con 'Z', offset o naive-UTC) -> datetime UTC, difensivo."""
    if not txt:
        return None
    try:
        s = str(txt).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _sx_best_level(levels: Any) -> Tuple[Optional[float], float]:
    """Primo livello (miglior prezzo taker) -> (quota, size). Nessun ordine."""
    try:
        import execution_engine as ee
        parsed = ee._sx_levels_to_decimal(levels)
    except Exception:
        return None, 0.0
    if not parsed:
        return None, 0.0
    best = parsed[0]
    price = best.get("price")
    size = float(best.get("size") or 0.0)
    if not price or float(price) <= 1.0:
        return None, 0.0
    return float(price), size


def _sm_entry_price_qty(entry: Any) -> Tuple[Optional[int], float]:
    """Entrata quote Smarkets -> (prezzo in bps, quantita' grezza), difensivo."""
    def _from_dict(d: dict) -> Tuple[Optional[int], float]:
        price = d.get("price")
        if not price:
            # Senza prezzo l'entrata non e' usabile: una quantita' da sola
            # non e' una quotazione (mai un lato "capiente ma senza prezzo").
            return None, 0.0
        qty = d.get("quantity", d.get("size"))
        return (int(price),
                float(qty) if isinstance(qty, (int, float)) else 0.0)

    if isinstance(entry, dict):
        return _from_dict(entry)
    if isinstance(entry, list):
        for item in entry:
            if isinstance(item, dict) and item.get("price"):
                return _from_dict(item)
            if isinstance(item, (list, tuple)) and item and item[0]:
                qty = item[1] if len(item) > 1 else 0.0
                try:
                    return int(item[0]), float(qty)
                except (TypeError, ValueError):
                    return None, 0.0
    return None, 0.0


def _sm_best(quotes: Any, selection_id: Any) -> Tuple[Optional[float], float]:
    """Miglior prezzo `buy` + size di un contratto Smarkets -> (quota, stake)."""
    if not isinstance(quotes, dict):
        return None, 0.0
    book = quotes.get(str(selection_id))
    if not isinstance(book, dict):
        return None, 0.0
    bps, qty = _sm_entry_price_qty(book.get("buy"))
    if not bps or int(bps) <= 0:
        return None, 0.0
    try:
        import execution_engine as ee
        price = ee.prob_bps_to_decimal(int(bps))
        size = ee.quantity_to_stake(qty) if qty else 0.0
    except Exception:
        return None, 0.0
    if not price or float(price) <= 1.0:
        return None, 0.0
    return float(price), float(size)


def _outcome_of(outcome_one_name: str, home: str, away: str) -> Optional[str]:
    """Mappa `outcomeOneName` (SX) su 1/X/2: mai un indovinello.

    Su SX il 1X2 e' spezzato in tre mercati binari "X vs Not X": il nome
    dell'esito UNO dice quale dei tre e'. Un nome che non coincide con nessuno
    dei due team ne' col pareggio resta fuori (il mercato non e' un 1X2).
    """
    try:
        import team_names as tn
    except Exception:
        return None
    name = (outcome_one_name or "").strip()
    if not name:
        return None
    low = name.lower()
    if any(tok == low or tok in low.split() for tok in _DRAW_TOKENS):
        return "X"
    if home and tn.same_team(name, home):
        return "1"
    if away and tn.same_team(name, away):
        return "2"
    return None


# ---------------------------------------------------------------------------
# Discovery per venue
# ---------------------------------------------------------------------------

def sx_events(provider=None, *, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """Eventi 1X2 calcio su SX con best back + profondita' per esito.

    Il 1X2 di SX sono TRE mercati binari per evento: si raggruppano per
    (squadra uno, squadra due, minuto di kickoff) e si mappa ogni mercato sul
    suo esito. Nessuna chiave richiesta (letture pubbliche).
    """
    limit = limit or max_events()
    try:
        prov = provider or build_sx_provider()
        cats = prov.list_market_catalogue(
            event_type_ids=("5",), market_type="1X2", max_results=limit,
            market_type_ids=("1",))
    except Exception as e:
        logger.warning("arbitrage_scan: discovery SX fallita: %s", e)
        return []

    grouped: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for m in cats or []:
        home = (m.get("team_one_name") or "").strip()
        away = (m.get("team_two_name") or "").strip()
        if not home or not away:
            continue
        outcome = _outcome_of(m.get("outcome_one_name") or "", home, away)
        if outcome is None:
            continue
        ko = _parse_ts(m.get("open_date"))
        key = (home.lower(), away.lower(),
               ko.strftime("%Y%m%d%H%M") if ko else "")
        ev = grouped.setdefault(key, {
            "venue": "sx", "name": f"{home} vs {away}", "home": home,
            "away": away, "kickoff": ko.isoformat() if ko else None,
            "external_id": None, "league": m.get("league_label"),
            "odds": {},
        })
        # Book (lettura pubblica). Un book assente non elimina l'evento: lo
        # lascia senza quel lato, cosi' il report puo' dichiararlo.
        odds, depth = (None, 0.0)
        try:
            book = prov.get_market_book(m.get("market_id"))
            for r in (book or {}).get("runners") or []:
                if int(r.get("selectionId") or 0) == 1:
                    odds, depth = _sx_best_level(r.get("availableToBack"))
                    break
        except Exception as e:
            logger.debug("arbitrage_scan: book SX %s assente: %s",
                         m.get("market_id"), e)
        if odds:
            ev["odds"][outcome] = {"odds": odds, "depth": depth,
                                   "id": m.get("market_id")}
        if ev["external_id"] is None:
            ev["external_id"] = m.get("event_id") or m.get("market_id")
    return list(grouped.values())


def smarkets_events(provider=None, *, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """Eventi 1X2 calcio su Smarkets con best buy + profondita' per esito.

    Richiede le credenziali (SMARKETS_USERNAME/PASSWORD): senza, ritorna [] e
    il report lo dichiara. Un solo mercato `match_odds` per evento, con i tre
    contratti Home/Draw/Away.
    """
    limit = limit or max_events()
    try:
        prov = provider or build_smarkets_provider()
        cats = prov.list_market_catalogue(
            event_type_ids=("football_match",), market_type="match_odds",
            max_results=limit)
    except Exception as e:
        logger.warning("arbitrage_scan: discovery Smarkets fallita: %s", e)
        return []

    out: List[Dict[str, Any]] = []
    for m in cats or []:
        runners = m.get("runners") or []
        if len(runners) < 3:
            continue
        market_id = m.get("market_id")
        try:
            book = prov.get_market_book(market_id)
        except Exception as e:
            logger.debug("arbitrage_scan: book Smarkets %s assente: %s",
                         market_id, e)
            book = {}
        runner_quotes = {r.get("selectionId"): r.get("quotes")
                         for r in (book or {}).get("runners") or []}
        # Nomi: l'evento e' "Home vs Away", i contratti sono Home/Draw/Away.
        event_name = (m.get("event_name") or "").strip()
        home = away = ""
        if " vs " in event_name:
            home, away = [p.strip() for p in event_name.split(" vs ", 1)]
        ko = _parse_ts(m.get("open_date"))
        odds: Dict[str, Any] = {}
        for r in runners:
            name = (r.get("name") or "").strip()
            sid = r.get("selection_id")
            outcome = _outcome_of(name, home, away)
            if outcome is None:
                continue
            price, depth = _sm_best(runner_quotes.get(sid), sid)
            if price:
                odds[outcome] = {"odds": price, "depth": depth, "id": sid}
        if not odds:
            continue
        out.append({
            "venue": "smarkets", "name": event_name or f"{home} vs {away}",
            "home": home, "away": away,
            "kickoff": ko.isoformat() if ko else None,
            "external_id": market_id, "league": m.get("market_name"),
            "odds": odds,
        })
    return out


# ---------------------------------------------------------------------------
# Matching e detection
# ---------------------------------------------------------------------------

def match_events(sx: List[Dict[str, Any]], sm: List[Dict[str, Any]],
                 *, window_min: Optional[float] = None
                 ) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """Accoppia eventi SX<->Smarkets per NOMI squadra + kickoff vicino.

    Mai fuzzy: `team_names.same_team` e' deterministico e simmetrico (stessa
    regola usata dal settlement). Il kickoff entro `window_min` evita di
    accoppiare due partite diverse della stessa coppia (andata/ritorno).
    """
    win = timedelta(minutes=window_min if window_min is not None else match_window_min())
    try:
        import team_names as tn
    except Exception:
        return []
    pairs: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    used: set = set()
    for a in sx:
        for idx, b in enumerate(sm):
            if idx in used:
                continue
            if not (a.get("home") and a.get("away")):
                continue
            if not (tn.same_team(a["home"], b.get("home") or "")
                    and tn.same_team(a["away"], b.get("away") or "")):
                continue
            ta, tb = _parse_ts(a.get("kickoff")), _parse_ts(b.get("kickoff"))
            if ta and tb and abs((ta - tb).total_seconds()) > win.total_seconds():
                continue
            used.add(idx)
            pairs.append((a, b))
            break
    return pairs


def find_arbs(pairs: List[Tuple[Dict[str, Any], Dict[str, Any]]], *,
              margin: Optional[float] = None, budget: Optional[float] = None
              ) -> List[Dict[str, Any]]:
    """Cerca l'arbitraggio 1X2 cross-venue su OGNI opportunita' accoppiata.

    Per ciascun esito si prende il MIGLIOR prezzo fra le due venue; se
    `sum(1/quota) < 1 - margine` l'arbitraggio esiste. Il piano di capitale
    e' quello classico (stake proporzionale all'inverso, normalizzato sulla
    somma), con il vincolo di PROFONDITA' (massimo budget che entra in tutti
    i lati) e di STAKE MINIMO per venue.
    """
    margin = min_margin() if margin is None else float(margin)
    budget = default_budget() if budget is None else float(budget)
    mins = min_stake_by_venue()
    out: List[Dict[str, Any]] = []
    for sx_ev, sm_ev in pairs:
        venues = {"sx": sx_ev, "smarkets": sm_ev}
        legs: List[Dict[str, Any]] = []
        complete = True
        for oc in OUTCOMES:
            best = None
            for vname, ev in venues.items():
                q = (ev.get("odds") or {}).get(oc)
                if not q or not q.get("odds"):
                    continue
                if best is None or float(q["odds"]) > float(best[1]["odds"]):
                    best = (vname, q)
            if best is None:
                complete = False
                break
            vname, q = best
            legs.append({"outcome": oc, "venue": vname,
                         "odds": float(q["odds"]), "depth": float(q.get("depth") or 0.0),
                         "external_id": q.get("id")})
        if not complete:
            continue
        inv = sum(1.0 / leg["odds"] for leg in legs)
        if inv >= 1.0 - margin:
            continue
        # Piano di capitale sul budget richiesto.
        for leg in legs:
            leg["stake"] = round(budget * (1.0 / leg["odds"]) / inv, 2)
            floor = mins.get(leg["venue"], 0.0)
            leg["min_stake_ok"] = leg["stake"] >= floor
            leg["min_stake"] = floor
        # Budget massimo che entra nella profondita' di TUTTI i lati.
        depth_cap: Optional[float] = None
        for leg in legs:
            share = (1.0 / leg["odds"]) / inv  # quota del budget su quel lato
            if share <= 0:
                continue
            if leg["depth"] <= 0:
                # Profondita' ignota (book non letto): non si conclude nulla,
                # ma nemmeno si finge che sia capiente.
                depth_cap = 0.0 if depth_cap is None else min(depth_cap, 0.0)
                continue
            cap = leg["depth"] / share
            depth_cap = cap if depth_cap is None else min(depth_cap, cap)
        per_venue: Dict[str, float] = {}
        for leg in legs:
            per_venue[leg["venue"]] = round(
                per_venue.get(leg["venue"], 0.0) + leg["stake"], 2)
        profit = round(budget / inv - budget, 2)
        out.append({
            "event": sx_ev.get("name"),
            "kickoff": sx_ev.get("kickoff"),
            "league": sx_ev.get("league") or sm_ev.get("league"),
            "legs": legs,
            "inverse_sum": round(inv, 5),
            "margin_pct": round((1.0 - inv) * 100.0, 3),
            "roi_pct": round(profit / budget * 100.0, 3) if budget > 0 else 0.0,
            "profit": profit,
            "budget": budget,
            "per_venue_cash": per_venue,
            "max_budget_by_depth": (round(depth_cap, 2)
                                    if isinstance(depth_cap, float) else None),
            "executable_at_budget": bool(
                all(leg["min_stake_ok"] for leg in legs)
                and (depth_cap is None or depth_cap + 1e-9 >= budget)),
        })
    out.sort(key=lambda o: -o["roi_pct"])
    return out


# ---------------------------------------------------------------------------
# Scan + report
# ---------------------------------------------------------------------------

def scan(*, sx_provider=None, sm_provider=None, limit: Optional[int] = None,
         margin: Optional[float] = None, budget: Optional[float] = None
         ) -> Dict[str, Any]:
    """Misura completa: discovery, matching, opportunita' e verdetto.

    Fail-safe: ogni errore diventa un campo dichiarato, mai un'eccezione al
    chiamante. Non esegue ordini e non scrive nulla.
    """
    limit = limit or max_events()
    res: Dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "smarkets_configured": smarkets_configured(),
        "min_margin": margin if margin is not None else min_margin(),
        "budget": budget if budget is not None else default_budget(),
        "errors": [],
        "opportunities": [],
    }
    try:
        sx = sx_events(sx_provider, limit=limit)
    except Exception as e:                                        # pragma: no cover
        sx = []
        res["errors"].append(f"sx: {e}")
    try:
        sm = smarkets_events(sm_provider, limit=limit) if res["smarkets_configured"] \
            else []
    except Exception as e:                                        # pragma: no cover
        sm = []
        res["errors"].append(f"smarkets: {e}")

    pairs = match_events(sx, sm)
    arbs = find_arbs(pairs, margin=res["min_margin"], budget=res["budget"])
    res.update({
        "sx_events": len(sx),
        "smarkets_events": len(sm),
        "matched": len(pairs),
        "matched_with_3_outcomes": sum(
            1 for a, b in pairs if len(a.get("odds") or {}) == 3
            and len(b.get("odds") or {}) == 3),
        "opportunities": arbs,
    })
    res["verdict"] = {
        "opportunities": len(arbs),
        "executable": sum(1 for o in arbs if o["executable_at_budget"]),
        "best_roi_pct": arbs[0]["roi_pct"] if arbs else None,
        "note": _verdict_note(res),
    }
    return res


def _verdict_note(res: Dict[str, Any]) -> str:
    if not res.get("smarkets_configured"):
        return ("credenziali Smarkets assenti: impossibile leggere il secondo "
                "exchange (nessuna opportunita' misurabile)")
    if not res.get("sx_events"):
        return "nessun evento 1X2 su SX in discovery"
    if not res.get("smarkets_events"):
        return "nessun evento 1X2 su Smarkets in discovery (o discovery fallita)"
    if not res.get("matched"):
        return "nessun evento accoppiato fra le due venue (nomi/kickoff)"
    return f"{len(res.get('opportunities') or [])} opportunita' sopra soglia"


def format_report(d: Dict[str, Any]) -> str:
    """Report testuale Telegram-friendly, con i numeri azionabili."""
    lines = ["🔀 ARBITRAGGIO SX Bet ↔ Smarkets (misura, solo lettura)"]
    lines.append(f"  📡 SX: {d.get('sx_events', 0)} eventi | "
                 f"Smarkets: {d.get('smarkets_events', 0)} eventi"
                 + ("" if d.get("smarkets_configured")
                    else "  ⚠️ credenziali Smarkets ASSENTI"))
    lines.append(f"  🔗 accoppiati: {d.get('matched', 0)} "
                 f"(con 3 esiti su entrambe: {d.get('matched_with_3_outcomes', 0)})")
    lines.append(f"  🎯 soglia margine: {d.get('min_margin')} | "
                 f"budget piano: {d.get('budget')}")
    opps = d.get("opportunities") or []
    if not opps:
        lines.append("  ➖ nessuna opportunita' sopra soglia")
    else:
        lines.append(f"  ⚡ opportunita': {len(opps)}")
        for o in opps[:5]:
            legs = " + ".join(
                f"{lg['outcome']}@{lg['odds']:.2f}({lg['venue']})"
                for lg in o["legs"])
            lines.append(f"    • {o['event']} — ROI +{o['roi_pct']:.2f}% | "
                         f"inv {o['inverse_sum']:.4f}")
            lines.append(f"      {legs}")
            lines.append(f"      cassa per venue: {o['per_venue_cash']} | "
                         f"max per profondita': {o['max_budget_by_depth']} | "
                         f"eseguibile: {'si' if o['executable_at_budget'] else 'no'}")
    for err in d.get("errors") or []:
        lines.append(f"  ⚠️ {err}")
    v = d.get("verdict") or {}
    lines.append(f"  🧾 verdetto: {v.get('note')}")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Misura l'arbitraggio SX Bet <-> Smarkets (solo lettura)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--min-margin", type=float, default=None)
    ap.add_argument("--budget", type=float, default=None)
    ap.add_argument("--max-events", type=int, default=None)
    ap.add_argument("--window-min", type=float, default=None)
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    res = scan(limit=args.max_events, margin=args.min_margin,
               budget=args.budget)
    if args.json:
        print(json.dumps(res, indent=2, ensure_ascii=False, default=str))
    else:
        print(format_report(res))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
