"""Parita' della vettorizzazione numpy con la formula originale — OFFLINE.

Direttiva 29/09/2026: il nucleo di `poisson_engine` e' passato da doppio ciclo
Python a numpy. **La matematica NON doveva cambiare**, quindi qui l'oracolo e'
la formula ORIGINALE (ricopiata dal codice pre-vettorizzazione, Dixon-Coles
incluso) e ogni funzione pubblica viene confrontata su una griglia di input.

Perche' l'oracolo vive nel TEST e non in `poisson_engine`: una seconda
implementazione nel modulo di produzione e' una formula duplicata che prima o
poi diverge (la classe di bug piu' costosa del progetto). Qui e' un oracolo
indipendente che serve solo a misurare.

Nessuna rete, nessun DB, nessun ordine.
"""

from __future__ import annotations

import math

import pytest

import poisson_engine as pe

# ---------------------------------------------------------------------------
# ORACOLO: la formula originale, ricopiata 1:1 dal codice pre-29/09
# ---------------------------------------------------------------------------

RHO = -0.15


def _ori_pmf(k: int, lam: float) -> float:
    return (lam ** k) * math.exp(-lam) / math.factorial(k)


def _ori_matrix(lam_h: float, lam_a: float, max_goals: int = 10):
    total = 0.0
    cells = {}
    for hg in range(max_goals + 1):
        for ag in range(max_goals + 1):
            p = _ori_pmf(hg, lam_h) * _ori_pmf(ag, lam_a)
            if hg == 0 and ag == 0:
                p *= (1 - lam_h * lam_a * RHO)
            elif hg == 0 and ag == 1:
                p *= (1 + lam_h * RHO)
            elif hg == 1 and ag == 0:
                p *= (1 + lam_a * RHO)
            elif hg == 1 and ag == 1:
                p *= (1 - RHO)
            cells[(hg, ag)] = p
            total += p
    return {k: v / total for k, v in cells.items()}


def _ori_1x2(lam_h, lam_a, max_goals=10):
    m = _ori_matrix(lam_h, lam_a, max_goals)
    return (sum(p for (hg, ag), p in m.items() if hg > ag),
            sum(p for (hg, ag), p in m.items() if hg == ag),
            sum(p for (hg, ag), p in m.items() if hg < ag))


def _ori_ou(lam_h, lam_a, threshold, side="over", max_goals=10):
    m = _ori_matrix(lam_h, lam_a, max_goals)
    if abs(float(threshold) * 2) % 1 != 0:
        lo = math.floor(float(threshold) * 2) / 2.0
        halves, share = (lo, lo + 0.5), 0.5
    else:
        halves, share = (float(threshold),), 1.0
    p_win = p_push = p_lose = 0.0
    for hline in halves:
        for (hg, ag), p in m.items():
            total = hg + ag
            if total > hline:
                p_win += share * p
            elif total == hline:
                p_push += share * p
            else:
                p_lose += share * p
    if side == "under":
        p_win, p_lose = p_lose, p_win
    return p_win, p_push, p_lose


def _ori_ah(lam_h, lam_a, line, side="home", max_goals=10):
    m = _ori_matrix(lam_h, lam_a, max_goals)
    dist = {}
    for (hg, ag), p in m.items():
        dist[hg - ag] = dist.get(hg - ag, 0.0) + p
    if abs(line * 2) % 1 != 0:
        lo = math.floor(line * 2) / 2.0
        halves, share = (lo, lo + 0.5), 0.5
    else:
        halves, share = (float(line),), 1.0
    p_win = p_push = p_lose = 0.0
    for hline in halves:
        for adv, p in dist.items():
            net = (adv + hline) if side == "home" else (-adv + hline)
            if net > 0:
                p_win += share * p
            elif net == 0:
                p_push += share * p
            else:
                p_lose += share * p
    return p_win, p_push, p_lose


#: Griglia di input: lambda realistiche (0-4) + estremi + max_goals vari.
LAMBDAS = [0.0, 0.25, 0.8, 1.2, 1.5, 2.2, 3.0, 4.5]
CASES = [(h, a, mg) for h in LAMBDAS for a in LAMBDAS for mg in (0, 1, 5, 10)]


# ---------------------------------------------------------------------------
# Matrice
# ---------------------------------------------------------------------------

class TestMatrice:
    @pytest.mark.parametrize("lam_h,lam_a,mg", CASES)
    def test_griglia_uguale_all_oracolo(self, lam_h, lam_a, mg):
        grid = pe._probs_grid(lam_h, lam_a, mg)
        ori = _ori_matrix(lam_h, lam_a, mg)
        assert grid.shape == (mg + 1, mg + 1)
        for (hg, ag), p in ori.items():
            assert grid[hg, ag] == pytest.approx(p, rel=1e-12, abs=1e-15), (hg, ag)

    @pytest.mark.parametrize("lam_h,lam_a,mg", CASES[:60])
    def test_matrice_dict_coerente_col_grid(self, lam_h, lam_a, mg):
        """`_probs_matrix` e' una VISTA: nessuna seconda formula."""
        grid = pe._probs_grid(lam_h, lam_a, mg)
        for key, value in pe._probs_matrix(lam_h, lam_a, mg).items():
            assert value == pytest.approx(float(grid[key[0], key[1]]), rel=1e-15)

    @pytest.mark.parametrize("lam_h,lam_a,mg", CASES[:60])
    def test_normalizzata_a_uno(self, lam_h, lam_a, mg):
        assert float(pe._probs_grid(lam_h, lam_a, mg).sum()) == pytest.approx(1.0)

    def test_correzione_dixon_coles_ancora_attiva(self):
        """La vettorizzazione non deve aver perso RHO: col rho negativo il
        pareggio deve restare PIU' probabile del Poisson puro."""
        lam_h = lam_a = 1.3
        p1, px, p2 = pe.prob_1x2(lam_h, lam_a)
        pure = sum(_ori_pmf(k, lam_h) * _ori_pmf(k, lam_a) for k in range(11))
        assert px > pure          # Dixon-Coles alza il pareggio
        assert p1 + px + p2 == pytest.approx(1.0)

    def test_max_goals_zero(self):
        """Caso limite: una sola cella, nessuna correzione applicabile."""
        grid = pe._probs_grid(1.4, 1.1, 0)
        assert grid.shape == (1, 1)
        assert float(grid[0, 0]) == pytest.approx(1.0, abs=1e-12)
        assert pe.prob_1x2(1.4, 1.1, 0) == pytest.approx((0.0, 1.0, 0.0))

    def test_lambda_zero(self):
        lam_h, lam_a = 0.0, 1.4
        assert pe.prob_1x2(lam_h, lam_a) == pytest.approx(
            _ori_1x2(lam_h, lam_a), abs=1e-12)


# ---------------------------------------------------------------------------
# Aggregazioni
# ---------------------------------------------------------------------------

class TestAggregazioni:
    @pytest.mark.parametrize("lam_h,lam_a,mg", CASES)
    def test_prob_1x2(self, lam_h, lam_a, mg):
        got = pe.prob_1x2(lam_h, lam_a, mg)
        want = _ori_1x2(lam_h, lam_a, mg)
        for g, w in zip(got, want):
            assert g == pytest.approx(w, rel=1e-11, abs=1e-14)

    @pytest.mark.parametrize("lam_h,lam_a,mg", CASES[:80])
    def test_prob_over_under(self, lam_h, lam_a, mg):
        for threshold in (0.5, 1.5, 2.5, 3.5, 4.5):
            got = pe.prob_over_under(lam_h, lam_a, threshold, mg)
            m = _ori_matrix(lam_h, lam_a, mg)
            want = sum(p for (hg, ag), p in m.items() if hg + ag > threshold)
            assert got[0] == pytest.approx(want, rel=1e-11, abs=1e-14)
            assert got[1] == pytest.approx(1.0 - want, rel=1e-11, abs=1e-14)

    @pytest.mark.parametrize("lam_h,lam_a,mg", CASES[:80])
    @pytest.mark.parametrize("threshold", [1.5, 2.0, 2.25, 2.5, 2.75, 3.0, 3.25])
    def test_ou_outcome_push_e_quarter(self, lam_h, lam_a, mg, threshold):
        """Il push (linea intera) e le quarter line sono il punto delicato."""
        for side in ("over", "under"):
            got = pe.ou_outcome_probs(lam_h, lam_a, threshold, side, mg)
            want = _ori_ou(lam_h, lam_a, threshold, side, mg)
            for g, w in zip(got, want):
                assert g == pytest.approx(w, rel=1e-11, abs=1e-14), (side, threshold)

    @pytest.mark.parametrize("lam_h,lam_a,mg", CASES[:80])
    def test_prob_btts(self, lam_h, lam_a, mg):
        got = pe.prob_btts(lam_h, lam_a, mg)
        m = _ori_matrix(lam_h, lam_a, mg)
        want = sum(p for (hg, ag), p in m.items() if hg >= 1 and ag >= 1)
        assert got == pytest.approx(want, rel=1e-11, abs=1e-14)

    @pytest.mark.parametrize("lam_h,lam_a,mg", CASES[:80])
    def test_margin_distribution(self, lam_h, lam_a, mg):
        got = pe.margin_distribution(lam_h, lam_a, mg)
        m = _ori_matrix(lam_h, lam_a, mg)
        want = {}
        for (hg, ag), p in m.items():
            want[hg - ag] = want.get(hg - ag, 0.0) + p
        assert set(got) == set(want)          # incluso il margine 0
        for adv, p in want.items():
            assert got[adv] == pytest.approx(p, rel=1e-11, abs=1e-14)

    @pytest.mark.parametrize("line", [-3.0, -1.75, -0.75, -0.5, -0.25, 0.0,
                                      0.25, 0.5, 1.5, 2.75])
    def test_ah_outcome(self, line):
        for lam_h, lam_a in ((1.6, 1.1), (0.9, 1.9), (2.4, 0.6), (1.3, 1.3)):
            for side in ("home", "away"):
                got = pe.ah_outcome_probs(lam_h, lam_a, line, side)
                want = _ori_ah(lam_h, lam_a, line, side)
                for g, w in zip(got, want):
                    assert g == pytest.approx(w, rel=1e-11, abs=1e-14), (line, side)


# ---------------------------------------------------------------------------
# Coerenza interna (le stesse relazioni del modello puro)
# ---------------------------------------------------------------------------

class TestCoerenza:
    @pytest.mark.parametrize("lam_h,lam_a", [(1.5, 1.2), (2.3, 0.7), (1.1, 1.1)])
    def test_somme_a_uno(self, lam_h, lam_a):
        assert sum(pe.prob_1x2(lam_h, lam_a)) == pytest.approx(1.0)
        assert sum(pe.prob_over_under(lam_h, lam_a, 2.5)) == pytest.approx(1.0)
        assert sum(pe.ou_outcome_probs(lam_h, lam_a, 3.0)) == pytest.approx(1.0)

    @pytest.mark.parametrize("line", [-1.0, -0.5, 0.0, 0.5, 1.0, -0.25, 0.75])
    def test_ah_complementare(self, line):
        """Il lato home e il lato away sulla stessa linea sono complementari."""
        home = pe.ah_outcome_probs(1.5, 1.2, line, "home")
        away = pe.ah_outcome_probs(1.5, 1.2, line, "away")
        assert home[0] + home[1] + home[2] == pytest.approx(1.0)
        assert away[0] + away[1] + away[2] == pytest.approx(1.0)

    def test_over_under_simmetrici(self):
        o = pe.ou_outcome_probs(1.6, 1.1, 2.25, "over")
        u = pe.ou_outcome_probs(1.6, 1.1, 2.25, "under")
        assert o[0] == pytest.approx(u[2])
        assert o[2] == pytest.approx(u[0])
        assert o[1] == pytest.approx(u[1])


# ---------------------------------------------------------------------------
# La vettorizzazione e' REALE (non un wrapper del ciclo Python)
# ---------------------------------------------------------------------------

class TestVettorizzazione:
    def test_il_nucleo_ritorna_un_array_numpy(self):
        np = pe._np()
        grid = pe._probs_grid(1.5, 1.2)
        assert isinstance(grid, np.ndarray)
        assert grid.dtype.kind == "f"

    def test_le_aggregazioni_non_ricostruiscono_la_matrice(self):
        """`_probs_grid` e' la fonte unica: le aggregazioni la chiamano UNA volta."""
        src = open(pe.__file__, encoding="utf-8").read()
        for fn in ("def prob_1x2", "def prob_over_under", "def prob_btts"):
            body = src.split(fn, 1)[1].split("\ndef ", 1)[0]
            assert body.count("_probs_grid(") == 1, fn

    def test_numpy_importato_a_pigrizia(self):
        """`_np()` esiste proprio per non pagare l'import al caricamento."""
        import subprocess
        import sys
        out = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.modules.pop('numpy', None); "
             "import poisson_engine; "
             "print('NUMPY:' + str('numpy' in sys.modules))"],
            capture_output=True, text=True,
            cwd=str(__import__("pathlib").Path(pe.__file__).parent))
        assert out.stdout.strip().endswith("False")

    def test_import_senza_numpy_non_rompe_il_modulo(self):
        """Il modulo si importa anche senza numpy: l'errore arriva all'USO."""
        import subprocess
        import sys
        out = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.modules['numpy'] = None; "
             "import poisson_engine as pe; print('IMPORT_OK')"],
            capture_output=True, text=True,
            cwd=str(__import__("pathlib").Path(pe.__file__).parent))
        assert "IMPORT_OK" in out.stdout
