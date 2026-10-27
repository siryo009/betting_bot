"""Test dell'adeguatezza del capitale (capital_adequacy.py) — 27/10/2026.

Direttiva del proprietario dopo il controllo del saldo: "penso che ti accorgerai
delle perdite avute ... dammi la soglia di top-up". Il ledger dice 101 puntate
live chiuse, -21,19 USDC (-9,2% di ROI) e un'equity scesa da 47,16 a 16,24
USDC. Il modulo risponde a UNA domanda: **quanto capitale serve perche' il
minimo ordine dell'exchange (1 USDC) non sia un rischio piu' grande di quello
che il Kelly giustifica?**

Copre: la formula di Kelly corretta (fix del 27/10), il capitale minimo per
livello di EV, le soglie di lettura, il fail-safe, la mediana delle quote dal
ledger, il cablaggio in `/api/dashboard` e `/api/agents`, e i tripwire
(sola lettura, nessuna rete, nessun ordine, import leggero, formula unica).

Tutto OFFLINE: ledger SQLite temporaneo, nessuna rete, nessun provider.
"""

import math
import sqlite3
import subprocess
import sys

import pytest

import capital_adequacy as ca
import value_filter as vf


# ---------------------------------------------------------------------------
# 1. LA FORMULA DI KELLY — il fix che ha motivato il modulo
# ---------------------------------------------------------------------------

class TestKellyVero:
    def test_formula_chiusa(self):
        """f* = (p*o - 1) / (o - 1), confrontato con la formula chiusa."""
        for p, o in ((0.55, 1.90), (0.62, 1.66), (0.40, 3.00), (0.75, 1.35),
                     (0.52, 2.10)):
            atteso = (p * o - 1.0) / (o - 1.0)
            assert vf.kelly_fraction(p, o, fraction=1.0) == pytest.approx(
                max(0.0, atteso), abs=1e-12)

    def test_non_e_piu_la_formula_vecchia(self):
        """REGRESSIONE del bug: `(p*o - q)/o` sovrastimava di 2,5-7x.

        Il caso reale misurato sul ledger: p 0.5395 @ 1.90 (EV +2,5%).
        """
        p, o = 0.5395, 1.90
        vecchia = (p * o - (1 - p)) / o
        nuova = vf.kelly_fraction(p, o, fraction=1.0)
        assert nuova == pytest.approx((p * o - 1) / (o - 1), abs=1e-12)
        assert vecchia / nuova > 5.0        # la distorsione e' misurabile

    def test_ev_negativo_non_alloca_nulla(self):
        """Il caso piu' grave della vecchia formula: allocava su EV NEGATIVO.

        p 0.60 @ 1.66 = EV -0,4%, eppure la vecchia formula chiedeva il 35,9%
        di bankroll. Il Kelly vero risponde zero (nessuno stake).
        """
        assert 0.60 * 1.66 - 1 < 0
        assert vf.kelly_fraction(0.60, 1.66, fraction=1.0) == 0.0
        assert vf.kelly_fraction(0.60, 1.66) == 0.0

    def test_frazionamento_lineare(self):
        full = vf.kelly_fraction(0.60, 1.80, fraction=1.0)
        assert vf.kelly_fraction(0.60, 1.80, fraction=0.25) == pytest.approx(
            full * 0.25, abs=1e-12)

    def test_quota_non_valida(self):
        assert vf.kelly_fraction(0.6, 1.0) == 0.0
        assert vf.kelly_fraction(0.6, 0.5) == 0.0


# ---------------------------------------------------------------------------
# 2. CAPITALE MINIMO PER LIVELLO DI EV
# ---------------------------------------------------------------------------

class TestCapitaleMinimo:
    def test_derivato_dalle_fonti_di_produzione(self):
        """Il capitale minimo non e' un numero copiato: si ricalcola da
        `kelly_fraction` x `k` e il test rifa' lo stesso conto."""
        r = ca.required_bankroll(ev=0.05, odds=1.90)
        assert r["ok"] is True
        atteso = r["floor"] / (r["kelly_full"] * r["k"])
        assert r["required_bankroll"] == pytest.approx(atteso, abs=0.01)

    def test_ev_piu_alto_richiede_meno_capitale(self):
        basso = ca.required_bankroll(ev=0.025, odds=1.90)
        alto = ca.required_bankroll(ev=0.10, odds=1.90)
        assert alto["required_bankroll"] < basso["required_bankroll"]

    def test_ordine_di_grandezza_al_floor_di_1_usdc(self):
        """Con EV alla soglia minima servono ~240 USDC perche' 1 USDC sia il
        0,42% del capitale (k al minimo della banda). Un test che fissasse il
        numero esatto scadrebbe al primo ritocco delle soglie: qui si fissa
        l'ordine di grandezza e la relazione con i fattori."""
        r = ca.required_bankroll(ev=vf.EV_MIN, odds=1.90, floor=1.0)
        assert 150.0 <= r["required_bankroll"] <= 400.0
        # Il floor dell'exchange e' ~11-15x il rischio voluto con 16,24 USDC.
        assert 1.0 / r["risk_pct"] / 16.2441 > 8.0

    def test_nessun_edge_non_inventa_un_capitale(self):
        r = ca.required_bankroll(ev=-0.02, odds=2.0)
        assert r["ok"] is False and r["reason"] == "no_edge"

    def test_input_illeggibili_fail_closed(self):
        assert ca.required_bankroll(ev="x", odds=1.9)["reason"] == "invalid_inputs"
        assert ca.required_bankroll(ev=0.03, odds=1.0)["reason"] == "invalid_inputs"


# ---------------------------------------------------------------------------
# 3. ISTANTANEA (soglie di lettura) E FAIL-SAFE
# ---------------------------------------------------------------------------

class TestEvaluate:
    def test_stati(self):
        base = ca.evaluate(1.0, odds=1.90)["required_bankroll"]
        assert ca.evaluate(base * 1.2, odds=1.90)["status"] == "adequate"
        assert ca.evaluate(base * 0.75, odds=1.90)["status"] == "thin"
        assert ca.evaluate(base * 0.2, odds=1.90)["status"] == "overbet"

    def test_rischio_reale_di_oggi(self):
        """Il numero operativo: con 16,24 USDC il floor da 1 USDC e' il 6,16%."""
        r = ca.evaluate(16.2441, odds=1.90)
        assert r["risk_pct_now"] == pytest.approx(1.0 / 16.2441, abs=1e-6)
        assert r["risk_pct_now_label"] == "6.16%"
        assert r["deficit"] > 0 and r["status"] == "overbet"

    def test_livelli_tre(self):
        r = ca.evaluate(100.0, odds=1.90)
        assert len(r["levels"]) == 3
        assert all("required_bankroll" in x for x in r["levels"])

    def test_equity_non_determinabile_non_solleva(self):
        for valore in ("abc", 0.0, -5.0, None):
            r = ca.evaluate(valore, odds=1.90)
            assert r["ok"] is False and r["error"]

    def test_equity_letta_dal_wallet(self, monkeypatch):
        monkeypatch.setattr("order_watch.current_equity", lambda: 55.0)
        r = ca.evaluate(None, odds=1.90)
        assert r["ok"] is True and r["equity"] == 55.0

    def test_lettore_rotto_ricade_sull_errore(self, monkeypatch):
        def boom():
            raise RuntimeError("provider giu'")
        monkeypatch.setattr("order_watch.current_equity", boom)
        r = ca.evaluate(None)
        assert r["ok"] is False and "RuntimeError" in r["error"]


# ---------------------------------------------------------------------------
# 4. QUOTE DAL LEDGER (fallback dichiarato) E REPORT
# ---------------------------------------------------------------------------

class TestQuoteDalLedger:
    def _db(self, tmp_path, prices):
        db = tmp_path / "q.db"
        conn = sqlite3.connect(db)
        conn.execute("""CREATE TABLE bets (id INTEGER PRIMARY KEY, mode TEXT,
                        price REAL, created_at TEXT)""")
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        for i, p in enumerate(prices):
            conn.execute("INSERT INTO bets (mode, price, created_at) "
                         "VALUES ('live', ?, ?)",
                         (p, (now - timedelta(days=i)).isoformat()))
        conn.commit()
        conn.close()
        return db

    def test_mediana_delle_quote_recenti(self, tmp_path, monkeypatch):
        import tracker
        db = self._db(tmp_path, [1.30, 1.80, 1.95, 2.10, 3.00])
        monkeypatch.setattr(tracker, "DB_PATH", db)
        monkeypatch.setattr(tracker, "DATA_DIR", tmp_path)
        info = ca.typical_odds()
        assert info["source"] == "ledger" and info["n"] == 5
        assert info["odds"] == pytest.approx(1.95)

    def test_senza_ledger_fallback_dichiarato(self, tmp_path, monkeypatch):
        import tracker
        monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "assente.db")
        monkeypatch.setattr(tracker, "DATA_DIR", tmp_path)
        info = ca.typical_odds()
        assert info["source"] == "default" and info["n"] == 0
        assert info["odds"] == ca.DEFAULT_ODDS


class TestReport:
    def test_dichiara_capitale_e_mancante(self):
        testo = ca.format_report(ca.evaluate(16.2441, odds=1.90))
        assert "16.24" in testo and "servono" in testo and "mancano" in testo
        assert "🔴" in testo

    def test_non_misurabile(self):
        testo = ca.format_report({"ok": False, "error": "equity non disponibile"})
        assert "non misurabile" in testo


# ---------------------------------------------------------------------------
# 5. GARANZIE: sola lettura, nessuna rete, nessun ordine, formula unica
# ---------------------------------------------------------------------------

class TestTripwire:
    def test_sorgente_senza_scritture_ne_rete_ne_ordini(self):
        code = open("capital_adequacy.py", encoding="utf-8").read()
        # Il controllo giudica il CODICE, non la prosa: le docstring parlano di
        # "ordini" e "denaro" per spiegare il perche' (lezione del 30/09).
        import ast
        docstrings = set()
        for node in ast.walk(ast.parse(code)):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)):
                doc = ast.get_docstring(node)
                if doc:
                    docstrings.add(doc)
        corpo = code
        for doc in docstrings:
            corpo = corpo.replace(doc, "")
        for vietato in ("INSERT", "UPDATE ", "DELETE FROM", "save_bet",
                        "place_limit_order", "_live_fill", "requests",
                        "sqlite3.connect"):
            assert vietato not in corpo, vietato
        # il ledger si legge con `tracker._get_conn` (import pigro)
        assert "_get_conn" in code

    def test_formula_unica(self):
        """Il modulo NON ricopia il Kelly: delega a value_filter."""
        code = open("capital_adequacy.py", encoding="utf-8").read()
        assert "kelly_fraction" in code
        assert "(prob * odds - 1.0) / (odds - 1.0)" not in code

    def test_import_leggero(self):
        out = subprocess.run(
            [sys.executable, "-c",
             "import capital_adequacy, sys; "
             "print('auto_bet' in sys.modules, 'tracker' in sys.modules, "
             "'bot' in sys.modules)"],
            capture_output=True, text=True, timeout=120)
        assert out.returncode == 0
        assert out.stdout.strip() == "False False False"

    def test_cli_json(self):
        out = subprocess.run([sys.executable, "capital_adequacy.py", "--json",
                              "--equity", "50"], capture_output=True, text=True,
                             timeout=120)
        assert out.returncode == 0
        import json as _json
        dati = _json.loads(out.stdout)
        assert dati["ok"] is True and dati["equity"] == 50.0


# ---------------------------------------------------------------------------
# 6. CABLAGGIO: dashboard e tabella agenti
# ---------------------------------------------------------------------------

class TestCablaggio:
    def test_dashboard_espone_fonte_e_adeguatezza(self, monkeypatch):
        import web_api
        monkeypatch.setattr("order_watch.current_equity", lambda: 16.2441)
        d = web_api._dashboard_json()
        assert d["bankroll"] == pytest.approx(16.2441, abs=1e-4)
        assert d["bankroll_source"] == "live_equity"
        assert d["capital_adequacy"]["ok"] is True
        assert d["capital_adequacy"]["status"] == "overbet"
        assert d["capital_adequacy"]["required_bankroll"] > 0

    def test_dashboard_senza_equity_dichiara_la_fonte(self, monkeypatch):
        """Nessun wallet leggibile: si mostra il valore di ripiego MA la fonte
        non viene spacciata per equity reale."""
        import web_api
        monkeypatch.setattr("order_watch.current_equity", lambda: None)
        import adaptive_staking
        monkeypatch.setattr(adaptive_staking, "bankroll_stats",
                            lambda **kw: {"current": 0.0, "peak": 0.0})
        d = web_api._dashboard_json()
        assert d["bankroll_source"] in ("default", "cassa")
        assert d["bankroll"] != 16.2441

    def test_tabella_agenti_porta_il_blocco(self, monkeypatch):
        import agent_board
        monkeypatch.setattr("order_watch.current_equity", lambda: 16.2441)
        cap = agent_board.capital_snapshot()
        assert cap["available"] is True
        assert cap["capital_adequacy"]["status"] == "overbet"
        assert cap["capital_adequacy"]["required_bankroll"] > 0
