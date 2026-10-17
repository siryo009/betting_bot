"""Logging sicuro: nessun segreto nei log di Railway.

Il bug del 01/09: httpx (usato da python-telegram-bot) logga l'URL completo
delle richieste, incluso il token del bot:
    POST https://api.telegram.org/bot<TOKEN>/getUpdates
su Railway i log sono visibili a chiunque abbia accesso al progetto.

Difese (tutte attive di default via setup()):
  1. SensitiveDataFilter: filtro logging che maschera i valori dei segreti
     noti (dagli env var sensibili) e i pattern di token riconoscibili.
  2. httpx e python-telegram-bot a WARNING: gli HttpRequestInfoLogged di
     httpx (che contengono l'URL col token) non passano piu' a INFO.
  3. urllib3 a WARNING (riduce rumore, nessun URL di richieste in log).

Chiamare setup() al bootstrap di OGNI entrypoint (run_all, web_api, bot).
Idempotente: aggiunge il filtro una sola volta.
"""
import logging
import os
import re
from typing import Mapping, Optional

logger = logging.getLogger(__name__)

# Variabili d'ambiente i cui valori sono segreti da non stampare mai.
SECRET_ENV_KEYS = (
    "QUOTAVERACE_BOT_TOKEN", "TELEGRAM_BOT_TOKEN",
    "ODDS_API_KEY", "API_FOOTBALL_KEY", "GITHUB_TOKEN",
    "GOOGLE_API_KEY", "SECRETS_MASTER_KEY",
    "TEST_NOTIFY_KEY", "RAILWAY_TOKEN",
    # 01/10/2026: le chiavi di SX Bet e dei provider d'oracolo (OddsPapi/Exa)
    # non erano coperte. Il match e' per VALORE ESATTO (nessun falso positivo
    # possibile su nomi di partita o identificatori), quindi e' la difesa
    # giusta dopo aver tolto la maschera generica sulle stringhe esadecimali.
    "SX_API_KEY", "SX_PRIVATE_KEY", "ODDSPAPI_KEY", "EXA_API_KEY",
)

# Fallback: pattern di segreti riconoscibili anche se la variabile non e'
# tra quelle sopra (nuove chiavi, token passati per errore).
# NB: niente \b iniziale nel token Telegram: negli URL e' attaccato a 'bot'
# (api.telegram.org/bot<TOKEN>/getUpdates) e li' non c'e' word boundary.
_BOT_TOKEN_RE = re.compile(r"\d{6,12}:[A-Za-z0-9_-]{25,}")  # token Telegram

#: Chiave esadecimale SEGNALATA da un'etichetta di credenziale.
#:
#: Perche' il CONTESTO e' obbligatorio (fix 01/10/2026): il pattern precedente
#: era nudo (`\b[0-9a-f]{32}\b`) e mascherava anche i normali IDENTIFICATORI
#: di partita — gli event id di the-odds-api sono esattamente 32 caratteri
#: esadecimali (`cd50cbdecb590b3b66ff69dac64b1c7d`). Nei log di `auto_bet` il
#: match id finiva come `***REDACTED*** (1) @ 1.31`, cioe' illeggibile e senza
#: alcun guadagno di sicurezza: quel valore NON e' un segreto.
#: I valori reali delle env restano coperti da `collect_secrets()` (match sul
#: valore esatto); questa e' la rete per un segreto NON presente in ambiente al
#: momento del log. Un token nudo senza etichetta non e' distinguibile da un id:
#: preferiamo un log leggibile a una maschera che nasconde dati utili.
_HEX_LABELLED_RE = re.compile(
    r"\b(?P<label>api[_-]?key|apikey|key|token|secret|bearer|password|passwd)\b"
    r"(?P<gap>\s*[:=]?\s*)(?P<hex>[0-9a-f]{32,64})\b",
    re.IGNORECASE,
)

_REDACTED = "***REDACTED***"


def collect_secrets() -> set[str]:
    """Valori dei segreti presenti nell'ambiente (filtrati per lunghezza minima
    per non mascherare stringhe banali tipo '1' o 'true')."""
    secrets = set()
    for key in SECRET_ENV_KEYS:
        val = os.getenv(key, "")
        if val and len(val) >= 8:
            secrets.add(val)
    return secrets


class SensitiveDataFilter(logging.Filter):
    """Maschera i segreti nei messaggi di log (record.getMessage())."""

    def __init__(self):
        super().__init__()
        self._secrets: set[str] = set()

    def refresh(self) -> None:
        """Ricarica i segreti dall'ambiente (chiamare dopo load_secrets_dir)."""
        self._secrets = collect_secrets()

    @staticmethod
    def scrub(text: str, secrets: set[str]) -> str:
        for s in secrets:
            if s in text:
                text = text.replace(s, _REDACTED)
        # Pattern generici (anche per segreti non in env al momento del log).
        # La chiave esadecimale si maschera SOLO con l'etichetta davanti: senza,
        # un event id di the-odds-api (32 hex) sparirebbe dai log (fix 01/10).
        text = _BOT_TOKEN_RE.sub(_REDACTED, text)
        text = _HEX_LABELLED_RE.sub(
            lambda m: f"{m.group('label')}{m.group('gap')}{_REDACTED}", text)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            self.refresh()
        try:
            msg = record.getMessage()
        except Exception:
            return True  # mai bloccare il logging per un formato rotto
        redacted = self.scrub(msg, self._secrets)
        if redacted != msg:
            record.msg = redacted
            record.args = None
        # Gli URL delle richieste httpx contengono il token: maschera anche
        # gli args (es. logger.info("%s", url)).
        if record.args:
            try:
                # ATTENZIONE ai MAPPING: `logger.warning("... %s", dati)` con un
                # dict come unico argomento fa si' che logging metta il DICT in
                # `record.args` (non una tupla). Iterarlo direttamente produceva
                # una tupla di CHIAVI e il record non era piu' formattabile:
                # `TypeError: not all arguments converted during string
                # formatting` alla scrittura (trovato il 15/09: bastava un log
                # con un dict per rompere l'handler). I valori vanno mascherati
                # MANTENENDO il mapping.
                if isinstance(record.args, Mapping):
                    record.args = {
                        key: (self.scrub(val, self._secrets)
                              if isinstance(val, str) else val)
                        for key, val in record.args.items()
                    }
                else:
                    record.args = tuple(
                        self.scrub(a, self._secrets) if isinstance(a, str) else a
                        for a in record.args
                    )
            except Exception:
                pass
        return True


_filter_instance: SensitiveDataFilter | None = None


def _get_filter() -> SensitiveDataFilter:
    global _filter_instance
    if _filter_instance is None:
        _filter_instance = SensitiveDataFilter()
    return _filter_instance


def _apply_to_root() -> None:
    """Attacca il filtro a tutti gli handler del root logger (e ai logger
    senza handler propri, che propagano al root)."""
    flt = _get_filter()
    root = logging.getLogger()
    for h in root.handlers:
        if not any(isinstance(f, SensitiveDataFilter) for f in h.filters):
            h.addFilter(flt)
    # Filtro anche sul logger di libreria che ha handler propri (raro, ma sicuro)
    for name in ("httpx", "httpcore", "urllib3", "telegram"):
        lg = logging.getLogger(name)
        for h in lg.handlers:
            if not any(isinstance(f, SensitiveDataFilter) for f in h.filters):
                h.addFilter(flt)


# Pacchetti di renderer di log che le librerie terze installano sul ROOT logger
# e che NON devono sopravvivere in produzione.
#
# Caso reale misurato il 17/10/2026 in produzione: `import soccerdata` (che
# `live_intel` importa pigramente) installa sul root logger un
# `rich.logging.RichHandler` — e due `RotatingFileHandler` propri. Quando il
# renderer rich deve formattare i messaggi LUNGHI del bot (timeout del wallet,
# riepilogo della closing line) va in `RecursionError`: il logging di Python
# stampa "--- Logging error ---" e la riga viene PERSA. In produzione se ne
# vedevano 2 in ~7 minuti, e a sparire erano proprio i log che servono al
# monitoraggio (il bot non si ferma, ma la diagnosi diventa cieca).
FOREIGN_HANDLER_PACKAGES: tuple[str, ...] = ("rich",)


# Formato standard del progetto (unica definizione: prima era scritto inline
# in `basicConfig` e ripetuto qui per l'handler ripristinato dalla guardia).
LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

#: Livello che il PROGETTO ha configurato (aggiornato da `setup`). Serve alla
#: guardia: se una libreria alza il livello del root (soccerdata lo imposta da
#: `SOCCERDATA_LOGLEVEL`, default INFO) i nostri INFO spariscono e vanno
#: riportati a questo valore.
_OWNED_LEVEL: int = logging.INFO


#: Marcatore dell'handler di stdout del progetto. La guardia riconosce cosi'
#: l'handler "nostro" senza dipendere dal suo modulo (un `StreamHandler` e' un
#: `StreamHandler` anche quando lo crea una libreria).
_OWNED_ATTR = "_qv_owned"


def _ensure_owned_handler(level: Optional[int] = None):
    """Garantisce un handler di stdout del PROGETTO sul root logger.

    Una libreria che riconfigura il logging (soccerdata lo fa con
    `logging.config.dictConfig`) **sostituisce** gli handler del root: da quel
    momento il bot scriverebbe solo sui file della libreria e perderebbe
    formato e filtro dei segreti. Qui l'handler viene ripristinato.

    Non crea duplicati: se esiste gia' un handler marcato lo riusa, e se il
    root ha handler non-file (quelli di `basicConfig`, pytest, o il `_Capture`
    di `verify_guardrails.py`) li ADOTTA invece di aggiungerne un altro — e'
    cio' che tiene valida la promessa "setup non aggiunge un secondo handler".
    """
    root = logging.getLogger()
    owned = [h for h in root.handlers if getattr(h, _OWNED_ATTR, False)]
    if not owned:
        candidati = [h for h in root.handlers
                     if not getattr(h, "baseFilename", None)]
        if candidati:
            for h in candidati:
                try:
                    setattr(h, _OWNED_ATTR, True)
                except Exception:
                    pass
            owned = candidati
        else:
            nuovo = logging.StreamHandler()
            nuovo.setFormatter(logging.Formatter(LOG_FORMAT))
            setattr(nuovo, _OWNED_ATTR, True)
            root.addHandler(nuovo)
            owned = [nuovo]
    wanted = _OWNED_LEVEL if level is None else level
    # Solo verso il BASSO (piu' verboso): una libreria che alza il livello a
    # WARNING/ERROR farebbe sparire i nostri INFO.
    if root.level > wanted:
        root.setLevel(wanted)
    return owned[0]


def strip_foreign_handlers(level: Optional[int] = None) -> int:
    """Rimuove dal ROOT logger gli handler di librerie terze e RIMETTE il nostro.

    Due regole, entrambe misurate sul campo (17/10/2026):
      1. DENYLIST per pacchetto (`rich`): il `RichHandler` che soccerdata
         installa sul root va in `RecursionError` sui messaggi lunghi e la
         riga di log viene PERSA. NON e' un "cancella tutto tranne i miei":
         un handler legittimo aggiunto dopo `setup()` (il `_Capture` di
         `verify_guardrails.py`) resta al suo posto.
      2. HANDLER SU FILE: il progetto non ne installa MAI sul root (verificato:
         l'unico logging su file e' quello di `buffer`/telemetria, che non
         passa dal root). Quelli di soccerdata scrivevano TUTTI i log del bot
         in `~/soccerdata/logs/info.log` (misurato in produzione: 175 KB con
         1.657 righe di `auto_bet`/`gridiron_lane` e ZERO valori mascherati,
         perche' aggiunti dopo il bootstrap senza il filtro).

    Poi garantisce l'handler di stdout del progetto (`_ensure_owned_handler`) e
    riattacca il filtro dei segreti a cio' che sopravvive: da qui in poi nessun
    handler sul root puo' scrivere una credenziale in chiaro.

    Fail-safe: non solleva mai. Ritorna il numero di handler rimossi, cosi' il
    chiamante puo' dichiararlo invece di ripulire in silenzio.
    """
    removed = 0
    try:
        root = logging.getLogger()
        for handler in list(root.handlers):
            try:
                package = (type(handler).__module__ or "").split(".")[0]
            except Exception:
                continue                      # handler ostile: si lascia stare
            if package in FOREIGN_HANDLER_PACKAGES:
                try:
                    root.removeHandler(handler)
                    removed += 1
                except Exception:
                    pass
                continue
            if getattr(handler, "baseFilename", None):
                try:
                    root.removeHandler(handler)
                    removed += 1
                except Exception:
                    pass
        _ensure_owned_handler(level)
        _apply_to_root()
    except Exception:
        pass
    return removed


def setup(level: int = logging.INFO) -> None:
    """Configura il logging sicuro per l'entrypoint. Da chiamare al bootstrap.

    - root handler con formato standard + SensitiveDataFilter
    - libreria httpx (e affini) a WARNING: niente URL col token a INFO
    """
    global _OWNED_LEVEL
    _OWNED_LEVEL = level
    flt = _get_filter()
    flt.refresh()
    root = logging.getLogger()
    # PRIMA di configurare: un RichHandler gia' presente (libreria importata
    # prima del bootstrap) non deve essere scambiato per un handler del
    # progetto e adottato.
    strip_foreign_handlers(level)
    if not root.handlers:
        logging.basicConfig(format=LOG_FORMAT, level=level)
    root.setLevel(level)
    _ensure_owned_handler(level)
    _apply_to_root()
    # httpx logga a INFO ogni richiesta con URL completo (che per Telegram
    # include il token): a WARNING sparisce. I messaggi httpx che passano
    # sono comunque mascherati da SensitiveDataFilter.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    # APScheduler a WARNING (21/09/2026): a INFO logga OGNI avvio e OGNI fine
    # di ogni job ("Running job ..." / "Job ... executed successfully") piu'
    # l'elenco "Adding job tentatively" all'avvio. Con ~15 job registrati e
    # i job da 60s erano ~il 45% di TUTTO il volume di log del container, e
    # affogavano i messaggi operativi (puntate, settlement, scarti). I
    # WARNING restano visibili: "skipped: maximum number of running
    # instances" e gli errori di job sono segnali reali, non rumore.
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    # Copre il caso in cui una libreria terza abbia gia' importato/modificato il
    # logging PRIMA del bootstrap (`import soccerdata` -> dictConfig sul root).
    strip_foreign_handlers(level)


def refresh_secrets() -> None:
    """Ricarica i segreti nel filtro (dopo load_secrets_dir tardivo)."""
    if _filter_instance is not None:
        _filter_instance.refresh()
