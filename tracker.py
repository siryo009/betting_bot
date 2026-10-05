"""Tracker SQLite per segnali, calendario e analisi"""
import json
import logging
import math
import sqlite3
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from config import DATA_DIR

logger = logging.getLogger(__name__)

DB_PATH = DATA_DIR / "quotaverace.db"

SETTLEMENT_PAUSE_FILE = DATA_DIR / "execution" / "settlement_paused.json"


def settlement_paused() -> bool:
    """True se il settlement automatico e' in pausa (11/09/2026).

    Pausa richiesta dal proprietario durante il cambio di strategia: nessuna
    chiusura automatica di bet/previsioni/cassa. L'override vive sul volume
    (data/execution/settlement_paused.json) e sopravvive ai redeploy; si
    attiva anche via env `SETTLEMENT_PAUSED=1` (fail-safe per il container).
    """
    env = os.getenv("SETTLEMENT_PAUSED", "").strip().lower()
    if env in ("1", "true", "yes", "on"):
        return True
    try:
        data = json.loads(SETTLEMENT_PAUSE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return False
    return bool(data.get("paused"))


def set_settlement_paused(paused: bool) -> dict:
    """Attiva/disattiva la pausa del settlement (scrittura atomica)."""
    SETTLEMENT_PAUSE_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {"paused": bool(paused),
            "updated_at": datetime.now().isoformat()}
    tmp = SETTLEMENT_PAUSE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, SETTLEMENT_PAUSE_FILE)
    return data


def settlement_pause_status() -> dict:
    """Stato della pausa settlement per bot/API."""
    return {"paused": settlement_paused(),
            "env": os.getenv("SETTLEMENT_PAUSED", "") or None,
            "file": str(SETTLEMENT_PAUSE_FILE)}

# Ordine di preferenza dei verdetti nel dedup (prima = meglio): il verdetto
# definitivo batte quello provvisorio. Le righe APERTE (esito_finale NULL)
# sono trattate come 'lost' provvisorio: qualsiasi riga gia' chiusa la batte.
PREFERRED_OUTCOME_ORDER = {"won": 0, "push": 1, "lost": 2}

class Signal:
    # `surface` = ultima colonna della tabella signals (migrazione 09/09, usata
    # dal ledger tennis): senza il parametro get_signals crashava con
    # "takes 11 positional arguments but 12 were given".
    def __init__(self, id, chat_id, evento, esito, quota, probabilita, ev,
                 timestamp, esito_finale, profit, surface=None):
        self.id = id; self.chat_id = chat_id; self.evento = evento; self.esito = esito
        self.quota = quota; self.probabilita = probabilita; self.ev = ev
        self.timestamp = timestamp; self.esito_finale = esito_finale; self.profit = profit
        self.surface = surface

def _get_conn():
    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER, evento TEXT, esito TEXT, quota REAL,
        probabilita REAL, ev REAL, timestamp TEXT, esito_finale TEXT, profit REAL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS subscribers (chat_id INTEGER PRIMARY KEY)''')
    # Migrazione free/premium: colonne tier e scadenza abbonamento.
    sub_cols = [r[1] for r in c.execute("PRAGMA table_info(subscribers)")]
    if "tier" not in sub_cols:
        c.execute("ALTER TABLE subscribers ADD COLUMN tier TEXT DEFAULT 'free'")
    if "premium_until" not in sub_cols:
        c.execute("ALTER TABLE subscribers ADD COLUMN premium_until TEXT")
    c.execute('''CREATE TABLE IF NOT EXISTS matches (
        id TEXT PRIMARY KEY, league TEXT, home_team TEXT, away_team TEXT,
        commence_time TEXT, status TEXT, last_updated TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS match_analysis (
        id INTEGER PRIMARY KEY AUTOINCREMENT, match_id TEXT,
        lam_h REAL, lam_a REAL, prob_1 REAL, prob_X REAL, prob_2 REAL,
        prob_over REAL, best_ev REAL, best_esito TEXT, best_quota REAL,
        best_bookmaker TEXT, status TEXT, timestamp TEXT,
        market_prob REAL, market_edge REAL,
        FOREIGN KEY (match_id) REFERENCES matches(id))''')
    # Migrazione per DB esistenti: aggiunge le colonne mercato se mancano.
    cols = [r[1] for r in c.execute("PRAGMA table_info(match_analysis)")]
    if "market_prob" not in cols:
        c.execute("ALTER TABLE match_analysis ADD COLUMN market_prob REAL")
    if "market_edge" not in cols:
        c.execute("ALTER TABLE match_analysis ADD COLUMN market_edge REAL")
    c.execute('''CREATE TABLE IF NOT EXISTS notifications (
        match_id TEXT, date TEXT, PRIMARY KEY (match_id, date))''')
    c.execute('''CREATE TABLE IF NOT EXISTS clv_history (
        match_id TEXT, esito TEXT,
        signal_quota REAL, closing_quota REAL, updated_at TEXT,
        pinnacle_quota REAL,
        closing_odds REAL,
        PRIMARY KEY (match_id, esito))''')
    clv_cols = [r[1] for r in c.execute("PRAGMA table_info(clv_history)")]
    if "pinnacle_quota" not in clv_cols:
        c.execute("ALTER TABLE clv_history ADD COLUMN pinnacle_quota REAL")
    if "closing_odds" not in clv_cols:
        # Direttiva 04/10/2026 (punto 5): quota FINALE di Pinnacle catturata a
        # T-0 dalla routine `closing_line.py`. E' il riferimento per la
        # percentuale di beat sul mercato di ogni ordine (`beat_pct`);
        # `closing_quota` resta invece la chiusura del miglior bookmaker
        # (convenzione storica di `save_clv`).
        c.execute("ALTER TABLE clv_history ADD COLUMN closing_odds REAL")
    c.execute('''CREATE TABLE IF NOT EXISTS cassa (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        data TEXT, partita TEXT, esito TEXT, quota REAL,
        importo REAL, ev REAL, timestamp TEXT)''')
    # Migrazione: colonne di saldo (risultato reale + profitto realizzato).
    cassa_cols = [r[1] for r in c.execute("PRAGMA table_info(cassa)")]
    if "esito_finale" not in cassa_cols:
        c.execute("ALTER TABLE cassa ADD COLUMN esito_finale TEXT")
    if "profit" not in cassa_cols:
        c.execute("ALTER TABLE cassa ADD COLUMN profit REAL")
    if "settled_at" not in cassa_cols:
        c.execute("ALTER TABLE cassa ADD COLUMN settled_at TEXT")
    # Ledger previsioni: TUTTI i segnali proposti dal motore, con mercato,
    # saldati a fine partita (esito_finale) per calibrare il modello.
    c.execute('''CREATE TABLE IF NOT EXISTS predictions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        match_id TEXT, mercato TEXT, esito TEXT,
        quota REAL, prob REAL, ev REAL,
        market_prob REAL, market_edge REAL,
        status TEXT, esito_finale TEXT, profit REAL,
        league TEXT,
        created_at TEXT, settled_at TEXT,
        UNIQUE(match_id, mercato, esito))''')
    # Migrazione idempotente per DB nati prima di settled_at (usato da
    # settle_predictions, settle_bets e drift_monitor): stessa convenzione
    # delle colonne di saldo su cassa.
    _pred_cols = [r[1] for r in c.execute("PRAGMA table_info(predictions)")]
    if "settled_at" not in _pred_cols:
        c.execute("ALTER TABLE predictions ADD COLUMN settled_at TEXT")
    # Migrazione idempotente (22/09): la LEGA del segnale sul ledger
    # previsioni. Prima era ricavabile solo dalla JOIN con `matches`, che non
    # copre le righe senza partita (i match SX cancellati/riscritti): il 65%
    # del ledger risultava cosi' non attribuibile a una lega e la strategia
    # per lega (core/probation/bloccata) era non misurabile. La lega vive
    # sulla riga del segnale, cosi' resta anche se la partita sparisce.
    if "league" not in _pred_cols:
        c.execute("ALTER TABLE predictions ADD COLUMN league TEXT")
    # Puntate automatiche (auto_bet.py): SIM-only dal 04/09 (paper trading
    # con la quota del segnale), saldati a fine partita come le previsioni.
    c.execute('''CREATE TABLE IF NOT EXISTS bets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        match_id TEXT, mercato TEXT, esito TEXT,
        market_id TEXT, selection_id INTEGER,
        price REAL, stake REAL, mode TEXT, status TEXT, bet_id TEXT,
        esito_finale TEXT, profit REAL,
        created_at TEXT, settled_at TEXT,
        UNIQUE(match_id, esito))''')
    _bet_cols = [r[1] for r in c.execute("PRAGMA table_info(bets)")]
    if "settled_at" not in _bet_cols:
        c.execute("ALTER TABLE bets ADD COLUMN settled_at TEXT")
    # Ledger delle DECISIONI (decision/, 14/09/2026): una riga per ogni
    # segnale passato nella catena Signal -> Risk -> Stake, col verdetto e il
    # motivo (machine-readable). E' la base del feedback engine: lega input,
    # decisione, stake e (dopo) ordine ed esito, cosi' si puo' misurare se i
    # gate avevano ragione ANCHE sulle righe non giocate (shadow).
    _ensure_decisions_table(c)
    # Ledger quote MULTI-MERCATO (schema 2.0 di decision/market.py): una riga
    # per quota, chiave (partita, mercato, linea, esito). Nasce vuoto e lo
    # riempie l'ingestione delle quote (gateway `MarketQuotesGateway`).
    _ensure_market_quotes_table(c)
    # Migrazione: colonna surface nella tabella signals (09/09)
    # per supportare il tracking delle superfici nel modulo tennis.
    sig_cols = [r[1] for r in c.execute("PRAGMA table_info(signals)")]
    if "surface" not in sig_cols:
        c.execute("ALTER TABLE signals ADD COLUMN surface TEXT")
    _ensure_unique_constraints(c)
    conn.commit()
    return conn


def _dedupe_normalized_esito(c) -> int:
    """Dedup dei ledger per chiave NORMALIZZATA (match_id, mercato, esito).

    I segnali 1X2 usano il nome squadra (es. "Inter") mentre il salvataggio
    delle puntate usa la chiave compatta ("1"), e l'OU arriva come "Over 2.5"
    o "over" a seconda del percorso: senza normalizzazione il dedup non li
    riconosce come lo stesso segnale. Ricerca per gruppo NORMALIZZATO e
    conserva la riga migliore (PREFERRED_OUTCOME_ORDER).

    Ritorna il numero di righe eliminate. Chiamata da _ensure_unique_constraints
    (solo dove serve: la normalizzazione puo' generare conflitti NUOVI).
    """
    removed = 0
    try:
        groups = c.execute(
            '''SELECT match_id, mercato, LOWER(TRIM(esito)), COUNT(*) n
               FROM predictions
               WHERE mercato IN ('1X2', 'OU')
               GROUP BY match_id, mercato, LOWER(TRIM(esito))
               HAVING COUNT(*) > 1''').fetchall()
    except sqlite3.OperationalError:
        return 0
    for mid, mkt, es, n in groups:
        rows = c.execute(
            '''SELECT id, esito, esito_finale, profit FROM predictions
               WHERE match_id=? AND mercato=? AND LOWER(TRIM(esito))=?
               ORDER BY id''', (mid, mkt, es)).fetchall()
        if len(rows) <= 1:
            continue
        def _rank(row):
            _id, esito, outcome, profit = row
            if outcome is not None:
                return (0, PREFERRED_OUTCOME_ORDER.get(outcome, 3), -_id)
            return (1, PREFERRED_OUTCOME_ORDER.get("lost"), -_id)
        best_id = min(rows, key=_rank)[0]
        for r_id, _, _, _ in rows:
            if r_id != best_id:
                c.execute("DELETE FROM predictions WHERE id=?", (r_id,))
                removed += 1
    return removed


def _create_ledger_table(c, table: str) -> None:
    """CREATE TABLE (IF NOT EXISTS) per i ledger con i vincoli UNIQUE.

    Unico punto di definizione dello schema: usato da _get_conn, dal
    recupero delle migrazioni interrotte e dalla migrazione stessa.
    """
    if table == "predictions":
        c.execute('''CREATE TABLE IF NOT EXISTS predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id TEXT, mercato TEXT, esito TEXT,
            quota REAL, prob REAL, ev REAL,
            market_prob REAL, market_edge REAL,
            status TEXT, esito_finale TEXT, profit REAL,
            league TEXT,
            created_at TEXT, settled_at TEXT,
            UNIQUE(match_id, mercato, esito))''')
    elif table == "bets":
        c.execute('''CREATE TABLE IF NOT EXISTS bets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id TEXT, mercato TEXT, esito TEXT,
            market_id TEXT, selection_id INTEGER,
            price REAL, stake REAL, mode TEXT, status TEXT, bet_id TEXT,
            esito_finale TEXT, profit REAL,
            created_at TEXT, settled_at TEXT,
            UNIQUE(match_id, esito))''')


# --- Ledger decisioni (decision/) -------------------------------------------
# Colonne del ledger `decisions`, nell'ordine in cui vengono scritte: unica
# fonte di verita' per CREATE TABLE, migrazione e INSERT.
DECISION_FIELDS = (
    "record_id", "signal_id", "match_id", "league", "market", "outcome",
    "selection_label", "kickoff", "price", "price_source", "market_prob",
    "model_prob", "blended_prob", "edge", "ev", "tier", "confidence",
    "model_coverage", "calibrated", "verdict", "reason", "status",
    "mode", "provider", "stake", "stake_executable", "kelly_fraction",
    "cap_pct", "cap_source", "approved_by", "review_note", "created_at",
)
# Colonne riempite DOPO la decisione (esecuzione e referto): un secondo
# salvataggio dello stesso record non deve mai cancellarle.
DECISION_LATE_FIELDS = ("order_id", "order_status", "esito_finale", "profit",
                        "settled_at")
_DECISION_INT_FIELDS = ("calibrated", "stake_executable")

# Stati del ciclo di vita di una decisione (Shadow Validation, 16/09/2026): la
# riga NASCE `pending` col salvataggio e diventa `validated`/`rejected` quando il
# motore di convalida la esamina (`decision/validation.py`). Solo `validated`
# autorizza l'ordine reale: "persistito" non vuol dire "approvato".
# ⚠️ Il ledger NON importa il pacchetto `decision` (e viceversa): le stringhe
# sono duplicate qui di proposito e un tripwire in `test_decision_validation.py`
# verifica che le due tabelle coincidano, cosi' non possono divergere in
# silenzio.
DECISION_STATUS_PENDING = "pending"
DECISION_STATUS_VALIDATED = "validated"
DECISION_STATUS_REJECTED = "rejected"
DECISION_STATUSES = (DECISION_STATUS_PENDING, DECISION_STATUS_VALIDATED,
                     DECISION_STATUS_REJECTED)


def _ensure_decisions_table(c) -> None:
    """Ledger decisioni pronto all'uso: tabella -> colonne -> indici.

    L'ORDINE conta: un `decisions` creato da un deploy precedente puo' avere
    meno colonne, quindi gli indici (e le colonne mancanti) vanno gestiti
    DOPO la CREATE TABLE — altrimenti `_get_conn` fallisce all'avvio e con
    lui l'intero bot (bug trovato dai test il 14/09).
    """
    _create_decisions_table(c)
    _migrate_decisions(c)


def _create_decisions_table(c) -> None:
    """Crea il ledger delle decisioni (idempotente)."""
    c.execute('''CREATE TABLE IF NOT EXISTS decisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        record_id TEXT UNIQUE,
        signal_id TEXT, match_id TEXT, league TEXT, market TEXT, outcome TEXT,
        selection_label TEXT, kickoff TEXT, price REAL, price_source TEXT,
        market_prob REAL, model_prob REAL, blended_prob REAL,
        edge REAL, ev REAL, tier TEXT, confidence REAL,
        model_coverage REAL, calibrated INTEGER,
        verdict TEXT, reason TEXT, mode TEXT, provider TEXT,
        stake REAL, stake_executable INTEGER, kelly_fraction REAL,
        cap_pct REAL, cap_source TEXT,
        approved_by TEXT, review_note TEXT, status TEXT,
        order_id TEXT, order_status TEXT,
        esito_finale TEXT, profit REAL,
        created_at TEXT, settled_at TEXT)''')


def _migrate_decisions(c) -> None:
    """Colonne mancanti + indici di un `decisions` creato da un deploy
    precedente (ALTER TABLE idempotente, stessa convenzione di predictions/
    bets/cassa). Nessun default copiato a mano: i tipi si leggono da
    DECISION_FIELDS + DECISION_LATE_FIELDS."""
    try:
        cols = [r[1] for r in c.execute("PRAGMA table_info(decisions)")]
    except sqlite3.OperationalError:
        return
    numeric = ("price", "market_prob", "model_prob", "blended_prob", "edge",
               "ev", "confidence", "model_coverage", "stake", "kelly_fraction",
               "cap_pct", "profit")
    integer = _DECISION_INT_FIELDS
    for name in DECISION_FIELDS + DECISION_LATE_FIELDS:
        if name in cols:
            continue
        kind = "REAL" if name in numeric else ("INTEGER" if name in integer else "TEXT")
        c.execute(f"ALTER TABLE decisions ADD COLUMN {name} {kind}")
    c.execute("CREATE INDEX IF NOT EXISTS idx_decisions_match ON decisions(match_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_decisions_open "
              "ON decisions(esito_finale)")
    # Indice sullo STATO della validazione: e' il filtro del lavoro in attesa
    # (`get_decisions(status=...)`) e dell'audit "cosa non e' mai stato validato".
    c.execute("CREATE INDEX IF NOT EXISTS idx_decisions_status ON decisions(status)")


# --- Quote di mercato MULTI-MERCATO (schema 2.0 di decision/market.py) ------
# Una riga per QUOTA, non per partita: il ledger deve ospitare piu' mercati
# dello stesso fixture (1X2, OU 2.5, AH -0.75, BTTS, ...). La chiave e' la
# stessa IDENTITA' del contratto (`MarketQuote.identity_key` = fixture_id +
# market_type + line_key + selection): se cambia la chiave del contratto,
# cambia questa tabella.
#
# Perche' la LINEA sta nella chiave: due totali 2.5 e 3.5 sono due mercati
# diversi, e confonderli significherebbe saldare un esito col risultato di un
# altro (la classe di bug del 09/09 sul match OU/1X2).
#
# `line_key` e' una STRINGA ('' per i mercati senza linea): e' la forma
# canonica del contratto, cosi' 2.5 e 2.5000000001 non diventano due righe.
# ⚠️ NULL su una colonna di chiave non e' una chiave: SQLite considera i NULL
# distinti fra loro e ammetterebbe righe "uguali" all'infinito — le quattro
# colonne della chiave sono quindi NOT NULL (difesa, non formalita').
MARKET_QUOTE_KEYS = ("fixture_id", "market_type", "line_key", "selection")

#: Colonne del ledger (nome, tipo SQLite) nell'ordine in cui vengono scritte:
#: CREATE TABLE, migrazione e INSERT leggono tutte da qui (un solo posto in cui
#: lo schema e' dichiarato).
MARKET_QUOTE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("fixture_id", "TEXT"), ("market_type", "TEXT"), ("line_key", "TEXT"),
    ("selection", "TEXT"), ("line", "REAL"), ("selection_label", "TEXT"),
    ("ledger_esito", "TEXT"), ("price", "REAL"), ("implied_prob", "REAL"),
    ("market_prob", "REAL"), ("liquidity", "REAL"), ("main_line", "INTEGER"),
    ("origin", "TEXT"), ("derived_from_json", "TEXT"), ("source", "TEXT"),
    ("gateway_id", "TEXT"), ("schema_version", "TEXT"), ("observed_at", "TEXT"),
    ("kickoff", "TEXT"), ("event_name", "TEXT"), ("league", "TEXT"),
    ("home", "TEXT"), ("away", "TEXT"), ("identity_key", "TEXT"),
    ("quote_id", "TEXT"), ("extra_json", "TEXT"), ("updated_at", "TEXT"),
)
MARKET_QUOTE_FIELDS = tuple(name for name, _ in MARKET_QUOTE_COLUMNS)

#: Rinomine DICHIARATE fra la riga del contratto (`MarketQuote.as_row()`) e le
#: colonne del ledger: il contratto dice `odds`, lo schema dice `price`;
#: `depth_usdc` -> `liquidity`; tuple/dict -> JSON. Gli altri campi si chiamano
#: uguale. Un tripwire in `test_market_quotes_store.py` pretende che OGNI campo
#: di `MARKET_ROW_FIELDS` finisca in una colonna: un campo nuovo del contratto
#: non puo' sparire in silenzio.
MARKET_QUOTE_SOURCE = {
    "price": "odds",
    "liquidity": "depth_usdc",
    "derived_from_json": "derived_from",
    "extra_json": "extra",
}

#: Colonne che NON arrivano dalla riga del contratto:
#: - `implied_prob` e' CALCOLATA (1/price): aritmetica della forma, non una
#:   scelta di strategia;
#: - `market_prob` si scrive SOLO se il chiamante la porta (devig): il
#:   devigging e' una misura dell'engine, il ledger non la inventa;
#: - `updated_at` e' l'istante della scrittura (UTC ISO).
MARKET_QUOTE_DERIVED = ("implied_prob", "market_prob", "updated_at")


def _ensure_market_quotes_table(c) -> None:
    """Ledger quote pronto all'uso: tabella -> colonne -> indici.

    L'ORDINE conta (lezione del 14/09 su `decisions`): un `market_quotes`
    creato da una versione precedente puo' avere meno colonne, quindi le
    colonne mancanti e gli indici si gestiscono DOPO la CREATE TABLE —
    altrimenti `_get_conn` fallisce all'avvio e con lui tutto il bot.
    """
    _create_market_quotes_table(c)
    _migrate_market_quotes(c)


def _create_market_quotes_table(c) -> None:
    """Crea il ledger multi-mercato (idempotente)."""
    defs = []
    for name, kind in MARKET_QUOTE_COLUMNS:
        if name in MARKET_QUOTE_KEYS:
            defs.append(f"{name} {kind} NOT NULL")
        elif name == "origin":
            defs.append("origin TEXT NOT NULL DEFAULT 'native'")
        elif name == "updated_at":
            # TEXT e non TIMESTAMP: le date del ledger sono testo ISO e ogni
            # confronto SQL va avvolto in datetime(...) — regola del 17/09.
            defs.append("updated_at TEXT DEFAULT CURRENT_TIMESTAMP")
        else:
            defs.append(f"{name} {kind}")
    defs.append("PRIMARY KEY (fixture_id, market_type, line_key, selection)")
    c.execute("CREATE TABLE IF NOT EXISTS market_quotes (\n    "
              + ",\n    ".join(defs) + ")")


def _migrate_market_quotes(c) -> None:
    """Colonne mancanti + indici di un `market_quotes` precedente (idempotente)."""
    try:
        cols = [r[1] for r in c.execute("PRAGMA table_info(market_quotes)")]
    except sqlite3.OperationalError:
        return
    for name, kind in MARKET_QUOTE_COLUMNS:
        if name in cols:
            continue
        if name in MARKET_QUOTE_KEYS:
            # Una colonna di chiave non puo' essere NULL (vedi sopra): una riga
            # migrata senza valore non deve diventare una chiave NULL.
            c.execute(f"ALTER TABLE market_quotes ADD COLUMN {name} {kind} "
                      "NOT NULL DEFAULT ''")
        else:
            # ALTER TABLE non accetta default non costanti (CURRENT_TIMESTAMP):
            # `updated_at` nasce vuoto sulle righe migrate e si riempie alla
            # prima riscrittura della riga.
            c.execute(f"ALTER TABLE market_quotes ADD COLUMN {name} {kind}")
    # Indice del LOOKUP del ciclo di auto-bet: lo snapshot di un mercato esatto
    # (partita + mercato + linea) senza full table scan.
    c.execute("CREATE INDEX IF NOT EXISTS idx_market_quotes_lookup "
              "ON market_quotes(fixture_id, market_type, line_key)")
    # Indice di AUDIT/analisi per mercato (es. tutti gli OU 2.5) che attraversa
    # le partite: il composite della chiave non lo copre, perche' comincia dal
    # fixture.
    c.execute("CREATE INDEX IF NOT EXISTS idx_market_quotes_market "
              "ON market_quotes(market_type, line_key)")


def _utc_iso() -> str:
    """Istante UTC in ISO-8601 (con offset): la data si scrive esplicita."""
    return datetime.now(timezone.utc).isoformat()


def _quote_field(row, name):
    """Lettura DIFENSIVA di un campo: una riga ostile non fa esplodere il lotto."""
    try:
        if hasattr(row, "as_row"):
            row = row.as_row()
        if hasattr(row, "get"):
            return row.get(name)
        return getattr(row, name, None)
    except Exception:
        return None


def _quote_float(value):
    """Float sicuro (None se non numerico): mai un'eccezione dal feed."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _quote_flag(value):
    """Booleano a tre stati: None (non dichiarato) / 0 / 1."""
    if value is None:
        return None
    return 1 if value else 0


def _quote_json(value, fallback):
    """Serializza una struttura per il ledger: non solleva MAI."""
    payload = value if value is not None else fallback
    try:
        return json.dumps(payload, ensure_ascii=False, default=str)
    except Exception:
        return json.dumps(fallback, ensure_ascii=False, default=str)


def _quote_load(text, fallback):
    """Rilegge il JSON scritto dal ledger (riga corrotta -> fallback)."""
    if text in (None, ""):
        return fallback
    try:
        return json.loads(text)
    except Exception:
        return fallback


def _market_quote_row(row) -> tuple:
    """Normalizza una quota per l'upsert: (riga pronta, motivo di scarto).

    Fail-safe e fail-closed insieme: una riga malformata viene SCARTATA (mai
    scritta a meta') e contabilizzata dal chiamante, ma non solleva mai — una
    quota sporca non deve impedire il salvataggio delle altre.
    """
    fixture_id = str(_quote_field(row, "fixture_id") or "").strip()
    if not fixture_id:
        return None, "fixture_id_mancante"
    market_type = str(_quote_field(row, "market_type") or "").strip()
    if not market_type:
        return None, "market_type_mancante"
    selection = str(_quote_field(row, "selection") or "").strip()
    if not selection:
        return None, "selection_mancante"
    price = _quote_float(_quote_field(row, MARKET_QUOTE_SOURCE["price"]))
    if price is None or price <= 0:
        return None, "price_non_valido"
    line = _quote_float(_quote_field(row, "line"))
    raw_key = _quote_field(row, "line_key")
    line_key = "" if raw_key is None else str(raw_key).strip()
    if not line_key and line is not None:
        # Mai fondere due linee nella stessa riga: un OU 2.5 e un OU 3.5 con la
        # stessa `selection` collasserebbero nella stessa chiave e si
        # salderebbe l'esito di un mercato con il risultato di un altro.
        return None, "line_key_mancante"
    # Copertura GENERICA: ogni colonna legge il suo campo dichiarato, cosi' un
    # campo aggiunto allo schema non puo' restare fuori dal salvataggio.
    prepared = {name: _quote_field(row, MARKET_QUOTE_SOURCE.get(name, name))
                for name in MARKET_QUOTE_FIELDS}
    prepared.update({
        "fixture_id": fixture_id,
        "market_type": market_type,
        "selection": selection,
        "line_key": line_key,
        "line": line,
        "selection_label": _quote_field(row, "selection_label"),
        "ledger_esito": _quote_field(row, "ledger_esito") or selection,
        "price": price,
        "implied_prob": 1.0 / price,
        "market_prob": _quote_float(_quote_field(row, "market_prob")),
        "liquidity": _quote_float(_quote_field(row, MARKET_QUOTE_SOURCE["liquidity"])),
        "main_line": _quote_flag(_quote_field(row, "main_line")),
        "origin": str(_quote_field(row, "origin") or "native"),
        "derived_from_json": _quote_json(
            _quote_field(row, MARKET_QUOTE_SOURCE["derived_from_json"]), []),
        "extra_json": _quote_json(
            _quote_field(row, MARKET_QUOTE_SOURCE["extra_json"]), {}),
        "updated_at": _utc_iso(),
    })
    return prepared, None


def _market_quote_iter(rows):
    """Itera le righe da salvare: lista, `FixtureQuotes` (as_rows) o singola."""
    if hasattr(rows, "as_rows"):
        return list(rows.as_rows())
    if isinstance(rows, dict) or hasattr(rows, "as_row"):
        return [rows]
    try:
        return list(rows)
    except TypeError:
        return [rows]


def save_market_quotes(rows, conn=None) -> dict:
    """Upsert delle quote multi-mercato (una riga per quota). Idempotente.

    Una lettura ripetuta del palinsesto AGGIORNA `price`/`updated_at` della
    riga esistente invece di duplicare (PRIMARY KEY composta + ON CONFLICT):
    e' esattamente cio' che serve quando le quote fluttuano nella finestra
    T-60 e il feed gira di continuo.

    Ritorna {saved, skipped, fixtures, by_reason, error} e NON solleva mai:
    l'ingestione e' telemetria, non deve fermare il giro (stessa regola del
    feedback engine del 14/09).
    """
    try:
        prepared_rows, rejected, fixtures = [], {}, set()
        for row in _market_quote_iter(rows):
            prepared, reason = _market_quote_row(row)
            if prepared is None:
                key = reason or "riga_non_valida"
                rejected[key] = rejected.get(key, 0) + 1
                continue
            prepared_rows.append(prepared)
            fixtures.add(prepared["fixture_id"])
    except Exception as exc:                       # lotto non leggibile
        logger.warning("market_quotes: lotto non leggibile (%s)", exc)
        return {"saved": 0, "skipped": 0, "fixtures": 0, "by_reason": {},
                "error": f"lotto non leggibile: {exc}"}
    if rejected:
        logger.warning("market_quotes: %s righe scartate %s",
                       sum(rejected.values()), rejected)
    if not prepared_rows:
        return {"saved": 0, "skipped": sum(rejected.values()), "fixtures": 0,
                "by_reason": rejected, "error": None}
    own_conn = conn is None
    if own_conn:
        conn = _get_conn()
    c = conn.cursor()
    try:
        _ensure_market_quotes_table(c)
        cols = ", ".join(MARKET_QUOTE_FIELDS)
        marks = ", ".join("?" * len(MARKET_QUOTE_FIELDS))
        # Sulla riga esistente si aggiorna TUTTO tranne la chiave: la quota
        # nuova sostituisce la vecchia (lo storico dei prezzi vive in
        # `price_snapshots`, non qui).
        updates = ", ".join(f"{name}=excluded.{name}"
                            for name in MARKET_QUOTE_FIELDS
                            if name not in MARKET_QUOTE_KEYS)
        values = [tuple(row[name] for name in MARKET_QUOTE_FIELDS)
                  for row in prepared_rows]
        c.executemany(
            f"INSERT INTO market_quotes ({cols}) VALUES ({marks}) "
            f"ON CONFLICT({', '.join(MARKET_QUOTE_KEYS)}) DO UPDATE SET {updates}",
            values)
        if own_conn:
            conn.commit()
    except Exception as exc:                       # scrittura
        logger.warning("market_quotes: salvataggio fallito (%s)", exc)
        return {"saved": 0, "skipped": len(prepared_rows),
                "fixtures": len(fixtures), "by_reason": rejected,
                "error": str(exc)}
    finally:
        if own_conn:
            conn.close()
    return {"saved": len(prepared_rows), "skipped": sum(rejected.values()),
            "fixtures": len(fixtures), "by_reason": rejected, "error": None}


def _market_quote_from_row(row) -> dict:
    """Riga del ledger -> dict leggibile (bool e JSON ripristinati)."""
    data = dict(zip(MARKET_QUOTE_FIELDS, row))
    main_line = data.get("main_line")
    data["main_line"] = None if main_line is None else bool(main_line)
    data["derived_from"] = _quote_load(data.pop("derived_from_json", None), [])
    data["extra"] = _quote_load(data.pop("extra_json", None), {})
    return data


def get_market_quotes(fixture_id=None, market_type=None, line_key=None,
                      selection=None, limit=None, conn=None) -> list[dict]:
    """Snapshot delle quote salvate, filtrate per chiave (sola lettura)."""
    where, params = [], []
    for name, value in (("fixture_id", fixture_id),
                        ("market_type", market_type),
                        ("line_key", line_key),
                        ("selection", selection)):
        if value is None:
            continue
        where.append(f"{name} = ?")
        params.append(value)
    sql = f"SELECT {', '.join(MARKET_QUOTE_FIELDS)} FROM market_quotes"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY fixture_id, market_type, line_key, selection"
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    own_conn = conn is None
    if own_conn:
        conn = _get_conn()
    try:
        _ensure_market_quotes_table(conn.cursor())
        rows = conn.execute(sql, tuple(params)).fetchall()
    except Exception as exc:                       # lettura fallita
        logger.warning("market_quotes: lettura fallita (%s)", exc)
        return []
    finally:
        if own_conn:
            conn.close()
    return [_market_quote_from_row(row) for row in rows]


def market_quote(fixture_id, market_type, selection, line_key=None,
                 conn=None) -> Optional[dict]:
    """La quota di un mercato ESATTO (lookup della finestra T-60).

    None significa "non c'e' quella riga": il chiamante decide cosa farne —
    qui non si inventa un prezzo.
    """
    rows = get_market_quotes(fixture_id=fixture_id, market_type=market_type,
                             line_key="" if line_key is None else str(line_key),
                             selection=selection, limit=1, conn=conn)
    return rows[0] if rows else None


def count_market_quotes(fixture_id=None, conn=None) -> int:
    """Quante quote sono sul ledger (opzionalmente di una sola partita)."""
    own_conn = conn is None
    if own_conn:
        conn = _get_conn()
    try:
        _ensure_market_quotes_table(conn.cursor())
        if fixture_id is None:
            row = conn.execute("SELECT COUNT(*) FROM market_quotes").fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) FROM market_quotes WHERE fixture_id = ?",
                               (fixture_id,)).fetchone()
        return int(row[0] if row else 0)
    except Exception as exc:
        logger.warning("market_quotes: conteggio fallito (%s)", exc)
        return 0
    finally:
        if own_conn:
            conn.close()


def prune_market_quotes(days=7, conn=None) -> int:
    """Elimina le quote di partite giocate da oltre `days` giorni (default 7).

    La tabella si aggiorna in upsert (quindi non cresce a ogni ciclo), ma i
    fixture PASSATI resterebbero per sempre: senza potatura il volume cresce
    comunque. Si guarda il kickoff e, per le righe che non lo portano,
    l'istante di osservazione.

    ⚠️ Ogni confronto di data e' avvolto in `datetime(...)` (regola del 17/09:
    il ledger salva ISO con la 'T', il cutoff di SQLite usa lo spazio, e fra
    stringhe il confronto sbaglia di ~un giorno).
    """
    window = f"-{max(1, int(days))} days"
    own_conn = conn is None
    if own_conn:
        conn = _get_conn()
    try:
        _ensure_market_quotes_table(conn.cursor())
        cur = conn.execute(
            "DELETE FROM market_quotes "
            "WHERE datetime(COALESCE(kickoff, observed_at)) < datetime('now', ?)",
            (window,))
        removed = int(cur.rowcount or 0)
        if own_conn:
            conn.commit()
        return removed
    except Exception as exc:
        logger.warning("market_quotes: potatura fallita (%s)", exc)
        return 0
    finally:
        if own_conn:
            conn.close()


def _decision_flat_row(record) -> dict:
    """Riga piatta da un `DecisionRecord` (o da un dict gia' piatto).

    Import PIGRO di `decision.models`: `tracker` e' il ledger, non deve
    dipendere dal pacchetto di decisione all'import (il tripwire in
    test_decision_pipeline.py verifica che `import decision` non carichi
    tracker, e questa e' l'altra meta' della stessa regola).
    """
    if hasattr(record, "as_row"):
        return dict(record.as_row())
    return dict(record)


def _decision_values(row: dict) -> list:
    """Valori nell'ordine di DECISION_FIELDS (bool -> int, None -> NULL)."""
    values = []
    for name in DECISION_FIELDS:
        value = row.get(name)
        if name in _DECISION_INT_FIELDS and value is not None:
            value = 1 if value else 0
        values.append(value)
    return values


def save_decision(record, conn=None) -> str:
    """Registra (o aggiorna) una decisione della catena. Ritorna il record_id.

    IDEMPOTENTE su `record_id` (id stabile del segnale + timestamp): ripersistere
    lo stesso record aggiorna verdetto/stake ma NON cancella ordine ed esito —
    sono le colonne "late", riempite dopo la decisione. Il chiamante di
    produzione e' `decision.feedback.persist`, che rende la scrittura
    fail-safe: un ledger non scrivibile non deve mai fermare una puntata.
    """
    row = _decision_flat_row(record)
    record_id = str(row.get("record_id") or "")
    if not record_id:
        raise ValueError("decisione senza record_id")
    late = {
        "order_id": row.get("order_id"),
        "order_status": row.get("order_status"),
        "esito_finale": row.get("outcome_final", row.get("esito_finale")),
        "profit": row.get("profit"),
        "settled_at": row.get("settled_at"),
    }
    own_conn = conn is None
    if own_conn:
        conn = _get_conn()
    c = conn.cursor()
    try:
        _ensure_decisions_table(c)
        cols = ", ".join(DECISION_FIELDS + DECISION_LATE_FIELDS)
        marks = ", ".join("?" * (len(DECISION_FIELDS) + len(DECISION_LATE_FIELDS)))
        # Le colonne "late" in update usano COALESCE: un None non sovrascrive
        # un ordine o un esito gia' registrati.
        updates = ", ".join(
            [f"{name}=excluded.{name}" for name in DECISION_FIELDS] +
            [f"{name}=COALESCE(excluded.{name}, decisions.{name})"
             for name in DECISION_LATE_FIELDS])
        c.execute(f'''INSERT INTO decisions ({cols}) VALUES ({marks})
                      ON CONFLICT(record_id) DO UPDATE SET {updates}''',
                  _decision_values(row) + [late[name] for name in DECISION_LATE_FIELDS])
        if own_conn:
            conn.commit()
    finally:
        if own_conn:
            conn.close()
    return record_id


def get_decisions(closed=None, verdict=None, limit=500, status=None) -> list[dict]:
    """Righe del ledger decisioni (piu' recenti prima).

    `status` filtra sullo stato della Shadow Validation (`pending` =
    persistita ma non ancora convalidata, `validated` = ordine autorizzato).
    """
    conn = _get_conn(); c = conn.cursor()
    q = f"SELECT {', '.join(DECISION_FIELDS + DECISION_LATE_FIELDS)} FROM decisions"
    conds, args = [], []
    if verdict:
        conds.append("verdict=?"); args.append(verdict)
    if status:
        conds.append("status=?"); args.append(status)
    if closed is True:
        conds.append("esito_finale IS NOT NULL")
    elif closed is False:
        conds.append("esito_finale IS NULL")
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY id DESC LIMIT ?"; args.append(limit)
    rows = c.execute(q, args).fetchall()
    conn.close()
    return [dict(zip(DECISION_FIELDS + DECISION_LATE_FIELDS, r)) for r in rows]


def update_decision_order(record_id, order: dict, conn=None) -> bool:
    """Aggancia l'ordine eseguito (bet_id/status) alla decisione.

    Ritorna True se una riga e' stata aggiornata. Non solleva mai su un
    `order` malformato: il ledger non deve rompere l'esecuzione.
    """
    order = order or {}
    order_id = order.get("bet_id") or order.get("order_id")
    status = order.get("status")
    own_conn = conn is None
    if own_conn:
        conn = _get_conn()
    try:
        cur = conn.cursor()
        _ensure_decisions_table(cur)
        cur.execute("UPDATE decisions SET order_id=COALESCE(?, order_id), "
                    "order_status=COALESCE(?, order_status) WHERE record_id=?",
                    (order_id, status, record_id))
        changed = cur.rowcount > 0
        if own_conn:
            conn.commit()
    finally:
        if own_conn:
            conn.close()
    return changed


def get_decision(record_id, conn=None) -> Optional[dict]:
    """Una riga del ledger decisioni per `record_id` (None se assente).

    Serve al motore di convalida per esaminare cio' che e' stato DAVVERO
    scritto, non l'oggetto in memoria: se la riga non c'e', non c'e' nulla da
    convalidare (fail-closed, vedi `decision/validation.py`).
    """
    if not record_id:
        return None
    own_conn = conn is None
    if own_conn:
        conn = _get_conn()
    c = conn.cursor()
    try:
        _ensure_decisions_table(c)
        row = c.execute(
            f"SELECT {', '.join(DECISION_FIELDS + DECISION_LATE_FIELDS)} "
            "FROM decisions WHERE record_id=?", (str(record_id),)).fetchone()
    finally:
        if own_conn:
            conn.close()
    if not row:
        return None
    return dict(zip(DECISION_FIELDS + DECISION_LATE_FIELDS, row))


def set_decision_status(record_id, status: str, conn=None) -> bool:
    """Scrive lo stato della Shadow Validation su una decisione persistita.

    Ritorna True se una riga e' stata aggiornata. NON solleva su uno stato
    ignoto: il ledger non deve rompere l'esecuzione (il chiamante fail-safe e'
    `decision.feedback.set_status`).
    """
    if not record_id:
        return False
    own_conn = conn is None
    if own_conn:
        conn = _get_conn()
    try:
        cur = conn.cursor()
        _ensure_decisions_table(cur)
        cur.execute("UPDATE decisions SET status=? WHERE record_id=?",
                    (str(status or ""), str(record_id)))
        changed = cur.rowcount > 0
        if own_conn:
            conn.commit()
    finally:
        if own_conn:
            conn.close()
    return changed


def decision_exists_for_signal(signal_id, conn=None) -> bool:
    """Esiste gia' una riga per questo segnale?

    E' la chiave di deduplicazione della shadow persistence: `record_id` porta
    i SECONDI e cambia a ogni giro del job (ogni 60s), quindi deduplicare su di
    esso scriverebbe la stessa opportunita' 1440 volte al giorno. Il segnale
    (`signal_id` = match+mercato+esito) e' invece STABILE nel tempo.
    """
    if not signal_id:
        return False
    own_conn = conn is None
    if own_conn:
        conn = _get_conn()
    c = conn.cursor()
    try:
        _ensure_decisions_table(c)
        row = c.execute("SELECT 1 FROM decisions WHERE signal_id=? LIMIT 1",
                        (str(signal_id),)).fetchone()
    finally:
        if own_conn:
            conn.close()
    return row is not None


def settle_decisions() -> tuple:
    """Salda le decisioni aperte coi risultati reali (idempotente).

    Chiude OGNI riga con un risultato in `match_results`, anche i `reject` e
    le `review`: senza l'esito dei segnali scartati non si puo' misurare se il
    gate aveva ragione (metrica "shadow" di `decision_stats`).

    `profit` e' il P/L PER UNITA' di stake (stessa semantica di predictions):
    il peso monetario lo da' la colonna `stake` (0 sui segnali non giocati).
    Sanity check e pausa settlement: identici agli altri ledger.

    Ritorna (saldate, push).
    """
    if settlement_paused():
        logger.info("settle_decisions: settlement in PAUSA, nessuna riga chiusa")
        return 0, 0
    conn = _get_conn()
    _create_results_table(conn)
    c = conn.cursor()
    try:
        open_rows = c.execute("SELECT id, match_id, market, outcome, price "
                              "FROM decisions WHERE esito_finale IS NULL").fetchall()
        results = c.execute("SELECT match_id, home_team, away_team, score_home, score_away "
                            "FROM match_results").fetchall()
    finally:
        conn.close()
    res_map = {r[0]: r[1:] for r in results}

    conn = _get_conn(); c = conn.cursor()
    now = datetime.now().isoformat()
    settled = pushes = blocked = 0
    for did, match_id, market, outcome_sel, price in open_rows:
        r = res_map.get(match_id)
        if not r:
            continue
        home, away, sh, sa = r
        if not _goals_sane(sh, sa):
            logger.warning("settle_decisions: gol non validi (%r-%r) su match %s: "
                           "settlement BLOCCATO (decisione #%d)", sh, sa, match_id, did)
            blocked += 1
            continue
        outcome, unit_profit = _prediction_outcome(market or "1X2", outcome_sel,
                                                   price or 0.0, sh, sa, home, away)
        if outcome is None:
            continue
        if outcome == "won" and _esito_possible(market or "1X2", outcome_sel, sh, sa,
                                                home, away) is False:
            logger.warning("settle_decisions: esito '%s' VINTO in contraddizione "
                           "coi gol %s-%s (%s vs %s): settlement BLOCCATO "
                           "(decisione #%d)", outcome_sel, sh, sa, home, away, did)
            blocked += 1
            continue
        if outcome == "push":
            pushes += 1
        c.execute("UPDATE decisions SET esito_finale=?, profit=?, settled_at=? "
                  "WHERE id=?", (outcome, unit_profit, now, did))
        settled += 1
    conn.commit(); conn.close()
    if blocked:
        logger.warning("settle_decisions: %d righe BLOCCATE dal sanity check", blocked)
    return settled, pushes


def decision_stats() -> dict:
    """Telemetria del feedback engine sul ledger decisioni.

    Tre letture, in ordine di importanza:

    1. `by_verdict` / `by_reason`: cosa ha deciso la catena e PERCHE' (i
       motivi non sono prosa, sono `ReasonCode` contabili);
    2. `settled`: le decisioni GIOCATE e chiuse — hit rate, ROI flat, ROI
       pesato per stake e `gap` = pnl realizzato - EV atteso (il numero che
       dice se il modello batte davvero il mercato, stessa convenzione di
       `predictions_summary`);
    3. `shadow`: le decisioni NON giocate (review/reject) e chiuse — cosa
       sarebbe successo: e' il costo (o il risparmio) dei gate.
    """
    rows = get_decisions(limit=100000)
    out = {
        "n": len(rows),
        "by_verdict": {}, "by_reason": {}, "by_status": {},
        "executable": 0, "with_order": 0, "stake_total": 0.0,
        "open": 0,
        "settled": _decision_bucket(),
        "shadow": {},
    }
    played, shadow = [], {}
    for row in rows:
        verdict = row.get("verdict") or "?"
        reason = row.get("reason") or "?"
        status = str(row.get("status") or "")
        out["by_verdict"][verdict] = out["by_verdict"].get(verdict, 0) + 1
        out["by_reason"][reason] = out["by_reason"].get(reason, 0) + 1
        # Stato della Shadow Validation: le righe scritte prima del 16/09 non
        # hanno stato (NULL) e restano fuori dal conteggio invece di essere
        # contate come `pending` (non inventiamo uno stato che non c'e').
        if status:
            out["by_status"][status] = out["by_status"].get(status, 0) + 1
        if row.get("order_id"):
            out["with_order"] += 1
        if row.get("esito_finale"):
            if row.get("stake_executable"):
                played.append(row)
            elif verdict != "approve":
                shadow.setdefault(verdict, []).append(row)
        else:
            out["open"] += 1
        if row.get("stake_executable"):
            out["executable"] += 1
            out["stake_total"] = round(out["stake_total"] + (row.get("stake") or 0.0), 2)

    out["settled"] = _decision_bucket(played)
    for verdict, group in shadow.items():
        out["shadow"][verdict] = _decision_bucket(group)
    return out


def _decision_bucket(rows: list = ()) -> dict:
    """Aggregato di un gruppo di decisioni chiuse (giocate o shadow)."""
    bucket = {"n": 0, "won": 0, "lost": 0, "push": 0, "hit_rate": 0.0,
              "pnl_units": 0.0, "roi_flat": 0.0, "stake": 0.0,
              "pnl_staked": 0.0, "roi_staked": 0.0,
              "avg_ev": 0.0, "gap_pp": 0.0}
    if not rows:
        return bucket
    ev_sum = 0.0
    for row in rows:
        bucket["n"] += 1
        outcome = row.get("esito_finale")
        if outcome == "won":
            bucket["won"] += 1
        elif outcome == "lost":
            bucket["lost"] += 1
        else:
            bucket["push"] += 1
        profit = row.get("profit") or 0.0
        stake = row.get("stake") or 0.0
        bucket["pnl_units"] += profit
        bucket["stake"] += stake
        bucket["pnl_staked"] += profit * stake
        ev_sum += row.get("ev") or 0.0
    n = bucket["n"]
    bucket["pnl_units"] = round(bucket["pnl_units"], 4)
    bucket["roi_flat"] = round(bucket["pnl_units"] / n * 100, 2)
    bucket["stake"] = round(bucket["stake"], 2)
    bucket["pnl_staked"] = round(bucket["pnl_staked"], 4)
    bucket["roi_staked"] = (round(bucket["pnl_staked"] / bucket["stake"] * 100, 2)
                             if bucket["stake"] else 0.0)
    bucket["avg_ev"] = round(ev_sum / n * 100, 2)
    bucket["gap_pp"] = round((bucket["pnl_units"] - ev_sum) / n * 100, 2)
    decided = n - bucket["push"]
    bucket["hit_rate"] = round(bucket["won"] / decided * 100, 2) if decided else 0.0
    return bucket


def _ensure_unique_constraints(c) -> None:
    """Garantisce UNIQUE(match_id, mercato, esito) su predictions e
    UNIQUE(match_id, esito) su bets ANCHE sui DB creati prima che i vincoli
    esistessero nel codice (i CREATE TABLE IF NOT EXISTS non migrano le
    tabelle esistenti): su quei DB un re-run del job poteva duplicare le
    righe e sporcare il dataset ML (audit: hash 36aa024f...).

    Idempotente ed economico: due PRAGMA index_list a ogni connessione.
    Se il vincolo manca: dedup NORMALIZZATO -> ricrea la tabella copiando
    le righe deduplicate. I backup (_old) di migrazioni interrotte vengono
    recuperati PRIMA di qualunque check, a ogni avvio.
    """
    migrations = [
        ("predictions", "uq_predictions_sig", "match_id, mercato, esito"),
        ("bets", "uq_bets_sig", "match_id, esito"),
    ]
    for table, idx_name, cols in migrations:
        # 0) Recupero di una migrazione precedente interrotta: SEMPRE prima
        # di qualunque continue, perche' puo' esserci da recuperare anche
        # quando la tabella nuova esiste gia' (o non esiste proprio).
        #   (a) tabella nuova presente: copia le righe mancanti da _old
        #   (b) tabella nuova ASSENTE (crash tra rename e create): ricreala
        try:
            leftover = c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (f"{table}_old",)).fetchone()
        except sqlite3.OperationalError:
            leftover = None
        if leftover:
            try:
                cur_cols = [r[1] for r in c.execute(
                    f"PRAGMA table_info({table})").fetchall()]
                bk_cols = [r[1] for r in c.execute(
                    f"PRAGMA table_info({table}_old)").fetchall()]
                if not cur_cols and bk_cols:
                    _create_ledger_table(c, table)
                    cur_cols = [r[1] for r in c.execute(
                        f"PRAGMA table_info({table})").fetchall()]
                if bk_cols and bk_cols == cur_cols:
                    bkl = ", ".join(bk_cols)
                    c.execute(f"INSERT OR IGNORE INTO {table} ({bkl}) "
                              f"SELECT {bkl} FROM {table}_old")
                    c.execute(f"DROP TABLE {table}_old")
                    logger.warning("tracker: recuperate righe da %s_old "
                                   "(migrazione precedente interrotta)", table)
            except sqlite3.OperationalError as e:
                logger.warning("tracker: recupero %s_old rimandato: %s", table, e)

        # 1) Il vincolo esiste gia'? (nome nostro o autoindex dello schema)
        try:
            idx = [r[1] for r in c.execute(f"PRAGMA index_list({table})").fetchall()]
        except sqlite3.OperationalError:
            continue
        if idx_name in idx:
            continue
        if any(i and i.startswith("sqlite_autoindex") for i in idx):
            continue

        # 2) Vincolo davvero assente: migrazione (mai perdere dati)
        try:
            table_cols = [r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()]
            if not table_cols:
                continue
            col_list = ", ".join(table_cols)
            if table == "predictions":
                removed = _dedupe_normalized_esito(c)
                if removed:
                    logger.warning("predictions: eliminate %d righe duplicate "
                                   "(chiave normalizzata)", removed)
            c.execute(f"ALTER TABLE {table} RENAME TO {table}_old")
            _create_ledger_table(c, table)
            c.execute(f"INSERT OR IGNORE INTO {table} ({col_list}) "
                      f"SELECT {col_list} FROM {table}_old")
            c.execute(f"DROP TABLE {table}_old")
            logger.info("tracker: applicato UNIQUE(%s) su %s (migrazione schema)",
                        cols, table)
        except sqlite3.OperationalError as e:
            # Tipicamente DB lockato da un'altra connessione. MAI buttare il
            # backup: le righe restano in _old e vengono recuperate al prossimo
            # avvio (blocco 0 in cima a questo loop).
            logger.warning("tracker: migrazione UNIQUE su %s rimandata: %s "
                           "(backup dati in %s_old, recupero al prossimo avvio)",
                           table, e, table)


# --- Cassa (registro scommesse inserite dal sito) ---
def save_cassa_entry(partita, esito, quota, importo, ev=0.0, data=None,
                     esito_finale=None, profit=None, settled_at=None):
    """Registra una scommessa nella cassa."""
    conn = _get_conn(); c = conn.cursor()
    c.execute('''INSERT INTO cassa (data, partita, esito, quota, importo, ev, timestamp,
                                    esito_finale, profit, settled_at)
                 VALUES (?,?,?,?,?,?,?,?,?,?)''',
              (data or datetime.now().strftime("%Y-%m-%d"), partita, esito,
               float(quota), float(importo), float(ev), datetime.now().isoformat(),
               esito_finale, profit, settled_at))
    conn.commit(); conn.close()


def get_cassa():
    """Tutte le scommesse della cassa, dalla piu' recente (compreso il saldo)."""
    conn = _get_conn(); c = conn.cursor()
    c.execute("SELECT id, data, partita, esito, quota, importo, ev, timestamp,"
              " esito_finale, profit, settled_at FROM cassa ORDER BY id DESC")
    rows = c.fetchall(); conn.close()
    return [
        {"id": r[0], "data": r[1], "partita": r[2], "esito": r[3],
         "quota": r[4], "importo": r[5], "ev": r[6], "timestamp": r[7],
         "esito_finale": r[8], "profit": r[9], "settled_at": r[10]}
        for r in rows
    ]


def _norm_team(name):
    """Normalizza un nome squadra per il match: minuscole, no accenti, no 'fc/cf'."""
    import unicodedata
    s = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode("ascii")
    s = s.lower()
    return " ".join(w for w in s.split() if w not in ("fc", "cf"))


#: Linea di default di un esito Over/Under senza numero esplicito: le righe
#: OU saldate prima del multi-mercato (19/09) erano tutte Over/Under 2.5.
OU_DEFAULT_LINE = 2.5


def ou_line(esito, default: float = OU_DEFAULT_LINE) -> float:
    """Linea di un esito Over/Under letta dal testo ('Over 3.5' -> 3.5).

    Il formato del ledger e' 'Over 2.5' / 'Under 3.25' (vedi
    `multi_market.ledger_esito`): si prende l'ULTIMO token numerico. Senza
    numero si ricade su `OU_DEFAULT_LINE` (comportamento storico).
    """
    for token in reversed(str(esito or "").split()):
        try:
            return float(token.replace("+", ""))
        except (TypeError, ValueError):
            continue
    try:
        return float(default)
    except (TypeError, ValueError):
        return OU_DEFAULT_LINE


def ou_won(side: str, total: float, line: float):
    """(won, push) di un Over/Under con la sua linea.

    Push solo sulle linee INTERE (total == line): li' la puntata e'
    restituita e il P/L e' 0, non una perdita. Sulle linee .5 non esiste.
    """
    if total > line:
        return side == "over", False
    if total < line:
        return side == "under", False
    return None, True


def _esito_won(esito, sh, sa):
    """True/False se l'esito e' vincente col risultato (sh, sa); None se non riconosciuto.

    ATTENZIONE (fix 09/09): il match OU deve scattare SOLO per esiti che
    INIZIANO con 'over'/'under' come parola ('Over 2.5'), mai come
    sottostringa: 'Blackburn Rovers' contiene 'over' dentro 'Rovers' e
    veniva saldato come Over 2.5 (won con gol 1+2=3) invece che come
    sconfitta della squadra (bug pred #98).

    Multi-mercato (19/09): la linea si legge dall'esito ('Over 3.5'), quindi
    vale per QUALUNQUE linea e non solo per il 2.5.
    """
    el = str(esito or "").lower().strip()
    first = (el.split() or [""])[0]
    if first in ("over", "under"):
        won, push = ou_won(first, sh + sa, ou_line(el))
        return False if push else won
    if "btts" in el or "gol gol" in el:
        return sh > 0 and sa > 0
    if el == "1" or "casa" in el:
        return sh > sa
    if el == "2" or "trasferta" in el:
        return sa > sh
    if el == "x" or "pareggio" in el:
        return sh == sa
    return None


# Prefissi club comuni ignorati nel match per nome della cassa
# (_norm_team elimina solo fc/cf: 'CA Osasuna' non agganciava 'Osasuna'
# → la cassa finiva saldata su una partita VECCHIA della stessa coppia).
_CLUB_PREFIXES = ("ca ", "ac ", "as ", "cd ", "fc ", "cf ", "de ",
                   "ss ", "sc ", "us ", "ud ", "sd ", "at ", "sv ")


def _loose_team(name):
    """Normalizzazione TOLERANTE per il match cassa: _norm_team + rimozione
    dei prefissi club comuni (es. 'CA Osasuna' → 'osasuna')."""
    t = _norm_team(name)
    for _ in range(3):
        changed = False
        for p in _CLUB_PREFIXES:
            if t.startswith(p):
                t = t[len(p):].strip()
                changed = True
        if not changed:
            break
    return t or _norm_team(name)


def _goals_sane(sh, sa, result=None) -> bool:
    """True se (sh, sa) è un punteggio finale plausibile (int non negativi).

    Se `result` è dato ('1'/'X'/'2'), verifica anche che sia coerente coi
    gol: una riga con result='1' ma sh < sa è un dato CORROTTO su cui il
    settlement non deve chiudere nulla.
    """
    try:
        sh_i, sa_i = int(sh), int(sa)
    except (TypeError, ValueError):
        return False
    if sh_i < 0 or sa_i < 0:
        return False
    if result is not None:
        expected = "1" if sh_i > sa_i else ("2" if sh_i < sa_i else "X")
        if str(result).strip().upper() != expected:
            return False
    return True


def _esito_possible(mercato, esito, sh, sa, home=None, away=None):
    """Tripwire anti-contraddizione: l'esito PUÒ essere vinto coi gol (sh, sa)?

    Ritorna True/False, oppure None per i mercati non verificabili qui
    (Asian Handicap, esiti sconosciuti). Il settlement deve BLOCCARE la
    chiusura di una scommessa quando il verdetto calcolato è 'won' ma i gol
    rendono l'esito impossibile (es. esito '2' con vittoria casa): è il
    segnale di dati corrotti o di una regressione nel calcolo dell'esito.
    """
    m = str(mercato or "").upper()
    el = str(esito or "").lower().strip()
    if m == "AH":
        return None
    home_n = _norm_team(home) if home else ""
    away_n = _norm_team(away) if away else ""
    el_n = _norm_team(el)
    if m in ("1X2",) or el in ("1", "2", "x", "draw", "pareggio") or \
            (home and el_n == home_n) or (away and el_n == away_n):
        if el == "1" or (home and el_n == home_n):
            return sh > sa
        if el == "2" or (away and el_n == away_n):
            return sa > sh
        if el in ("x", "draw", "pareggio"):
            return sh == sa
        return None
    # match OU solo per esiti che INIZIANO con over/under (parola intera):
    # 'Blackburn Rovers' contiene 'over' (in 'Rovers') ma e' un 1X2.
    first = (el.split() or [""])[0]
    if first in ("over", "under"):
        won, push = ou_won(first, sh + sa, ou_line(el))
        # Push (linea intera con total == linea): il verdetto 'push' non
        # contraddice i gol, quindi l'esito non e' impossibile.
        return True if push else won
    if "btts" in el or "gol gol" in el:
        return sh > 0 and sa > 0
    return None


# Finestra temporale della GUARDIA CASSA (11/09/2026): un risultato viene
# agganciato a una scommessa della cassa solo se la sua data e' entro N
# giorni dalla data della bet. Senza la finestra, una coppia di squadre
# ripetuta (stessa partita dell'anno prima, andata/ritorno) poteva essere
# saldata col risultato SBAGLIATO. Env `CASSA_MATCH_WINDOW_DAYS`.
def _cassa_window_days() -> float:
    try:
        return max(0.0, float(os.getenv("CASSA_MATCH_WINDOW_DAYS", "14")))
    except (TypeError, ValueError):
        return 14.0


def _cassa_anchor(data, timestamp):
    """Data di riferimento di una bet: `data` (giorno partita, preferito)
    oppure il `timestamp` di inserimento. None se nessuno dei due e' ISO."""
    for v in (data, timestamp):
        dt = _parse_ts_utc(v)
        if dt is not None:
            return dt
    return None


def _pick_cassa_result(cands, anchor, window_days):
    """Sceglie il risultato da usare per una bet della cassa.

    Guardia (11/09): tra i candidati con la stessa coppia di squadre si
    prende quello temporalmente PIU' VICINO alla data della bet, ma solo se
    entro `window_days`. Se i candidati sono tutti piu' vecchi della
    finestra, NON si salda (meglio lasciare in gioco che pagare il
    risultato sbagliato). Fallback storico (il piu' recente) solo quando
    non c'e' alcuna data utilizzabile.
    """
    if not cands:
        return None
    timed, untimed = [], []
    for sh, sa, ts in cands:
        dt = _parse_ts_utc(ts)
        (timed if dt is not None else untimed).append((sh, sa, dt, ts))
    if anchor is None:
        if timed:
            best = max(timed, key=lambda c: c[2])
        else:
            best = untimed[-1]
        return best[0], best[1]
    best, best_delta = None, None
    for sh, sa, dt, _ts in timed:
        delta = abs((dt - anchor).total_seconds())
        if delta > window_days * 86400:
            continue
        if best_delta is None or delta < best_delta:
            best, best_delta = (sh, sa), delta
    if best is not None:
        return best
    # Nessun risultato datato dentro la finestra: se esistono candidati
    # datati, la guardia blocca la chiusura. Solo se SONO TUTTI senza data
    # si torna al fallback storico (impossibile blindare).
    if not timed and untimed:
        return untimed[-1][0], untimed[-1][1]
    return None


def settle_cassa():
    """Salda le scommesse aperte della cassa con i risultati reali (match_results).

    Idempotente: tocca solo le righe con esito_finale NULL. Il match avviene
    per coppia di squadre normalizzate; le scommesse senza risultato ancora
    disponibile restano in 'in gioco'. Ritorna il numero saldate in questa
    chiamata.

    PAUSA SETTLEMENT (11/09): con la pausa attiva non chiude nulla (ritorna 0).

    GUARDIA CASSA (11/09): il risultato viene scelto per vicinanza temporale
    alla data della bet e solo entro `CASSA_MATCH_WINDOW_DAYS` (default 14):
    coppie di squadre ripetute (2015 vs 2026, andata/ritorno) non pescano
    piu' un risultato sballato.

    SANITY CHECK: se i gol registrati sono corrotti (punteggio negativo/
    non numerico) o l'esito calcolato è in contraddizione EVIDENTE coi gol
    (es. esito '2' vinto con vittoria casa), la riga NON viene chiusa: resta
    in gioco e viene loggata, per non pagare un verdetto su dati falsi.
    """
    if settlement_paused():
        logger.info("settle_cassa: settlement in PAUSA, nessuna riga chiusa")
        return 0
    conn = _get_conn()
    _create_results_table(conn)
    c = conn.cursor()
    try:
        rows = c.execute("SELECT id, partita, esito, quota, importo, data, "
                         "timestamp FROM cassa "
                         "WHERE esito_finale IS NULL").fetchall()
        results = c.execute("SELECT home_team, away_team, score_home, score_away, "
                            "settled_at FROM match_results").fetchall()
    finally:
        conn.close()
    # Candidati per coppia normalizzata: TUTTI i risultati (non solo il piu'
    # recente) — la scelta avviene per vicinanza alla data della bet.
    # Doppia chiave (stretta + loose): 'CA Osasuna' e 'Osasuna' devono
    # agganciare lo stesso match.
    cand_map = {}

    def _add(key, sh, sa, ts):
        if not key[0] or not key[1]:
            return
        cand_map.setdefault(key, []).append((sh, sa, ts or ""))

    for home, away, sh, sa, settled_at in results:
        _add((_norm_team(home), _norm_team(away)), sh, sa, settled_at)
        _add((_loose_team(home), _loose_team(away)), sh, sa, settled_at)

    window = _cassa_window_days()
    conn = _get_conn(); c = conn.cursor()
    now = datetime.now().isoformat()
    settled = blocked = out_window = 0
    for cid, partita, esito, quota, importo, data_row, ts_row in rows:
        # "Serie A – Roma vs Empoli" -> ultimo " vs " separa casa/trasferta
        clean = partita.split(" – ")[-1].strip() if " – " in partita else partita
        if " vs " not in clean:
            continue
        parts = clean.split(" vs ")
        home, away = _loose_team(parts[0].strip()), _loose_team(parts[1].strip())
        cands = cand_map.get((home, away))
        if not cands:
            continue
        anchor = _cassa_anchor(data_row, ts_row)
        match = _pick_cassa_result(cands, anchor, window)
        if match is None:
            out_window += 1
            continue
        sh, sa = match[0], match[1]
        if not _goals_sane(sh, sa):
            logger.warning("settle_cassa: gol non validi (%r-%r) su %s: "
                           "settlement BLOCCATO (cassa #%d)", sh, sa, partita, cid)
            blocked += 1
            continue
        won = _esito_won(esito, sh, sa)
        if won is None:
            continue
        if won and _esito_possible(None, esito, sh, sa) is False:
            logger.warning("settle_cassa: esito '%s' VINTO in contraddizione coi "
                           "gol %s-%s (%s): settlement BLOCCATO (cassa #%d)",
                           esito, sh, sa, partita, cid)
            blocked += 1
            continue
        profit = round((quota - 1) * importo, 2) if won else round(-importo, 2)
        c.execute("UPDATE cassa SET esito_finale=?, profit=?, settled_at=? WHERE id=?",
                  ("won" if won else "lost", profit, now, cid))
        settled += 1
    conn.commit(); conn.close()
    if blocked:
        logger.warning("settle_cassa: %d righe BLOCCATE dal sanity check", blocked)
    if out_window:
        logger.info("settle_cassa: %d righe con risultati fuori dalla finestra "
                    "temporale (%.0f gg) — restano in gioco", out_window, window)
    return settled


def cassa_totals(entries=None):
    """Totali cassa: speso, potenziale delle in gioco, P/L realizzato."""
    entries = entries if entries is not None else get_cassa()
    speso = sum(e["importo"] or 0 for e in entries)
    vincita = sum((e["importo"] or 0) * (e["quota"] or 0) for e in entries)
    closed = [e for e in entries if e.get("esito_finale")]
    in_gioco = [e for e in entries if not e.get("esito_finale")]
    vinti = sum(1 for e in closed if e["esito_finale"] == "won")
    persi = sum(1 for e in closed if e["esito_finale"] == "lost")
    profit_real = sum(e.get("profit") or 0 for e in closed)
    speso_chiuso = sum(e["importo"] or 0 for e in closed)
    roi = (profit_real / speso_chiuso * 100) if speso_chiuso else 0.0
    return {"n": len(entries), "totale_speso": round(speso, 2),
            "vincita_potenziale": round(vincita, 2),
            "profit_potenziale": round(vincita - speso, 2),
            "chiusi": len(closed), "in_gioco": len(in_gioco),
            "vinti": vinti, "persi": persi,
            "speso_chiuso": round(speso_chiuso, 2),
            "profit_realizzato": round(profit_real, 2),
            "roi": round(roi, 2)}


# --- Ledger previsioni (segnale proposto -> esito reale -> calibrazione) ---
# Ogni segnale che il motore propone (qualunque mercato: 1X2, Over/Under,
# BTTS, Asian Handicap) viene registrato qui con il suo mercato, cosi' a
# fine partita si puo' verificare torto/ragione per mercato e correggere
# il modello dove sbaglia.

def save_prediction(match_id, mercato, esito, quota, prob, ev,
                    market_prob=None, market_edge=None, status="value",
                    league=None):
    """Registra (o aggiorna, se ancora non chiusa) una previsione del motore.

    Idempotente per (match_id, mercato, esito): a ogni nuova analisi la
    previsione non ancora saldata viene aggiornata con i prezzi correnti;
    quella gia' saldata non viene toccata (per non falsare il record).

    `league` = campionato del segnale (22/09). Prima la lega si ricavava solo
    dalla JOIN con `matches`, che non copre le righe senza partita: il 65% del
    ledger risultava non attribuibile a una lega e la strategia per lega non
    era misurabile. Sulla riga del segnale la lega sopravvive anche se la
    partita viene riscritta o cancellata.

    In UPDATE la lega usa COALESCE: una chiamata che non la passa (o la passa
    vuota) NON cancella il valore gia' registrato.
    """
    now = datetime.now().isoformat()
    conn = _get_conn(); c = conn.cursor()
    c.execute('''INSERT INTO predictions (match_id, mercato, esito, quota, prob, ev,
                                          market_prob, market_edge, status, league,
                                          created_at)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?)
                 ON CONFLICT(match_id, mercato, esito) DO UPDATE SET
                   quota=excluded.quota, prob=excluded.prob, ev=excluded.ev,
                   market_prob=excluded.market_prob, market_edge=excluded.market_edge,
                   status=excluded.status,
                   league=COALESCE(NULLIF(excluded.league, ''), predictions.league),
                   created_at=excluded.created_at
                 WHERE esito_finale IS NULL''',
              (match_id, mercato, esito, float(quota), float(prob), float(ev),
               market_prob, market_edge, status, league, now))
    conn.commit(); conn.close()


def _date_key(value) -> str:
    """Chiave di data CONFRONTABILE per le date del ledger.

    Le date del ledger sono ISO con la 'T' (a volte con 'Z' o con l'offset)
    mentre SQLite produce date con lo SPAZIO: confrontarle direttamente e' la
    classe di bug del 17/09/2026 (la scadenza delle righe slittava di un
    giorno perche' 'T' > ' ' in una comparazione fra stringhe). Qui la
    normalizzazione e' in PYTHON, cosi' ogni confronto fra date passa da un
    solo posto e non dipende dal formato della fonte.
    """
    s = str(value or "").strip()
    if not s:
        return ""
    s = s.replace("T", " ")
    if s.endswith("Z"):
        s = s[:-1]
    if len(s) > 19 and s[19] in "+-":        # offset '+02:00' / '-0300'
        s = s[:19]
    return s[:19]


def _row_era_key(row) -> str:
    """Data che definisce l'ERA di un segnale: quando e' NATO (`created_at`).

    `created_at` e' il momento in cui la strategia ha prodotto la riga, ed e'
    la grandezza giusta per separare due strategie diverse (non `settled_at`,
    che dipende da quando e' arrivato il risultato). Solo se manca si ripiega
    su `settled_at`: una riga senza data di nascita non deve sparire.
    """
    try:
        return _date_key(row.get("created_at") or row.get("settled_at"))
    except Exception:
        return ""


def filter_predictions(rows, *, created_since=None, odds_min=None,
                       odds_max=None):
    """Filtro CONDIVISO del ledger previsioni: era strategica + fascia quota.

    UNA sola definizione per tutti i consumatori (report shadow multi-mercato,
    diagnosi per mercato, CLI): due implementazioni diverse della stessa
    "era" sono il modo silenzioso di ottenere due misure che non coincidono
    — ed e' esattamente la trappola che il 25/09/2026 ha reso invisibile il
    vero ROI dell'Over/Under (il +21.21% era portato per intero da una
    pipeline ritirata: 22 righe a quota media ~2.25).

    `created_since` (ISO, es. "2026-09-19"): tiene solo i segnali NATI da
    quella data in poi.
    `odds_min`/`odds_max`: fascia di quota.

    FAIL-CLOSED sul dato mancante: con un filtro attivo una riga senza data
    (o con quota non leggibile) viene ESCLUSA — non si puo' dimostrare che
    appartenga alla popolazione richiesta — e il chiamante la dichiara fra
    le escluse, cosi' non sparisce in silenzio.
    """
    out = list(rows or [])
    if created_since:
        cutoff = _date_key(created_since)
        if cutoff:
            out = [r for r in out if _row_era_key(r) >= cutoff]

    def _quota(row):
        try:
            return float(row.get("quota"))
        except (TypeError, ValueError, AttributeError):
            return None

    if odds_min is not None:
        out = [r for r in out
               if _quota(r) is not None and _quota(r) >= float(odds_min)]
    if odds_max is not None:
        out = [r for r in out
               if _quota(r) is not None and _quota(r) <= float(odds_max)]
    return out


def get_predictions(mercato=None, status=None, closed=None, limit=500, *,
                    created_since=None, odds_min=None, odds_max=None):
    """Righe del ledger previsioni (filtri opzionali di era e fascia quota).

    I filtri sono applicati da `filter_predictions` (definizione unica):
    vedi la sua docstring per la semantica del fail-closed sul dato mancante.
    """
    conn = _get_conn(); c = conn.cursor()
    q = "SELECT match_id, mercato, esito, quota, prob, ev, market_prob, market_edge, " \
        "status, esito_finale, profit, created_at, settled_at, league FROM predictions"
    conds, args = [], []
    if mercato:
        conds.append("mercato=?"); args.append(mercato)
    if status:
        conds.append("status=?"); args.append(status)
    if closed is True:
        conds.append("esito_finale IS NOT NULL")
    elif closed is False:
        conds.append("esito_finale IS NULL")
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY id DESC LIMIT ?"; args.append(limit)
    rows = c.execute(q, args).fetchall()
    conn.close()
    out = [
        {"match_id": r[0], "mercato": r[1], "esito": r[2], "quota": r[3],
         "prob": r[4], "ev": r[5], "market_prob": r[6], "market_edge": r[7],
         "status": r[8], "esito_finale": r[9], "profit": r[10],
         "created_at": r[11], "settled_at": r[12], "league": r[13]}
        for r in rows
    ]
    return filter_predictions(out, created_since=created_since,
                              odds_min=odds_min, odds_max=odds_max)


def _ah_halves(line: float):
    """Linea AH quarter -> (due mezze linee, share 0.5); altrimenti (linea, 1.0)."""
    if abs(line * 2) % 1 != 0:  # .25 / .75 -> due mezze puntate
        low = math.floor(line * 2) / 2
        return (low, low + 0.5), 0.5
    return (line,), 1.0


def ah_pnl_units(esito: str, quota: float, adv: int) -> float:
    """P/L (in unita' da 1) di una scommessa AH flat, con split-bet per le quarter.

    esito nel formato "Home -0.75" / "Away +0.25": side + linea del lato.
    Linee intere/mezze = puntata piena; quarter (.25/.75) = due mezze.
    """
    parts = str(esito).split()
    if len(parts) < 2:
        return None
    side_raw = parts[0].lower()
    if side_raw.startswith("home"):
        side = "home"
    elif side_raw.startswith("away") or side_raw.startswith("guest"):
        side = "away"
    else:
        return None
    try:
        line = float(parts[1].replace("+", ""))
    except ValueError:
        return None
    halves, share = _ah_halves(line)
    total = 0.0
    for hline in halves:
        net = (adv + hline) if side == "home" else (-adv + hline)
        if net > 0:
            total += share * (quota - 1)
        elif net < 0:
            total -= share
    return total


def _prediction_outcome(mercato, esito, quota, sh, sa, home, away):
    """Risolve l'esito di una previsione. Ritorna (outcome, profit) o (None, None).

    outcome: 'won' | 'lost' | 'push'. Per l'Asian Handicap il profitto usa
    la logica split-bet (quarter lines = due mezze puntate).
    """
    m = str(mercato or "").upper()
    el = str(esito or "").lower().strip()
    if m == "AH" or any(t in el.split()[:1] for t in ("home", "away")):
        pnl = ah_pnl_units(esito, quota, sh - sa)
        if pnl is None:
            return None, None
        if pnl > 0:
            return "won", round(pnl, 4)
        if pnl == 0:
            return "push", 0.0
        return "lost", round(pnl, 4)
    # match OU SOLO se l'esito inizia con over/under (parola intera): con la
    # sottostringa 'over' in 'blackburn rovers' la pred 1X2 veniva saldata
    # come Over 2.5 (bug 09/09, bloccato dal sanity check su pred #98).
    first = (el.split() or [""])[0]
    if first in ("over", "under"):
        line = ou_line(el)
        won, push = ou_won(first, sh + sa, line)
        if push:
            return "push", 0.0
    elif "btts" in el or "gol gol" in el:
        won = (sh > 0 and sa > 0)
    elif el in ("draw", "pareggio", "x"):
        won = sh == sa
    elif el == "1":
        won = sh > sa
    elif el == "2":
        won = sa > sh
    else:
        # esito = nome squadra (es. "Osasuna")
        hn = _norm_team(home); an = _norm_team(away); en = _norm_team(el)
        if en == hn:
            won = sh > sa
        elif en == an:
            won = sa > sh
        else:
            return None, None
    return ("won" if won else "lost"), round((quota - 1) if won else -1.0, 4)


# SCADENZA RIGHE SX SENZA RISULTATO (12/09/2026): le righe sx-* aperte e
# fuori dalla finestra di refertazione esterna (the-odds-api copre al massimo
# 3 giorni) facevano ripartire fetch_scores a ogni giro per SEMPRE senza
# mai trovarle (match orfani del batch 09/09: 6 match -> ~2 crediti/giro
# sprecati). L'esito lo conosce SX, che e' dove il denaro si regula: chiudo
# come PUSH (P/L 0) solo le righe SCADUTE, cioe' senza alcun risultato sx-*
# salvato dalle fonti dopo SX_STALE_DAYS giorni dal kickoff (letta a
# runtime: un override env vale anche senza riavviare il processo). La pausa
# settlement blocca anche la scadenza (nessuna chiusura while in pausa).
SX_STALE_DAYS_DEFAULT = 5.0


def _stale_days() -> float:
    try:
        return float(os.getenv("SX_STALE_DAYS", "") or SX_STALE_DAYS_DEFAULT)
    except (TypeError, ValueError):
        return SX_STALE_DAYS_DEFAULT


def expire_stale_sx_rows() -> dict:
    """Chiude come push le righe sx-* scadute (kickoff passato, senza risultato).

    Una riga e' SCADUTA se: match_id LIKE 'sx-%', esito_finale IS NULL,
    esiste una partita in `matches` con commence_time piu' vecchio di
    SX_STALE_DAYS giorni, e NON esiste nessuna riga in match_results per
    quel match_id (se c'e' il risultato le fonti hanno gia' parlato: il
    settle normale la chiudera' col verdetto vero).

    RAMO ORFANI (12/09): le righe SENZA riga in `matches` (batch 09/09:
    niente nomi squadra, nessuna fonte puo' mai abbinarle) usano `created_at`
    come riferimento temporale — senza questo ramo resterebbero aperte per
    sempre E continuerebbero a finire in `missing` nel settlement (fetch_scores
    a credito sprecato).
    Dal 13/09 il ramo orfani NON e' piu' limitato a `sx-%`: senza riga in
    `matches` non esistono ne' kickoff ne' nomi squadra, quindi la riga e'
    insaldabile per COSTRUZIONE con qualsiasi prefisso (es. una previsione
    OU del 01/09 rimasta senza partita). Le righe CON riga in `matches`
    restano invece intatte: sono refertabili e le chiude il settle vero.

    Ritorna {bets: n, predictions: m} (righe appena chiuse, 0 se in pausa).
    """
    if settlement_paused():
        return {"bets": 0, "predictions": 0}
    conn = _get_conn()
    _create_results_table(conn)
    c = conn.cursor()
    try:
        stale_days = _stale_days()
        cutoff = f"-{stale_days} days"
        # ⚠️ datetime(col) NON e' cosmetico (fix 17/09/2026): le date del
        # ledger sono salvate in formato ISO con 'T' ('2026-09-12T07:11:39')
        # e a volte con 'Z', mentre `datetime('now', ?)` produce il formato
        # SQLite con lo SPAZIO ('2026-09-12 10:48:40'). Nel confronto fra
        # STRINGHE 'T' (0x54) > ' ' (0x20), quindi a parita' di giorno la
        # riga risultava piu' NUOVA del cutoff e la scadenza arrivava con
        # ~1 giorno di ritardo (misurato sul container: `overdue_orphans` 2
        # invece di 0). `datetime(...)` normalizza 'T', 'Z' e l'offset.
        stale = {r[0] for r in c.execute(
            "SELECT m.id FROM matches m "
            "WHERE m.id LIKE 'sx-%' "
            "AND datetime(m.commence_time) < datetime('now', ?) "
            "AND NOT EXISTS (SELECT 1 FROM match_results r WHERE r.match_id = m.id)",
            (cutoff,)).fetchall()}
        # Ramo orfani: nessuna riga in `matches` -> il kickoff non esiste,
        # usa la data di creazione della riga (bets + predictions, UNION).
        # Nessun filtro sul prefisso: senza partita in `matches` la riga e'
        # insaldabile con QUALSIASI fonte (i rami per nome/lega partono da
        # `_sx_open_matches`, che fa JOIN su `matches`).
        stale |= {r[0] for r in c.execute(
            "SELECT DISTINCT match_id FROM bets "
            "WHERE esito_finale IS NULL "
            "AND datetime(created_at) < datetime('now', ?) "
            "AND match_id NOT IN (SELECT id FROM matches) "
            "AND NOT EXISTS (SELECT 1 FROM match_results r WHERE r.match_id = bets.match_id)",
            (cutoff,)).fetchall()}
        stale |= {r[0] for r in c.execute(
            "SELECT DISTINCT match_id FROM predictions "
            "WHERE esito_finale IS NULL "
            "AND datetime(created_at) < datetime('now', ?) "
            "AND match_id NOT IN (SELECT id FROM matches) "
            "AND NOT EXISTS (SELECT 1 FROM match_results r WHERE r.match_id = predictions.match_id)",
            (cutoff,)).fetchall()}
        now = datetime.now().isoformat()
        nb = np_ = 0
        if stale:
            qmarks = ",".join("?" for _ in stale)
            ids = tuple(stale)
            cur = c.execute(
                f"UPDATE bets SET esito_finale='push', profit=0.0, "
                f"settled_at=? WHERE esito_finale IS NULL AND match_id IN ({qmarks})",
                (now, *ids))
            nb = cur.rowcount
            cur = c.execute(
                f"UPDATE predictions SET esito_finale='push', profit=0.0, "
                f"settled_at=? WHERE esito_finale IS NULL AND match_id IN ({qmarks})",
                (now, *ids))
            np_ = cur.rowcount
            if nb or np_:
                conn.commit()
                logger.warning(
                    "expire_stale_sx_rows: %d bet e %d previsioni "
                    "scadute (> %.0fgg senza risultato: partita passata o "
                    "assente) chiuse come push — insaldabili, smettono di "
                    "generare fetch_scores",
                    nb, np_, stale_days)
        return {"bets": nb, "predictions": np_}
    finally:
        conn.close()


def settle_predictions():
    """Salda le previsioni aperte coi risultati reali (idempotente).

    Ritorna (saldate, push): numero di previsioni chiuse in questa chiamata
    e quante di queste si sono concluse in push (es. handicap pari).

    PAUSA SETTLEMENT (11/09): con la pausa attiva ritorna (0, 0).
    """
    if settlement_paused():
        logger.info("settle_predictions: settlement in PAUSA, nessuna riga chiusa")
        return 0, 0
    conn = _get_conn()
    _create_results_table(conn)
    c = conn.cursor()
    try:
        open_rows = c.execute("SELECT id, match_id, mercato, esito, quota FROM predictions "
                              "WHERE esito_finale IS NULL").fetchall()
        results = c.execute("SELECT match_id, home_team, away_team, score_home, score_away "
                            "FROM match_results").fetchall()
    finally:
        conn.close()
    res_map = {r[0]: r[1:] for r in results}

    conn = _get_conn(); c = conn.cursor()
    now = datetime.now().isoformat()
    settled = pushes = blocked = 0
    for pid, match_id, mercato, esito, quota in open_rows:
        r = res_map.get(match_id)
        if not r:
            continue
        home, away, sh, sa = r
        if not _goals_sane(sh, sa):
            logger.warning("settle_predictions: gol non validi (%r-%r) su "
                           "match %s: settlement BLOCCATO (pred #%d)",
                           sh, sa, match_id, pid)
            blocked += 1
            continue
        outcome, profit = _prediction_outcome(mercato, esito, quota, sh, sa, home, away)
        if outcome is None:
            continue
        if outcome == "won" and _esito_possible(mercato, esito, sh, sa,
                                                home, away) is False:
            logger.warning("settle_predictions: esito '%s' VINTO in "
                           "contraddizione coi gol %s-%s (%s vs %s): "
                           "settlement BLOCCATO (pred #%d)",
                           esito, sh, sa, home, away, pid)
            blocked += 1
            continue
        if outcome == "push":
            pushes += 1
        c.execute("UPDATE predictions SET esito_finale=?, profit=?, settled_at=? WHERE id=?",
                  (outcome, profit, now, pid))
        settled += 1
    conn.commit(); conn.close()
    if blocked:
        logger.warning("settle_predictions: %d righe BLOCCATE dal sanity check", blocked)
    return settled, pushes


def predictions_summary(mercato=None, settled_since=None, statuses=None, *,
                        created_since=None, odds_min=None, odds_max=None):
    """Riepilogo previsioni CHIUSE per mercato: hit, ROI, gap EV, edge mercato.

    E' la telemetria di calibrazione: mostra per ogni mercato se il modello
    batte davvero la closing line (ROI realizzato vs EV atteso).
    Con `settled_since` (ISO, es. "2026-09-01") filtra solo le previsioni
    saldate a partire da quella data (report giornaliero).

    Con `statuses` (es. `value_filter.PLAYABLE_TIERS`) restringe ai soli stati
    indicati: serve a MISURARE la strategia (cio' che sarebbe stato giocato)
    invece di sommare i candidati scartati dai gate, che sono un'altra
    popolazione. Default None = tutti gli stati (comportamento storico).

    `created_since` + `odds_min`/`odds_max` (25/09/2026): filtro di ERA e di
    FASCIA QUOTA, applicato da `filter_predictions` (definizione unica del
    progetto). `created_since` e' la data di NASCITA del segnale, quindi
    distinta da `settled_since` (data di saldo): servono a domande diverse e
    vengono tenute separate di proposito.
    """
    rows = get_predictions(mercato=mercato, closed=True, limit=100000,
                           created_since=created_since,
                           odds_min=odds_min, odds_max=odds_max)
    if settled_since:
        cutoff = _date_key(settled_since)
        rows = [r for r in rows if _date_key(r.get("settled_at")) >= cutoff]
    if statuses is not None:
        wanted = {str(s).strip().lower() for s in statuses}
        rows = [r for r in rows if str(r.get("status") or "").strip().lower() in wanted]
    by_mkt: dict = {}
    for r in rows:
        key = r["mercato"] or "?"
        b = by_mkt.setdefault(key, {"n": 0, "won": 0, "lost": 0, "push": 0,
                                    "pnl": 0.0, "ev_sum": 0.0, "prob_sum": 0.0,
                                    "edge_sum": 0.0, "edge_n": 0})
        b["n"] += 1
        out = r["esito_finale"]
        if out == "won":
            b["won"] += 1
        elif out == "lost":
            b["lost"] += 1
        else:
            b["push"] += 1
        b["pnl"] += r["profit"] or 0.0
        b["ev_sum"] += (r["ev"] or 0.0)
        b["prob_sum"] += (r["prob"] or 0.0)
        if r.get("market_edge") is not None:
            b["edge_sum"] += r["market_edge"]
            b["edge_n"] += 1
    out = {}
    for key, b in by_mkt.items():
        closed = b["n"] - b["push"]
        roi = (b["pnl"] / b["n"] * 100) if b["n"] else 0.0
        out[key] = {
            "n": b["n"], "won": b["won"], "lost": b["lost"], "push": b["push"],
            "hit_rate": ((b["won"] / closed * 100) if closed else 0.0),
            "roi": round(roi, 2),
            "avg_ev": round(((b["ev_sum"] / b["n"]) * 100), 2) if b["n"] else 0.0,
            "gap": round(((b["pnl"] - b["ev_sum"]) / b["n"] * 100), 2) if b["n"] else 0.0,
            "avg_prob": round((b["prob_sum"] / b["n"]), 4) if b["n"] else 0.0,
            "avg_market_edge": round((b["edge_sum"] / b["edge_n"] * 100), 2) if b["edge_n"] else None,
        }
    return out


# --- Puntate automatiche (auto_bet.py) ---
def save_bet(match_id, mercato, esito, market_id=None, selection_id=None,
             price=0.0, stake=0.0, mode="dry-run", status=None, bet_id=None):
    """Registra (o aggiorna, se non chiusa) una puntata automatica.

    UNIQUE(match_id, esito): una sola puntata per esito anche se il job
    viene rilanciato; la puntata gia' saldata non viene toccata.
    """
    now = datetime.now().isoformat()
    conn = _get_conn(); c = conn.cursor()
    c.execute('''INSERT INTO bets (match_id, mercato, esito, market_id, selection_id,
                                    price, stake, mode, status, bet_id, created_at)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?)
                 ON CONFLICT(match_id, esito) DO UPDATE SET
                   market_id=excluded.market_id, selection_id=excluded.selection_id,
                   price=excluded.price, stake=excluded.stake, mode=excluded.mode,
                   status=excluded.status, bet_id=excluded.bet_id
                 WHERE esito_finale IS NULL''',
              (match_id, mercato, esito, market_id, selection_id,
               float(price), float(stake), mode, status, bet_id, now))
    conn.commit(); conn.close()


def bet_exists_open(match_id, esito):
    """True se esiste gia' una puntata aperta per (match_id, esito)."""
    conn = _get_conn(); c = conn.cursor()
    r = c.execute("SELECT 1 FROM bets WHERE match_id=? AND esito=? "
                  "AND esito_finale IS NULL", (match_id, esito)).fetchone()
    conn.close()
    return r is not None


def get_bets(day=None, closed=None, limit=200):
    conn = _get_conn(); c = conn.cursor()
    q = "SELECT match_id, mercato, esito, market_id, selection_id, price, stake, " \
        "mode, status, bet_id, esito_finale, profit, created_at, settled_at FROM bets"
    conds, args = [], []
    if day:
        conds.append("created_at LIKE ?"); args.append(f"{day}%")
    if closed is True:
        conds.append("esito_finale IS NOT NULL")
    elif closed is False:
        conds.append("esito_finale IS NULL")
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY id DESC LIMIT ?"; args.append(limit)
    rows = c.execute(q, args).fetchall()
    conn.close()
    return [
        {"match_id": r[0], "mercato": r[1], "esito": r[2], "market_id": r[3],
         "selection_id": r[4], "price": r[5], "stake": r[6], "mode": r[7],
         "status": r[8], "bet_id": r[9], "esito_finale": r[10], "profit": r[11],
         "created_at": r[12], "settled_at": r[13]}
        for r in rows
    ]


def settle_bets(return_details: bool = False):
    """Salda le puntate automatiche aperte coi risultati reali (idempotente).

    Ritorna (saldate, push); con `return_details=True` anche la lista dei
    verdetti appena emessi: {match_id, league, home, away, mercato, esito,
    price, stake, mode, outcome, profit} — serve alle notifiche Telegram.

    PAUSA SETTLEMENT (11/09): con la pausa attiva ritorna (0, 0) — e con
    return_details=True anche la lista vuota.
    """
    if settlement_paused():
        logger.info("settle_bets: settlement in PAUSA, nessuna riga chiusa")
        return (0, 0, []) if return_details else (0, 0)
    conn = _get_conn()
    _create_results_table(conn)
    c = conn.cursor()
    try:
        open_rows = c.execute("SELECT id, match_id, mercato, esito, price, stake, mode "
                              "FROM bets WHERE esito_finale IS NULL").fetchall()
        results = c.execute("SELECT match_id, home_team, away_team, score_home, score_away "
                            "FROM match_results").fetchall()
        leagues = {row[0]: row[1] for row in
                   c.execute("SELECT id, league FROM matches").fetchall()}
    finally:
        conn.close()
    res_map = {r[0]: r[1:] for r in results}

    conn = _get_conn(); c = conn.cursor()
    now = datetime.now().isoformat()
    settled = pushes = blocked = 0
    details = []
    for bid, match_id, mercato, esito, price, stake, mode in open_rows:
        r = res_map.get(match_id)
        if not r:
            continue
        home, away, sh, sa = r
        if not _goals_sane(sh, sa):
            logger.warning("settle_bets: gol non validi (%r-%r) su match %s: "
                           "settlement BLOCCATO (bet #%d)", sh, sa, match_id, bid)
            blocked += 1
            continue
        outcome, _ = _prediction_outcome(mercato, esito, price, sh, sa, home, away)
        if outcome is None:
            continue
        if outcome == "won" and _esito_possible(mercato, esito, sh, sa,
                                                home, away) is False:
            logger.warning("settle_bets: esito '%s' VINTO in contraddizione "
                           "coi gol %s-%s (%s vs %s): settlement BLOCCATO "
                           "(bet #%d)", esito, sh, sa, home, away, bid)
            blocked += 1
            continue
        if outcome == "push":
            profit = 0.0
            pushes += 1
        elif outcome == "won":
            profit = round(stake * (price - 1), 2)
        else:
            profit = round(-stake, 2)
        c.execute("UPDATE bets SET esito_finale=?, profit=?, settled_at=? WHERE id=?",
                  (outcome, profit, now, bid))
        settled += 1
        details.append({
            "match_id": match_id,
            "league": leagues.get(match_id, ""),
            "home": home, "away": away,
            "mercato": mercato, "esito": esito,
            "price": price, "stake": stake, "mode": mode,
            "outcome": outcome, "profit": profit,
        })
    conn.commit(); conn.close()
    if blocked:
        logger.warning("settle_bets: %d righe BLOCCATE dal sanity check", blocked)

    if return_details:
        return settled, pushes, details
    return settled, pushes


def settlement_sanity_check() -> list:
    """Righe GIÀ saldate (bets/predictions) il cui verdetto contraddice i
    gol correnti di match_results.

    Tripwire post-fix (bug 02/09): se match_results viene corretto dopo che
    la bet era stata chiusa (es. il watchdog risalva il punteggio vero con
    match_scores_by_name), il verdetto salvato può restare SPECCHIATO
    (esito '2' marcato won mentre i gol dicono vittoria casa). Questa
    funzione le trova ricomputando l'esito atteso dai gol correnti.

    Ritorna una lista di dict: {table, id, match_id, mercato, esito,
    stored, expected, home, away, sh, sa}. Vuota se tutto coerente.
    """
    conn = _get_conn()
    _create_results_table(conn)
    c = conn.cursor()
    try:
        results = {r[0]: r[1:] for r in c.execute(
            "SELECT match_id, home_team, away_team, score_home, score_away "
            "FROM match_results").fetchall()}
        rows = []
        for bid, match_id, mercato, esito, price, stored in c.execute(
                "SELECT id, match_id, mercato, esito, price, esito_finale "
                "FROM bets WHERE esito_finale IS NOT NULL").fetchall():
            rows.append(("bets", bid, match_id, mercato, esito, price, stored))
        for pid, match_id, mercato, esito, quota, stored in c.execute(
                "SELECT id, match_id, mercato, esito, quota, esito_finale "
                "FROM predictions WHERE esito_finale IS NOT NULL").fetchall():
            rows.append(("predictions", pid, match_id, mercato, esito, quota, stored))
    finally:
        conn.close()

    out = []
    for table, rid, match_id, mercato, esito, price, stored in rows:
        r = results.get(match_id)
        if not r:
            continue
        home, away, sh, sa = r
        if not _goals_sane(sh, sa):
            continue
        expected, _ = _prediction_outcome(mercato, esito, price, sh, sa,
                                          home, away)
        if expected is None or expected == stored:
            continue
        out.append({"table": table, "id": rid, "match_id": match_id,
                    "mercato": mercato, "esito": esito,
                    "stored": stored, "expected": expected,
                    "home": home, "away": away, "sh": sh, "sa": sa})
    return out


def heal_settled_contradictions(contradictions: list) -> int:
    """Riapre le righe in contraddizione e le ri-salda coi gol correnti.

    Il verdetto specchiato (bug 02/09) viene azzerato (esito_finale/profit/
    settled_at NULL) e il settlement ricomputa l'esito dai gol VERI di
    match_results: la bet passa da 'won' a 'lost' senza intervento manuale.

    Ritorna il numero di righe corrette (0 se la lista è vuota).
    """
    if not contradictions:
        return 0
    conn = _get_conn(); c = conn.cursor()
    for item in contradictions:
        c.execute(f"UPDATE {item['table']} SET esito_finale=NULL, profit=NULL, "
                  f"settled_at=NULL WHERE id=?", (item["id"],))
    conn.commit(); conn.close()
    nb, _ = settle_bets()
    npr, _ = settle_predictions()
    logger.warning("heal_settled_contradictions: riaperte e ri-sal date "
                   "%d righe (%d bets, %d pred)",
                   len(contradictions), nb, npr)
    return len(contradictions)


def bets_period(since: str):
    """Puntate automatiche piazzate da `since` (ISO): stake, chiuse e P/L."""
    rows = get_bets(limit=100000)
    rows = [r for r in rows if (r.get("created_at") or "") >= since]
    closed = [r for r in rows if r.get("esito_finale")]
    return {
        "piazzate": len(rows),
        "stake_totale": round(sum(r["stake"] or 0 for r in rows), 2),
        "chiusi": len(closed),
        "vinti": sum(1 for r in closed if r["esito_finale"] == "won"),
        "persi": sum(1 for r in closed if r["esito_finale"] == "lost"),
        "push": sum(1 for r in closed if r["esito_finale"] == "push"),
        "profit": round(sum(r["profit"] or 0 for r in closed), 2),
    }


def day_completed(day: str | None = None) -> bool:
    """True se tutte le partite del giorno (gia' iniziate) hanno il risultato.

    Le partite non ancora iniziate NON bloccano la giornata (in tarda serata
    nessuna partita inizia piu', quindi il check converge). Serve per il
    riepilogo "a fine ultima partita".
    """
    from datetime import timezone
    day = day or datetime.now().strftime("%Y-%m-%d")
    conn = _get_conn(); c = conn.cursor()
    rows = c.execute("SELECT id, commence_time FROM matches WHERE commence_time LIKE ?",
                     (f"{day}%",)).fetchall()
    conn.close()
    if not rows:
        return True  # nessuna partita -> giornata "completata" (niente da aspettare)
    now_utc = datetime.now(timezone.utc)
    result_conn = _get_conn()
    _create_results_table(result_conn)
    result_c = result_conn.cursor()
    try:
        for mid, commence in rows:
            try:
                start = datetime.fromisoformat(str(commence).replace("Z", "+00:00"))
            except Exception:
                continue  # data malformata: non blocca
            if start > now_utc:
                continue  # non ancora iniziata
            r = result_c.execute("SELECT 1 FROM match_results WHERE match_id=?", (mid,)).fetchone()
            if r is None:
                return False
    finally:
        result_conn.close()
    return True


def cassa_period(since: str):
    """Scommesse della cassa SALDATE a partire da `since` (ISO): P/L del periodo.

    Serve ai report giornalieri (sera = oggi, mattina = ieri): quanto ho
    davvero vinto/perso con le puntate in quel periodo.
    """
    closed = [e for e in get_cassa()
              if e.get("esito_finale") and (e.get("settled_at") or "") >= since]
    vinti = sum(1 for e in closed if e["esito_finale"] == "won")
    persi = sum(1 for e in closed if e["esito_finale"] == "lost")
    profit = sum(e.get("profit") or 0 for e in closed)
    speso = sum(e["importo"] or 0 for e in closed)
    return {"chiusi": len(closed), "vinti": vinti, "persi": persi,
            "speso": round(speso, 2), "profit": round(profit, 2),
            "roi": round((profit / speso * 100), 2) if speso else 0.0}


def clear_cassa():
    """Svuota la cassa (backup server)."""
    conn = _get_conn(); c = conn.cursor()
    c.execute("DELETE FROM cassa")
    conn.commit(); conn.close()

def init_db():
    conn = _get_conn(); c = conn.cursor()
    # Pulizia idempotente: una sola analisi per match (i doppioni di
    # match_analysis duplicavano i pick nella schedina via JOIN).
    c.execute("DELETE FROM match_analysis WHERE id NOT IN "
              "(SELECT MAX(id) FROM match_analysis GROUP BY match_id)")
    conn.commit(); conn.close()

def log_signal(chat_id, evento, esito, quota, probabilita, ev,
               surface: str = None):
    conn = _get_conn(); c = conn.cursor()
    c.execute(
        '''INSERT INTO signals
        (chat_id, evento, esito, quota, probabilita, ev, timestamp,
         esito_finale, profit, surface)
        VALUES (?,?,?,?,?,?,?,NULL,0.0,?)''',
        (chat_id, evento, esito, quota, probabilita, ev,
         datetime.now().isoformat(), surface))
    conn.commit(); conn.close()

def get_signals(chat_id=None, limit=50):
    conn = _get_conn(); c = conn.cursor()
    if chat_id:
        c.execute("SELECT * FROM signals WHERE chat_id=? ORDER BY id DESC LIMIT ?", (chat_id, limit))
    else:
        c.execute("SELECT * FROM signals ORDER BY id DESC LIMIT ?", (limit,))
    rows = c.fetchall(); conn.close()
    return [Signal(*r) for r in rows]

def get_performance_summary(days=30):
    conn = _get_conn(); c = conn.cursor()
    c.execute("SELECT COUNT(*), SUM(CASE WHEN esito_finale='won' THEN 1 ELSE 0 END), SUM(CASE WHEN esito_finale='lost' THEN 1 ELSE 0 END), SUM(profit) FROM signals WHERE esito_finale IS NOT NULL")
    row = c.fetchone(); conn.close()
    total, won, lost, profit = row if row else (0,0,0,0.0)
    roi = (profit/total*100) if total>0 else 0.0
    return {"closed":total or 0, "won":won or 0, "lost":lost or 0, "net_profit":profit or 0.0, "roi":roi}

def add_subscriber(chat_id, tier="free"):
    """Iscrive una chat. tier: 'free' o 'premium'.

    Se la chat esiste gia' la riga non viene sovrascritta (INSERT OR IGNORE):
    il tier esistente resta invariato; usare set_tier() per cambiarlo.
    """
    conn = _get_conn(); c = conn.cursor()
    c.execute('INSERT OR IGNORE INTO subscribers (chat_id, tier, premium_until) VALUES (?,?,NULL)',
              (chat_id, tier))
    conn.commit(); conn.close()

def remove_subscriber(chat_id):
    conn = _get_conn(); c = conn.cursor()
    c.execute('DELETE FROM subscribers WHERE chat_id=?', (chat_id,))
    conn.commit(); conn.close()

def set_tier(chat_id, tier, premium_until=None):
    """Aggiorna tier e scadenza premium di un iscritto.

    tier 'premium' senza premium_until = abbonamento senza scadenza.
    """
    conn = _get_conn(); c = conn.cursor()
    c.execute('UPDATE subscribers SET tier=?, premium_until=? WHERE chat_id=?',
              (tier, premium_until, chat_id))
    conn.commit(); conn.close()

def get_subscription(chat_id):
    """Ritorna (tier, premium_until) di una chat, o None se non iscritta.

    Un abbonamento premium scaduto viene degradato a 'free' (senza cancellare
    la riga: e' comunque un iscritto).
    """
    conn = _get_conn(); c = conn.cursor()
    c.execute('SELECT tier, premium_until FROM subscribers WHERE chat_id=?', (chat_id,))
    row = c.fetchone(); conn.close()
    if not row:
        return None
    tier, premium_until = row
    if tier == "premium" and premium_until:
        try:
            if datetime.fromisoformat(premium_until) < datetime.now():
                tier = "free"
        except ValueError:
            tier = "free"
    return tier, premium_until

def is_premium(chat_id):
    """True se la chat e' iscritta con abbonamento premium valido."""
    sub = get_subscription(chat_id)
    return bool(sub) and sub[0] == "premium"

def get_subscribers(tier=None):
    """Chat_id degli iscritti, opzionalmente filtrati per tier.

    Con tier='premium' include solo abbonamenti ancora validi (scadenza
    futura o assente). Con tier='free' include anche i premium scaduti.
    """
    conn = _get_conn(); c = conn.cursor()
    if tier is None:
        c.execute('SELECT chat_id FROM subscribers')
    elif tier == "premium":
        c.execute("SELECT chat_id FROM subscribers WHERE tier='premium' AND "
                  "(premium_until IS NULL OR premium_until >= ?)",
                  (datetime.now().isoformat(),))
    else:
        c.execute("SELECT chat_id FROM subscribers WHERE tier=? OR tier IS NULL", (tier,))
    rows = c.fetchall(); conn.close()
    return [r[0] for r in rows]

# --- Calendario ---
def save_match(match_id, league, home, away, commence, status="scheduled"):
    conn = _get_conn(); c = conn.cursor()
    c.execute('''INSERT OR REPLACE INTO matches VALUES (?,?,?,?,?,?,?)''',
              (match_id, league, home, away, commence, status, datetime.now().isoformat()))
    conn.commit(); conn.close()

def get_today_matches():
    conn = _get_conn(); c = conn.cursor()
    today = datetime.now().strftime("%Y-%m-%d")
    c.execute("SELECT * FROM matches WHERE commence_time LIKE ? ORDER BY commence_time", (f"{today}%",))
    rows = c.fetchall(); conn.close()
    return rows

def save_analysis(match_id, lam_h, lam_a, p1, px, p2, p_over, best_ev, best_esito, best_quota, best_bookmaker, status,
                  market_prob=None, market_edge=None):
    """Salva l'analisi di un match, incluso il confronto col mercato.

    Idempotente per match_id: ogni nuova analisi SOSTITUISCE la precedente
    (prima con piu' righe per match la schedina mostrava doppioni nel JOIN
    matches x match_analysis).

    market_prob: probabilita' implicita del mercato (devig) per l'esito scelto.
    market_edge: model_prob - market_prob (quanto il modello batte il mercato).
    """
    conn = _get_conn(); c = conn.cursor()
    c.execute("DELETE FROM match_analysis WHERE match_id=?", (match_id,))
    c.execute('''INSERT INTO match_analysis (match_id,lam_h,lam_a,prob_1,prob_X,prob_2,prob_over,best_ev,best_esito,best_quota,best_bookmaker,status,timestamp,market_prob,market_edge)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
              (match_id, lam_h, lam_a, p1, px, p2, p_over, best_ev, best_esito, best_quota, best_bookmaker, status,
               datetime.now().isoformat(), market_prob, market_edge))
    conn.commit(); conn.close()

def get_analysis_for_match(match_id):
    conn = _get_conn(); c = conn.cursor()
    c.execute("SELECT * FROM match_analysis WHERE match_id=? ORDER BY id DESC LIMIT 1", (match_id,))
    row = c.fetchone(); conn.close()
    return row

def is_notified(match_id, date_str):
    conn = _get_conn(); c = conn.cursor()
    c.execute("SELECT 1 FROM notifications WHERE match_id=? AND date=?", (match_id, date_str))
    row = c.fetchone(); conn.close()
    return row is not None

def mark_notified(match_id, date_str):
    conn = _get_conn(); c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO notifications VALUES (?,?)", (match_id, date_str))
    conn.commit(); conn.close()

def save_clv(match_id, esito, quota, signal_started=False, pinnacle_quota=None,
             closing_odds=None):
    """Registra un campione CLV per una coppia match+esito.

    - Prima analisi del match (signal_started=True): la quota corrente diventa la
      quota del segnale, e viene usata anche come primo campione di chiusura.
    - Analisi successive (signal_started=False): aggiorna la quota di chiusura,
      che converge verso il prezzo di mercato finale (CLV).
    - pinnacle_quota (opz.): prezzo Pinnacle per lo stesso esito, la closing
      line piu' sharp. Se assente, si usa solo la chiusura del miglior bookmaker.
    - closing_odds (opz., 04/10/2026): quota FINALE di Pinnacle a T-0, scritta
      dalla routine `closing_line.py`. E' il riferimento del "beat the market"
      per ogni ordine (`closing_line.beat_pct`).
    """
    conn = _get_conn(); c = conn.cursor()
    now = datetime.now().isoformat()
    pin = pinnacle_quota if pinnacle_quota and pinnacle_quota > 0 else None
    close = closing_odds if closing_odds and closing_odds > 0 else None
    c.execute("SELECT signal_quota, closing_quota, pinnacle_quota, closing_odds "
              "FROM clv_history WHERE match_id=? AND esito=?", (match_id, esito))
    row = c.fetchone()
    if row is None or signal_started:
        c.execute("INSERT OR REPLACE INTO clv_history "
                  "(match_id, esito, signal_quota, closing_quota, "
                  " pinnacle_quota, closing_odds, updated_at) "
                  "VALUES (?,?,?,?,?,?,?)",
                  (match_id, esito, quota, quota, pin, close, now))
    else:
        # COALESCE sui valori opzionali: una scrittura senza `closing_odds`
        # NON cancella quella gia' catturata a T-0 (la routine gira una volta
        # sola, mentre `fixture_engine` aggiorna la chiusura a ogni analisi).
        c.execute("UPDATE clv_history SET closing_quota=?, updated_at=?, "
                  "pinnacle_quota=COALESCE(?, pinnacle_quota), "
                  "closing_odds=COALESCE(?, closing_odds) "
                  "WHERE match_id=? AND esito=?",
                  (quota, now, pin, close, match_id, esito))
    conn.commit(); conn.close()

def clear_old_matches():
    conn = _get_conn(); c = conn.cursor()
    c.execute("DELETE FROM matches WHERE status='finished' OR commence_time < date('now','-2 days')")
    conn.commit(); conn.close()

# --- Tracking risultati ---
def _create_results_table(conn):
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS match_results (
        match_id TEXT PRIMARY KEY, league TEXT, home_team TEXT, away_team TEXT,
        score_home INTEGER, score_away INTEGER, result TEXT, settled_at TEXT)''')

def save_result(match_id, league, home, away, sh, sa, settled_at):
    conn = _get_conn(); _create_results_table(conn); c = conn.cursor()
    res = "1" if sh > sa else ("2" if sh < sa else "X")
    c.execute('''INSERT OR REPLACE INTO match_results VALUES (?,?,?,?,?,?,?,?)''',
              (match_id, league, home, away, sh, sa, res, settled_at))
    conn.commit(); conn.close()

def get_leagues_with_signals(days=3):
    conn = _get_conn(); c = conn.cursor()
    c.execute('''SELECT DISTINCT m.league FROM matches m
                 JOIN match_analysis a ON a.match_id = m.id
                 WHERE a.status IN ('value','strong_value')
                   AND m.commence_time >= date('now', ?)''', (f"-{days} days",))
    rows = c.fetchall(); conn.close()
    return [r[0] for r in rows]


def _parse_ts_utc(s):
    """ISO-8601 → datetime naive UTC (None se non parsabile)."""
    from datetime import timezone
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


# Finestra di refertazione in giorni: DEVE combaciare con
# `odds_api.SCORES_DAYS_FROM` (la API /scores restituisce ~3 giorni di
# risultati e `daysFrom` ha massimo 3). Se le due finestre divergono, il
# settlement interroga leghe che l'API non puo' piu' refertare: chiamate
# PAGATE che non possono saldare nulla. Override: env SETTLEMENT_WINDOW_DAYS.
SETTLEMENT_WINDOW_DAYS_DEFAULT = 3

# Intervallo (ore) con cui ri-verificare le leghe SENZA righe aperte.
# Quelle leghe entrano nel giro solo per la finestra di verifica/heal (un
# punteggio corretto dopo la chiusura deve poter ri-saldare la riga): senza
# questo intervallo verrebbero ri-interrogate a ogni scadenza della cache
# punteggi (24h), cioe' OGNI GIORNO — misurato il 13/09: erano 14 leghe su
# 28, meta' del costo di settlement. 0 = verifica a ogni scadenza cache
# (comportamento pre-13/09). Override: SETTLEMENT_HEAL_INTERVAL_HOURS.
SETTLEMENT_HEAL_INTERVAL_HOURS_DEFAULT = 36


def _heal_interval_hours() -> float:
    """Ore tra due verifiche di una lega senza righe aperte (0 = sempre)."""
    try:
        return max(0.0, float(os.getenv("SETTLEMENT_HEAL_INTERVAL_HOURS",
                                        str(SETTLEMENT_HEAL_INTERVAL_HOURS_DEFAULT))))
    except (TypeError, ValueError):
        return float(SETTLEMENT_HEAL_INTERVAL_HOURS_DEFAULT)


def _scores_cache_age_hours(sport) -> float | None:
    """Eta' in ore della cache punteggi di uno sport (None = assente/illeggibile).

    None e' trattato come "da scaricare": fail-open verso la verifica.
    """
    if not sport:
        return None
    f = DATA_DIR / f"toa_scores_{sport}.json"
    if not f.exists():
        return None
    try:
        data = json.loads(f.read_text())
        return (time.time() - float(data.get("ts", 0))) / 3600.0
    except Exception:
        return None


def _settlement_window_days() -> int:
    """Giorni di risultati effettivamente refertabili dalla API /scores."""
    try:
        import odds_api
        default = int(odds_api.SCORES_DAYS_FROM)
    except Exception:            # dipendenza assente/errore: default prudente
        default = SETTLEMENT_WINDOW_DAYS_DEFAULT
    try:
        return max(1, int(os.getenv("SETTLEMENT_WINDOW_DAYS", str(default))))
    except (TypeError, ValueError):
        return default


# --- COPERTURA DEL SETTLEMENT (15/09/2026) ---------------------------------
# Direttiva del proprietario ("riduco la copertura settlement"): misurato il
# 15/09, il referto costava **27 chiamate /scores in 24h (~51-58 crediti/giorno,
# ~2 a chiamata)** contro 18/giorno sostenibili fino al reset del 01/10 — e la
# voce dominante erano le leghe con le SOLE PREVISIONI aperte (telemetria di
# calibrazione), riscaricate a ogni giro del watchdog (ogni 4h).
# Da qui in poi una lega entra nel piano SOLO se ha una PUNTATA nel ledger
# (reale o simulata) aperta o chiusa da poco: **il referto segue il denaro**.
# Le previsioni delle leghe senza puntate restano aperte fino alla scadenza
# automatica (`expire_stale_sx_rows`, chiusura come push): si perde telemetria
# di calibrazione, MAI il referto di una puntata.
# Env: `SETTLEMENT_BETS_ONLY=0` -> comportamento esteso (tutte le righe aperte).
SETTLEMENT_BETS_ONLY_ENV = "SETTLEMENT_BETS_ONLY"


def _settlement_bets_only() -> bool:
    """True (default) se il settlement segue solo le leghe con puntate."""
    return os.getenv(SETTLEMENT_BETS_ONLY_ENV, "1").strip().lower() not in (
        "0", "false", "no", "off")


def _credits_below_low_water() -> bool:
    """True se i crediti sono sotto la soglia di risparmio (`CREDIT_LOW`).

    Serve a SALTARE la sola verifica periodica (leghe senza righe aperte)
    quando la quota scarseggia: e' costo puro che non salda nulla. Se i
    crediti non sono leggibili si comporta come "non sotto soglia": nessun
    cambio di comportamento silenzioso per un errore di lettura.
    """
    try:
        import odds_api
        rem = odds_api.get_remaining()
        return rem is not None and rem < odds_api.CREDIT_LOW
    except Exception:
        return False


def open_bet_match_ids() -> set:
    """match_id con una PUNTATA APERTA (nessun risultato finale).

    Serve a `sx_signals.settle_sx_bets`: il percorso PAGATO delle fonti
    esterne (the-odds-api / API-Football) segue il DENARO — la stessa
    politica `SETTLEMENT_BETS_ONLY` del watchdog (15/09/2026) — mentre il
    percorso SX-native (gratis) continua a coprire anche le sole previsioni.

    Senza questo filtro ogni previsione `sx-*` aperta faceva interrogare la
    sua lega a ogni scadenza di cache punteggi: `fetch_scores` costa 2
    crediti a chiamata (misurato il 04/10/2026 con `x-requests-last`,
    indipendente da `daysFrom`) e il percorso SX, a differenza del watchdog,
    NON applicava la politica solo-puntate — era il consumo dominante dei
    46,5 crediti/giorno.
    """
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT DISTINCT match_id FROM bets "
            "WHERE esito_finale IS NULL AND match_id IS NOT NULL").fetchall()
    finally:
        conn.close()
    return {r[0] for r in rows if r[0]}


def get_leagues_with_open_rows(recent_settled_hours: int = 48,
                               days_back: int | None = None,
                               heal_interval_hours: float | None = None,
                               bets_only: bool | None = None) -> list:
    """Leghe con scommesse ATTIVE (o chiuse da poco) da refertare.

    Refertazione MIRATA del settlement (risparmio crediti the-odds-api): si
    scaricano i risultati SOLO per le leghe che hanno davvero righe da
    seguire su partite GIÀ INIZIATE di recente:
      - predictions/bets APERTE (esito_finale IS NULL);
      - predictions/bets chiuse da meno di `recent_settled_hours` (finestra
        di verifica: se l'API corregge un punteggio dopo la chiusura, il
        sanity check/heal del watchdog deve poterlo vedere e ri-saldare);
      - solo partite iniziate da non piu' di `days_back` giorni — che di
        default e' ESATTAMENTE la finestra `odds_api.SCORES_DAYS_FROM` (vedi
        `_settlement_window_days`): una riga piu' vecchia della finestra non
        e' refertabile da nessuna fonte, quindi interrogare la sua lega e'
        una chiamata pagata che non salda nulla.

    Zero scommesse attive = zero chiamate fetch_scores per quella lega.
    Prima si interrogavano TUTTE le leghe con un segnale value negli ultimi
    3 giorni (get_leagues_with_signals): leghe con sole partite FUTURE o con
    righe chiuse da giorni venivano comunque interrogate ogni giorno,
    bruciando crediti senza saldare nulla.

    Le leghe che entrano SOLO per la finestra di verifica/heal (nessuna riga
    aperta) sono ri-interrogate con periodicità `heal_interval_hours`
    (default `SETTLEMENT_HEAL_INTERVAL_HOURS`, 36h), non a ogni scadenza
    della cache punteggi: senza quel limite erano ~metà del costo. Con i
    crediti sotto `CREDIT_LOW` la verifica periodica viene SALTATA del tutto
    (e' costo puro): si paga solo per saldare.

    `bets_only` (default da `SETTLEMENT_BETS_ONLY`, ON): il piano segue SOLO
    il ledger delle PUNTATE — le previsioni di leghe senza denaro non
    generano chiamate (vedi il commento del blocco COPERTURA).

    Misura del residuo e del costo atteso del prossimo giro:
    `settlement_residue()`.
    """
    from datetime import timedelta, timezone
    if days_back is None:
        days_back = _settlement_window_days()
    if heal_interval_hours is None:
        heal_interval_hours = _heal_interval_hours()
    if bets_only is None:
        bets_only = _settlement_bets_only()
    conn = _get_conn(); c = conn.cursor()
    try:
        if bets_only:
            # Solo il ledger delle puntate: il referto segue il denaro.
            c.execute('''SELECT m.league, m.commence_time,
                                b.esito_finale, b.settled_at
                         FROM bets b JOIN matches m ON m.id = b.match_id''')
        else:
            # UNION ALL per non perdere le leghe presenti solo in uno dei due
            # ledger (comportamento esteso, prima del 15/09).
            c.execute('''SELECT m.league, m.commence_time,
                                p.esito_finale, p.settled_at
                         FROM predictions p JOIN matches m ON m.id = p.match_id
                         UNION ALL
                         SELECT m.league, m.commence_time,
                                b.esito_finale, b.settled_at
                         FROM bets b JOIN matches m ON m.id = b.match_id''')
        rows = c.fetchall()
    finally:
        conn.close()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    start = now - timedelta(days=days_back)
    # Le righe chiuse da tracker usano datetime.now() NAIVE LOCALE (non UTC):
    # sui container l'offset e' ~0, ma in locale (es. CEST +2) la stringa
    # naive risulta "nel futuro" rispetto a UTC. Tolleranza simmetrica per
    # non perdere la finestra di verifica/heal per un mero skew di fuso.
    recent = now - timedelta(hours=recent_settled_hours + 12)
    recent_max = now + timedelta(hours=36)
    open_leagues = set()      # hanno una riga APERTA: si interrogano sempre
    heal_leagues = set()      # entrano solo per la verifica di righe chiuse
    for league, commence, esito_finale, settled_at in rows:
        ts = _parse_ts_utc(commence)
        if ts is None:
            # Data non interpretabile: prudenza, la teniamo (come prima).
            open_leagues.add(league)
            continue
        # Solo partite già iniziate di recente: quelle future non hanno
        # ancora un risultato da scaricare, quelle vecchie sono fuori dalla
        # finestra scores dell'API.
        if not (start <= ts <= now):
            continue
        if esito_finale is None:
            open_leagues.add(league)   # riga aperta da saldare
            continue
        st = _parse_ts_utc(settled_at)
        if st is not None and recent <= st <= recent_max:
            heal_leagues.add(league)   # chiusa da poco: verifica/heal
    needed = set(open_leagues)
    skip_heal = _credits_below_low_water()
    for lg in heal_leagues:
        if lg in open_leagues:
            needed.add(lg)
            continue
        if skip_heal:
            # Crediti sotto soglia: la verifica periodica e' la prima cosa da
            # tagliare (nessuna riga da saldare in questa lega).
            continue
        # Verifica periodica: solo se la cache punteggi e' piu' vecchia
        # dell'intervallo (None = assente/illeggibile -> si scarica).
        age = _scores_cache_age_hours(_league_to_sport(lg))
        if age is None or age >= heal_interval_hours:
            needed.add(lg)
    return sorted(needed)


def _league_to_sport(league):
    """Sport key the-odds-api per una lega del ledger, o None se non mappata.

    Fail-safe: qualunque errore -> None (mai una sport key inventata).
    `sx_signals` importa `tracker`, quindi l'import e' pigro (niente cicli).
    """
    if not league:
        return None
    try:
        from odds_api import SPORTS_MAP
        if league in SPORTS_MAP:
            return SPORTS_MAP[league]
    except Exception:
        pass
    try:
        from sx_signals import league_to_sport
        return league_to_sport(league)
    except Exception:
        return None


def settlement_coverage_policy() -> str:
    """Politica di copertura del settlement, in una riga (per i log).

    Rende visibile nei log cio' che il referto sta facendo: senza, un residuo
    piu' basso sembrerebbe un referto migliore invece di una scelta.
    """
    parts = ["solo-puntate" if _settlement_bets_only() else "tutte-le-righe"]
    if _credits_below_low_water():
        parts.append("verifica-periodica-saltata (crediti scarsi)")
    return ", ".join(parts)


def settlement_residue(window_days: int | None = None,
                        stale_days: int | None = None) -> dict:
    """PERCHE' le righe restano aperte: diagnosi del residuo di settlement.

    Rompe le righe aperte (bet + previsioni) per MOTIVO, cosi' il residuo e'
    leggibile a colpo d'occhio invece di essere un numero opaco:

      no_match_row    nessuna riga in `matches`: niente kickoff ne' nomi
                      squadra, quindi NESSUNA fonte puo' abbinarla — la
                      chiude la scadenza come push (`overdue_orphans` conta
                      quelle gia' oltre la soglia: DEVE restare 0);
      league_unmapped lega senza sport key the-odds-api (fuori catalogo o
                      etichetta ambigua): nessun download possibile;
      out_of_window   partita iniziata da piu' di `window_days` giorni, fuori
                      dalla finestra /scores: non piu' refertabile;
      not_started     partita futura (il risultato non esiste ancora);
      awaiting_result refertabile: la lega viene interrogata, il risultato
                      non e' ancora arrivato.

    `leagues_to_query`/`estimated_credits` = quello che il prossimo
    `_update_results` interroghera' DAVVERO (stesso pianificatore
    `get_leagues_with_open_rows`): la stima conta solo le leghe MAPPATE con
    cache scores assente o piu' vecchia di `odds_api.ODDS_TTL` (le altre sono
    servite dalla cache, costo 0). Il costo e' poi spaccato in
    `cost_open_driven` (c'e' una riga da saldare) e `cost_heal_only`
    (nessuna riga aperta: si paga solo per la verifica periodica).
    """
    from datetime import timedelta, timezone
    if window_days is None:
        window_days = _settlement_window_days()
    if stale_days is None:
        stale_days = _stale_days()
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT 'pred' AS kind, p.match_id, m.league, m.commence_time, "
            "p.created_at FROM predictions p "
            "LEFT JOIN matches m ON m.id = p.match_id "
            "WHERE p.esito_finale IS NULL "
            "UNION ALL "
            "SELECT 'bet', b.match_id, m.league, m.commence_time, b.created_at "
            "FROM bets b LEFT JOIN matches m ON m.id = b.match_id "
            "WHERE b.esito_finale IS NULL").fetchall()
    finally:
        conn.close()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    window_start = now - timedelta(days=window_days)
    stale_cutoff = now - timedelta(days=stale_days)
    open_count = {"bets": 0, "predictions": 0}
    reasons = {"no_match_row": 0, "league_unmapped": 0, "out_of_window": 0,
               "not_started": 0, "awaiting_result": 0}
    by_league: dict = {}
    overdue = 0
    refertabili: set = set()
    for kind, _mid, league, commence, created in rows:
        open_count["bets" if kind == "bet" else "predictions"] += 1
        if league is None:
            # Nessuna riga in `matches`: insaldabile per costruzione.
            reasons["no_match_row"] += 1
            ct = _parse_ts_utc(created) if created else None
            if ct is not None and ct < stale_cutoff:
                overdue += 1
            continue
        ts = _parse_ts_utc(commence) if commence else None
        if ts is None:
            # Data illeggibile/assente: `get_leagues_with_open_rows` tiene la
            # lega "per prudenza", quindi la contiamo come refertabile.
            reasons["awaiting_result"] += 1
            refertabili.add(league)
            by_league[league] = by_league.get(league, 0) + 1
            continue
        if not _league_to_sport(league):
            reasons["league_unmapped"] += 1
            continue
        if ts > now:
            reasons["not_started"] += 1
            continue
        if ts < window_start:
            reasons["out_of_window"] += 1
            continue
        reasons["awaiting_result"] += 1
        refertabili.add(league)
        by_league[league] = by_league.get(league, 0) + 1
    # Costo atteso del prossimo giro coi numeri del pianificatore vero: una
    # lega costa 1 credito solo se la sua cache punteggi e' assente o piu'
    # vecchia di ODDS_TTL (altrimenti la serve la cache, costo 0).
    try:
        planned = get_leagues_with_open_rows(recent_settled_hours=48,
                                            days_back=window_days)
    except Exception:
        planned = sorted(refertabili)
    try:
        import odds_api
        ttl_h = float(odds_api.ODDS_TTL) / 3600.0
    except Exception:
        ttl_h = 24.0
    estimated = 0
    for lg in planned:
        sport = _league_to_sport(lg)
        if not sport:
            continue           # non mappata: saltata senza chiamata, costo 0
        age = _scores_cache_age_hours(sport)
        if age is None or age >= ttl_h:
            estimated += 1
    mapped = [lg for lg in planned if _league_to_sport(lg)]
    open_driven = [lg for lg in mapped if lg in refertabili]
    return {
        "open": open_count,
        "reasons": reasons,
        "by_league": by_league,
        "leagues_to_query": planned,
        "leagues_to_query_mapped": mapped,
        "cost_open_driven": open_driven,
        "cost_heal_only": [lg for lg in mapped if lg not in refertabili],
        "estimated_credits": estimated,
        "overdue_orphans": overdue,
        "window_days": window_days,
        "stale_days": stale_days,
        "heal_interval_hours": _heal_interval_hours(),
        # Politica di copertura in vigore: senza dichiararla, un residuo piu'
        # basso sembrerebbe un miglioramento del referto invece di una scelta.
        "bets_only": _settlement_bets_only(),
        "heal_skipped_low_credits": _credits_below_low_water(),
    }

def get_results_stats():
    conn = _get_conn(); _create_results_table(conn); c = conn.cursor()
    c.execute('''SELECT r.home_team, r.away_team, r.score_home, r.score_away,
                        a.best_esito, a.best_quota, a.best_ev, a.status, a.match_id
                 FROM match_results r
                 JOIN match_analysis a ON a.match_id = r.match_id''')
    rows = c.fetchall(); conn.close()
    # Mappa CLV per (match_id, esito): raw + vig-free.
    clv_map = {}
    clv_vf_map = {}  # vig-free CLV
    try:
        from market_calib import clv_vig_free, clv_raw, devig
        conn2 = _get_conn(); c2 = conn2.cursor()
        c2.execute("SELECT match_id, esito, signal_quota, closing_quota, "
                   "pinnacle_quota FROM clv_history")
        for mid, esito, sig, clos, pin in c2.fetchall():
            el = (esito or "").lower().strip()
            if clos and clos > 0 and sig and sig > 0:
                clv_map[(mid, el)] = (sig / clos) - 1.0
                # CLV vig-free: usa Pinnacle come closing line se disponibile
                # (Pinnacle ha vig minimo, quasi fair).
                # Altrimenti deviga la closing quota stimando il mercato.
                fair_close = pin if (pin and pin > 0) else clos
                vf = clv_vig_free(sig, fair_close)
                if vf is not None:
                    clv_vf_map[(mid, el)] = vf
        conn2.close()
    except Exception:
        pass
    bets = []
    clvs = []
    clvs_vf = []
    for home, away, sh, sa, esito, quota, ev, status, mid in rows:
        if status not in ("value", "strong_value") or not esito or not quota or quota <= 1.0:
            continue
        el = esito.lower().strip()
        if "over" in el:
            won = (sh + sa) >= 3
        elif "under" in el:
            won = (sh + sa) <= 2
        elif el == (home or "").lower().strip():
            won = sh > sa
        elif el == (away or "").lower().strip():
            won = sa > sh
        else:
            won = sh == sa
        bets.append({"quota": quota, "won": won, "ev": ev or 0.0})
        clv = clv_map.get((mid, el))
        if clv is not None:
            clvs.append(clv)
        clvf = clv_vf_map.get((mid, el))
        if clvf is not None:
            clvs_vf.append(clvf)
    total = len(bets)
    won_n = sum(1 for b in bets if b["won"])
    net = sum((b["quota"] - 1) if b["won"] else -1 for b in bets)
    return {
        "total": total, "won": won_n, "lost": total - won_n,
        "net": net, "roi": (net / total * 100) if total else 0.0,
        "hit_rate": (won_n / total * 100) if total else 0.0,
        "avg_ev": (sum(b["ev"] for b in bets) / total) if total else 0.0,
        "clv_tracked": len(clvs),
        "avg_clv": (sum(clvs) / len(clvs)) if clvs else 0.0,
        "avg_clv_vf": (sum(clvs_vf) / len(clvs_vf)) if clvs_vf else 0.0,
        "clv_vf_tracked": len(clvs_vf),
    }
