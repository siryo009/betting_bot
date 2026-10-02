"""decision/market.py — Il contratto di mercato: una quota valida, o niente.

Perche' esiste: finora una quota entrava nel sistema come **dict anonimo** —
chiavi diverse per provider diversi, tipi non controllati, timestamp a volte
senza fuso orario (il caso classico: il punteggio di una partita in corso usato
come finale, vedi `STALE_INPLAY_HOURS`). L'errore si scopriva a valle, quando
costava: un CLV calcolato su un timestamp ambiguo, un confronto fra quote in
unita' diverse, un gate di liquidita' che leggeva una stringa.

Qui la quota diventa un **tipo**: `MarketQuote`. Chi la produce (feed SX Bet,
the-odds-api, un import manuale) deve rispettare il contratto; chi la consuma
riceve campi garantiti, normalizzati e confrontabili.

**Schema 2.0 — multi-mercato (18/09/2026).** Il sistema non si limita piu' al
1X2: una stessa partita porta PIU' mercati, e alcuni di questi hanno una linea.
Il contratto lo rappresenta con tre aggiunte sostanziali:

    market_type   tipo tipizzato (1X2, OU, AH, BTTS, DC, CS) — il registro
                  `MARKET_SPECS` dice per ognuno esiti ammessi, linee,
                  sorgente nativa e da cosa si deriva;
    line          la linea (2.5 / -0.75), OBBLIGATORIA sui mercati che ne
                  hanno una e VIETATA sugli altri: un OU senza linea sarebbe
                  indistinguibile da un OU 2.5;
    origin        "native" (la pubblica un book) o "derived" (la produce il
                  sistema) — e una quota derivata DEVE dichiarare `derived_from`.

Le 1X2 e l'OU non esauriscono SX Bet: la doc ufficiale
(`docs.sx.bet/api-reference/market-types`) elenca oltre 30 tipi, con la colonna
"Has lines". Il registro tiene i type id NATIVI (1 = 1X2, 2 = OU, 3 = AH,
17 = BTTS, 28/342/226 = la FAMIGLIA "including overtime" di OU/AH/12, vedi
sotto) e dichiara esplicitamente i tipi osservati vivi ma non modellati
(`SX_TYPES_NOT_MODELLED`: 52, 835, ...) e i mercati che su SX NON esistono
(Double Chance, Risultato Esatto: derivati, non inventati).

**La famiglia "including overtime" (01/10/2026).** I tipi SX 28
("Under/Over Including Overtime", con linea), 342 ("Asian Handicap Including
Overtime", con linea) e 226 ("12 Including Overtime", senza linea) sono le
varianti con supplementari dei mercati base, e sono i tipi che SX pubblica sui
NON-calcio (Basketball sportId 1, American Football sportId 8: misurato il
01/10/2026 su `GET /markets/active`). Sono tipi canonici SEPARATI e non un
flag su OU/AH: la chiave del ledger e' `(fixture_id, market_type, line_key,
selection)`, quindi un OU 2.5 e un OU-inclusi-supplementari 2.5 collasserebbero
sulla STESSA riga — lo stesso errore che il 18/09 si e' evitato fra linee
diverse. Marcare lo sport e' compito del feed (`home`/`away`/`league`), non del
tipo di mercato.

I campi chiave (tutti obbligatori):

    schema_version  versione del contratto dichiarata DAL PRODUTTORE
    event_id        identificativo dell'evento / della partita (fixture_id)
    market          mercato canonico, normalizzato dagli alias del provider
    selection       esito canonico del mercato
    odds            quota decimale europea, **minimo 0.1**
    timestamp       istante della rilevazione (obbligatoriamente UTC aware)
    source          chi ha prodotto la quota (provider)
    gateway_id      quale gateway l'ha ingerita (tracciabilita' all'ingresso)

Cosa valida il contratto e cosa NO (la separazione conta):

- **Struttura**: campi presenti, tipi, quote finite e >= 0.1, timestamp con
  fuso orario, coerenza mercato/esito (`1X2` + `over` = rifiuto), regole del
  registro (linea dove serve, vietata dove non serve, passi di 0.25),
  provenienza delle quote derivate, versione dello schema supportata. Tutto QUI.
- **Strategia**: fascia quote 1.30-1.80, EV minimo, edge minimo, cap di stake.
  Tutto nel **Risk Engine** (`decision/risk_engine.py`), che legge le soglie da
  `value_filter`/`market_calib`. Il contratto non conosce la strategia: se la
  conoscesse, ogni cambio di soglia sarebbe un cambio di schema.

Come si valida all'ingresso:

    quote = parse_quote(row, gateway_id="sx-feed")         # strict: solleva
    batch = validate_batch(rows, gateway_id="sx-feed")     # mai un'eccezione
    multi = validate_fixture_quotes(rows, gateway_id="sx-feed")   # multi-mercato

`parse_quote` solleva `MarketQuoteError` (con `issues` machine-readable) e
**logga ogni errore**: una riga `logger.error` per diagnostica umana piu' un
evento JSON `market.quote_rejected` sul sink di osservabilita', con
`error_code`, campo colpevole e gateway. `validate_batch` fa la stessa cosa in
modo non bloccante (accettate + respinte + conteggi per codice) per gli import
a lotti: una riga rotta non deve fermare le altre. `validate_fixture_quotes`
aggiunge il raggruppamento per partita (`FixtureQuotes`), che verifica le
invarianti di gruppo: un fixture con quote incoerenti viene respinto INTERO.

Persistenza: `MarketQuote.as_row()` produce la riga flat descritta da
`MARKET_ROW_FIELDS` (JSON-safe), che e' il contratto verso il gateway SQLite —
una riga per quota, chiave composta `fixture_id + market_type + line_key +
selection`.

Regole del modulo:

1. **Purezza**: solo pydantic + stdlib + il middleware. Nessun import di
   `tracker`/`auto_bet`/provider a livello di modulo.
2. **Fail-closed sulla struttura, fail-open sulla telemetria**: un dato ambiguo
   viene respinto; un sink di log rotto non ferma l'ingresso.
3. **Nessun payload intero nei log**: solo nome del campo e valore troncato
   (`TRUNCATE`, 80 char). Un feed non deve poter scrivere segreti nei log.
4. **Nessun indovinello**: gli alias di chiave/mercato/selezione sono tabelle
   esplicite; cio' che non e' in tabella e' un rifiuto, mai una scelta silenziosa.
   Vale anche per i mercati: un tipo che SX non pubblica NON viene derivato in
   silenzio (serve `origin="derived"` + `derived_from`).
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable, Mapping, Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from .middleware import Observability, TraceContext

logger = logging.getLogger("decision.market")

#: Versione del contratto. Cambia quando cambia la FORMA (campi obbligatori,
#: unita' di misura, semantica di un campo), non quando cambiano le soglie.
#:
#: 1.0 -> 2.0 (18/09/2026): il contratto rappresenta PIU' mercati dello stesso
#: evento (1X2, OU, AH, BTTS, DC, CS) e quindi anche la LINEA. La 1.0 resta
#: SUPPORTATA in lettura: i produttori vecchi (e le righe gia' sul ledger)
#: continuano a essere validi (mercato 1X2/OU senza linea).
MARKET_SCHEMA_VERSION = "2.0"
SUPPORTED_SCHEMA_VERSIONS = (MARKET_SCHEMA_VERSION, "1.0")

#: Quota minima ammessa dal contratto (decimale europeo, 1.0 = pari).
MIN_ODDS = 0.1

#: Lunghezza massima del valore riportato nei log (mai il payload intero).
TRUNCATE = 80

#: Eventi di rifiuto emessi da `validate_batch` prima di passare al solo
#: riepilogo: un lotto di 500 righe rotte non deve scrivere 500 eventi.
DEFAULT_MAX_REJECTION_EVENTS = 20

#: Chiavi accettate al posto dei nomi canonici (i feed usano nomi propri).
#: Il nome canonico VINCE sempre: nessuna sovrascrittura silenziosa.
KEY_ALIASES = {
    "eventid": "event_id",
    "event": "event_id",
    "matchid": "event_id",
    "match_id": "event_id",
    "fixture_id": "event_id",
    "sportxeventid": "event_id",
    "markettype": "market_type",
    "market_type": "market_type",
    "market_name": "market",
    "marketkey": "market",
    "line_value": "line",
    "handicap": "line",
    "total_line": "line",
    "mainline": "main_line",
    "is_main_line": "main_line",
    "outcome": "selection",
    "selectionid": "selection",
    "selection_id": "selection",
    "side": "selection",
    "odd": "odds",
    "price": "odds",
    "quote": "odds",
    "decimal_odds": "odds",
    "ts": "timestamp",
    "time": "timestamp",
    "updated_at": "timestamp",
    "observed_at": "timestamp",
    "gateway": "gateway_id",
    "gatewayid": "gateway_id",
    "feed": "gateway_id",
    "provider": "source",
    "bookmaker": "source",
    "version": "schema_version",
    "schemaversion": "schema_version",
}

#: Alias dei mercati -> mercato canonico (chiavi normalizzate).
#: Le chiavi sono normalizzate con `_norm_key` (minuscole, senza separatori),
#: quindi 'Match Odds' == 'match_odds' == 'match-odds' senza doppie voci.
MARKET_ALIASES = {
    # Famiglia "including overtime" (SX 28/342/226): i nomi arrivano dai feed
    # che scrivono l'etichetta per esteso invece del codice canonico.
    "ou_ot": "OU_OT", "totals_ot": "OU_OT", "over_under_ot": "OU_OT",
    "over_under_including_overtime": "OU_OT",
    "ah_ot": "AH_OT", "spread_ot": "AH_OT",
    "asian_handicap_including_overtime": "AH_OT",
    "ml_ot": "ML_OT", "moneyline_ot": "ML_OT",
    "12_including_overtime": "ML_OT", "moneyline_including_overtime": "ML_OT",
    "1x2": "1X2",
    "12": "1X2",
    "h2h": "1X2",
    "headtohead": "1X2",
    "match_odds": "1X2",
    "matchwinner": "1X2",
    "moneyline": "1X2",
    "money_line": "1X2",
    "winner": "1X2",
    "ou": "OU",
    "o/u": "OU",
    "overunder": "OU",
    "over_under": "OU",
    "total": "OU",
    "totals": "OU",
    "ah": "AH",
    "asianhandicap": "AH",
    "handicap": "AH",
    "handicapasiatico": "AH",
    "spread": "AH",
    "spreads": "AH",
    "btts": "BTTS",
    "golgol": "BTTS",
    "bothteamstoscore": "BTTS",
    "bothscore": "BTTS",
    "dc": "DC",
    "doublechance": "DC",
    "doppiachance": "DC",
    "cs": "CS",
    "correctscore": "CS",
    "exactscore": "CS",
    "risultatoesatto": "CS",
}

#: Alias degli esiti -> esito canonico (chiavi normalizzate).
SELECTION_ALIASES = {
    "1": "1", "home": "1", "h": "1", "casa": "1", "team1": "1", "t1": "1",
    "x": "X", "draw": "X", "tie": "X", "pareggio": "X", "d": "X",
    "2": "2", "away": "2", "a": "2", "trasferta": "2", "team2": "2", "t2": "2",
    "over": "over", "o": "over", "piu": "over",
    "under": "under", "u": "under", "meno": "under",
    # BTTS (type 17 su SX: "Both Teams To Score")
    "yes": "yes", "si": "yes", "gol": "yes", "golgol": "yes",
    "no": "no", "nogoal": "no",
    # Double Chance (mercato DERIVATO: non esiste un book nativo su SX)
    "1x": "1X", "homedraw": "1X", "homeordraw": "1X", "casaopareggio": "1X",
    "x2": "X2", "drawaway": "X2", "draworaway": "X2", "pareggiootrasferta": "X2",
    "12": "12", "homeaway": "12", "homeoraway": "12",
}


# ---------------------------------------------------------------------------
# MULTI-MERCATO: il registro dei tipi (schema 2.0)
#
# Perche' un registro e non un `if` sparso: ogni mercato ha una FORMA diversa —
# chi ha linee e chi no, quali esiti ammette, quali sorgenti lo pubblicano, se
# esiste o va derivato. Tenere queste regole in un unico tavola (dati, non
# codice) significa che aggiungere un mercato e' una riga, e che chi valida
# non deve conoscerne le eccezioni.
#
# **Convenzione degli esiti** (allineata al ledger del progetto):
#
#   1X2   selezioni 1 / X / 2
#   12    (non modellato: vedi SX_TYPES_NOT_MODELLED)
#   OU    over / under  + `line` (es. 2.5)
#   AH    1 / 2 (teamOne / teamTwo) + `line` dal punto di vista di teamOne
#   BTTS  yes / no
#   DC    1X / X2 / 12  (derivato, non nativo)
#   CS    <golCasa>-<golTrasferta> (es. "3-1"; derivato dalla Poisson)
#
# La `selection` resta la chiave MACHINE (stabile, deduplicabile); la resa
# leggibile per il ledger la produce `MarketQuote.ledger_esito` ('Home -0.75',
# 'Over 2.5', 'Yes'), che e' esattamente il formato che `tracker`/`ml_audit`
# gia' usano per AH e BTTS.
# ---------------------------------------------------------------------------

class MarketType(str, Enum):
    """I mercati che il sistema sa rappresentare (schema 2.0)."""

    MATCH_RESULT = "1X2"
    OVER_UNDER = "OU"
    ASIAN_HANDICAP = "AH"
    BOTH_TEAMS_TO_SCORE = "BTTS"
    DOUBLE_CHANCE = "DC"
    CORRECT_SCORE = "CS"
    # -- famiglia "including overtime" (SX 28/342/226) ----------------------
    # Stessa forma dei mercati base (esiti, linee), dominio NON calcistico:
    # sono i tipi che SX pubblica su Basketball/American Football. Tipi
    # canonici a se' stanti perche' la chiave del ledger include il tipo: un
    # OU 220.5 (tempo regolamentare) e un OU-inclusi-supplementari 220.5 sono
    # due mercati diversi e non devono sovrascriversi.
    OVER_UNDER_OT = "OU_OT"
    ASIAN_HANDICAP_OT = "AH_OT"
    MONEYLINE_OT = "ML_OT"


class MarketTypeSpec(BaseModel):
    """Le regole di UN mercato: cosa ammette e chi lo pubblica (immutabile)."""

    model_config = ConfigDict(frozen=True)

    market_type: MarketType
    label: str
    #: Esiti canonici ammessi. Vuoto = "validato da regola" (vedi `
    #: requires_score`): il Risultato Esatto non e' enumerabile in una tupla.
    selections: tuple[str, ...] = ()
    #: Il mercato comporta una linea (handicap/totale)?
    has_lines: bool = False
    #: La linea puo' essere quartata (x.25 / x.75)
    quarter_line_eligible: bool = False
    #: Limiti di sanity della linea (estremi inclusi)
    line_bounds: Optional[tuple[float, float]] = None
    #: True se un book NATIVO lo pubblica (False = va derivato)
    native: bool = True
    #: Type id nativo per sorgente (SX Bet: vedi `SX_TYPE_IDS`)
    source_type_ids: tuple[tuple[str, int], ...] = ()
    #: Da quali mercati si puo' derivare (obbligatorio per i non nativi)
    derivable_from: tuple[MarketType, ...] = ()
    #: L'esito e' un punteggio ("3-1") e va validato come tale
    requires_score: bool = False
    note: str = ""

    @property
    def line_required(self) -> bool:
        return self.has_lines


#: IL registro. Fonte delle regole: docs.sx.bet/api-reference/market-types
#: (letta il 18/09/2026) + probe REALE su GET /markets/active (sportIds=5).
MARKET_SPECS: dict[MarketType, MarketTypeSpec] = {
    MarketType.MATCH_RESULT: MarketTypeSpec(
        market_type=MarketType.MATCH_RESULT, label="Esito finale (1X2)",
        selections=("1", "X", "2"), source_type_ids=(("sxbet", 1),),
        note="SX: 3 mercati binari 'X vs Not X' per evento"),
    MarketType.OVER_UNDER: MarketTypeSpec(
        market_type=MarketType.OVER_UNDER, label="Totale gol Over/Under",
        selections=("over", "under"), has_lines=True,
        quarter_line_eligible=True, line_bounds=(0.5, 12.0),
        source_type_ids=(("sxbet", 2),),
        note="SX: piu' linee per evento (mainLine segnala la principale)"),
    MarketType.ASIAN_HANDICAP: MarketTypeSpec(
        market_type=MarketType.ASIAN_HANDICAP, label="Asian Handicap",
        selections=("1", "2"), has_lines=True,
        quarter_line_eligible=True, line_bounds=(-12.0, 12.0),
        source_type_ids=(("sxbet", 3),),
        note="line dal punto di vista di teamOne (positiva = teamOne riceve)"),
    MarketType.BOTH_TEAMS_TO_SCORE: MarketTypeSpec(
        market_type=MarketType.BOTH_TEAMS_TO_SCORE, label="Gol/Niente Gol (BTTS)",
        selections=("yes", "no"), source_type_ids=(("sxbet", 17),),
        note="type 17 esiste ma sul calcio risultava NON popolato al "
             "18/09/2026: il feed deve dirlo, non inventarlo"),
    MarketType.DOUBLE_CHANCE: MarketTypeSpec(
        market_type=MarketType.DOUBLE_CHANCE, label="Doppia chance",
        selections=("1X", "X2", "12"), native=False,
        derivable_from=(MarketType.MATCH_RESULT,),
        note="non nativo su SX: derivabile dal 1X2 devigato"),
    MarketType.CORRECT_SCORE: MarketTypeSpec(
        market_type=MarketType.CORRECT_SCORE, label="Risultato esatto",
        native=False, requires_score=True,
        derivable_from=(MarketType.MATCH_RESULT, MarketType.OVER_UNDER),
        note="non nativo su SX: derivato dalla distribuzione di Poisson"),
    # -- famiglia "including overtime" (SX 28/342/226) ----------------------
    # Limiti di linea LARGHI di proposito: questi mercati vivono su sport dove
    # i punteggi non sono gol (basket ~150-260, basket spread fino a ~30, NFL
    # ~35-60). I bounds di OU/AH (0.5..12) li respingerebbero TUTTI: il
    # contratto serve anche la telemetria, e una riga rifiutata per un limite
    # calcistico non e' un dato corretto, e' un dato perso.
    MarketType.OVER_UNDER_OT: MarketTypeSpec(
        market_type=MarketType.OVER_UNDER_OT,
        label="Totale punti Over/Under (inclusi supplementari)",
        selections=("over", "under"), has_lines=True,
        quarter_line_eligible=True, line_bounds=(0.5, 500.0),
        source_type_ids=(("sxbet", 28),),
        note="SX 28 'Under/Over Including Overtime': non calcistico "
             "(basket/NFL). Quarter-line ammessa dalla doc ufficiale"),
    MarketType.ASIAN_HANDICAP_OT: MarketTypeSpec(
        market_type=MarketType.ASIAN_HANDICAP_OT,
        label="Asian Handicap (inclusi supplementari)",
        selections=("1", "2"), has_lines=True,
        quarter_line_eligible=False, line_bounds=(-100.0, 100.0),
        source_type_ids=(("sxbet", 342),),
        note="SX 342 'Asian Handicap Including Overtime'. La doc NON elenca "
             "342 fra i tipi quarter-line eligible (2, 3, 28): una linea "
             "quartata qui e' respinta, come dichiarato dalla fonte"),
    MarketType.MONEYLINE_OT: MarketTypeSpec(
        market_type=MarketType.MONEYLINE_OT,
        label="Testa a testa 2 esiti (inclusi supplementari)",
        selections=("1", "2"), source_type_ids=(("sxbet", 226),),
        note="SX 226 '12 Including Overtime': moneyline SENZA pareggio "
             "(basket/NFL). Su SX esiste anche il 52 '12' senza supplementari: "
             "NON modellato (usato dalle corsie tennis/eSports via type id)"),
}

#: Mercati canonici ammessi dal contratto (derivato dal registro: mai a mano).
SUPPORTED_MARKETS = tuple(spec.market_type.value for spec in MARKET_SPECS.values())

#: Selezione ammessa per ogni mercato canonico (validazione INCROCIATA): e' il
#: controllo che a valle costa di piu' — un esito che non appartiene al mercato
#: e' il modo in cui un `over` finisce saldato su un 1X2 (vedi il caso
#: 'Blackburn Rovers' del 09/09, chiuso come Over 2.5). Il Risultato Esatto
#: non compare: i suoi esiti li valida `_validate_score`.
MARKET_SELECTIONS: dict[str, tuple[str, ...]] = {
    spec.market_type.value: spec.selections
    for spec in MARKET_SPECS.values() if spec.selections
}

#: Type id ufficiali SX Bet -> mercato canonico (docs.sx.bet, 18/09/2026;
#: famiglia "including overtime" aggiunta il 01/10/2026 dalla stessa doc e
#: confermata dal probe reale su /markets/active di Basketball/American
#: Football: tipi osservati vivi 28, 342, 226).
SX_TYPE_IDS: dict[int, MarketType] = {
    1: MarketType.MATCH_RESULT,
    2: MarketType.OVER_UNDER,
    3: MarketType.ASIAN_HANDICAP,
    17: MarketType.BOTH_TEAMS_TO_SCORE,
    28: MarketType.OVER_UNDER_OT,
    226: MarketType.MONEYLINE_OT,
    342: MarketType.ASIAN_HANDICAP_OT,
}

#: Type id SX che portano linee (colonna "Has lines" della doc ufficiale) e
#: quali sono quarter-line eligible: servono al feed per decidere se chiedere
#: `onlyMainLine` e come normalizzare la linea.
SX_LINE_BEARING_TYPES = (2, 3, 28, 29, 166, 201, 342, 835, 1536, 165, 866,
                          53, 77, 21, 64, 45, 65, 46, 66, 236, 281)
SX_QUARTER_LINE_TYPES = (2, 3, 28)

#: Type id SX OSSERVATI VIVI sul calcio ma NON modellati dal contratto.
#: Restano qui dichiarati perche' "non modellato" non vuol dire "inesistente":
#: chi legge il codice deve sapere cosa manca e perche' (e non ri-scoprirlo).
#: Verificato il 18/09/2026 su /markets/active (sportIds=5):
#:   1 -> 1X2 (100 mercati), 2 -> OU (100), 3 -> AH (100), 52 -> 12 (100),
#:   17 -> BTTS (0 attivi), 53/63/77/226/835 -> 0 attivi.
#: ⚠️ 226 e' USCITO da qui il 01/10/2026: e' modellato (`MONEYLINE_OT`),
#: insieme a 28 e 342 (famiglia "including overtime", sport non calcistici).
SX_TYPES_NOT_MODELLED: dict[int, str] = {
    52: "12 (senza pareggio) — usato dalle corsie tennis/eSports via type id",
    835: "Asian Under/Over",
    77: "Under/Over primo tempo",
    63: "12 primo tempo",
}


def spec_for(market: Any) -> Optional[MarketTypeSpec]:
    """Spec del mercato (accetta valore canonico, alias o `MarketType`)."""
    market_type = market_type_of(market)
    return MARKET_SPECS.get(market_type) if market_type else None


def market_type_of(value: Any) -> Optional[MarketType]:
    """`MarketType` da un valore qualsiasi (alias inclusi), None se ignoto."""
    if isinstance(value, MarketType):
        return value
    key = _alias_key(value)
    if key is None:
        return None
    canonical = _MARKET_LOOKUP.get(_norm_key(key))
    if canonical is None:
        candidate = key.upper()
        if candidate in SUPPORTED_MARKETS:
            canonical = candidate
        else:
            return None
    try:
        return MarketType(canonical)
    except ValueError:
        return None


def line_required(market: Any) -> bool:
    """Il mercato ESIGE una linea? (regola del registro, mai duplicata a mano)"""
    spec = spec_for(market)
    return bool(spec and spec.line_required)


def market_accepts_lines(market: Any) -> bool:
    """Il mercato TOLLERA una linea? (per i mercati senza linee una linea e' un dato incoerente)"""
    spec = spec_for(market)
    return bool(spec and spec.has_lines)


class QuoteErrorCode(str, Enum):
    """Perche' una quota e' stata respinta (machine-readable, aggregabile)."""

    MISSING_FIELD = "missing_field"
    EMPTY_FIELD = "empty_field"
    INVALID_TYPE = "invalid_type"
    UNSUPPORTED_SCHEMA = "unsupported_schema"
    UNKNOWN_MARKET = "unknown_market"
    UNKNOWN_MARKET_TYPE = "unknown_market_type"
    MARKET_TYPE_MISMATCH = "market_type_mismatch"
    UNKNOWN_SELECTION = "unknown_selection"
    SELECTION_MARKET_MISMATCH = "selection_market_mismatch"
    INVALID_SCORE = "invalid_score"
    ODDS_BELOW_MIN = "odds_below_min"
    ODDS_NOT_FINITE = "odds_not_finite"
    TIMESTAMP_NAIVE = "timestamp_naive"
    # -- multi-mercato (schema 2.0) --------------------------------------
    LINE_REQUIRED = "line_required"
    LINE_NOT_ALLOWED = "line_not_allowed"
    LINE_INVALID = "line_invalid"
    UNKNOWN_ORIGIN = "unknown_origin"
    DERIVED_MISSING_SOURCE = "derived_missing_source"
    FIXTURE_MISMATCH = "fixture_mismatch"


#: Origini ammesse di una quota: nativa (la pubblica un book) o derivata (la
#: produce il sistema da altri mercati). Una quota derivata DEVE dichiarare da
#: dove viene (`derived_from`): senza provenienza non e' verificabile.
QUOTE_ORIGINS = ("native", "derived")


#: Tipi di errore di pydantic -> codice del contratto (i validator usano il
#: formato "<codice>: <dettaglio>", questo e' il ripiego per gli altri).
_PYDANTIC_CODES = {
    "missing": QuoteErrorCode.MISSING_FIELD,
    "greater_than_equal": QuoteErrorCode.ODDS_BELOW_MIN,
    "less_than": QuoteErrorCode.INVALID_TYPE,
    "string_type": QuoteErrorCode.INVALID_TYPE,
    "int_type": QuoteErrorCode.INVALID_TYPE,
    "float_type": QuoteErrorCode.INVALID_TYPE,
    "float_parsing": QuoteErrorCode.INVALID_TYPE,
    "datetime_type": QuoteErrorCode.INVALID_TYPE,
    "datetime_parsing": QuoteErrorCode.INVALID_TYPE,
    "string_too_short": QuoteErrorCode.EMPTY_FIELD,
}

_CODE_RE = re.compile("|".join(sorted((code.value for code in QuoteErrorCode),
                                      key=len, reverse=True)))
_ISO_Z = re.compile(r"[Zz]$")
_NOT_ALNUM = re.compile(r"[^a-z0-9]")


def _norm_key(value: Any) -> str:
    """Chiave di confronto degli alias: minuscola, senza separatori.

    Cosi' 'Match Odds', 'match_odds' e 'match-odds' sono la stessa cosa senza
    dover elencare ogni variante nella tabella.
    """
    return _NOT_ALNUM.sub("", str(value).strip().lower())


#: Tabelle di alias con la chiave NORMALIZZATA (una sola forma di confronto).
_MARKET_LOOKUP = {_norm_key(key): value for key, value in MARKET_ALIASES.items()}
_SELECTION_LOOKUP = {_norm_key(key): value for key, value in SELECTION_ALIASES.items()}


# ---------------------------------------------------------------------------
# Problem reporting
# ---------------------------------------------------------------------------

class QuoteIssue(BaseModel):
    """Un problema rilevato all'ingresso (campo + motivo, mai prosa libera)."""

    code: QuoteErrorCode
    field: str = ""
    detail: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code.value, "field": self.field, "detail": self.detail}

    def __str__(self) -> str:
        where = f" [{self.field}]" if self.field else ""
        return f"{self.code.value}{where}: {self.detail}"


class MarketQuoteError(ValueError):
    """Contratto di mercato violato. Porta TUTTI i problemi, non solo il primo."""

    def __init__(self, issues: Iterable[QuoteIssue], *, gateway_id: str = "",
                 source: str = "", raw_keys: Optional[Iterable[str]] = None) -> None:
        self.issues = list(issues)
        self.gateway_id = gateway_id
        self.source = source
        self.raw_keys = list(raw_keys or [])
        codes = ", ".join(sorted({issue.code.value for issue in self.issues}))
        super().__init__(f"contratto di mercato non valido ({len(self.issues)} "
                         f"problemi: {codes})")

    def codes(self) -> list[str]:
        return [issue.code.value for issue in self.issues]


class QuoteRejection(BaseModel):
    """Rifiuto registrato da `validate_batch` (una riga per problema)."""

    index: int = 0
    code: QuoteErrorCode
    field: str = ""
    detail: str = ""
    event_id: str = ""
    source: str = ""
    gateway_id: str = ""
    raw_keys: list[str] = Field(default_factory=list)

    @classmethod
    def from_issue(cls, issue: QuoteIssue, *, index: int = 0, event_id: str = "",
                   source: str = "", gateway_id: str = "",
                   raw_keys: Optional[Iterable[str]] = None) -> "QuoteRejection":
        return cls(index=index, code=issue.code, field=issue.field,
                   detail=issue.detail, event_id=event_id, source=source,
                   gateway_id=gateway_id, raw_keys=list(raw_keys or []))

    def as_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


# ---------------------------------------------------------------------------
# Il contratto
# ---------------------------------------------------------------------------

class MarketQuote(BaseModel):
    """Una quota che rispetta il contratto: tipi garantiti, unita' dichiarate.

    `extra="allow"`: i campi sconosciuti del feed NON vengono persi (restano in
    `extra_fields`), cosi' si puo' ispezionare cosa manda davvero un provider
    senza allargare il contratto.
    """

    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)

    # -- identita' del contratto -----------------------------------------
    schema_version: str = Field(..., description="versione dello schema dichiarata dal produttore")
    event_id: str = Field(..., description="identificativo dell'evento (fixture)")
    market: str = Field(..., description=f"mercato canonico ({', '.join(SUPPORTED_MARKETS)})")
    selection: str = Field(..., description="esito canonico del mercato")
    odds: float = Field(..., ge=MIN_ODDS, allow_inf_nan=False,
                        description=f"quota decimale europea (minimo {MIN_ODDS})")
    timestamp: datetime = Field(..., description="istante della rilevazione (UTC)")
    source: str = Field(..., description="provider che ha prodotto la quota")
    gateway_id: str = Field(..., description="gateway che ha ingerito la quota")

    # -- multi-mercato (schema 2.0) --------------------------------------
    #: Tipo tipizzato. E' la STESSA cosa di `market` (forma stringa): si
    #: completano a vicenda e non possono contraddirsi (vedi `_resolve_market`).
    market_type: Optional[MarketType] = Field(
        None, description="tipo di mercato (1X2, OU, AH, BTTS, DC, CS)")
    #: Linea del mercato: obbligatoria per i mercati che ne hanno una (OU, AH),
    #: vietata sugli altri. Per l'AH e' dal punto di vista di teamOne.
    line: Optional[float] = Field(None, description="linea (handicap/totale)")
    #: La linea principale dichiarata dalla fonte (nulla se la fonte non lo dice)
    main_line: Optional[bool] = Field(None, description="linea principale per la fonte")
    #: Provenienza: "native" (book reale) o "derived" (prodotta dal sistema)
    origin: str = Field("native", description="native | derived")
    #: Da quali mercati e' stata derivata (obbligatorio se `origin=derived`)
    derived_from: tuple[str, ...] = Field(default_factory=tuple)

    # -- contesto (facoltativo: arricchisce, non identifica) -------------
    event_name: str = ""
    league: str = ""
    home: str = ""
    away: str = ""
    kickoff: Optional[datetime] = None
    selection_label: str = ""
    depth_usdc: Optional[float] = Field(None, ge=0.0,
                                        description="profondita' al floor della selezione")

    # -- `market` / `market_type`: una cosa sola --------------------------
    @model_validator(mode="before")
    @classmethod
    def _resolve_market(cls, data: Any) -> Any:
        """Risolve il tipo di mercato PRIMA dei campi, e non ammette ambiguita'.

        Accetta entrambe le forme (`market` stringa o `market_type`), le
        completa a vicenda, e respinge la contraddizione: `market='1X2'` con
        `market_type='AH'` e' un dato rotto, non una scelta da indovinare.
        """
        if not isinstance(data, Mapping):
            return data
        row = dict(data)
        raw_market = _raw_field(row, "market")
        raw_type = _raw_field(row, "market_type")
        resolved_market = market_type_of(raw_market) if raw_market is not None else None
        resolved_type = market_type_of(raw_type) if raw_type is not None else None
        if raw_market is not None and raw_type is not None:
            if resolved_market is None or resolved_type is None or resolved_market != resolved_type:
                raise _fail(QuoteErrorCode.MARKET_TYPE_MISMATCH,
                            f"'market'={_short(raw_market)} e "
                            f"'market_type'={_short(raw_type)} non concordano")
        resolved = resolved_market or resolved_type
        if resolved is not None:
            row["market"] = resolved.value
            row["market_type"] = resolved
        elif raw_type is not None and not raw_market:
            raise _fail(QuoteErrorCode.UNKNOWN_MARKET_TYPE,
                        f"tipo di mercato '{_short(raw_type)}' non riconosciuto "
                        f"(supportati: {', '.join(SUPPORTED_MARKETS)})")
        return row

    # -- validatori ------------------------------------------------------
    @field_validator("schema_version")
    @classmethod
    def _schema(cls, value: str) -> str:
        text = (value or "").strip()
        if not text:
            raise _fail(QuoteErrorCode.EMPTY_FIELD, "'schema_version' vuoto")
        if text not in SUPPORTED_SCHEMA_VERSIONS:
            raise _fail(QuoteErrorCode.UNSUPPORTED_SCHEMA,
                        f"versione '{text}' non supportata (supportate: "
                        f"{', '.join(SUPPORTED_SCHEMA_VERSIONS)})")
        return text

    @field_validator("event_id", "source", "gateway_id")
    @classmethod
    def _non_empty(cls, value: str, info: ValidationInfo) -> str:
        text = (value or "").strip()
        if not text:
            raise _fail(QuoteErrorCode.EMPTY_FIELD, f"'{info.field_name}' vuoto")
        return text

    @field_validator("market", mode="before")
    @classmethod
    def _normalize_market(cls, value: Any) -> str:
        key = _alias_key(value)
        if key is None:
            raise _fail(QuoteErrorCode.INVALID_TYPE, f"mercato non testuale: {_short(value)}")
        if not key:
            raise _fail(QuoteErrorCode.EMPTY_FIELD, "'market' vuoto")
        canonical = _MARKET_LOOKUP.get(_norm_key(key))
        if canonical is None:
            canonical = key if key.upper() in SUPPORTED_MARKETS else None
        if canonical is None:
            raise _fail(QuoteErrorCode.UNKNOWN_MARKET,
                        f"mercato '{_short(value)}' non riconosciuto (supportati: "
                        f"{', '.join(SUPPORTED_MARKETS)})")
        return canonical

    @field_validator("selection", mode="before")
    @classmethod
    def _normalize_selection(cls, value: Any, info: ValidationInfo) -> str:
        key = _alias_key(value)
        if key is None:
            raise _fail(QuoteErrorCode.INVALID_TYPE, f"selezione non testuale: {_short(value)}")
        if not key:
            raise _fail(QuoteErrorCode.EMPTY_FIELD, "'selection' vuota")
        # Il Risultato Esatto non ha un elenco di esiti: e' un PUNTEGGIO, e si
        # valida come tale ("3-1"). Va fatto PRIMA della tabella degli alias,
        # altrimenti "3-1" verrebbe cercato come alias e respinto come ignoto.
        if str(info.data.get("market") or "") == MarketType.CORRECT_SCORE.value:
            return _validate_score(key)
        canonical = _SELECTION_LOOKUP.get(_norm_key(key))
        if canonical is None:
            raise _fail(QuoteErrorCode.UNKNOWN_SELECTION,
                        f"selezione '{_short(value)}' non riconosciuta (1X2: 1/X/2, "
                        f"OU: over/under)")
        market = info.data.get("market")
        allowed = MARKET_SELECTIONS.get(str(market or ""))
        if allowed and canonical not in allowed:
            raise _fail(QuoteErrorCode.SELECTION_MARKET_MISMATCH,
                        f"selezione '{canonical}' non appartiene al mercato "
                        f"'{market}' (ammesse: {', '.join(allowed)})")
        return canonical

    # -- regole multi-mercato (linee, origine, provenienza) ----------------
    @model_validator(mode="after")
    def _check_market_shape(self) -> "MarketQuote":
        """Le regole del REGISTRO: linea dove serve, vietata dove non serve.

        E' qui che un mercato senza linea non puo' passare per un totale (e
        viceversa): senza questo controllo un OU "senza linea" sarebbe
        indistinguibile da un OU 2.5 e finirebbe sul ledger come tale.
        """
        spec = MARKET_SPECS.get(self.market_type) if self.market_type else None
        has_lines = bool(spec and spec.has_lines)
        if has_lines and self.line is None:
            raise _fail(QuoteErrorCode.LINE_REQUIRED,
                        f"il mercato '{self.market}' richiede una linea "
                        f"(es. 2.5) — senza linea l'esito non e' eseguibile")
        if spec is not None and not has_lines and self.line is not None:
            raise _fail(QuoteErrorCode.LINE_NOT_ALLOWED,
                        f"il mercato '{self.market}' non ha linee "
                        f"(ricevuta {self.line:g})")
        if spec is not None and not has_lines and self.main_line is not None:
            raise _fail(QuoteErrorCode.LINE_NOT_ALLOWED,
                        f"'main_line' su un mercato senza linee ('{self.market}')")
        if self.line is not None:
            self._validate_line_bounds(spec)
        if self.origin == "derived" and not self.derived_from:
            raise _fail(QuoteErrorCode.DERIVED_MISSING_SOURCE,
                        "quota derivata senza 'derived_from': la provenienza "
                        "non e' verificabile (mai un mercato inventato)")
        if self.origin == "native" and self.derived_from:
            raise _fail(QuoteErrorCode.UNKNOWN_ORIGIN,
                        f"origine 'native' con 'derived_from'={list(self.derived_from)}: "
                        f"una quota nativa non si deriva da altri mercati")
        return self

    def _validate_line_bounds(self, spec: Optional[MarketTypeSpec]) -> None:
        """Linea nel range del mercato, a passi di 0.25 (quarter-line SX)."""
        line = float(self.line or 0.0)
        bounds = (spec.line_bounds if spec else None)
        if bounds and not (bounds[0] <= line <= bounds[1]):
            raise _fail(QuoteErrorCode.LINE_INVALID,
                        f"linea {line:g} fuori range per '{self.market}' "
                        f"({bounds[0]:g}..{bounds[1]:g})")
        quarter = round(line * 4, 6)
        if abs(quarter - round(quarter)) > 1e-6:
            raise _fail(QuoteErrorCode.LINE_INVALID,
                        f"linea {line:g} non e' un multiplo di 0.25")
        if (spec is not None and not spec.quarter_line_eligible
                and abs(line * 2 - round(line * 2)) > 1e-6):
            raise _fail(QuoteErrorCode.LINE_INVALID,
                        f"linea {line:g} e' quartata ma '{self.market}' non "
                        f"ammette quarter-line")

    @field_validator("market_type", mode="before")
    @classmethod
    def _normalize_market_type(cls, value: Any) -> Optional[MarketType]:
        if value is None or value == "":
            return None
        resolved = market_type_of(value)
        if resolved is None:
            raise _fail(QuoteErrorCode.UNKNOWN_MARKET_TYPE,
                        f"tipo di mercato '{_short(value)}' non riconosciuto "
                        f"(supportati: {', '.join(SUPPORTED_MARKETS)})")
        return resolved

    @field_validator("line", mode="before")
    @classmethod
    def _check_line(cls, value: Any) -> Optional[float]:
        """La linea e' un numero finito (stringhe numeriche ammesse, virgola inclusa)."""
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        number = _as_float(value)
        if number is None:
            raise _fail(QuoteErrorCode.INVALID_TYPE, f"linea non numerica: {_short(value)}")
        if not math.isfinite(number):
            raise _fail(QuoteErrorCode.INVALID_TYPE, f"linea non finita: {number}")
        return round(number, 4)

    @field_validator("main_line", mode="before")
    @classmethod
    def _check_main_line(cls, value: Any) -> Optional[bool]:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in ("true", "1", "yes", "si"):
            return True
        if text in ("false", "0", "no"):
            return False
        raise _fail(QuoteErrorCode.INVALID_TYPE, f"'main_line' non booleano: {_short(value)}")

    @field_validator("origin", mode="before")
    @classmethod
    def _check_origin(cls, value: Any) -> str:
        text = ("native" if value is None or value == "" else str(value).strip().lower())
        if text not in QUOTE_ORIGINS:
            raise _fail(QuoteErrorCode.UNKNOWN_ORIGIN,
                        f"origine '{_short(value)}' non ammessa (native | derived)")
        return text

    @field_validator("derived_from", mode="before")
    @classmethod
    def _check_derived_from(cls, value: Any) -> tuple[str, ...]:
        """Normalizza la provenienza a nomi canonici di mercato."""
        if value in (None, ""):
            return ()
        if isinstance(value, (str, MarketType)):
            value = [value]
        if not isinstance(value, (list, tuple, set)):
            raise _fail(QuoteErrorCode.INVALID_TYPE,
                        f"'derived_from' non e' una lista: {_short(value)}")
        out: list[str] = []
        for item in value:
            resolved = market_type_of(item)
            if resolved is None:
                raise _fail(QuoteErrorCode.UNKNOWN_MARKET_TYPE,
                            f"'derived_from' cita un mercato ignoto: {_short(item)}")
            out.append(resolved.value)
        return tuple(dict.fromkeys(out))

    @field_validator("odds", mode="before")
    @classmethod
    def _check_odds(cls, value: Any) -> float:
        number = _as_float(value)
        if number is None:
            raise _fail(QuoteErrorCode.INVALID_TYPE, f"quota non numerica: {_short(value)}")
        if not math.isfinite(number):
            raise _fail(QuoteErrorCode.ODDS_NOT_FINITE, f"quota non finita: {number}")
        if number < MIN_ODDS:
            raise _fail(QuoteErrorCode.ODDS_BELOW_MIN,
                        f"quota {number} sotto il minimo {MIN_ODDS}")
        return number

    @field_validator("timestamp", "kickoff", mode="before")
    @classmethod
    def _check_timestamp(cls, value: Any, info: ValidationInfo) -> Optional[datetime]:
        if value is None:
            return None                      # `timestamp` mancante lo dice pydantic
        moment = _as_datetime(value)
        if moment is None:
            raise _fail(QuoteErrorCode.INVALID_TYPE,
                        f"'{info.field_name}' non e' una data ISO-8601: {_short(value)}")
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise _fail(QuoteErrorCode.TIMESTAMP_NAIVE,
                        f"'{info.field_name}' senza fuso orario (serve UTC; usa "
                        f"assume_utc=True se la fonte e' UTC)")
        return moment.astimezone(timezone.utc)

    # -- derivati --------------------------------------------------------
    @property
    def extra_fields(self) -> dict[str, Any]:
        """Campi non previsti dal contratto, come arrivati dal feed."""
        return dict(self.model_extra or {})

    @property
    def fixture_id(self) -> str:
        """Id della PARTITA: e' `event_id`.

        Nel contratto fixture ed evento coincidono (un fixture = una partita);
        l'alias esiste perche' e' il termine con cui si ragiona quando i
        mercati sono molti: N quote di UNA partita.
        """
        return self.event_id

    @property
    def market_spec(self) -> Optional[MarketTypeSpec]:
        """Regole del mercato (None solo se il tipo non e' nel registro)."""
        return MARKET_SPECS.get(self.market_type) if self.market_type else None

    @property
    def line_key(self) -> str:
        """Linea in forma canonica per chiavi/dedup ('', '2.5', '-0.75').

        La stringa (non il float) e' la chiave: 2.5 e 2.5000000001 non devono
        diventare due righe diverse sul ledger. La linea e' gia' arrotondata a
        4 decimali dal contratto.
        """
        if self.line is None:
            return ""
        text = f"{self.line:.4f}".rstrip("0").rstrip(".")
        return "0" if text in ("-0", "", "-0.") else text

    @property
    def handicap_for_selection(self) -> Optional[float]:
        """Handicap DELLA SELEZIONE giocata (l'AH si legge dal suo lato).

        `line` e' dal punto di vista di teamOne: se si gioca teamTwo, il suo
        handicap e' l'opposto. Un AH con il segno sbagliato e' un esito perso
        per un motivo che non e' il calcio.
        """
        if self.line is None or self.market not in (
                MarketType.ASIAN_HANDICAP.value,
                MarketType.ASIAN_HANDICAP_OT.value):
            return None
        return self.line if self.selection == "1" else -self.line

    @property
    def ledger_esito(self) -> str:
        """Esito nel formato del LEDGER del progetto (`tracker`/`ml_audit`).

        1X2 -> '1'/'X'/'2' · OU -> 'Over 2.5' · AH -> 'Home -0.75' ·
        BTTS -> 'Yes'/'No' · DC -> '1X'/'X2'/'12' · CS -> '3-1' ·
        famiglia OT -> come il mercato base (OU_OT -> 'Over 220.5', AH_OT ->
        'Home -3.5', ML_OT -> 'Home'/'Away').
        E' il ponte fra la chiave macchina (`selection`) e cio' che il resto
        del sistema gia' scrive e salda.
        """
        market = self.market_type
        if market in (MarketType.OVER_UNDER, MarketType.OVER_UNDER_OT):
            side = "Over" if self.selection == "over" else "Under"
            return f"{side} {self.half_line_label()}"
        if market in (MarketType.ASIAN_HANDICAP, MarketType.ASIAN_HANDICAP_OT):
            side = "Home" if self.selection == "1" else "Away"
            return f"{side} {_signed_line(self.handicap_for_selection or 0.0)}"
        if market is MarketType.MONEYLINE_OT:
            return "Home" if self.selection == "1" else "Away"
        if market is MarketType.BOTH_TEAMS_TO_SCORE:
            return "Yes" if self.selection == "yes" else "No"
        return self.selection_label or self.selection

    def half_line_label(self) -> str:
        """Linea leggibile per i mercati di totale ('2.5')."""
        return f"{float(self.line or 0.0):g}"

    @property
    def is_derived(self) -> bool:
        return self.origin == "derived"

    @property
    def quote_id(self) -> str:
        """Id della RILEVAZIONE (cambia quando la quota cambia nel tempo)."""
        return _digest("quote", self.event_id, self.market, self.line_key,
                       self.selection, self.gateway_id, self.source,
                       self.timestamp.isoformat())

    @property
    def identity_key(self) -> str:
        """Chiave STABILE di (evento, mercato, linea, esito): senza il tempo.

        La LINEA fa parte dell'identita': due totali 2.5 e 3.5 sono due
        mercati diversi, e confonderli significherebbe saldare un esito con
        il risultato di un altro.
        """
        return _digest("identity", self.event_id, self.market, self.line_key,
                       self.selection)

    def age_seconds(self, now: Optional[datetime] = None) -> float:
        """Eta' della rilevazione in secondi (negativa se nel futuro)."""
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        return (current - self.timestamp).total_seconds()

    def as_row(self) -> dict[str, Any]:
        """La riga FLAT da serializzare (contratto verso il gateway SQLite).

        Sono i campi dichiarati in `MARKET_ROW_FIELDS`, tutti JSON-safe
        (datetime -> ISO): il gateway non deve interpretare nulla, solo
        scrivere. `extra` conserva cio' che il feed ha mandato in piu'.
        """
        return {
            "fixture_id": self.fixture_id,
            "market_type": self.market_type.value if self.market_type else self.market,
            "line_key": self.line_key,
            "line": self.line,
            "selection": self.selection,
            "selection_label": self.selection_label or self.ledger_esito,
            "ledger_esito": self.ledger_esito,
            "odds": self.odds,
            "main_line": self.main_line,
            "origin": self.origin,
            "derived_from": list(self.derived_from),
            "depth_usdc": self.depth_usdc,
            "source": self.source,
            "gateway_id": self.gateway_id,
            "schema_version": self.schema_version,
            "observed_at": self.timestamp.isoformat(),
            "kickoff": self.kickoff.isoformat() if self.kickoff else None,
            "event_name": self.event_name,
            "league": self.league,
            "home": self.home,
            "away": self.away,
            "identity_key": self.identity_key,
            "quote_id": self.quote_id,
            "extra": {key: _jsonable(value) for key, value in self.extra_fields.items()},
        }

    def to_signal_fields(self) -> dict[str, Any]:
        """La parte di `Signal` che il mercato puo' riempire (le probabilita' no).

        `outcome` compare SOLO per il mercato 1X2: il contratto non inventa un
        esito che il `Signal` non accetta (`Outcome = 1|X|2`). Gli altri
        mercati viaggiano con `market_type` e `line`, che e' cio' che serve a
        distinguerli a valle (un OU 2.5 non e' un OU 3.5).
        """
        fields: dict[str, Any] = {
            "match_id": self.event_id,
            "league": self.league,
            "market": self.market,
            "market_type": self.market_type.value if self.market_type else self.market,
            "line": self.line,
            "selection_label": self.selection_label or self.ledger_esito,
            "kickoff": self.kickoff or self.timestamp,
            "price": self.odds,
            "price_source": self.source,
        }
        if self.market == MarketType.MATCH_RESULT.value:
            fields["outcome"] = self.selection
        return fields


#: Colonne della riga serializzata da `MarketQuote.as_row()`: e' il CONTRATTO
#: verso il gateway SQLite (una riga per quota, chiave composta
#: fixture_id + market_type + line_key + selection). Se cambia questa tupla
#: cambia la persistenza: tenerla dichiarata evita che tabella e modello
#: divergano in silenzio.
MARKET_ROW_FIELDS: tuple[str, ...] = (
    "fixture_id", "market_type", "line_key", "line", "selection",
    "selection_label", "ledger_esito", "odds", "main_line", "origin",
    "derived_from", "depth_usdc", "source", "gateway_id", "schema_version",
    "observed_at", "kickoff", "event_name", "league", "home", "away",
    "identity_key", "quote_id", "extra",
)


class QuoteBatch(BaseModel):
    """Esito di una validazione a lotti: cosa e' entrato e cosa e' stato respinto."""

    gateway_id: str = ""
    source: str = ""
    accepted: list[MarketQuote] = Field(default_factory=list)
    rejected: list[QuoteRejection] = Field(default_factory=list)
    total: int = 0
    #: Rifiuti che NON hanno emesso un evento (protezione anti-flood).
    suppressed_events: int = 0

    @property
    def ok(self) -> bool:
        return not self.rejected

    @property
    def rejected_rows(self) -> int:
        """Righe respinte: una riga puo' violare piu' regole insieme."""
        return len({rejection.index for rejection in self.rejected})

    @property
    def issues(self) -> int:
        """Problemi totali (>= righe respinte)."""
        return len(self.rejected)

    def by_code(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for rejection in self.rejected:
            counts[rejection.code.value] = counts.get(rejection.code.value, 0) + 1
        return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))

    def as_dict(self) -> dict[str, Any]:
        return {
            "gateway_id": self.gateway_id,
            "source": self.source,
            "total": self.total,
            "accepted": len(self.accepted),
            "rejected_rows": self.rejected_rows,
            "issues": self.issues,
            "rejected": len(self.rejected),
            "by_code": self.by_code(),
            "suppressed_events": self.suppressed_events,
        }


# ---------------------------------------------------------------------------
# Un FIXTURE, molti mercati
# ---------------------------------------------------------------------------

class FixtureQuotes(BaseModel):
    """Tutte le quote di mercato di UNA partita (`fixture_id`), insieme.

    Perche' un contenitore e non una lista sciolta: i mercati di un fixture
    hanno invarianti che una lista non puo' esprimere — stessa partita, stesso
    kickoff, nessuna riga di un'altra partita mescolata. Il contenitore le
    verifica all'ingresso, cosi' il resto del sistema puo' fidarsi del gruppo.

    Non impone COMPLETEZZA (un feed puo' avere solo una parte dei mercati: e'
    la normalita'). La dice: `is_complete()` / `incomplete()` esistono perche'
    uno scanner "universale" deve sapere cosa e' negoziabile e cosa e' parziale.
    """

    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)

    fixture_id: str = Field(..., min_length=1, description="id della partita")
    schema_version: str = MARKET_SCHEMA_VERSION
    source: str = ""
    gateway_id: str = ""
    event_name: str = ""
    league: str = ""
    home: str = ""
    away: str = ""
    kickoff: Optional[datetime] = None
    quotes: list[MarketQuote] = Field(default_factory=list, min_length=1)

    @field_validator("kickoff", mode="before")
    @classmethod
    def _check_kickoff(cls, value: Any) -> Optional[datetime]:
        if value is None or value == "":
            return None
        moment = _as_datetime(value)
        if moment is None:
            raise _fail(QuoteErrorCode.INVALID_TYPE,
                        f"'kickoff' non e' una data ISO-8601: {_short(value)}")
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise _fail(QuoteErrorCode.TIMESTAMP_NAIVE,
                        "'kickoff' senza fuso orario (serve UTC)")
        return moment.astimezone(timezone.utc)

    @model_validator(mode="after")
    def _check_same_fixture(self) -> "FixtureQuotes":
        """Tutte le quote sono della STESSA partita (e dello stesso kickoff).

        E' la guardia che impedisce il bug piu' costoso della multi-mercato:
        mescolare le gambe di due partite diverse e leggere una probabilita'
        dall'una e una quota dall'altra.
        """
        # Il kickoff di riferimento: quello del gruppo, o il primo dichiarato da
        # una quota. Cosi' l'incoerenza si vede ANCHE quando il contenitore non
        # lo porta: due quote della stessa partita non possono avere due orari.
        reference = self.kickoff or next(
            (item.kickoff for item in self.quotes if item.kickoff is not None), None)
        for quote in self.quotes:
            if quote.event_id != self.fixture_id:
                raise _fail(QuoteErrorCode.FIXTURE_MISMATCH,
                            f"la quota {quote.identity_key} e' dell'evento "
                            f"'{quote.event_id}', non di '{self.fixture_id}'")
            if (reference is not None and quote.kickoff is not None
                    and quote.kickoff != reference):
                raise _fail(QuoteErrorCode.FIXTURE_MISMATCH,
                            f"kickoff incoerente sulla quota {quote.identity_key}: "
                            f"{quote.kickoff.isoformat()} vs {reference.isoformat()}")
        return self

    # -- letture ---------------------------------------------------------
    def market_types(self) -> list[MarketType]:
        """I tipi di mercato presenti, in ordine di registro (stabile)."""
        present = {quote.market_type for quote in self.quotes if quote.market_type}
        return [market for market in MARKET_SPECS if market in present]

    def by_market(self) -> dict[MarketType, list[MarketQuote]]:
        """Quote raggruppate per tipo di mercato (linee incluse)."""
        out: dict[MarketType, list[MarketQuote]] = {}
        for quote in self.quotes:
            if quote.market_type:
                out.setdefault(quote.market_type, []).append(quote)
        return out

    def lines_for(self, market_type: Any) -> list[float]:
        """Le linee disponibili per un mercato (ordinate)."""
        resolved = market_type_of(market_type)
        lines = {quote.line for quote in self.quotes
                 if quote.market_type is resolved and quote.line is not None}
        return sorted(lines)

    def quotes_for(self, market_type: Any,
                   line: Optional[float] = None) -> list[MarketQuote]:
        """Quote di un mercato: tutte le linee, o solo quella indicata."""
        resolved = market_type_of(market_type)
        out = [quote for quote in self.quotes if quote.market_type is resolved]
        if line is not None:
            key = _line_key(line)
            out = [quote for quote in out if quote.line_key == key]
        return out

    def main_line(self, market_type: Any) -> Optional[float]:
        """Linea principale dichiarata dalla fonte (None se non e' dichiarata)."""
        for quote in self.quotes_for(market_type):
            if quote.main_line:
                return quote.line
        return None

    def is_complete(self, market_type: Any,
                    line: Optional[float] = None) -> Optional[bool]:
        """Tutti gli esiti del mercato sono presenti? None se non e' decidibile.

        Per il Risultato Esatto "completo" non vuol dire nulla (gli esiti
        possibili non sono enumerabili): dire `False` sarebbe un falso, quindi
        la risposta e' None — esplicitamente sconosciuta.
        """
        resolved = market_type_of(market_type)
        spec = MARKET_SPECS.get(resolved) if resolved else None
        if spec is None or not spec.selections:
            return None
        keys = {quote.selection for quote in self.quotes_for(resolved, line)}
        return all(selection in keys for selection in spec.selections)

    def incomplete(self) -> list[dict[str, Any]]:
        """Elenco dei mercati (tipo, linea) a cui MANCANO esiti.

        E' il report che serve allo scanner: cosa non e' giocabile e perche'.
        """
        out: list[dict[str, Any]] = []
        for market, quotes in self.by_market().items():
            spec = MARKET_SPECS.get(market)
            if spec is None or not spec.selections:
                continue
            groups: dict[str, list[MarketQuote]] = {}
            for quote in quotes:
                groups.setdefault(quote.line_key, []).append(quote)
            for line_key, group in groups.items():
                keys = {quote.selection for quote in group}
                missing = [s for s in spec.selections if s not in keys]
                if missing:
                    out.append({"market_type": market.value,
                                "line_key": line_key,
                                "missing": missing,
                                "present": len(keys)})
        return out

    def as_rows(self) -> list[dict[str, Any]]:
        """Righe flat per il gateway (una per quota), in ordine deterministico."""
        return [quote.as_row() for quote in sorted(
            self.quotes, key=lambda q: ((q.market_type.value if q.market_type
                                         else q.market), q.line_key, q.selection))]

    def summary(self) -> dict[str, Any]:
        """Riepilogo per log/report: quanti mercati, quali linee, cosa manca."""
        return {
            "fixture_id": self.fixture_id,
            "event_name": self.event_name,
            "league": self.league,
            "kickoff": self.kickoff.isoformat() if self.kickoff else None,
            "quotes": len(self.quotes),
            "markets": [market.value for market in self.market_types()],
            "lines": {market.value: self.lines_for(market)
                      for market in self.market_types()},
            "incomplete": self.incomplete(),
        }


class FixtureQuoteBatch(BaseModel):
    """Esito di un ingresso a LOTTI multi-mercato (mai un'eccezione)."""

    gateway_id: str = ""
    source: str = ""
    fixtures: list[FixtureQuotes] = Field(default_factory=list)
    rejected: list[QuoteRejection] = Field(default_factory=list)
    total: int = 0
    suppressed_events: int = 0

    @property
    def ok(self) -> bool:
        return not self.rejected

    @property
    def quotes(self) -> int:
        """Quote accettate in totale (su tutti i fixture)."""
        return sum(len(fixture.quotes) for fixture in self.fixtures)

    @property
    def rejected_rows(self) -> int:
        return len({rejection.index for rejection in self.rejected})

    def by_code(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for rejection in self.rejected:
            counts[rejection.code.value] = counts.get(rejection.code.value, 0) + 1
        return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))

    def fixture(self, fixture_id: str) -> Optional[FixtureQuotes]:
        for item in self.fixtures:
            if item.fixture_id == fixture_id:
                return item
        return None

    def as_dict(self) -> dict[str, Any]:
        return {
            "gateway_id": self.gateway_id,
            "source": self.source,
            "total": self.total,
            "fixtures": len(self.fixtures),
            "accepted": self.quotes,
            "rejected_rows": self.rejected_rows,
            "rejected": len(self.rejected),
            "by_code": self.by_code(),
            "suppressed_events": self.suppressed_events,
        }


def validate_fixture_quotes(rows: Iterable[Any], *, gateway_id: Optional[str] = None,
                            source: Optional[str] = None,
                            schema_version: Optional[str] = None,
                            assume_utc: bool = False,
                            max_events: int = DEFAULT_MAX_REJECTION_EVENTS,
                            obs: Optional[Observability] = None,
                            ctx: Optional[TraceContext] = None
                            ) -> FixtureQuoteBatch:
    """Ingresso a lotti MULTI-MERCATO: raggruppa per fixture, mai un'eccezione.

    Riusa la porta unica di validazione (`parse_quote`, quindi `prepare_payload`
    e il contratto): qui si aggiunge solo il raggruppamento per partita e le
    invarianti di gruppo. Una riga rotta non ferma le altre, e **un fixture con
    quote incoerenti viene respinto intero** (non si tiene la meta' buona:
    sarebbe un gruppo che promette invarianti che non ha).
    """
    accepted: list[MarketQuote] = []
    rejections: list[QuoteRejection] = []
    suppressed = 0
    total = 0
    for index, row in enumerate(rows or ()):
        total += 1
        event_hint = _safe_get(row, "event_id", "fixture_id", "sportXeventId")
        try:
            quote = parse_quote(row, gateway_id=gateway_id, source=source,
                                schema_version=schema_version, assume_utc=assume_utc)
        except MarketQuoteError as exc:
            for issue in exc.issues:
                if len(rejections) >= max_events:
                    suppressed += 1
                    continue
                rejections.append(QuoteRejection.from_issue(
                    issue, index=index, event_id=event_hint,
                    source=_safe_get(row, "source") or (source or ""),
                    gateway_id=gateway_id or "", raw_keys=_safe_keys(row)))
            continue
        accepted.append(quote)

    grouped: dict[str, list[MarketQuote]] = {}
    for quote in accepted:
        grouped.setdefault(quote.event_id, []).append(quote)

    fixtures: list[FixtureQuotes] = []
    for fixture_id, quotes in sorted(grouped.items()):
        first = quotes[0]
        try:
            fixtures.append(FixtureQuotes(
                fixture_id=fixture_id, schema_version=first.schema_version,
                source=first.source, gateway_id=gateway_id or first.gateway_id,
                event_name=first.event_name, league=first.league,
                home=first.home, away=first.away, kickoff=first.kickoff,
                quotes=quotes))
        except ValidationError as exc:
            for issue in _issues_from_validation_error(exc):
                if len(rejections) >= max_events:
                    suppressed += 1
                    continue
                rejections.append(QuoteRejection.from_issue(
                    issue, index=-1, event_id=fixture_id,
                    source=source or first.source, gateway_id=gateway_id or ""))
    batch = FixtureQuoteBatch(gateway_id=gateway_id or "", source=source or "",
                              fixtures=fixtures, rejected=rejections, total=total,
                              suppressed_events=suppressed)
    if obs is not None:
        level = logger.info if batch.ok else logger.warning
        level("multi-mercato: %d righe -> %d fixture / %d quote accettate, "
              "%d problemi %s (gateway=%s)", total, len(fixtures), batch.quotes,
              len(rejections), batch.by_code() or "-", gateway_id or "-")
        obs.event("market.fixtures_validated", ctx=ctx, stage="market",
                  gateway_id=gateway_id or "", source=source or "",
                  total=total, fixtures=len(fixtures), accepted=batch.quotes,
                  rejected=len(rejections), by_code=batch.by_code(),
                  suppressed_events=suppressed)
    return batch


# ---------------------------------------------------------------------------
# Helper di conversione e formato
# ---------------------------------------------------------------------------

def _fail(code: QuoteErrorCode, detail: str) -> ValueError:
    """Errore di validazione col formato '<codice>: <dettaglio>'."""
    return ValueError(f"{code.value}: {detail}")


def _short(value: Any) -> str:
    """Valore troncato per i log (mai un payload intero, mai un valore lungo)."""
    text = repr(value) if not isinstance(value, str) else value
    text = text.replace("\n", " ")
    return text if len(text) <= TRUNCATE else text[:TRUNCATE] + "…"


def _digest(*parts: Any) -> str:
    raw = "|".join("" if part is None else str(part) for part in parts).encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:12]


def _alias_key(value: Any) -> Optional[str]:
    """Chiave di alias di mercato/selezione (str ripulita, int -> str)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if not isinstance(value, str):
        return None
    return re.sub(r"\s+", "", value.strip())


def _line_key(line: Any) -> str:
    """Linea in forma canonica di chiave ('2.5', '-0.75', '' se assente)."""
    if line is None or (isinstance(line, str) and not line.strip()):
        return ""
    number = _as_float(line)
    if number is None:
        return str(line).strip()
    text = f"{round(number, 4):.4f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def _signed_line(value: float) -> str:
    """Linea col segno esplicito nel formato del ledger: -1.0, +0.25, 0."""
    number = round(float(value), 4)
    if number == 0:
        return "0"
    return f"{number:+.1f}" if number == int(number) else f"{number:+g}"


def _raw_field(row: Mapping[str, Any], canonical: str) -> Any:
    """Valore di un campo canonico cercando anche fra i suoi alias di chiave.

    Serve al contratto per risolvere `market`/`market_type` anche quando la
    riga arriva con i nomi del feed (`marketType`, `market_name`, ...) e non e'
    passata da `prepare_payload`. Prudente: una riga ostile non fa esplodere.
    """
    try:
        value = row.get(canonical)
    except Exception:
        value = None
    if value not in (None, ""):
        return value
    try:
        items = list(row.items())
    except Exception:
        return None
    for key, candidate in items:
        try:
            alias = KEY_ALIASES.get(re.sub(r"[^a-z0-9]", "", str(key).lower()))
        except Exception:
            continue
        if alias == canonical and candidate not in (None, ""):
            return candidate
    return None


_SCORE_RE = re.compile(r"^(\d{1,2})\s*[-–:]\s*(\d{1,2})$")
#: Tetto di sanity sui gol di un risultato esatto (oltre e' un dato sporco).
MAX_SCORE_GOALS = 20


def _validate_score(value: str) -> str:
    """Valida e normalizza un esito di Risultato Esatto nella forma 'C-T'.

    Accetta i separatori che i feed usano davvero (`3-1`, `3:1`, `3 – 1`) e
    normalizza in `3-1`. Un punteggio non plausibile e' un dato sporco, non un
    esito raro: 25-3 non e' un mercato, e' un errore di parsing.
    """
    match = _SCORE_RE.match(str(value).strip())
    if not match:
        raise _fail(QuoteErrorCode.INVALID_SCORE,
                    f"esito di risultato esatto non valido: {_short(value)} "
                    f"(forma attesa 'Casa-Trasferta', es. '3-1')")
    home, away = int(match.group(1)), int(match.group(2))
    if home > MAX_SCORE_GOALS or away > MAX_SCORE_GOALS:
        raise _fail(QuoteErrorCode.INVALID_SCORE,
                    f"punteggio implausibile {home}-{away} (max {MAX_SCORE_GOALS} gol)")
    return f"{home}-{away}"


def _jsonable(value: Any) -> Any:
    """Valore serializzabile in JSON (datetime -> ISO, il resto invariato)."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return str(value)


def _as_float(value: Any) -> Optional[float]:
    """Numero da int/float/stringa numerica (virgola decimale ammessa)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip().replace(",", ".")
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _as_datetime(value: Any) -> Optional[datetime]:
    """Data da datetime o stringa ISO-8601 (accetta il suffisso `Z`)."""
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    text = _ISO_Z.sub("+00:00", value.strip())
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _issue_from_error(error: Mapping[str, Any]) -> QuoteIssue:
    """`ValidationError` di pydantic -> `QuoteIssue` col codice del contratto."""
    location = [str(part) for part in error.get("loc", ()) if part != "__root__"]
    message = str(error.get("msg", ""))
    match = _CODE_RE.search(message)
    if match:
        code = QuoteErrorCode(match.group(0))
        detail = message[match.end():].lstrip(": ").strip()
    else:
        code = _PYDANTIC_CODES.get(str(error.get("type")), QuoteErrorCode.INVALID_TYPE)
        # Messaggio di pydantic in inglese: tradotto per i campi obbligatori,
        # cosi' un log di rifiuto si legge senza conoscere la libreria.
        detail = message
        if code is QuoteErrorCode.MISSING_FIELD and message == "Field required":
            detail = f"'{'.'.join(location)}' obbligatorio" if location else "campo obbligatorio"
    return QuoteIssue(code=code, field=".".join(location), detail=detail)


def _issues_from_validation_error(error: ValidationError) -> list[QuoteIssue]:
    return [_issue_from_error(item) for item in error.errors()]


def _safe_keys(row: Any) -> list[str]:
    """Chiavi della riga, senza fidarsi della riga.

    Una riga puo' essere un `Mapping` ostile (un `.get` che solleva): la
    contabilita' del lotto non deve mai dipendere dal comportamento del dato.
    """
    try:
        return sorted(str(key) for key in row.keys())
    except Exception:
        return []


def _safe_get(row: Any, *keys: str) -> str:
    """Primo valore non vuoto fra le chiavi date, con la stessa prudenza."""
    for key in keys:
        try:
            value = row.get(key)
        except Exception:
            continue
        if value not in (None, ""):
            return str(value)
    return ""


# ---------------------------------------------------------------------------
# Validazione ALL'INGRESSO
# ---------------------------------------------------------------------------

def prepare_payload(data: Any, *, gateway_id: Optional[str] = None,
                    source: Optional[str] = None,
                    schema_version: Optional[str] = None,
                    assume_utc: bool = False) -> tuple[dict[str, Any], list[QuoteIssue]]:
    """Normalizza un payload grezzo PRIMA della validazione del contratto.

    Fa solo cio' che e' deterministico: applica gli alias di chiave (il nome
    canonico vince), completa i default di feed (`gateway_id`/`source`/
    `schema_version` **solo se assenti**), e — con `assume_utc=True` — dichiara
    UTC i timestamp senza fuso orario. Non corregge valori: un rifiuto esplicito
    e' meglio di un dato sistemato a mano.
    """
    if not isinstance(data, Mapping):
        return {}, [QuoteIssue(code=QuoteErrorCode.INVALID_TYPE, field="",
                               detail=f"payload non e' un oggetto: {_short(data)}")]
    payload: dict[str, Any] = dict(data)
    for key in list(payload):
        canonical = KEY_ALIASES.get(re.sub(r"[^a-z0-9]", "", str(key).lower()))
        if canonical and canonical not in payload:
            payload[canonical] = payload[key]
    if gateway_id and not payload.get("gateway_id"):
        payload["gateway_id"] = gateway_id
    if source and not payload.get("source"):
        payload["source"] = source
    if schema_version and not payload.get("schema_version"):
        payload["schema_version"] = schema_version
    if assume_utc:
        for key in ("timestamp", "kickoff"):
            value = payload.get(key)
            if value is None:
                continue
            moment = _as_datetime(value)
            if moment is not None and (moment.tzinfo is None or moment.utcoffset() is None):
                payload[key] = moment.replace(tzinfo=timezone.utc)
    return payload, []


def log_issues(issues: Iterable[QuoteIssue], *, gateway_id: str = "", source: str = "",
               event_id: str = "", obs: Optional[Observability] = None,
               ctx: Optional[TraceContext] = None, stage: str = "market") -> None:
    """Logga i problemi di ingresso: una riga di log e un evento, per ognuno."""
    for issue in issues:
        logger.error("contratto di mercato respinto [%s] campo=%s: %s "
                     "(gateway=%s, source=%s, event=%s)",
                     issue.code.value, issue.field or "-", issue.detail,
                     gateway_id or "-", source or "-", event_id or "-")
        if obs is not None:
            obs.event("market.quote_rejected", ctx=ctx, stage=stage, outcome="rejected",
                      error_code=issue.code.value, field=issue.field or "",
                      detail=issue.detail, gateway_id=gateway_id, source=source,
                      event_id=event_id)


def parse_quote(data: Any, *, gateway_id: Optional[str] = None,
                source: Optional[str] = None,
                schema_version: Optional[str] = None,
                assume_utc: bool = False,
                obs: Optional[Observability] = None,
                ctx: Optional[TraceContext] = None,
                log_accepted: bool = False) -> MarketQuote:
    """Valida UNA quota all'ingresso. Solleva `MarketQuoteError` se non conforme.

    Ogni problema viene loggato prima di sollevare: chi cattura l'eccezione ha
    gia' la diagnostica nei log (e nel sink di osservabilita', se fornito).
    """
    payload, issues = prepare_payload(data, gateway_id=gateway_id, source=source,
                                      schema_version=schema_version, assume_utc=assume_utc)
    raw_keys = _safe_keys(data) if isinstance(data, Mapping) else []
    event_id = str(payload.get("event_id") or "")
    if issues:
        log_issues(issues, gateway_id=gateway_id or "", source=source or "",
                   event_id=event_id, obs=obs, ctx=ctx)
        raise MarketQuoteError(issues, gateway_id=gateway_id or "", source=source or "",
                               raw_keys=raw_keys)
    try:
        quote = MarketQuote(**payload)
    except ValidationError as error:
        issues = _issues_from_validation_error(error)
        log_issues(issues, gateway_id=payload.get("gateway_id") or gateway_id or "",
                   source=payload.get("source") or source or "", event_id=event_id,
                   obs=obs, ctx=ctx)
        raise MarketQuoteError(issues, gateway_id=payload.get("gateway_id") or gateway_id or "",
                               source=payload.get("source") or source or "",
                               raw_keys=raw_keys) from error
    if log_accepted:
        logger.debug("contratto di mercato valido [%s] %s %s @ %s (gateway=%s)",
                     quote.quote_id, quote.event_id, quote.selection, quote.odds,
                     quote.gateway_id)
        if obs is not None:
            obs.event("market.quote_accepted", ctx=ctx, stage="market", outcome="ok",
                      quote_id=quote.quote_id, event_id=quote.event_id,
                      market=quote.market, selection=quote.selection, odds=quote.odds,
                      source=quote.source, gateway_id=quote.gateway_id)
    return quote


def validate_batch(rows: Optional[Iterable[Any]], *, gateway_id: str = "",
                   source: str = "", assume_utc: bool = False,
                   obs: Optional[Observability] = None,
                   ctx: Optional[TraceContext] = None,
                   max_events: int = DEFAULT_MAX_REJECTION_EVENTS) -> QuoteBatch:
    """Valida un lotto **senza mai sollevare**: accettate + respinte + conteggi.

    E' il punto d'ingresso per gli import: una riga rotta non ferma le altre e
    il rifiuto e' contabilizzato (non silenzioso). Gli eventi di rifiuto sono
    limitati a `max_events` per non inondare il sink; le righe di log restano
    per ogni problema e i rifiuti oltre il limite finiscono in
    `suppressed_events`.
    """
    batch = QuoteBatch(gateway_id=gateway_id, source=source)
    try:
        items = list(rows or [])
    except TypeError:
        items = []
        batch.rejected.append(QuoteRejection(
            code=QuoteErrorCode.INVALID_TYPE, field="",
            detail=f"lotto non iterabile: {_short(rows)}", gateway_id=gateway_id,
            source=source))
    batch.total = len(items)
    emitted = 0
    for index, row in enumerate(items):
        probe = obs if emitted < max_events else None
        try:
            quote = parse_quote(row, gateway_id=gateway_id, source=source,
                                assume_utc=assume_utc, obs=probe, ctx=ctx)
        except MarketQuoteError as error:
            raw_keys = _safe_keys(row) if isinstance(row, Mapping) else []
            event_id = _safe_get(row, "event_id", "match_id") if isinstance(row, Mapping) else ""
            for issue in error.issues:
                batch.rejected.append(QuoteRejection.from_issue(
                    issue, index=index, event_id=event_id,
                    source=str(error.source or source), gateway_id=str(error.gateway_id or gateway_id),
                    raw_keys=raw_keys))
            if probe is not None:
                emitted += len(error.issues)
            else:
                batch.suppressed_events += len(error.issues)
            continue
        except Exception as exc:                 # qualunque sorpresa: mai un'eccezione
            batch.rejected.append(QuoteRejection(
                code=QuoteErrorCode.INVALID_TYPE, index=index,
                detail=f"{type(exc).__name__}: {_short(exc)}",
                gateway_id=gateway_id, source=source))
            continue
        batch.accepted.append(quote)
    _log_batch(batch, obs=obs, ctx=ctx)
    return batch


def _log_batch(batch: QuoteBatch, *, obs: Optional[Observability] = None,
               ctx: Optional[TraceContext] = None) -> None:
    """Riepilogo del lotto: una riga di log + un evento (fail-safe)."""
    summary = batch.as_dict()
    try:
        if batch.rejected:
            logger.warning("contratto di mercato: %d/%d quote respinte, %d problemi "
                           "(gateway=%s, source=%s, motivi=%s)",
                           batch.rejected_rows, batch.total, batch.issues,
                           batch.gateway_id or "-", batch.source or "-",
                           batch.by_code())
        else:
            logger.info("contratto di mercato: %d/%d quote valide (gateway=%s)",
                        len(batch.accepted), batch.total, batch.gateway_id or "-")
        if obs is not None:
            obs.event("market.batch_validated", ctx=ctx, stage="market",
                      outcome="ok" if batch.ok else "rejected", **summary)
    except Exception as exc:                     # la telemetria non blocca l'ingresso
        logger.warning("contratto di mercato: riepilogo non registrato (%s)", exc)


__all__ = [
    "DEFAULT_MAX_REJECTION_EVENTS", "KEY_ALIASES", "MARKET_ALIASES", "MARKET_ROW_FIELDS",
    "MARKET_SCHEMA_VERSION", "MARKET_SELECTIONS", "MARKET_SPECS", "MAX_SCORE_GOALS",
    "MIN_ODDS", "MarketQuote", "MarketQuoteError", "MarketType", "MarketTypeSpec",
    "QuoteBatch", "QuoteErrorCode", "QuoteIssue", "QuoteRejection", "QUOTE_ORIGINS",
    "SELECTION_ALIASES", "SUPPORTED_MARKETS", "SUPPORTED_SCHEMA_VERSIONS",
    "SX_LINE_BEARING_TYPES", "SX_QUARTER_LINE_TYPES", "SX_TYPE_IDS",
    "SX_TYPES_NOT_MODELLED", "TRUNCATE", "FixtureQuoteBatch", "FixtureQuotes",
    "line_required", "log_issues", "market_accepts_lines", "market_type_of",
    "parse_quote", "prepare_payload", "spec_for", "validate_batch",
    "validate_fixture_quotes",
]
