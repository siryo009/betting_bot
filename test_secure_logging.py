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
