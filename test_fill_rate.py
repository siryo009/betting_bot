"""Test del fill rate PER PICK (17/10/2026, `fill_rate.py`).

Copre:
  1. classificazione dell'esito di un tentativo (tutti i rami di `_live_fill`);
  2. la metrica che conta: **per pick** contro **per invio** (un pick che si
     riempie al 15o tentativo NON deve contare come 14 fallimenti);
  3. scrittura fail-safe (log non scrivibile -> `error`, mai eccezioni);
  4. `from_log` su un estratto di log storico (stessa metrica, altra fonte);
  5. integrazione col wrapper `auto_bet._live_fill` (registra SENZA cambiare
     il valore di ritorno, con `_live_fill_impl` sostituito: zero rete);
  6. tripwire: diagnostica pura (nessun ordine, nessun import pesante).

TUTTI OFFLINE: nessuna rete, nessun provider, nessun credito. Il log sta nella
tmp (isolamento in `conftest.py`).
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

import fill_rate
import auto_bet


@pytest.fixture(autouse=True)
def _enabled(tmp_path, monkeypatch):
    """Telemetria ACCESA e log nella tmp (il conftest la spegne altrove)."""
    monkeypatch.setenv("FILL_RATE_ENABLED", "1")
    monkeypatch.setenv("FILL_RATE_LOG", str(tmp_path / "attempts.jsonl"))


def _pick(**kw):
    base = {"match_id": "sx-1", "home": "Norwich", "away": "Portsmouth",
            "esito_key": "1", "mercato": "1X2", "league": "EFL Championship",
            "quota": 1.94, "best_ev": 0.03}
    base.update(kw)
    return base


def _no_fill(status="CANCELLED"):
    return {"ok": False, "market_id": "0xabc", "selection_id": 1,
            "status": status, "error": "nessun match"}


def _filled(stake=2.79, mid="0xabc"):
    return {"ok": True, "market_id": mid, "selection_id": 1,
            "bet_id": "0xorder", "status": "FULLY_FILLED",
            "price": 1.94, "stake": stake}


# ---------------------------------------------------------------------------
# 1. Classificazione
# ---------------------------------------------------------------------------

class TestClassify:
    def test_riempimento(self):
        assert fill_rate.classify(_filled(), 2.79) == "filled"

    def test_riempimento_parziale(self):
        assert fill_rate.classify(_filled(stake=1.56), 7.35) == "partial"

    def test_inviato_non_riempito(self):
        assert fill_rate.classify(_no_fill(), 2.79) == "no_fill"

    def test_riempito_ma_senza_bet_id(self):
        res = {"ok": False, "market_id": "0xabc", "selection_id": 1,
               "bet_id": None, "status": "FULLY_FILLED", "stake": 2.79}
        assert fill_rate.classify(res, 2.79) == "no_bet_id"

    def test_ordine_resting(self):
        res = {"ok": True, "resting": True, "market_id": "0xabc",
               "status": "RESTING_OPEN", "price": 1.94}
        assert fill_rate.classify(res, 2.79) == "resting_open"

    def test_nessun_ordine_inviato(self):
        assert fill_rate.classify(None, 2.79) == "skipped"

    def test_trace_forzato_dall_impl(self):
        """`auto_bet` marca l'errore del provider: esito IGNOTO, non salto."""
        assert fill_rate.classify(None, 2.79, {"outcome": "error"}) == "error"

    def test_input_ostile_non_solleva(self):
        assert fill_rate.classify({"ok": "forse"}, "abc") in (
            "filled", "partial", "no_fill")

    def test_esito_ignoto_tracciato_da_trace(self):
        assert fill_rate.classify({"ok": True, "stake": 1.0}, 1.0,
                                  {"outcome": "resting_open"}) == "resting_open"


# ---------------------------------------------------------------------------
# 2. La metrica: PER PICK, non per invio
# ---------------------------------------------------------------------------

class TestPerPickNonPerInvio:
    def test_la_metrica_che_ha_motivato_il_modulo(self):
        """1 pick, 14 invii a vuoto, 1 invio riempito (il caso reale 10/07).

        Il metrico storico ("riempiti / POST") diceva 1/15 = 6,7%; la lettura
        corretta e' "questo pick E' stato eseguito" = 100%.
        """
        for _ in range(14):
            fill_rate.record_attempt(_pick(), 2.79, 1.94, _no_fill())
        fill_rate.record_attempt(_pick(), 2.79, 1.94, _filled())
        s = fill_rate.summarize(fill_rate.iter_attempts(days=7))
        assert s["picks_attempted"] == 1
        assert s["picks_filled"] == 1
        assert s["fill_rate_per_pick"] == 100.0
        assert s["attempts_total"] == 15
        assert s["attempts_per_pick"]["avg"] == 15.0
        assert s["attempts_per_pick"]["max"] == 15
        assert s["fill_rate_per_attempt"] == pytest.approx(6.67, abs=0.01)

    def test_pick_mai_riempito_resta_nel_denominatore(self):
        for _ in range(3):
            fill_rate.record_attempt(_pick(), 2.0, 1.9, _no_fill())
        s = fill_rate.summarize(fill_rate.iter_attempts())
        assert s["picks_attempted"] == 1 and s["picks_filled"] == 0
        assert s["fill_rate_per_pick"] == 0.0

    def test_due_pick_uno_si_e_uno_no(self):
        fill_rate.record_attempt(_pick(match_id="sx-1"), 2.0, 1.9, _filled())
        fill_rate.record_attempt(_pick(match_id="sx-2"), 2.0, 1.9, _no_fill())
        fill_rate.record_attempt(_pick(match_id="sx-2"), 2.0, 1.9, _no_fill())
        s = fill_rate.summarize(fill_rate.iter_attempts())
        assert (s["picks_attempted"], s["picks_filled"]) == (2, 1)
        assert s["fill_rate_per_pick"] == 50.0
        assert s["attempts_total"] == 3

    def test_esito_diverso_e_pick_diverso(self):
        """Stessa partita, esiti diversi = due pick distinti."""
        fill_rate.record_attempt(_pick(esito_key="1"), 2.0, 1.9, _filled())
        fill_rate.record_attempt(_pick(esito_key="2"), 2.0, 1.9, _no_fill())
        s = fill_rate.summarize(fill_rate.iter_attempts())
        assert (s["picks_attempted"], s["picks_filled"]) == (2, 1)

    def test_salto_non_e_un_tentativo(self):
        """Un pick saltato PRIMA del POST non abbassa il fill rate."""
        fill_rate.record_attempt(_pick(match_id="sx-1"), 2.0, 1.9, None)
        fill_rate.record_attempt(_pick(match_id="sx-2"), 2.0, 1.9, _filled())
        s = fill_rate.summarize(fill_rate.iter_attempts())
        assert s["picks_attempted"] == 1
        assert s["fill_rate_per_pick"] == 100.0
        assert s["skipped_picks"] == 1
        assert s["by_outcome"]["skipped"] == 1

    def test_resting_conta_come_tentativo_non_come_fill(self):
        res = {"ok": True, "resting": True, "market_id": "0xabc",
               "status": "RESTING_OPEN"}
        fill_rate.record_attempt(_pick(), 2.0, 1.9, res)
        s = fill_rate.summarize(fill_rate.iter_attempts())
        assert s["picks_attempted"] == 1 and s["picks_filled"] == 0
        assert s["by_outcome"]["resting_open"] == 1

    def test_histogramma_dei_retry(self):
        for i, n in enumerate([1, 2, 4, 8, 20]):
            for _ in range(n):
                fill_rate.record_attempt(_pick(match_id=f"sx-{i}"), 2.0, 1.9,
                                         _no_fill())
        s = fill_rate.summarize(fill_rate.iter_attempts())
        assert s["retry_histogram"] == {"1": 1, "2": 1, "3-5": 1,
                                        "6-10": 1, ">10": 1}

    def test_motivo_dei_non_riempiti(self):
        fill_rate.record_attempt(_pick(), 2.0, 1.9, _no_fill("CANCELLED"))
        fill_rate.record_attempt(_pick(), 2.0, 1.9, _no_fill("FAILURE"))
        s = fill_rate.summarize(fill_rate.iter_attempts())
        assert s["no_fill_status"] == {"CANCELLED": 1, "FAILURE": 1}

    def test_fill_rate_per_corsia(self):
        fill_rate.record_attempt(_pick(mercato="TENNIS", match_id="sx-t1"),
                                 2.0, 1.9, _filled())
        fill_rate.record_attempt(_pick(mercato="TENNIS", match_id="sx-t2"),
                                 2.0, 1.9, _no_fill())
        fill_rate.record_attempt(_pick(gridiron_lane=True, match_id="sx-g1"),
                                 2.0, 1.9, _no_fill())
        s = fill_rate.summarize(fill_rate.iter_attempts())
        assert s["by_lane"]["tennis"]["fill_rate_per_pick"] == 50.0
        assert s["by_lane"]["tennis"]["attempts_total"] == 2
        assert s["by_lane"]["gridiron"]["picks_attempted"] == 1

    def test_pick_senza_identita_contato_a_parte(self):
        # Ne' il pick ne' l'esito portano un'identita': non e' attribuibile a
        # nessun pick, quindi non entra nel denominatore (contato a parte).
        sans = {"ok": True, "status": "FULLY_FILLED", "stake": 2.0,
                "bet_id": "0xorder"}
        fill_rate.record_attempt({"mercato": "1X2"}, 2.0, 1.9, sans)
        s = fill_rate.summarize(fill_rate.iter_attempts())
        assert s["unkeyed"] == 1 and s["picks_attempted"] == 0

    def test_fallback_su_identita_di_mercato(self):
        """Senza match_id il pick resta aggregabile per market_id."""
        pick = {"mercato": "1X2", "market_id": "0xdead", "selection_id": 2}
        fill_rate.record_attempt(pick, 2.0, 1.9, _filled())
        s = fill_rate.summarize(fill_rate.iter_attempts())
        assert s["picks_attempted"] == 1 and s["unkeyed"] == 0

    def test_campione_sotto_soglia_non_e_affidabile(self):
        fill_rate.record_attempt(_pick(), 2.0, 1.9, _filled())
        s = fill_rate.summarize(fill_rate.iter_attempts())
        assert s["reliable"] is False and s["min_attempts"] == 30

    def test_campione_oltre_soglia_affidabile(self):
        for i in range(30):
            fill_rate.record_attempt(_pick(match_id=f"sx-{i}"), 2.0, 1.9,
                                     _filled())
        s = fill_rate.summarize(fill_rate.iter_attempts())
        assert s["reliable"] is True and s["fill_rate_per_pick"] == 100.0

    def test_nessun_tentativo(self):
        s = fill_rate.summarize([])
        assert s["fill_rate_per_pick"] is None and s["picks_attempted"] == 0


# ---------------------------------------------------------------------------
# 3. Scrittura fail-safe + interruttore
# ---------------------------------------------------------------------------

class TestScrittura:
    def test_evento_scritto_con_identita_e_corsia(self):
        evt = fill_rate.record_attempt(_pick(), 2.79, 1.94, _filled())
        assert evt["outcome"] == "filled" and evt["lane"] == "football"
        assert evt["match_id"] == "sx-1" and evt["esito"] == "1"
        assert evt["stake"] == 2.79 and evt["stake_filled"] == 2.79
        assert evt["bet_id"] == "0xorder" and evt["league"] == "EFL Championship"
        disk = [json.loads(l) for l in
                Path(fill_rate.log_path()).read_text().splitlines()]
        assert len(disk) == 1 and disk[0]["outcome"] == "filled"

    def test_log_non_scrivibile_non_solleva(self, monkeypatch, tmp_path):
        """Percorso impossibile (una directory): `error`, mai eccezioni."""
        monkeypatch.setenv("FILL_RATE_LOG", str(tmp_path))
        evt = fill_rate.record_attempt(_pick(), 2.0, 1.9, _filled())
        assert evt["outcome"] == "filled"
        assert evt["error"]

    def test_interruttore_spento_non_scrive(self, monkeypatch):
        monkeypatch.setenv("FILL_RATE_ENABLED", "0")
        evt = fill_rate.record_attempt(_pick(), 2.0, 1.9, _filled())
        assert evt.get("skipped_telemetry") is True
        assert not Path(fill_rate.log_path()).exists()

    def test_interruttore_default_acceso(self, monkeypatch):
        monkeypatch.delenv("FILL_RATE_ENABLED", raising=False)
        assert fill_rate.enabled() is True
        for off in ("0", "false", "no", "off", "OFF"):
            monkeypatch.setenv("FILL_RATE_ENABLED", off)
            assert fill_rate.enabled() is False

    def test_percorso_letto_a_runtime(self, monkeypatch, tmp_path):
        monkeypatch.setenv("FILL_RATE_LOG", str(tmp_path / "x.jsonl"))
        assert fill_rate.log_path() == tmp_path / "x.jsonl"

    def test_righe_corrotte_ignorate(self):
        p = Path(fill_rate.log_path())
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{non json}\n\n" + json.dumps(
            {"ts_epoch": 1, "outcome": "filled", "match_id": "a",
             "esito": "1"}) + "\n")
        evts = fill_rate.iter_attempts()
        assert len(evts) == 1 and evts[0]["outcome"] == "filled"


# ---------------------------------------------------------------------------
# 4. from_log: la stessa metrica da un estratto di log
# ---------------------------------------------------------------------------

_LOG = """2026-10-08 02:00:00,000 - auto_bet - WARNING - auto_bet: ordine reale 0xaaa non riempito (Alfa vs Beta, CANCELLED): nessun match
2026-10-08 02:01:00,000 - auto_bet - WARNING - auto_bet: ordine reale 0xaaa non riempito (Alfa vs Beta, CANCELLED): nessun match
2026-10-08 02:02:00,000 - auto_bet - WARNING - auto_bet: ordine reale 0xaaa non riempito (Alfa vs Beta, CANCELLED): nessun match
2026-10-08 02:03:00,000 - auto_bet - INFO - auto_bet: ORDINE REALE riempito 0xbbb (Gamma vs Delta, 1) @ 1.94 per \u20ac2.79 [FULLY_FILLED]
2026-10-08 02:04:00,000 - auto_bet - INFO - auto_bet: liquidita' 3.00 < richiesta 4.00 al floor 1.90, salto (rischio slippage)
"""


class TestFromLog:
    def test_metrica_per_pick_da_un_estratto(self, tmp_path):
        f = tmp_path / "log.txt"
        f.write_text(_LOG)
        res = fill_rate.from_log(str(f))
        assert res["picks_attempted"] == 2 and res["picks_filled"] == 1
        assert res["attempts_total"] == 4
        assert res["fill_rate_per_pick"] == 50.0
        assert res["fill_rate_per_attempt"] == 25.0
        assert res["attempts_per_pick"]["max"] == 3

    def test_una_riga_di_salto_non_e_un_tentativo(self, tmp_path):
        res = fill_rate.from_log(
            "2026-10-08 - auto_bet - INFO - auto_bet: liquidita' 3.00 < "
            "richiesta 4.00 al floor 1.90, salto (rischio slippage)")
        assert res["picks_attempted"] == 0 and res["fill_rate_per_pick"] is None

    def test_testo_multiriga_non_e_un_percorso(self):
        """Regressione: `Path.exists()` su un testo lungo solleva
        `OSError: File name too long` e la misura usciva con `error`."""
        res = fill_rate.from_log(_LOG)
        assert "error" not in res and res["picks_attempted"] == 2

    def test_testo_vuoto_e_spazzatura(self):
        for payload in ("", "riga senza senso\n  \n"):
            res = fill_rate.from_log(payload)
            assert res["picks_attempted"] == 0 and "error" not in res

    def test_riempimento_resting_immediato_conta_una_volta(self):
        """Il percorso RESTING logga DUE volte lo stesso ordine (caso reale
        Norwich-Portsmouth del 17/10): un tentativo, riempito."""
        log = (
            "2026-10-17 02:00:49,679 - auto_bet - WARNING - auto_bet: "
            "ordine RESTING 0x0d35 (Norwich City vs Portsmouth, 1) riempito "
            "SUBITO @ 1.9417 per 2.79 USDC: trattato come ordine normale\n"
            "2026-10-17 02:00:50,382 - auto_bet - INFO - auto_bet: "
            "ORDINE REALE riempito 0x0d35 (Norwich vs Portsmouth, 1) @ 1.94 "
            "per \u20ac2.79 [FULLY_FILLED]")
        res = fill_rate.from_log(log)
        assert (res["picks_attempted"], res["picks_filled"]) == (1, 1)
        assert res["attempts_total"] == 1
        assert res["fill_rate_per_pick"] == 100.0

    def test_resting_aperto_e_un_tentativo_non_un_fill(self):
        log = (
            "2026-10-17 02:00:00,000 - resting_orders - INFO - resting_orders: "
            "ORDINE RESTING 0xaaa (Alfa vs Beta, 1) @ 1.9000 per 2.00 USDC "
            "[0xorder] \u2014 deadline 2026-10-17 12:00:00+00:00\n"
            "2026-10-17 02:00:00,001 - auto_bet - INFO - auto_bet: ORDINE "
            "RESTING aperto su SX per 0xaaa (Alfa vs Beta, 1) @ 1.90 per "
            "2.00 USDC [0xorder] \u2014 motivo: CANCELLED; nessuna riga ledger")
        res = fill_rate.from_log(log)
        assert res["picks_attempted"] == 1 and res["picks_filled"] == 0
        assert res["attempts_total"] == 1

    def test_riconciliazione_resting_chiude_il_pick(self):
        log = (
            "2026-10-17 02:00:00,000 - auto_bet - INFO - auto_bet: ORDINE "
            "RESTING aperto su SX per 0xaaa (Alfa vs Beta, 1) @ 1.90 per "
            "2.00 USDC [0xorder] \u2014 motivo: CANCELLED\n"
            "2026-10-17 09:30:00,000 - resting_orders - INFO - resting_orders: "
            "RIEMPITO 0xaaa (Alfa vs Beta, 1) @ 1.9000 per 2.00 USDC \u2192 "
            "riga bets mode='live'")
        res = fill_rate.from_log(log)
        assert (res["picks_attempted"], res["picks_filled"]) == (1, 1)
        assert res["attempts_total"] == 2   # apertura + riconciliazione

    def test_retry_vero_non_dedupato(self):
        """Due tentativi a 60s di distanza sono due tentativi."""
        log = ("2026-10-17 02:00:00,000 - auto_bet - WARNING - auto_bet: "
               "ordine reale 0xaaa non riempito (Alfa vs Beta, CANCELLED): x\n"
               "2026-10-17 02:01:00,000 - auto_bet - WARNING - auto_bet: "
               "ordine reale 0xaaa non riempito (Alfa vs Beta, CANCELLED): x")
        res = fill_rate.from_log(log)
        assert res["attempts_total"] == 2 and res["attempts_per_pick"]["avg"] == 2.0

    def test_coerenza_col_percorso_jsonl(self, tmp_path):
        """Le due fonti devono concordare sullo STESSO scenario."""
        f = tmp_path / "log.txt"
        f.write_text(_LOG)
        from_log = fill_rate.from_log(str(f))
        for _ in range(3):
            fill_rate.record_attempt(_pick(match_id="0xaaa", esito_key="-"),
                                     2.0, 1.9, _no_fill())
        fill_rate.record_attempt(_pick(match_id="0xbbb", esito_key="-"),
                                 2.79, 1.94, _filled(mid="0xbbb"))
        from_jsonl = fill_rate.summarize(fill_rate.iter_attempts())
        assert from_jsonl["fill_rate_per_pick"] == from_log["fill_rate_per_pick"]
        assert from_jsonl["attempts_total"] == from_log["attempts_total"]


# ---------------------------------------------------------------------------
# 5. Integrazione col percorso d'ordine (wrapper, nessuna rete)
# ---------------------------------------------------------------------------

class TestWrapperLiveFill:
    def test_registra_il_fill(self, monkeypatch):
        monkeypatch.setattr(auto_bet, "_live_fill_impl",
                            lambda p, s, f, t=None: _filled())
        res = auto_bet._live_fill(_pick(), 2.79, 1.94)
        assert res["ok"] is True
        events = fill_rate.iter_attempts()
        assert len(events) == 1 and events[0]["outcome"] == "filled"

    def test_registra_il_salto(self, monkeypatch):
        monkeypatch.setattr(auto_bet, "_live_fill_impl",
                            lambda p, s, f, t=None: None)
        assert auto_bet._live_fill(_pick(), 2.79, 1.94) is None
        assert fill_rate.iter_attempts()[0]["outcome"] == "skipped"

    def test_non_cambia_il_valore_di_ritorno(self, monkeypatch):
        expected = _no_fill()
        monkeypatch.setattr(auto_bet, "_live_fill_impl",
                            lambda p, s, f, t=None: expected)
        assert auto_bet._live_fill(_pick(), 2.0, 1.9) is expected

    def test_telemetria_rotta_non_rompe_l_ordine(self, monkeypatch):
        """Se `fill_rate` esplode, `_live_fill` ritorna comunque il suo esito."""
        monkeypatch.setattr(auto_bet, "_live_fill_impl",
                            lambda p, s, f, t=None: _filled())
        import types
        broken = types.ModuleType("fill_rate")

        def _boom(*a, **k):
            raise RuntimeError("telemetria rotta")

        broken.record_attempt = _boom
        monkeypatch.setitem(sys.modules, "fill_rate", broken)
        assert auto_bet._live_fill(_pick(), 2.0, 1.9)["ok"] is True

    def test_impl_marca_l_errore_per_il_classificatore(self, monkeypatch):
        """Il ramo 'provider esploso' deve risultare `error` (esito ignoto)."""
        src = Path("auto_bet.py").read_text(encoding="utf-8")
        block = src.split("def _live_fill_impl(", 1)[1].split(
            "\ndef ", 1)[0]
        assert 'trace["outcome"] = "error"' in block

    def test_wrapper_delega_all_impl(self):
        src = Path("auto_bet.py").read_text(encoding="utf-8")
        assert "_live_fill_impl(pick, stake, floor, trace)" in src


# ---------------------------------------------------------------------------
# 6. Tripwire: diagnostica pura
# ---------------------------------------------------------------------------

def _code_only(path: str) -> str:
    """Sorgente senza DOCSTRING: il tripwire giudica il CODICE, non la prosa.

    (Lezione del 30/09/2026: un tripwire che boccia la documentazione obbliga a
    non spiegare perche' una regola esiste — esattamente il contrario di cio'
    che serve. Qui il modulo NOMINA `auto_bet._live_fill` per dire da dove
    arriva il dato: e' documentazione, non un percorso d'ordine.)
    """
    import ast
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
            continue
        body = node.body
        if body and isinstance(body[0], ast.Expr) \
                and isinstance(body[0].value, ast.Constant) \
                and isinstance(body[0].value.value, str):
            node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


class TestTripwire:
    def test_nessun_ordine_ne_scrittura_di_ledger(self):
        code = _code_only("fill_rate.py")
        for banned in ("place_limit_order", "_live_fill", "save_bet",
                       "resolve_market_for", "execution_engine", "auto_bet",
                       "INSERT", "UPDATE", "DELETE", "connect"):
            assert banned not in code, (
                f"fill_rate non deve contenere {banned} nel codice")

    def test_import_leggero(self):
        code = ("import sys, fill_rate;"
                "heavy=[m for m in ('tracker','auto_bet','bot','odds_api',"
                "'execution_engine','decision','sx_signals') if m in sys.modules];"
                "print('|'.join(heavy))")
        out = subprocess.run([sys.executable, "-c", code], cwd=".",
                             capture_output=True, text=True, timeout=90)
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == "", f"import pesante: {out.stdout}"

    def test_report_dichiara_il_campione(self):
        for _ in range(3):
            fill_rate.record_attempt(_pick(), 2.0, 1.9, _no_fill())
        text = fill_rate.format_report(fill_rate.summarize(
            fill_rate.iter_attempts()))
        assert "PER PICK" in text and "0.0%" in text
        assert "non conclusivi" in text

    def test_report_senza_campione(self):
        assert "nessun tentativo" in fill_rate.format_report(
            fill_rate.summarize([]))

    def test_cli_json(self, capsys, monkeypatch):
        fill_rate.record_attempt(_pick(), 2.0, 1.9, _filled())
        rc = fill_rate.main(["--json", "--days", "7"])
        assert rc == 0
        assert json.loads(capsys.readouterr().out)["picks_filled"] == 1

    def test_cli_su_estratto_di_log(self, capsys, tmp_path):
        f = tmp_path / "log.txt"
        f.write_text(_LOG)
        assert fill_rate.main(["--log", str(f), "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["picks_attempted"] == 2
