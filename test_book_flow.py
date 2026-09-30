"""Test del rilevatore di flusso dell'order book SX (`book_flow.py`, 26/09/2026).

Tutti OFFLINE: nessuna rete, nessun provider, nessun ordine, nessuna
credenziale. Il modulo e' **TELEMETRIA** e i test lo blindano: non importa
`auto_bet`/`tracker`/`bot` (verifica in sottoprocesso) e non contiene alcuna
chiamata di esecuzione.
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import book_flow


# ---------------------------------------------------------------------------
# Fixture: stato e registro SEMPRE in tmp (mai sul volume reale)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(book_flow, "STATE_PATH",
                        tmp_path / "execution" / "book_flow_state.json")
    monkeypatch.setattr(book_flow, "LOG_PATH",
                        tmp_path / "execution" / "book_flow_events.jsonl")
    monkeypatch.setattr(book_flow, "MIN_INGRESS_USDC", 50.0)
    monkeypatch.setattr(book_flow, "MIN_JUMP_PCT", 0.35)
    monkeypatch.setattr(book_flow, "DEDUP_MIN", 30.0)
    monkeypatch.setattr(book_flow, "MAX_KEYS", 2000)
    yield


def _entry(depth: float, levels=None) -> dict:
    """Book nel formato di `sx_signals._book` (`depth` + `levels`)."""
    lv = levels if levels is not None else [(1.70, depth)]
    return {"depth": depth,
            "best": {"price": lv[0][0], "size": lv[0][1]},
            "levels": [{"price": p, "size": s} for p, s in lv]}


def _ts(minutes_ago: float = 0.0) -> datetime:
    return datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)


# ---------------------------------------------------------------------------
# 1. detect_ingress — funzione PURA, nessun I/O
# ---------------------------------------------------------------------------

class TestDetectIngress:
    def test_prima_osservazione_non_e_un_ingresso(self):
        """Senza un termine di paragone il book non 'si sta riempiendo':
        dichiararlo al primo giro riempirebbe il registro di tutto il
        palinsesto."""
        assert book_flow.detect_ingress(None, _entry(500)) is None

    def test_riempimento_anomalo(self):
        found = book_flow.detect_ingress(_entry(100), _entry(200))
        assert found and found["reason"] == "depth_ingress"
        assert found["delta"] == pytest.approx(100.0)
        assert found["jump_pct"] == pytest.approx(1.0)

    def test_book_profondo_non_e_un_ingresso(self):
        """+100 USDC su 1000 e' +10%: sotto la soglia relativa NON e' notizia
        (i mercati profondi non riempiono il registro a ogni rimbalzo)."""
        assert book_flow.detect_ingress(_entry(1000), _entry(1100)) is None

    def test_sotto_la_soglia_assoluta(self):
        assert book_flow.detect_ingress(_entry(100), _entry(120)) is None

    def test_book_che_si_SVUOTA_non_e_un_ingresso(self):
        assert book_flow.detect_ingress(_entry(300), _entry(150)) is None

    def test_livello_nuovo_fuori_scala(self):
        """La seconda firma: un prezzo che PRIMA non c'era, con size fuori
        scala — e' l'ordine massiccio che entra, non il riempimento del book."""
        prev = _entry(1000, levels=[(1.70, 1000.0)])
        cur = _entry(1010, levels=[(1.70, 1010.0), (1.60, 80.0)])
        found = book_flow.detect_ingress(prev, cur)
        assert found and found["reason"] == "new_level"
        assert found["level_price"] == pytest.approx(1.60)
        assert found["level_size"] == pytest.approx(80.0)

    def test_livello_nuovo_ma_piccolo_ignorato(self):
        prev = _entry(1000, levels=[(1.70, 1000.0)])
        cur = _entry(1005, levels=[(1.70, 1005.0), (1.60, 5.0)])
        assert book_flow.detect_ingress(prev, cur) is None

    def test_livelli_malformati_ignorati(self):
        """Un book sporco non e' un errore del rilevatore."""
        prev = {"depth": 100, "levels": [{"price": "boh", "size": None}, 42]}
        cur = {"depth": 300, "levels": ["x", {"price": 1.5, "size": 10.0}]}
        found = book_flow.detect_ingress(prev, cur)
        assert found and found["reason"] == "depth_ingress"

    def test_forme_dello_stato_dopo_il_giro_json(self):
        """Dopo un salvataggio i livelli sono LISTE [prezzo, size]: il
        rilevatore deve leggerli (altrimenti la firma 'livello nuovo' non
        scatterebbe mai dal secondo giro in poi)."""
        prev = {"depth": 1000, "levels": [[1.70, 1000.0]]}
        cur = {"depth": 1010, "levels": [[1.70, 1010.0], [1.60, 90.0]]}
        found = book_flow.detect_ingress(prev, cur)
        assert found and found["reason"] == "new_level"

    def test_soglie_override(self):
        assert book_flow.detect_ingress(
            _entry(100), _entry(130), min_ingress_usdc=10,
            min_jump_pct=0.10) is not None


# ---------------------------------------------------------------------------
# 2. observe_books — lotto: uno stato, un salvataggio
# ---------------------------------------------------------------------------

class TestObserveBooks:
    def test_primo_giro_solo_stato(self):
        events = book_flow.observe_books([("m1", 1, _entry(100), None)])
        assert events == []
        assert "m1|1" in book_flow.load_state()

    def test_secondo_giro_registra_l_ingresso_con_contesto(self):
        ctx = {"home": "Osasuna", "away": "Getafe", "league": "La Liga",
               "market": "1X2"}
        book_flow.observe_books([("m1", 1, _entry(100), ctx)])
        events = book_flow.observe_books([("m1", 1, _entry(300), ctx)])
        assert len(events) == 1
        evt = events[0]
        assert evt["reason"] == "depth_ingress"
        assert evt["home"] == "Osasuna" and evt["league"] == "La Liga"
        assert evt["market_id"] == "m1" and evt["selection"] == 1

    def test_book_stabile_non_registra(self):
        book_flow.observe_books([("m1", 1, _entry(200), None)])
        assert book_flow.observe_books([("m1", 1, _entry(200), None)]) == []

    def test_dedup_dentro_la_finestra(self):
        """Lo stesso mercato che continua a riempirsi NON e' un secondo
        ingresso: il giro gira spesso e il registro non deve gonfiarsi."""
        book_flow.observe_books([("m1", 1, _entry(100), None)])
        assert len(book_flow.observe_books([("m1", 1, _entry(300), None)])) == 1
        assert book_flow.observe_books([("m1", 1, _entry(600), None)]) == []
        # ...e il dedup NON si "dimentica" al giro dopo (altrimenti il giro
        # successivo registrerebbe di nuovo lo stesso ingresso).
        assert book_flow.observe_books([("m1", 1, _entry(900), None)]) == []

    def test_dedup_scaduto_registra_di_nuovo(self):
        book_flow.observe_books([("m1", 1, _entry(100), None)],
                                ts=_ts(minutes_ago=60))
        assert len(book_flow.observe_books([("m1", 1, _entry(300), None)],
                                          ts=_ts(minutes_ago=45))) == 1
        assert len(book_flow.observe_books([("m1", 1, _entry(600), None)],
                                          ts=_ts(minutes_ago=0))) == 1

    def test_book_rotto_saltato(self):
        events = book_flow.observe_books([
            ("m1", 1, {"error": "timeout"}, None),
            ("m2", 1, {"depth": 10}, None),
        ])
        assert events == []
        assert "m1|1" not in book_flow.load_state()

    def test_esiti_diversi_sono_chiavi_diverse(self):
        book_flow.observe_books([("m1", 1, _entry(100), None),
                                 ("m1", 2, _entry(100), None)])
        events = book_flow.observe_books([("m1", 1, _entry(300), None),
                                          ("m1", 2, _entry(300), None)])
        assert len(events) == 2

    def test_observe_books_from_scan(self):
        books = {"h1": {1: _entry(100), 2: _entry(100)}}
        assert book_flow.observe_books_from_scan(books) == []
        assert len(book_flow.observe_books_from_scan(
            {"h1": {1: _entry(300), 2: _entry(300)}})) == 2

    def test_stato_limitato(self, monkeypatch):
        monkeypatch.setattr(book_flow, "MAX_KEYS", 3)
        items = [(f"m{i}", 1, _entry(100 + i), None) for i in range(6)]
        book_flow.observe_books(items)
        assert len(book_flow.load_state()) <= 3


# ---------------------------------------------------------------------------
# 3. Fail-safe: nessuna funzione solleva, mai
# ---------------------------------------------------------------------------

class TestFailSafe:
    def test_stato_corrotto_riparte_da_zero(self):
        book_flow.STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        book_flow.STATE_PATH.write_text("{non json", encoding="utf-8")
        assert book_flow.load_state() == {}
        assert book_flow.observe_books([("m1", 1, _entry(100), None)]) == []
        assert "m1|1" in book_flow.load_state()

    def test_log_non_scrivibile_non_propaga(self, tmp_path, monkeypatch):
        blocker = tmp_path / "not_a_dir"
        blocker.write_text("x", encoding="utf-8")
        monkeypatch.setattr(book_flow, "LOG_PATH",
                            blocker / "nested" / "events.jsonl")
        monkeypatch.setattr(book_flow, "STATE_PATH",
                            tmp_path / "execution" / "state.json")
        book_flow.observe_books([("m1", 1, _entry(100), None)])
        events = book_flow.observe_books([("m1", 1, _entry(300), None)])
        assert len(events) == 1 and "error" in events[0]   # segnalato, non propagato

    def test_stato_non_scrivibile_non_propaga(self, tmp_path, monkeypatch):
        blocker = tmp_path / "blocker"
        blocker.write_text("x", encoding="utf-8")
        monkeypatch.setattr(book_flow, "STATE_PATH",
                            blocker / "nested" / "state.json")
        assert book_flow.save_state({"k": {}}) is False

    def test_registro_inesistente(self):
        assert book_flow.iter_events(days=7) == []
        assert book_flow.summary(days=7)["events"] == 0
        assert book_flow.format_report(days=7) is None

    def test_righe_corrotte_ignorate(self):
        book_flow.observe_books([("m1", 1, _entry(100), None)])
        book_flow.observe_books([("m1", 1, _entry(300), None)])
        with book_flow.LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write("non-json\n\n{\"rotto\": \n")
        assert book_flow.summary(days=7)["events"] == 1


# ---------------------------------------------------------------------------
# 4. Riepilogo e report
# ---------------------------------------------------------------------------

class TestReport:
    def test_riepilogo_conta_firme_leghe_mercati(self):
        ctx = {"home": "A", "away": "B", "league": "Serie A", "market": "AH"}
        book_flow.observe_books([("m1", 1, _entry(100), ctx)])
        book_flow.observe_books([("m1", 1, _entry(300), ctx)])
        s = book_flow.summary(days=7)
        assert s["events"] == 1
        assert s["by_reason"] == {"depth_ingress": 1}
        assert s["by_league"] == {"Serie A": 1}
        assert s["by_market"] == {"AH": 1}
        assert s["total_delta_usdc"] == pytest.approx(200.0)
        assert s["thresholds"]["min_ingress_usdc"] == pytest.approx(50.0)

    def test_finestra_temporale(self):
        # ~2 giorni fa: fuori dalla finestra di 1 giorno, dentro quella di 30.
        book_flow.observe_books([("m1", 1, _entry(100), None)],
                                ts=_ts(minutes_ago=2 * 24 * 60))
        book_flow.observe_books([("m1", 1, _entry(300), None)],
                                ts=_ts(minutes_ago=2 * 24 * 60 - 10))
        assert book_flow.summary(days=1)["events"] == 0
        assert book_flow.summary(days=30)["events"] == 1

    def test_report_dichiara_che_e_telemetria(self):
        book_flow.observe_books([("m1", 1, _entry(100), None)])
        book_flow.observe_books([("m1", 1, _entry(300), None)])
        text = book_flow.format_report(days=1)
        assert "Flusso book SX" in text
        assert "TELEMETRIA" in text and "nessun ordine" in text


# ---------------------------------------------------------------------------
# 5. Perimetro: TELEMETRIA, non ordini (tripwire sul SORGENTE)
# ---------------------------------------------------------------------------

class TestPerimetro:
    def test_niente_esecuzione_nel_sorgente(self):
        """Tripwire sull'AST, non sul testo: la docstring PUO' nominare
        `auto_bet` (e' il perimetro che dichiara), il codice no."""
        import ast
        tree = ast.parse(Path("book_flow.py").read_text(encoding="utf-8"))
        banned_mods = {"auto_bet", "tracker", "bot", "sx_signals",
                       "multi_market", "execution_engine", "requests",
                       "websocket", "websockets", "centrifuge", "aiohttp"}
        banned_calls = {"_live_fill", "place_order", "save_bet",
                        "resolve_market_for", "save_prediction",
                        "save_market_quotes"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] not in banned_mods, alias.name
            elif isinstance(node, ast.ImportFrom):
                mod = (node.module or "").split(".")[0]
                assert mod not in banned_mods, node.module
            elif isinstance(node, ast.Call):
                name = getattr(node.func, "attr", None) \
                    or getattr(node.func, "id", None)
                assert name not in banned_calls, name

    def test_niente_rete(self):
        """Nessun client di rete: il rilevatore legge solo lo stato/registro
        che gli altri moduli popolano (nessuna dipendenza nuova, nessun WS)."""
        import ast
        tree = ast.parse(Path("book_flow.py").read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        assert not imported & {"requests", "urllib", "socket", "httpx",
                               "websocket", "websockets", "centrifuge",
                               "aiohttp"}

    def test_import_non_carica_la_produzione(self):
        code = ("import sys, book_flow;"
                "print(sorted(m for m in ('auto_bet','tracker','bot',"
                "'sx_signals','multi_market','execution_engine') "
                "if m in sys.modules))")
        out = subprocess.run([sys.executable, "-c", code],
                             capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == "[]"


# ---------------------------------------------------------------------------
# 6. Innesto: il rilevatore vive dove i book sono GIA' in mano
# ---------------------------------------------------------------------------

class TestInnesto:
    def test_book_espone_i_livelli(self):
        """`sx_signals._book` e' la fonte: senza `levels` il rilevatore non
        avrebbe nulla da confrontare, e non si aprono letture in piu'."""
        import sx_signals

        class _Prov:
            def _get(self, path, params=None):
                return {"data": {
                    "outcomeOne": [{"percentageOdds": 10 ** 20 // 2,
                                    "size": 40 * 10 ** 6}],
                    "outcomeTwo": [{"percentageOdds": 10 ** 20 // 3,
                                    "size": 10 * 10 ** 6}]}}

        out = sx_signals._book(_Prov(), "h1")
        assert out[1]["levels"] and out[1]["levels"][0]["price"] == pytest.approx(2.0)
        assert out[1]["levels"][0]["size"] == pytest.approx(40.0)
        assert out[1]["best"] and out[1]["depth"] == pytest.approx(40.0)
        assert sx_signals.BOOK_LEVELS_KEPT >= 1

    def test_livelli_limitati(self):
        import sx_signals
        assert isinstance(sx_signals.BOOK_LEVELS_KEPT, int)
        assert sx_signals.BOOK_LEVELS_KEPT <= 20

    def test_scan_e_ingest_chiamano_il_rilevatore(self):
        assert "book_flow" in Path("sx_signals.py").read_text(encoding="utf-8")
        assert "book_flow" in Path("multi_market.py").read_text(encoding="utf-8")

    def test_job_e_sezione_nel_report(self):
        src = Path("bot.py").read_text(encoding="utf-8")
        assert "async def book_flow_job" in src
        assert "run_repeating(book_flow_job" in src
        assert "Flusso book SX" in src

    def test_iac_dichiara_le_env(self):
        iac = Path(".railway/railway.ts").read_text(encoding="utf-8")
        for name in ("BOOK_FLOW_MIN_SIZE_USDC", "BOOK_FLOW_MIN_JUMP_PCT",
                     "BOOK_FLOW_DEDUP_MIN", "BOOK_FLOW_MAX_KEYS",
                     "BOOK_FLOW_STATE", "BOOK_FLOW_LOG", "SX_BOOK_LEVELS_KEPT"):
            assert name in iac, f"{name} non dichiarata in preserve()"


# ---------------------------------------------------------------------------
# 7. Copertura OU/AH (direttiva 26/09): piu' linee, STESSI gate
# ---------------------------------------------------------------------------

class TestCoperturaMultiMercato:
    def test_limiti_alzati(self):
        """4 -> 6: la copertura non deve tornare indietro in silenzio."""
        import multi_market
        assert multi_market.MAX_LINES_PER_MARKET >= 20
        assert multi_market.MAX_RAW_MARKETS >= 600

    def test_gate_di_strategia_non_toccati(self):
        """Allargare la COPERTURA non e' allentare la STRATEGIA: fascia
        quota, edge e EV restano quelli congelati del 22/09."""
        import multi_market
        from market_calib import MARKET_EDGE_MIN
        from value_filter import EV_MIN, ODDS_MAX, ODDS_MIN
        assert (ODDS_MIN, ODDS_MAX) == (1.30, 1.80)
        assert EV_MIN == pytest.approx(0.025)
        assert MARKET_EDGE_MIN == pytest.approx(0.02)
        assert multi_market.MIN_EXEC_DEPTH_USDC >= 20.0
        assert multi_market.MIN_DEPTH_USDC >= 20.0
