"""Test di `tennis_markov.py` — TUTTI OFFLINE.

Nessuna rete, nessun credito, nessun ordine. Il motore e' matematico puro e le
prove che contano sono le **parita'**:
- la formula chiusa del game contro la DP (la prima stesura della formula era
  SBAGLIATA — `20 p^3 q^3 / (1 - 2pq)` da' 1.076 a p = 0.6 — ed e' stata colta
  proprio da questo confronto);
- il tie-break della fase estesa (sistema lineare) contro la catena ricorsiva
  troncata;
- la simmetria: con due giocatori identici ogni probabilita' deve valere 0.5.
"""
from __future__ import annotations

import ast
import json
import math
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

import pytest

import tennis_markov as tm

ROOT = Path(__file__).resolve().parent


def _code_only(path) -> str:
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and \
                    isinstance(body[0].value, ast.Constant) and \
                    isinstance(body[0].value.value, str):
                node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def _brute_game(p, a, b):
    q = 1.0 - p

    @lru_cache(None)
    def f(x, y):
        if x >= 4 and x - y >= 2:
            return 1.0
        if y >= 4 and y - x >= 2:
            return 0.0
        if x >= 3 and y >= 3:
            d = p * p / (p * p + q * q)
            if x == y:
                return d
            return p + q * d if x > y else p * d
        return p * f(x + 1, y) + q * f(x, y + 1)
    return f(a, b)


def _brute_tiebreak(pa, pb, a, b, tgt=7, cap=80):
    @lru_cache(None)
    def f(x, y, i):
        if x >= tgt and x - y >= 2:
            return 1.0
        if y >= tgt and y - x >= 2:
            return 0.0
        if i > cap:
            return 0.5
        pw = pa if tm._tiebreak_server(i) == "a" else 1.0 - pb
        return pw * f(x + 1, y, i + 1) + (1 - pw) * f(x, y + 1, i + 1)
    return f(a, b, 0)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    for name in ("TENNIS_MARKOV_ENABLED", "TENNIS_MARKOV_BEST_OF",
                 "TENNIS_MARKOV_TIEBREAK_TO", "TENNIS_MARKOV_TIEBREAK",
                 tm.SURFACE_DELTA_ENV):
        monkeypatch.delenv(name, raising=False)
    yield


# ---------------------------------------------------------------------------
# Configurazione e superficie
# ---------------------------------------------------------------------------
class TestConfig:
    def test_default(self):
        conf = tm.config()
        assert conf["enabled"] is True
        assert conf["best_of"] == 3 and conf["tiebreak"] is True
        assert conf["tiebreak_to"] == 7

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("TENNIS_MARKOV_BEST_OF", "5")
        monkeypatch.setenv("TENNIS_MARKOV_ENABLED", "0")
        conf = tm.config()
        assert conf["best_of"] == 5 and conf["enabled"] is False

    def test_valori_impossibili(self, monkeypatch):
        monkeypatch.setenv("TENNIS_MARKOV_BEST_OF", "abc")
        assert tm.config()["best_of"] == 3

    def test_delta_di_superficie_dichiarato(self):
        assert tm.surface_delta("grass")[0] > 0
        assert tm.surface_delta("clay")[0] < 0
        assert tm.surface_delta("hard") == (0.0, "prior_surface")
        # Superficie ignota -> nessun delta e provenienza dichiarata
        assert tm.surface_delta("boh") == (0.0, "surface_unknown")
        assert tm.surface_delta(None) == (0.0, "surface_unknown")

    def test_delta_da_env(self, monkeypatch):
        monkeypatch.setenv(tm.SURFACE_DELTA_ENV, "clay:-0.05,indoor:0.01")
        assert tm.surface_delta("clay") == (-0.05, "env_surface_delta")
        assert tm.surface_delta("indoor") == (0.01, "env_surface_delta")

    def test_delta_malformato_scartato(self, monkeypatch):
        monkeypatch.setenv(tm.SURFACE_DELTA_ENV, "spazzatura,clay:abc")
        assert tm.surface_delta("clay")[1] == "prior_surface"


# ---------------------------------------------------------------------------
# Punto -> game
# ---------------------------------------------------------------------------
class TestGame:
    def test_formula_chiusa_uguale_alla_dp(self):
        for i in range(1, 200):
            p = i / 200.0
            assert tm.game_prob(p) == pytest.approx(tm.game_prob_dp(p), abs=1e-9)

    def test_valore_di_letteratura(self):
        # p = 0.60 -> 0.7357 (Bartlett); la formula ERRATA dava 1.076
        assert tm.game_prob(0.60) == pytest.approx(0.7357, abs=1e-4)

    def test_simmetria_a_meta(self):
        assert tm.game_prob(0.5) == pytest.approx(0.5)

    def test_dp_da_punteggio_uguale_alla_forza_bruta(self):
        for p in (0.45, 0.55, 0.7, 0.9):
            for a in range(4):
                for b in range(4):
                    assert tm.game_prob_dp(p, points=a, points_against=b) == \
                        pytest.approx(_brute_game(p, a, b), abs=1e-12)

    def test_punteggi_terminali(self):
        assert tm.game_prob_dp(0.6, points=4, points_against=2) == 1.0
        assert tm.game_prob_dp(0.6, points=2, points_against=4) == 0.0

    def test_vantaggio_e_deuce(self):
        # AD-40 (4-3) e' meglio di deuce (3-3)
        assert tm.game_prob_dp(0.6, points=4, points_against=3) > \
            tm.game_prob_dp(0.6, points=3, points_against=3)
        # 40-AD (3-4) e' peggio di deuce
        assert tm.game_prob_dp(0.6, points=3, points_against=4) < \
            tm.game_prob_dp(0.6, points=3, points_against=3)

    def test_monotonia_nel_punto(self):
        vals = [tm.game_prob(i / 100.0) for i in range(1, 100)]
        assert all(b >= a - 1e-12 for a, b in zip(vals, vals[1:]))

    def test_input_estremi_non_solleva(self):
        for p in (-1.0, 0.0, 1.0, 2.0, "abc"):
            got = tm.game_prob(p)
            assert 0.0 <= got <= 1.0


# ---------------------------------------------------------------------------
# Game -> set (tie-break)
# ---------------------------------------------------------------------------
class TestSet:
    def test_simmetria(self):
        assert tm.set_prob(0.5, 0.5) == pytest.approx(0.5)
        assert tm.tiebreak_prob(0.5, 0.5) == pytest.approx(0.5)

    def test_servizio_di_a_da_il_punto(self):
        assert tm._tiebreak_server(0) == "a"
        assert tm._tiebreak_server(1) == "b"
        assert tm._tiebreak_server(2) == "b"
        assert tm._tiebreak_server(3) == "a"
        assert tm._tiebreak_server(4) == "a"
        assert tm._tiebreak_server(5) == "b"

    def test_tiebreak_esteso_uguale_alla_ricorsione(self):
        for pa, pb in ((0.6, 0.55), (0.7, 0.6), (0.5, 0.5), (0.62, 0.58)):
            for a, b in ((5, 5), (6, 6), (7, 7), (8, 8), (10, 10)):
                assert tm.tiebreak_prob(pa, pb, points_a=a, points_b=b) == \
                    pytest.approx(_brute_tiebreak(pa, pb, a, b), abs=1e-6)

    def test_tiebreak_chiude_a_due_di_margine(self):
        assert tm.tiebreak_prob(0.6, 0.5, points_a=7, points_b=5) == 1.0
        assert tm.tiebreak_prob(0.6, 0.5, points_a=5, points_b=7) == 0.0

    def test_set_senza_tiebreak_non_si_blocca(self):
        got = tm.set_prob(0.6, 0.5, games_a=6, games_b=6, tiebreak=False)
        assert 0.0 < got < 1.0

    def test_set_concluso(self):
        assert tm.set_prob(0.6, 0.5, games_a=6, games_b=2) == 1.0
        assert tm.set_prob(0.6, 0.5, games_a=2, games_b=6) == 0.0

    def test_favorito_vince_piu_spesso(self):
        assert tm.set_prob(0.65, 0.60) > 0.5
        assert tm.set_prob(0.55, 0.60) < 0.5

    def test_monotonia_nel_punto(self):
        vals = [tm.set_prob(i / 100.0, 0.60) for i in range(40, 90)]
        assert all(b >= a - 1e-12 for a, b in zip(vals, vals[1:]))


# ---------------------------------------------------------------------------
# Set -> match
# ---------------------------------------------------------------------------
class TestMatch:
    def test_simmetria_best_of_3_e_5(self):
        for bo in (1, 3, 5):
            assert tm.match_prob(0.5, 0.5, best_of=bo) == pytest.approx(0.5)

    def test_best_of_5_e_piu_favorevole_al_favorito(self):
        assert tm.match_prob(0.62, 0.58, best_of=5) > \
            tm.match_prob(0.62, 0.58, best_of=3)

    def test_match_gia_deciso(self):
        assert tm.match_prob(0.6, 0.5, sets_a=2, sets_b=0) == 1.0
        assert tm.match_prob(0.6, 0.5, sets_a=0, sets_b=2) == 0.0

    def test_servizio_alterna_fra_i_set(self):
        assert tm._set_first_server("a", 0) == "a"
        assert tm._set_first_server("a", 1) == "b"
        assert tm._set_first_server("a", 2) == "a"
        assert tm._set_first_server("b", 1) == "a"

    def test_monotonia_nel_punto(self):
        vals = [tm.match_prob(i / 100.0, 0.58) for i in range(40, 90)]
        assert all(b >= a - 1e-12 for a, b in zip(vals, vals[1:]))

    def test_valori_sempre_in_range(self):
        for pa in (0.01, 0.3, 0.5, 0.7, 0.99):
            got = tm.match_prob(pa, 0.5)
            assert 0.0 <= got <= 1.0


# ---------------------------------------------------------------------------
# LIVE
# ---------------------------------------------------------------------------
class TestLive:
    def test_parse_forma_numerica_e_tennistica(self):
        got = tm.parse_live("1-0,3-2,30-15")
        assert got == {"sets_a": 1, "sets_b": 0, "games_a": 3, "games_b": 2,
                       "points_a": 2, "points_b": 1}
        got = tm.parse_live("0-0,0-0,40-AD")
        assert got["points_a"] == 3 and got["points_b"] == 4
        # AD-AD equivale a deuce
        assert tm.parse_live("0-0,0-0,AD-AD")["points_a"] == 3

    def test_parse_stato_illeggibile_solleva(self):
        for bad in ("1-0,3-2", "x-y,3-2,0-0", "1-0,3-2,zz-15", ""):
            with pytest.raises(ValueError):
                tm.parse_live(bad)

    def test_parse_accetta_un_dict(self):
        assert tm.parse_live({"sets_a": 1}) == {"sets_a": 1}

    def test_live_coerente_col_prematch_a_inizio(self):
        """A inizio match (0-0,0-0,0-0) il LIVE deve valere il pre-match."""
        pre = tm.match_prob(0.62, 0.58)
        live = tm.live_match_prob(0.62, 0.58, {"sets_a": 0, "sets_b": 0,
                                               "games_a": 0, "games_b": 0,
                                               "points_a": 0, "points_b": 0})
        assert live["prob"] == pytest.approx(pre, abs=1e-12)
        assert live["prob_before_game"] == pytest.approx(pre, abs=1e-12)

    def test_punto_vinto_alza_la_probabilita(self):
        # Probabilita' VICINE: con 0.65 vs 0.55 il match e' gia' deciso e il
        # singolo punto non sposta nulla (0.8889 in entrambi i casi) — sarebbe
        # un test che non misura il punto.
        base = {"sets_a": 0, "sets_b": 0, "games_a": 2, "games_b": 2,
                "points_a": 2, "points_b": 1}
        won = dict(base, points_a=3)
        lost = dict(base, points_b=2)
        p_base = tm.live_match_prob(0.58, 0.56, base)["prob"]
        p_won = tm.live_match_prob(0.58, 0.56, won)["prob"]
        p_lost = tm.live_match_prob(0.58, 0.56, lost)["prob"]
        assert p_won > p_base > p_lost

    def test_set_vinto_alza_molto(self):
        a = tm.live_match_prob(0.58, 0.56, {"sets_a": 0, "sets_b": 0})["prob"]
        b = tm.live_match_prob(0.58, 0.56, {"sets_a": 1, "sets_b": 0})["prob"]
        assert b > a + 0.10

    def test_prob_e_la_media_pesata_dei_due_rami(self):
        st = {"sets_a": 0, "sets_b": 0, "games_a": 2, "games_b": 3,
              "points_a": 1, "points_b": 2}
        lv = tm.live_match_prob(0.62, 0.58, st, server="a")
        expected = (lv["p_game"] * lv["prob_if_game_won"]
                    + (1.0 - lv["p_game"]) * lv["prob_if_game_lost"])
        assert lv["prob"] == pytest.approx(expected, abs=1e-12)
        assert 0.0 < lv["p_game"] < 1.0

    def test_prob_include_il_game_in_corso(self):
        """`prob` (live) e `prob_before_game` (game azzerato) NON coincidono
        quando il game e' in corso: e' la differenza che il motore esiste per
        misurare."""
        st = {"sets_a": 0, "sets_b": 0, "games_a": 3, "games_b": 3,
              "points_a": 3, "points_b": 0}
        lv = tm.live_match_prob(0.58, 0.56, st, server="a")
        assert lv["prob"] > lv["prob_before_game"]
        empty = {"sets_a": 0, "sets_b": 0, "games_a": 3, "games_b": 3,
                 "points_a": 0, "points_b": 0}
        lv0 = tm.live_match_prob(0.58, 0.56, empty, server="a")
        assert lv0["prob"] == pytest.approx(lv0["prob_before_game"], abs=1e-12)

    def test_a_punteggio_pari_l_ordine_di_servizio_non_cambia_il_set(self):
        """PROPRIETA' del modello (verificata con un simulatore Monte Carlo
        indipendente, non dedotta): in un set con tie-break, a GAME PARI l'ordine
        di servizio NON sposta la probabilita' del set — la rotazione si
        compensa esattamente. Il servizio conta invece a game DISPARI (test
        sotto) e dentro il GAME in corso.

        Se questo test dovesse fallire, il modello avrebbe introdotto un
        vantaggio (o uno svantaggio) di chi serve per primo che non e' stato
        deciso: sarebbe una scoperta, non un dettaglio."""
        for games_a, games_b in ((0, 0), (3, 3), (4, 4), (5, 5)):
            st = {"sets_a": 0, "sets_b": 0, "games_a": games_a,
                  "games_b": games_b, "points_a": 0, "points_b": 0}
            a = tm.live_match_prob(0.58, 0.56, st, server="a")["prob"]
            b = tm.live_match_prob(0.58, 0.56, st, server="b")["prob"]
            assert a == pytest.approx(b, abs=1e-9)

    def test_a_game_dispari_il_servizio_conta(self):
        """La regola e' di PARITA': a game totale DISPARI chi serve il prossimo
        game ha un vantaggio reale, a game totale PARI la rotazione si compensa.
        La differenza NON e' piccola quando c'e' (4-3: +0.060 sul match,
        +0.123 sul set) e vale ZERO esatto quando non c'e'.

        Questa asimmetria e' la ragione per cui la simmetria del test sopra vale
        SOLO a punteggio pari: se valesse sempre, il motore starebbe ignorando
        chi serve. La parita' e' stata verificata su tutta la griglia 0-7 x 0-7
        con una DP indipendente da quella del modulo."""
        for games_a, games_b in ((4, 3), (3, 4), (5, 4), (2, 5)):
            assert (games_a + games_b) % 2 == 1
            st = {"sets_a": 0, "sets_b": 0, "games_a": games_a,
                  "games_b": games_b, "points_a": 0, "points_b": 0}
            a = tm.live_match_prob(0.58, 0.56, st, server="a")["prob"]
            b = tm.live_match_prob(0.58, 0.56, st, server="b")["prob"]
            assert abs(a - b) > 0.01
        # chi serve il prossimo game sta SEMPRE meglio dell'altro, anche
        # quando e' indietro di un game (3-4): il vantaggio del servizio non
        # annulla il ritardo, ma sposta comunque la probabilita' dalla sua
        # parte rispetto a chi riceve.
        for ga, gb in ((4, 3), (3, 4), (5, 4), (2, 5)):
            st = {"sets_a": 0, "sets_b": 0, "games_a": ga, "games_b": gb,
                  "points_a": 0, "points_b": 0}
            a = tm.live_match_prob(0.58, 0.56, st, server="a")["prob"]
            b = tm.live_match_prob(0.58, 0.56, st, server="b")["prob"]
            assert a > b

    def test_tiebreak_simmetrico_rispetto_a_chi_serve_per_primo(self):
        """PROPRIETA' verificata con una DP indipendente (non dedotta): la
        probabilita' del tie-break e' la STESSA se si scambiano i giocatori E
        chi serve per primo — l'unico vantaggio e' la forza al servizio, non
        l'ordine di battuta. Se differisse, il motore starebbe regalando un
        punto a chi vince il sorteggio."""
        pa, pb = 0.58, 0.56
        a_first = tm.tiebreak_prob(pa, pb)
        b_first = 1.0 - tm.tiebreak_prob(pb, pa)
        assert a_first == pytest.approx(b_first, abs=1e-12)
        # il favorito al servizio vince piu' del 50%
        assert a_first > 0.5
        assert tm.tiebreak_prob(pb, pa) < 0.5

    def test_il_servizio_conta_nel_game_in_corso(self):
        """Il vantaggio del servizio si vede DENTRO il game: 40-0 al servizio e'
        molto meglio di 0-40, e chi serve ha il game in mano."""
        ahead = {"sets_a": 0, "sets_b": 0, "games_a": 3, "games_b": 3,
                 "points_a": 3, "points_b": 0}
        behind = dict(ahead, points_a=0, points_b=3)
        p_ahead = tm.live_match_prob(0.58, 0.56, ahead, server="a")["prob"]
        p_behind = tm.live_match_prob(0.58, 0.56, behind, server="a")["prob"]
        assert p_ahead > p_behind
        assert tm.live_match_prob(0.58, 0.56, ahead,
                                  server="a")["p_game"] > 0.9

    def test_stato_live_assurdo_non_solleva(self):
        got = tm.live_match_prob(0.6, 0.5, {"sets_a": 99, "sets_b": 0})
        assert got["prob"] == 1.0


# ---------------------------------------------------------------------------
# Serve/return e delega
# ---------------------------------------------------------------------------
class TestServe:
    def test_inversione_hold_punto(self):
        for hold in (0.6, 0.7, 0.8, 0.9):
            p = tm.point_from_hold(hold)
            assert tm.game_prob(p) == pytest.approx(hold, abs=1e-9)

    def test_hold_da_punto(self):
        assert tm.hold_from_point(0.6) == pytest.approx(tm.game_prob(0.6))

    def test_delega_a_tennis_quant(self, monkeypatch):
        import tennis_quant as tq
        seen = {}

        def _fake(prob):
            seen["prob"] = prob
            return 0.78, 0.74

        monkeypatch.setattr(tq, "hold_rates_from_prob", _fake)
        got = tm.serve_probabilities(0.62, surface="hard", calibrate=False)
        assert seen["prob"] == 0.62
        assert got["hold_source"] == "prior_surface"
        assert got["hold_a"] == pytest.approx(0.78)
        assert got["calibrated"] is False
        # Con la calibrazione (DEFAULT) la coppia arriva dal fallback ma viene
        # RISOLTA per riprodurre l'ingresso: `hold_a` non e' piu' il valore grezzo.
        from tennis_quant import hold_rates_from_prob as _h
        cal = tm.serve_probabilities(0.62, surface="hard")
        raw_a, raw_b = _h(0.62)
        assert cal["calibrated"] is True
        assert cal["raw_holds"] == pytest.approx([raw_a, raw_b])
        assert cal["hold_a"] != pytest.approx(0.78)

    def test_delta_superficie_applicato_al_fallback(self, monkeypatch):
        import tennis_quant as tq
        monkeypatch.setattr(tq, "hold_rates_from_prob", lambda p: (0.78, 0.78))
        clay = tm.serve_probabilities(0.62, surface="clay", calibrate=False)
        grass = tm.serve_probabilities(0.62, surface="grass", calibrate=False)
        assert clay["hold_a"] < 0.78 < grass["hold_a"]
        # Con la calibrazione il delta NON trasforma il numero finale (viene
        # riassorbito: l'obiettivo e' riprodurre l'ingresso) — l'effetto si legge
        # in punti di probabilita'. Senza questo campo il delta sparirebbe in
        # silenzio e passare `surface=` sembrerebbe non fare nulla.
        clay_c = tm.serve_probabilities(0.62, surface="clay")
        grass_c = tm.serve_probabilities(0.62, surface="grass")
        assert clay_c["surface_offset_pp"] < 0.0 < grass_c["surface_offset_pp"]
        assert clay_c["calibrated"] and grass_c["calibrated"]
        f = tm.config()
        assert tm.match_prob(clay_c["p_a"], clay_c["p_b"],
                             best_of=f["best_of"]) == pytest.approx(0.62, abs=1e-6)
        assert tm.match_prob(grass_c["p_a"], grass_c["p_b"],
                             best_of=f["best_of"]) == pytest.approx(0.62, abs=1e-6)

    def test_hold_espliciti_non_vengono_corretti(self):
        """Un dato MISURATO non si corregge con un priore di superficie."""
        got = tm.serve_probabilities(0.5, surface="clay", hold_a=0.80,
                                     hold_b=0.75)
        assert got["hold_a"] == 0.80 and got["hold_b"] == 0.75
        assert got["hold_source"] == "provided"
        assert got["surface_delta"] == pytest.approx(-0.025)

    def test_fallback_se_tennis_quant_non_disponibile(self, monkeypatch):
        import builtins
        real_import = builtins.__import__

        def _fake_import(name, *a, **k):
            if name == "tennis_quant":
                raise ImportError("assente")
            return real_import(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", _fake_import)
        got = tm.serve_probabilities(0.62, surface="hard", calibrate=False)
        assert 0.0 < got["p_a"] < 1.0
        assert got["hold_a"] == pytest.approx(0.80)
        # ...e anche senza `tennis_quant` la calibrazione resta esatta: la media
        # del fallback (0.80) e' un punto di partenza, non la risposta.
        cal = tm.serve_probabilities(0.62, surface="hard")
        assert cal["calibrated"] is True
        assert cal["hold_a"] == pytest.approx(0.822, abs=2e-3)


# ---------------------------------------------------------------------------
# Valutazione e confronto
# ---------------------------------------------------------------------------
class TestEvaluate:
    def test_verdetto_completo(self):
        got = tm.evaluate(prob_a=0.62, surface="hard", price_a=1.85)
        assert got["ok"] is True and got["engine"] == "markov"
        assert 0.0 < got["prob_a"] < 1.0
        assert got["prob_a"] + got["prob_b"] == pytest.approx(1.0)
        assert got["ev"]["a"] is not None

    def test_ev_delega_a_tennis_quant(self):
        import tennis_quant as tq
        got = tm.evaluate(prob_a=0.62, price_a=1.85, price_b=2.10)
        assert got["ev"]["a"] == pytest.approx(
            tq.expected_value(got["prob_a"], 1.85))
        assert got["ev"]["b"] == pytest.approx(
            tq.expected_value(got["prob_b"], 2.10))

    def test_senza_prezzo_nessun_ev(self):
        got = tm.evaluate(prob_a=0.62)
        assert got["ev"] is None

    def test_prezzo_rotto_non_solleva(self):
        got = tm.evaluate(prob_a=0.62, price_a="abc")
        assert got["ev"] is None

    def test_live_incluso_se_richiesto(self):
        got = tm.evaluate(prob_a=0.62, live={"sets_a": 1, "sets_b": 0})
        assert got["live"] is not None
        assert got["live"]["prob"] > got["prob_a"]

    def test_best_of_da_env(self, monkeypatch):
        monkeypatch.setenv("TENNIS_MARKOV_BEST_OF", "5")
        got = tm.evaluate(prob_a=0.62)
        assert got["best_of"] == 5

    def test_confronto_col_poisson(self):
        got = tm.compare_with_poisson(prob_a=0.62, surface="hard")
        assert got["ok"] is True
        assert 0.0 < got["markov"] < 1.0 and 0.0 < got["poisson"] < 1.0
        assert got["diff_pp"] == pytest.approx(
            100.0 * (got["markov"] - got["poisson"]))

    def test_confronto_non_solleva_se_il_modello_manca(self, monkeypatch):
        import tennis_quant as tq
        monkeypatch.setattr(tq, "poisson_prob_from_hold_break",
                            lambda *a, **k: (_ for _ in ()).throw(
                                RuntimeError("rotto")))
        got = tm.compare_with_poisson(prob_a=0.62)
        assert got["ok"] is False
        assert got["reason"] == "confronto_non_disponibile"


# ---------------------------------------------------------------------------
# Report e CLI
# ---------------------------------------------------------------------------
class TestReport:
    def test_report_completo(self):
        block = tm.evaluate(prob_a=0.62, surface="clay", price_a=1.9,
                            live={"sets_a": 0, "sets_b": 0, "games_a": 3,
                                  "games_b": 2, "points_a": 2, "points_b": 1})
        text = tm.format_report(block)
        assert "TENNIS MARKOV" in text and "LIVE" in text
        assert "clay" in text and "EV" in text

    def test_report_su_errore(self):
        assert "non disponibile" in tm.format_report({"ok": False,
                                                      "reason": "errore"})

    def test_cli_json(self, capsys):
        assert tm.main(["--prob", "0.62", "--surface", "clay", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is True and payload["surface"] == "clay"

    def test_cli_live(self, capsys):
        assert tm.main(["--prob", "0.62", "--live", "1-0,3-2,30-15",
                        "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["live"]["state"]["sets_a"] == 1

    def test_cli_compare(self, capsys):
        assert tm.main(["--prob", "0.62", "--compare", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["poisson"] is not None

    def test_cli_senza_prob(self, capsys):
        assert tm.main([]) == 2

    def test_cli_live_illeggibile(self, capsys):
        assert tm.main(["--prob", "0.62", "--live", "boh"]) == 2


# ---------------------------------------------------------------------------
# Tripwire
# ---------------------------------------------------------------------------
class TestTripwire:
    def _src(self) -> str:
        return _code_only(tm.__file__)

    def test_sorgente_senza_scritture(self):
        src = self._src()
        for token in ("INSERT", "UPDATE ", "DELETE FROM", "save_bet(",
                      "save_prediction(", "save_clv(", "unlink("):
            assert token not in src, token

    def test_sorgente_senza_rete_ne_ordini(self):
        src = self._src()
        for token in ("import requests", "httpx", "odds_api", "sx_signals",
                      "_live_fill", "place_limit_order", "resolve_market_for",
                      "execution_engine", "value_filter", "auto_bet"):
            assert token not in src, token

    def test_non_ricopia_hold_break_ne_il_devig(self):
        """Hold/break e de-vig vivono altrove: qui si delegano."""
        src = self._src()
        for token in ("def break_rates", "def hold_rates_from_prob",
                      "def market_implied", "def expected_value"):
            assert token not in src, token

    def test_import_leggero(self):
        code = ("import sys, tennis_markov;"
                "print(any(m in sys.modules for m in "
                "('tracker','auto_bet','bot','odds_api')))")
        out = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT),
                             capture_output=True, text=True, timeout=120)
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == "False"


# ---------------------------------------------------------------------------
# PONTE CALIBRATO (15/10/2026): il ponte derivato e' un CAMBIO DI VARIABILE
# ---------------------------------------------------------------------------
class TestPonteCalibrato:
    GRID = (0.10, 0.25, 0.38, 0.50, 0.55, 0.62, 0.70, 0.75, 0.82, 0.90)

    def test_riproduce_la_probabilita_di_ingresso(self):
        """La catena applicata alla coppia calibrata riproduce l'ingresso.

        E' l'intero scopo della calibrazione: se questo test cade, il ponte e'
        tornato a essere una SECONDA STIMA (con un artefatto dentro) invece di un
        cambio di variabile.
        """
        conf = tm.config()
        for p in self.GRID:
            cal = tm.calibrate_hold_pair(p)
            assert cal["ok"] is True, p
            assert cal["residual"] == pytest.approx(0.0, abs=1e-6), p
            prob = tm.match_prob(tm.point_from_hold(cal["hold_a"]),
                                 tm.point_from_hold(cal["hold_b"]),
                                 best_of=conf["best_of"], tiebreak=conf["tiebreak"])
            assert prob == pytest.approx(p, abs=1e-6), p

    def test_coppia_dentro_il_box_e_ordinata(self):
        for p in self.GRID:
            cal = tm.calibrate_hold_pair(p)
            assert tm.HOLD_MIN <= min(cal["hold_a"], cal["hold_b"])
            assert max(cal["hold_a"], cal["hold_b"]) <= tm.HOLD_MAX
            # chi e' favorito tiene di piu': il verso del differenziale segue la
            # probabilita' (a p < 0.5 il favorito e' B, e la coppia si inverte).
            if p > 0.5:
                assert cal["dnet"] > 0.0 and cal["hold_a"] > cal["hold_b"]
            elif p < 0.5:
                assert cal["dnet"] < 0.0 and cal["hold_a"] < cal["hold_b"]
            else:
                assert cal["dnet"] == pytest.approx(0.0, abs=1e-6)

    def test_differenziale_monotono_nella_probabilita(self):
        dns = [tm.calibrate_hold_pair(p)["dnet"] for p in self.GRID]
        assert dns == sorted(dns)

    def test_media_rispettata(self):
        """La MEDIA (il livello) viene dal fallback, non si inventa: si risolve
        solo il differenziale."""
        for mean in (0.70, 0.80, 0.86):
            cal = tm.calibrate_hold_pair(0.62, mean=mean)
            assert cal["mean"] == pytest.approx(mean)
            assert (cal["hold_a"] + cal["hold_b"]) / 2.0 == pytest.approx(mean, abs=1e-9)

    def test_la_catena_e_monotona_in_dnet(self):
        """Proprieta' su cui si fonda il risolutore (scansione + bisezione)."""
        prev = -1.0
        for step in range(41):
            dn = -0.75 + 1.5 * step / 40.0
            p, _ha, _hb = tm._chain_at_dnet(0.80, dn, best_of=3, tiebreak=True)
            assert p >= prev - 1e-12, dn
            prev = p

    def test_artefatto_rimosso_di_un_ordine_di_grandezza(self):
        """Il ponte GREZZO sbagliava di parecchi punti; quello calibrato no.

        Caso reale misurato il 15/10: input 0.75 con superficie clay -> la catena
        restituiva 0.636 (-11.4pp). Il residuo calibrato e' a livello macchina.
        """
        raw = tm.evaluate(prob_a=0.75, surface="clay", calibrate=False)
        cal = tm.evaluate(prob_a=0.75, surface="clay")
        assert raw["raw_deviation_from_input"] == pytest.approx(-0.114, abs=0.03)
        assert cal["deviation_from_input"] == pytest.approx(0.0, abs=1e-6)
        assert cal["calibration"]["residual"] == pytest.approx(0.0, abs=1e-6)

    def test_superficie_non_sparisce_in_silenzio(self):
        """Con la calibrazione il delta non trasforma il numero: si LEGGE."""
        grass = tm.surface_prob_offset(0.62, "grass")
        clay = tm.surface_prob_offset(0.62, "clay")
        hard = tm.surface_prob_offset(0.62, "hard")
        assert grass["offset_pp"] > 0.0
        assert clay["offset_pp"] < 0.0
        assert hard["offset_pp"] == pytest.approx(0.0, abs=1e-9)
        assert grass["offset_pp"] - hard["offset_pp"] == pytest.approx(
            100.0 * (grass["prob_delta"] - grass["prob_neutral"]), abs=1e-9)

    def test_coppia_misurata_non_si_calibra(self):
        """Un dato da stat provider e' informazione, non una derivazione."""
        got = tm.serve_probabilities(0.62, hold_a=0.85, hold_b=0.70)
        assert got["calibrated"] is False
        assert got["hold_a"] == 0.85 and got["hold_b"] == 0.70
        assert got["hold_source"] == "provided"
        assert got["calibration"] is None

    def test_interruttore_e_default(self, monkeypatch):
        assert tm.calibrate_default() is True
        default = tm.serve_probabilities(0.62)
        assert default["calibrated"] is True
        monkeypatch.setenv("TENNIS_MARKOV_CALIBRATE", "0")
        assert tm.calibrate_default() is False
        assert tm.serve_probabilities(0.62)["calibrated"] is False
        # l'argomento vince sull'env, in entrambe le direzioni
        monkeypatch.setenv("TENNIS_MARKOV_CALIBRATE", "1")
        assert tm.serve_probabilities(0.62, calibrate=False)["calibrated"] is False

    def test_tolleranza_a_input_impossibili(self):
        for bad in (0.0, 1.0, -0.3, 2.0, None, "x"):
            cal = tm.calibrate_hold_pair(bad)
            assert cal["ok"] is False and cal["reason"]

    def test_bounded_fuori_dal_box(self):
        """Un obiettivo estremo non e' raggiungibile: si dichiara, non si finge."""
        cal = tm.calibrate_hold_pair(0.999, mean=0.80)
        assert cal["ok"] is True
        assert cal["bounded"] is True
        assert abs(cal["residual"]) > 1e-3        # il residuo e' ESPOSTO
        assert cal["prob"] <= 0.999

    def test_cache_restituisce_una_copia(self):
        tm._CAL_CACHE.clear()
        first = tm.calibrate_hold_pair(0.62)
        first["hold_a"] = -999.0                     # non deve inquinare la cache
        again = tm.calibrate_hold_pair(0.62)
        assert again["hold_a"] != -999.0
        assert again["ok"] is True

    def test_fail_safe_se_tennis_quant_e_rotto(self, monkeypatch):
        import tennis_quant as tq
        monkeypatch.setattr(tq, "hold_rates_from_prob",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
        tm._CAL_CACHE.clear()
        cal = tm.calibrate_hold_pair(0.62)
        assert cal["ok"] is True
        assert cal["residual"] == pytest.approx(0.0, abs=1e-6)


# ---------------------------------------------------------------------------
# GRANDEZZE A LIVELLO DI GAME + validazione sul ledger
# ---------------------------------------------------------------------------
class TestGameEsatti:
    def test_distribuzione_del_set_normalizzata(self):
        dist = tm.set_outcome_distribution(0.62, 0.61, server="a")
        assert sum(dist.values()) == pytest.approx(1.0, abs=1e-9)
        for (winner, games), p in dist.items():
            assert winner in ("a", "b") and p >= 0.0
            assert games >= 6                      # nessun set si chiude sotto 6-4

    def test_tiebreak_vale_tredici_game(self):
        """A 6-6 il set finisce 7-6: 13 game, non 12."""
        dist = tm.set_outcome_distribution(0.62, 0.61, server="a")
        # la massa sul 7-6 esiste ed e' positiva (nessuna approssimazione a meta')
        winner_prob = sum(p for (_w, _g), p in dist.items())
        assert winner_prob == pytest.approx(1.0, abs=1e-9)
        assert any(g == 13 for (_w, g) in dist)

    def test_probabilita_di_set_parita_col_motore(self):
        p_set = tm.set_prob(0.62, 0.61, server="a")
        dist = tm.set_outcome_distribution(0.62, 0.61, server="a")
        assert sum(p for (w, _g), p in dist.items() if w == "a") == \
            pytest.approx(p_set, abs=1e-9)

    def test_simmetria(self):
        """Stesse tenute -> 0.5 e distribuzione normale."""
        eg = tm.expected_total_games(0.80, 0.80)
        assert eg["ok"] is True
        assert eg["prob_a"] == pytest.approx(0.5, abs=1e-9)
        assert eg["total_prob"] == pytest.approx(1.0, abs=1e-9)
        assert eg["expected_total_games"] > 18.0

    def test_piu_game_con_tenute_alte(self):
        low = tm.expected_total_games(0.70, 0.70)
        high = tm.expected_total_games(0.88, 0.88)
        assert high["expected_total_games"] > low["expected_total_games"]

    def test_best_of_5_ha_piu_game(self):
        tre = tm.expected_total_games(0.80, 0.78, best_of=3)
        cinque = tm.expected_total_games(0.80, 0.78, best_of=5)
        assert cinque["expected_total_games"] > tre["expected_total_games"]

    def test_senza_tiebreak_dichiarato_non_supportato(self):
        with pytest.raises(ValueError):
            tm.set_outcome_distribution(0.62, 0.61, tiebreak=False)
        eg = tm.expected_total_games(0.80, 0.78, tiebreak=False)
        assert eg["ok"] is False and eg["reason"]

    def test_report_riporta_i_game_attesi(self):
        block = tm.evaluate(prob_a=0.62)
        assert block["expected_total_games"] is not None
        assert "E[game totali]" in tm.format_report(block)


class TestValidazioneSulLedger:
    TENNIS_ROWS = [{"sh": 8, "sa": 12, "prob": 0.45},
                   {"sh": 13, "sa": 11, "prob": 0.55},
                   {"sh": 6, "sa": 13, "prob": 0.60},
                   {"sh": 12, "sa": 10, "prob": 0.50}]
    CALCIO_ROWS = [{"sh": 1, "sa": 2, "prob": 0.5},
                   {"sh": 0, "sa": 0, "prob": 0.5},
                   {"sh": 3, "sa": 1, "prob": 0.5}]

    def test_semantica_game_riconosciuta(self):
        stats = tm.ledger_games_stats(self.TENNIS_ROWS)
        assert stats["ok"] is True
        assert stats["semantics"] == "games"
        assert stats["sample"] == 4
        assert stats["observed_mean"] == pytest.approx(
            (20 + 24 + 19 + 22) / 4.0)
        assert stats["prob_source"] == "ledger"

    def test_punteggi_da_calcio_rifiutati(self):
        """0-7 gol non sono game: senza questo controllo la calibrazione
        imparerebbe da un campione sbagliato in modo INVISIBILE."""
        stats = tm.ledger_games_stats(self.CALCIO_ROWS)
        assert stats["ok"] is False
        assert stats["semantics"] == "unknown"
        assert stats["reason"] == "punteggi_non_compatibili_con_i_game"

    def test_campione_vuoto_o_illeggibile(self):
        assert tm.ledger_games_stats([])["ok"] is False
        assert tm.ledger_games_stats([{"sh": None, "sa": "x"}])["reason"] == \
            "nessun_punteggio_utilizzabile"

    def test_ledger_assente_non_solleva(self, tmp_path):
        stats = tm.ledger_games_stats(db=str(tmp_path / "nope.db"))
        assert stats["ok"] is False and stats["reason"] == "ledger_assente"

    def test_previsione_media_monotona_nella_base(self):
        probs = [0.45, 0.5, 0.55, 0.6]
        low = tm.predicted_mean_games(probs, base=0.65)
        high = tm.predicted_mean_games(probs, base=0.88)
        assert low["ok"] and high["ok"]
        assert high["mean_games"] > low["mean_games"]

    def test_base_suggerita_dai_game_osservati(self):
        cal = tm.calibrate_base_hold(rows=self.TENNIS_ROWS,
                                     grid=(0.60, 0.70, 0.80, 0.90))
        assert cal["ok"] is True
        assert cal["semantics"] == "games"
        assert cal["base_current"] is not None
        assert cal["base_suggested"] is not None
        assert cal["monotone"] is True
        assert cal["probs_source"] == "ledger"
        assert "E[game]" in cal["method"]

    def test_base_alta_se_i_game_osservati_sono_tanti(self):
        """Con tanti game servono tenute alte: la misura deve MUOVERSI."""
        tanti = [{"sh": 20, "sa": 18, "prob": 0.5} for _ in range(4)]
        pochi = [{"sh": 8, "sa": 6, "prob": 0.5} for _ in range(4)]
        grid = (0.60, 0.70, 0.75, 0.80, 0.85, 0.90)
        a = tm.calibrate_base_hold(rows=tanti, grid=grid)
        b = tm.calibrate_base_hold(rows=pochi, grid=grid)
        assert a["base_suggested"] > b["base_suggested"]

    def test_su_calcio_non_calibra(self):
        cal = tm.calibrate_base_hold(rows=self.CALCIO_ROWS)
        assert cal["ok"] is False
        assert cal["reason"] == "punteggi_non_compatibili_con_i_game"
        assert cal["base_suggested"] is None

    def test_report_e_cli(self, capsys):
        text = tm.format_games_report(
            tm.ledger_games_stats(self.TENNIS_ROWS))
        assert "GAME dal referto" in text and "media" in text
        assert "non disponibile" in tm.format_games_report(
            {"ok": False, "reason": "x"})
        assert tm.main(["--games", "--json"]) in (0, 1)
        capsys.readouterr()
        assert tm.main(["--calibrate-base", "--json"]) in (0, 1)
        capsys.readouterr()
        assert tm.main([]) == 2
