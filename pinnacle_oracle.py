"""pinnacle_oracle.py — Pinnacle come oracolo del mercato (FASE PROBE).

Direttiva del proprietario (25/09/2026): pivot verso un modello TOP-DOWN.
Congelare il modello statistico bottom-up (Poisson) e far dettare la
probabilita' "vera" dal mercato sharp (Pinnacle), usando poi lo SCARTO fra
quella probabilita' e il prezzo disponibile sull'exchange (SX Bet) come
trigger di valore. La decisione diventa puramente matematica: Pinnacle dice
quanto vale un esito, SX dice quanto lo pagano, e si compra solo il ritardo.

⚠️ TRE COSE MISURATE PRIMA DI SCRIVERE QUESTO CODICE (non assunte, verificate
   il 25/09/2026 sulle cache di PRODUZIONE — costo 0 crediti):

1. **PINNACLE E' GIA' NEL PAYLOAD CHE PAGHIAMO.** La fetch delle quote usa
   `regions=eu` SENZA filtro `bookmakers`, quindi il payload contiene gia'
   Pinnacle. Verificato: **9 leghe di calcio su 9** hanno Pinnacle (MLS 15/15
   partite, Liga MX 9/9, League Two 12/12, Bundesliga 2 9/9, League One 7/7,
   Primeira Liga 9/10, Brazil B 1/1, K League 1 1/1, Superettan 1/1).
   → Aggiungere `bookmakers=pinnacle` NON compra dati nuovi: riduce il payload.
   Il costo marginale dell'oracolo e' quindi **ZERO** se si estrae dalla fetch
   che facciamo gia', ed e' questo il percorso che la pipeline deve usare.
2. **IL DEVIG ESISTE GIA'**: `market_calib.devig` con tre metodi
   (`multiplicative`, `power`, `shin`) + `market_implied`. Nessuna copia.
3. **LA CADENZA E' IL VINCOLO, NON L'ESTRAZIONE.** the-odds-api addebita
   `markets x regions` per chiamata (qui 1x1 = 1 credito) e il piano free da'
   500 crediti/mese (~16/giorno). **Un job che interroga l'API in tempo reale
   non e' sostenibile**: 1 lega ogni 5 minuti = 288 chiamate/giorno, ~17 volte
   il budget. Da qui i DUE percorsi espliciti: `--from-cache` (0 crediti,
   quello che gira nella pipeline) e `--live` (1 credito, SOLO diagnostica).

CONSENSO MULTI-ORACOLO (26/09/2026): la probabilita' "vera" non e' piu' il
prezzo secco di UNA sola fonte. Ogni fonte sharp disponibile (Pinnacle,
Betfair Exchange EU, Matchbook) viene de-vigata con la stessa formula e le
probabilita' fair vengono aggregate in un CONSENSO:
  - **benchmark primario** = Pinnacle + Betfair Exchange (media o mediana per
    esito);
  - **validatore secondario** = Matchbook: entra nell'aggregato SOLO se la sua
    probabilita' fair resta entro tolleranza dal benchmark, altrimenti viene
    escluso e il disallineamento viene registrato (un controllo di coerenza,
    non una terza voce che guida il consenso).
Il **fallback e' robusto**: se una fonte non ha il 1X2 completo per quella
partita, il consenso ripiega su quelle presenti — con la sola Pinnacle il
risultato COINCIDE col comportamento storico, e la pipeline non si blocca mai.
Env: `PINNACLE_CONSENSUS` (default 1), `PINNACLE_CONSENSUS_METHOD`
(mean|median, default mean), `PINNACLE_VALIDATOR_TOLERANCE` (default 0.05).

Questo modulo e' la FASE 1 (probe): dimostra che estrazione, de-vig e gate
funzionano. NON scrive sul ledger, NON piazza ordini, NON consulta Poisson:
`bypass` del motore statistico e' una decisione di pipeline (fase 2), non un
effetto collaterale di una funzione di lettura.

⚠️ Il gate EV e' definito una volta sola:
    EV = p_true x (quota - 1) - (1 - p_true)
e le due letture della direttiva COINCIDONO esattamente:
    EV >= ev_min   <=>   quota >= true_odd x (1 + ev_min)
(`true_odd` = 1 / p_true, la "True Odd" di Pinnacle). `required_price` espone
la seconda forma: stessa condizione, niente doppio standard.

CLI:
  venv/bin/python pinnacle_oracle.py --from-cache          # 0 crediti
  venv/bin/python pinnacle_oracle.py --live soccer_usa_mls # 1 credito
  venv/bin/python pinnacle_oracle.py --from-cache --json
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from config import DATA_DIR, load_dotenv

load_dotenv()

logger = logging.getLogger("pinnacle_oracle")

#: --- FONTI DEL CONSENSO MULTI-ORACOLO (26/09/2026) -------------------------
#: Ruoli ESPLICITI (tabelle, mai fuzzy):
#: - PRIMARIA  = Pinnacle (lo sharp di riferimento storico del progetto);
#: - BENCHMARK = Betfair Exchange EU (exchange, stessa natura di SX Bet);
#: - VALIDATORE = Matchbook (verifica il consenso, non lo guida).
#: Il match e' per SOTTOSTRINGA su chiave/titolo: "betfair" copre anche la
#: variante `betfair_ex_uk`.
PRIMARY_BOOK = "pinnacle"
BENCHMARK_BOOK = "betfair_ex_eu"
VALIDATOR_BOOK = "matchbook"

BOOK_PATTERNS: Dict[str, Tuple[str, ...]] = {
    PRIMARY_BOOK: ("pinnacle",),
    BENCHMARK_BOOK: ("betfair",),
    VALIDATOR_BOOK: ("matchbook",),
}

#: Ordine di priorita' delle fonti: benchmark PRIMA, validatore dopo.
CONSENSUS_BOOKS: Tuple[str, ...] = (PRIMARY_BOOK, BENCHMARK_BOOK, VALIDATOR_BOOK)

#: Array multi-bookmaker per la chiamata the-odds-api (percorso `--live`).
#: Il costo the-odds-api e' `markets x regions`: filtrare i bookmaker NON
#: costa di piu' e riduce il payload.
MULTI_BOOKMAKERS = ",".join(CONSENSUS_BOOKS)

#: Esiti 1X2 (ordine canonico).
OUTCOMES: Tuple[str, ...] = ("1", "X", "2")

#: Book sharp riconosciuti da `is_sharp` (vitello per compatibilita': un
#: book che non e' nel consenso non e' per forza "soft").
SHARP_BOOKS: Tuple[str, ...] = ("pinnacle", "betfair", "matchbook")

#: Chiavi di SERVIZIO: non sono esiti e non devono mai entrare in un calcolo
#: di EV/true-odd (una lista o un booleano non hanno un inverso).
_META_KEYS = frozenset({"overround", "sources", "n_sources",
                        "consensus_method", "validated", "fallback",
                        "agreement_pp", "devig_method", "shin_z"})

#: Metodo di de-vig. Dal 02/10/2026 il default e' **Shin (1993)**: pulisce le
#: quote sharp di Pinnacle tenendo conto del denaro INFORMATO presente nel
#: mercato (parametro z) e corregge il favourite-longshot bias in modo piu'
#: deciso del `power`. La formula vive SOLO in `market_calib.shin_devig`:
#: qui si sceglie il metodo, non lo si riscrive. Rollback a una env:
#: `PINNACLE_DEVIG_METHOD=power` (o `multiplicative`).
DEVIG_METHOD: str = (os.getenv("PINNACLE_DEVIG_METHOD", "shin").strip().lower()
                     or "shin")

#: --- CONFIGURAZIONE DEL CONSENSO -----------------------------------------
#: `PINNACLE_CONSENSUS=0` ripristina la Pinnacle-secca (rollback a una env).
CONSENSUS_ENABLED: bool = os.getenv("PINNACLE_CONSENSUS", "1").strip().lower() \
    in ("1", "true", "yes", "on")
#: "mean" (media) o "median" (mediana): la mediana e' piu' robusta a una
#: fonte anomala, la media sfrutta tutta l'informazione. Default = media.
CONSENSUS_METHOD: str = (os.getenv("PINNACLE_CONSENSUS_METHOD", "mean")
                         .strip().lower() or "mean")
#: Scostamento massimo (prob.) entro cui il validatore conferma il benchmark.
VALIDATOR_TOLERANCE: float = float(
    os.getenv("PINNACLE_VALIDATOR_TOLERANCE", "0.05"))

try:                        # stessa soglia del gate di produzione, mai copiata
    from value_filter import EV_MIN as DEFAULT_EV_MIN
except Exception:                                                   # pragma: no cover
    # Ripiego DICHIARATO solo per l'import a modulo rotto: stessa soglia di
    # produzione (2.5%). Un valore diverso qui sarebbe una seconda soglia.
    DEFAULT_EV_MIN = 0.025

#: Endpoint della fonte (the-odds-api). Usato SOLO dal percorso `--live`.
ODDS_ENDPOINT = "https://api.the-odds-api.com/v4/sports/{sport}/odds"


# ---------------------------------------------------------------------------
# 1. ESTRAZIONE: Pinnacle dal payload che scarichiamo gia'
# ---------------------------------------------------------------------------

def _cf(value: Any) -> str:
    """Chiave di confronto per nomi (case-fold + spazi normalizzati)."""
    return " ".join(str(value or "").strip().casefold().split())


# ---------------------------------------------------------------------------
# 0. NORMALIZZAZIONE CANONICA DELLE LINEE (05/10/2026)
# ---------------------------------------------------------------------------
# Il matching fra la linea di SX Bet e quella di Pinnacle/the-odds-api e' un
# confronto NUMERICO, ma i provider scrivono la stessa linea in formati
# diversi: `+0.25`, `2.50`, `'0.0, 0.5'` (linea quarter come DUE mezze-linee),
# str vs float, testi con la linea dentro (`'Over 2.5'`). Due rappresentazioni
# della stessa linea che non si agganciano producono un `no_oracle` per un
# falso disallineamento: il pick viene scartato anche se l'oracolo lo prezza.
# Qui c'e' l'UNICA definizione della forma canonica; la si applica SU ENTRAMBI
# I LATI (SX in `multi_market._market_line`, the-odds-api in `totals_odds_of`/
# `spreads_odds_of`/`oracle_lines`) PRIMA di qualunque lookup di matching.
NORMALIZED_DECIMALS = 2

#: Un numero con segno opzionale, intero o decimale (`2`, `2.5`, `-0.75`).
_LINE_NUM_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")


def normalize_line(raw_line: Any) -> float:
    """Linea canonica (float a 2 decimali) da qualunque formato dei provider.

    Formati gestiti (tutti osservati sulle fonti reali):
      - numeri (`2.5`, `-0.75`, `0`) e stringhe numeriche (`'2.50'`, `'+0.25'`);
      - testi con la linea dentro (`'Over 2.5'`, `'Cagliari -0.75'`);
      - linea quarter espressa come DUE mezze-linee: `'0.0, 0.5'` -> **0.25**
        (la media delle due, che e' la convenzione del mercato).

    Le linee reali sono multipli di 0.25, quindi 2 decimali non perdono nulla
    e rendono ESATTO il confronto fra le due fonti.

    Solleva `ValueError` su un input non interpretabile: **0.0 e' una linea
    valida** (handicap pari), quindi non si puo' usare 0 come "assente" — un
    valore inventato sarebbe peggio di un errore dichiarato. I chiamanti
    fail-closed usano `normalize_line_or_none`.
    """
    if isinstance(raw_line, bool):
        raise ValueError("linea booleana")
    if isinstance(raw_line, (int, float)):
        value = float(raw_line)
        if not math.isfinite(value):
            raise ValueError(f"linea non finita: {raw_line!r}")
        return round(value, NORMALIZED_DECIMALS)
    text = str(raw_line or "").strip()
    if not text:
        raise ValueError("linea vuota")
    nums = _LINE_NUM_RE.findall(text)
    if not nums:
        raise ValueError(f"nessun numero in {text!r}")
    # `'0.0, 0.5'` / `'+0.25, +0.5'`: linea quarter = MEDIA delle due mezze-linee.
    if len(nums) >= 2 and "," in text:
        a, b = float(nums[0]), float(nums[1])
        if not (math.isfinite(a) and math.isfinite(b)):
            raise ValueError(f"linea non finita in {text!r}")
        return round((a + b) / 2.0, NORMALIZED_DECIMALS)
    value = float(nums[0])
    if not math.isfinite(value):
        raise ValueError(f"linea non finita in {text!r}")
    return round(value, NORMALIZED_DECIMALS)


def normalize_line_or_none(raw_line: Any) -> Optional[float]:
    """`normalize_line` in versione fail-closed: None invece di eccezione."""
    try:
        return normalize_line(raw_line)
    except Exception:
        return None


def _epoch_of(ts: Any) -> Optional[float]:
    """Istante epoch (secondi UTC) da ISO ('T'/'Z'/spazio), datetime o numero.

    Un numero grande e' interpretato come MILLISECONDI (convenzione SX): senza
    questa conversione `minutes_to_kickoff` darebbe milioni di minuti e il TTL
    finirebbe nel tier sbagliato. None se non interpretabile (fail-closed).
    """
    if ts is None or isinstance(ts, bool):
        return None
    if isinstance(ts, (int, float)):
        value = float(ts)
        if not math.isfinite(value) or value <= 0:
            return None
        return value / 1000.0 if value > 1e11 else value
    try:
        dt = datetime.fromisoformat(str(ts).strip().replace("Z", "+00:00")
                                    .replace(" ", "T"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def is_sharp(bookmaker: Any) -> bool:
    """True se la chiave/titolo del bookmaker e' un book sharp (Pinnacle)."""
    name = _cf(bookmaker)
    return any(s in name for s in SHARP_BOOKS)


def h2h_odds_of(bookmaker: Dict[str, Any], home: str, away: str,
                *, outcomes: Tuple[str, ...] = OUTCOMES
                ) -> Optional[Dict[str, float]]:
    """Quote del mercato testa-a-testa di UN bookmaker. None se incomplete.

    FAIL-CLOSED su TUTTI gli esiti attesi: per de-vigare servono completi. Con
    due su tre il margine dell'esito mancante verrebbe attribuito agli altri e
    la probabilita' "vera" risulterebbe sbagliata **senza che nulla lo dica** —
    meglio nessun oracolo che un oracolo distorto.

    `outcomes` e' la forma del mercato: i tre esiti 1X2 di default (calcio:
    comportamento INVARIATO) oppure due esiti (`("1", "2")`) per i mercati
    testa-a-testa SENZA pareggio — tennis ed eSports (30/09/2026). Il chiamante
    DICHIARA la forma: non si deduce dal payload, perche' un 1X2 a cui il book
    ha pubblicato solo due quote non deve passare per un mercato a due esiti.
    """
    if not isinstance(bookmaker, dict):
        return None
    wanted = tuple(outcomes)
    h, a = _cf(home), _cf(away)
    out: Dict[str, float] = {}
    for mkt in bookmaker.get("markets") or []:
        if not isinstance(mkt, dict) or mkt.get("key") != "h2h":
            continue
        for o in mkt.get("outcomes") or []:
            if not isinstance(o, dict):
                continue
            try:
                price = float(o.get("price"))
            except (TypeError, ValueError):
                continue
            if price <= 1.0:
                continue
            name = _cf(o.get("name"))
            if name == h:
                out["1"] = price
            elif name == a:
                out["2"] = price
            elif name in ("draw", "pareggio", "x"):
                out["X"] = price
    return out if all(e in out for e in wanted) else None


def totals_odds_of(bookmaker: Dict[str, Any], line: float
                   ) -> Optional[Dict[str, float]]:
    """Quote Over/Under di Pinnacle ALLA LINEA richiesta ({"Over": q, "Under": q}).

    ⚠️ FORMA DEL PAYLOAD (verificata su dati reali, 30/09): nel mercato
    `totals` di the-odds-api il campo `point` (2.5, 3.25...) sta sull'
    OUTCOME, non sull'oggetto mercato — il lettore che cercava il punto a
    livello mercato trovava None e scartava TUTTE le linee. Fail-closed:
    servono ENTRAMBI i lati alla STESSA linea (un solo lato non si puo'
    de-vigare) con prezzi > 1.0; confronto con tolleranza 1e-6 (i
    quarter-line 2.25/2.75 sono frazionari).

    Entrambe le linee (quella richiesta e quella del payload) passano per
    `normalize_line` PRIMA del confronto: `'2.50'`, `2.5` e `'2,50'` sono la
    stessa linea e devono agganciarsi (05/10/2026).
    """
    if not isinstance(bookmaker, dict):
        return None
    # Linea richiesta in forma CANONICA: il confronto con il payload deve
    # avvenire fra due valori normalizzati (lezione del falso disallineamento).
    want = normalize_line_or_none(line)
    if want is None:
        return None
    out: Dict[str, float] = {}
    for mkt in bookmaker.get("markets") or []:
        if not isinstance(mkt, dict) or mkt.get("key") != "totals":
            continue
        for o in mkt.get("outcomes") or []:
            if not isinstance(o, dict):
                continue
            try:
                price = float(o.get("price"))
            except (TypeError, ValueError):
                continue
            point = normalize_line_or_none(o.get("point"))
            if point is None or price <= 1.0 or abs(point - want) > 1e-6:
                continue
            name = _cf(o.get("name"))
            if name.startswith("over"):
                out["Over"] = price
            elif name.startswith("under"):
                out["Under"] = price
    return out if ("Over" in out and "Under" in out) else None


def spreads_odds_of(bookmaker: Dict[str, Any], home: str, away: str,
                    home_line: float) -> Optional[Dict[str, float]]:
    """Quote Asian Handicap di Pinnacle al lato CASA con linea `home_line`.

    `home_line` e' la linea vista da `teamOne` (es. -0.75 per "Home -0.75"),
    la STESSA convenzione di `multi_market.sx_line_of_esito`/`order_target`.
    Nel payload the-odds-api gli esiti spreads sono identificati da NOME
    SQUADRA + `point` SULL'OUTCOME (il mercato non porta il punto — forma
    verificata su dati reali, 30/09): l'esito di casa ha `point =
    home_line` e nome della squadra di casa, il trasferta `point =
    -home_line`. Matching: nome
    (case-fold, con ripiego su contenimento per le varianti) + linea; se i
    nomi non combaciano MA le due linee sono speculari e DISTINTE, decide
    il punto (difesa per forme di payload diverse). Fail-closed su ENTRAMBI
    i lati con prezzi > 1.0; a linea 0 le linee coincidono e decide SOLO il
    nome (mai un lato assegnato a caso).
    """
    if not isinstance(bookmaker, dict):
        return None
    h, a = _cf(home), _cf(away)
    if not h or not a:
        return None
    line = normalize_line_or_none(home_line)
    if line is None:
        return None
    out: Dict[str, float] = {}
    fallback_home: Optional[float] = None
    fallback_away: Optional[float] = None
    for mkt in bookmaker.get("markets") or []:
        if not isinstance(mkt, dict) or mkt.get("key") != "spreads":
            continue
        for o in mkt.get("outcomes") or []:
            if not isinstance(o, dict):
                continue
            try:
                price = float(o.get("price"))
            except (TypeError, ValueError):
                continue
            point = normalize_line_or_none(o.get("point"))
            if point is None or price <= 1.0:
                continue
            name = _cf(o.get("name"))
            name_home = name == h or (h in name) or (name in h)
            name_away = name == a or (a in name) or (name in a)
            if name_home and abs(point - line) <= 1e-6:
                out["Home"] = price
            elif name_away and abs(point + line) <= 1e-6:
                out["Away"] = price
            # Difesa per payload senza nomi utilizzabili: linee speculari
            # DISTINTE identificano i lati dal punto (a linea 0 sono uguali:
            # resta fail-closed).
            if abs(line) > 1e-9:
                if abs(point - line) <= 1e-6 and fallback_home is None:
                    fallback_home = price
                if abs(point + line) <= 1e-6 and fallback_away is None:
                    fallback_away = price
    if "Home" not in out and fallback_home is not None \
            and fallback_away is not None:
        out["Home"] = fallback_home
        out["Away"] = fallback_away
    return out if ("Home" in out and "Away" in out) else None


def _find_match(payload: Sequence[Dict[str, Any]], home: str, away: str
                ) -> Optional[Dict[str, Any]]:
    """La partita del payload con ENTRAMBE le squadre (case-fold, spazi)."""
    h, a = _cf(home), _cf(away)
    for match in payload or []:
        if not isinstance(match, dict):
            continue
        if _cf(match.get("home_team")) == h and _cf(match.get("away_team")) == a:
            return match
    return None


def canonical_book(bookmaker: Any) -> Optional[str]:
    """Chiave canonica della fonte (pinnacle / betfair_ex_eu / matchbook).

    Matching per SOTTOSTRINGA su `key` quando presente (quindi
    `betfair_ex_eu` e `betfair_ex_uk` cadono nella stessa fonte benchmark),
    sul `title` solo se la `key` manca: una chiave nota e non-sharp non
    diventa sharp per via di un titolo fuorviante.
    """
    if not isinstance(bookmaker, dict):
        return None
    key = _cf(bookmaker.get("key"))
    title = _cf(bookmaker.get("title"))
    if key:
        # Chiave PRESENTE: e' lei ad avere l'ultima parola. Una chiave nota e
        # non-sharp non diventa sharp per via di un titolo fuorviante (era il
        # comportamento di `is_sharp(key or title)`, che qui resta invariato).
        for canon, patterns in BOOK_PATTERNS.items():
            for pat in patterns:
                if pat in key:
                    return canon
        return None
    for canon, patterns in BOOK_PATTERNS.items():
        for pat in patterns:
            if pat in title:
                return canon
    return None


def book_quotes(match: Dict[str, Any], home: str, away: str,
                book: str = PRIMARY_BOOK, *,
                outcomes: Tuple[str, ...] = OUTCOMES
                ) -> Optional[Dict[str, float]]:
    """Quote di UNA fonte specifica per la partita. None se incomplete.

    Fail-closed su TUTTI gli esiti attesi (vedi `h2h_odds_of`). Funzione PURA:
    nessuna rete, nessun credito, nessuna scrittura.
    """
    if not isinstance(match, dict):
        return None
    for bm in match.get("bookmakers") or []:
        if not isinstance(bm, dict):
            continue
        if canonical_book(bm) != book:
            continue
        got = h2h_odds_of(bm, home, away, outcomes=outcomes)
        if got:
            return got
    return None


def pinnacle_quotes(payload: Sequence[Dict[str, Any]], home: str, away: str,
                    *, outcomes: Tuple[str, ...] = OUTCOMES
                    ) -> Optional[Dict[str, float]]:
    """Quote di Pinnacle per UNA partita del payload. None se assenti.

    Resta **specifica su Pinnacle** (la fonte primaria): le altre fonti del
    consenso si estraggono con `oracle_quotes`/`book_quotes`.
    """
    match = _find_match(payload, home, away)
    if match is None:
        return None
    return book_quotes(match, home, away, PRIMARY_BOOK, outcomes=outcomes)


def oracle_quotes(payload: Sequence[Dict[str, Any]], home: str, away: str,
                  *, outcomes: Tuple[str, ...] = OUTCOMES
                  ) -> Dict[str, Dict[str, float]]:
    """{fonte: quote} per OGNI fonte sharp completa sulla partita.

    Chiavi in `CONSENSUS_BOOKS`. Solo le fonti con TUTTI gli esiti attesi
    (fail-closed come `h2h_odds_of`): una fonte parziale non entra nel
    consenso. `{}` se la partita non c'e' o nessuna fonte sharp e' completa.
    """
    match = _find_match(payload, home, away)
    if match is None:
        return {}
    out: Dict[str, Dict[str, float]] = {}
    for bm in match.get("bookmakers") or []:
        if not isinstance(bm, dict):
            continue
        book = canonical_book(bm)
        if not book or book in out:
            continue
        got = h2h_odds_of(bm, home, away, outcomes=outcomes)
        if got:
            out[book] = got
    return out


def iter_pinnacle_markets(payload: Sequence[Dict[str, Any]]
                          ) -> List[Tuple[Dict[str, Any], Dict[str, float]]]:
    """[(partita, quote 1X2 Pinnacle)] per ogni partita con oracolo completo."""
    out: List[Tuple[Dict[str, Any], Dict[str, float]]] = []
    for match in payload or []:
        if not isinstance(match, dict):
            continue
        home = match.get("home_team") or ""
        away = match.get("away_team") or ""
        if not home or not away:
            continue
        quotes = pinnacle_quotes([match], home, away)
        if quotes:
            out.append((match, quotes))
    return out


# ---------------------------------------------------------------------------
# 2. TRUE PROBABILITY: de-vig (delega a market_calib, nessuna copia)
# ---------------------------------------------------------------------------

def true_probabilities(odds_map: Dict[str, float], *,
                       method: Optional[str] = None,
                       min_outcomes: int = 3
                       ) -> Optional[Dict[str, Any]]:
    """Quote sharp -> probabilita' fair (somma 1) + `overround`.

    Delega a `market_calib.market_implied` (metodi: power / multiplicative /
    shin). Richiede **almeno `min_outcomes` esiti**: su un 1X2 de-vigare 2 quote
    su 3 sposterebbe sugli altri il margine dell'esito mancante. `min_outcomes`
    e' 3 di default (calcio, INVARIATO) e 2 per i mercati testa-a-testa senza
    pareggio (tennis: due esiti, `("1", "2")`).
    """
    if not odds_map or len(odds_map) < int(min_outcomes):
        return None
    try:
        from market_calib import market_implied
    except Exception as exc:                                    # pragma: no cover
        logger.warning("pinnacle_oracle: market_calib non disponibile (%s)", exc)
        return None
    result = market_implied({k: float(v) for k, v in odds_map.items()},
                            method=method or DEVIG_METHOD)
    if not result:
        return None
    return result


def shin_z(odds_map: Dict[str, float], *, min_outcomes: int = 3
           ) -> Optional[float]:
    """Parametro z di Shin (1992/93) per un mercato sharp: nulla o un numero.

    NULLA se il mercato non e' completo (`min_outcomes`), se `market_calib`
    non e' disponibile o se il de-vig fallisce: un valore inventato sarebbe
    peggio di nessun valore. Delega a `market_calib.devig_with_z` — la
    formula di z vive in UN solo posto e i chiamanti non la ricopiano.
    """
    if not odds_map or len(odds_map) < int(min_outcomes):
        return None
    try:
        from market_calib import devig_with_z
    except Exception as exc:                                    # pragma: no cover
        logger.debug("pinnacle_oracle: market_calib non disponibile (%s)", exc)
        return None
    try:
        fair, z = devig_with_z([float(v) for v in odds_map.values()],
                               method="shin")
    except Exception:
        return None
    if not fair or z is None:
        return None
    return round(float(z), 6)


def line_probabilities(quotes: Dict[str, float],
                       *, devig_method: Optional[str] = None
                       ) -> Optional[Dict[str, Any]]:
    """De-vig a 2 esiti per i mercati A LINEA (OU/AH): {lato: p, overround}.

    Il gate top-down compara la quota del segnale con la p_true del lato
    GIOCATO ('Over 2.5' -> 'Over'; 'Home -0.75' -> 'Home'): due esiti basta
    e il de-vig e' ESATTO (su un binario il margine e' ripartito interamente
    fra i due). Delega a `market_calib.market_implied` (stesso metodo del
    progetto: power). None se manca un lato o i prezzi sono degeneri:
    mai un oracolo distorto (fail-closed come `h2h_odds_of`).
    """
    if not quotes or len(quotes) != 2:
        return None
    return true_probabilities(quotes, method=devig_method, min_outcomes=2)


def fair_odds(true_probs: Dict[str, Any]) -> Dict[str, float]:
    """'True Odd' per esito = 1 / probabilita' fair (quota equa, senza vig).

    Ignora le chiavi di servizio (`overround`) e qualunque valore non
    interpretabile come probabilita' in (0, 1].
    """
    out: Dict[str, float] = {}
    for key, value in (true_probs or {}).items():
        if key in _META_KEYS:
            continue
        try:
            p = float(value)
        except (TypeError, ValueError):
            continue
        if 0.0 < p <= 1.0:
            out[str(key)] = 1.0 / p
    return out


# ---------------------------------------------------------------------------
# 2b. CONSENSO MULTI-ORACOLO (26/09/2026)
# ---------------------------------------------------------------------------

def _aggregate(fairs: Sequence[Dict[str, float]], method: str, *,
               outcomes: Tuple[str, ...] = OUTCOMES) -> Dict[str, float]:
    """Media (o mediana) per esito delle probabilita' fair, RINORMALIZZATA.

    Ogni fonte somma gia' 1 per costruzione (de-vig); l'aggregato di piu'
    fonti somma ancora 1 con la media, ma la rinormalizzazione e' una difesa
    a costo zero contro arrotondamenti e fonti parziali.
    """
    out: Dict[str, float] = {}
    for esito in tuple(outcomes):
        vals = [float(f[esito]) for f in fairs if esito in f]
        if not vals:
            continue
        out[esito] = (statistics.median(vals) if method == "median"
                      else sum(vals) / len(vals))
    total = sum(out.values())
    if total <= 0:
        return {}
    return {k: v / total for k, v in out.items()}


def _consensus_result(fair: Dict[str, float], *, sources: Sequence[str],
                      method: str, validated: Optional[bool],
                      fallback: Optional[str],
                      agreement_pp: Optional[float] = None,
                      overrounds: Optional[Sequence[float]] = None,
                      outcomes: Tuple[str, ...] = OUTCOMES,
                      devig_method: Optional[str] = None,
                      shin_z_value: Optional[float] = None
                      ) -> Dict[str, Any]:
    """Struttura del consenso: esiti numerici + METADATI di servizio.

    I metadati stanno nelle stesse chiavi ma sono dichiarati in `_META_KEYS`,
    quindi `fair_odds`/`ev_gate` li saltano e non possono mai finire in un
    calcolo di EV o di true-odd. `devig_method` e `shin_z` rendono ISPEZIONABILE
    la pulizia applicata alle quote sharp: un z alto = mercato guidato da
    denaro informato.
    """
    out: Dict[str, Any] = {e: round(float(fair[e]), 6)
                           for e in tuple(outcomes) if e in fair}
    ov = [float(x) for x in (overrounds or [])]
    out["overround"] = round(sum(ov) / len(ov), 5) if ov else None
    out["sources"] = list(sources)
    out["n_sources"] = len(sources)
    out["consensus_method"] = method
    out["validated"] = validated
    out["fallback"] = fallback
    out["agreement_pp"] = agreement_pp
    out["devig_method"] = devig_method
    out["shin_z"] = shin_z_value
    return out


def consensus_probabilities(quotes_by_book: Dict[str, Dict[str, float]], *,
                            method: Optional[str] = None,
                            devig_method: Optional[str] = None,
                            validator_tolerance: Optional[float] = None,
                            enabled: Optional[bool] = None,
                            outcomes: Tuple[str, ...] = OUTCOMES
                            ) -> Optional[Dict[str, Any]]:
    """Probabilita' "vera" di CONSENSO dalle fonti sharp disponibili.

    `outcomes` e' la FORMA del mercato: i tre esiti 1X2 di default (calcio,
    comportamento INVARIATO) oppure due esiti (`("1", "2")`) per i mercati
    testa-a-testa senza pareggio (tennis/eSports). La forma governa de-vig,
    aggregazione e validatore: mai dedotta dal payload.

    Ogni fonte con il mercato completo viene de-vigata (`true_probabilities`,
    stessa formula del progetto: nessun doppio standard) e le probabilita'
    fair vengono aggregate:
      - benchmark = Pinnacle + Betfair Exchange (aggregato per esito);
      - Matchbook = validatore: entra nell'aggregato SOLO se la sua fair resta
        entro `validator_tolerance` dal benchmark; se diverge troppo viene
        ESCLUSO e il disallineamento e' registrato (`validated=False`).
    **Fallback robusto**: con una sola fonte il consenso E' quella fonte
    (Pinnacle-only = comportamento storico, identico bit per bit).

    Ritorna None solo se NESSUNA fonte ha un 1X2 completo (nessuna verita' =
    nessun oracolo). Mai eccezioni. Override `enabled=False` (env
    `PINNACLE_CONSENSUS=0`) per ripristinare la Pinnacle-secca.
    """
    if not quotes_by_book:
        return None
    if enabled is None:
        enabled = CONSENSUS_ENABLED
    devig = devig_method or DEVIG_METHOD
    m = (method or CONSENSUS_METHOD or "mean").strip().lower()
    if m not in ("mean", "median"):
        logger.warning("pinnacle_oracle: metodo consenso '%s' ignoto — uso mean", m)
        m = "mean"
    tol = (VALIDATOR_TOLERANCE if validator_tolerance is None
           else float(validator_tolerance))
    wanted = tuple(outcomes)
    n_wanted = len(wanted)
    if n_wanted < 2:
        logger.warning("pinnacle_oracle: forma di mercato con %d esiti — "
                       "serve almeno 2", n_wanted)
        return None

    fairs: Dict[str, Dict[str, float]] = {}
    overrounds: List[float] = []
    for book in CONSENSUS_BOOKS:
        odds = quotes_by_book.get(book)
        if not odds:
            continue
        probs = true_probabilities(odds, method=devig, min_outcomes=n_wanted)
        if not probs:
            continue
        fair = {e: float(probs[e]) for e in wanted if e in probs}
        total = sum(fair.values())
        if len(fair) != n_wanted or total <= 0:
            continue
        fairs[book] = {k: v / total for k, v in fair.items()}
        if probs.get("overround") is not None:
            overrounds.append(float(probs["overround"]))
    if not fairs:
        return None

    # z di Shin: misurato sulla fonte PRIMARIA disponibile (Pinnacle quando
    # c'e'), cosi' il numero e' confrontabile fra giri. Solo con devig shin.
    z_value: Optional[float] = None
    if devig == "shin":
        for book in CONSENSUS_BOOKS:
            odds = quotes_by_book.get(book)
            if not odds:
                continue
            z_value = shin_z(odds, min_outcomes=n_wanted)
            if z_value is not None:
                break

    if not enabled:
        only = PRIMARY_BOOK if PRIMARY_BOOK in fairs else next(iter(fairs))
        return _consensus_result(fairs[only], sources=[only], method="single",
                                 validated=None, fallback="consensus_disabled",
                                 overrounds=overrounds, outcomes=wanted,
                                 devig_method=devig, shin_z_value=z_value)

    base_books = [b for b in (PRIMARY_BOOK, BENCHMARK_BOOK) if b in fairs]
    used = list(base_books)
    fallback: Optional[str] = None
    validated: Optional[bool] = None
    agreement_pp: Optional[float] = None
    if not base_books:
        # Nessuna fonte "base": si degrada con grazia sul validatore, poi
        # sull'unica fonte disponibile. Mai un blocco della pipeline.
        if VALIDATOR_BOOK in fairs:
            used = [VALIDATOR_BOOK]
            fallback = "validator_only"
        else:
            only = next(iter(fairs))
            used = [only]
            fallback = f"single_source:{only}"
    base = _aggregate([fairs[b] for b in base_books or used], m, outcomes=wanted)
    if VALIDATOR_BOOK in fairs and VALIDATOR_BOOK not in used and base:
        val = fairs[VALIDATOR_BOOK]
        dev = max(abs(val[e] - base.get(e, 0.0)) for e in wanted)
        agreement_pp = round(dev * 100.0, 2)
        if dev <= tol:
            used.append(VALIDATOR_BOOK)
            validated = True           # conferma il consenso
        else:
            validated = False          # escluso: disallineamento registrato
            logger.warning(
                "pinnacle_oracle: validatore %s in disaccordo (%.1fpp > "
                "%.1fpp) — ESCLUSO dal consenso", VALIDATOR_BOOK,
                dev * 100.0, tol * 100.0)
    final = _aggregate([fairs[b] for b in used], m, outcomes=wanted)
    if not final:
        return None
    if fallback is None and len(used) == 1:
        fallback = ("pinnacle_only" if used[0] == PRIMARY_BOOK
                    else f"single_source:{used[0]}")
    return _consensus_result(final, sources=used, method=m, validated=validated,
                             fallback=fallback, agreement_pp=agreement_pp,
                             overrounds=overrounds, outcomes=wanted,
                             devig_method=devig, shin_z_value=z_value)


# ---------------------------------------------------------------------------
# 3. ORACOLO PER PARTITA: la p_true DALLE CACHE (0 crediti) — fase 2
# ---------------------------------------------------------------------------

def _cache_candidates(cache_dir: Optional[Path] = None) -> List[Path]:
    """Cache quote ordinate per freschezza (prima la piu' recente).

    Sono le STESSE cache della rotazione quote (`toa_<sport>.json`): il
    percorso dell'oracolo NON chiama mai l'API, quindi costa 0 crediti.
    L'ordinamento serve a decidere in modo deterministico se la stessa
    partita compare in piu' cache (es. una squadra in coppa e in campionato).
    """
    files = _cache_files(Path(cache_dir) if cache_dir else Path(DATA_DIR))
    try:
        def _ts(p: Path) -> float:
            try:
                return float(json.loads(p.read_text(encoding="utf-8")).get("ts") or 0)
            except Exception:
                return 0.0
        files.sort(key=_ts, reverse=True)
    except Exception:
        pass
    return files


#: Un oracolo piu' vecchio di cosi' non e' piu' il mercato: e' storia. Le
#: quote di una partita muovono (specie vicino al kickoff) e la cache si
#: riscrive a ogni fetch — il tetto serve solo a NON decidere su un
#: palinsesto abbandonato (chiave sostituita, lega non piu' interrogata).
#: Override: PINNACLE_CACHE_MAX_AGE_H.
CACHE_MAX_AGE_H: float = float(os.getenv("PINNACLE_CACHE_MAX_AGE_H", "24"))

#: --- TTL DINAMICO sul TEMPO AL KICKOFF (05/10/2026) -----------------------
#: Nella finestra pre-match le linee di spread si muovono: un TTL FISSO (le 24h
#: del tetto qui sopra) tiene per "valido" un dato che il mercato ha gia'
#: cambiato, e il matching fallisce per un falso disallineamento. Il TTL e'
#: quindi funzione del TEMPO AL KICKOFF: si e' severi dove il prezzo sta per
#: diventare eseguibile, si risparmia quando il kickoff e' lontano.
#:   T > 180 min    -> 30 min (risparmio crediti: la linea non e' ancora viva)
#:   60 <= T <= 180 -> 5 min  (freschezza per la finestra T-180)
#:   T < 60 min     -> 2 min  (alta frequenza: il prezzo puo' muoversi in fretta)
#: Le SOGLIE di tempo (180/60) sono fisse; i VALORI di TTL sono da env, cosi'
#: si possono tarare senza redeploy (valore impossibile -> default dichiarato).
TTL_FAR_MINUTES: float = 180.0
TTL_NEAR_MINUTES: float = 60.0
DEFAULT_TTL_LONG_MIN: float = 30.0      # T > 180
DEFAULT_TTL_MID_MIN: float = 5.0        # 60 <= T <= 180
DEFAULT_TTL_SHORT_MIN: float = 2.0      # T < 60


def _ttl_env(name: str, default: float) -> float:
    """Valore di TTL da env; assente/impossibile -> default dichiarato.

    Una guardia non si spegne con un env sbagliato: un valore non numerico o
    non positivo ricade sul default con un warning (mai in silenzio).
    """
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning("pinnacle_oracle: %s=%r non numerico, uso %.1f",
                       name, raw, default)
        return default
    if value <= 0:
        logger.warning("pinnacle_oracle: %s=%r non positivo, uso %.1f",
                       name, raw, default)
        return default
    return value


def cache_ttl_minutes(minutes_to_kickoff: Optional[float]) -> float:
    """TTL (minuti) dell'oracolo a linea per un kickoff a N minuti di distanza.

    **UNICA definizione del TTL dinamico**: la usano la lettura della cache
    (`_oracle_fixture_status`), `line_oracle.leagues_needing_fetch` (la
    decisione di REFETCH) e `odds_api.oracle_cache_ttl_s`. Un tempo al kickoff
    IGNOTO (None) o gia' passato usa il tier piu' CONSERVATIVO (2 min):
    un'incertezza non allunga MAI la vita di un dato.
    """
    long_min = _ttl_env("PINNACLE_TTL_LONG_MIN", DEFAULT_TTL_LONG_MIN)
    mid_min = _ttl_env("PINNACLE_TTL_MID_MIN", DEFAULT_TTL_MID_MIN)
    short_min = _ttl_env("PINNACLE_TTL_SHORT_MIN", DEFAULT_TTL_SHORT_MIN)
    try:
        t = float(minutes_to_kickoff)
    except (TypeError, ValueError):
        return short_min
    if not math.isfinite(t) or t < TTL_NEAR_MINUTES:
        return short_min
    if t > TTL_FAR_MINUTES:
        return long_min
    return mid_min


def minutes_to_kickoff(kickoff: Any, *,
                       now: Optional[float] = None) -> Optional[float]:
    """Minuti che mancano al kickoff. None se il kickoff non e' interpretabile."""
    ts = _epoch_of(kickoff)
    if ts is None:
        return None
    ts_now = time.time() if now is None else float(now)
    return (ts - ts_now) / 60.0


_CACHE_MEMO: Dict[Path, tuple] = {}


def _read_cache(path: Path) -> Optional[Dict[str, Any]]:
    """Contenuto di UNA cache con memo su (mtime, size).

    Il giro ordini gira ogni 60s e piu' pick condividono la stessa lega:
    senza memo si rileggerebbero gli stessi file decine di volte al minuto.
    La chiave include mtime e size, quindi una cache riscritta da un fetch
    si auto-invalida e una cartella diversa (test) non condivide nulla.
    """
    try:
        st = path.stat()
        key = (st.st_mtime_ns, st.st_size)
    except Exception:
        return None
    hit = _CACHE_MEMO.get(path)
    if hit is not None and hit[0] == key:
        return hit[1]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        data = None
    if len(_CACHE_MEMO) > 64:
        _CACHE_MEMO.clear()
    _CACHE_MEMO[path] = (key, data)
    return data


def _iter_cached_matches(home: str, away: str, *, cache_dir: Optional[Path] = None,
                         sport_key: Optional[str] = None,
                         now: Optional[float] = None):
    """Genera `(match, data, path)` per la partita nelle cache FRESCHE.

    UNICO punto di scansione del percorso a costo zero (nome squadre per
    sottostringa + controllo di freschezza): `load_oracle` e
    `pinnacle_odds_from_cache` non possono divergere su COME si trova la
    partita. Ordinamento per cache piu' recente.
    """
    folder = Path(cache_dir) if cache_dir else Path(DATA_DIR)
    paths = ([folder / f"toa_{sport_key}.json"] if sport_key
             else _cache_candidates(folder))
    ts_now = time.time() if now is None else float(now)
    h, a = _cf(home), _cf(away)
    if not h or not a:
        return
    for path in paths:
        data = _read_cache(path)
        if not isinstance(data, dict):
            continue
        age_h = (ts_now - float(data.get("ts") or 0)) / 3600.0
        if age_h > CACHE_MAX_AGE_H:
            continue
        for match in (data.get("payload") or []):
            # Matching delegato a `_row_matches` (UNICA definizione: contenimento
            # + fallback tollerante `team_names.same_team`). Prima questa
            # funzione duplicava la sola sottostringa, quindi `load_oracle`
            # (1X2) soffriva dello stesso falso "partita assente" del percorso a
            # linea (05/10/2026).
            if not _row_matches(match, h, a):
                continue
            yield match, data, path


def load_oracle(home: str, away: str, sport_key: Optional[str] = None, *,
                cache_dir: Optional[Path] = None,
                devig_method: Optional[str] = None,
                now: Optional[float] = None,
                outcomes: Tuple[str, ...] = OUTCOMES
                ) -> Optional[Dict[str, Any]]:
    """Probabilita' "vera" (di CONSENSO) per esito per UNA partita, dalle cache.

    E' il punto di aggancio della fase 2: il giro ordini chiede QUI la
    p_true e calcola l'EV del segnale sull'oracolo invece che sul modello.
    Dal 26/09 la p_true e' il CONSENSO delle fonti sharp disponibili
    (`consensus_probabilities`): Pinnacle + Betfair sono il benchmark,
    Matchbook un validatore. Con la sola Pinnacle il valore coincide col
    comportamento storico (fallback automatico, mai un blocco).

    **Costo: 0 crediti** — legge i file `toa_<sport>.json` che la rotazione
    quote scarica gia' (nessuna chiamata HTTP in questo percorso; il percorso
    `--live` resta diagnostica a parte). Scelta della partita per SOTTOSTRINGA
    case-insensitive: i nomi the-odds-api del payload coincidono con quelli
    del segnale (stessa fonte), ma restano varianti tipo "Tottenham Hotspur".
    Due partite diverse non si confondono: `home` e `away` devono combaciare
    ENTRAMBE nella stessa riga del payload.

    Fail-closed: None se la partita non e' nel payload, se NESSUNA fonte
    sharp ha TUTTI gli esiti attesi (de-vig su 2 su 3 distorcerebbe l'oracolo)
    o se la cache e' piu' vecchia di `CACHE_MAX_AGE_H` — un oracolo stantio non
    e' il mercato.

    **`outcomes` = la FORMA del mercato** (30/09/2026): i tre esiti 1X2 di
    default (calcio: percorso INVARIATO) oppure due esiti (`("1", "2")`) per i
    testa-a-testa SENZA pareggio — tennis (SX sportId 6, type 52) ed eSports.
    La forma la DICHIARA il chiamante: non si deduce, altrimenti un 1X2 a cui
    il book ha pubblicato due quote passerebbe per un mercato a due esiti. Con
    `outcomes=("1","2")` l'aggancio della partita resta per NOMI (i due
    partecipanti devono combaciare sulla STESSA riga): non serve alcuna mappa
    torneo -> chiave sport, sono i nomi a fare da ponte — utile perche' le
    chiavi tennis della the-odds-api cambiano a ogni torneo.

    Args:
        home, away: nomi squadre/giocatori del segnale (the-odds-api).
        sport_key: opzionale, restringe la lettura a UNA cache
            (`toa_<sport_key>.json`). None = ricerca su tutte, dalla piu'
            recente (una squadra puo' comparire in due competizioni).
        devig_method: override puntuale del metodo (default DEVIG_METHOD).
    """
    for match, _data, _path in _iter_cached_matches(
            home, away, cache_dir=cache_dir, sport_key=sport_key, now=now):
        by_book = oracle_quotes([match],
                                match.get("home_team") or "",
                                match.get("away_team") or "",
                                outcomes=outcomes)
        if not by_book:
            continue                   # fail-closed: nessuna fonte completa
        probs = consensus_probabilities(by_book, devig_method=devig_method,
                                        outcomes=outcomes)
        if probs:
            return probs
    return None


def pinnacle_odds_from_cache(home: str, away: str,
                             sport_key: Optional[str] = None, *,
                             cache_dir: Optional[Path] = None,
                             now: Optional[float] = None,
                             outcomes: Tuple[str, ...] = OUTCOMES
                             ) -> Optional[Dict[str, Any]]:
    """Quote GREZZE di Pinnacle per la partita, dalle cache. **0 crediti.**

    NON e' un oracolo: nessun de-vig, nessuna probabilita'. Restituisce il
    prezzo PUBBLICATO dallo sharp (con il `ts` della cache e la chiave sport)
    per registrare lo STORICO dei prezzi e misurare il movimento
    (steam move). Fail-closed: None se la partita non c'e' in nessuna cache
    fresca o se Pinnacle non ha TUTTI gli esiti attesi.
    """
    for match, data, path in _iter_cached_matches(
            home, away, cache_dir=cache_dir, sport_key=sport_key, now=now):
        odds = book_quotes(match, match.get("home_team") or "",
                           match.get("away_team") or "", PRIMARY_BOOK,
                           outcomes=outcomes)
        if odds:
            stem = path.stem[len("toa_"):] if path.stem.startswith("toa_") \
                else path.stem
            return {"odds": {k: float(v) for k, v in odds.items()},
                    "ts": data.get("ts"), "sport_key": stem}
    return None


# ---------------------------------------------------------------------------
# 4. TRIGGER: EV dello scarto fra vera probabilita' e prezzo SX
# ---------------------------------------------------------------------------

def ev_gate(true_probs: Dict[str, Any], prices: Dict[str, float], *,
            ev_min: Optional[float] = None, method: Optional[str] = None
            ) -> List[Dict[str, Any]]:
    """Candidati value: per ogni esito prezzato, EV e quota minima di trigger.

    `EV = p_true x (quota - 1) - (1 - p_true)`.

    Le due letture della direttiva coincidono esattamente:
        EV >= ev_min  <=>  quota >= true_odd x (1 + ev_min)
    quindi `required_price` (la "True Odd + margine") e il gate sull'EV sono
    LA STESSA condizione, qui esposta nei due modi. Ordinati per EV decrescente.
    """
    th = DEFAULT_EV_MIN if ev_min is None else float(ev_min)
    rows: List[Dict[str, Any]] = []
    for esito, prob in (true_probs or {}).items():
        if esito in _META_KEYS:
            continue
        try:
            p = float(prob)
            price = float(prices.get(esito))
        except (TypeError, ValueError):
            continue
        if not (0.0 < p <= 1.0) or price <= 1.0:
            continue
        true_odd = 1.0 / p
        ev = p * (price - 1.0) - (1.0 - p)
        required = true_odd * (1.0 + th)
        rows.append({
            "esito": str(esito),
            "prob": round(p, 6),
            "true_odd": round(true_odd, 4),
            "price": round(price, 4),
            "required_price": round(required, 4),
            "ev": round(ev, 6),
            "edge_pp": round((p - 1.0 / price) * 100.0, 2),
            "trigger": ev >= th,
        })
    rows.sort(key=lambda r: -r["ev"])
    return rows


def value_candidates(true_probs: Dict[str, Any], prices: Dict[str, float], *,
                     ev_min: Optional[float] = None
                     ) -> List[Dict[str, Any]]:
    """Solo gli esiti che fanno SCATTARE il trigger (EV >= ev_min)."""
    return [r for r in ev_gate(true_probs, prices, ev_min=ev_min)
            if r["trigger"]]


# ---------------------------------------------------------------------------
# 5. PERCORSO A COSTO ZERO: le cache che abbiamo gia' scaricato
# ---------------------------------------------------------------------------

def _cache_files(cache_dir: Path) -> List[Path]:
    """Cache delle QUOTE (`toa_<sport>.json`), mai quelle dei punteggi."""
    try:
        return sorted(p for p in cache_dir.glob("toa_*.json")
                      if not p.name.startswith("toa_scores_"))
    except Exception:
        return []


def scan_cache(cache_dir: Optional[Path] = None, *,
               ev_min: Optional[float] = None,
               price_lookup: Optional[Callable[[Dict[str, Any], str], Optional[float]]] = None,
               max_leagues: Optional[int] = None) -> Dict[str, Any]:
    """Copertura dell'oracolo sulle cache GIA' scaricate. **Zero crediti.**

    `price_lookup(partita, esito) -> quota SX | None` e' iniettabile: senza di
    esso si misura solo la COPERTURA. Tre livelli: partite con Pinnacle
    completo, con un CONSENSO disponibile (`with_consensus`) e con PIU' fonti
    (`with_multi`, il consenso multi-oracolo vero e proprio). Il collegamento
    a SX Bet e' fase 2.
    """
    folder = Path(cache_dir) if cache_dir else Path(DATA_DIR)
    leagues: List[Dict[str, Any]] = []
    totals = {"leagues": 0, "matches": 0, "with_pinnacle": 0,
              "with_consensus": 0, "with_multi": 0,
              "candidates": 0, "price_errors": 0}
    candidates: List[Dict[str, Any]] = []
    for path in _cache_files(folder):
        if max_leagues is not None and totals["leagues"] >= int(max_leagues):
            break
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.debug("pinnacle_oracle: cache illeggibile %s (%s)", path, exc)
            continue
        payload = (data or {}).get("payload") or []
        if not payload:
            continue
        with_pin = with_cons = with_multi = 0
        overrounds: List[float] = []
        for match in payload:
            if not isinstance(match, dict):
                continue
            home = match.get("home_team") or ""
            away = match.get("away_team") or ""
            if not home or not away:
                continue
            by_book = oracle_quotes([match], home, away)
            if not by_book:
                continue
            if PRIMARY_BOOK in by_book:
                with_pin += 1
            if len(by_book) >= 2:
                with_multi += 1
            # La p_true e' il CONSENSO (fallback automatico alla sola fonte
            # disponibile: con la sola Pinnacle = comportamento storico).
            probs = consensus_probabilities(by_book)
            if not probs:
                continue
            with_cons += 1
            if probs.get("overround") is not None:
                overrounds.append(float(probs["overround"]))
            if price_lookup is None:
                continue
            # Un lookup che esplode NON deve essere indistinguibile da "il
            # book non offre nulla": altrimenti una lettura rotta si legge
            # come "zero value" (la lezione del probe BTTS del 25/09, dove
            # `_discover_type` inghiottiva l'eccezione). Si CONTA e si
            # dichiara, e la scansione prosegue sulle altre partite.
            try:
                prices = {e: price_lookup(match, e)
                          for e in ("1", "X", "2")}
            except Exception as exc:
                totals["price_errors"] += 1
                logger.debug("pinnacle_oracle: lookup prezzi fallito su %s vs "
                             "%s (%s)", match.get("home_team"),
                             match.get("away_team"), exc)
                continue
            prices = {k: v for k, v in prices.items() if v}
            for cand in value_candidates(probs, prices, ev_min=ev_min):
                candidates.append({
                    "sport": path.stem.replace("toa_", ""),
                    "event": f"{match.get('home_team')} vs {match.get('away_team')}",
                    "commence": match.get("commence_time"),
                    "sources": probs.get("sources"),
                    "validated": probs.get("validated"),
                    **cand,
                })
        totals["leagues"] += 1
        totals["matches"] += len(payload)
        totals["with_pinnacle"] += with_pin
        totals["with_consensus"] += with_cons
        totals["with_multi"] += with_multi
        leagues.append({
            "sport": path.stem.replace("toa_", ""),
            "file": path.name,
            "matches": len(payload),
            "with_pinnacle": with_pin,
            "with_consensus": with_cons,
            "with_multi": with_multi,
            "avg_overround": (round(sum(overrounds) / len(overrounds), 5)
                              if overrounds else None),
        })
    totals["candidates"] = len(candidates)
    candidates.sort(key=lambda c: -c["ev"])
    if totals["price_errors"]:
        logger.warning("pinnacle_oracle: lettura prezzi fallita in %d casi — "
                       "i candidati sono INCOMPLETI", totals["price_errors"])
    return {"cache_dir": str(folder), "leagues": leagues, "totals": totals,
            "candidates": candidates,
            "gate": {"ev_min": DEFAULT_EV_MIN if ev_min is None else ev_min,
                     "devig_method": DEVIG_METHOD,
                     "sharp_book": SHARP_BOOKS[0],
                     "consensus_books": list(CONSENSUS_BOOKS),
                     "consensus_method": CONSENSUS_METHOD,
                     "validator_tolerance": VALIDATOR_TOLERANCE}}


# ---------------------------------------------------------------------------
# 6. PERCORSO LIVE (1 credito): SOLO diagnostica, misura il costo reale
# ---------------------------------------------------------------------------

def _fetch_window_min() -> int:
    """Finestra (minuti) della query `/odds`: la STESSA dell'oracolo a linea.

    Delega a `odds_api.oracle_fetch_window_min()` (default 120 minuti, env
    `ORACLE_FETCH_WINDOW_MIN`) invece di duplicare la soglia: se i due
    percorsi usassero finestre diverse, un ramo scaricherebbe partite che
    l'altro non considera. Import PIGRO (il modulo resta leggero all'import)
    con fallback dichiarato se `odds_api` non e' disponibile.
    """
    try:
        from odds_api import oracle_fetch_window_min
        return int(oracle_fetch_window_min())
    except Exception:                                            # pragma: no cover
        return 120


def fetch_pinnacle_payload(sport_key: str, *, minutes_ahead: Optional[int] = None,
                           bookmakers: str = MULTI_BOOKMAKERS,
                           regions: str = "eu",
                           timeout: int = 30) -> Dict[str, Any]:
    """UNA chiamata `/odds` con il filtro `bookmakers` — e misura il costo.

    Ritorna {"payload", "remaining", "last_cost", "status", "error"}.
    Fail-safe: non solleva mai. Rifiuta (senza chiamare) se i crediti sono
    sotto la soglia di blocco totale del progetto.

    FINESTRA (03/10/2026): `commenceTimeTo` e' `minutes_ahead` minuti avanti
    (default `_fetch_window_min()`, 70) e NON piu' 7 giorni. Si ordina solo
    nella finestra esecutiva T-60..T-5: scaricare l'intero palinsesto
    significa pagare e parsare partite che non entreranno mai in finestra.
    ⚠️ Il costo the-odds-api e' per CHIAMATA, non per evento: la finestra
    stretta riduce il PAYLOAD (byte/parsing), non i crediti.

    Perche' esiste nonostante l'oracolo gratis sia la via di produzione:
    serve a DIMOSTRARE quanto costa una chiamata filtrata (`x-requests-last`)
    e quindi a decidere, con un numero e non con un'opinione, se un job di
    confronto continuo sia sostenibile col piano crediti attuale.
    """
    out: Dict[str, Any] = {"payload": [], "remaining": None, "last_cost": None,
                           "status": None, "error": None}
    key = (os.getenv("ODDS_API_KEY") or "").strip()
    if not key:
        out["error"] = "ODDS_API_KEY assente"
        return out
    try:
        from odds_api import credits_hard_stopped
        if credits_hard_stopped():
            out["error"] = "crediti sotto la soglia di blocco totale"
            return out
    except Exception:
        pass                                   # telemetria assente -> si procede
    window_min = _fetch_window_min() if minutes_ahead is None \
        else int(minutes_ahead)
    try:
        import requests
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        r = requests.get(ODDS_ENDPOINT.format(sport=sport_key), params={
            "apiKey": key, "regions": regions, "markets": "h2h",
            "bookmakers": bookmakers, "oddsFormat": "decimal",
            "commenceTimeFrom": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "commenceTimeTo": (now + timedelta(minutes=window_min))
                              .strftime("%Y-%m-%dT%H:%M:%SZ"),
        }, timeout=timeout)
        out["status"] = r.status_code
        for header, field in (("x-requests-remaining", "remaining"),
                              ("x-requests-last", "last_cost"),
                              ("x-requests-used", "used")):
            raw = r.headers.get(header)
            if raw is not None:
                try:
                    out[field] = int(raw)
                except (TypeError, ValueError):
                    pass
        if r.status_code != 200:
            out["error"] = f"HTTP {r.status_code}: {r.text[:200]}"
            return out
        out["payload"] = r.json() or []
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_scan(res: Dict[str, Any]) -> None:
    t = res["totals"]
    gate = res["gate"]
    print("🔺 ORACOLO PINNACLE — copertura sulle cache (0 crediti)")
    print(f"Cache in {res['cache_dir']} | devig: {gate['devig_method']} | "
          f"EV_MIN {gate['ev_min'] * 100:.1f}%")
    print(f"Leghe con quote: {t['leagues']} | partite: {t['matches']} | "
          f"con 1X2 Pinnacle completo: {t['with_pinnacle']} | "
          f"consenso: {t.get('with_consensus', 0)} "
          f"(multi-fonte: {t.get('with_multi', 0)})")
    print(f"Fonti del consenso: {' + '.join(gate.get('consensus_books') or [])} "
          f"| metodo: {gate.get('consensus_method')} | validatore tol. "
          f"{gate.get('validator_tolerance', 0.0) * 100:.1f}pp")
    for lg in res["leagues"]:
        ov = lg["avg_overround"]
        print(f"  {lg['sport']:<46} partite={lg['matches']:<3} "
              f"pinnacle={lg['with_pinnacle']:<3} "
              f"consenso={lg.get('with_consensus', 0):<3} "
              f"multi={lg.get('with_multi', 0):<3} "
              f"overround={'-' if ov is None else f'{ov:.3%}'}")
    if t.get("price_errors"):
        print(f"⚠️ lettura prezzi fallita in {t['price_errors']} casi: "
              f"i candidati sono INCOMPLETI (non 'zero value')")
    if res["candidates"]:
        print(f"Candidati value (EV >= {gate['ev_min'] * 100:.1f}%): "
              f"{t['candidates']}")
        for c in res["candidates"][:15]:
            print(f"  {c['event']} {c['esito']} @ {c['price']} "
                  f"(true {c['true_odd']}) EV {c['ev'] * 100:+.2f}%")
    else:
        print("Candidati value: non valutati (SX non collegato in fase 1)")


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Pinnacle come oracolo (fase probe: nessun ordine)")
    ap.add_argument("--from-cache", action="store_true",
                    help="scansiona le cache gia' scaricate (0 crediti)")
    ap.add_argument("--live", metavar="SPORT_KEY", default=None,
                    help="UNA chiamata /odds con bookmakers="
                         f"{MULTI_BOOKMAKERS} (1 credito)")
    ap.add_argument("--ev-min", type=float, default=None,
                    help=f"margine EV del trigger (default {DEFAULT_EV_MIN})")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if args.live:
        res = fetch_pinnacle_payload(args.live)
        if args.json:
            print(json.dumps({k: v for k, v in res.items() if k != "payload"},
                             indent=2, default=str))
        else:
            print(f"live {args.live}: status={res['status']} "
                  f"eventi={len(res['payload'])} "
                  f"crediti: rimasti={res['remaining']} "
                  f"costo_chiamata={res['last_cost']}")
            if res["error"]:
                print(f"errore: {res['error']}")
            hits = iter_pinnacle_markets(res["payload"])
            multi = 0
            for m in res["payload"]:
                if not isinstance(m, dict):
                    continue
                if len(oracle_quotes([m], m.get("home_team") or "",
                                     m.get("away_team") or "")) >= 2:
                    multi += 1
            print(f"partite con 1X2 Pinnacle completo: {len(hits)} "
                  f"| con consenso multi-fonte: {multi}")
        return 0 if not res["error"] else 1

    res = scan_cache(ev_min=args.ev_min)
    print(json.dumps(res, indent=2, ensure_ascii=False) if args.json
          else "")
    if not args.json:
        _print_scan(res)
    return 0


if __name__ == "__main__":                                    # pragma: no cover
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    raise SystemExit(main())


# ---------------------------------------------------------------------------
# 5. ORACOLO A LINEA (30/09/2026): p_true per i mercati a linea (OU/AH)
# ---------------------------------------------------------------------------
# Senza questo blocco ogni pick a linea muore con `no_oracle`: il gate che
# governa il denaro legge `probs.get(pick["esito_key"])` dove l'esito e'
# 'Over 2.5' / 'Home -0.75' — chiavi che un oracolo 1X2 non ha mai. La
# p_true arriva dai mercati `totals`/`spreads` di Pinnacle, letti dalle
# cache `toao_<sport>.json` che `odds_api.fetch_line_odds` scarica SOLO per
# le leghe con pick in gioco (follow-the-money, budget dedicato).


def h2h_cache_is_stale(home: str, away: str, *,
                       cache_dir: Optional[Path] = None,
                       now: Optional[float] = None) -> bool:
    """True se ESISTE una cache h2h (`toa_*.json`) fresca che contiene la
    partita ma Pinnacle NON ha gli esiti completi (e la cache e' dentro
    `CACHE_MAX_AGE_H`): la lega e' coperta ma l'oracolo non c'e' ANCORA.

    Distingue 'la verita' serve ma non e' stata pagata' (il motivo del gate
    diventa `linea`: si puo' scaricare con `fetch_line_odds`) da 'la partita
    non e' nel payload di nessuna cache' (no_oracle secco, non recuperabile
    a credito). Fail-closed: qualunque errore -> False (il chiamante resta
    sul motivo certo no_oracle).
    """
    folder = Path(cache_dir) if cache_dir else Path(DATA_DIR)
    ts_now = time.time() if now is None else float(now)
    h, a = _cf(home), _cf(away)
    if not h or not a:
        return False
    try:
        paths = sorted(folder.glob("toa_*.json"),
                       key=lambda p: p.stat().st_mtime if p.exists() else 0.0,
                       reverse=True)
    except Exception:
        return False
    for path in paths:
        data = _read_cache(path)
        if not isinstance(data, dict):
            continue
        age_h = (ts_now - float(data.get("ts") or 0)) / 3600.0
        if age_h > CACHE_MAX_AGE_H:
            continue
        payload = (data or {}).get("payload") or []
        for match in payload:
            if not isinstance(match, dict):
                continue
            mh = _cf(match.get("home_team"))
            ma = _cf(match.get("away_team"))
            if (not mh or not ma) or (h not in mh and mh not in h) \
                    or (a not in ma and ma not in a):
                continue
            if oracle_quotes([match], match.get("home_team") or "",
                             match.get("away_team") or ""):
                return False    # l'oracolo 1X2 c'e': non e' questo il problema
            return True         # cache fresca, partita presente, oracolo assente
    return False


def _ou_label(line: float) -> str:
    """Linea OU in formato ledger ('2.5' -> '2.5', '2.25' -> '2.25').

    `:g` perche' il ledger scrive la linea SENZA zeri inutili
    (`multi_market.ledger_esito` usa `{value:g}`).
    """
    return f"{float(line):g}"


def _ah_label(home_line: float) -> str:
    """Linea AH della GAMBA CASA in formato SX (sempre con segno esplicito
    per i positivi: la convenzione del ledger e' 'Home -0.75'/'Home +0.25').
    """
    v = float(home_line)
    return f"{v:+g}" if abs(v) >= 1e-9 else "0"


def _row_matches(match: Any, h: str, a: str) -> bool:
    """True se la riga del payload contiene ENTRAMBE le squadre.

    UNICA definizione del matching dei nomi del percorso a cache: mai
    l'incrocio (home contro away di righe diverse).

    Due stadi: contenimento di stringa (veloce, copre le varianti di suffisso
    tipo 'Tottenham' vs 'Tottenham Hotspur') e, se fallisce, il matcher
    TOLLERANTE `team_names.same_team` — lo stesso usato dal settlement dal
    12/09 (accenti, codici di stato RJ/SP/GO/BA, particelle, contenimento di
    token, con guardia di ambiguita').

    ⚠️ Perche' il secondo stadio e' necessario (misurato in produzione il
    05/10/2026): l'oracolo a linea rispondeva `MISSING_MARKET` su una partita
    CHE AVEVA IN CACHE — the-odds-api scrive `Central Córdoba`, SX
    `Central Cordoba Santiago del Estero`: nessuna delle due e' sottostringa
    dell'altra, quindi la partita risultava assente e il gate non pagava
    nulla di utile (la fetch on-demand era appena stata eseguita).
    """
    if not isinstance(match, dict):
        return False
    mh = _cf(match.get("home_team"))
    ma = _cf(match.get("away_team"))
    if not (mh and ma) or not (h and a):
        return False
    if (h in mh or mh in h) and (a in ma or ma in a):
        return True
    try:
        from team_names import same_team
    except Exception:                                            # pragma: no cover
        return False
    return bool(same_team(match.get("home_team"), h)
                and same_team(match.get("away_team"), a))


def _oracle_fixture_status(home: str, away: str, *,
                           market_type: Optional[str] = None,
                           cache_dir: Optional[Path] = None,
                           sport_key: Optional[str] = None,
                           now: Optional[float] = None,
                           kickoff: Any = None) -> Dict[str, Any]:
    """Stato della cache oracolo a LINEA per una partita (0 crediti).

    UNICA lettura-cache del percorso a LINEA: la usano `_oracle_fixture_books`
    (prezzabilita'), `line_oracle_reason` (diagnosi dello scarto) e, per
    estensione, `line_true_probs`/`oracle_lines`. Due letture separate
    divergerebbero su matching dei nomi, freschezza e `canonical_book`.

    FRESCHEZZA = TTL DINAMICO sul tempo al kickoff (`cache_ttl_minutes`), non
    il solo tetto fisso a 24h: un dato di tre ore nella finestra T-180 non e'
    il mercato, e usarlo produce falsi disallineamenti di linea. Il tetto
    assoluto `CACHE_MAX_AGE_H` resta come ultima rete (il TTL dinamico e'
    sempre piu' stretto).

    Ritorna SEMPRE un dict ben formato (mai None, mai eccezioni):
      `found`      la partita e' in almeno una cache `toao_*`;
      `expired`    il dato piu' fresco e' piu' vecchio del TTL applicato;
      `age_min`    eta' (minuti) della cache piu' fresca che la contiene;
      `ttl_min`    TTL applicato (dinamico);
      `kickoff`    kickoff usato per il TTL (del chiamante o `commence_time`);
      `books`      bookmaker Pinnacle della riga PIU' FRESCA della partita;
      `has_market` Pinnacle pubblica il mercato richiesto (totals/spreads).
    """
    out: Dict[str, Any] = {"found": False, "expired": False, "age_min": None,
                           "ttl_min": None, "kickoff": kickoff, "books": [],
                           "has_market": False}
    h, a = _cf(home), _cf(away)
    if not h or not a:
        return out
    folder = Path(cache_dir) if cache_dir else Path(DATA_DIR)
    # Prefisso della cache oracolo: costante GEMELLA di `odds_api.
    # ORACLE_CACHE_PREFIX` (import pigro con fallback al letterale — il
    # tripwire `test_prefisso_cache_gemello` pretende che coincidano).
    try:
        import odds_api as _oa
        prefix = getattr(_oa, "ORACLE_CACHE_PREFIX", "toao_")
    except Exception:                                            # pragma: no cover
        prefix = "toao_"
    if sport_key:
        paths = [folder / f"{prefix}{sport_key}.json"]
    else:
        try:
            paths = sorted(folder.glob(f"{prefix}*.json"),
                           key=lambda p: (p.stat().st_mtime
                                          if p.exists() else 0.0),
                           reverse=True)
        except Exception:                                        # pragma: no cover
            paths = []
    ts_now = time.time() if now is None else float(now)
    matches: List[Tuple[float, Any, List[Dict[str, Any]]]] = []
    for path in paths:
        data = _read_cache(path)
        if not isinstance(data, dict):
            continue
        age_min = (ts_now - float(data.get("ts") or 0)) / 60.0
        for match in (data or {}).get("payload") or []:
            if not _row_matches(match, h, a):
                continue
            books = [bm for bm in match.get("bookmakers") or []
                     if isinstance(bm, dict)
                     and canonical_book(bm) == PRIMARY_BOOK]
            matches.append((age_min, match.get("commence_time"), books))
    if not matches:
        return out
    # La riga PIU' FRESCA della partita: un dato vecchio in un'altra cache
    # (lega precedente, altra competizione) non deve decidere la freschezza.
    age_min, payload_kickoff, books = min(matches, key=lambda m: m[0])
    out["found"] = True
    out["age_min"] = round(age_min, 3)
    out["books"] = books
    out["kickoff"] = kickoff or payload_kickoff
    ttl = cache_ttl_minutes(minutes_to_kickoff(out["kickoff"], now=ts_now))
    ttl = min(ttl, CACHE_MAX_AGE_H * 60.0)      # tetto assoluto di sicurezza
    out["ttl_min"] = round(ttl, 3)
    out["expired"] = bool(age_min > ttl)
    mt = str(market_type or "").strip().upper()
    if mt in ("OU", "AH"):
        key = "totals" if mt == "OU" else "spreads"
        out["has_market"] = any(
            isinstance(mkt, dict) and str(mkt.get("key") or "").lower() == key
            for bm in books for mkt in (bm.get("markets") or []))
    return out


def _oracle_fixture_books(home: str, away: str, *,
                          cache_dir: Optional[Path] = None,
                          sport_key: Optional[str] = None,
                          now: Optional[float] = None,
                          kickoff: Any = None
                          ) -> Optional[List[Dict[str, Any]]]:
    """Bookmaker Pinnacle della partita dalle cache oracolo (0 crediti).

    Wrapper di `_oracle_fixture_status`: INCAPSULA la regola di freschezza,
    cosi' `line_true_probs` e `oracle_lines` non possono divergere.

    Ritorna:
      - `None` -> la partita NON e' in nessuna cache (o il dato e' SCADUTO
        per il TTL dinamico): oracolo IGNOTO, non si puo' concludere ne'
        "prezzabile" ne' "non prezzabile";
      - lista (eventualmente VUOTA) -> la partita c'e' e il dato e' fresco;
        la lista porta i bookmaker Pinnacle (vuota = Pinnacle non ha ancora
        pubblicato). La distinzione conta: un "non prezzato" su un oracolo
        NOTO e' un'informazione, su un oracolo ignoto e' rumore.
    """
    status = _oracle_fixture_status(home, away, cache_dir=cache_dir,
                                   sport_key=sport_key, now=now,
                                   kickoff=kickoff)
    if not status["found"] or status["expired"]:
        return None
    return list(status["books"])


def _complete_lines(books: Sequence[Dict[str, Any]], market_type: str,
                    home: str, away: str) -> Set[float]:
    """Linee del mercato prezzate su ENTRAMBI i lati (de-vigabile).

    Delega il matching a `totals_odds_of`/`spreads_odds_of` (nessuna copia
    della convenzione di nome/linea): una linea con un solo lato pubblicato
    non e' negoziabile e non entra.
    """
    mt = str(market_type or "").strip().upper()
    if mt not in ("OU", "AH"):
        return set()
    key = "totals" if mt == "OU" else "spreads"
    candidates: Set[float] = set()
    for bm in books:
        for mkt in bm.get("markets") or []:
            if not isinstance(mkt, dict) \
                    or str(mkt.get("key") or "").lower() != key:
                continue
            for o in mkt.get("outcomes") or []:
                if not isinstance(o, dict):
                    continue
                try:
                    if float(o.get("price")) <= 1.0:
                        continue
                except (TypeError, ValueError):
                    continue
                point = normalize_line_or_none(o.get("point"))
                if point is not None:
                    candidates.add(point)
    out: Set[float] = set()
    for bm in books:
        for point in candidates:
            quotes = (totals_odds_of(bm, point) if mt == "OU"
                      else spreads_odds_of(bm, home, away, point))
            if quotes:
                out.add(point)
    return out


def line_true_probs(home: str, away: str, *, market_type: str,
                    line: float, cache_dir: Optional[Path] = None,
                    sport_key: Optional[str] = None,
                    devig_method: Optional[str] = None,
                    now: Optional[float] = None,
                    kickoff: Any = None
                    ) -> Optional[Dict[str, Any]]:
    """p_true {"Over"/"Under" (OU) oppure "Home"/"Away" (AH)} da Pinnacle.

    De-vig a 2 esiti (`line_probabilities`) del mercato a linea della
    partita (match per sottostringa case-insensitive, entrambe le squadre
    sulla STESSA riga — come `load_oracle`). AH: la linea del payload e'
    quella del lato CASA (convenzione SX = `sx_line_of_esito`). La LINEA
    richiesta viene normalizzata (`normalize_line`) prima del matching, cosi'
    `'2.50'` e `2.5` sono la stessa linea.

    Fail-closed (None): partita non trovata, dato SCADUTO per il TTL
    dinamico (`cache_ttl_minutes` sul tempo al kickoff), Pinnacle senza
    ENTRAMBI i lati alla linea, de-vig impossibile. ZERO crediti: legge solo
    le cache gia' scaricate.
    """
    mt = str(market_type or "").strip().upper()
    if mt not in ("OU", "AH"):
        return None
    want = normalize_line_or_none(line)
    if want is None:
        return None
    books = _oracle_fixture_books(home, away, cache_dir=cache_dir,
                                  sport_key=sport_key, now=now, kickoff=kickoff)
    if not books:
        return None
    for bm in books:
        if mt == "OU":
            quotes = totals_odds_of(bm, want)
        else:
            quotes = spreads_odds_of(bm, home, away, want)
        if quotes:
            probs = line_probabilities(quotes, devig_method=devig_method)
            if probs:
                return probs
    return None


def line_oracle_reason(home: str, away: str, *, market_type: str,
                       line: Any = None, cache_dir: Optional[Path] = None,
                       sport_key: Optional[str] = None,
                       now: Optional[float] = None,
                       kickoff: Any = None) -> Dict[str, str]:
    """PERCHE' l'oracolo a LINEA non ha dato una p_true: causa machine-readable.

    Sostituisce il generico "Pinnacle assente/incompleto/stantio" con tre
    sottocause che dicono COSA FARE (05/10/2026):
      - `EXPIRED_CACHE` — il dato c'e' ma e' piu' vecchio del TTL dinamico:
        si rifetcha (follow-the-money) e il pick non e' perso per sempre;
      - `LINE_MISMATCH` — Pinnacle prezza il mercato ma NON questa linea (o
        non entrambi i lati): un altro fetch non serve, serve un'altra linea;
      - `MISSING_MARKET` — la partita non e' in nessuna cache, oppure Pinnacle
        non pubblica il mercato: non recuperabile a credito (o non ancora).

    `recoverable` (05/10/2026) dice SE una fetch della lega puo' cambiare
    l'esito: True per il dato scaduto e per la partita mai scaricata, False
    quando il mercato/la linea non e' pubblicata (pagare non servirebbe). E'
    la condizione su cui il gate decide di pagare la fetch on-demand.

    Mai eccezioni: qualunque errore ricade su `MISSING_MARKET` con il motivo
    in `detail` (fail-closed, ma dichiarato).
    """

    mt = str(market_type or "").strip().upper()
    try:
        status = _oracle_fixture_status(home, away, market_type=mt,
                                       cache_dir=cache_dir,
                                       sport_key=sport_key, now=now,
                                       kickoff=kickoff)
    except Exception as exc:                                     # pragma: no cover
        return {"code": "MISSING_MARKET", "recoverable": False,
                "detail": f"lettura cache fallita ({exc})"}
    if status["expired"]:
        ko = status.get("kickoff") or "kickoff ignoto"
        return {"code": "EXPIRED_CACHE", "recoverable": True,
                "detail": (f"dato di {status['age_min']:.0f} min > TTL "
                           f"{status['ttl_min']:.0f} min ({ko}): serve un "
                           f"refetch follow-the-money")}
    if not status["found"]:
        # La rotazione h2h puo' coprire la partita mentre l'oracolo a LINEA non
        # e' ancora stato pagato (o la sua cache e' vecchia): in quel caso il
        # rimedio e' un REFETCH follow-the-money, non un'altra linea. Il nome
        # storicamente usato dal gate per questo caso era `linea`; qui diventa
        # la sotto-causa granulare `EXPIRED_CACHE` (05/10/2026).
        try:
            covered = h2h_cache_is_stale(home, away, cache_dir=cache_dir,
                                        now=now)
        except Exception:                                    # pragma: no cover
            covered = False
        if covered:
            return {"code": "EXPIRED_CACHE", "recoverable": True,
                    "detail": "partita coperta dalla cache h2h ma oracolo a "
                              "linea non ancora pagato: serve fetch_line_odds "
                              "(follow-the-money)"}
        # Non e' in cache: una fetch della lega PUO' coprirla (l'oracolo a
        # linea si paga solo per le leghe con pick in finestra, quindi la
        # maggior parte delle partite non e' mai stata scaricata). `recoverable`
        # distingue questo caso dal mercato NON pubblicato da Pinnacle, dove
        # pagare di nuovo non cambierebbe nulla (05/10/2026).
        return {"code": "MISSING_MARKET", "recoverable": True,
                "detail": "partita assente dalle cache oracolo"}
    if not status["has_market"]:
        return {"code": "MISSING_MARKET", "recoverable": False,
                "detail": f"Pinnacle non pubblica il mercato {mt}"}
    want = normalize_line_or_none(line)
    if want is not None and want not in _complete_lines(status["books"], mt,
                                                       home, away):
        # Il mercato C'E' e la partita e' in cache: rifetchare non aggiunge la
        # linea mancante -> non recuperabile (serve un'altra linea o aspettare
        # che Pinnacle la pubblichi).
        return {"code": "LINE_MISMATCH", "recoverable": False,
                "detail": (f"linea {want:g} non prezzata su entrambi i lati "
                           f"da Pinnacle (linee disponibili: "
                           f"{sorted(_complete_lines(status['books'], mt, home, away))})")}
    return {"code": "MISSING_MARKET", "recoverable": False,
            "detail": "linea presente ma de-vig impossibile (lato incompleto)"}


def oracle_lines(home: str, away: str, *, market_type: str,
                 cache_dir: Optional[Path] = None,
                 sport_key: Optional[str] = None,
                 now: Optional[float] = None,
                 kickoff: Any = None) -> Optional[Set[float]]:
    """Linee che l'oracolo Pinnacle PREZZA per la partita (0 crediti).

    PERCHE' ESISTE (fix linee 01/10/2026). SX Bet quota MOLTE linee
    (AH +0.5/+1/+1.5..., OU 1.5/2/2.5/3/3.5...) mentre Pinnacle, via
    the-odds-api, pubblica tipicamente la sola linea MAIN: un pick su una
    linea che l'oracolo non prezza non potra' MAI diventare un ordine, perche'
    il gate top-down e' fail-closed (`linea`/`no_oracle`). Sapere IN ANTICIPO
    quali linee sono prezzabili permette di SCEGLIERE quella giusta al momento
    della selezione, invece di scoprire il disallineamento all'ordine.

    Ritorna `None` se la partita non e' in nessuna cache fresca (oracolo
    IGNOTO: nessuna conclusione possibile), altrimenti un `set` (eventualmente
    vuoto): per l'OU le linee dei `totals`, per l'AH le linee del lato CASA
    (la convenzione di `spreads_odds_of`/`sx_line_of_esito`); se il payload
    ha le squadre invertite la linea dell'altro lato viene ribaltata di segno,
    cosi' il confronto col ledger resta corretto.
    """
    mt = str(market_type or "").strip().upper()
    if mt not in ("OU", "AH"):
        return None
    books = _oracle_fixture_books(home, away, cache_dir=cache_dir,
                                 sport_key=sport_key, now=now)
    if books is None:
        return None
    h, a = _cf(home), _cf(away)
    lines: Set[float] = set()
    for bm in books:
        for mkt in bm.get("markets") or []:
            if not isinstance(mkt, dict):
                continue
            key = str(mkt.get("key") or "").lower()
            if mt == "OU" and key != "totals":
                continue
            if mt == "AH" and key != "spreads":
                continue
            for o in mkt.get("outcomes") or []:
                if not isinstance(o, dict):
                    continue
                try:
                    price = float(o.get("price"))
                except (TypeError, ValueError):
                    continue
                # Linea canonica (05/10/2026): le linee raccolte qui vengono
                # confrontate con quelle di SX Bet da `multi_market`: senza la
                # stessa forma il confronto fallisce per un falso disallineamento.
                point = normalize_line_or_none(o.get("point"))
                if point is None or price <= 1.0:
                    continue
                if mt == "OU":
                    lines.add(point)
                    continue
                name = _cf(o.get("name"))
                if h and name and (name == h or name in h or h in name):
                    lines.add(point)
                elif a and name and (name == a or name in a or a in name):
                    # Squadre invertite nel payload: la linea vista da casa
                    # e' l'opposta di quella dell'altro lato.
                    lines.add(-point)
    return lines


def line_oracle_probs(pick: Dict[str, Any],
                      cache_dir: Optional[Path] = None,
                      devig_method: Optional[str] = None,
                      now: Optional[float] = None,
                      kickoff: Any = None
                      ) -> Optional[Dict[str, Any]]:
    """p_true per un pick a linea del ledger (ponte verso il gate top-down:
    il chiamante e' il MODULO che orchestra le puntate, qui non si importa
    nulla di esecutivo — la direzione resta chiamante -> oracolo).

    Ricostruisce (market_type, linea) dall'esito di ledger con
    `multi_market.order_target` — l'UNICA fonte della convenzione (esito
    'Over 2.5' -> ("OU", 2.5, over); 'Home -0.75' -> ("AH", -0.75, home);
    mai una seconda implementazione che divergerebbe). Il ritorno porta
    anche `line_key` (lato puro) per la lettura del gate.
    """
    try:
        from multi_market import order_target
    except Exception as exc:                                     # pragma: no cover
        logger.warning("pinnacle_oracle: multi_market non disponibile (%s)", exc)
        return None
    target = order_target({"mercato": pick.get("mercato") or pick.get("market"),
                           "esito_key": pick.get("esito_key") or pick.get("esito")})
    if not target or target.get("line") is None:
        return None
    probs = line_true_probs(pick.get("home") or "", pick.get("away") or "",
                            market_type=str(target.get("market_type") or ""),
                            line=float(target["line"]),
                            cache_dir=cache_dir, devig_method=devig_method,
                            now=now, kickoff=kickoff)
    if probs:
        probs = dict(probs)
        probs["line_key"] = str(target.get("side") or "")
    return probs
