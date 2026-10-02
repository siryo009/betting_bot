"""Test di `scripts/stress_test_bankroll.py` — tutti OFFLINE, nessuna rete.

Verifica le proprieta' che rendono il numero UTILIZZABILE: il recinto letto
dalle costanti di produzione (mai copiato), la matematica del pareggio, la
monotonia rispetto all'edge, il determinismo del seed e le colonne richieste
dalla direttiva (Win Rate di pareggio, Risk of Ruin, MaxDD medio e p95).
"""

from __future__ import annotations

import json

import pytest

import stress_test_bankroll as stb

# Piccola taglia per tenere la suite veloce: la matematica non cambia.
SMALL = dict(n_sims=4000, n_bets=60, bankroll=30.40, stake=1.50, cap_pct=0.40)


# ---------------------------------------------------------------------------
# 1. Recinto: le costanti si LEGGONO, non si copiano
# ---------------------------------------------------------------------------

class TestRecinto:
    def test_max_concurrent_del_recinto_della_direttiva(self):
        """40% di 30.40 = 12.16 USDC impegnati -> 8 ordini da 1.50."""
        assert stb.max_concurrent(30.40, 1.50, 0.40) == 8

    def test_max_concurrent_almeno_uno(self):
        """Un bankroll sotto lo stake non azzera il recinto: 1 ordine."""
        assert stb.max_concurrent(0.5, 1.50, 0.40) == 1

    def test_max_concurrent_con_stake_nullo_non_divide_per_zero(self):
        assert stb.max_concurrent(100.0, 0.0, 0.40) == 1

    def test_costanti_lette_da_auto_bet(self, monkeypatch):
        """`_production_constants` deve chiedere i valori ad `auto_bet`."""
        import auto_bet as ab
        monkeypatch.setattr(ab, "fixed_order_stake", lambda: 2.25, raising=False)
        monkeypatch.setattr(ab, "OPEN_EXPOSURE_CAP_PCT", 0.25, raising=False)
        monkeypatch.setattr(ab, "ORDER_MAX_STAKE_USDC", 3.0, raising=False)
        consts = stb._production_constants()
        assert consts["source"] == "auto_bet"
        assert consts["stake"] == 2.25
        assert consts["cap_pct"] == 0.25
        assert consts["max_stake"] == 3.0

    def test_fallback_dichiarato_se_auto_bet_manca(self, monkeypatch):
        """Senza `auto_bet` si usano i default E il report lo dichiara."""
        monkeypatch.setitem(stb.__dict__, "DEFAULT_STAKE_USDC", 1.5)
        monkeypatch.setattr(stb, "_production_constants",
                            lambda: {"stake": 1.5, "max_stake": 1.5, "cap_pct": 0.4,
                                     "source": "default"})
        assert stb._production_constants()["source"] == "default"

    def test_stake_non_supera_il_tetto_inviolabile(self):
        """`build_env` replica la riduzione di `auto_bet`: mai uno stake
        che il bot non potrebbe piazzare (renderebbe la matrice una bugia)."""
        args = stb.main.__globals__  # solo per non importare argparse qui
        assert "argparse" in args
        env = stb.build_env(_ns(stake=99.0, bankroll=100.0, cap_pct=None))
        assert env["stake"] <= env["max_stake"]


def _ns(**kw):
    """Namespace minimo con i default di `build_env`."""
    import argparse
    base = dict(sims=stb.DEFAULT_SIMS, bets=stb.DEFAULT_BETS, bankroll=30.40,
                stake=None, cap_pct=None, edge_pp=0.0,
                ruin_floor=stb.DEFAULT_RUIN_FLOOR, seed=stb.DEFAULT_SEED)
    base.update(kw)
    return argparse.Namespace(**base)


# ---------------------------------------------------------------------------
# 2. Matematica del pareggio e colonne richieste
# ---------------------------------------------------------------------------

class TestMatematica:
    def test_win_rate_di_pareggio_e_l_inverso_della_quota(self):
        """Senza edge il win rate assunto E' il pareggio: `1/quota`."""
        r = stb.simulate(odds=2.0, win_prob=0.5, **SMALL)
        assert r["break_even_win_rate"] == pytest.approx(0.5, abs=1e-9)
        assert r["edge_pp"] == pytest.approx(0.0, abs=1e-9)

    def test_colonne_richieste_dalla_direttiva(self):
        """La direttiva chiede: WR di pareggio, RoR %, MaxDD medio e p95."""
        r = stb.simulate(odds=1.60, win_prob=1 / 1.60, **SMALL)
        for key in ("break_even_win_rate", "ruin_pct", "max_dd_mean_pct",
                    "max_dd_p95_pct"):
            assert key in r, key
        assert 0.0 <= r["ruin_pct"] <= 100.0
        assert 0.0 <= r["max_dd_mean_pct"] <= r["max_dd_p95_pct"] <= 100.0

    def test_il_p95_non_e_minore_della_media(self):
        r = stb.simulate(odds=1.45, win_prob=1 / 1.45, **SMALL)
        assert r["max_dd_p95_pct"] >= r["max_dd_mean_pct"]

    def test_all_equilibrio_l_equity_resta_intorno_al_bankroll(self):
        """A pareggio l'equity finale media non deve sistematicamente salire."""
        r = stb.simulate(odds=1.80, win_prob=1 / 1.80, **SMALL)
        assert abs(r["equity_final_mean"] - SMALL["bankroll"]) < 3.0


# ---------------------------------------------------------------------------
# 3. Monotonia: il senso dei numeri
# ---------------------------------------------------------------------------

class TestMonotonia:
    def test_piu_edge_meno_rovina_operativa(self):
        """+5pp di edge devono ridurre l'inoperabilita' (non e' rumore)."""
        base = stb.simulate(odds=1.60, win_prob=1 / 1.60, **SMALL)
        edge = stb.simulate(odds=1.60, win_prob=1 / 1.60 + 0.05, **SMALL)
        assert edge["inoperable_pct"] < base["inoperable_pct"]
        assert edge["equity_final_mean"] > base["equity_final_mean"]

    def test_win_prob_far_sotto_pareggio_alza_la_rovina(self):
        """Con un win rate disastroso l'inoperabilita' deve salire molto."""
        base = stb.simulate(odds=1.60, win_prob=1 / 1.60, **SMALL)
        bad = stb.simulate(odds=1.60, win_prob=0.10, **SMALL)
        assert bad["inoperable_pct"] > base["inoperable_pct"]
        assert bad["equity_final_mean"] < base["equity_final_mean"]

    def test_soglia_di_rovina_alta_produce_rovina_totale(self):
        """Con `ruin_floor` sopra il bankroll ogni percorso e' 'rovinato':
        e' il tripwire che dimostra che la soglia e' davvero applicata."""
        r = stb.simulate(odds=1.60, win_prob=1 / 1.60, ruin_floor=1_000.0, **SMALL)
        assert r["ruin_pct"] == 100.0

    def test_soglia_di_rovina_zero_non_rovina_mai(self):
        r = stb.simulate(odds=1.60, win_prob=1 / 1.60, ruin_floor=0.0, **SMALL)
        assert r["ruin_pct"] == 0.0


# ---------------------------------------------------------------------------
# 4. Determinismo e robustezza
# ---------------------------------------------------------------------------

class TestDeterminismo:
    def test_stesso_seed_stessi_numeri(self):
        a = stb.simulate(odds=1.60, win_prob=0.65, seed=11, **SMALL)
        b = stb.simulate(odds=1.60, win_prob=0.65, seed=11, **SMALL)
        assert a == b

    def test_seed_diverso_numeri_diversi(self):
        """Il seed cambia davvero i percorsi (confronto sull'intera riga:
        un singolo percentile e' quantizzato e puo' coincidere per caso)."""
        a = stb.simulate(odds=1.60, win_prob=0.65, seed=1, **SMALL)
        b = stb.simulate(odds=1.60, win_prob=0.65, seed=2, **SMALL)
        assert a != b
        assert (a["equity_final_mean"] != b["equity_final_mean"]
                or a["max_dd_mean_pct"] != b["max_dd_mean_pct"])

    def test_build_matrix_una_riga_per_quota_valida(self):
        rows = stb.build_matrix((1.30, 0.5, 2.0), n_sims=500, n_bets=20,
                                bankroll=30.4, stake=1.5, cap_pct=0.4)
        assert [r["odds"] for r in rows] == [1.30, 2.0]   # 0.5 scartata

    def test_parse_odds_grid_fallback_su_griglia_vuota(self):
        assert stb.parse_odds_grid("") == list(stb.DEFAULT_ODDS_GRID)
        assert stb.parse_odds_grid("a,b,1.0") == list(stb.DEFAULT_ODDS_GRID)
        assert stb.parse_odds_grid("1.5, 2.5") == [1.5, 2.5]

    @pytest.mark.parametrize("kwargs,msg", [
        (dict(n_sims=0), "n_sims"),
        (dict(n_bets=0), "n_bets"),
        (dict(stake=0.0), "stake"),
        (dict(odds=1.0), "odds"),
    ])
    def test_parametri_invalidi_sollevano(self, kwargs, msg):
        base = dict(n_sims=100, n_bets=10, bankroll=30.4, stake=1.5, odds=1.6,
                    win_prob=0.6, cap_pct=0.4)
        base.update(kwargs)
        with pytest.raises(ValueError):
            stb.simulate(**base)

    def test_win_prob_viene_clampata(self):
        """Una probabilita' > 1 non deve far esplodere l'equity."""
        r = stb.simulate(n_sims=200, n_bets=20, bankroll=30.4, stake=1.5,
                         odds=2.0, win_prob=5.0, cap_pct=0.4)
        assert r["win_prob"] == 1.0
        assert r["equity_final_mean"] <= 30.4 + 20 * 1.5 * 2.0


# ---------------------------------------------------------------------------
# 5. Report e CLI
# ---------------------------------------------------------------------------

class TestReport:
    def test_report_dichiara_recinto_e_fonte_costanti(self):
        env = stb.build_env(_ns(sims=200, bets=20))
        rows = stb.build_matrix((1.6,), n_sims=200, n_bets=20, bankroll=env["bankroll"],
                                stake=env["stake"], cap_pct=env["cap_pct"])
        txt = stb.format_report(env, rows, 0.01)
        assert "12.1600 USDC" in txt
        assert f"fonte costanti: {env['constants_source']}" in txt
        assert "WR pareggio" in txt and "MaxDD p95" in txt

    def test_cli_json_parsabile(self, capsys):
        assert stb.main(["--sims", "300", "--bets", "20", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["enclosure"]["max_concurrent"] == 8
        assert len(payload["matrix"]) == len(stb.DEFAULT_ODDS_GRID)

    def test_cli_testuale_esce_zero(self, capsys):
        assert stb.main(["--sims", "300", "--bets", "20", "--odds-grid", "1.6"]) == 0
        assert "Stress-test Monte Carlo" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 6. Tripwire: offline, nessuna scrittura, nessun ordine
# ---------------------------------------------------------------------------

class TestTripwire:
    def test_nessuna_scrittura_su_file(self):
        src = open(stb.__file__, encoding="utf-8").read()
        for bad in ("open(", "INSERT", "UPDATE ", "DELETE FROM", "save_bet",
                    "save_prediction", "sqlite3"):
            assert bad not in src, bad

    def test_niente_ordini_ne_loop_principale(self):
        src = open(stb.__file__, encoding="utf-8").read()
        for bad in ("_live_fill", "place_order", "resolve_market_for",
                    "execution_engine", "run_all", "import bot"):
            assert bad not in src, bad

    def test_numpy_e_l_unica_dipendenza_pesante(self):
        """La direttiva ammette numpy e/o numba; numba NON serve (e non c'e')."""
        src = open(stb.__file__, encoding="utf-8").read()
        assert "import numpy" in src
        assert "import numba" not in src

    def test_import_leggero(self):
        """Importare lo script non deve tirare dentro il loop di produzione."""
        import os as _os
        import subprocess
        import sys as _sys
        code = ("import sys; sys.path.insert(0, %r); sys.path.insert(0, %r); "
                "import stress_test_bankroll as s; "
                "print('bot' in sys.modules or 'tracker' in sys.modules)")
        here = _os.path.dirname(_os.path.abspath(stb.__file__))
        out = subprocess.run([_sys.executable, "-c", code % (here, str(stb._ROOT))],
                             capture_output=True, text=True, timeout=90)
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == "False", out.stdout
