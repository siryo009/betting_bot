"""clv_tracker.py — TRACKER AUTOMATICO del CLV (Closing Line Value).

PERCHE' ESISTE (direttiva del proprietario, 13/10/2026)
-------------------------------------------------------
Il P/L di poche decine di puntate e' **rumore**: il 22/09 un `-21,64%` sul 1X2
e' stato letto come prova contro la strategia quando 74 righe su 85 venivano da
una pipeline RITIRATA, e il 25/09 un `+21,21%` sull'OU era portato per intero da
22 righe pre-19/09 (`significance.py` ha reso la cosa calcolabile: con 30
chiusure a quota media 1.65 l'edge minimo distinguibile e' ~41%).

Il **CLV** e' l'unica misura che non dipende dalla varianza dei risultati:
confronta la **quota d'acquisto effettiva di un trade** con la **closing line**
del mercato di riferimento (Pinnacle / SX Bet), cioe' il prezzo con cui gli
sharp CHIUDONO l'evento. Battere la closing line in modo sistematico e'
l'evidenza standard di edge, indipendentemente da come e' andata la partita.

Questo modulo fa UNA cosa: misurare. Connette tre pezzi che esistevano gia' e
non si parlavano:

1. **la quota d'acquisto**: `bets.price` delle puntate `mode='live'` (il prezzo
   che l'ordine ha DAVVERO ottenuto — il ledger registra i riempimenti, non le
   intenzioni);
2. **la closing line**: `clv_history.closing_odds` con la sua FONTE
   (`closing_source`: `pinnacle_t0` scritta da `closing_line.py`, oppure `sx`
   scritta qui da `capture_sx_closing`), con ripiego dichiarato su
   `pinnacle_quota` (cache) e poi `closing_quota` (miglior bookmaker);
3. **la statistica**: `significance.py` (media, intervallo di confidenza,
   p-value, edge minimo distinguibile) — nessuna formula ricopiata, una sola
   idea di "campione affidabile" (`significance.MIN_SAMPLES` = 30).

⚠️ COSA NON E' — da leggere prima di usarlo
-------------------------------------------
- **Non decide nulla**: nessun gate, nessuno stake, nessun ordine. Sola lettura
  del ledger (con `--db` la connessione e' `mode=ro`), zero crediti, zero rete
  (l'unica eccezione e' la cattura SX a T-0, che legge l'order book PUBBLICO e
  va chiesta esplicitamente o dal job).
- **Un CLV positivo non e' un profitto**: dice che il prezzo preso era meglio di
  quello con cui il mercato ha chiuso. E' la condizione necessaria per un edge
  lungo, non sufficiente (serve la disciplina di stake, che vive altrove).
- **Le chiusure di venue diverse non si confrontano**: per questo la fonte
  viaggia col campione (`by_source`) e la priorita' e' esplicita
  (`CLOSING_SOURCES`). Un valore senza fonte non e' verificabile.

Confini (tripwire in `test_clv_tracker.py`): nessun `INSERT`/`UPDATE` nel
percorso di analisi (l'unica scrittura e' `capture_sx_closing`, che passa da
`tracker.save_clv`), nessun ordine, nessuna soglia di strategia, import
leggero (`import clv_tracker` non carica `auto_bet`/`bot`).

CLI:
    venv/bin/python clv_tracker.py [--json] [--days N] [--include-signals]
                                   [--db PATH] [--capture-sx]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("clv_tracker")

try:                                        # pragma: no cover - dipendenza soft
    import significance as _sig
except Exception:                           # pragma: no cover
    _sig = None

#: Sorgenti della closing line, in ordine di PRIORITA': la prima colonna
#: valorizzata vince e la sua etichetta viaggia col campione. Le prime due
#: sono prezzi SHARP (Pinnacle a T-0 e Pinnacle dalla cache), la terza e' la
#: chiusura del miglior bookmaker (`closing_quota`, convenzione storica di
#: `save_clv`): tenerle separate e' cio' che rende il numero verificabile.
CLOSING_SOURCES: Tuple[Tuple[str, str], ...] = (
    ("closing_odds", "pinnacle_t0"),
    ("pinnacle_quota", "pinnacle_cache"),
    ("closing_quota", "bookmaker"),
)

#: Fonte scritta da `capture_sx_closing` (l'altro lato della closing line).
SOURCE_SX = "sx"

#: SECONDO BENCHMARK (15/10/2026): l'etichetta della chiusura dell'exchange
#: (`clv_history.betfair_odds`, scritta da `closing_line`). Non entra in
#: `CLOSING_SOURCES` — non e' un'alternativa alla chiusura primaria, e' una
#: misura INDIPENDENTE della stessa cosa: un edge che batte due sharp di
#: meccanismo diverso e' molto meno spiegabile con l'errore di una sola fonte.
#: L'etichetta e' la stessa costante di `closing_line.SOURCE_BENCHMARK`:
#: duplicata qui di proposito (come gli stati del ledger in `tracker`), con un
#: tripwire che confronta le DUE tabelle di stringhe cosi' non possono divergere
#: in silenzio.
SOURCE_BENCHMARK: str = "betfair_t0"

#: Sotto questo numero di campioni non si conclude nulla: STESSA soglia di
#: `multi_market.MIN_RELIABLE_CLOSED` e di `significance.MIN_SAMPLES`.
MIN_SAMPLES: int = int(getattr(_sig, "MIN_SAMPLES", 30) or 30)

STATUS_UNAVAILABLE = "unavailable"
STATUS_INSUFFICIENT = "insufficient"
STATUS_BEATING = "beating"
STATUS_LAGGING = "lagging"
STATUS_FLAT = "flat"

STATUS_LABEL = {
    STATUS_UNAVAILABLE: "non calcolabile",
    STATUS_INSUFFICIENT: "campione insufficiente",
    STATUS_BEATING: "BATTE il mercato (significativo)",
    STATUS_LAGGING: "PERDE sul mercato (significativo)",
    STATUS_FLAT: "non distinguibile da zero",
}

#: Provenienza dell'osservazione: un ORDINE eseguito (la direttiva) oppure un
#: segnale mai giocato (`--include-signals`, misura del MODELLO, non degli
#: ordini). Le due popolazioni non si sommano mai in un numero solo.
ORIGIN_TRADE = "trade"
ORIGIN_SIGNAL = "signal"

#: Motivi machine-readable (mai prosa): un'osservazione che non si misura viene
#: CONTATA, non fatta sparire. `bad_*`/`outcome_uncanonical`/`not_live` sono
#: RIFIUTI (`excluded`); `no_closing` ed `echo_signal` sono ATTESE
#: (`pending`): la chiusura arrivera' ai prossimi T-0 e il campione crescera'.
EXCLUDE_REASONS = (
    "bad_price", "no_closing", "echo_signal", "bad_closing",
    "outcome_uncanonical", "not_live",
)

#: Motivi che finiscono in `pending` (attese) invece che in `excluded`.
PENDING_REASONS = ("no_closing", "echo_signal")


# ---------------------------------------------------------------------------
# Configurazione (env lette a RUNTIME: nessun redeploy per tarare)
# ---------------------------------------------------------------------------

def _num_env(name: str, default: float, *, minimum: float = 0.0) -> float:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return float(default)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning("clv_tracker: %s='%s' non numerico, uso %.2f",
                       name, raw, default)
        return float(default)
    if value < minimum:
        logger.warning("clv_tracker: %s=%s sotto il minimo, uso %.2f",
                       name, value, default)
        return float(default)
    return value


def window_minutes() -> Tuple[float, float]:
    """Finestra T-0 (minuti prima/dopo il kickoff) per la cattura SX.

    DELEGA a `closing_line` (l'altra meta' della stessa misura): due routine
    che catturano "il prezzo di chiusura" devono usare la STESSA finestra,
    altrimenti il campione Pinnacle e quello SX non sono confrontabili. Le env
    dedicate (`CLV_SX_PRE_MIN`/`CLV_SX_POST_MIN`) permettono di stringerla, non
    di allargarla oltre il default dell'altra routine.
    """
    pre, post = 10.0, 5.0
    try:
        import closing_line
        pre, post = closing_line.pre_minutes(), closing_line.post_minutes()
    except Exception as exc:                                  # pragma: no cover
        logger.debug("clv_tracker: finestra di default (%s)", exc)
    return (_num_env("CLV_SX_PRE_MIN", pre, minimum=0.0),
            _num_env("CLV_SX_POST_MIN", post, minimum=0.0))


def _parse_ts(value: Any) -> Optional[datetime]:
    """Timestamp ISO del ledger -> datetime UTC aware (None se illeggibile).

    Le date del ledger sono ISO con la 'T' e a volte 'Z': il confronto si fa in
    PYTHON, mai in SQL (lezione del 17/09 sui formati di data).
    """
    if isinstance(value, datetime):
        ts = value
    else:
        try:
            ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError, AttributeError):
            return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _as_float(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out or out in (float("inf"), float("-inf")):
        return None
    return out


# ---------------------------------------------------------------------------
# Classificazione (deterministica: etichette esplicite, mai fuzzy)
# ---------------------------------------------------------------------------

#: Etichette di lega che identificano il football americano (sono le stesse che
#: `gridiron_lane` scrive sul ledger: NFL / NCAAF / CFL).
_FOOTBALL_USA_LABELS = ("NFL", "NCAA", "CFL", "FOOTBALL USA", "GRIDIRON")


def sport_of(mercato: Any, league: Any = "") -> str:
    """Sport di un campione: dal MERCATO quando basta, altrimenti dalla lega.

    Deterministica e dichiarata: `TENNIS` -> tennis, `ML` -> esports, e per il
    calcio (`1X2`/`AH`/`OU`) la lega decide fra football e football USA. Non e'
    un indovinello: sono le etichette che i moduli scrivono davvero.
    """
    m = str(mercato or "").strip().upper()
    if m == "TENNIS":
        return "tennis"
    if m == "ML":
        return "esports"
    lg = str(league or "").upper()
    if any(k in lg for k in _FOOTBALL_USA_LABELS):
        return "football_usa"
    return "football"


def expected_outcomes(market: Any) -> Optional[Tuple[str, ...]]:
    """Esiti attesi di un mercato testa-a-testa (per il CLV vig-free).

    DELEGA a `closing_line._outcomes_for` (una sola definizione): i mercati a
    LINEA (AH/OU) non hanno una closing nel h2h, quindi il vig-free non si
    calcola e il campione lo dichiara invece di inventare una devig.
    """
    try:
        import closing_line
        return closing_line._outcomes_for(market)          # noqa: SLF001
    except Exception:                                          # pragma: no cover
        m = str(market or "").strip().upper()
        if m in ("1X2", "1X2_OT"):
            return ("1", "X", "2")
        if m in ("TENNIS", "ML", "ML_OT"):
            return ("1", "2")
        return None


def _canonical(esito: Any, home: Any, away: Any) -> Optional[str]:
    """Esito canonico ('1'/'X'/'2') con la stessa semantica del ledger.

    Il ledger NON e' omogeneo (lezione del 15/09): `sx_signals` scrive
    `1`/`X`/`2`, `fixture_engine` scrive il NOME della squadra. Si delega a
    `decision.adapters.canonical_outcome` con import pigro e fallback locale
    minimale — mai un indovinello (nome che combacia con entrambe o nessuna
    squadra -> None).
    """
    raw = str(esito or "").strip()
    if raw in ("1", "X", "2"):
        return raw
    try:
        from decision.adapters import canonical_outcome
        got = canonical_outcome(raw, home or "", away or "")
        if got in ("1", "X", "2"):
            return got
        return None
    except Exception as exc:
        logger.debug("clv_tracker: canonical_outcome non disponibile (%s)", exc)
    low = raw.lower()
    if low in ("x", "draw", "pareggio"):
        return "X"
    return None


# ---------------------------------------------------------------------------
# 1. RACCOLTA: quota d'acquisto vs closing line, direttamente dal ledger
# ---------------------------------------------------------------------------

def _connect(db: Optional[str] = None) -> Any:
    """Connessione al ledger. Con `db` esplicito e' in SOLA LETTURA."""
    if db:
        import sqlite3
        return sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    from tracker import _get_conn
    return _get_conn()


def _rows(conn, sql: str, params: Sequence[Any] = (),
          *, errors: Optional[List[str]] = None) -> List[Any]:
    try:
        cur = conn.cursor()
        cur.execute(sql, tuple(params))
        return list(cur.fetchall())
    except Exception as exc:
        # Fail-safe dichiarato: un DB senza la tabella/colonna risponde "vuoto",
        # ma il motivo (dal 16/10/2026) viene anche CONTATO in `errors`, cosi'
        # il report puo' dire "query non eseguibile" invece di "zero campioni".
        # Non e' cosmesi: la prima misura sul ledger di produzione ha letto
        # "0 trade live" a fronte di 67 puntate a ledger, perche' una colonna
        # mancava — un guasto che si legge come un fatto manda a cercare il
        # problema nella direzione sbagliata (stessa lezione del 20/10 e del
        # 30/09: "0" e "illeggibile" sono diagnosi diverse).
        logger.debug("clv_tracker: query non eseguibile (%s)", exc)
        if errors is not None:
            errors.append(f"{type(exc).__name__}: {exc}")
        return []


def _table_columns(conn, table: str) -> set:
    """Colonne REALI di una tabella: set VUOTO se la tabella non e' leggibile.

    Serve a costruire le query sull'INTERSEZIONE delle colonne esistenti: su un
    DB di un deploy precedente (migrazione non ancora applicata) una SELECT che
    nomina una colonna assente fallisce in blocco, e il fail-safe di `_rows` la
    trasformerebbe in "nessun dato".
    """
    return {str(r[1]) for r in _rows(conn, f"PRAGMA table_info({table})")}


def _col_or_null(columns: set, name: str, alias: str) -> str:
    """`NULLIF(<alias>.<col>,'')` se la colonna esiste, altrimenti `NULL`.

    Il `COALESCE` che la usa ripiega sulla riga `matches`: e' la degradazione
    dichiarata (copertura ridotta, mai un errore).
    """
    return f"NULLIF({alias}.{name},'')" if name in columns else "NULL"


# Identita' dell'evento: PRIMA dalla puntata (15/10/2026: la riga del denaro e'
# autosufficiente), con ripiego sulla partita per le righe storiche. E' il fix
# che porta la copertura della closing line dai due terzi al totale: senza, una
# bet di una settimana prima non aveva piu' i nomi squadra (`clear_old_matches`
# pota `matches` dopo due giorni) e il CLV non era piu' calcolabile.
#: Colonne di `bets` che tengono l'IDENTITA' DELL'EVENTO sulla riga del denaro
#: (15/10/2026). Se mancano (DB di un deploy precedente) le query leggono da
#: `matches` come prima e la degradazione viene DICHIARATA nel report.
_BET_EVENT_COLS: Tuple[str, ...] = ("home", "away", "league", "commence")

#: Colonne di `clv_history` non di chiave: ognuna puo' mancare su un ledger
#: precedente alla migrazione e la query degrada a `NULL` invece di fallire
#: (`betfair_odds`, il secondo benchmark, e' l'ultima arrivata).
_CLV_OPTIONAL_COLS: Tuple[str, ...] = (
    "signal_quota", "closing_quota", "pinnacle_quota", "closing_odds",
    "closing_source", "updated_at", "betfair_odds",
)


def _bet_sql(columns: set) -> str:
    """SELECT dei trade eseguiti, costruita sulle colonne che ESISTONO."""
    league = _col_or_null(columns, "league", "b")
    home = _col_or_null(columns, "home", "b")
    away = _col_or_null(columns, "away", "b")
    commence = _col_or_null(columns, "commence", "b")
    return (
        "SELECT b.match_id, b.mercato, b.esito, b.price, b.stake, b.mode, "
        "       b.created_at, b.settled_at, b.esito_finale, b.market_id, "
        "       b.selection_id, "
        f"       COALESCE({league}, m.league) AS league, "
        f"       COALESCE({home}, m.home_team) AS home, "
        f"       COALESCE({away}, m.away_team) AS away, "
        f"       COALESCE({commence}, m.commence_time) AS commence "
        "FROM bets b LEFT JOIN matches m ON m.id = b.match_id "
        "WHERE b.mode = 'live' AND b.price IS NOT NULL"
    )


def _open_bets_sql(columns: set) -> str:
    """Come `_bet_sql`, per le puntate ancora APERTE (cattura a T-0)."""
    home = _col_or_null(columns, "home", "b")
    away = _col_or_null(columns, "away", "b")
    commence = _col_or_null(columns, "commence", "b")
    return (
        "SELECT b.match_id, b.mercato, b.esito, b.price, b.market_id, "
        "       b.selection_id, "
        f"       COALESCE({home}, m.home_team) AS home, "
        f"       COALESCE({away}, m.away_team) AS away, "
        f"       COALESCE({commence}, m.commence_time) AS commence "
        "FROM bets b LEFT JOIN matches m ON m.id = b.match_id "
        "WHERE b.mode = 'live' AND b.esito_finale IS NULL "
        "  AND b.market_id IS NOT NULL AND b.market_id != ''"
    )


def _clv_sql(columns: set) -> str:
    """SELECT della chiusura di un match, con le colonne che ESISTONO."""
    names = [c for c in _CLV_OPTIONAL_COLS if c in columns]
    return "SELECT esito, " + ", ".join(names) + \
        " FROM clv_history WHERE match_id = ?"


_PRED_SQL = (
    "SELECT p.match_id, p.mercato, p.esito, p.quota, p.status, p.prob, "
    "       p.created_at, p.settled_at, p.esito_finale, "
    "       m.league, m.home_team, m.away_team, m.commence_time "
    "FROM predictions p LEFT JOIN matches m ON m.id = p.match_id "
    "WHERE p.quota IS NOT NULL AND p.status IN "
    "('value', 'strong_value', 'moderate')"
)

def _clv_index(conn, match_id: str, home: Any = "", away: Any = ""
               ) -> Dict[str, Dict[str, Any]]:
    """Righe di `clv_history` del match indicizzate per ESITO CANONICO.

    L'indicizzazione tollerante e' necessaria perche' lo stesso match puo'
    avere l'esito scritto come nome squadra da un percorso (`fixture_engine`) e
    come `1`/`2` dall'altro (`sx_signals`): senza, il campione risulterebbe
    "senza chiusura" pur avendo la chiusura a ledger.

    ⚠️ `home`/`away` NON sono decorativi: senza i nomi delle squadre un esito
    come `Osasuna` non e' canonizzabile (`canonical_outcome` non ha nulla con
    cui confrontarlo) e l'indicizzazione tollerante non aggancia nulla — la
    chiave resta il nome grezzo. E' il parametro che rende vera la tolleranza
    dichiarata qui sopra (bug trovato dai test del 13/10).
    """
    out: Dict[str, Dict[str, Any]] = {}
    # Le colonne si leggono dal DB: su un ledger precedente alla migrazione
    # `betfair_odds` non esiste e la SELECT nominale farebbe fallire TUTTO
    # (una chiusura assente si leggerebbe come "nessuna chiusura").
    columns = _table_columns(conn, "clv_history")
    names = [c for c in _CLV_OPTIONAL_COLS if c in columns]
    for row in _rows(conn, _clv_sql(columns), (match_id,)):
        values = dict(zip(names, list(row)[1:]))
        rec = {"esito": row[0],
               "signal_quota": values.get("signal_quota"),
               "closing_quota": values.get("closing_quota"),
               "pinnacle_quota": values.get("pinnacle_quota"),
               "closing_odds": values.get("closing_odds"),
               "closing_source": values.get("closing_source"),
               "updated_at": values.get("updated_at"),
               "betfair_odds": values.get("betfair_odds"),
               "home": home or "", "away": away or ""}
        # La chiave GREZZA c'e' sempre (una riga resta raggiungibile anche se
        # l'esito non e' canonizzabile); quella canonica si AGGIUNGE.
        out[str(row[0]).strip()] = rec
        key = _canonical(row[0], home, away)
        if key is not None:
            out[key] = rec
    return out


def _closing_of(rec: Optional[Dict[str, Any]], market: Any
                ) -> Tuple[Optional[float], Optional[str]]:
    """(valore di chiusura, fonte) secondo `CLOSING_SOURCES`. Mai un valore
    senza fonte: una chiusura non etichettata non si confronta."""
    if not rec:
        return None, None
    for column, source in CLOSING_SOURCES:
        value = _as_float(rec.get(column))
        if value is not None and value > 1.0:
            declared = (str(rec.get("closing_source") or "").strip()
                        if column == "closing_odds" else "")
            return value, (declared or source)
    return None, None


def _is_echo(rec: Dict[str, Any], value: float) -> bool:
    """La "chiusura" e' l'ECO della quota del segnale (nessun secondo campione)?

    `save_clv` semina `closing_quota = quota` alla prima analisi: quella riga
    non e' una chiusura, e' il prezzo di partenza. Contarla fra i campioni
    produrrebbe un CLV finto a 0 (la stessa trappola documentata nel report
    del 02/09 su `_clv_stats`).
    """
    sig = _as_float(rec.get("signal_quota"))
    return sig is not None and abs(sig - value) < 1e-9


def collect(*, conn=None, db: Optional[str] = None, days: Optional[float] = None,
            include_signals: bool = False, now: Optional[datetime] = None
            ) -> Dict[str, Any]:
    """Raccoglie i campioni: {samples, excluded, pending, totals}. Mai solleva.

    `samples` = osservazioni MISURABILI (quota d'acquisto + chiusura con fonte);
    `excluded` = osservazioni contate per motivo (un campione non misurabile non
    sparisce); `pending` = trade aperti la cui chiusura arrivera' a T-0
    (il campione crescera': e' la parte onesta del report).
    """
    bundle: Dict[str, Any] = {"samples": [], "excluded": {},
                              "pending": [], "totals": {"trades": 0,
                                                        "signals": 0,
                                                        "scanned": 0},
                              "schema": {}, "degraded": [], "query_errors": [],
                              "error": None}
    errs: List[str] = []
    close_conn = False
    try:
        if conn is None:
            conn = _connect(db)
            close_conn = True
        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(days=days) if days else None
        bet_cols = _table_columns(conn, "bets")
        clv_cols = _table_columns(conn, "clv_history")
        bundle["schema"] = {
            "bets_event_fields": [c for c in _BET_EVENT_COLS if c in bet_cols],
            "clv_columns": sorted(clv_cols),
        }
        missing_event = [c for c in _BET_EVENT_COLS if c not in bet_cols]
        if missing_event:
            # Le query funzionano lo stesso (leggono da `matches`), ma la
            # copertura dipende da una tabella che viene POTATA: chi legge il
            # report deve saperlo prima di concludere "il CLV non e'
            # calcolabile".
            bundle["degraded"].append(
                "identita' dell'evento NON sulla puntata (mancano " +
                ", ".join(missing_event) + " su `bets`): la copertura della "
                "closing line dipende da `matches`, che viene potata dopo 2 "
                "giorni (la migrazione si applica al primo avvio col codice "
                "nuovo)")
        if "betfair_odds" not in clv_cols:
            bundle["degraded"].append(
                "secondo benchmark non disponibile su questo ledger: la "
                "colonna `clv_history.betfair_odds` non esiste ancora")

        # --- 1a. I TRADE eseguiti (la direttiva: "quota d'acquisto effettiva")
        for row in _rows(conn, _bet_sql(bet_cols), errors=errs):
            (match_id, mercato, esito, price, _stake, mode, created_at,
             _settled_at, esito_finale, _market_id, _sel, league, home, away,
             _commence) = row
            created = _parse_ts(created_at)
            if cutoff is not None and created is not None and created < cutoff:
                continue
            bundle["totals"]["scanned"] += 1
            if str(mode or "").strip().lower() != "live":
                _bump(bundle["excluded"], "not_live")
                continue
            bundle["totals"]["trades"] += 1
            sample = _build_sample(
                conn, match_id=match_id, mercato=mercato, esito=esito,
                price=_as_float(price), league=league, home=home, away=away,
                origin=ORIGIN_TRADE, closed=esito_finale is not None)
            if sample.get("ok"):
                bundle["samples"].append(sample["sample"])
            elif sample.get("pending"):
                bundle["pending"].append(sample["pending"])
            else:
                _bump(bundle["excluded"], sample.get("reason") or "no_closing")

        # --- 1b. I SEGNALI mai giocati (opzionale: misura del MODELLO)
        if include_signals:
            seen = {(s["match_id"], s["esito"]) for s in bundle["samples"]}
            for row in _rows(conn, _PRED_SQL, errors=errs):
                (match_id, mercato, esito, quota, _status, _prob, created_at,
                 _settled_at, esito_finale, league, home, away,
                 _commence) = row
                created = _parse_ts(created_at)
                if cutoff is not None and created is not None and created < cutoff:
                    continue
                bundle["totals"]["scanned"] += 1
                bundle["totals"]["signals"] += 1
                sample = _build_sample(
                    conn, match_id=match_id, mercato=mercato, esito=esito,
                    price=_as_float(quota), league=league, home=home, away=away,
                    origin=ORIGIN_SIGNAL, closed=esito_finale is not None)
                if sample.get("ok"):
                    key = (sample["sample"]["match_id"], sample["sample"]["esito"])
                    if key in seen:
                        continue            # stesso esito gia' misurato sul trade
                    seen.add(key)
                    bundle["samples"].append(sample["sample"])
                elif sample.get("pending"):
                    bundle["pending"].append(sample["pending"])
                else:
                    _bump(bundle["excluded"], sample.get("reason") or "no_closing")
    except Exception as exc:                                  # fail-safe totale
        logger.debug("clv_tracker: raccolta non completata (%s)", exc)
        bundle["error"] = str(exc)
    finally:
        bundle["query_errors"] = errs
        if close_conn:
            try:
                conn.close()
            except Exception:
                pass
    return bundle


def _bump(counter: Dict[str, int], key: str) -> None:
    counter[key] = counter.get(key, 0) + 1


def _build_sample(conn, *, match_id: Any, mercato: Any, esito: Any,
                  price: Optional[float], league: Any, home: Any, away: Any,
                  origin: str, closed: bool) -> Dict[str, Any]:
    """Un campione (o il motivo per cui non lo e'). Mai un'eccezione."""
    try:
        if price is None or price <= 1.0:
            return {"ok": False, "reason": "bad_price"}
        key = _canonical(esito, home, away)
        if key is None:
            return {"ok": False, "reason": "outcome_uncanonical"}
        index = _clv_index(conn, str(match_id), home, away)
        rec = index.get(key) or index.get(str(esito).strip())
        closing, source = _closing_of(rec, mercato)
        if closing is None:
            return {"ok": False, "pending": {
                "match_id": str(match_id), "mercato": mercato, "esito": key,
                "price": round(price, 6), "origin": origin,
                "closed": bool(closed), "reason": "no_closing"}}
        if rec and source == "bookmaker" and _is_echo(rec, closing):
            # ECO: l'unica "chiusura" a ledger e' il seme del segnale. NON e' un
            # campione (darebbe un CLV finto a zero), ma NON e' nemmeno un
            # rifiuto: la riga si aggiorna a ogni analisi e diventera'
            # misurabile a T-0. Va quindi in `pending` ("non misurabile
            # ANCORA"), come un trade senza alcuna riga: due situazioni
            # strutturalmente identiche non possono finire in bucket diversi.
            return {"ok": False, "pending": {
                "match_id": str(match_id), "mercato": mercato, "esito": key,
                "price": round(price, 6), "origin": origin,
                "closed": bool(closed), "reason": "echo_signal"}}
        clv = _clv_raw(price, closing)
        if clv is None:
            return {"ok": False, "reason": "bad_closing"}
        # SECONDO BENCHMARK (15/10/2026): la stessa quota d'acquisto contro la
        # chiusura dell'EXCHANGE (Betfair EX EU), quando catturata. Non e' un
        # requisito del campione: senza il benchmark il trade resta misurabile
        # sulla fonte primaria (`clv` invariato) e il confronto lo dichiara.
        bench_price = _as_float(rec.get("betfair_odds")) if rec else None
        bench_price = bench_price if (bench_price and bench_price > 1.0) else None
        sample = {
            "match_id": str(match_id), "mercato": str(mercato or ""),
            "esito": key, "price": round(price, 6),
            "closing": round(closing, 6), "closing_source": source,
            "clv": clv, "clv_vig_free": None,
            "benchmark": (round(bench_price, 6) if bench_price else None),
            "clv_benchmark": (_clv_raw(price, bench_price)
                              if bench_price else None),
            "benchmark_source": SOURCE_BENCHMARK if bench_price else None,
            "sport": sport_of(mercato, league), "league": str(league or ""),
            "home": str(home or ""), "away": str(away or ""),
            "origin": origin, "closed": bool(closed),
            "echo": False,
        }
        # CLV vig-free: si deviga la chiusura con TUTTI gli esiti del mercato,
        # presi dalla STESSA fonte preferita (mai un mix di venue). Se un esito
        # manca il valore resta None e il campione lo dichiara.
        outcomes = expected_outcomes(mercato)
        if outcomes and all(index.get(o) and
                            _closing_of(index.get(o), mercato)[0]
                            for o in outcomes):
            same = {_closing_of(index.get(o), mercato)[1] for o in outcomes}
            if len(same) == 1:
                others = [_closing_of(index.get(o), mercato)[0]
                          for o in outcomes]
                sample["clv_vig_free"] = _clv_vig_free(price, closing, others)
        return {"ok": True, "sample": sample}
    except Exception as exc:                                  # pragma: no cover
        logger.debug("clv_tracker: campione non costruito (%s)", exc)
        return {"ok": False, "reason": "bad_closing"}


def _clv_raw(price: float, closing: float) -> Optional[float]:
    """CLV = quota d'acquisto / chiusura - 1 (formula UNICA del progetto)."""
    try:
        from market_calib import clv_raw
        return clv_raw(price, closing)
    except Exception as exc:                                  # pragma: no cover
        logger.debug("clv_tracker: clv_raw non disponibile (%s)", exc)
        return None


def _clv_vig_free(price: float, closing: float,
                  all_closings: Sequence[float]) -> Optional[float]:
    """CLV vig-free: la chiusura viene DEVIGATA sugli esiti del mercato.

    Delega a `market_calib.clv_vig_free` (metodo `power`, il default del
    progetto): una formula sola, confrontabile col report giornaliero.
    """
    try:
        from market_calib import clv_vig_free
        return clv_vig_free(price, closing, list(all_closings), method="power")
    except Exception as exc:                                  # pragma: no cover
        logger.debug("clv_tracker: clv_vig_free non disponibile (%s)", exc)
        return None


# ---------------------------------------------------------------------------
# 2. METRICHE: media, intervallo, p-value, edge minimo distinguibile
# ---------------------------------------------------------------------------

def _bucket(samples: Sequence[Dict[str, Any]], field: str
            ) -> Dict[str, Dict[str, Any]]:
    """Aggrega per campo (sport/mercato/fonte): n, CLV medio, beat positivi.

    Fail-safe su righe ostili (`None`, stringhe, dict senza `clv`): una
    telemetria sporca non deve far esplodere il report. Si usa `_as_float`, la
    stessa conversione di `summarize`, cosi' i due conteggi coincidono.
    """
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for s in samples or []:
        if not isinstance(s, dict):
            continue
        groups.setdefault(str(s.get(field) or "?"), []).append(s)
    out: Dict[str, Dict[str, Any]] = {}
    for key, items in sorted(groups.items()):
        vals = [_as_float(i.get("clv")) for i in items]
        vals = [v for v in vals if v is not None]
        k = sum(1 for v in vals if v > 0)
        out[key] = {
            "n": len(vals),
            "avg_clv": round(sum(vals) / len(vals), 6) if vals else None,
            "median_clv": round(median(vals), 6) if vals else None,
            "share_positive": round(k / len(vals), 6) if vals else None,
        }
    return out


def summarize(samples: Sequence[Dict[str, Any]], *, alpha: float = 0.05
              ) -> Dict[str, Any]:
    """Il blocco metriche del campione. Fail-safe: mai un'eccezione.

    Il CLV medio e' un valore "per unita'" (come il ROI del report), quindi la
    statistica e' quella di `significance.roi_test`: t-test sulla media con
    intervallo di confidenza — nessuna formula reinventata.
    """
    vals: List[float] = []
    vf: List[float] = []
    bench: List[float] = []
    both = 0
    for s in samples or []:
        if not isinstance(s, dict):
            continue                       # riga ostile: ignorata, mai un crash
        v = _as_float(s.get("clv"))
        if v is not None:
            vals.append(v)
        w = _as_float(s.get("clv_vig_free"))
        if w is not None:
            vf.append(w)
        b = _as_float(s.get("clv_benchmark"))
        if b is not None:
            bench.append(b)
            if v is not None and v > 0 and b > 0:
                both += 1
    block: Dict[str, Any] = {
        "n": len(vals), "min_samples": MIN_SAMPLES,
        "alpha": alpha, "scipy": bool(_sig and _sig.available()),
        "avg_clv": round(sum(vals) / len(vals), 6) if vals else None,
        "median_clv": round(median(vals), 6) if vals else None,
        "sd": None, "ci95": None, "p_value": None, "t": None,
        "n_positive": sum(1 for v in vals if v > 0),
        "share_positive": (round(sum(1 for v in vals if v > 0) / len(vals), 6)
                           if vals else None),
        "share_positive_ci95": None,
        "avg_clv_vig_free": round(sum(vf) / len(vf), 6) if vf else None,
        "n_vig_free": len(vf),
        "reliable": False, "detectable_clv": None,
        "status": STATUS_UNAVAILABLE, "note": "",
        "by_source": _bucket(samples, "closing_source"),
        "by_sport": _bucket(samples, "sport"),
        "by_market": _bucket(samples, "mercato"),
        # SECONDO BENCHMARK (15/10/2026). `n` e' il numero di campioni per cui
        # esiste ANCHE la chiusura dell'exchange: se e' piccolo la conferma
        # indipendente non e' disponibile, e il report lo dice invece di
        # spacciare la fonte primaria per un doppio controllo.
        "benchmark": {
            "source": SOURCE_BENCHMARK,
            "n": len(bench),
            "avg_clv": (round(sum(bench) / len(bench), 6) if bench else None),
            "median_clv": (round(median(bench), 6) if bench else None),
            "share_positive": (round(sum(1 for x in bench if x > 0) / len(bench), 6)
                               if bench else None),
            "confirmed_both": both,
            "confirmed_share": (round(both / len(bench), 6) if bench else None),
        },
    }
    if not vals:
        block["note"] = ("nessun campione misurabile: servono trade eseguiti e "
                         "una closing line catturata")
        return block
    if not (block["scipy"] and _sig):
        block["note"] = "significativita' non calcolabile (scipy assente)"
        return block
    test = _sig.roi_test(vals, alpha=alpha)
    if test:
        block["sd"] = test.get("sd")
        block["t"] = test.get("t")
        block["p_value"] = test.get("p_value")
        if test.get("ci_low") is not None:
            block["ci95"] = [test["ci_low"], test["ci_high"]]
        block["degenerate"] = bool(test.get("degenerate"))
    ci = _sig.wilson_interval(block["n_positive"], len(vals), alpha=alpha)
    if ci:
        block["share_positive_ci95"] = [round(ci[0], 6), round(ci[1], 6)]
    # Verdetto: PRIMA il campione, poi il segno e la sua significativita'
    # (stessa disciplina di `significance.py`).
    block["reliable"] = len(vals) >= MIN_SAMPLES
    if not block["reliable"]:
        block["status"] = STATUS_INSUFFICIENT
    elif block["p_value"] is not None and block["p_value"] < alpha:
        block["status"] = (STATUS_BEATING if (block["avg_clv"] or 0) > 0
                           else STATUS_LAGGING)
    else:
        block["status"] = STATUS_FLAT
    # Con QUESTO campione, quale CLV e' distinguibile da zero? E' il numero che
    # rende onesta l'etichetta "batte il mercato".
    if block.get("sd"):
        block["detectable_clv"] = _sig.detectable_edge(
            len(vals), sd=block["sd"], alpha=alpha)
    block["note"] = _note(block)
    return block


def _note(block: Dict[str, Any]) -> str:
    """La frase da leggere accanto al CLV medio (l'etichetta puo' mentire)."""
    n = block.get("n") or 0
    if n < MIN_SAMPLES:
        return (f"campione insufficiente ({n}/{MIN_SAMPLES}): un CLV medio "
                "dentro l'intervallo non e' distinguibile da zero")
    if block.get("status") == STATUS_FLAT:
        return "CLV medio non distinguibile da zero sull'intervallo di confidenza"
    return ""


# ---------------------------------------------------------------------------
# 3. REPORT
# ---------------------------------------------------------------------------

def _shared(block: Dict[str, Any], key: str, label: str) -> str:
    items = block.get(key) or {}
    if not items:
        return ""
    parts = []
    for name, b in items.items():
        avg = b.get("avg_clv")
        parts.append(f"{label}{name} n={b.get('n')} "
                     f"{(avg * 100.0):+.2f}%" if avg is not None
                     else f"{label}{name} n={b.get('n')} —")
    return " | ".join(parts)


def report(*, conn=None, db: Optional[str] = None, days: Optional[float] = None,
           include_signals: bool = False,
           now: Optional[datetime] = None) -> Dict[str, Any]:
    """Il report completo: campione, metriche, motivi degli scarti, attese."""
    bundle = collect(conn=conn, db=db, days=days,
                     include_signals=include_signals, now=now)
    metrics = summarize(bundle.get("samples") or [])
    return {
        "totals": bundle.get("totals") or {},
        "excluded": bundle.get("excluded") or {},
        "pending": len(bundle.get("pending") or []),
        "pending_closed": sum(1 for p in (bundle.get("pending") or [])
                              if p.get("closed")),
        "samples": bundle.get("samples") or [],
        "metrics": metrics,
        "include_signals": bool(include_signals),
        "schema": bundle.get("schema") or {},
        "degraded": bundle.get("degraded") or [],
        "query_errors": bundle.get("query_errors") or [],
        "error": bundle.get("error"),
    }


def format_report(rep: Optional[Dict[str, Any]] = None, *,
                  limit: int = 10) -> str:
    """Report leggibile (Telegram/CLI). Non decide: descrive."""
    rep = rep if isinstance(rep, dict) else report()
    m = rep.get("metrics") or {}
    tot = rep.get("totals") or {}
    n = m.get("n") or 0
    lines = ["📈 *CLV TRACKER* — quota d'acquisto vs closing line"]
    lines.append(f"Campione: *{n}* osservazioni misurate su "
                 f"{tot.get('trades', 0)} trade live"
                 + (f" (+{tot.get('signals', 0)} segnali)" if rep.get("include_signals")
                    else ""))
    # Una query fallita e una degradazione di schema NON si leggono come "zero
    # campioni": sono due diagnosi diverse e vanno dette PRIMA del verdetto.
    if rep.get("query_errors"):
        lines.append("⚠️ query non eseguibile: " +
                     "; ".join(str(e) for e in rep["query_errors"][:3]))
    for reason in rep.get("degraded") or []:
        lines.append(f"⚠️ {reason}")
    if m.get("avg_clv") is not None:
        avg = m["avg_clv"] * 100.0
        ci = m.get("ci95")
        ci_txt = (f" (CI95 {ci[0] * 100.0:+.2f}% .. {ci[1] * 100.0:+.2f}%)"
                  if ci else " (CI95 non calcolabile)")
        lines.append(f"CLV medio: *{avg:+.2f}%*{ci_txt}"
                     + (f" | p={m['p_value']}" if m.get("p_value") is not None
                        else ""))
        lines.append(f"Mediana: {m['median_clv'] * 100.0:+.2f}% | "
                     f"beat positivi: {m['n_positive']}/{n} "
                     f"({(m['share_positive'] or 0) * 100.0:.1f}%)")
        if m.get("n_vig_free"):
            lines.append(f"Vig-free (chiusura devigata): "
                         f"*{m['avg_clv_vig_free'] * 100.0:+.2f}%* "
                         f"su {m['n_vig_free']} campioni")
    else:
        lines.append("CLV medio: — (nessun campione misurabile)")
    src = _shared(m, "by_source", "fonte ")
    if src:
        lines.append(f"Per fonte: {src}")
    bm = m.get("benchmark") or {}
    if bm.get("n"):
        lines.append(
            f"Secondo benchmark ({bm.get('source')}): CLV "
            f"*{(bm.get('avg_clv') or 0) * 100.0:+.2f}%* su {bm['n']} campioni "
            f"| positivi {(bm.get('share_positive') or 0) * 100.0:.1f}% "
            f"| confermati da ENTRAMBI gli sharp: "
            f"{bm.get('confirmed_both')}/{bm['n']} "
            f"({(bm.get('confirmed_share') or 0) * 100.0:.1f}%)")
    elif n:
        lines.append(f"Secondo benchmark ({bm.get('source')}): nessun campione "
                     "con la chiusura dell'exchange — la conferma indipendente "
                     "non e' disponibile (la colonna si riempie a T-0)")
    for key, label in (("by_sport", "Per sport: "), ("by_market", "Per mercato: ")):
        row = _shared(m, key, "")
        if row:
            lines.append(label + row)
    if rep.get("pending"):
        lines.append(f"⏳ In attesa di chiusura: {rep['pending']} osservazioni "
                     f"({rep.get('pending_closed', 0)} su trade gia' conclusi) "
                     "— il campione crescera' ai prossimi T-0")
    exc = rep.get("excluded") or {}
    if exc:
        parts = ", ".join(f"{k} {v}" for k, v in sorted(exc.items()))
        lines.append(f"Esclusi (dichiarati, mai fatti sparire): {parts}")
    lines.append(f"Verdetto: *{STATUS_LABEL.get(m.get('status'), m.get('status'))}*"
                 + (f" — {m['note']}" if m.get("note") else ""))
    if m.get("detectable_clv") is not None:
        lines.append(f"Con questo campione il CLV minimo distinguibile e' "
                     f"±{m['detectable_clv'] * 100.0:.2f}%")
    if rep.get("error"):
        lines.append(f"⚠️ {rep['error']}")
    rows = sorted((rep.get("samples") or []),
                  key=lambda s: (s.get("closing_source") != "pinnacle_t0",
                                 -(s.get("clv") or 0.0)))[:max(0, limit)]
    if rows:
        lines.append("Ultimi campioni (per CLV):")
        for s in rows:
            lines.append(f"  • {s.get('home')} vs {s.get('away')} — "
                         f"{s.get('mercato')} {s.get('esito')} @ "
                         f"{s.get('price'):.3f} -> {s.get('closing'):.3f} "
                         f"({s.get('closing_source')}) "
                         f"CLV {float(s.get('clv') or 0) * 100.0:+.2f}%")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 4. CATTURA della closing line su SX Bet (il lato che mancava)
# ---------------------------------------------------------------------------

def _sx_price(provider: Any, market_id: str, selection_id: Any
              ) -> Optional[float]:
    """Miglior prezzo BACK sul book SX (lettura PUBBLICA, zero crediti)."""
    try:
        sel = int(selection_id) if str(selection_id or "").strip().isdigit() else 1
    except Exception:                                          # pragma: no cover
        sel = 1
    try:
        return provider.best_back_price(market_id, sel)
    except Exception as exc:
        logger.warning("clv_tracker: book SX %s non leggibile (%s)",
                       market_id, exc)
        return None


def _default_provider() -> Optional[Any]:
    """Provider SX per la cattura. Fail-closed su un provider diverso.

    La closing line che ci interessa e' quella della VENUE su cui eseguiamo
    (SX Bet): su un altro provider il prezzo sarebbe di un mercato che non
    abbiamo usato, e confrontarlo con la nostra quota non significherebbe
    niente.
    """
    try:
        import execution_engine as ee
        if str(getattr(ee, "EXECUTION_PROVIDER", "")).strip().lower() != "sxbet":
            return None
        return ee.build_provider()
    except Exception as exc:
        logger.warning("clv_tracker: provider SX non disponibile (%s)", exc)
        return None


def capture_sx_closing(*, provider: Any = None, price_lookup=None,
                       conn=None, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Scrive la closing line di SX Bet a T-0 per i trade LIVE ancora aperti.

    Perche' serve, oltre a `closing_line.py`: quella routine legge la chiusura
    di **Pinnacle**, che copre il calcio 1X2 ma non e' la venue su cui
    eseguiamo gli altri mercati (tennis, eSports, football USA) — e per quelli
    `Pinnacle` non e' nemmeno la chiusura piu' informativa. Qui il prezzo viene
    letto dall'**order book SX** via `market_id`/`selection_id` del trade:
    nessuna mappatura per nome, nessuna ambiguita'.

    Regole (tutte fail-closed, si veda il docstring del modulo):
    - gira nella STESSA finestra T-0 di `closing_line` (`window_minutes`);
    - **prima chiusura vince**: se `closing_odds` e' gia' stato scritto (da
      Pinnacle) non si sovrascrive — le due fonti non si mescolano;
    - nessun prezzo leggibile -> si salta con motivo, mai un valore inventato;
    - fail-safe per riga: un errore non ferma le altre.

    Ritorna `{checked, captured, skipped, rows}`.
    """
    out: Dict[str, Any] = {"checked": 0, "captured": 0, "skipped": {},
                           "rows": []}
    now = now or datetime.now(timezone.utc)
    pre, post = window_minutes()
    close_conn = False
    try:
        if conn is None:
            conn = _connect()
            close_conn = True
        rows = _rows(conn, _open_bets_sql(_table_columns(conn, "bets")),
                     errors=out.setdefault("query_errors", []))
    except Exception as exc:
        logger.debug("clv_tracker: cattura SX non avviata (%s)", exc)
        return out
    prov = provider
    if prov is None and price_lookup is None:
        prov = _default_provider()
        if prov is None:
            out["skipped"]["provider_not_sx"] = 1
            if close_conn:
                try:
                    conn.close()
                except Exception:
                    pass
            return out
    try:
        for row in rows:
            (match_id, mercato, esito, price, market_id, selection_id,
             _home, _away, commence) = row
            out["checked"] += 1
            reason = ""
            kickoff = _parse_ts(commence)
            if kickoff is None:
                # Senza kickoff non si sa se siamo a T-0: catturare adesso
                # significherebbe chiamare "chiusura" un prezzo qualunque.
                reason = "no_kickoff"
            else:
                delta = (kickoff - now).total_seconds() / 60.0
                if delta > pre or -delta > post:
                    reason = "out_of_window"
            if not reason:
                try:
                    exists, prev = _closing_present(conn, str(match_id),
                                                    str(esito), _home, _away)
                    if prev is not None:
                        reason = "already_captured"
                    elif exists:
                        reason = "already_captured"
                except Exception as exc:
                    logger.debug("clv_tracker: lettura chiusura fallita (%s)", exc)
                    reason = "read_error"
            value = None
            if not reason:
                try:
                    value = (price_lookup({"match_id": match_id,
                                           "mercato": mercato, "esito": esito,
                                           "market_id": market_id,
                                           "selection_id": selection_id})
                             if price_lookup is not None
                             else _sx_price(prov, market_id, selection_id))
                except Exception as exc:
                    logger.debug("clv_tracker: prezzo non letto (%s)", exc)
                    reason = "read_error"
                if not reason and (value is None or float(value) <= 1.0):
                    reason = "no_price"
            if not reason:
                try:
                    from tracker import save_clv
                    save_clv(str(match_id), str(esito),
                             float(price or 0.0) if price else float(value),
                             closing_odds=float(value), closing_source=SOURCE_SX)
                except Exception as exc:
                    logger.debug("clv_tracker: scrittura fallita (%s)", exc)
                    reason = "write_error"
            if reason:
                _bump(out["skipped"], reason)
                continue
            out["captured"] += 1
            out["rows"].append({"match_id": str(match_id), "mercato": mercato,
                                "esito": esito, "closing_odds": round(float(value), 6),
                                "source": SOURCE_SX})
    except Exception as exc:                                  # pragma: no cover
        logger.warning("clv_tracker: cattura SX interrotta (%s)", exc)
    finally:
        if close_conn:
            try:
                conn.close()
            except Exception:
                pass
    return out


def _closing_present(conn, match_id: str, esito: str, home: Any = "",
                     away: Any = "") -> Tuple[bool, Optional[float]]:
    """(esiste una riga per match+esito?, closing_odds gia' catturata?).

    Il match e' tollerante come il resto del modulo (esito scritto come nome
    squadra da un percorso e come `1`/`2` dall'altro): senza, la guardia
    `already_captured` mancherebbe la riga e la chiusura SX sovrascriverebbe
    quella di Pinnacle — le due fonti non si mescolano.
    """
    index = _clv_index(conn, str(match_id), home, away)
    key = _canonical(esito, home, away)
    rec = None
    if key is not None:
        rec = index.get(key)
    if rec is None:
        rec = index.get(str(esito).strip())
    if rec is None:
        return False, None
    value = _as_float(rec.get("closing_odds"))
    return True, (value if value and value > 1.0 else None)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:                 # pragma: no cover
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--days", type=float, default=None,
                    help="solo osservazioni create negli ultimi N giorni")
    ap.add_argument("--db", default=None, help="ledger (sola lettura)")
    ap.add_argument("--include-signals", action="store_true",
                    help="misura anche i segnali mai giocati (popolazione a parte)")
    ap.add_argument("--capture-sx", action="store_true",
                    help="cattura la closing line SX a T-0 (lettura pubblica)")
    ap.add_argument("--limit", type=int, default=10)
    args = ap.parse_args(argv)
    if args.capture_sx:
        got = capture_sx_closing()
        print(json.dumps(got, ensure_ascii=False, indent=2) if args.json
              else f"cattura SX: {got['captured']} su {got['checked']} "
                   f"(salti: {got['skipped']})")
    rep = report(db=args.db, days=args.days, include_signals=args.include_signals)
    print(json.dumps(rep, ensure_ascii=False, indent=2, default=str)
          if args.json else format_report(rep, limit=args.limit))
    return 0


if __name__ == "__main__":                                         # pragma: no cover
    raise SystemExit(main())
