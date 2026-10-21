"""P1 (19/10/2026) — rotte HTTP che scrivono o espongono il ledger.

Cosa difende questo file, in ordine di importanza:
1. `POST /api/analisi` (consuma crediti, riscrive il ledger), `POST /api/cassa`
   (scrive denaro) e `DELETE /api/cassa` (cancella il ledger) NON sono
   raggiungibili da un anonimo: token admin obbligatorio, altrimenti 503.
2. Il permesso e' per **metodo**: `GET /api/cassa` resta una lettura pubblica
   (la pagina Cassa del sito la usa), la DELETE sullo stesso path no.
3. Doppio interruttore (`API_WRITE_ENABLED`): il token dice CHI, l'env dice SE.
4. Nessuna GET ha effetti collaterali: la verifica e' AST-based (guarda le
   CHIAMATE, non la prosa delle docstring) e il settlement non e' piu'
   innescabile da `/api/dashboard` ne' da `/api/cassa`.
5. Cancellazione protetta: conferma esplicita `CANCELLA_CASSA`, `dry_run`,
   backup su volume PRIMA del DELETE; se il backup fallisce non si cancella.
6. Permessi di lettura separati (`API_READ_TOKEN` / `API_REQUIRE_READ_TOKEN`),
   senza rompere il sito quando non sono configurati.
7. Le chiavi in query string non finiscono nei log (`log_message`).

Tutto OFFLINE: server su 127.0.0.1 con porta effimera, `tracker` e i moduli
di analisi sostituiti con stub in memoria, Telegram sostituito. Nessun
ordine, nessuna rete esterna, nessun credito, nessun segreto reale (i token
sono `fake/...` e dichiarati tali).
"""

import ast
import inspect
import json
import logging
import sys
import textwrap
import threading
import types
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

import tracker
import web_api

ADMIN = "fake/p1-admin-token-0123456789abcdef"  # finto e dichiarato
READ = "fake/p1-read-token-0123456789abcdef"    # finto e dichiarato
CONFIRM = web_api.CASSA_DELETE_CONFIRM
ROWS = [
    {"id": 1, "partita": "Inter vs Napoli", "esito": "1", "quota": 2.1,
     "importo": 10.0, "esito_finale": None},
    {"id": 2, "partita": "Roma vs Milan", "esito": "X", "quota": 3.0,
     "importo": 20.0, "esito_finale": "won"},
]

_ENV_KEYS = (
    "API_ADMIN_TOKEN", "API_WRITE_ENABLED", "API_READ_TOKEN",
    "API_REQUIRE_READ_TOKEN", "API_AUTO_BET_ENABLED",
    "API_AUTO_BET_ALLOW_LIVE", "TEST_NOTIFY_KEY", "ADMIN_CHAT_ID",
    "QUOTAVERACE_BOT_TOKEN",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    yield


@pytest.fixture
def base_url():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), web_api.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _req(url, method="GET", body=None, headers=None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw or b"{}") if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw or b"{}")
        except Exception:
            payload = {"raw": raw.decode("utf-8", "replace")}
        return exc.code, payload


def _auth(token=ADMIN):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def cassa(monkeypatch, tmp_path):
    """Stub del ledger cassa: registra scritture/cancellazioni, zero DB."""
    state = {"saved": [], "cleared": 0}
    rows = [dict(r) for r in ROWS]
    monkeypatch.setattr(tracker, "get_cassa", lambda: rows)
    monkeypatch.setattr(
        tracker, "cassa_totals",
        lambda entries=None: {"entrate": 0.0, "uscite": 0.0, "saldo": 0.0})
    monkeypatch.setattr(
        tracker, "save_cassa_entry",
        lambda *a, **k: state["saved"].append((a, k)))
    monkeypatch.setattr(
        tracker, "settle_cassa", lambda: state.__setitem__("settle_calls", 1))

    def _clear():
        state["cleared"] += 1
        rows.clear()

    monkeypatch.setattr(tracker, "clear_cassa", _clear)
    monkeypatch.setattr(web_api, "DATA_DIR", tmp_path)
    return state, rows, tmp_path


@pytest.fixture
def analisi(monkeypatch):
    """Sostituisce la scansione: nessun credito, nessuna rete."""
    fake_fe = types.ModuleType("fixture_engine")
    calls = {"fetch": 0, "ratings": 0}

    def _fetch():
        calls["fetch"] += 1
        return 3, 1, []

    fake_fe.fetch_and_analyze_today = _fetch
    fake_re = types.ModuleType("rating_engine")
    fake_re.compute_ratings = lambda: calls.__setitem__(
        "ratings", calls["ratings"] + 1)
    monkeypatch.setitem(sys.modules, "fixture_engine", fake_fe)
    monkeypatch.setitem(sys.modules, "rating_engine", fake_re)
    return calls


# --------------------------------------------------------------------------
# 1) Il permesso e' per METODO, non per path
# --------------------------------------------------------------------------
class TestPermessiPerMetodo:
    def test_mappa_dei_permessi(self):
        assert web_api.ADMIN_METHOD_ROUTES["POST"] == frozenset(
            {"/api/auto_bet", "/api/analisi", "/api/cassa"})
        assert web_api.ADMIN_METHOD_ROUTES["DELETE"] == frozenset({"/api/cassa"})
        assert "GET" not in web_api.ADMIN_METHOD_ROUTES

    def test_get_cassa_resta_una_lettura(self):
        # la GET sulla cassa e' registrata e NON e' fra le rotte admin di GET
        assert "/api/cassa" in web_api.ROUTES
        admin_get = web_api.ADMIN_METHOD_ROUTES.get("GET", frozenset())
        assert "/api/cassa" not in admin_get
        # ... mentre la DELETE sullo stesso path richiede il token admin
        assert "/api/cassa" in web_api.ADMIN_METHOD_ROUTES["DELETE"]

    def test_analisi_non_e_piu_anonima(self):
        assert "/api/analisi" in web_api.ADMIN_METHOD_ROUTES["POST"]


# --------------------------------------------------------------------------
# 2) POST /api/analisi
# --------------------------------------------------------------------------
class TestAnalisi:
    def test_senza_token_503_e_nessuna_scansione(self, base_url, analisi):
        code, payload = _req(base_url + "/api/analisi", "POST", {})
        assert code == 503
        assert payload["error"] == "admin_disabled"
        assert analisi["fetch"] == 0

    def test_token_errato_401_e_nessuna_scansione(self, base_url, monkeypatch,
                                                  analisi):
        monkeypatch.setenv("API_ADMIN_TOKEN", ADMIN)
        monkeypatch.setenv("API_WRITE_ENABLED", "1")
        code, payload = _req(base_url + "/api/analisi", "POST", {},
                             headers=_auth("fake/sbagliato-0123456789abcd"))
        assert code == 401
        assert payload["error"] == "unauthorized"
        assert analisi["fetch"] == 0

    def test_token_valido_ma_scritture_spente_403(self, base_url, monkeypatch,
                                                  analisi):
        monkeypatch.setenv("API_ADMIN_TOKEN", ADMIN)
        code, payload = _req(base_url + "/api/analisi", "POST", {},
                             headers=_auth())
        assert code == 403
        assert payload["error"] == "disabled"
        assert analisi["fetch"] == 0

    def test_token_e_interruttore_abilitano(self, base_url, monkeypatch,
                                            analisi):
        monkeypatch.setenv("API_ADMIN_TOKEN", ADMIN)
        monkeypatch.setenv("API_WRITE_ENABLED", "1")
        code, payload = _req(base_url + "/api/analisi", "POST", {},
                             headers=_auth())
        assert code == 200
        assert payload["ok"] is True
        assert payload["partite"] == 3
        assert analisi["fetch"] == 1

    def test_token_di_lettura_non_basta(self, base_url, monkeypatch, analisi):
        monkeypatch.setenv("API_ADMIN_TOKEN", ADMIN)
        monkeypatch.setenv("API_WRITE_ENABLED", "1")
        monkeypatch.setenv("API_READ_TOKEN", READ)
        code, _ = _req(base_url + "/api/analisi", "POST", {},
                       headers=_auth(READ))
        assert code == 401
        assert analisi["fetch"] == 0


# --------------------------------------------------------------------------
# 3) POST /api/cassa (scrittura di denaro)
# --------------------------------------------------------------------------
class TestCassaPost:
    def test_anonimo_503_nessuna_scrittura(self, base_url, cassa):
        state, _, _ = cassa
        code, payload = _req(base_url + "/api/cassa", "POST",
                             {"partita": "A vs B", "esito": "1",
                              "quota": 2.0, "importo": 10})
        assert code == 503
        assert payload["error"] == "admin_disabled"
        assert state["saved"] == []

    def test_token_errato_401_nessuna_scrittura(self, base_url, monkeypatch,
                                                cassa):
        state, _, _ = cassa
        monkeypatch.setenv("API_ADMIN_TOKEN", ADMIN)
        monkeypatch.setenv("API_WRITE_ENABLED", "1")
        code, _ = _req(base_url + "/api/cassa", "POST",
                       {"partita": "A vs B", "esito": "1",
                        "quota": 2.0, "importo": 10},
                       headers=_auth("fake/nope-0123456789abcdefgh"))
        assert code == 401
        assert state["saved"] == []

    def test_token_ma_interruttore_spento_403(self, base_url, monkeypatch,
                                              cassa):
        state, _, _ = cassa
        monkeypatch.setenv("API_ADMIN_TOKEN", ADMIN)
        code, payload = _req(base_url + "/api/cassa", "POST",
                             {"partita": "A vs B", "esito": "1",
                              "quota": 2.0, "importo": 10},
                             headers=_auth())
        assert code == 403
        assert payload["error"] == "disabled"
        assert state["saved"] == []

    def test_admin_accettato(self, base_url, monkeypatch, cassa):
        state, _, _ = cassa
        monkeypatch.setenv("API_ADMIN_TOKEN", ADMIN)
        monkeypatch.setenv("API_WRITE_ENABLED", "1")
        code, payload = _req(base_url + "/api/cassa", "POST",
                             {"partita": "A vs B", "esito": "1",
                              "quota": 2.0, "importo": 10},
                             headers=_auth())
        assert code == 200
        assert payload["ok"] is True
        assert len(state["saved"]) == 1
        assert state["saved"][0][0][0] == "A vs B"

    def test_importo_non_numerico_400(self, base_url, monkeypatch, cassa):
        state, _, _ = cassa
        monkeypatch.setenv("API_ADMIN_TOKEN", ADMIN)
        monkeypatch.setenv("API_WRITE_ENABLED", "1")
        code, payload = _req(base_url + "/api/cassa", "POST",
                             {"partita": "A vs B", "esito": "1",
                              "quota": 2.0, "importo": "dieci"},
                             headers=_auth())
        assert code == 400
        assert state["saved"] == []


# --------------------------------------------------------------------------
# 4) DELETE /api/cassa (cancellazione del ledger)
# --------------------------------------------------------------------------
class TestCassaDelete:
    def test_anonimo_503_nessuna_cancellazione(self, base_url, cassa):
        state, rows, _ = cassa
        code, payload = _req(base_url + "/api/cassa", "DELETE", {})
        assert code == 503
        assert payload["error"] == "admin_disabled"
        assert state["cleared"] == 0
        assert len(rows) == 2

    def test_token_errato_401(self, base_url, monkeypatch, cassa):
        state, rows, _ = cassa
        monkeypatch.setenv("API_ADMIN_TOKEN", ADMIN)
        monkeypatch.setenv("API_WRITE_ENABLED", "1")
        code, _ = _req(base_url + "/api/cassa", "DELETE", {},
                       headers=_auth("fake/nope-0123456789abcdefgh"))
        assert code == 401
        assert state["cleared"] == 0
        assert len(rows) == 2

    def test_interruttore_spento_403(self, base_url, monkeypatch, cassa):
        state, rows, _ = cassa
        monkeypatch.setenv("API_ADMIN_TOKEN", ADMIN)
        code, _ = _req(base_url + "/api/cassa", "DELETE", {},
                       headers=_auth())
        assert code == 403
        assert state["cleared"] == 0
        assert len(rows) == 2

    def test_senza_conferma_428(self, base_url, monkeypatch, cassa):
        state, rows, _ = cassa
        monkeypatch.setenv("API_ADMIN_TOKEN", ADMIN)
        monkeypatch.setenv("API_WRITE_ENABLED", "1")
        code, payload = _req(base_url + "/api/cassa", "DELETE", {},
                             headers=_auth())
        assert code == 428
        assert payload["error"] == "confirmation_required"
        assert payload["righe"] == 2
        assert state["cleared"] == 0
        assert len(rows) == 2

    def test_conferma_sbagliata_non_cancella(self, base_url, monkeypatch,
                                             cassa):
        state, rows, _ = cassa
        monkeypatch.setenv("API_ADMIN_TOKEN", ADMIN)
        monkeypatch.setenv("API_WRITE_ENABLED", "1")
        code, _ = _req(base_url + "/api/cassa", "DELETE",
                       {"confirm": "cancella_cassa"}, headers=_auth())
        assert code == 428
        assert state["cleared"] == 0
        assert len(rows) == 2

    def test_dry_run_non_cancella(self, base_url, monkeypatch, cassa):
        state, rows, _ = cassa
        monkeypatch.setenv("API_ADMIN_TOKEN", ADMIN)
        monkeypatch.setenv("API_WRITE_ENABLED", "1")
        code, payload = _req(base_url + "/api/cassa", "DELETE",
                             {"dry_run": 1}, headers=_auth())
        assert code == 200
        assert payload["dry_run"] is True
        assert payload["righe"] == 2
        assert payload["eliminate"] == 0
        assert state["cleared"] == 0
        assert len(rows) == 2

    def test_conferma_nel_body_cancella_e_salva_backup(self, base_url,
                                                       monkeypatch, cassa):
        state, rows, tmp_path = cassa
        monkeypatch.setenv("API_ADMIN_TOKEN", ADMIN)
        monkeypatch.setenv("API_WRITE_ENABLED", "1")
        code, payload = _req(base_url + "/api/cassa", "DELETE",
                             {"confirm": CONFIRM}, headers=_auth())
        assert code == 200
        assert payload["ok"] is True
        assert payload["eliminate"] == 2
        assert state["cleared"] == 1
        backups = list((tmp_path / "backups").glob("cassa_*.json"))
        assert len(backups) == 1
        saved = json.loads(backups[0].read_text(encoding="utf-8"))
        assert len(saved["righe"]) == 2
        assert saved["righe"][0]["partita"] == "Inter vs Napoli"
        assert payload["backup"].endswith(backups[0].name)

    def test_conferma_in_query_string(self, base_url, monkeypatch, cassa):
        state, _, _ = cassa
        monkeypatch.setenv("API_ADMIN_TOKEN", ADMIN)
        monkeypatch.setenv("API_WRITE_ENABLED", "1")
        code, payload = _req(base_url + "/api/cassa?confirm=" + CONFIRM,
                             "DELETE", {}, headers=_auth())
        assert code == 200
        assert payload["eliminate"] == 2
        assert state["cleared"] == 1

    def test_backup_fallito_annulla_la_cancellazione(self, base_url,
                                                     monkeypatch, cassa):
        state, rows, _ = cassa
        monkeypatch.setenv("API_ADMIN_TOKEN", ADMIN)
        monkeypatch.setenv("API_WRITE_ENABLED", "1")
        monkeypatch.setattr(web_api, "_backup_cassa_rows",
                            lambda rows: None)
        code, payload = _req(base_url + "/api/cassa", "DELETE",
                             {"confirm": CONFIRM}, headers=_auth())
        assert code == 500
        assert payload["error"] == "backup_failed"
        assert state["cleared"] == 0
        assert len(rows) == 2

    def test_backup_scrive_su_volume(self, tmp_path, monkeypatch):
        monkeypatch.setattr(web_api, "DATA_DIR", tmp_path)
        path = web_api._backup_cassa_rows([{"partita": "X vs Y"}])
        assert path is not None and path.exists()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["righe"] == [{"partita": "X vs Y"}]
        assert "ts" in data

    def test_backup_non_solleva_su_volume_rotto(self, tmp_path, monkeypatch):
        # DATA_DIR e' un FILE, non una cartella: mkdir deve fallire
        broken = tmp_path / "file"
        broken.write_text("x", encoding="utf-8")
        monkeypatch.setattr(web_api, "DATA_DIR", broken)
        assert web_api._backup_cassa_rows([{"a": 1}]) is None


# --------------------------------------------------------------------------
# 5) GET pure
# --------------------------------------------------------------------------
_WRITE_PREFIXES = (
    "settle_", "save_", "clear_", "delete_", "record_", "update_",
    "log_signal", "place_order", "place_limit_order",
)


def _called_names(fn):
    """Nomi delle funzioni CHIAMATE nel codice (AST: la prosa non conta)."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


class TestGetPure:
    def test_nessuna_get_chiama_funzioni_di_scrittura(self):
        offenders = {}
        for path, fn in web_api.ROUTES.items():
            bad = sorted(n for n in _called_names(fn)
                         if n.startswith(_WRITE_PREFIXES))
            if bad:
                offenders[path] = bad
        assert offenders == {}

    def test_nessuna_get_invia_messaggi_o_ordini(self):
        banned = {"_telegram_send_message", "sendMessage", "run_today_bets",
                  "_live_fill", "place_limit_order", "resolve_market_for"}
        offenders = {}
        for path, fn in web_api.ROUTES.items():
            bad = sorted(_called_names(fn) & banned)
            if bad:
                offenders[path] = bad
        assert offenders == {}

    def test_cassa_get_non_salda(self, monkeypatch):
        monkeypatch.setattr(tracker, "get_cassa", lambda: [])
        monkeypatch.setattr(tracker, "cassa_totals", lambda e=None: {})
        monkeypatch.setattr(
            tracker, "settle_cassa",
            lambda: (_ for _ in ()).throw(
                AssertionError("GET /api/cassa non deve saldare")))
        assert web_api._cassa_get() == {"scommesse": [], "totali": {}}

    def test_dashboard_non_salda_nel_sorgente(self):
        names = _called_names(web_api._dashboard_json)
        assert not any(n.startswith(("settle_", "save_", "clear_"))
                       for n in names)


# --------------------------------------------------------------------------
# 6) Permessi di LETTURA separati da quelli admin
# --------------------------------------------------------------------------
class TestPermessiLettura:
    def test_health_sempre_pubblica(self, base_url, monkeypatch):
        monkeypatch.setenv("API_REQUIRE_READ_TOKEN", "1")
        code, payload = _req(base_url + "/api/health")
        assert code == 200
        assert payload["status"] == "ok"

    def test_get_pubbliche_senza_configurazione(self, base_url):
        code, payload = _req(base_url + "/api/campionati")
        assert code == 200
        assert payload["campionati"]

    def test_con_read_token_get_401_senza_token(self, base_url, monkeypatch):
        monkeypatch.setenv("API_READ_TOKEN", READ)
        code, payload = _req(base_url + "/api/campionati")
        assert code == 401
        assert payload["error"] == "unauthorized"

    def test_read_token_accettato(self, base_url, monkeypatch):
        monkeypatch.setenv("API_READ_TOKEN", READ)
        code, _ = _req(base_url + "/api/campionati", headers=_auth(READ))
        assert code == 200

    def test_admin_token_vale_anche_per_la_lettura(self, base_url,
                                                   monkeypatch):
        monkeypatch.setenv("API_READ_TOKEN", READ)
        monkeypatch.setenv("API_ADMIN_TOKEN", ADMIN)
        code, _ = _req(base_url + "/api/campionati", headers=_auth(ADMIN))
        assert code == 200

    def test_token_errato_sulla_lettura_401(self, base_url, monkeypatch):
        monkeypatch.setenv("API_READ_TOKEN", READ)
        code, _ = _req(base_url + "/api/campionati",
                       headers=_auth("fake/nope-0123456789abcdefgh"))
        assert code == 401

    def test_strict_senza_token_spegne_le_letture(self, base_url,
                                                  monkeypatch):
        monkeypatch.setenv("API_REQUIRE_READ_TOKEN", "1")
        code, payload = _req(base_url + "/api/campionati")
        assert code == 503
        assert payload["error"] == "read_disabled"

    def test_read_token_troppo_corto_spegne_le_letture(self, base_url,
                                                       monkeypatch):
        monkeypatch.setenv("API_READ_TOKEN", "corto")
        code, payload = _req(base_url + "/api/campionati",
                             headers=_auth("corto"))
        assert code == 503
        assert payload["error"] == "read_token_too_short"

    def test_header_x_api_token(self, base_url, monkeypatch):
        monkeypatch.setenv("API_READ_TOKEN", READ)
        code, _ = _req(base_url + "/api/campionati",
                       headers={"X-API-Token": READ})
        assert code == 200

    def test_read_check_direttamente(self, monkeypatch):
        monkeypatch.delenv("API_READ_TOKEN", raising=False)
        ok, code, _ = web_api.read_check({})
        assert (ok, code) == (True, 200)

    def test_read_check_fail_closed_su_token_corto(self, monkeypatch):
        monkeypatch.setenv("API_READ_TOKEN", "x" * 10)
        ok, code, payload = web_api.read_check(
            {"Authorization": "Bearer " + "x" * 10})
        assert ok is False and code == 503
        assert payload["error"] == "read_token_too_short"


# --------------------------------------------------------------------------
# 7) Nessun segreto nei log + confronto a tempo costante
# --------------------------------------------------------------------------
class TestLogENotify:
    def test_chiave_in_query_non_finisce_nei_log(self, base_url, monkeypatch,
                                                 caplog):
        segreto = "SUPERSECRET1234567890abcdef"
        monkeypatch.setenv("TEST_NOTIFY_KEY", "fake/p1-notify-0123456789")
        with caplog.at_level(logging.INFO, logger="web_api"):
            code, _ = _req(base_url + "/api/test_notify?key=" + segreto,
                           "POST", {})
        assert code == 401
        testi = [r.getMessage() for r in caplog.records]
        assert testi, "la request line deve essere loggata"
        assert all(segreto not in t for t in testi)
        assert any("key=***" in t for t in testi)

    def test_confronto_test_notify_a_tempo_costante(self, base_url,
                                                    monkeypatch):
        monkeypatch.setenv("TEST_NOTIFY_KEY", "fake/p1-notify-0123456789")
        vero = web_api.hmac.compare_digest
        chiamate = {"n": 0}

        def _spia(a, b):
            chiamate["n"] += 1
            return vero(a, b)

        monkeypatch.setattr(web_api.hmac, "compare_digest", _spia)
        code, _ = _req(base_url + "/api/test_notify", "POST",
                       {"key": "sbagliata"})
        assert code == 401
        assert chiamate["n"] == 1

    def test_nessun_segreto_nel_sorgente(self):
        src = inspect.getsource(web_api)
        for token in (ADMIN, READ):
            assert token not in src
        assert "TEST_NOTIFY_KEY" in src  # il NOME, mai il valore
