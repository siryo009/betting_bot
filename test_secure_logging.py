"""Test secure_logging: nessun segreto nei log (token Telegram, API key)
e setup idempotente.

Regressione 01/09: httpx loggava a INFO gli URL delle richieste Telegram,
che contengono il token del bot in chiaro nei log Railway.
"""

import logging

import secure_logging
from secure_logging import SensitiveDataFilter, collect_secrets, setup


def test_scrub_maschera_token_telegram():
    # Token FAKE di prova (mai una credenziale reale): la maschera si attiva
    # sul FORMATO del token, non sul valore.
    fake_token = "123456789:AAfake-test-token-xxxxxxxxxxxxxxxxxxxxxx-00"
    msg = (f"HTTP Request: POST https://api.telegram.org/bot"
           f"{fake_token}/getUpdates")
    out = SensitiveDataFilter.scrub(msg, set())
    assert "AAfake" not in out
    assert out.startswith("HTTP Request: POST https://api.telegram.org/bot***")


def test_scrub_maschera_hex_key():
    """Una chiave esadecimale CON etichetta davanti resta mascherata."""
    out = SensitiveDataFilter.scrub("key df59346f72e7c52b910f56a30f6011f4 non valida", set())
    assert "df59346f" not in out
    assert "***" in out
    # L'etichetta resta leggibile: si maschera il valore, non il contesto.
    assert out.startswith("key ***")


def test_scrub_non_maschera_gli_identificatori_di_partita():
    """Regressione 01/10/2026: gli event id di the-odds-api sono 32 hex.

    Il pattern esadecimale NUDO li mascherava: nei log di `auto_bet` il match id
    diventava `***REDACTED*** (1) @ 1.31` — un identificatore di partita (non un
    segreto) nascosto, con i log resi illeggibili a costo zero di sicurezza.
    """
    match_id = "6f1d2c3b4a5968778695a4b3c2d1e0f1"      # 32 hex, NON segreto
    msg = (f"auto_bet: {match_id} (1) @ 1.31 EV top-down -0.52% < 2.0%: "
           "no value")
    out = SensitiveDataFilter.scrub(msg, set())
    assert match_id in out, out
    assert "REDACTED" not in out


def test_scrub_maschera_la_chiave_con_etichetta_ma_non_il_match():
    """Stessa riga: la chiave vera sparisce, l'identificatore della partita no."""
    row = {"match_id": "cd50cbdecb590b3b66ff69dac64b1c7d",
           "api_key": "df59346f72e7c52b910f56a30f6011f4"}
    out = SensitiveDataFilter.scrub(
        f"evento {row['match_id']} api_key={row['api_key']}", set())
    assert row["match_id"] in out
    assert row["api_key"] not in out


def test_scrub_maschera_valori_env():
    filt = SensitiveDataFilter()
    filt._secrets = {"supersecretvalue123"}
    record = logging.LogRecord("x", logging.INFO, "f", 1,
                               "token=%s ok", ("supersecretvalue123",), None)
    assert filt.filter(record) is True
    assert "supersecretvalue123" not in record.getMessage()


def test_collect_secrets_legge_env(monkeypatch):
    monkeypatch.setenv("ODDS_API_KEY", "abcdefgh12345678")
    monkeypatch.delenv("QUOTAVERACE_BOT_TOKEN", raising=False)
    s = collect_secrets()
    assert "abcdefgh12345678" in s


def test_filtro_non_rompe_i_log_con_mapping():
    """Un dict come argomento non deve corrompere il record (bug del 15/09).

    `logger.warning("... %s", dati)` con un dict fa mettere il DICT (non una
    tupla) in `record.args`: il filtro lo iterava come una sequenza, producendo
    una tupla di chiavi e un `TypeError: not all arguments converted during
    string formatting` al momento della scrittura — cioe' un log che rompe
    l'handler invece di essere scritto.
    """
    import io
    import logging as _logging

    stream = io.StringIO()
    handler = _logging.StreamHandler(stream)
    handler.addFilter(SensitiveDataFilter())
    log = _logging.getLogger("test.mapping")
    log.handlers = [handler]
    log.propagate = False
    log.setLevel(_logging.DEBUG)
    log.warning("riga rifiutata: %s", {"event_id": "sx-1", "odds": 0.05})
    scritto = stream.getvalue()
    assert "sx-1" in scritto and "0.05" in scritto
    # La chiave del mapping non e' un segreto: il valore si'. Il valore e'
    # marcato `fake/` perche' e' un finto di test (la guardia
    # `test_secret_hygiene.py` cerca credenziali VERE nei sorgenti).
    fake_secret = "fake/supersegreto-1234567890"
    monkeypatch_set = SensitiveDataFilter()
    monkeypatch_set._secrets = {fake_secret}
    handler.filters = [monkeypatch_set]
    log.warning("credenziale: %s", {"token": fake_secret})
    assert fake_secret not in stream.getvalue()


def test_setup_httpx_a_warning():
    setup()
    assert logging.getLogger("httpx").level == logging.WARNING


def test_setup_apscheduler_a_warning():
    """APScheduler a INFO loggava OGNI avvio e OGNI fine di OGNI job
    ("Running job ..." / "Job ... executed successfully"): con ~15 job e
    quelli da 60s era ~il 45% del volume di log del container (misurato
    il 21/09/2026) e affogava i messaggi operativi.
    """
    setup()
    assert logging.getLogger("apscheduler").level == logging.WARNING
    # I child (executors/jobstores) EREDITANO: un INFO non passa piu'.
    assert not logging.getLogger("apscheduler.executors.default").isEnabledFor(
        logging.INFO)
    # ...ma i WARNING restano visibili: "skipped: maximum number of running
    # instances" e' un segnale reale (job sovrapposti), non rumore.
    assert logging.getLogger("apscheduler.scheduler").isEnabledFor(
        logging.WARNING)


def test_setup_idempotente_handler_root():
    root = logging.getLogger()
    n_before = len(root.handlers)
    setup()
    setup()
    assert len(root.handlers) == max(n_before, 1)


def test_filtro_su_handler_root_non_duplicato():
    setup()
    setup()
    root = logging.getLogger()
    for h in root.handlers:
        n = sum(isinstance(f, secure_logging.SensitiveDataFilter)
                for f in h.filters)
        assert n <= 1


# ---------------------------------------------------------------------------
# Handler di librerie terze sul root logger (fix 17/10/2026)
# ---------------------------------------------------------------------------


def _rich_like_handler():
    """Handler che DICHIARA il proprio modulo come `rich.logging`.

    E' la forma esatta dell'handler che `import soccerdata` installa sul root
    (verificato: `rich.logging.RichHandler`), senza dipendere da rich.
    """
    cls = type("RichHandler", (logging.Handler,), {"__module__": "rich.logging"})
    return cls()


def test_strip_rimuove_l_handler_rich_installato_da_una_libreria():
    """`import soccerdata` installa un RichHandler sul root: va rimosso.

    Il renderer rich va in `RecursionError` sui messaggi lunghi del bot e la
    riga di log viene PERSA (2 occorrenze in ~7 minuti in produzione, proprio
    sui log del monitoraggio).
    """
    setup()
    root = logging.getLogger()
    h = _rich_like_handler()
    root.addHandler(h)
    assert h in root.handlers
    assert secure_logging.strip_foreign_handlers() == 1
    assert h not in root.handlers
    # Il nostro handler di bootstrap resta al suo posto: si toglie il renderer
    # di libreria, non il logging del progetto.
    assert len(root.handlers) >= 1


def test_strip_non_tocca_gli_handler_legittimi_aggiunti_dopo_setup():
    """Denylist per PACCHETTO, non "cancella tutto tranne i miei".

    `verify_guardrails.py` aggiunge un `_Capture` al root DOPO `setup()`: una
    regola basata sull'identita' avrebbe rimosso anche quello, rompendo la
    diagnostica.
    """
    setup()
    root = logging.getLogger()
    keep = logging.StreamHandler()          # modulo `logging`, non terzo
    root.addHandler(keep)
    try:
        secure_logging.strip_foreign_handlers()
        assert keep in root.handlers
    finally:
        root.removeHandler(keep)


def test_strip_riattacca_il_filtro_ai_sopravvissuti():
    """Un handler aggiunto dopo il bootstrap non porta il filtro dei segreti.

    I due RotatingFileHandler di soccerdata scrivono su disco TUTTI i log del
    bot: senza questo passaggio ci finirebbero i messaggi non mascherati.
    """
    setup()
    root = logging.getLogger()
    late = logging.StreamHandler()          # nessun SensitiveDataFilter
    root.addHandler(late)
    try:
        secure_logging.strip_foreign_handlers()
        assert any(isinstance(f, SensitiveDataFilter) for f in late.filters)
    finally:
        root.removeHandler(late)


def test_strip_idempotente_e_fail_safe():
    """Due chiamate non fanno danno e un handler ostile non solleva mai."""
    setup()
    root = logging.getLogger()
    assert secure_logging.strip_foreign_handlers() == 0

    class _BoomMeta(type):
        def __getattribute__(cls, name):
            if name == "__module__":
                raise RuntimeError("modulo illeggibile")
            return super().__getattribute__(name)

    class _Ostile(logging.Handler, metaclass=_BoomMeta):
        pass

    ostile = _Ostile()
    root.addHandler(ostile)
    try:
        assert secure_logging.strip_foreign_handlers() == 0   # non solleva
        assert ostile in root.handlers                        # e non lo tocca
    finally:
        root.removeHandler(ostile)


def test_setup_ripulisce_un_richhandler_gia_presente():
    """Caso "soccerdata importato PRIMA del bootstrap": setup() ripulisce."""
    root = logging.getLogger()
    h = _rich_like_handler()
    root.addHandler(h)
    setup()
    assert h not in root.handlers


def test_denylist_dichiarata_contiene_rich():
    assert "rich" in secure_logging.FOREIGN_HANDLER_PACKAGES


def test_strip_rimuove_gli_handler_su_file_di_libreria(tmp_path):
    """`import soccerdata` scrive TUTTI i log del bot in `~/soccerdata/logs`.

    Misurato in produzione: `info.log` 175 KB con 1.657 righe di
    `auto_bet`/`gridiron_lane` e ZERO valori mascherati (handler aggiunti dopo
    il bootstrap, senza SensitiveDataFilter).
    """
    setup()
    root = logging.getLogger()
    fh = logging.FileHandler(tmp_path / "soccerdata-info.log")
    try:
        secure_logging.strip_foreign_handlers()
        assert fh not in root.handlers
    finally:
        try:
            fh.close()
        except Exception:
            pass


def test_guardia_ripristina_l_handler_dopo_una_riconfigurazione():
    """Il caso reale: `logging.config.dictConfig` di una libreria svuota il root.

    soccerdata lo esegue all'import e poi mette un RichHandler al posto del
    console: senza il ripristino il bot perdeva formato E filtro dei segreti.
    """
    import logging.config

    setup()
    root = logging.getLogger()
    logging.config.dictConfig({
        "version": 1,
        "disable_existing_loggers": False,
        "handlers": {"console": {"class": "logging.StreamHandler",
                                  "stream": "ext://sys.stdout"}},
        "root": {"handlers": ["console"], "level": "INFO"},
    })
    assert not any(getattr(h, secure_logging._OWNED_ATTR, False)
                   for h in root.handlers)
    root.handlers[0] = _rich_like_handler()          # come fa soccerdata
    assert secure_logging.strip_foreign_handlers() == 1
    owned = [h for h in root.handlers
             if getattr(h, secure_logging._OWNED_ATTR, False)]
    assert len(owned) == 1                          # UN handler di progetto
    assert owned[0].formatter._fmt == secure_logging.LOG_FORMAT
    assert any(isinstance(f, SensitiveDataFilter) for f in owned[0].filters)


def test_guardia_riabbassa_il_livello_alzato_da_una_libreria():
    """soccerdata imposta il root da `SOCCERDATA_LOGLEVEL`: se sale, i nostri
    INFO sparirebbero (`auto_bet` logga a INFO)."""
    setup()
    root = logging.getLogger()
    root.setLevel(logging.ERROR)
    secure_logging.strip_foreign_handlers()
    assert root.level == logging.INFO
    # ...ma un livello piu' VERBOSO richiesto da noi resta: si corregge solo
    # verso il basso, mai si stringe.
    secure_logging.setup(level=logging.DEBUG)
    secure_logging.strip_foreign_handlers()
    assert logging.getLogger().level == logging.DEBUG
    secure_logging.setup(level=logging.INFO)


def test_tripwire_il_progetto_non_installa_handler_su_file():
    """La regola "file handler sul root = di libreria" regge solo se il progetto
    non ne crea mai uno: verificato su tutti i sorgenti (AST, non grep, cosi'
    i commenti non contano)."""
    import ast
    from pathlib import Path

    colpevoli = []
    for p in sorted(Path(__file__).parent.glob("*.py")):
        # I test sono esclusi: simulare l'handler di una libreria richiede di
        # crearne uno. La regola riguarda i moduli di PRODUZIONE.
        if p.name.startswith("test_"):
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and getattr(n.func, "id", "") in (
                    "FileHandler", "RotatingFileHandler",
                    "TimedRotatingFileHandler", "WatchedFileHandler"):
                colpevoli.append(f"{p.name}:{n.lineno}")
    assert colpevoli == [], (
        "il progetto usa un file handler sul root: la regola della guardia "
        f"lo rimuoverebbe — {colpevoli}")
