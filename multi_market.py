"""multi_market.py — Over/Under e Asian Handicap: dal book SX al ledger, alla corsia ordini.

Perche' questo modulo (19/09/2026): `sx_signals.py` copre SOLO il 1X2. SX Bet
pubblica anche i mercati a LINEA (type 2 = Over/Under, type 3 = Asian Handicap)
e il ledger multi-mercato `tracker.market_quotes` esiste dalla fase 1 — qui i
due pezzi si collegano e i calcoli di Poisson (`poisson_engine`) diventano
candidati giocabili.

Catena completa, in quattro passi:

1. **INGESTIONE** (`ingest`): discovery dei mercati SX type 2/3 (API PUBBLICA:
   zero chiavi, zero crediti, zero ordini), order book taker con la STESSA
   lettura di `sx_signals` (`_books_parallel`), righe validate dal CONTRATTO
   (`decision.market.parse_quote`, schema 2.0: mercato, linea, provenienza) e
   upsert sul ledger `tracker.market_quotes` (`save_market_quotes`).

2. **ANALISI** (`analyze_fixture`): per OGNI (mercato, linea) del ledger, la
   probabilita' del modello da Poisson — `ou_outcome_probs` per l'OU e
   `ah_outcome_probs` per l'AH, **push-aware** (linea intera con totale
   esattamente uguale alla linea = puntata restituita, P/L 0; quarter line =
   due mezze puntate) — devig a due esiti del mercato (`market_implied`) e
   blend modello+mercato come per il 1X2 (`adjusted_probability`).
   L'EV e' calcolato in modo esatto, non dalla probabilita' "efficace":
   `EV = p_win x (quota - 1) - p_lose` (il push vale 0).

3. **LEDGER** (`scan`): i candidati finiscono in `predictions` (mercato `OU` /
   `AH`, esito in formato ledger `Over 2.5` / `Home -0.75`) con lo stesso
   tiering del 1X2 (value / strong_value / rejected) — cosi' il settlement di
   `tracker` li salda e la calibrazione per mercato li misura.

4. **ORDINI** (`live_picks` + `order_target`): la corsia esecutiva legge dal
   ledger i soli pick APERTI in fascia, applica di nuovo il gate di lega e
   restituisce il bersaglio per `execution_engine.resolve_market_for`
   (mercato + LINEA + lato). `auto_bet` la concatena alla sua selezione 1X2:
   tutti i guardrail (T-60, stop-loss, cap, feed, dedup) valgono identici.

INTERRUTTORI LIVE PER MERCATO (direttiva del proprietario, 19/09/2026):

    ENABLE_LIVE_AH=1   -> l'Asian Handicap puo' piazzare ORDINI REALI;
    ENABLE_LIVE_OU=1   -> l'Over/Under viene AUTORIZZATO agli ordini reali.

AUTORIZZAZIONE ≠ ORDINI (direttiva del proprietario, 26/09/2026).
`authorized_markets()` e' cio' che l'operatore accende con gli interruttori;
`live_markets()` e' cio' che puo' davvero ordinare ADESSO, perche' aggiunge i
prerequisiti di fatto. L'OU ne ha uno: il gate di PRONTEZZA (`ou_readiness`) —
servono almeno `OU_LIVE_MIN_CLOSURES` chiusure refertate dell'ERA nuova
(`OU_LIVE_SINCE`; la corsia multi-mercato nasce il 19/09/2026) E un ROI
POSITIVO. Il -10.95% su 8 chiusure misurato il 25/09/2026 non e' una base per
rischiare denaro: l'OU continua a generare, registrare e saldare i segnali e
parte da solo quando la soglia e' raggiunta — nessun intervento manuale.

Gli interruttori vivono SOLO qui e `live_picks` legge `live_markets()`: una
corsia spenta (o non pronta) non puo' accendere ordini per distrazione di un
chiamante.

Regole del modulo:

1. **Fail-closed**: senza prezzo, senza linea o senza lato riconoscibile non
   nasce alcun candidato (mai un segnale su un mercato ambiguo).
2. **Sola lettura verso l'exchange**: le scritture sul ledger passano dal CRUD
   di `tracker` (`save_market_quotes`, `save_prediction`), mai da SQL qui.
3. **Nessuna divergenza**: le soglie di liquidita' sono gli STESSI nomi di env
   di `sx_signals`/`auto_bet` (un tripwire verifica i default), la devig e il
   blend sono quelli di `market_calib`, il tier e' quello di `value_filter`.
"""

from __future__ import annotations

import logging
import math
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from market_calib import MARKET_EDGE_STRONG, market_implied
from poisson_engine import ah_outcome_probs, expected_goals, ou_outcome_probs
from value_filter import (
    EV_MIN,
    MIN_FAVOURITE_MARKET_PROB,
    ODDS_MAX,
    ODDS_MIN,
    PLAYABLE_TIERS,
    adjusted_probability,
    canonical_league,
    compute_ev,
    get_signal_tier,
    is_sane,
    league_allowed,
)

logger = logging.getLogger("multi_market")


# ---------------------------------------------------------------------------
# Configurazione (tutto da env: nessun redeploy per cambiare una soglia)
# ---------------------------------------------------------------------------

def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return str(raw).strip().lower() not in ("0", "false", "no", "off")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


#: Interruttori LIVE per mercato (vedi docstring). AH acceso: e' il mercato
#: scelto dal proprietario per ripartire a monetizzare. OU spento: shadow.
ENABLE_LIVE_AH = _env_flag("ENABLE_LIVE_AH", True)
ENABLE_LIVE_OU = _env_flag("ENABLE_LIVE_OU", False)

#: Gate di PRONTEZZA dell'Over/Under (26/09/2026). L'interruttore autorizza,
#: il CAMPIONE abilita: sotto questa soglia (o con ROI non positivo) l'OU
#: resta shadow anche con `ENABLE_LIVE_OU=1`. La misura del 25/09 (-10.95% su
#: 8 chiusure post-19/09, linee intere alte con 3 push su 8) non e' una base
#: per rischiare denaro reale su un bankroll di ~33 USDC.
OU_LIVE_MIN_CLOSURES = _env_int("OU_LIVE_MIN_CLOSURES", 20)
#: Inizio dell'era strategica da misurare: la corsia multi-mercato nasce il
#: 19/09/2026, prima e' `fixture_engine` (pipeline RITIRATA: mescolarla
#: significherebbe misurare una strategia che non gira piu').
OU_LIVE_SINCE = os.getenv("OU_LIVE_SINCE", "2026-09-19")
#: Memoria della prontezza: il giro auto-bet gira ogni 60s e `live_markets()`
#: viene chiamata anche per ogni candidato — senza TTL il ledger locale si
#: leggerebbe centinaia di volte al minuto per una misura che cambia solo a
#: fine partita.
OU_READY_TTL = _env_float("OU_READY_TTL", 60.0)

#: Identita' del feed nel contratto (`gateway_id` non vuoto e' obbligatorio).
GATEWAY_ID = os.getenv("MM_GATEWAY_ID", "sxbet-multi")
SOURCE = "sxbet"

#: Finestra dei fixture candidati (identica a sx_signals/auto_bet: 24h).
HOURS_AHEAD = _env_float("MM_HOURS_AHEAD", 24.0)
#: Pavimento condiviso con `auto_bet`/`sx_signals`: 2 minuti (direttiva
#: 04/10/2026, finestra T-180..T-2).
MIN_MINUTES_TO_START = _env_int("MM_MIN_MINUTES_TO_START", 2)
#: 26/09/2026 (direttiva "piu' volume su AH/OU"): 400 -> 600 mercati grezzi
#: e 12 -> 20 linee per mercato. Si allarga SOLO la COPERTURA: fascia quota,
#: edge, EV, gate di lega e soglie di liquidita' restano quelle congelate del
#: 22/09 — piu' linee scansionate, gli STESSI criteri per giocarne una.
#: Il costo e' tempo di scansione (letture pubbliche SX: zero chiavi, zero
#: crediti, zero ordini) e il vincolo e' il runtime del giro, perche' il job
#: `multi_market_job` gira ogni 15' con `max_instances=1`.
MAX_RAW_MARKETS = _env_int("MM_MAX_RAW_MARKETS", 600)
#: Quante linee diverse analizzare per mercato e partita. SX ne offre molte
#: (OU 0.5..8.5): analizzarle tutte riempirebbe il ledger di righe che nessuno
#: gioca. Le linee principali/gia' piu' liquide vengono prima.
MAX_LINES_PER_MARKET = _env_int("MM_MAX_LINES_PER_MARKET", 20)

#: Soglie di liquidita' (STESSI nomi/env di sx_signals e auto_bet: la taratura
#: del 21/09/2026 e' una sola in tutto il progetto: 20/4/20 USDC).
MIN_DEPTH_USDC = _env_float("SX_MIN_DEPTH_USDC", 20.0)
MIN_LEG_DEPTH_USDC = _env_float("SX_MIN_LEG_DEPTH_USDC", 4.0)
MIN_EXEC_DEPTH_USDC = _env_float("SX_MIN_EXEC_DEPTH_USDC", 20.0)
MIN_INV_SUM, MAX_INV_SUM = 0.98, 1.08

#: I mercati gestiti qui e il type id nativo SX (vedi decision.market).
MARKETS: Tuple[str, ...] = ("OU", "AH")
SX_TYPE_IDS: Dict[str, str] = {"OU": "2", "AH": "3"}
_NATIVE_TYPE_TO_MARKET = {v: k for k, v in SX_TYPE_IDS.items()}

#: I tier che il bot considera GIOCABILI. La definizione vive in
#: `value_filter.PLAYABLE_TIERS` (il modulo che possiede i tier) e qui e'
#: solo un ALIAS: cosi' un tier nuovo non puo' contare come "giocabile" nel
#: report e non esserlo nell'ordine (o viceversa).
PLAYABLE_STATUSES: Tuple[str, ...] = PLAYABLE_TIERS

#: Sotto questo numero di chiusure il ROI di un bucket e' rumore, non una
#: misura: e' la stessa soglia che `league_gate_impact` dichiara al
#: proprietario, cosi' il progetto non ha due idee di "campione affidabile".
MIN_RELIABLE_CLOSED = 30


def authorized_markets() -> Tuple[str, ...]:
    """Mercati AUTORIZZATI dall'operatore (soli interruttori env).

    E' l'INTENZIONE, non lo stato di fatto: per sapere cosa puo' piazzare un
    ordine adesso si legge `live_markets()` (che aggiunge i prerequisiti).
    """
    out = []
    if ENABLE_LIVE_AH:
        out.append("AH")
    if ENABLE_LIVE_OU:
        out.append("OU")
    return tuple(out)


#: Stato della memoria di prontezza. Modulo-livello di proposito (il TTL
#: esiste per non leggere il ledger a ogni candidato); `reset_ou_ready_cache`
#: lo azzera nei test, dove l'isolamento fra casi e' obbligatorio.
_ou_ready_memo: Dict[str, Any] = {"ts": 0.0, "data": None}


def reset_ou_ready_cache() -> None:
    """Azzera la memoria di prontezza (i test la usano prima di ogni caso)."""
    _ou_ready_memo["ts"] = 0.0
    _ou_ready_memo["data"] = None


def ou_readiness(*, rows: Optional[Iterable[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Prerequisito di fatto per gli ORDINI REALI sull'Over/Under.

    Misura il campione dell'era STRATEGICA corrente (`OU_LIVE_SINCE`) e della
    FASCIA QUOTA corrente (`ODDS_MIN`-`ODDS_MAX`), usando le stesse fonti del
    resto del progetto: `tracker.filter_predictions` (definizione unica del
    filtro d'era/fascia) e `PLAYABLE_TIERS` (definizione unica dei tier
    giocabili). Solo le righe GIOCABILI e CHIUSE contano.

    `ready` e' True solo se le chiusure sono almeno `OU_LIVE_MIN_CLOSURES` e
    il ROI e' POSITIVO. FAIL-CLOSED: se la misura non e' disponibile (ledger
    assente/corrotto) l'OU NON e' pronto — un'incertezza non apre gli ordini.

    `rows` iniettabile: i test passano il campione senza toccare il DB, e il
    report puo' riusare le righe che ha gia' letto.
    """
    out: Dict[str, Any] = {
        "market": "OU", "since": OU_LIVE_SINCE,
        "min_closures": OU_LIVE_MIN_CLOSURES, "closed": 0,
        "roi": None, "ready": False, "reason": "",
    }
    try:
        if rows is None:
            from tracker import filter_predictions, get_predictions
            rows = filter_predictions(
                get_predictions(mercato="OU", limit=5000),
                created_since=OU_LIVE_SINCE,
                odds_min=ODDS_MIN, odds_max=ODDS_MAX)
        playable = [r for r in (rows or []) if isinstance(r, dict)
                    and str(r.get("status") or "").strip().lower()
                    in PLAYABLE_STATUSES]
        closed = [r for r in playable if r.get("esito_finale") is not None]
        out["closed"] = len(closed)
        profit = 0.0
        for r in closed:
            try:
                profit += float(r.get("profit") or 0.0)
            except (TypeError, ValueError):
                pass
        if closed:
            out["roi"] = round(profit / len(closed), 4)
        if len(closed) < OU_LIVE_MIN_CLOSURES:
            out["reason"] = (f"campione insufficiente: {len(closed)} chiusure "
                             f"su {OU_LIVE_MIN_CLOSURES} richieste "
                             f"(era dal {OU_LIVE_SINCE})")
        elif (out["roi"] or 0.0) <= 0:
            out["reason"] = (f"ROI {out['roi'] * 100:+.2f}% non positivo su "
                             f"{len(closed)} chiusure")
        else:
            out["ready"] = True
            out["reason"] = (f"soglia raggiunta: {len(closed)} chiusure, "
                             f"ROI {out['roi'] * 100:+.2f}%")
    except Exception as exc:
        out["reason"] = f"misura non disponibile ({exc})"
    return out


def ou_live_ready(*, refresh: bool = False) -> bool:
    """True se l'OU puo' piazzare ordini reali (memoria TTL `OU_READY_TTL`)."""
    now = time.monotonic()
    cached = _ou_ready_memo.get("data")
    if not refresh and cached is not None and \
            (now - float(_ou_ready_memo.get("ts") or 0.0)) < OU_READY_TTL:
        return bool(cached.get("ready"))
    data = ou_readiness()
    _ou_ready_memo["ts"] = now
    _ou_ready_memo["data"] = data
    return bool(data.get("ready"))


def live_markets() -> Tuple[str, ...]:
    """I mercati che possono piazzare ORDINI REALI ADESSO (definizione unica).

    = mercati AUTORIZZATI ∩ prerequisiti di fatto. L'AH non ne ha oltre
    all'interruttore; l'OU ha il gate di prontezza. Ogni chiamante (ordini,
    report, log) legge QUESTA funzione, cosi' una corsia non puo' essere
    accesa in un percorso e spenta in un altro.
    """
    out = list(authorized_markets())
    if "OU" in out and not ou_live_ready():
        out.remove("OU")
    return tuple(out)


# ---------------------------------------------------------------------------
# Helper di formato: linea, esito di ledger, lato
# ---------------------------------------------------------------------------

_LINE_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")


def line_key(line: Any) -> str:
    """Chiave canonica della linea per il ledger (`2.5`, `-0.75`)."""
    try:
        return f"{float(line):g}"
    except (TypeError, ValueError):
        return ""


def parse_line(value: Any) -> Optional[float]:
    """Prima linea numerica nel testo ('Over 3.25' -> 3.25). None se assente.

    Zero E' una linea valida per l'Asian Handicap (handicap pari): il valore 0
    non viene mai confuso con "assente".
    """
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    match = _LINE_RE.search(str(value))
    if not match:
        return None
    try:
        return float(match.group(0))
    except (TypeError, ValueError):
        return None


def ledger_esito(market_type: str, selection: str, line: Any) -> str:
    """Esito nel formato che `tracker` sa saldare (e `ml_audit` riconosce).

    OU -> 'Over 2.5' / 'Under 3.25' (la linea si legge dall'esito: il
    settlement e' line-aware dal 19/09).
    AH -> 'Home -0.75' / 'Away +0.25': la linea e' quella del LATO (l'AH del
    ledger e' sempre dal punto di vista della squadra giocata).
    """
    value = float(line)
    if str(market_type).upper() == "OU":
        side = "Over" if str(selection) == "over" else "Under"
        return f"{side} {value:g}"
    home = str(selection) == "1"
    side_line = value if home else -value
    if abs(side_line) < 1e-9:
        # Handicap pari: senza questa normalizzazione l'Away diventava
        # 'Away +-0' (verificato in produzione il 19/09 sulle righe a linea 0).
        side_line = 0.0
    sign = "+" if side_line > 0 else ""
    return f"{'Home' if home else 'Away'} {sign}{side_line:g}"


def sx_line_of_esito(esito: str) -> Optional[float]:
    """Linea dal punto di vista di teamOne a partire dall'esito di ledger.

    Inversa di `ledger_esito` per l'AH: 'Home -0.75' -> -0.75,
    'Away +0.25' -> -0.25.
    """
    parts = str(esito or "").split()
    if len(parts) < 2:
        return parse_line(esito)
    line = parse_line(parts[1])
    if line is None:
        return None
    side = parts[0].strip().lower()
    if side.startswith("away"):
        return -line
    if side.startswith("home"):
        return line
    return None


def order_target(pick: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Bersaglio d'ordine per `execution_engine.resolve_market_for`.

    Ritorna {"market_type", "line" (dal punto di vista di teamOne), "side"}
    dove per l'OU `side` e' 'over'/'under' e per l'AH 'home'/'away'. None se
    il pick non e' riconducibile a un mercato a linea (fail-closed).
    """
    market_type = str(pick.get("mercato") or pick.get("market") or "").upper()
    esito = str(pick.get("esito_key") or pick.get("esito") or "")
    if market_type == "OU":
        side = "over" if esito.strip().lower().startswith("over") else (
            "under" if esito.strip().lower().startswith("under") else None)
        line = parse_line(esito)
        if side is None or line is None:
            return None
        return {"market_type": "OU", "line": line, "side": side}
    if market_type == "AH":
        line = sx_line_of_esito(esito)
        if line is None:
            return None
        return {"market_type": "AH", "line": line,
                "side": "home" if esito.strip().lower().startswith("home")
                else "away"}
    return None


def _clean_team(name: Any) -> str:
    """Nome squadra senza la linea attaccata ('Cagliari -0.75' -> 'Cagliari')."""
    return _LINE_RE.sub(" ", str(name or "")).strip(" -+")


def _same_team(a: str, b: str) -> bool:
    """Confronto squadre con il resolver unico del progetto (fail-closed)."""
    if not a or not b:
        return False
    try:
        from team_names import same_team
        return bool(same_team(a, b))
    except Exception:
        return a.strip().lower() == b.strip().lower()


def _uses_over_under_names(market_type: Any) -> bool:
    """Il mercato e' della famiglia "totale" (esiti over/under)?

    Derivato dal CONTRATTO (`decision.market.spec_for`): OU e la variante con
    supplementari (OU_OT) condividono la lettura dei nomi ('Over 220.5'), e un
    tipo nuovo della stessa famiglia segue da solo. Se il contratto non
    conosce il valore si ricade sul prefisso (mai indovinare la famiglia).
    """
    try:
        from decision.market import spec_for          # import pigro
        spec = spec_for(market_type)
    except Exception:
        spec = None
    if spec is not None:
        return tuple(spec.selections) == ("over", "under")
    return str(market_type or "").upper().startswith("OU")


def outcome_sides(market_type: str, outcome_one: Any,
                  home: str, away: str) -> Optional[Tuple[str, str]]:
    """Esiti canonici di outcomeOne/outcomeTwo nel mercato binario SX.

    TOTALE (OU / OU_OT): il nome dell'esito dice il lato ('Over 2.5' /
    'Under 2.5') — la famiglia e' derivata dal contratto, non da una lista
    riscritta qui.
    SCONTRO (AH / AH_OT / ML_OT): outcomeOne e' la squadra a cui si applica la
    linea; la selection 1 e' teamOne, la 2 teamTwo. Senza riconoscimento ->
    None (mai indovinare).
    """
    mt = str(market_type).upper()
    o1 = str(outcome_one or "")
    if _uses_over_under_names(market_type):
        low = o1.strip().lower()
        if low.startswith("over") or " over" in low:
            return "over", "under"
        if low.startswith("under") or " under" in low:
            return "under", "over"
        return None
    clean = _clean_team(o1)
    if _same_team(clean, home):
        return "1", "2"
    if _same_team(clean, away):
        return "2", "1"
    return None


# ---------------------------------------------------------------------------
# 1. INGESTIONE: mercati SX type 2/3 -> contratto -> ledger `market_quotes`
# ---------------------------------------------------------------------------

#: Limiti di plausibilita' di una linea presa da un CAMPO della fonte (non dai
#: nomi): SX potrebbe esprimerla in unita' scalate, quindi si accetta solo un
#: valore che assomigli a una linea di gol (|v| <= 12). Oltre -> si ignora.
_LINE_FIELD_MAX = 12.0


def _market_line(m: Dict[str, Any]) -> Optional[float]:
    """Linea di un mercato SX: PRIMA dai nomi degli esiti, poi dal campo.

    I nomi sono la fonte piu' affidabile osservata sull'API pubblica
    ('Over 2.5' / 'Cagliari -0.75'): il campo esplicito si usa solo se i nomi
    non portano numeri e il valore e' plausibile. Zero e' una linea valida
    (handicap pari): mai confuso con 'assente'.
    """
    for key in ("outcomeOneName", "outcomeTwoName"):
        value = parse_line(m.get(key))
        if value is not None:
            return value
    for key in ("line", "lineValue", "line_value", "handicap"):
        raw = m.get(key)
        if raw is None:
            continue
        value = parse_line(raw)
        if value is not None and abs(value) <= _LINE_FIELD_MAX:
            return value
    return None


def _resolve_league_label(label: str) -> str:
    """Etichetta SX -> nome canonico della strategia (chiave `SPORTS_MAP`).

    La corsia multi-mercato salvava l'etichetta GREZZA del provider: quando
    coincide col nome ammesso non succede nulla, ma `Major League Soccer` (SX)
    non e' `MLS` (chiave di `SPORTS_MAP`) e il gate di lega lo leggeva come
    lega VIETATA. Misurato il 24/09/2026: candidati con EV +52% ed edge +9.5pp
    scartati con "lega esclusa per ROI negativo", che per quella lega in
    PROBATION non e' vero.

    Riusa la risoluzione deterministica di `sx_signals` (mai fuzzy scritto a
    mano) con l'alias di sicurezza di `value_filter` come ripiego. Una
    etichetta sconosciuta resta se stessa: nessun nome inventato.
    """
    name = (label or "").strip()
    if not name:
        return ""
    try:
        from sx_signals import _league_sx_to_sports_map   # import pigro
        return _league_sx_to_sports_map(name) or canonical_league(name)
    except Exception:
        return canonical_league(name)


def _discover_type(provider: Any, type_id: str,
                   max_markets: int,
                   errors: Optional[List[str]] = None,
                   sport_ids: str = "5") -> List[Dict[str, Any]]:
    """Una pagina (o piu') di /markets/active per UN type id (come sx_signals).

    `errors` (opzionale): se passata, il motivo di un fallimento viene
    REGISTRATO invece di essere solo loggato. Serve al probe dei mercati
    sorvegliati: senza di essa "il book non pubblica il mercato" e "la lettura
    e' fallita" diventano la stessa cosa (lista vuota), che e' esattamente il
    fallimento silenzioso da evitare.

    `sport_ids` (01/10/2026): SX identifica lo sport con `sportIds` (5 = calcio,
    1 = basket, 8 = football americano). Il default resta 5 (nessun chiamante
    esistente cambia comportamento); la telemetria ombra lo usa per leggere gli
    sport NON calcistici con la STESSA paginazione, invece di riscriverla.
    """
    out: List[Dict[str, Any]] = []
    pagination_key: Optional[str] = None
    while len(out) < max_markets:
        params: Dict[str, Any] = {"sportIds": str(sport_ids),
                                  "type": str(type_id),
                                  "pageSize": 100}
        if pagination_key:
            params["paginationKey"] = pagination_key
        try:
            data = provider._get("markets/active", params=params)
        except Exception as exc:
            logger.warning("multi_market: discovery type %s fallita: %s",
                           type_id, exc)
            if errors is not None:
                errors.append(str(exc))
            break
        d = data.get("data") if isinstance(data, dict) else {}
        markets = (d or {}).get("markets") or []
        for m in markets:
            if isinstance(m, dict):
                out.append(m)
        pagination_key = (d or {}).get("nextKey")
        if not pagination_key or not markets:
            break
    return out[:max_markets]


def discover(provider: Any, *, types: Optional[Sequence[str]] = None,
             max_markets: int = MAX_RAW_MARKETS,
             now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Mercati OU/AH calcio SX nella finestra, normalizzati per l'analisi.

    Ogni record porta: evento (sportXeventId), kickoff, squadre, lega, mercato,
    LINEA, mainLine e il market hash. Fail-soft: un record malformato viene
    contato e saltato, il giro continua.
    """
    from sx_signals import _kickoff_utc_ms          # import pigro (riuso)

    wanted = tuple(types or MARKETS)
    per_type = max(1, max_markets // max(1, len(wanted)))
    now_ms = int((now or datetime.now(timezone.utc)).timestamp() * 1000)
    lo = now_ms - 60 * 60 * 1000                     # -1h: live appena iniziati
    hi = now_ms + HOURS_AHEAD * 3600 * 1000
    records: List[Dict[str, Any]] = []
    skipped = 0
    for market_type in wanted:
        type_id = SX_TYPE_IDS.get(str(market_type).upper())
        if not type_id:
            continue
        for m in _discover_type(provider, type_id, per_type):
            event_id = m.get("sportXeventId")
            kickoff_ms = _kickoff_utc_ms(m.get("gameTime"))
            home = str(m.get("teamOneName") or "").strip()
            away = str(m.get("teamTwoName") or "").strip()
            market_hash = m.get("marketHash")
            line = _market_line(m)
            if not (event_id and market_hash and home and away) \
                    or kickoff_ms is None or line is None \
                    or not (lo <= kickoff_ms <= hi):
                skipped += 1
                continue
            records.append({
                "event_id": str(event_id),
                "league_label": _resolve_league_label(m.get("leagueLabel")),
                "kickoff_ms": kickoff_ms,
                "home": home, "away": away,
                "market_type": str(market_type).upper(),
                "line": float(line),
                "main_line": bool(m.get("mainLine")),
                "market_hash": str(market_hash),
                "outcome_one": m.get("outcomeOneName"),
                "outcome_two": m.get("outcomeTwoName"),
            })
    if skipped:
        logger.info("multi_market: %d mercati scartati in discovery "
                    "(linea/squadre/kickoff non utilizzabili)", skipped)
    records.sort(key=lambda r: r["kickoff_ms"])
    return records


def build_quote_rows(records: Sequence[Dict[str, Any]],
                     books: Dict[str, Any], *,
                     observed: Optional[datetime] = None,
                     gateway_id: str = GATEWAY_ID
                     ) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Record discovery + order book -> righe validate dal CONTRATTO 2.0.

    Una riga per ESITO (2 per mercato binario). Il contratto (`parse_quote`) e'
    l'unico posto dove una quota viene validata: mercato/linea/coerenza, linea
    obbligatoria dove serve, versione schema. Una riga respinta non entra.

    Ritorna (righe, contatori). Non solleva mai: il feed non deve poter
    fermare il giro.
    """
    from decision.market import MARKET_SCHEMA_VERSION, parse_quote
    from sx_signals import _kickoff_iso

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
            row = {
                "schema_version": MARKET_SCHEMA_VERSION,
                "event_id": f"sx-{rec['event_id']}",
                "market": rec["market_type"],
                "market_type": rec["market_type"],
                "selection": selection,
                "odds": float(price),
                "timestamp": when.isoformat(),
                "source": SOURCE,
                "gateway_id": gateway_id,
                "line": rec["line"],
                "main_line": rec.get("main_line"),
                "origin": "native",
                "event_name": f"{rec['home']} - {rec['away']}",
                "league": rec.get("league_label") or "",
                "home": rec["home"], "away": rec["away"],
                "kickoff": kickoff,
                "selection_label": ledger_esito(rec["market_type"],
                                                selection, rec["line"]),
                "depth_usdc": float(info.get("depth") or 0.0),
                # Campi extra (ammessi dal contratto, non lo allargano):
                # servono al percorso d'ordine (market hash) e alla diagnosi.
                "market_hash": rec["market_hash"],
                "sport_x_event_id": rec["event_id"],
            }
            if len(prices) == 2:
                inv = sum(1.0 / p for p in prices.values())
                row["inv_sum"] = round(inv, 4)
                row["total_depth_usdc"] = round(
                    float((book.get(1) or {}).get("depth") or 0.0)
                    + float((book.get(2) or {}).get("depth") or 0.0), 2)
            try:
                quote = parse_quote(row, gateway_id=gateway_id)
            except Exception as exc:                 # riga non conforme
                stats["rejected"] += 1
                logger.debug("multi_market: quota respinta dal contratto "
                             "(%s %s %s): %s", rec["market_type"],
                             rec["line"], selection, exc)
                continue
            rows.append(quote.as_row())
            stats["built"] += 1
        if len(prices) == 2:
            inv = sum(1.0 / p for p in prices.values())
            if not (MIN_INV_SUM <= inv <= MAX_INV_SUM):
                # Mercato non coerente (book sporco o in movimento): le due
                # righe appena costruite restano fuori dal ledger.
                stats["incoherent"] += 1
                rows = rows[:-2]
                stats["built"] -= 2
    return rows, stats


def ingest(provider: Any = None, *, types: Optional[Sequence[str]] = None,
           max_markets: int = MAX_RAW_MARKETS,
           observed: Optional[datetime] = None, save: bool = True) -> Dict[str, Any]:
    """Discovery + book + contratto + upsert sul ledger `market_quotes`.

    Sola lettura verso SX (API pubblica: zero chiavi, zero crediti, zero
    ordini). Non solleva mai: un mercato sporco non deve fermare il giro.
    """
    from sx_signals import _books_parallel            # import pigro (riuso)

    summary: Dict[str, Any] = {"records": 0, "quotes": 0, "saved": 0,
                               "skipped": 0, "fixtures": 0, "error": None}
    try:
        if provider is None:
            from execution_engine import SxBetProvider
            provider = SxBetProvider()
        records = discover(provider, types=types, max_markets=max_markets,
                          now=observed)
        summary["records"] = len(records)
        if not records:
            logger.info("multi_market: nessun mercato OU/AH nella finestra")
            return summary
        hashes = [r["market_hash"] for r in records]
        books = _books_parallel(provider, hashes)
        # Flusso del book (26/09): il rilevatore consuma i book GIA' scaricati
        # — nessuna lettura in piu', nessun ordine, solo telemetria
        # (`book_flow.py`). Fail-safe: non puo' fermare l'ingestione.
        try:
            import book_flow
            book_flow.observe_books_from_scan(
                books, {r["market_hash"]: {
                    "home": r.get("home"), "away": r.get("away"),
                    "league": r.get("league_label"),
                    "market": r.get("market_type"), "line": r.get("line")}
                    for r in records})
        except Exception as exc:
            logger.debug("multi_market: book_flow non disponibile (%s)", exc)
        rows, stats = build_quote_rows(records, books, observed=observed)
        summary["quotes"] = stats["built"]
        summary["skipped"] = (stats["rejected"] + stats["no_book"]
                              + stats["no_side"] + stats["incoherent"])
        if save and rows:
            from tracker import save_market_quotes
            result = save_market_quotes(rows) or {}
            summary["saved"] = int(result.get("saved") or 0)
            summary["fixtures"] = int(result.get("fixtures") or 0)
            summary["error"] = result.get("error")
        logger.info("multi_market: ingest %d mercati -> %d quote salvate "
                    "(%d scartate, %d fixture)", summary["records"],
                    summary["saved"], summary["skipped"], summary["fixtures"])
    except Exception as exc:                        # rete/discovery/DB
        logger.warning("multi_market: ingest fallita (%s)", exc)
        summary["error"] = str(exc)
    return summary


# ---------------------------------------------------------------------------
# SORVEGLIANZA dei mercati NON modellati (BTTS) — 25/09/2026
# ---------------------------------------------------------------------------

#: Type id SX che NON modelliamo ma che vale la pena sorvegliare. Il type 17
#: (BTTS) esiste nella doc ufficiale di SX ma il book NON lo pubblica sul
#: calcio: probe reale del 18/09 e del 25/09/2026 -> **0 mercati** (mentre il
#: type 2 Over/Under ne pubblica 100+). Senza PREZZO non esiste value bet:
#: la probabilita' del modello la sappiamo calcolare (`prob_btts`), ma non
#: c'e' nulla con cui confrontarla — e non si paga the-odds-api (2 crediti per
#: chiamata su `btts`) per rincorrere un mercato che l'exchange non quota.
#: Il backlog BTTS (10 punti) resta quindi CONGELATO finche' il feed non si
#: popola; questa sorveglianza e' il campanello che lo riapre, ed e' GRATUITA.
WATCHED_TYPES: Dict[str, str] = {"BTTS": "17"}


def probe_market_type(type_id: str = "17", *, provider: Any = None,
                      max_markets: int = 5) -> Dict[str, Any]:
    """Quanti mercati ATTIVI pubblica SX per UN type id (gratis, fail-safe).

    Lettura PUBBLICA di `/markets/active` (la stessa di `_discover_type`):
    zero chiavi, zero crediti the-odds-api, zero ordini. Un errore di rete o
    un provider ostile NON solleva mai: torna nel campo `error` e il
    chiamante resta silenzioso.
    """
    out: Dict[str, Any] = {"type_id": str(type_id), "markets": 0,
                           "available": False, "error": None}
    try:
        if provider is None:
            from execution_engine import SxBetProvider
            provider = SxBetProvider()
        errors: List[str] = []
        found = _discover_type(provider, str(type_id),
                               max(1, int(max_markets)), errors=errors)
        # Lettura FALLITA != mercato assente: senza questa distinzione un
        # errore di rete sembrerebbe "il book non quota il BTTS" e il
        # campanello tacerebbe proprio quando serve.
        out["error"] = errors[0] if errors else None
        out["markets"] = len(found)
        out["available"] = bool(found) and not errors
        if found:
            m = found[0]
            out["example"] = {
                "event": f"{m.get('teamOneName')} - {m.get('teamTwoName')}",
                "outcome_one": m.get("outcomeOneName"),
                "outcome_two": m.get("outcomeTwoName"),
                "league": m.get("leagueLabel"),
                "kickoff": m.get("gameTime"),
            }
    except Exception as exc:
        out["error"] = str(exc)
    return out


def probe_watched_markets(*, provider: Any = None) -> List[Dict[str, Any]]:
    """Probe di TUTTI i mercati sorvegliati (oggi solo BTTS type 17)."""
    out: List[Dict[str, Any]] = []
    for market, type_id in WATCHED_TYPES.items():
        probe = probe_market_type(type_id, provider=provider)
        probe["market"] = market
        out.append(probe)
    return out


def format_probe(probes: Sequence[Dict[str, Any]]) -> str:
    """Riga leggibile del probe (CLI, log del job, messaggio Telegram)."""
    parts: List[str] = []
    for probe in probes or []:
        name = str(probe.get("market") or probe.get("type_id"))
        if probe.get("error"):
            parts.append(f"{name}: errore ({probe['error']})")
        elif probe.get("available"):
            parts.append(f"{name}: DISPONIBILE ({probe['markets']} mercati)")
        else:
            parts.append(f"{name}: non disponibile (0 mercati)")
    return " | ".join(parts) or "nessun mercato sorvegliato"


# ---------------------------------------------------------------------------
# 2. ANALISI: Poisson (push-aware) + devig + blend -> candidati
# ---------------------------------------------------------------------------

def _model_side(market_type: str, lam_h: float, lam_a: float, line: float,
                selection: str):
    """(p_win, p_push, p_lose) del modello per UN lato. None se non calcolabile.

    OU -> `ou_outcome_probs` (side 'over'/'under');
    AH -> `ah_outcome_probs` (linea dal punto di vista di teamOne, `side`
    della squadra giocata) come gia' fa la telemetria AH del progetto.
    """
    try:
        if str(market_type).upper() == "OU":
            return ou_outcome_probs(lam_h, lam_a, float(line),
                                    side=str(selection))
        return ah_outcome_probs(lam_h, lam_a, float(line),
                                side="home" if str(selection) == "1" else "away")
    except Exception as exc:
        logger.debug("multi_market: modello fallito (%s %s %s): %s",
                     market_type, line, selection, exc)
        return None


def _quotes_for(fixture_id: str) -> List[Dict[str, Any]]:
    """Righe del ledger per la partita (fail-safe: [] se il DB non risponde)."""
    try:
        from tracker import get_market_quotes
        return list(get_market_quotes(fixture_id=fixture_id) or [])
    except Exception as exc:
        logger.debug("multi_market: lettura ledger %s fallita: %s",
                     fixture_id, exc)
        return []


def _group_key(row: Dict[str, Any]) -> Tuple[str, str]:
    return (str(row.get("market_type") or "").upper(),
            str(row.get("line_key") or ""))


def _group_rank(group: List[Dict[str, Any]]) -> Tuple[int, float, float]:
    """Ordine dei gruppi: linea principale prima, poi la piu' liquida."""
    main = 1 if any(r.get("main_line") for r in group) else 0
    depth = max((float(r.get("liquidity") or 0.0) for r in group), default=0.0)
    line = float(group[0].get("line") or 0.0)
    return (-main, -depth, abs(line))


def analyze_fixture(fixture_id: str, lam_h: float, lam_a: float, *,
                    quotes: Optional[Sequence[Dict[str, Any]]] = None,
                    league: str = "",
                    max_lines: Optional[int] = None
                    ) -> List[Dict[str, Any]]:
    """Candidati OU/AH di UNA partita dalle quote del ledger `market_quotes`.

    Per OGNI (mercato, linea): devig a due esiti (`market_implied`), probabilita'
    del modello push-aware, blend (`adjusted_probability`) e filtri di sanita'
    (`is_sane`) — gli STESSI del 1X2, con `favourites_only=False` perche' il
    lato favorito qui e' gia' selezionato sotto (mercato a 2 esiti).

    `playable=True` solo per il lato scelto (favorito di mercato, quota in
    fascia, edge/EV minimi, libro profondo a sufficienza): e' l'unico che la
    corsia d'ordine considerera'.
    """
    rows = list(quotes) if quotes is not None else _quotes_for(fixture_id)
    limit = int(max_lines if max_lines is not None else MAX_LINES_PER_MARKET)
    groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for row in rows:
        market_type, _key = _group_key(row)
        if market_type not in MARKETS or row.get("price") is None:
            continue
        groups.setdefault((market_type, _key), []).append(row)
    out: List[Dict[str, Any]] = []
    used: Dict[str, int] = {}
    for (market_type, _key), group in sorted(groups.items(),
                                             key=lambda item: _group_rank(item[1])):
        if used.get(market_type, 0) >= limit:
            continue
        line = group[0].get("line")
        if line is None:
            continue
        prices = {str(r.get("selection")): float(r["price"])
                  for r in group if r.get("price")}
        if len(prices) != 2:
            continue
        market = market_implied(prices)
        if not market:
            continue
        used[market_type] = used.get(market_type, 0) + 1
        depths = {str(r.get("selection")): float(r.get("liquidity") or 0.0)
                  for r in group}
        total_depth = sum(depths.values())
        market_ok = (total_depth >= MIN_DEPTH_USDC
                     and all(d >= MIN_LEG_DEPTH_USDC for d in depths.values()))
        cands: List[Dict[str, Any]] = []
        for selection, price in prices.items():
            probs = _model_side(market_type, lam_h, lam_a, float(line), selection)
            if probs is None:
                continue
            p_win, p_push, p_lose = probs
            market_prob = market.get(selection)
            # Probabilita' "efficace" (push = mezza vincita): e' la grandezza
            # confrontabile con la prob. fair devigata del mercato.
            model_eff = p_win + 0.5 * p_push
            prob = adjusted_probability(model_eff, market_prob, price,
                                        league=league)
            # EV ESATTO: il push restituisce lo stake (P/L 0), non e' una vincita.
            ev = p_win * (price - 1.0) - p_lose
            edge = (model_eff - market_prob) if market_prob is not None else None
            cands.append({
                "fixture_id": fixture_id, "market_type": market_type,
                "mercato": market_type, "line": float(line),
                "esito_key": ledger_esito(market_type, selection, line),
                "selection": selection,
                "quota": price, "price": price,
                "prob": prob, "prob_model": model_eff,
                "p_win": p_win, "p_push": p_push, "p_lose": p_lose,
                "market_prob": market_prob, "market_edge": edge, "ev": ev,
                "depth": depths.get(selection, 0.0),
                "total_depth": total_depth, "market_ok": market_ok,
                "league": league,
            })
        if len(cands) != 2:
            continue
        eligible = [c for c in cands
                    if c["market_prob"] is not None
                    and c["market_prob"] >= MIN_FAVOURITE_MARKET_PROB
                    and ODDS_MIN <= c["quota"] <= ODDS_MAX]
        chosen = max(eligible, key=lambda c: c["ev"]) if eligible else None
        for cand in cands:
            # `market=market_type`: i mercati liquidi (OU/AH) usano la soglia
            # EV dedicata del 04/10 (1.0%); il resto la soglia unica.
            sane, reason = is_sane(cand["prob"], cand["quota"], cand["ev"],
                                   market_prob=cand["market_prob"],
                                   league=league, favourites_only=False,
                                   market=market_type)
            depth_ok = (market_ok and cand["depth"] >= MIN_EXEC_DEPTH_USDC)
            if cand is chosen and sane and depth_ok:
                cand["tier"] = get_signal_tier(cand["ev"], cand["market_edge"])
                cand["status"] = cand["tier"]
                cand["playable"] = cand["status"] in PLAYABLE_STATUSES
            else:
                cand["playable"] = False
                cand["status"] = "rejected"
                cand["tier"] = "rejected"
                if not sane:
                    cand["reason"] = reason
                elif not depth_ok:
                    cand["reason"] = ("liquidita' insufficiente ("
                                      f"{cand['depth']:.1f} < "
                                      f"{MIN_EXEC_DEPTH_USDC:.1f} USDC al floor)")
                elif cand is not chosen:
                    cand["reason"] = "non e' il lato favorito della linea"
            out.append(cand)
    return out


# ---------------------------------------------------------------------------
# 3. LEDGER: candidati -> `predictions` (telemetria + settlement + calibrazione)
# ---------------------------------------------------------------------------

#: Quante righe del ledger multi-mercato leggere per la scansione. La tabella
#: e' potata (`prune_market_quotes`) e aggiornata in upsert: un tetto alto
#: basta e impedisce di leggere in memoria un volume patologico.
SCAN_LIMIT = _env_int("MM_SCAN_LIMIT", 5000)


def _window_iso(now: datetime) -> Tuple[str, str]:
    start = now.isoformat().replace("+00:00", "Z")
    end = (now + timedelta(hours=HOURS_AHEAD)).isoformat().replace("+00:00", "Z")
    return start, end


def _fixtures_with_quotes(now: datetime) -> List[Dict[str, Any]]:
    """Fixture con quote multi-mercato nella finestra (dal ledger, non da SX)."""
    try:
        from tracker import get_market_quotes
    except Exception as exc:
        logger.warning("multi_market: tracker non disponibile (%s)", exc)
        return []
    rows = get_market_quotes(limit=SCAN_LIMIT) or []
    start, end = _window_iso(now)
    out: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        market_type = str(row.get("market_type") or "").upper()
        if market_type not in MARKETS or row.get("price") is None:
            continue
        fixture_id = str(row.get("fixture_id") or "")
        if not fixture_id:
            continue
        kickoff = str(row.get("kickoff") or "")
        if not kickoff or not (start <= kickoff < end):
            continue
        entry = out.setdefault(fixture_id, {
            "id": fixture_id,
            "home": row.get("home") or "", "away": row.get("away") or "",
            "commence": kickoff, "league": row.get("league") or "",
        })
        if not entry["league"] and row.get("league"):
            entry["league"] = row["league"]
    return sorted(out.values(), key=lambda f: f["commence"])


def _ensure_match(fixture: Dict[str, Any]) -> None:
    """Riga in `matches` se manca (il ledger p_vuole una partita per il referto).

    Non SOVRASCRIVE una riga esistente: `save_match` fa INSERT OR REPLACE e
    riscrivere una partita gia' saldata (status/altri campi) sarebbe un danno
    silenzioso.
    """
    try:
        from tracker import _get_conn, save_match
        conn = _get_conn()
        row = conn.execute("SELECT 1 FROM matches WHERE id = ?",
                           (fixture["id"],)).fetchone()
        conn.close()
        if row:
            return
        save_match(fixture["id"], fixture.get("league") or "",
                   fixture.get("home") or "", fixture.get("away") or "",
                   fixture.get("commence"))
    except Exception as exc:
        logger.debug("multi_market: save_match %s saltata: %s",
                     fixture.get("id"), exc)


def _lam_for(match_id: str, home: str, away: str) -> Tuple[float, float]:
    """(lam_h, lam_a) del modello: dal ledger analisi, altrimenti dal motore."""
    try:
        from tracker import _get_conn
        conn = _get_conn()
        row = conn.execute("SELECT lam_h, lam_a FROM match_analysis "
                           "WHERE match_id = ? ORDER BY id DESC LIMIT 1",
                           (match_id,)).fetchone()
        conn.close()
        if row and row[0] is not None and row[1] is not None:
            return float(row[0]), float(row[1])
    except Exception:
        pass
    return expected_goals(home, away)


def _line_priceable(home: str, away: str, market_type: str,
                    line: float) -> bool:
    """True se l'oracolo prezza QUESTA linea (o se l'oracolo e' IGNOTO).

    Distinzione voluta: `oracle_lines` ritorna `None` quando la partita non e'
    in nessuna cache fresca (nessuna conclusione possibile) e un `set`
    quando la partita c'e'. Solo nel secondo caso un "assente" e' una
    informazione: la' il pick viene scartato, perche' sappiamo che l'oracolo
    non lo prezza. Con l'oracolo ignoto NON si blocca qui — la decisione
    definitiva resta al gate top-down di `auto_bet` (`linea`/`no_oracle`),
    cosi' il comportamento di oggi non cambia in peggio per una cache che
    arrivera' al prossimo fetch.
    """
    try:
        import pinnacle_oracle as po
        known = po.oracle_lines(home or "", away or "",
                                market_type=market_type)
    except Exception as exc:                                     # pragma: no cover
        logger.debug("multi_market: oracolo linee non disponibile (%s)", exc)
        return True
    if known is None:
        return True
    return any(abs(float(line) - ln) < 1e-6 for ln in known)


def _oracle_lines_for(fixture: Dict[str, Any],
                      market_type: str) -> Optional[Set[float]]:
    """Linee che l'oracolo prezza per la fixture (`None` = oracolo IGNOTO).

    Import PIGRO di `pinnacle_oracle` (che resta il padrone della lettura
    delle cache) e fail-open sull'errore: la corsia multi-mercato non deve
    poter fermare il giro per un problema dell'oracolo — la decisione
    definitiva resta al gate top-down, che e' fail-closed.
    """
    try:
        import pinnacle_oracle as po
        return po.oracle_lines(fixture.get("home") or "",
                               fixture.get("away") or "",
                               market_type=market_type)
    except Exception as exc:                                     # pragma: no cover
        logger.debug("multi_market: oracolo linee non disponibile (%s)", exc)
        return None


def _prefer_oracle_lines(fixture: Dict[str, Any], market_type: str,
                         groups: List[List[Dict[str, Any]]]
                         ) -> List[List[Dict[str, Any]]]:
    """Tra i gruppi giocabili, preferisce quelli che l'ORACOLO puo' prezzare.

    Fix linee (01/10/2026): SX quota molte linee (AH +0.5/+1/+1.5, OU
    1.5/2/2.5/3/3.5) mentre Pinnacle pubblica tipicamente la sola MAIN; un
    pick su una linea non prezzata non potra' mai diventare un ordine (il
    gate top-down risponde `linea`). Preferire le linee prezzabili evita di
    registrare come giocabile un candidato che non lo e'.

    CONSERVATIVO per costruzione: se l'oracolo e' IGNOTO per la partita (nessuna
    cache fresca) l'ordine resta quello di prima (`_group_rank`: linea main,
    poi liquidita'); se l'oracolo e' NOTO ma NESSUN gruppo e' prezzabile si
    torna comunque all'ordine di prima, senza scartare nulla — la telemetria
    del ledger resta completa e a impedire l'ordine ci pensa `live_picks`
    (fail-closed).
    """
    known = _oracle_lines_for(fixture, market_type)
    if not known:
        return groups
    preferred = [g for g in groups
                 if any(abs(float(c.get("line") or 0.0) - ln) < 1e-6
                        for c in g for ln in known)]
    return preferred or groups


def _ledger_rows(fixture: Dict[str, Any],
                 cands: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Righe da registrare in `predictions` (le sole linee che contano).

    Il ledger delle previsioni e' fatto per i segnali AZIONABILI: registrare
    ogni linea (fino a 12 x 2 per mercato) lo riempirebbe di migliaia di righe
    al giorno. Lo snapshot completo di tutti i mercati vive in
    `market_quotes`; qui entra la linea giocabile (coi suoi due lati, cosi' il
    tiering per-candidato resta confrontabile col 1X2) e, se nessuna linea e'
    giocabile, il solo candidato con l'EV piu' alto (diagnostica del perche').
    """
    by_group: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for cand in cands:
        by_group.setdefault((cand["mercato"], line_key(cand["line"])), []).append(cand)
    out: List[Dict[str, Any]] = []
    for market_type in MARKETS:
        groups = [g for (mt, _k), g in by_group.items() if mt == market_type]
        if not groups:
            continue
        playable = [g for g in groups if any(c.get("playable") for c in g)]
        if playable:
            out.extend(_prefer_oracle_lines(fixture, market_type,
                                            playable)[0])
        else:
            best = max(groups, key=lambda g: max(float(c["ev"]) for c in g))
            out.append(max(best, key=lambda c: float(c["ev"])))
    return out


def _persist(fixture: Dict[str, Any], cands: Sequence[Dict[str, Any]]) -> int:
    """Scrive i candidati scelti in `predictions` (fail-safe, mai un'eccezione)."""
    written = 0
    try:
        from tracker import save_prediction
        for cand in _ledger_rows(fixture, cands):
            save_prediction(fixture["id"], cand["mercato"], cand["esito_key"],
                            cand["quota"], cand["prob"], cand["ev"],
                            market_prob=cand.get("market_prob"),
                            market_edge=cand.get("market_edge"),
                            status=cand.get("status") or "rejected",
                            league=fixture.get("league") or "")
            written += 1
    except Exception as exc:
        logger.warning("multi_market: salvataggio previsioni %s: %s",
                       fixture.get("id"), exc)
    return written


def scan(provider: Any = None, *, ingest_quotes: bool = True,
         persist: bool = True, now: Optional[datetime] = None
         ) -> List[Dict[str, Any]]:
    """Un giro completo: ingest SX -> analisi -> ledger `predictions`.

    Ritorna i segnali giocabili trovati (forma compatta: e' la telemetria del
    giro). Fail-soft: una partita che esplode viene saltata, il giro continua.
    """
    when = now or datetime.now(timezone.utc)
    if ingest_quotes:
        ingest(provider, observed=when)
    saved: List[Dict[str, Any]] = []
    fixtures = _fixtures_with_quotes(when)
    for fixture in fixtures:
        try:
            quotes = _quotes_for(fixture["id"])
            if not quotes:
                continue
            _ensure_match(fixture)
            lam_h, lam_a = _lam_for(fixture["id"], fixture["home"],
                                    fixture["away"])
            cands = analyze_fixture(fixture["id"], lam_h, lam_a, quotes=quotes,
                                    league=fixture.get("league") or "")
            if not cands:
                continue
            if persist:
                _persist(fixture, cands)
            for cand in cands:
                if not cand.get("playable"):
                    continue
                saved.append({
                    "match_id": fixture["id"], "home": fixture["home"],
                    "away": fixture["away"], "commence": fixture["commence"],
                    "league": fixture.get("league") or "",
                    "mercato": cand["mercato"], "esito": cand["esito_key"],
                    "quota": cand["quota"], "ev": cand["ev"],
                    "status": cand["status"], "line": cand["line"],
                    "live": cand["mercato"] in live_markets(),
                })
        except Exception as exc:                     # partita ostile
            logger.warning("multi_market: analisi %s saltata (%s)",
                           fixture.get("id"), exc)
    live_n = sum(1 for s in saved if s.get("live"))
    logger.info("multi_market: %d fixture con quote, %d segnali giocabili "
                "(%d nelle corsie live %s)", len(fixtures), len(saved), live_n,
                ",".join(live_markets()) or "nessuna")
    return saved


# ---------------------------------------------------------------------------
# 4. CORSAIA ORDINI: pick aperti del ledger multi-mercato (interruttori live)
# ---------------------------------------------------------------------------

def live_picks(*, hours: float = HOURS_AHEAD,
               now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Pick OU/AH pronti per l'ordine, dai SOLI mercati accesi.

    Il gate di lega e la fascia quota sono RIPETUTI qui (difesa in profondita',
    come in `auto_bet._today_value_picks`): una riga storica scritta prima di
    un cambio di strategia non puo' diventare un ordine. FAIL-CLOSED sulla
    lega assente: senza sapere cosa si sta giocando non si ordina.
    """
    markets = live_markets()
    if not markets:
        return []
    when = now or datetime.now(timezone.utc)
    start = when.isoformat().replace("+00:00", "Z")
    end = (when + timedelta(hours=hours)).isoformat().replace("+00:00", "Z")
    try:
        from tracker import _get_conn
        conn = _get_conn()
        marks = ",".join("?" * len(markets))
        rows = conn.execute(
            f'''SELECT p.match_id, m.home_team, m.away_team, m.commence_time,
                       m.league, p.mercato, p.esito, p.quota, p.market_edge,
                       p.market_prob, p.ev, p.status
                  FROM predictions p JOIN matches m ON m.id = p.match_id
                 WHERE p.mercato IN ({marks})
                   AND p.status IN ('value','strong_value','moderate')
                   AND p.esito_finale IS NULL
                   AND m.commence_time >= ? AND m.commence_time < ?
                 ORDER BY p.ev DESC''',
            tuple(markets) + (start, end)).fetchall()
        conn.close()
    except Exception as exc:
        logger.warning("multi_market: lettura pick fallita (%s)", exc)
        return []
    out: List[Dict[str, Any]] = []
    for (match_id, home, away, commence, league, mercato, esito, quota,
         edge, market_prob, ev, status) in rows:
        league_name = (league or "").strip()
        if not league_name:
            logger.info("multi_market: skip %s %s (lega assente)",
                        match_id, esito)
            continue
        if not league_allowed(league_name):
            logger.info("multi_market: skip %s %s (lega '%s' fuori dai "
                        "campionati vincenti)", match_id, esito, league_name)
            continue
        try:
            price = float(quota)
        except (TypeError, ValueError):
            continue
        if price < ODDS_MIN or price > ODDS_MAX:
            logger.info("multi_market: skip %s %s (quota %.2f fuori fascia "
                        "%.2f-%.2f)", match_id, esito, price, ODDS_MIN, ODDS_MAX)
            continue
        if market_prob is not None and float(market_prob) < MIN_FAVOURITE_MARKET_PROB:
            continue
        target = order_target({"mercato": mercato, "esito_key": esito})
        if target is None or target.get("line") is None:
            logger.info("multi_market: skip %s %s (mercato/linea non "
                        "riconoscibili per l'ordine)", match_id, esito)
            continue
        # FIX linee (01/10/2026): un pick su una linea che l'oracolo NON prezza
        # non puo' diventare un ordine — il gate top-down risponderebbe `linea`
        # e il pick resterebbe in coda a ogni giro. Fail-closed, ma SOLO quando
        # l'oracolo e' NOTO: con la cache assente/stantia non si conclude nulla
        # e la decisione resta al gate a valle (che e' fail-closed pure lui).
        market_upper = str(mercato).upper()
        if not _line_priceable(home, away, market_upper, float(target["line"])):
            logger.info("multi_market: skip %s %s (l'oracolo non prezza la "
                        "linea %s del mercato %s)", match_id, esito,
                        target["line"], market_upper)
            continue
        out.append({
            "match_id": match_id, "home": home, "away": away,
            "commence": commence, "league": league_name,
            "mercato": str(mercato).upper(), "esito_key": esito,
            "esito_raw": esito, "quota": price, "price": price,
            "market_line": target["line"], "order_side": target["side"],
            "market_edge": float(edge) if edge is not None else None,
            "market_prob": float(market_prob) if market_prob is not None else None,
            "best_ev": float(ev) if ev is not None else 0.0,
            "status": status,
        })
    return out


def _empty_bucket() -> Dict[str, Any]:
    return {"open": 0, "closed": 0, "won": 0, "lost": 0, "push": 0,
            "other": 0, "profit": 0.0, "roi": None, "reliable": False}


def _bucket_add(bucket: Dict[str, Any], row: Dict[str, Any]) -> None:
    """Accumula una riga di ledger in un bucket. Mai un'eccezione."""
    if row.get("esito_finale") is None:
        bucket["open"] += 1
        return
    bucket["closed"] += 1
    verdict = str(row.get("esito_finale") or "").lower()
    if verdict in ("won", "lost", "push"):
        bucket[verdict] += 1
    else:
        # Verdetto inatteso (dato sporco): contato a parte invece di essere
        # fatto sparire dentro un "won"/"lost" che non e' avvenuto.
        bucket["other"] += 1
    try:
        bucket["profit"] += float(row.get("profit") or 0.0)
    except (TypeError, ValueError):
        pass


def _significance(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Blocco di significativita' statistica (`significance.py`), fail-safe.

    Il ROI di un campione di 8 chiusure non e' una misura: questo blocco dice
    se e' distinguibile da zero e quante chiusure servirebbero. Un errore qui
    non deve rompere il report: si degrada la singola sezione.
    """
    try:
        import significance
        return significance.evaluate(rows)
    except Exception as exc:
        logger.debug("multi_market: significativita' non calcolata: %s", exc)
        return {"status": "unavailable"}


def _significance_lines(block: Optional[Dict[str, Any]]) -> List[str]:
    """Righe di significativita' per il report (lista vuota se non c'e')."""
    try:
        import significance
        return significance.format_lines(block, indent="      ")
    except Exception:
        return []


def _finalize(bucket: Dict[str, Any]) -> Dict[str, Any]:
    """Chiude un bucket: arrotonda il profitto, calcola il ROI, dichiara il campione."""
    bucket["profit"] = round(float(bucket.get("profit") or 0.0), 4)
    if bucket.get("closed"):
        bucket["roi"] = round(bucket["profit"] / bucket["closed"], 4)
    bucket["reliable"] = (bucket.get("closed") or 0) >= MIN_RELIABLE_CLOSED
    return bucket


def _filter_line(rep: Dict[str, Any]) -> str:
    """Riga che DICHIARA il filtro applicato al report (o la sua assenza).

    Senza questa riga un report filtrato e uno completo sono indistinguibili:
    e' la stessa ragione per cui `excluded` e' dichiarato e mai nascosto.
    """
    f = rep.get("filter") or {}
    if not f.get("applied"):
        return ("Filtro: NESSUNO — mescola ere e strategie diverse "
                "(usare --since, es. --since 2026-09-19)")
    bits = []
    if f.get("since"):
        bits.append(f"era dal {f['since']}")
    lo, hi = f.get("odds_min"), f.get("odds_max")
    if lo is not None or hi is not None:
        bits.append(f"quota {lo if lo is not None else '-'}-"
                    f"{hi if hi is not None else '-'}")
    return (f"Filtro: {' | '.join(bits)} → {f.get('rows_kept', 0)} righe "
            f"tenute su {f.get('rows_total', 0)} "
            f"({f.get('rows_excluded', 0)} escluse dal filtro)")


def shadow_report(*, now: Optional[datetime] = None, since: Any = None,
                  odds_min: Optional[float] = None,
                  odds_max: Optional[float] = None) -> Dict[str, Any]:
    """Riepilogo della corsia multi-mercato (shadow OU + live AH), sola lettura.

    `since` (ISO, es. "2026-09-19") + `odds_min`/`odds_max` restringono il
    campione all'ERA strategica e alla FASCIA QUOTA correnti. Serve perche'
    il 25/09/2026 lo split per stato da solo si e' rivelato INSUFFICIENTE: il
    ROI aggregato dell'Over/Under (+21.21% su 30 chiuse) era portato per
    intero da una pipeline ritirata (22 righe della vecchia `fixture_engine`,
    quota media ~2.25, oggi `rejected`), mentre le 8 chiusure della strategia
    in produzione davano **-10.9%**. Filtrare per era e' l'unico modo di
    leggere un numero che corrisponda alla strategia che gira davvero.

    Il P/L e' separato per STATO del segnale, perche' le due popolazioni
    misurano cose diverse e sommarle da' un numero che non corrisponde a
    nessuna strategia:

    * **giocabili** (`PLAYABLE_STATUSES`: value / strong_value / moderate) =
      cio' che la corsia avrebbe giocato → il ROI REALE del campione;
    * **scartati** (`rejected`) = cio' che i gate hanno tagliato → il costo
      (o il risparmio) dei filtri, non una performance;
    * **altro** = stati non riconosciuti: contati a parte, mai fatti sparire.

    Sotto `MIN_RELIABLE_CLOSED` chiusure `reliable` e' False: un ROI su un
    campione piccolo e' rumore, e il report lo dichiara invece di lasciarlo
    leggere come una misura. Zero crediti: legge solo il ledger locale.
    """
    # La prontezza si misura UNA volta e il report ne deriva le corsie live:
    # leggere due volte (una per `live_markets`, una per il blocco) potrebbe
    # far comparire nel report una corsia diversa da quella che ordina.
    authorized = list(authorized_markets())
    readiness = ou_readiness()
    live_now = [m for m in authorized if m != "OU" or readiness.get("ready")]
    report: Dict[str, Any] = {
        "live_markets": live_now, "markets": {},
        "authorized_markets": authorized,
        "ou_readiness": readiness,
        "playable_statuses": list(PLAYABLE_STATUSES),
        "min_reliable_closed": MIN_RELIABLE_CLOSED,
    }
    rows_seen = rows_kept = 0
    for market_type in MARKETS:
        entry = _empty_bucket()
        entry["quotes"] = 0
        by_status: Dict[str, Dict[str, Any]] = {}
        playable = _empty_bucket()
        rejected = _empty_bucket()
        unclassified = _empty_bucket()
        # Righe giocabili conservate per la significativita': il ROI da solo
        # non dice se il campione e' giudicabile (28/09/2026).
        playable_rows: List[Dict[str, Any]] = []
        try:
            from tracker import get_predictions, filter_predictions
            rows_all = get_predictions(mercato=market_type, limit=5000)
            # Il filtro e' quello CONDIVISO del ledger (`tracker`): la stessa
            # "era" deve valere per il report shadow e per la diagnosi per
            # mercato, altrimenti le due misure non coincidono.
            rows = filter_predictions(rows_all, created_since=since,
                                      odds_min=odds_min, odds_max=odds_max)
            rows_seen += len(rows_all)
            rows_kept += len(rows)
        except Exception as exc:
            logger.debug("multi_market: report %s fallito: %s", market_type, exc)
            rows = []
        for row in rows:
            _bucket_add(entry, row)
            status = str(row.get("status") or "senza_stato").strip().lower()
            _bucket_add(by_status.setdefault(status, _empty_bucket()), row)
            if status in PLAYABLE_STATUSES:
                _bucket_add(playable, row)
                playable_rows.append(row)
            elif status == "rejected":
                _bucket_add(rejected, row)
            else:
                _bucket_add(unclassified, row)
        try:
            from tracker import get_market_quotes
            entry["quotes"] = len(get_market_quotes(market_type=market_type) or [])
        except Exception:
            pass
        report["markets"][market_type] = _finalize(entry)
        report["markets"][market_type]["by_status"] = {
            name: _finalize(bucket) for name, bucket in sorted(by_status.items())}
        report["markets"][market_type]["playable"] = _finalize(playable)
        report["markets"][market_type]["significance"] = _significance(playable_rows)
        report["markets"][market_type]["rejected"] = _finalize(rejected)
        report["markets"][market_type]["unclassified"] = _finalize(unclassified)
    report["filter"] = {
        "since": since, "odds_min": odds_min, "odds_max": odds_max,
        "applied": bool(since) or odds_min is not None or odds_max is not None,
        "rows_total": rows_seen, "rows_kept": rows_kept,
        "rows_excluded": max(0, rows_seen - rows_kept),
    }
    return report


def _counts(bucket: Dict[str, Any]) -> str:
    """'29 chiuse (17V/12P/0push), profitto +5.99/unita'' — difensivo."""

    def n(key: str) -> int:
        try:
            return int(bucket.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    txt = f"{n('closed')} chiuse ({n('won')}V/{n('lost')}P/{n('push')}push)"
    if n("other"):
        txt += f" di cui {n('other')} con verdetto inatteso"
    if n("open"):
        txt += f" + {n('open')} aperte"
    try:
        txt += f", profitto {float(bucket.get('profit') or 0.0):+.2f}/unita'"
    except (TypeError, ValueError):
        pass
    return txt


def _group_line(label: str, bucket: Dict[str, Any]) -> str:
    """Una riga per un gruppo di stati, con il campione dichiarato."""
    data = bucket or {}
    roi = data.get("roi")
    text = (f"    {label}: {_counts(data)} | ROI "
            f"{'n/d' if roi is None else f'{roi * 100:+.2f}%'}")
    try:
        closed = int(data.get("closed") or 0)
    except (TypeError, ValueError):
        closed = 0
    if closed and not data.get("reliable"):
        text += f"   ⚠️ campione < {MIN_RELIABLE_CLOSED} chiusure: rumore"
    return text


def format_report(data: Optional[Dict[str, Any]] = None) -> str:
    """Riepilogo leggibile (CLI/Telegram), mai un'eccezione.

    Giocabili e scartati su RIGHE SEPARATE: la riga del totale, da sola, e' un
    numero che non corrisponde a nessuna strategia (mescola cio' che sarebbe
    stato giocato con cio' che i filtri hanno rifiutato).
    """
    rep = data if isinstance(data, dict) else shadow_report()
    live = rep.get("live_markets") or []
    # Un report esterno/vecchio puo' non dichiarare l'autorizzazione: in quel
    # caso le corsie live SONO l'autorizzazione (nessuna informazione persa).
    authorized = rep.get("authorized_markets")
    authorized = list(authorized) if isinstance(authorized, (list, tuple)) \
        else list(live)
    readiness = rep.get("ou_readiness")
    readiness = readiness if isinstance(readiness, dict) else {}
    lines = ["📐 MULTI-MERCATO (OU/AH)",
             "Corsie LIVE: " + (", ".join(live) or "nessuna (tutto shadow)"),
             "Split per stato: giocabili "
             + "/".join(rep.get("playable_statuses") or PLAYABLE_STATUSES)
             + " | scartati rejected | soglia affidabilita' "
             + f"{rep.get('min_reliable_closed', MIN_RELIABLE_CLOSED)} chiusure",
             _filter_line(rep)]
    # Dichiarare il PERCHE' una corsia autorizzata non ordina: senza questa
    # riga "OU in shadow" e "OU non pronto" sono indistinguibili.
    if "OU" in authorized and "OU" not in live:
        lines.append("⏳ OU autorizzato ma NON pronto agli ordini: "
                     + str(readiness.get("reason")
                           or "prerequisito non soddisfatto"))
    for market_type, entry in (rep.get("markets") or {}).items():
        # Un riepilogo malformato (input esterno, JSON vecchio) non deve
        # rompere il report: si degrada la singola sezione, non il comando.
        try:
            lines.append(
                f"• {market_type}: {entry.get('quotes', 0)} quote sul ledger | "
                f"{entry.get('closed', 0)} chiuse + {entry.get('open', 0)} aperte")
            lines.append(_group_line("giocabili ", entry.get("playable") or {}))
            lines.extend(_significance_lines(entry.get("significance")))
            lines.append(_group_line("scartati  ", entry.get("rejected") or {}))
            unclassified = entry.get("unclassified") or {}
            if (unclassified.get("closed") or unclassified.get("open")):
                lines.append(_group_line("altro     ", unclassified))
            for status, bucket in (entry.get("by_status") or {}).items():
                if status in PLAYABLE_STATUSES and (bucket or {}).get("closed"):
                    lines.append(_group_line(f"  · {status}", bucket))
        except Exception as exc:
            logger.debug("multi_market: report %s malformato: %s", market_type, exc)
            lines.append(f"• {market_type}: dati non leggibili")
    return "\n".join(lines)


if __name__ == "__main__":                        # pragma: no cover
    import argparse
    import json as _json

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Multi-mercato OU/AH (SX Bet)")
    parser.add_argument("command", nargs="?", default="report",
                        choices=("ingest", "scan", "picks", "report", "btts",
                                 "ou"))
    parser.add_argument("--json", action="store_true")
    # Filtro d'ERA e di FASCIA QUOTA (25/09/2026): il report senza filtro
    # mescola la pipeline ritirata (< 19/09, quota media ~2.25) con quella in
    # produzione, e il ROI aggregato non corrisponde a nessuna strategia.
    parser.add_argument("--since", default=None, metavar="YYYY-MM-DD",
                        help="solo segnali NATI da questa data (es. 2026-09-19)")
    parser.add_argument("--odds-min", type=float, default=None,
                        help="quota minima (fascia corrente: 1.30)")
    parser.add_argument("--odds-max", type=float, default=None,
                        help="quota massima (fascia corrente: 1.80)")
    args = parser.parse_args()
    if args.command == "ingest":
        result = ingest()
        print(_json.dumps(result, indent=2, default=str) if args.json
              else f"ingest: {result}")
    elif args.command == "scan":
        found = scan()
        print(_json.dumps(found, indent=2, default=str) if args.json
              else f"scan: {len(found)} segnali giocabili")
    elif args.command == "picks":
        picks = live_picks()
        print(_json.dumps(picks, indent=2, default=str) if args.json
              else f"picks live: {len(picks)}")
    elif args.command == "btts":
        probes = probe_watched_markets()
        print(_json.dumps(probes, indent=2, default=str) if args.json
              else "sorveglianza mercati non modellati: " + format_probe(probes))
    elif args.command == "ou":
        # Quando l'OU partira' da solo: la soglia e il campione corrente,
        # senza toccare gli ordini (sola lettura del ledger).
        ready = ou_readiness()
        print(_json.dumps(ready, indent=2, default=str) if args.json
              else ("OU: PRONTO (ordini reali)" if ready.get("ready")
                    else "OU: NON pronto (shadow)") + f" — {ready.get('reason')}")
    else:
        data = shadow_report(since=args.since, odds_min=args.odds_min,
                             odds_max=args.odds_max)
        print(_json.dumps(data, indent=2, default=str) if args.json
              else format_report(data))
