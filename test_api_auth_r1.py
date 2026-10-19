"""R1 (19/10/2026) — autenticazione/autorizzazione di `POST /api/auto_bet`.

Cosa difende questo file, in ordine di importanza:
1. la rotta che puo' piazzare ordini reali e' **spenta** senza configurazione
   esplicita (`API_ADMIN_TOKEN` + `API_AUTO_BET_ENABLED`);
2. un token errato non passa e il confronto e' a tempo costante;
3. anche con token valido il giro e' **forzato in SIM** finche'
   `API_AUTO_BET_ALLOW_LIVE` non e' attivo: il token da solo non ordina;
4. `run_today_bets` accetta l'override di modalita' **solo verso il basso**
   ("sim"/"off"): un "live" scritto per errore non diventa un ordine;
5. nessuna rotta GET puo' ordinare o inviare messaggi (e le uniche GET che
   scrivono sono quelle dichiarate, con settlement idempotente).

Tutto OFFLINE: `auto_bet` e' sostituito da un modulo finto in `sys.modules`,
Telegram e' sostituito, il server ascolta su 127.0.0.1 con porta effimera.
Nessun ordine, nessuna rete, nessun credito.
"""

import inspect
import json
import re
import sys
import threading
import types
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

import web_api

TOKEN = "fake/r1-token-0123456789abcdef"  # finto e dichiarato, non un segreto

# Scritture ammesse su una rotta GET: settlement IDEMPOTENTE del ledger locale
# (nessun ordine, nessuna rete). Se una GET nuova scrive, il test fallisce e
# la si deve dichiarare qui — non scoprire dopo.
GET_WRITE_ALLOWLIST = {"/api/dashboard", "/api/cassa"}

_FORBIDDEN_GET = re.compile(
    r"\b(run_today_bets|_live_fill|place_limit_order|place_order|"
    r"resolve_market_for|_telegram_send_message|sendMessage)\b")
_WRITE = re.compile(r"\b(settle_\w+|save_\w+|clear_\w+)\s*\(|"
                    r"INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM",
                    re.IGNORECASE)


class FakeAutoBet(types.ModuleType):
    """Sostituto di `auto_bet`: registra le chiamate e non fa nulla."""

    def __init__(self):
        super().__init__("auto_bet")
        self.calls = []

    def run_today_bets(self, **kwargs):  # noqa: D401 - contratto del motore
        self.calls.append(kwargs)
        return []


@pytest.fixture
def fake_auto_bet(monkeypatch):
    fake = FakeAutoBet()
    monkeypatch.setitem(sys.modules, "auto_bet", fake)
    return fake


@pytest.fixture
def base_url():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), web_api.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d" % httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@pytest.fixture(autouse=True)
def _clean_auth_env(monkeypatch):
    """Ogni test parte senza credenziali: il default e' SPENTO."""
    for name in ("API_ADMIN_TOKEN", "API_AUTO_BET_ENABLED",
                 "API_AUTO_BET_ALLOW_LIVE", "TEST_NOTIFY_KEY"):
        monkeypatch.delenv(name, raising=False)


def _request(url, method="GET", body=None, headers=None, timeout=5):
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read() or b"{}"
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"raw": raw.decode("utf-8", "replace")}


def _auto_bet(base, headers=None):
    return _request(base + "/api/auto_bet", method="POST", body={},
                    headers=headers)


# --------------------------------------------------------------------------
# 1) Fail-closed: senza configurazione la rotta e' SPENTA
# --------------------------------------------------------------------------
class TestFailClosed:
    def test_token_assente_503(self, base_url, fake_auto_bet):
        code, payload = _auto_bet(base_url)
        assert code == 503
        assert payload["error"] == "admin_disabled"
        assert fake_auto_bet.calls == []

    def test_token_troppo_corto_503(self, base_url, fake_auto_bet, monkeypatch):
        monkeypatch.setenv("API_ADMIN_TOKEN", "corto")
        monkeypatch.setenv("API_AUTO_BET_ENABLED", "1")
        code, payload = _auto_bet(base_url)
        assert code == 503
        assert payload["error"] == "admin_token_too_short"
        assert fake_auto_bet.calls == []

    def test_senza_header_401(self, base_url, fake_auto_bet, monkeypatch):
        monkeypatch.setenv("API_ADMIN_TOKEN", TOKEN)
        monkeypatch.setenv("API_AUTO_BET_ENABLED", "1")
        code, payload = _auto_bet(base_url)
        assert code == 401
        assert payload["error"] == "unauthorized"
        assert fake_auto_bet.calls == []

    def test_token_errato_401(self, base_url, fake_auto_bet, monkeypatch):
        monkeypatch.setenv("API_ADMIN_TOKEN", TOKEN)
        monkeypatch.setenv("API_AUTO_BET_ENABLED", "1")
        code, payload = _auto_bet(
            base_url, {"Authorization": "Bearer fake/token-sbagliato-000000"})
        assert code == 401
        assert fake_auto_bet.calls == []

    def test_endpoint_disabilitato_anche_con_token_valido(
            self, base_url, fake_auto_bet, monkeypatch):
        """L'autenticazione non basta: serve l'interruttore esplicito."""
        monkeypatch.setenv("API_ADMIN_TOKEN", TOKEN)
        code, payload = _auto_bet(
            base_url, {"Authorization": f"Bearer {TOKEN}"})
        assert code == 403
        assert payload["error"] == "disabled"
        assert fake_auto_bet.calls == []

    @pytest.mark.parametrize("valore", ["si", "on-off", "2", "enabled"])
    def test_flag_ignoto_non_abilita(self, base_url, fake_auto_bet,
                                     monkeypatch, valore):
        monkeypatch.setenv("API_ADMIN_TOKEN", TOKEN)
        monkeypatch.setenv("API_AUTO_BET_ENABLED", valore)
        code, _ = _auto_bet(base_url, {"Authorization": f"Bearer {TOKEN}"})
        assert code == 403
        assert fake_auto_bet.calls == []


# --------------------------------------------------------------------------
# 2) Autorizzato: SIM forzata di default, LIVE solo con flag esplicito
# --------------------------------------------------------------------------
class TestAutorizzato:
    def _enabled(self, monkeypatch, allow_live=None):
        monkeypatch.setenv("API_ADMIN_TOKEN", TOKEN)
        monkeypatch.setenv("API_AUTO_BET_ENABLED", "1")
        if allow_live is not None:
            monkeypatch.setenv("API_AUTO_BET_ALLOW_LIVE", allow_live)

    def test_bearer_valido_ma_sim_forzata(self, base_url, fake_auto_bet,
                                          monkeypatch):
        self._enabled(monkeypatch)
        code, payload = _auto_bet(
            base_url, {"Authorization": f"Bearer {TOKEN}"})
        assert code == 200
        assert payload["ok"] is True
        assert payload["sim_forzata"] is True
        assert fake_auto_bet.calls == [{"allow_sim": True,
                                        "mode_override": "sim"}]

    def test_x_api_token_valido(self, base_url, fake_auto_bet, monkeypatch):
        self._enabled(monkeypatch)
        code, _ = _auto_bet(base_url, {"X-API-Token": TOKEN})
        assert code == 200
        assert len(fake_auto_bet.calls) == 1

    def test_live_solo_con_flag_esplicito(self, base_url, fake_auto_bet,
                                          monkeypatch):
        self._enabled(monkeypatch, allow_live="1")
        code, payload = _auto_bet(
            base_url, {"Authorization": f"Bearer {TOKEN}"})
        assert code == 200
        assert payload["sim_forzata"] is False
        assert fake_auto_bet.calls == [{"allow_sim": True,
                                        "mode_override": None}]

    def test_confronto_a_tempo_costante(self, base_url, fake_auto_bet,
                                        monkeypatch):
        """Il confronto passa da hmac.compare_digest, non da ==."""
        self._enabled(monkeypatch)
        chiamate = []
        originale = web_api.hmac.compare_digest

        def spia(a, b):
            chiamate.append(True)
            return originale(a, b)

        monkeypatch.setattr(web_api.hmac, "compare_digest", spia)
        code, _ = _auto_bet(base_url, {"Authorization": f"Bearer {TOKEN}"})
        assert code == 200
        assert chiamate, "il token deve essere confrontato a tempo costante"

    def test_letture_restano_aperte(self, base_url):
        """La protezione non deve rompere la dashboard del sito."""
        code, _ = _request(base_url + "/api/health")
        assert code == 200


# --------------------------------------------------------------------------
# 3) L'override del motore va SOLO verso il basso
# --------------------------------------------------------------------------
class TestOverrideMotore:
    def test_live_rifiutato_senza_toccare_nulla(self):
        import auto_bet
        # Ritorna PRIMA di wallet/ledger: nessun effetto collaterale.
        assert auto_bet.run_today_bets(mode_override="live") == []

    def test_valore_ignoto_rifiutato(self):
        import auto_bet
        assert auto_bet.run_today_bets(mode_override="dry-run") == []

    def test_off_accettato(self):
        import auto_bet
        assert auto_bet.run_today_bets(mode_override="off") == []

    def test_kill_switch_prevale_sull_override(self, monkeypatch):
        """Il kill switch e' un'autorita' superiore: un override SIM non
        puo' riaccendere le puntate dopo `/autobet off`."""
        import auto_bet
        monkeypatch.setattr(auto_bet, "_requested_mode", lambda: "off")
        monkeypatch.setattr(auto_bet, "stop_audit", lambda *a, **k: None)
        assert auto_bet.run_today_bets(mode_override="sim") == []

    def test_firma_espone_override(self):
        import auto_bet
        params = inspect.signature(auto_bet.run_today_bets).parameters
        assert "mode_override" in params
        assert params["mode_override"].default is None


# --------------------------------------------------------------------------
# 4) Rotte di monitoraggio: nessun ordine, nessun messaggio, scritture note
# --------------------------------------------------------------------------
class TestRotteMonitoraggio:
    def test_nessuna_get_ordina_o_invia(self):
        for path, fn in web_api.ROUTES.items():
            sorgente = inspect.getsource(fn)
            assert not _FORBIDDEN_GET.search(sorgente), (
                f"{path} usa un percorso d'ordine o di invio: "
                f"una GET non deve poterlo fare")

    def test_scritture_su_get_solo_quelle_dichiarate(self):
        trovate = {path for path, fn in web_api.ROUTES.items()
                   if _WRITE.search(inspect.getsource(fn))}
        assert trovate == GET_WRITE_ALLOWLIST, (
            "una rotta GET scrive e non e' dichiarata: aggiungerla "
            "all'allowlist con una motivazione, o toglierne la scrittura")

    def test_test_notify_non_e_piu_get(self):
        assert "/api/test_notify" not in web_api.ROUTES
        assert "/api/test_notify" in web_api.POST_ROUTES

    def test_get_test_notify_404_e_nessun_invio(self, base_url, monkeypatch):
        inviati = []
        monkeypatch.setattr(web_api, "_telegram_send_message",
                            lambda *a, **k: inviati.append(a))
        code, _ = _request(base_url + "/api/test_notify?key=x")
        assert code == 404
        assert inviati == []

    def test_post_test_notify_funziona_con_chiave(self, base_url,
                                                  monkeypatch):
        monkeypatch.setenv("TEST_NOTIFY_KEY", "fake/test-key")
        monkeypatch.setenv("ADMIN_CHAT_ID", "111")
        monkeypatch.setenv("QUOTAVERACE_BOT_TOKEN", "fake/bot-token")
        inviati = []
        monkeypatch.setattr(web_api, "_telegram_send_message",
                            lambda token, chat, text: inviati.append(chat)
                            or {"ok": True})
        code, payload = _request(
            base_url + "/api/test_notify", method="POST",
            body={"key": "fake/test-key"})
        assert code == 200
        assert payload["ok"] is True
        assert inviati == [111]

    def test_post_test_notify_senza_chiave_401(self, base_url, monkeypatch):
        monkeypatch.setenv("TEST_NOTIFY_KEY", "fake/test-key")
        code, _ = _request(base_url + "/api/test_notify", method="POST",
                           body={})
        assert code == 401
