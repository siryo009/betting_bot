"""test_tennis_quant.py — test OFFLINE di `tennis_quant.py` (03/10/2026).

Tutto gira senza rete, senza provider reali e senza credenziali: SQLite
temporaneo, `sharp_lookup`/`provider` iniettati, telemetria nella tmp (l'env
`TENNIS_QUANT_DB`/`TENNIS_QUANT_LOG` e' isolata da `conftest.py`).

I tripwire verificano le promesse che rendono il modulo sicuro da deployare:
- **nessun ordine** nel sorgente (niente `_live_fill`/`place_limit_order`/
  `save_bet`/`resolve_market_for`);
- **nessuna formula duplicata** (`def devig`/`shin`/`kelly_fraction`/`sqrt(`):
  la matematica si eredita da `tennis_sandbox`/`market_calib`/`value_filter`;
- **import leggero**: `import tennis_quant` non carica tracker/auto_bet/bot;
- le env nuove sono dichiarate nella IaC (lezione del 28/09: `config apply`
  distrugge cio' che non e' in `preserve()`).
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

import tennis_quant as tq


def _code_only(src: str) -> str:
    """Sorgente senza docstring (il codice, non la PROSA).

    I tripwire di questo progetto devono colpire il CODICE: le docstring
    NOMINANO di proposito i simboli vietati per spiegare perche' non si usano
    (stessa lezione di `test_money_decimal`). `ast.unparse` rimuove anche i
    commenti.
    """
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            body = getattr(node, "body", None) or []
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


# ---------------------------------------------------------------------------
# Helper: recinto libero iniettato (stessa forma di auto_bet.exposure_allows)
# ---------------------------------------------------------------------------

def _free_enclosure(bankroll: float, new_stake: float = 0.0) -> dict:
    bankroll = float(bankroll or 0.0)
    stake = max(float(new_stake or 0.0), 0.0)
    cap = round(max(bankroll, 0.0) * 0.40, 2)
    return {"allowed": stake <= cap, "blocked": False, "open_stake": 0.0,
            "count": 0, "cap": cap, "bankroll": round(bankroll, 2),
            "new_stake": round(stake, 2), "projected": round(stake, 2),
            "reason": ""}


def _db(tmp_path: Path) -> Path:
    return tmp_path / "quant.db"


# ---------------------------------------------------------------------------
# 1. ELO superficie-specifico su SQLite
# ---------------------------------------------------------------------------

class TestSurfaceElo:
    def test_round_trip_persistenza(self, tmp_path):
        db = _db(tmp_path)
        elo = tq.SurfaceElo(db_path=db)
        elo.ensure_pair("Alcaraz", 0.70, "Sinner", 0.30)
        elo.update("Alcaraz", "Sinner", surface="hard")
        elo.save()

        reloaded = tq.SurfaceElo(db_path=db)
        assert set(reloaded.players) == {"Alcaraz", "Sinner"}
        assert reloaded.matches_played("Alcaraz") == 1
        assert reloaded.match_prob("Alcaraz", "Sinner", surface="hard") > 0.5

    def test_db_vuoto_zero_giocatori(self, tmp_path):
        elo = tq.SurfaceElo(db_path=_db(tmp_path))
        assert elo.players == {}
        assert elo.matches_played("Ignoto") == 0

    def test_isolamento_superficie(self, tmp_path):
        """Un match su terra non deve muovere il rating di cemento."""
        db = _db(tmp_path)
        elo = tq.SurfaceElo(db_path=db)
        elo.ensure_pair("A", 0.5, "B", 0.5)
        elo.update("A", "B", surface="clay")
        elo.save()
        reloaded = tq.SurfaceElo(db_path=db)
        a = reloaded.players["A"]
        assert "clay" in a.surfaces and a.surfaces["clay"].n == 1
        assert "hard" not in a.surfaces
        # L'overall e' stato mosso, la superficie hard no.
        assert a.n == 1

    def test_persistenza_superfici_nel_ledger(self, tmp_path):
        db = _db(tmp_path)
        elo = tq.SurfaceElo(db_path=db)
        elo.ensure_pair("A", 0.5, "B", 0.5)
        elo.update("A", "B", surface="grass")
        elo.save()
        import sqlite3
        conn = sqlite3.connect(str(db))
        rows = conn.execute("SELECT player, surface FROM tennis_elo_surfaces").fetchall()
        conn.close()
        assert ("A", "grass") in rows and ("B", "grass") in rows

    def test_time_decay_ereditato(self, tmp_path):
        """Il time-decay arriva dall'Elo del sandbox, non da una copia."""
        from tennis_sandbox import TennisElo
        db = _db(tmp_path)
        elo = tq.SurfaceElo(db_path=db)
        # La formula e' letteralmente la stessa funzione (ereditata).
        assert tq.SurfaceElo._decayed is TennisElo._decayed
        assert tq.SurfaceElo.prob is TennisElo.prob


# ---------------------------------------------------------------------------
# 2. Modello di Poisson da hold/break
# ---------------------------------------------------------------------------

class TestPoisson:
    def test_pmf_somma_a_uno(self):
        for lam in (0.5, 2.0, 5.0, 12.0):
            assert sum(tq.poisson_pmf(k, lam) for k in range(200)) == pytest.approx(1.0, abs=1e-9)

    def test_pmf_lam_zero(self):
        assert tq.poisson_pmf(0, 0.0) == 1.0
        assert tq.poisson_pmf(3, 0.0) == 0.0

    def test_favorito_di_servizio_favorito_anche_nel_match(self):
        """REGRESSIONE: λ_A era attribuito alla tenuta di A (invertito).

        Un giocatore che tiene il servizio piu' dell'avversario deve risultare
        FAVORITO, non sfavorito. Il bug faceva il contrario esatto.
        """
        assert tq.poisson_prob_from_hold_break(0.90, 0.70) > 0.5
        assert tq.poisson_prob_from_hold_break(0.70, 0.90) < 0.5

    def test_simmetria(self):
        assert tq.poisson_prob_from_hold_break(0.80, 0.80) == pytest.approx(0.5, abs=1e-9)

    def test_complementarita(self):
        a = tq.poisson_prob_from_hold_break(0.85, 0.72)
        b = tq.poisson_prob_from_hold_break(0.72, 0.85)
        assert a + b == pytest.approx(1.0, abs=1e-9)

    def test_break_rates_attribuzione(self):
        la, lb = tq.break_rates(0.90, 0.70, return_games=10)
        assert la == pytest.approx(10 * 0.30)     # A buca il servizio di B
        assert lb == pytest.approx(10 * 0.10)     # B buca il servizio di A

    def test_hold_rates_da_prob_monotone(self):
        hi_a, hi_b = tq.hold_rates_from_prob(0.80)
        lo_a, lo_b = tq.hold_rates_from_prob(0.20)
        assert hi_a > lo_a and hi_b < lo_b
        assert tq.poisson_prob_from_hold_break(hi_a, hi_b) > 0.5
        assert tq.poisson_prob_from_hold_break(lo_a, lo_b) < 0.5

    def test_hold_rates_clamp(self):
        a, b = tq.hold_rates_from_prob(1.0)
        assert 0.50 <= a <= 0.95 and 0.50 <= b <= 0.95


# ---------------------------------------------------------------------------
# 3. De-vig di Shin (delega, nessuna formula ricopiata)
# ---------------------------------------------------------------------------

class TestShinDelegation:
    def test_coincide_con_market_calib(self):
        from market_calib import market_implied
        odds = {"1": 1.66, "2": 2.34}
        mine = tq.shin_probabilities(odds, method="shin")
        ref = market_implied(odds, method="shin")
        assert mine["1"] == pytest.approx(ref["1"])
        assert mine["2"] == pytest.approx(ref["2"])
        assert mine["method"] == "shin"

    def test_meno_di_due_quote_none(self):
        assert tq.shin_probabilities({"1": 1.66}) is None
        assert tq.shin_probabilities({"1": 0.5, "2": 1.9}) is None

    def test_metodo_invalido_ricade_su_shin(self):
        out = tq.shin_probabilities({"1": 1.66, "2": 2.34}, method="pippo")
        assert out["method"] == "shin"

    def test_shin_piu_aggressivo_del_proporzionale(self):
        odds = {"1": 1.66, "2": 2.34}
        shin = tq.shin_probabilities(odds, method="shin")
        prop = tq.shin_probabilities(odds, method="multiplicative")
        assert shin["1"] >= prop["1"]


# ---------------------------------------------------------------------------
# 4. Combinazione + EV
# ---------------------------------------------------------------------------

class TestCombineAndEv:
    def test_pesi(self):
        assert tq.combine_probabilities(0.8, 0.2, w_elo=1.0) == pytest.approx(0.8)
        assert tq.combine_probabilities(0.8, 0.2, w_elo=0.0) == pytest.approx(0.2)
        assert tq.combine_probabilities(0.8, 0.2, w_elo=0.5) == pytest.approx(0.5)

    def test_ev(self):
        assert tq.expected_value(0.5, 2.0) == pytest.approx(0.0)
        assert tq.expected_value(0.6, 2.0) == pytest.approx(0.2)


# ---------------------------------------------------------------------------
# 5. Il verdetto "entrambi confermano"
# ---------------------------------------------------------------------------

def _elo_with(tmp_path, n=6):
    """ELO con storico sufficiente (modello maturo) su due giocatori."""
    elo = tq.SurfaceElo(db_path=_db(tmp_path))
    elo.ensure_pair("A", 0.5, "B", 0.5)
    for _ in range(n):
        elo.update("A", "B", surface="hard")
    return elo


class TestEvaluate:
    def test_entrambi_confermano(self, tmp_path):
        elo = _elo_with(tmp_path)
        # Modello favorevole (A forte dopo 6 win) e sharp che conferma.
        v = tq.evaluate(team_a="A", team_b="B", price_a=1.60, price_b=2.40,
                        sharp_probs={"1": 0.70, "2": 0.30}, elo=elo,
                        surface="hard", bankroll=30.0,
                        enclosure_check=_free_enclosure)
        assert v["ok"] is True
        assert v["side"] == "1"
        assert v["ev_sharp"] == pytest.approx(0.70 * 1.60 - 1.0)
        assert v["both_confirm"] is True

    def test_sharp_non_conferma(self, tmp_path):
        elo = _elo_with(tmp_path)
        # Lo sharp prezza A molto basso: il prezzo SX non e' piu' value.
        v = tq.evaluate(team_a="A", team_b="B", price_a=1.60, price_b=2.40,
                        sharp_probs={"1": 0.50, "2": 0.50}, elo=elo,
                        surface="hard", bankroll=30.0,
                        enclosure_check=_free_enclosure)
        assert v["both_confirm"] is False

    def test_intrinseco_non_conferma(self, tmp_path):
        elo = _elo_with(tmp_path)
        # Lo sharp vede tanto valore, ma il modello intrinseco e' basso:
        # il gate richiede ENTRAMBI, non uno dei due.
        v = tq.evaluate(team_a="A", team_b="B", price_a=1.60, price_b=2.40,
                        sharp_probs={"1": 0.80, "2": 0.20}, elo=elo,
                        surface="hard", bankroll=30.0,
                        enclosure_check=_free_enclosure)
        assert v["ev_intrinsic"] < v["ev_sharp"]
        assert v["both_confirm"] is (v["ev_intrinsic"] >= v["ev_min"])

    def test_indipendenza_false_senza_storico(self, tmp_path):
        elo = tq.SurfaceElo(db_path=_db(tmp_path))
        elo.ensure_pair("X", 0.6, "Y", 0.4)     # seminati, n=0
        v = tq.evaluate(team_a="X", team_b="Y", price_a=1.65, price_b=2.30,
                        sharp_probs={"1": 0.60, "2": 0.40}, elo=elo,
                        surface="hard", bankroll=30.0,
                        enclosure_check=_free_enclosure)
        assert v["independent"] is False
        assert v["model_mature"] is False

    def test_serve_stats_provider(self, tmp_path):
        elo = _elo_with(tmp_path)
        v = tq.evaluate(team_a="A", team_b="B", price_a=1.60, price_b=2.40,
                        sharp_probs={"1": 0.65, "2": 0.35}, elo=elo,
                        serve_stats=(0.92, 0.68), surface="hard",
                        bankroll=30.0, enclosure_check=_free_enclosure)
        assert v["serve_stats_source"] == "provider"
        assert v["hold_a"] == pytest.approx(0.92)

    def test_kelly_telemetria_rispetta_il_cap(self, tmp_path):
        elo = _elo_with(tmp_path)
        v = tq.evaluate(team_a="A", team_b="B", price_a=1.60, price_b=2.40,
                        sharp_probs={"1": 0.75, "2": 0.25}, elo=elo,
                        surface="hard", bankroll=100.0,
                        enclosure_check=_free_enclosure)
        k = v["kelly"]
        assert k["suggested_stake"] <= k["cap"]
        assert k["cap"] == pytest.approx(min(100.0 * tq.MAX_STAKE_PCT, tq.MAX_STAKE_ABS))
        assert k["enclosure"]["allowed"] is True

    def test_input_incompleti_fail_closed(self, tmp_path):
        elo = _elo_with(tmp_path)
        for bad in (
            dict(sharp_probs={"1": 1.5, "2": 0.5}),
            dict(sharp_probs={}),
            dict(sharp_probs={"1": 0.6, "2": 0.4}, price_a=0.5),
        ):
            base = dict(team_a="A", team_b="B", price_a=1.6, price_b=2.4,
                        sharp_probs={"1": 0.6, "2": 0.4}, elo=elo,
                        surface="hard", bankroll=30.0,
                        enclosure_check=_free_enclosure)
            base.update(bad)
            assert tq.evaluate(**base)["ok"] is False


# ---------------------------------------------------------------------------
# 6. Riferimento sharp
# ---------------------------------------------------------------------------

class TestSharpReference:
    def test_lookup_iniettato(self):
        ref = tq.sharp_reference("A", "B", lookup=lambda a, b: {"1": 0.6, "2": 0.4})
        assert ref["1"] == 0.6

    def test_lookup_none(self):
        assert tq.sharp_reference("A", "B", lookup=lambda a, b: None) is None

    def test_fail_closed_senza_cache(self, monkeypatch):
        import pinnacle_oracle as po
        monkeypatch.setattr(po, "load_oracle", lambda *a, **k: None)
        assert tq.sharp_reference("Tizio", "Caio") is None


# ---------------------------------------------------------------------------
# 7. Ciclo di misura (nessun ordine)
# ---------------------------------------------------------------------------

def _event(price_a=1.60, price_b=2.40):
    return {"market_id": "m1", "event_id": "e1", "league_label": "ATP Beijing",
            "team_one": "A", "team_two": "B",
            "sides": [{"key": "1", "team": "A", "price": price_a, "depth": 100.0},
                      {"key": "2", "team": "B", "price": price_b, "depth": 100.0}],
            "inv_sum": 1.02}


class TestRunCycle:
    def _patch(self, monkeypatch, events, sharp, serve=None):
        import tennis_lane as tl
        monkeypatch.setattr(tl, "discover", lambda provider=None: events)
        return tq.run_cycle(
            provider=object(),
            sharp_lookup=lambda a, b: sharp,
            serve_stats_lookup=(lambda a, b: serve) if serve else None,
            bankroll=30.0, enclosure_check=_free_enclosure, write=True)

    def test_misura_e_registra(self, monkeypatch, tmp_path):
        res = self._patch(monkeypatch, [_event()], {"1": 0.70, "2": 0.30})
        assert res["events"] == 1 and res["evaluated"] == 1
        assert res["both_confirm"] == 1 and res["candidates"] == 1
        # Telemetria scritta nel file isolato.
        lines = tq.log_path().read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        row = json.loads(lines[0])
        assert row["team_a"] == "A" and row["candidate"] is True

    def test_senza_sharp_salta(self, monkeypatch):
        res = self._patch(monkeypatch, [_event()], None)
        assert res["skipped_no_sharp"] == 1 and res["evaluated"] == 0

    def test_spegne_con_interruttore(self, monkeypatch):
        monkeypatch.setenv("TENNIS_QUANT_ENABLED", "0")
        res = tq.run_cycle(bankroll=30.0)
        assert res["enabled"] is False and res["evaluated"] == 0

    def test_ratings_persistiti(self, monkeypatch, tmp_path):
        self._patch(monkeypatch, [_event()], {"1": 0.70, "2": 0.30})
        elo = tq.SurfaceElo()
        assert "A" in elo.players and "B" in elo.players

    def test_write_false_non_scrive_righe(self, monkeypatch, tmp_path):
        """`write=False` e' l'uso diagnostico: nessuna riga nuova nel log."""
        import tennis_lane as tl
        monkeypatch.setattr(tl, "discover", lambda provider=None: [_event()])
        before = len(tq._read_telemetry(limit=10_000))
        res = tq.run_cycle(
            provider=object(),
            sharp_lookup=lambda a, b: {"1": 0.7, "2": 0.3},
            bankroll=30.0, enclosure_check=_free_enclosure, write=False)
        assert res["evaluated"] == 1
        assert len(tq._read_telemetry(limit=10_000)) == before


# ---------------------------------------------------------------------------
# 8. Apprendimento dai settlement (idempotente)
# ---------------------------------------------------------------------------

def _ledger(tmp_path, rows):
    import sqlite3
    db = tmp_path / "led.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE matches (id TEXT PRIMARY KEY, home_team TEXT, "
                 "away_team TEXT, league TEXT)")
    conn.execute("CREATE TABLE predictions (match_id TEXT, mercato TEXT, esito TEXT, "
                 "esito_finale TEXT, settled_at TEXT)")
    for r in rows:
        conn.execute("INSERT INTO matches VALUES (?,?,?,?)",
                     (r["match_id"], r["home"], r["away"], r.get("league", "ATP Beijing")))
        conn.execute("INSERT INTO predictions VALUES (?,?,?,?,?)",
                     (r["match_id"], "TENNIS", r["esito"], r["fin"], "2026-10-02T10:00:00Z"))
    conn.commit()
    conn.close()
    return db


class TestRatingsFromLedger:
    def test_applica_una_volta(self, tmp_path):
        db = _ledger(tmp_path, [
            {"match_id": "t1", "home": "A", "away": "B", "esito": "1", "fin": "won"},
        ])
        elo = tq.SurfaceElo(db_path=db)
        first = tq.update_ratings_from_ledger(db_path=db, elo=elo)
        assert first["applied"] == 1
        again = tq.update_ratings_from_ledger(db_path=db, elo=tq.SurfaceElo(db_path=db))
        assert again["applied"] == 0 and again["skipped"] == 1

    def test_mappa_il_vincitore_in_entrambe_le_direzioni(self, tmp_path):
        db = _ledger(tmp_path, [
            {"match_id": "t1", "home": "A", "away": "B", "esito": "1", "fin": "won"},
            {"match_id": "t2", "home": "A", "away": "B", "esito": "2", "fin": "won"},
            {"match_id": "t3", "home": "A", "away": "B", "esito": "1", "fin": "lost"},
        ])
        elo = tq.SurfaceElo(db_path=db)
        elo.ensure("A", 0.5); elo.ensure("B", 0.5)
        res = tq.update_ratings_from_ledger(db_path=db, elo=elo)
        assert res["applied"] == 3
        assert elo.matches_played("A") == 3

    def test_push_non_apprende(self, tmp_path):
        db = _ledger(tmp_path, [
            {"match_id": "t1", "home": "A", "away": "B", "esito": "1", "fin": "push"},
        ])
        elo = tq.SurfaceElo(db_path=db)
        res = tq.update_ratings_from_ledger(db_path=db, elo=elo)
        assert res["applied"] == 0 and res["skipped"] == 1

    def test_senza_match_row_salta(self, tmp_path):
        db = _ledger(tmp_path, [])
        import sqlite3
        conn = sqlite3.connect(str(db))
        conn.execute("INSERT INTO predictions VALUES ('ghost','TENNIS','1','won','2026-10-02')")
        conn.commit(); conn.close()
        res = tq.update_ratings_from_ledger(db_path=db)
        assert res["applied"] == 0 and res["skipped"] == 1

    def test_ledger_assente_fail_safe(self, tmp_path):
        res = tq.update_ratings_from_ledger(db_path=tmp_path / "nope" / "x.db")
        assert res["applied"] == 0   # crea lo schema, nessuna riga


# ---------------------------------------------------------------------------
# 9. Tripwire
# ---------------------------------------------------------------------------

class TestTripwires:
    def test_nessun_percorso_di_ordine(self):
        code = _code_only(Path("tennis_quant.py").read_text(encoding="utf-8"))
        for banned in ("_live_fill", "place_limit_order", "resolve_market_for",
                       "save_bet", "execution_engine", "place_order"):
            assert banned not in code, f"tennis_quant non deve contenere {banned}"

    def test_nessuna_formula_duplicata(self):
        code = _code_only(Path("tennis_quant.py").read_text(encoding="utf-8"))
        for banned in ("def devig", "def shin_devig", "def kelly_fraction",
                       "def devig_power", "sqrt("):
            assert banned not in code, f"formula duplicata: {banned}"

    def test_import_leggero(self):
        code = ("import sys, tennis_quant; "
                "assert not any(m in sys.modules for m in "
                "('tracker', 'auto_bet', 'bot', 'decision'))")
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr

    def test_env_dichiarate_nella_iac(self):
        iac = Path(".railway/railway.ts").read_text(encoding="utf-8")
        for env in ("TENNIS_QUANT_ENABLED", "TENNIS_QUANT_DB", "TENNIS_QUANT_LOG",
                    "TENNIS_QUANT_EV_MIN", "TENNIS_QUANT_W_ELO",
                    "TENNIS_QUANT_RETURN_GAMES", "TENNIS_QUANT_KELLY_FRACTION",
                    "TENNIS_QUANT_SHARP_METHOD"):
            assert env in iac, f"{env} non dichiarata in preserve()"

    def test_log_path_legge_env_a_ogni_chiamata(self, monkeypatch, tmp_path):
        """REGRESSIONE: la costante a livello di modulo catturava l'env all'import.

        Dopo la prima lettura, cambiare `TENNIS_QUANT_LOG` non aveva effetto e
        i test scrivevano nel file di PRODUZIONE (`data/tennis_quant/`).
        """
        p1, p2 = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
        monkeypatch.setenv("TENNIS_QUANT_LOG", str(p1))
        assert tq.log_path() == p1
        monkeypatch.setenv("TENNIS_QUANT_LOG", str(p2))
        assert tq.log_path() == p2

    def test_elo_eredita_dal_sandbox(self):
        from tennis_sandbox import TennisElo
        assert issubclass(tq.SurfaceElo, TennisElo)

    def test_modulo_non_importato_dalla_corsia_ordini(self):
        """La corsia di denaro non deve dipendere dal modulo di misura."""
        src = Path("auto_bet.py").read_text(encoding="utf-8")
        assert "import tennis_quant" not in src


# ---------------------------------------------------------------------------
# 10. CLI
# ---------------------------------------------------------------------------

class TestCLI:
    def test_report_exit_zero(self):
        r = subprocess.run([sys.executable, "tennis_quant.py", "--report", "--json"],
                           capture_output=True, text=True)
        assert r.returncode == 0
        assert "rows" in r.stdout

    def test_update_ratings_cli(self):
        r = subprocess.run([sys.executable, "tennis_quant.py", "--update-ratings"],
                           capture_output=True, text=True)
        assert r.returncode == 0


# ---------------------------------------------------------------------------
# 11. Report
# ---------------------------------------------------------------------------

class TestReport:
    def test_summary_e_formato(self):
        s = tq.summary()
        assert "rows" in s and "elo_players" in s
        text = tq.format_report(s)
        assert "Tennis Quant" in text and "nessun ordine" in text

    def test_formato_spento(self):
        assert "SPENTO" in tq.format_report({"enabled": False})
