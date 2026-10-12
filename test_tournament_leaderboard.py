"""test_tournament_leaderboard.py — classifica del torneo SX Bet (12/10/2026).

Tutti i test sono OFFLINE: il trasporto (`http_get`) e' iniettato, quindi
nessuna chiamata a `api.sx.bet`, nessun credito the-odds-api, nessun ordine.
Le date usate nei seed sono SEMPRE relative a `now`: un test con una data
fissa scade col calendario e arriva sempre nel momento peggiore (lezione del
15/09, 17/09, 30/09).
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import tournament_leaderboard as tbl


# ---------------------------------------------------------------------------
# Fake HTTP
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, status: int, payload):
        self.status_code = status
        self._payload = payload
        self.headers = {}

    def json(self):
        return self._payload


def make_http(routes, calls=None):
    """`routes` = {frammento_url: (status, payload)}."""

    def _get(url, headers=None, timeout=None):
        if calls is not None:
            calls.append({"url": url, "headers": headers, "timeout": timeout})
        for frag, res in routes.items():
            if frag in url:
                return _Resp(res[0], res[1])
        return _Resp(404, {})

    return _get


def _end_date(days: float = 5.0) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def _tournament(tid="bf0929d23dc4", name="🏈 $7,777 Gridiron Gauntlet 🏈"):
    return {
        "tournamentId": tid,
        "name": name,
        "status": "ACTIVE",
        "type": "RETURN",
        "startDate": (datetime.now(timezone.utc)
                      - timedelta(days=4)).isoformat(),
        "endDate": _end_date(),
        "memberCount": 103,
    }


def _rows(me_rank=101, me_result=0.0, cutoff_result=191.278157):
    """Classifica sintetica: 3 sopra il taglio, l'utente, la coda."""
    rows = [
        {"username": "thadog90", "rank": 1, "totalResult": 21729.13,
         "totalTradeCount": 138, "prize": 2000},
        {"username": "Minionaire", "rank": 2, "totalResult": 19999.29,
         "totalTradeCount": 451, "prize": 1100},
        {"username": "Matanovik", "rank": 3, "totalResult": 7925.12,
         "totalTradeCount": 3, "prize": 700},
        {"username": "Farkinel", "rank": 30, "totalResult": cutoff_result,
         "totalTradeCount": 5, "prize": 76},
        {"username": "CoinFlipLover", "rank": 31, "totalResult": 179.59,
         "totalTradeCount": 79, "prize": 0},
        {"username": "Siryo", "rank": me_rank, "totalResult": me_result,
         "totalTradeCount": 1,
         "prize": 76 if 0 < me_rank <= 30 else 0},
    ]
    return rows


def _snap_http(me_rank=101, me_result=0.0, cutoff_result=191.278157,
               tor=None, rows=None, calls=None):
    routes = {
        "/tournaments/ongoing": (200, {"data": {"tournaments": [
            tor if tor is not None else _tournament()]}}),
        "/tournaments/": (200, {"status": "success",
                                "data": rows or _rows(me_rank, me_result,
                                                      cutoff_result)}),
    }
    return make_http(routes, calls)


# ---------------------------------------------------------------------------
# Configurazione
# ---------------------------------------------------------------------------

class TestConfigurazione:
    def test_default(self, monkeypatch):
        for name in ("TOURNAMENT_BOARD", "TOURNAMENT_USERNAME",
                     "TOURNAMENT_TOP_RANKS", "TOURNAMENT_BOARD_TTL_S",
                     "TOURNAMENT_BOARD_NOTIFY_HOURS",
                     "TOURNAMENT_BOARD_TIMEOUT_S", "SX_TOURNAMENT_API_BASE",
                     "SX_TOURNAMENT_UA", "TOURNAMENT_LABEL"):
            monkeypatch.delenv(name, raising=False)
        assert tbl.board_enabled() is True
        assert tbl.api_base() == "https://api.sx.bet"
        assert tbl.user_agent() == "Mozilla/5.0"
        assert tbl.username() == "Siryo"
        assert tbl.top_ranks() == 30
        assert tbl.cache_ttl_s() == 300.0
        assert tbl.notify_min_hours() == 12.0
        assert tbl.request_timeout_s() == 15.0

    @pytest.mark.parametrize("value", ["0", "false", "no", "OFF", "disabled"])
    def test_lettore_spento(self, monkeypatch, value):
        monkeypatch.setenv("TOURNAMENT_BOARD", value)
        assert tbl.board_enabled() is False

    def test_valore_ignoto_resta_acceso(self, monkeypatch):
        # fail-safe: la lettura e' GRATUITA, un valore strano non la spegne.
        monkeypatch.setenv("TOURNAMENT_BOARD", "boh")
        assert tbl.board_enabled() is True

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("TOURNAMENT_USERNAME", "  Tizio  ")
        monkeypatch.setenv("TOURNAMENT_TOP_RANKS", "10")
        monkeypatch.setenv("TOURNAMENT_BOARD_TTL_S", "60")
        monkeypatch.setenv("TOURNAMENT_BOARD_NOTIFY_HOURS", "6")
        monkeypatch.setenv("SX_TOURNAMENT_API_BASE", "https://example.test/")
        assert tbl.username() == "Tizio"                     # strip
        assert tbl.top_ranks() == 10
        assert tbl.cache_ttl_s() == 60.0
        assert tbl.notify_min_hours() == 6.0
        assert tbl.api_base() == "https://example.test"      # rstrip

    def test_valori_impossibili_tornano_al_default(self, monkeypatch):
        # Una guardia di lettura non si spegne con un env sbagliato.
        monkeypatch.setenv("TOURNAMENT_BOARD_TTL_S", "abc")
        monkeypatch.setenv("TOURNAMENT_TOP_RANKS", "-3")
        monkeypatch.setenv("TOURNAMENT_BOARD_TIMEOUT_S", "0")
        assert tbl.cache_ttl_s() == 300.0
        assert tbl.top_ranks() == 30
        assert tbl.request_timeout_s() == 15.0

    def test_top_ranks_minimo_uno(self, monkeypatch):
        monkeypatch.setenv("TOURNAMENT_TOP_RANKS", "0")
        assert tbl.top_ranks() == 30


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class TestHttp:
    def test_get_ok_e_user_agent(self, monkeypatch):
        calls = []
        res = tbl._get("/tournaments/ongoing", http_get=make_http(
            {"/tournaments/ongoing": (200, {"data": {}})}, calls))
        assert res["ok"] is True and res["status"] == 200
        assert res["payload"] == {"data": {}}
        # SX Bet SCARTA le richieste senza User-Agent: l'header deve esserci.
        assert calls[0]["headers"]["User-Agent"] == "Mozilla/5.0"
        assert calls[0]["url"].endswith("/tournaments/ongoing")

    def test_get_status_non_200(self):
        res = tbl._get("/x", http_get=make_http({"/x": (401, {})}))
        assert res["ok"] is False and res["status"] == 401
        assert "HTTP 401" in res["error"]

    def test_get_non_solleva_su_eccezione(self):
        def boom(url, headers=None, timeout=None):
            raise ConnectionError("rete giu'")

        res = tbl._get("/x", http_get=boom)
        assert res["ok"] is False
        assert "ConnectionError" in res["error"]

    def test_get_non_solleva_su_json_rotto(self):
        class Bad:
            status_code = 200

            def json(self):
                raise ValueError("json rotto")

        res = tbl._get("/x", http_get=lambda *a, **k: Bad())
        assert res["ok"] is False and "ValueError" in res["error"]

    def test_timeout_iniettabile(self):
        calls = []
        tbl._get("/x", http_get=make_http({"/x": (200, {})}, calls),
                 timeout=3.0)
        assert calls[0]["timeout"] == 3.0

    def test_nessun_import_di_requests_a_livello_modulo(self):
        src = Path("tournament_leaderboard.py").read_text(encoding="utf-8")
        head = src.split("def _get", 1)[0]
        assert "import requests" not in head


# ---------------------------------------------------------------------------
# Estrazione tollerante dei payload
# ---------------------------------------------------------------------------

class TestEstrazione:
    def test_tornei_da_forma_osservata(self):
        payload = {"data": {"tournaments": [{"tournamentId": "a"}]}}
        assert tbl._tournaments_of(payload) == [{"tournamentId": "a"}]

    def test_tornei_da_lista_diretta(self):
        assert tbl._tournaments_of([{"id": 1}]) == [{"id": 1}]

    def test_tornei_da_data_lista(self):
        assert tbl._tournaments_of({"data": [{"id": 2}]}) == [{"id": 2}]

    @pytest.mark.parametrize("payload", [None, 42, "stringa", {},
                                         {"data": {"altro": 1}}, [1, 2]])
    def test_tornei_spazzatura_vuoto(self, payload):
        assert tbl._tournaments_of(payload) == []

    def test_classifica_da_forma_osservata(self):
        payload = {"status": "success", "data": [{"rank": 1}]}
        assert tbl._rows_of(payload) == [{"rank": 1}]

    @pytest.mark.parametrize("key", ["data", "leaderboard", "rows", "entries"])
    def test_classifica_chiavi_tollerate(self, key):
        assert tbl._rows_of({key: [{"rank": 2}]}) == [{"rank": 2}]

    @pytest.mark.parametrize("payload", [None, {}, {"data": {}}, 7, "x"])
    def test_classifica_spazzatura_vuoto(self, payload):
        assert tbl._rows_of(payload) == []


class TestNormalizzazione:
    def test_rinomina_e_tipizza(self):
        row = tbl.normalize_row({"username": " Siryo ", "rank": "101",
                                 "totalResult": "191.278157",
                                 "totalTradeCount": "5", "prize": "76"})
        assert row == {"username": "Siryo", "rank": 101, "result": 191.278157,
                       "trades": 5, "prize": 76.0}

    def test_campi_assenti_o_sporchi(self):
        row = tbl.normalize_row({"username": None, "rank": "abc",
                                 "totalResult": None})
        assert row == {"username": "", "rank": 0, "result": 0.0, "trades": 0,
                       "prize": 0.0}

    def test_ordinamento_per_rank(self):
        rows = tbl.normalize_rows([{"username": "b", "rank": 3},
                                   {"username": "a", "rank": 1},
                                   {"username": "z", "rank": 0}])
        assert [r["username"] for r in rows] == ["a", "b", "z"]

    def test_find_row_case_insensitive(self):
        rows = tbl.normalize_rows(_rows())
        assert tbl.find_row(rows)["rank"] == 101
        assert tbl.find_row(rows, "siryo")["rank"] == 101

    def test_find_row_assente(self):
        assert tbl.find_row(tbl.normalize_rows(_rows()), "Nessuno") is None

    def test_find_row_nome_vuoto(self, monkeypatch):
        monkeypatch.setenv("TOURNAMENT_USERNAME", "   ")
        assert tbl.find_row(tbl.normalize_rows(_rows())) is None


class TestTaglio:
    def test_riga_esatta_del_taglio(self):
        rows = tbl.normalize_rows(_rows())
        cut = tbl.cutoff_row(rows, 30)
        assert cut["username"] == "Farkinel"
        assert cut["result"] == pytest.approx(191.278157)

    def test_fallback_ultima_premiata(self):
        # Senza la riga esatta si prende l'ultima posizione <= taglio.
        rows = tbl.normalize_rows([
            {"username": "a", "rank": 1, "totalResult": 10},
            {"username": "b", "rank": 28, "totalResult": 5},
        ])
        assert tbl.cutoff_row(rows, 30)["rank"] == 28

    def test_classifica_vuota(self):
        assert tbl.cutoff_row([], 30) is None

    def test_rank_zero_ignorato(self):
        rows = tbl.normalize_rows([{"username": "x", "rank": 0,
                                    "totalResult": 999}])
        assert tbl.cutoff_row(rows, 30) is None


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------

class TestSnapshot:
    def test_cammino_felice(self, monkeypatch):
        monkeypatch.delenv("TOURNAMENT_BOARD", raising=False)
        snap = tbl.snapshot(http_get=_snap_http(), use_cache=False)
        assert snap["ok"] is True
        assert snap["tournament"]["id"] == "bf0929d23dc4"
        assert snap["tournament"]["type"] == "RETURN"
        assert snap["me"]["rank"] == 101
        assert snap["me"]["trades"] == 1
        assert snap["cutoff"]["rank"] == 30
        assert snap["gap"] == pytest.approx(191.278157)
        assert snap["in_prizes"] is False
        assert snap["days_left"] is not None and snap["days_left"] > 0
        assert snap["top"][0]["username"] == "thadog90"
        assert snap["participants"] == 6

    def test_in_zona_premi(self, monkeypatch):
        monkeypatch.delenv("TOURNAMENT_BOARD", raising=False)
        http = _snap_http(me_rank=20, me_result=250.0)
        snap = tbl.snapshot(http_get=http, use_cache=False)
        assert snap["in_prizes"] is True
        assert snap["gap"] == 0.0            # nessun gap: siamo sopra il taglio

    def test_memo_dello_snapshot(self, monkeypatch):
        monkeypatch.delenv("TOURNAMENT_BOARD", raising=False)
        tbl.reset_cache()
        calls = []
        http = _snap_http(calls=calls)
        first = tbl.snapshot(http_get=http)
        n_after_first = len(calls)
        second = tbl.snapshot(http_get=http)
        assert second["cached"] is True
        assert len(calls) == n_after_first          # nessuna seconda lettura
        assert second["me"]["rank"] == first["me"]["rank"]

    def test_no_cache_ignora_il_memo(self, monkeypatch):
        monkeypatch.delenv("TOURNAMENT_BOARD", raising=False)
        tbl.reset_cache()
        calls = []
        http = _snap_http(calls=calls)
        tbl.snapshot(http_get=http)
        n1 = len(calls)
        tbl.snapshot(http_get=http, use_cache=False)
        assert len(calls) > n1

    def test_lettore_spento(self, monkeypatch):
        monkeypatch.setenv("TOURNAMENT_BOARD", "0")
        snap = tbl.snapshot(http_get=_snap_http(), use_cache=False)
        assert snap["ok"] is False
        assert "TOURNAMENT_BOARD" in snap["error"]

    def test_torneo_non_trovato(self, monkeypatch):
        monkeypatch.delenv("TOURNAMENT_BOARD", raising=False)
        http = make_http({"/tournaments/ongoing": (200, {"data": {
            "tournaments": [{"tournamentId": "x",
                             "name": "Altro Torneo"}]}}),
            "/tournaments/upcoming": (200, {"data": {"tournaments": []}})})
        snap = tbl.snapshot(http_get=http, use_cache=False)
        assert snap["ok"] is False
        assert "non trovato" in snap["error"]
        assert snap["me"] is None

    def test_classifica_vuota(self, monkeypatch):
        monkeypatch.delenv("TOURNAMENT_BOARD", raising=False)
        http = make_http({
            "/tournaments/ongoing": (200, {"data": {"tournaments": [
                _tournament()]}}),
            "/tournaments/": (200, {"status": "success", "data": []})})
        snap = tbl.snapshot(http_get=http, use_cache=False)
        assert snap["ok"] is False
        assert "0 righe" in snap["error"]

    def test_errore_di_rete_non_solleva(self, monkeypatch):
        monkeypatch.delenv("TOURNAMENT_BOARD", raising=False)

        def boom(url, headers=None, timeout=None):
            raise ConnectionError("giu'")

        snap = tbl.snapshot(http_get=boom, use_cache=False)
        assert snap["ok"] is False
        assert "non trovato" in snap["error"]        # nessuna rete -> nessun torneo

    def test_utente_non_in_classifica(self, monkeypatch):
        monkeypatch.setenv("TOURNAMENT_USERNAME", "Fantasma")
        snap = tbl.snapshot(http_get=_snap_http(), use_cache=False)
        assert snap["ok"] is True
        assert snap["me"] is None
        assert snap["gap"] is None
        assert snap["in_prizes"] is False

    def test_days_left_con_fuso(self, monkeypatch):
        monkeypatch.delenv("TOURNAMENT_BOARD", raising=False)
        tor = _tournament()
        tor["endDate"] = _end_date(5.0)
        snap = tbl.snapshot(http_get=_snap_http(tor=tor), use_cache=False)
        assert 4.0 < snap["days_left"] < 5.01
        assert snap["tournament"]["end_date"] == tor["endDate"]

    def test_days_left_senza_fuso_non_indovina(self):
        # Un istante ambiguo non si indovina: None (lezione del 15/09).
        assert tbl._days_left("2026-10-20T13:00:00") is None

    def test_days_left_iso_rotto(self):
        assert tbl._days_left("non-una-data") is None
        assert tbl._days_left(None) is None

    def test_days_left_Z(self):
        future = datetime.now(timezone.utc) + timedelta(days=2)
        raw = future.strftime("%Y-%m-%dT%H:%M:%S") + "Z"
        assert tbl._days_left(raw) is not None


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

class TestReport:
    def test_report_felice(self, monkeypatch):
        monkeypatch.delenv("TOURNAMENT_BOARD", raising=False)
        text = tbl.format_report(tbl.snapshot(http_get=_snap_http(),
                                              use_cache=False))
        assert "CLASSIFICA" in text
        assert "#101" in text
        assert "191.28" in text                  # taglio
        assert "RETURN" in text                  # nota sul formato

    def test_report_non_disponibile(self):
        text = tbl.format_report({"ok": False, "error": "rete giu'"})
        assert "non disponibile" in text

    def test_report_utente_assente(self, monkeypatch):
        monkeypatch.setenv("TOURNAMENT_USERNAME", "Fantasma")
        text = tbl.format_report(tbl.snapshot(http_get=_snap_http(),
                                              use_cache=False))
        assert "nessuna riga" in text

    def test_report_in_zona_premi(self, monkeypatch):
        monkeypatch.delenv("TOURNAMENT_BOARD", raising=False)
        snap = tbl.snapshot(http_get=_snap_http(me_rank=20, me_result=250.0),
                            use_cache=False)
        text = tbl.format_report(snap)
        assert "zona premi" in text
        assert "Premio" in text

    def test_report_mai_eccezioni(self):
        class Rotto(dict):
            def get(self, *a, **k):
                raise RuntimeError("rotto")

        assert "non disponibile" in tbl.format_report(Rotto())

    def test_report_chiusura_e_leader(self, monkeypatch):
        monkeypatch.delenv("TOURNAMENT_BOARD", raising=False)
        text = tbl.format_report(tbl.snapshot(http_get=_snap_http(),
                                              use_cache=False))
        assert "Tempo rimasto" in text
        assert "thadog90" in text


# ---------------------------------------------------------------------------
# Cablaggio nel bot + IaC
# ---------------------------------------------------------------------------

class TestCablaggio:
    def test_job_e_comando_presenti(self):
        src = Path("bot.py").read_text(encoding="utf-8")
        assert "async def tournament_board_job" in src
        assert "async def cmd_torneo" in src

    def test_handler_registrati(self):
        src = Path("bot.py").read_text(encoding="utf-8")
        assert 'CommandHandler("torneo", cmd_torneo)' in src
        assert 'CommandHandler("classifica", cmd_torneo)' in src

    def test_job_registrato_con_antispam(self):
        src = Path("bot.py").read_text(encoding="utf-8")
        assert "run_repeating(tournament_board_job" in src
        assert "max_instances" in src
        # anti-spam sulla chiave dedicata
        assert 'is_notified("T_BOARD", slot)' in src
        assert 'mark_notified("T_BOARD", slot)' in src

    def test_intervallo_da_env(self):
        src = Path("bot.py").read_text(encoding="utf-8")
        assert "TOURNAMENT_BOARD_INTERVAL_MIN" in src

    def test_help_documenta_il_comando(self):
        src = Path("bot.py").read_text(encoding="utf-8")
        assert "`/torneo`" in src

    def test_env_dichiarate_in_iac(self):
        iac = Path(".railway/railway.ts").read_text(encoding="utf-8")
        for name in ("TOURNAMENT_BOARD", "TOURNAMENT_BOARD_INTERVAL_MIN",
                     "TOURNAMENT_BOARD_NOTIFY_HOURS", "TOURNAMENT_BOARD_TTL_S",
                     "TOURNAMENT_BOARD_TIMEOUT_S", "TOURNAMENT_USERNAME",
                     "TOURNAMENT_TOP_RANKS", "SX_TOURNAMENT_API_BASE",
                     "SX_TOURNAMENT_UA"):
            assert name in iac, f"env non dichiarata nella IaC: {name}"

    def test_ogni_env_letta_e_dichiarata(self):
        # Tripwire: ogni env letta dal modulo deve stare nella IaC, altrimenti
        # un `config apply` distrugge un override operativo in silenzio.
        src = Path("tournament_leaderboard.py").read_text(encoding="utf-8")
        iac = Path(".railway/railway.ts").read_text(encoding="utf-8")
        for name in ("TOURNAMENT_BOARD", "SX_TOURNAMENT_API_BASE",
                     "SX_TOURNAMENT_UA", "TOURNAMENT_USERNAME",
                     "TOURNAMENT_TOP_RANKS", "TOURNAMENT_BOARD_TTL_S",
                     "TOURNAMENT_BOARD_TIMEOUT_S",
                     "TOURNAMENT_BOARD_NOTIFY_HOURS", "TOURNAMENT_LABEL"):
            # La lettura puo' passare da `os.getenv` diretto o dall'helper
            # `_num_env` (che riceve il nome come stringa): si verifica il
            # NOME, non la forma della chiamata.
            assert f'"{name}"' in src, f"{name} non letta dal modulo"
            assert name in iac, f"{name} non dichiarata nella IaC"


# ---------------------------------------------------------------------------
# Tripwire: il lettore e' TELEMETRIA (nessun ordine, nessuna scrittura, leggero)
# ---------------------------------------------------------------------------

class TestTripwire:
    def test_nessun_ordine_o_scrittura_nel_codice(self):
        src = Path("tournament_leaderboard.py").read_text(encoding="utf-8")
        code = src.split('"""', 2)[-1]           # esclude la docstring di testa
        for token in ("place_limit_order", "OrderResult", "_live_fill",
                      "resolve_market_for", "save_bet", "save_prediction",
                      "INSERT", "UPDATE", "DELETE", "sqlite3"):
            assert token not in code, f"token di scrittura/ordine trovato: {token}"

    def test_import_leggero(self):
        code = ("import tournament_leaderboard as t;"
                "import sys;"
                "bad=[m for m in ('tracker','auto_bet','bot','requests',"
                "'execution_engine','odds_api') if m in sys.modules];"
                "print(','.join(bad))")
        out = subprocess.run([sys.executable, "-c", code],
                             capture_output=True, text=True, cwd=str(Path.cwd()))
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == ""

    def test_runtime_non_usa_reti_quando_http_iniettato(self, monkeypatch):
        # Con il trasporto iniettato nessuna richiesta reale: il fake conta le
        # chiamate e `requests` non viene mai importato dal modulo.
        monkeypatch.delenv("TOURNAMENT_BOARD", raising=False)
        calls = []
        tbl.snapshot(http_get=_snap_http(calls=calls), use_cache=False)
        assert calls and all("api.sx.bet" in c["url"] for c in calls)

    def test_readme_module_dichiara_zero_crediti(self):
        head = Path("tournament_leaderboard.py").read_text(
            encoding="utf-8").split('"""', 2)[1]
        assert "ZERO CREDITI" in head
        assert "User-Agent" in head
