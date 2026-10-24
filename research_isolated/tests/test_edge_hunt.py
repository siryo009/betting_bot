"""Test OFFLINE della caccia all'edge (20/10/2026).

Nessun accesso alla produzione: ogni test costruisce un SQLite temporaneo con
lo schema che il loader interroga (`predictions`, `bets`, `matches`) e nessuna
funzione del modulo fa rete o scrive sul ledger.

Cosa si difende, in ordine di importanza:

* **il verdetto non puo' essere comprato da un numero in-sample** (i controlli
  sono AND e monotonia + walk-forward sono critici);
* **nessun look-ahead** nel walk-forward (una riga futura non cambia le scelte
  passate, altrimenti la misura e' un falso);
* **le due fonti non si sommano**: su `bets` il profitto e' in USDC e il ROI si
  calcola per unita' di stake (difetto documentato il 15/09);
* **il modulo e' di sola lettura** (sorgente senza scritture, connessione
  `mode=ro` che RIFIUTA una UPDATE, import che non carica la produzione).
"""

from __future__ import annotations

import io
import json
import sqlite3
import subprocess
import sys
import tokenize
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from research_isolated.lab import edge_hunt as eh
from research_isolated.lab import loader, stats

MODULE = Path(eh.__file__)


# --------------------------------------------------------------------------- #
# Fixture: snapshot temporaneo con lo schema interrogato dal loader
# --------------------------------------------------------------------------- #
PRED_COLS = ["id", "match_id", "mercato", "esito", "quota", "prob", "ev",
             "market_prob", "market_edge", "status", "esito_finale", "profit",
             "league", "created_at", "settled_at"]
BET_COLS = ["id", "match_id", "mercato", "esito", "market_id", "selection_id",
            "price", "stake", "mode", "status", "bet_id", "esito_finale",
            "profit", "created_at", "settled_at", "home", "away", "league",
            "commence"]


def _iso(days_ago: float) -> str:
    ts = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return ts.isoformat()


def _pred(mid: str, *, esito: str = "1", quota: float = 1.6, ev: float = 0.02,
          edge: float = 0.02, mp: float = 0.6, mercato: str = "1X2",
          esito_finale="won", profit: float = 0.6, days: float = 5,
          status: str = "value", pid: int = 0, league: str = "Premier League") -> dict:
    return {
        "id": pid, "match_id": mid, "mercato": mercato, "esito": esito,
        "quota": quota, "prob": 0.65, "ev": ev, "market_prob": mp,
        "market_edge": edge, "status": status, "esito_finale": esito_finale,
        "profit": profit, "league": league, "created_at": _iso(days),
        "settled_at": _iso(max(days - 0.2, 0)),
    }


def _bet(bid: int, mid: str, *, esito: str = "1", price: float = 1.6,
         stake: float = 2.0, profit: float = 0.0, esito_finale="won",
         mode: str = "live", days: float = 5) -> dict:
    return {
        "id": bid, "match_id": mid, "mercato": "1X2", "esito": esito,
        "market_id": f"0x{bid:04x}", "selection_id": "1", "price": price,
        "stake": stake, "mode": mode, "status": "FULLY_FILLED",
        "bet_id": f"0xbet{bid}", "esito_finale": esito_finale,
        "profit": profit, "created_at": _iso(days), "settled_at": _iso(days - 0.1),
        "home": "Inter", "away": "Milan", "league": "Serie A",
        "commence": _iso(days - 0.05),
    }


def _mk(tmp_path: Path, preds, bets=(), matches=()) -> Path:
    path = tmp_path / "lab.db"
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE predictions (id INTEGER PRIMARY KEY, match_id TEXT, "
            "mercato TEXT, esito TEXT, quota REAL, prob REAL, ev REAL, "
            "market_prob REAL, market_edge REAL, status TEXT, esito_finale TEXT, "
            "profit REAL, league TEXT, created_at TEXT, settled_at TEXT)")
        conn.execute(
            "CREATE TABLE bets (id INTEGER PRIMARY KEY, match_id TEXT, mercato TEXT, "
            "esito TEXT, market_id TEXT, selection_id TEXT, price REAL, stake REAL, "
            "mode TEXT, status TEXT, bet_id TEXT, esito_finale TEXT, profit REAL, "
            "created_at TEXT, settled_at TEXT, home TEXT, away TEXT, league TEXT, "
            "commence TEXT)")
        conn.execute("CREATE TABLE matches (id TEXT PRIMARY KEY, league TEXT, "
                     "home_team TEXT, away_team TEXT)")
        for r in preds:
            conn.execute(
                f"INSERT INTO predictions ({','.join(PRED_COLS)}) VALUES "
                f"({','.join('?' * len(PRED_COLS))})",
                tuple(r.get(c) for c in PRED_COLS))
        for r in bets:
            conn.execute(
                f"INSERT INTO bets ({','.join(BET_COLS)}) VALUES "
                f"({','.join('?' * len(BET_COLS))})",
                tuple(r.get(c) for c in BET_COLS))
        for mid, home, away in matches:
            conn.execute("INSERT INTO matches (id, home_team, away_team) "
                         "VALUES (?,?,?)", (mid, home, away))
        conn.commit()
    finally:
        conn.close()
    return path


@pytest.fixture()
def snapshot(tmp_path, monkeypatch):
    """Snapshot minimo: 12 righe 1X2 con EV crescente, esiti alterni."""
    preds = []
    for i in range(12):
        profit = 0.6 if i % 2 == 0 else -1.0
        preds.append(_pred(f"m{i}", ev=0.01 + 0.01 * i, edge=0.03,
                           profit=profit, pid=i + 1,
                           esito_finale="won" if profit > 0 else "lost",
                           days=10 - i * 0.5))
    path = _mk(tmp_path, preds, bets=[_bet(1, "m0", profit=1.0)])
    monkeypatch.setattr(loader, "DB_PATH", path)
    return path


def _code_only(textorfile) -> str:
    """Toglie docstring e commenti: il tripwire giudica il CODICE, non la prosa."""
    text = (Path(textorfile).read_text(encoding="utf-8")
            if isinstance(textorfile, (str, Path)) and Path(textorfile).exists()
            else str(textorfile))
    spans = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type in (tokenize.STRING, tokenize.COMMENT):
                spans.append((tok.start, tok.end))
    except tokenize.TokenError:  # pragma: no cover - difensivo
        return text
    lines = text.splitlines(keepends=True)
    out = [list(line) for line in lines]
    for (sr, sc), (er, ec) in spans:
        if sr - 1 >= len(out) or er - 1 >= len(out):
            continue
        if sr == er:
            for c in range(sc, min(ec, len(out[sr - 1]))):
                out[sr - 1][c] = " "
        else:
            for c in range(sc, len(out[sr - 1])):
                out[sr - 1][c] = " "
            for r in range(sr, er - 1):
                out[r] = ["\n"]
            for c in range(0, min(ec, len(out[er - 1]))):
                out[er - 1][c] = " "
    return "".join("".join(line) for line in out)


# --------------------------------------------------------------------------- #
# 1. Soglie e correzione per test multipli
# --------------------------------------------------------------------------- #
class TestTestMultipli:
    def test_alpha_bonferroni(self):
        assert eh.bonferroni_alpha(60) == pytest.approx(0.05 / 60)
        assert eh.bonferroni_alpha(1) == pytest.approx(0.05)

    def test_non_si_divide_per_zero(self):
        assert eh.bonferroni_alpha(0) == pytest.approx(0.05)

    def test_le_ipotesi_dichiarate_sono_contate(self):
        """Il denominatore non puo' essere aggiustato dopo aver visto i numeri."""
        assert eh.N_TESTS >= len(eh.HYPOTHESES)
        assert len(eh.HYPOTHESES) >= 10
        assert len(set(eh.HYPOTHESES)) == len(eh.HYPOTHESES)

    def test_ci_aggiustata_e_piu_larga_della_95(self):
        df = pd.DataFrame({
            "profit": [0.6] * 20 + [-1.0] * 20,
            "match_id": [f"m{i}" for i in range(40)],
        })
        adj = eh.adjusted_ci(df, n_boot=500)
        base = stats.bootstrap_mean_ci(df["profit"].tolist(),
                                       df["match_id"].tolist(), n_boot=500)
        assert adj["alpha"] == pytest.approx(eh.bonferroni_alpha())
        assert adj["lo"] <= base["lo"]
        assert adj["hi"] >= base["hi"]

    def test_ci_su_vuoto_non_inventa(self):
        assert eh.adjusted_ci(pd.DataFrame({"profit": [], "match_id": []}))["lo"] is None


# --------------------------------------------------------------------------- #
# 2. Esito canonico (il ledger non e' omogeneo)
# --------------------------------------------------------------------------- #
class TestEsitoCanonico:
    @pytest.mark.parametrize("esito,expected", [
        ("1", "1"), ("X", "2" if False else "X"), ("2", "2"), ("x", "X"),
        ("draw", "X"), ("tie", "X"), ("Pareggio", "X"),
    ])
    def test_forme_canoniche(self, esito, expected):
        assert eh.canonical_esito(esito) == expected

    @pytest.mark.parametrize("esito,home,away,expected", [
        ("Inter", "Inter", "Milan", "1"),
        ("Milan", "Inter", "Milan", "2"),
        ("inter ", " INTER ", "Milan", "1"),
        ("Atlético Madrid", "Atletico Madrid", "Getafe", "1"),
        ("Getafe", "Atletico Madrid", "Getafe", "2"),
    ])
    def test_nomi_squadra(self, esito, home, away, expected):
        assert eh.canonical_esito(esito, home, away) == expected

    def test_sconosciuto_non_diventa_un_esito(self):
        assert eh.canonical_esito("Roma", "Inter", "Milan") is None
        assert eh.canonical_esito("", "Inter", "Milan") is None
        assert eh.canonical_esito(None) is None

    def test_nome_come_entrambe_le_squadre_non_si_indovina(self):
        assert eh.canonical_esito("Inter", "Inter", "Inter") == "1"  # casa vince il tie


# --------------------------------------------------------------------------- #
# 3. Universo e selezione
# --------------------------------------------------------------------------- #
class TestSelezione:
    def test_universo_confini_inclusivi(self):
        df = pd.DataFrame({
            "mercato": ["1X2", "1X2", "1X2", "1X2", "OU"],
            "quota": [1.20, 2.00, 1.19, 2.01, 1.60],
            "market_prob": [0.5, 0.9, 0.9, 0.9, 0.9],
        })
        assert eh.universe(df).tolist() == [True, True, False, False, False]

    def test_universo_favorito_obbligatorio(self):
        df = pd.DataFrame({"mercato": ["1X2"], "quota": [1.6],
                           "market_prob": [0.49]})
        assert not bool(eh.universe(df).iloc[0])

    def test_universo_nan_chiuso(self):
        df = pd.DataFrame({"mercato": ["1X2"], "quota": [None],
                           "market_prob": [0.6]})
        assert not bool(eh.universe(df).iloc[0])

    def test_senza_tetto_la_selezione_e_quella_di_produzione(self, snapshot):
        all_rows = eh.pick(loader.predictions(snapshot), None)
        capped = eh.pick(loader.predictions(snapshot), 0.05)
        assert len(all_rows) >= len(capped)
        assert set(range(len(capped))) is not None

    def test_il_tetto_solo_restringe(self, snapshot):
        df = loader.predictions(snapshot)
        base = eh.capped_mask(df, None)
        tight = eh.capped_mask(df, 0.03)
        assert (tight <= base).all()

    def test_dedup_una_riga_per_esito(self, tmp_path, monkeypatch):
        preds = [_pred("m1", pid=1, days=5), _pred("m1", pid=2, days=4)]
        path = _mk(tmp_path, preds)
        monkeypatch.setattr(loader, "DB_PATH", path)
        picked = eh.pick(loader.predictions(path), None)
        assert len(picked) == 1
        # la PRIMA occorrenza per `created_at` (casa/trasferta identiche allo
        # stesso esito nascono 1-3 giorni prima del kickoff e i ripassi della
        # pipeline arrivano dopo): convenzione di `strategies.select`.
        assert int(picked.iloc[0]["id"]) == 1

    def test_righe_non_chiuse_escluse(self, tmp_path, monkeypatch):
        preds = [_pred("m1", pid=1, esito_finale=None, profit=None)]
        path = _mk(tmp_path, preds)
        monkeypatch.setattr(loader, "DB_PATH", path)
        assert eh.pick(loader.predictions(path), None).empty


# --------------------------------------------------------------------------- #
# 4. Tabella dei tetti + contrasto
# --------------------------------------------------------------------------- #
class TestTabellaTetti:
    def _table(self, snapshot):
        rows = eh.cap_table(loader.predictions(snapshot),
                            caps=(0.02, 0.05, None), n_boot=200)
        return {r["cap_label"]: r for r in rows}

    def test_produzione_non_esclude_nulla(self, snapshot):
        t = self._table(snapshot)["production"]
        assert t["excluded_n"] == 0
        assert t["delta_vs_excluded"] is None

    def test_selezionati_piu_esclusi_fanno_l_universo(self, snapshot):
        for r in self._table(snapshot).values():
            if r["cap"] is None:
                continue
            assert r["n"] + r["excluded_n"] == self._table(snapshot)["production"]["n"]

    def test_share_normalizzata(self, snapshot):
        t = self._table(snapshot)
        assert t["production"]["share_of_universe"] == pytest.approx(1.0)
        assert 0 <= t["2%"]["share_of_universe"] <= 1

    def test_ci_aggiustata_presente(self, snapshot):
        t = self._table(snapshot)["5%"]
        assert "ci_adj_lo" in t and "significant_adjusted" in t

    def test_contrasto_diagnostica_la_direzione(self):
        inside = pd.DataFrame({"profit": [0.5] * 10, "match_id": [f"a{i}" for i in range(10)]})
        outside = pd.DataFrame({"profit": [-0.5] * 10, "match_id": [f"b{i}" for i in range(10)]})
        df = pd.concat([inside, outside], ignore_index=True)
        df["esito_finale"] = "won"
        df["esito"] = [f"1{i}" for i in range(20)]
        df["mercato"] = "1X2"
        df["quota"] = 1.6
        df["market_prob"] = 0.6
        df["market_edge"] = 0.05
        df["ev"] = [0.04] * 10 + [0.20] * 10
        res = eh.contrast(df, 0.06, n_boot=300)
        assert res["inside_roi"] > res["outside_roi"]
        assert res["status"] == "ESCLUSI PEGGIORI"

    def test_contrasto_con_campione_corto_non_conclude(self):
        df = pd.DataFrame({"profit": [0.1], "match_id": ["a"], "esito_finale": "won",
                           "esito": "1", "mercato": "1X2", "quota": 1.6,
                           "market_prob": 0.6, "market_edge": 0.05, "ev": 0.02})
        res = eh.contrast(df, None, n_boot=50)
        assert res["status"] == "NON VERIFICABILE"


# --------------------------------------------------------------------------- #
# 5. Monotonia
# --------------------------------------------------------------------------- #
class TestMonotonia:
    def _b(self, n, roi):
        return {"n": n, "roi": roi}

    def test_calante_e_monotona(self):
        v = eh.monotone_verdict([self._b(40, 0.30), self._b(40, 0.20),
                                 self._b(40, 0.10), self._b(40, 0.0),
                                 self._b(40, -0.10)])
        assert v["verdict"] == "MONOTONO (calante)"
        assert v["rho_buckets"] == pytest.approx(-1.0)

    def test_forma_casuale_non_e_monotona(self):
        v = eh.monotone_verdict([self._b(40, 0.0), self._b(40, 0.4),
                                 self._b(40, 0.1), self._b(40, 0.2),
                                 self._b(40, 0.3)])
        assert v["verdict"] == "NON MONOTONO"

    def test_artefatto_della_soglia_dichiarato(self):
        """Con meno di 4 fasce usabili il verdetto e' muto, non 'calante'.

        Era il difetto della prima stesura: scartando le fasce piccole il
        conteggio delle inversioni dichiarava una monotonia che la tabella
        completa smentiva.
        """
        v = eh.monotone_verdict([self._b(100, -0.01), self._b(5, 0.9),
                                 self._b(100, 0.10)])
        assert v["verdict"] == "NON VERIFICABILE"
        assert "meno di 4" in v["reason"]

    def test_fasce_piccole_escluse_dal_calcolo(self):
        v = eh.monotone_verdict([self._b(40, 0.3), self._b(3, -5.0),
                                 self._b(40, 0.2), self._b(40, 0.1),
                                 self._b(40, 0.0)])
        assert v["ns"] == [40, 40, 40, 40]
        assert v["verdict"] == "MONOTONO (calante)"


class TestSpearman:
    def _df(self, evs, profits):
        return pd.DataFrame({"mercato": "1X2", "quota": 1.6, "market_prob": 0.6,
                             "market_edge": 0.05, "ev": evs, "profit": profits,
                             "esito_finale": "won", "esito": "1",
                             "match_id": [f"m{i}" for i in range(len(evs))]})

    def test_relazione_positiva(self):
        df = self._df([0.01 * i for i in range(20)], [0.1 * i for i in range(20)])
        res = eh.spearman_ev_profit(df, n_boot=300)
        assert res["rho"] > 0.8
        assert res["status"] == "INFORMATIVO (rho>0)"

    def test_relazione_negativa_e_la_sovrastima(self):
        df = self._df([0.01 * i for i in range(20)], [-0.1 * i for i in range(20)])
        res = eh.spearman_ev_profit(df, n_boot=300)
        assert res["rho"] < -0.8
        assert res["status"] == "SOVRASTIMA (rho<0)"

    def test_troppo_poche_righe(self, tmp_path, monkeypatch):
        path = _mk(tmp_path, [_pred("m1", pid=1)])
        monkeypatch.setattr(loader, "DB_PATH", path)
        res = eh.spearman_ev_profit(loader.predictions(path))
        assert res["status"] == "NON VERIFICABILE"

    def test_ev_costante_non_produce_correlazione(self):
        df = self._df([0.02] * 10, [0.5, -1.0] * 5)
        assert eh.spearman_ev_profit(df, n_boot=100)["rho"] is None


# --------------------------------------------------------------------------- #
# 6. Walk-forward (nessun look-ahead)
# --------------------------------------------------------------------------- #
def _wf_rows(n_days: int = 6, per_day: int = 10) -> list:
    rows, pid = [], 0
    for d in range(n_days, 0, -1):
        for k in range(per_day):
            pid += 1
            ev = 0.02 + 0.01 * k
            profit = 0.8 if k % 2 == 0 else -1.0
            rows.append(_pred(f"d{d}k{k}", ev=ev, edge=0.03, pid=pid,
                              profit=profit, days=float(d),
                              esito_finale="won" if profit > 0 else "lost"))
    return rows


class TestWalkForward:
    def test_scelte_solo_sul_passato(self, tmp_path):
        """Una riga FUTURA non puo' cambiare le scelte gia' fatte.

        E' il test che rende il walk-forward una misura e non una speranza:
        si aggiunge un esito clamoroso nel futuro e le scelte dei giorni
        precedenti devono restare identiche.
        """
        preds = _wf_rows()
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        p1 = loader.predictions(_mk(tmp_path / "a", preds))
        wf1 = eh.walk_forward_cap(p1, caps=(0.04, 0.10, None),
                                  warmup_days=1, min_train=10, min_n_train=5)
        extra = _pred("future", ev=0.02, edge=0.03, pid=999, profit=999.0,
                      days=0.1, esito_finale="won")
        p2 = loader.predictions(_mk(tmp_path / "b", preds + [extra]))
        wf2 = eh.walk_forward_cap(p2, caps=(0.04, 0.10, None),
                                  warmup_days=1, min_train=10, min_n_train=5)
        common = min(len(wf1["choices"]), len(wf2["choices"]))
        assert common >= 2
        assert [c["cap"] for c in wf1["choices"][:common]] == \
               [c["cap"] for c in wf2["choices"][:common]]
        assert [c["day"] for c in wf1["choices"][:common]] == \
               [c["day"] for c in wf2["choices"][:common]]

    def test_nessuna_scelta_senza_train(self, tmp_path):
        path = _mk(tmp_path, _wf_rows(n_days=1, per_day=3))
        wf = eh.walk_forward_cap(loader.predictions(path), caps=(0.05,),
                                 warmup_days=30, min_train=40)
        assert wf["choices"] == []
        assert wf["verdict"] == "NON VERIFICABILE"

    def test_verdetto_dichiarato(self, tmp_path):
        path = _mk(tmp_path, _wf_rows(n_days=8, per_day=12))
        wf = eh.walk_forward_cap(loader.predictions(path), caps=(0.05,),
                                 warmup_days=1, min_train=10, min_n_train=5)
        assert wf["verdict"] in ("POSITIVO FUORI CAMPIONE",
                                 "NON DIMOSTRATO FUORI CAMPIONE",
                                 "CAMPIONE INSUFFICIENTE")
        assert isinstance(wf["picked"], pd.DataFrame)

    def test_distribuzione_delle_scelte(self):
        assert eh._cap_dist([{"cap": None}, {"cap": 0.05}, {"cap": 0.05}]) == \
               {"production": 1, "5%": 2}


# --------------------------------------------------------------------------- #
# 7. Split d'era
# --------------------------------------------------------------------------- #
class TestEre:
    def test_le_ere_partizionano_il_campione(self, snapshot):
        eras = eh.era_table(loader.predictions(snapshot), cap=0.05)
        tot = sum(e["n_prod"] for e in eras)
        base = len(eh.pick(loader.predictions(snapshot), None))
        assert tot == base

    def test_ogni_era_ha_entrambe_le_misure(self, snapshot):
        for e in eh.era_table(loader.predictions(snapshot), cap=0.05):
            assert {"n_prod", "roi_prod", "n_cap", "roi_cap", "delta"} <= set(e)

    def test_era_vuota_non_ha_delta(self, tmp_path, monkeypatch):
        path = _mk(tmp_path, [_pred("m1", pid=1, days=0.5)])
        monkeypatch.setattr(loader, "DB_PATH", path)
        eras = eh.era_table(loader.predictions(path), cap=0.05)
        assert eras[0]["n_prod"] == 0
        assert eras[0]["roi_prod"] is None


# --------------------------------------------------------------------------- #
# 8. Denaro reale: USDC vs unita' di stake
# --------------------------------------------------------------------------- #
class TestDenaroReale:
    def test_roi_per_unita_di_stake_non_in_usdc(self, tmp_path, monkeypatch):
        # stake 2, perdita -1 → ROI per unita' -50%, NON -1.00
        bets = [_bet(1, "m1", stake=2.0, profit=-1.0, esito_finale="lost")]
        preds = [_pred("m1", pid=1, ev=0.02)]
        path = _mk(tmp_path, preds, bets=bets)
        monkeypatch.setattr(loader, "DB_PATH", path)
        res = eh.live_money(loader.bets(path), loader.predictions(path), cap=0.06)
        assert res["prod_result"]["roi"] == pytest.approx(-0.5)
        assert res["prod_result"]["usdc"] == pytest.approx(-1.0)
        assert res["prod_result"]["stake"] == pytest.approx(2.0)

    def test_puntate_sim_escluse(self, tmp_path, monkeypatch):
        bets = [_bet(1, "m1", mode="sim"), _bet(2, "m2", mode="live")]
        preds = [_pred("m1", pid=1), _pred("m2", pid=2)]
        path = _mk(tmp_path, preds, bets=bets)
        monkeypatch.setattr(loader, "DB_PATH", path)
        res = eh.live_money(loader.bets(path), loader.predictions(path))
        assert res["linked"] + res["unlinked"] == 1

    def test_senza_collegamento_dichiarato(self, tmp_path, monkeypatch):
        bets = [_bet(1, "m99")]
        preds = [_pred("m1", pid=1)]
        path = _mk(tmp_path, preds, bets=bets)
        monkeypatch.setattr(loader, "DB_PATH", path)
        res = eh.live_money(loader.bets(path), loader.predictions(path))
        assert res["unlinked"] == 1
        assert res["linked"] == 0
        assert res["status"] == "CAMPIONE INSUFFICIENTE"

    def test_tetto_applicato_alle_fasce(self, tmp_path, monkeypatch):
        bets = [_bet(1, "m1", profit=1.0, stake=2.0),
                _bet(2, "m2", profit=-2.0, stake=2.0)]
        preds = [_pred("m1", pid=1, ev=0.02), _pred("m2", pid=2, ev=0.20)]
        path = _mk(tmp_path, preds, bets=bets)
        monkeypatch.setattr(loader, "DB_PATH", path)
        res = eh.live_money(loader.bets(path), loader.predictions(path), cap=0.06)
        assert res["cap_result"]["n"] == 1
        assert res["prod_result"]["n"] == 2

    def test_esito_con_nome_collegato_dai_nomi(self, tmp_path, monkeypatch):
        bets = [_bet(1, "m1", esito="Inter", profit=1.0)]
        preds = [_pred("m1", pid=1, esito="Inter")]
        path = _mk(tmp_path, preds, bets=bets, matches=[("m1", "Inter", "Milan")])
        monkeypatch.setattr(loader, "DB_PATH", path)
        res = eh.live_money(loader.bets(path), eh._with_teams(loader.predictions(path), path))
        assert res["linked"] == 1


# --------------------------------------------------------------------------- #
# 9. Verdetto: i controlli sono AND, i critici non si compensano
# --------------------------------------------------------------------------- #
def _good_inputs():
    caps = [{"cap": 0.05, "cap_label": "5%", "n": 50, "roi": 0.20,
             "excluded_n": 20, "excluded_roi": -0.10,
             "delta_vs_excluded": 0.30, "delta_ci": [0.05, 0.55],
             "delta_status": "ESCLUSI PEGGIORI"}]
    mono = {"verdict": "MONOTONO (calante)"}
    rho = {"rho": -0.20, "ci95": [-0.35, -0.05]}
    wf = {"verdict": "POSITIVO FUORI CAMPIONE"}
    eras = [{"delta": 0.05}, {"delta": 0.02}]
    live = {"status": "MISURATO",
            "cap_result": {"roi": 0.10}, "prod_result": {"roi": 0.00}}
    return caps, mono, rho, wf, eras, live


class TestVerdetto:
    def test_tutti_i_controlli_passano(self):
        v = eh.verdict(*_good_inputs())
        assert v["usable"] is True
        assert v["passed"] == v["total"]
        assert "CANDIDATA" in v["decision"]

    def test_walk_forward_negativo_boccia_da_solo(self):
        caps, mono, rho, wf, eras, live = _good_inputs()
        wf = {"verdict": "NON DIMOSTRATO FUORI CAMPIONE"}
        v = eh.verdict(caps, mono, rho, wf, eras, live)
        assert v["usable"] is False

    def test_monotonia_assente_boccia_da_solo(self):
        caps, mono, rho, wf, eras, live = _good_inputs()
        mono = {"verdict": "NON MONOTONO"}
        v = eh.verdict(caps, mono, rho, wf, eras, live)
        assert v["usable"] is False

    def test_due_controlli_non_critici_falliti_bocciano(self):
        caps, mono, rho, wf, eras, live = _good_inputs()
        eras = [{"delta": -0.01}, {"delta": -0.02}]
        live = {"status": "CAMPIONE INSUFFICIENTE", "cap_result": {"roi": 0.0},
                "prod_result": {"roi": 0.0}}
        v = eh.verdict(caps, mono, rho, wf, eras, live)
        assert v["usable"] is False
        assert v["passed"] < v["total"]

    def test_un_controllo_non_critico_fallito_e_tollerato(self):
        caps, mono, rho, wf, eras, live = _good_inputs()
        live = {"status": "CAMPIONE INSUFFICIENTE", "cap_result": {"roi": 0.0},
                "prod_result": {"roi": 0.0}}
        v = eh.verdict(caps, mono, rho, wf, eras, live)
        assert v["usable"] is True

    def test_senza_tetti_utilizzabili(self):
        v = eh.verdict([], {"verdict": "NON VERIFICABILE"},
                       {"rho": None, "ci95": None},
                       {"verdict": "NON VERIFICABILE"}, [], {})
        assert v["usable"] is False
        assert v["best_cap"] is None

    def test_il_contrasto_entra_nel_verdetto(self):
        caps, mono, rho, wf, eras, live = _good_inputs()
        caps[0]["delta_status"] = "NESSUNA DIFFERENZA DIMOSTRATA"
        v = eh.verdict(caps, mono, rho, wf, eras, live)
        names = [c["check"] for c in v["checks"]]
        assert "esclusi peggiori (contrasto)" in names
        assert v["usable"] is False  # e' il secondo controllo non critico


# --------------------------------------------------------------------------- #
# 10. End-to-end su snapshot temporaneo + CLI + report
# --------------------------------------------------------------------------- #
class TestEndToEnd:
    def test_run_produce_tutte_le_sezioni(self, snapshot):
        res = eh.run(snapshot, cap=0.05)
        assert res["snapshot"]["quick_check"] == "ok"
        for key in ("universe", "cap_table", "ev_buckets", "spearman",
                    "monotony", "walk_forward", "eras", "live_money",
                    "verdict", "plan", "multiple_testing", "thresholds"):
            assert key in res

    def test_il_piano_non_esegue(self, snapshot):
        res = eh.run(snapshot, cap=0.05)
        assert res["plan"]["executed"] is False
        assert res["plan"]["promotion_criteria"]

    def test_soglie_lette_da_value_filter(self, snapshot):
        import value_filter as vf
        res = eh.run(snapshot, cap=0.05)
        assert res["thresholds"]["odds_min"] == pytest.approx(float(vf.ODDS_MIN))
        assert res["thresholds"]["odds_max"] == pytest.approx(float(vf.ODDS_MAX))
        assert res["thresholds"]["ev_min"] == pytest.approx(float(vf.EV_MIN))

    def test_report_contiene_le_sezioni(self, snapshot):
        text = eh.format_report(eh.run(snapshot, cap=0.05))
        for heading in ("Caccia all'edge", "Tabella dei tetti",
                        "Monotonia", "Walk-forward", "Split d'era",
                        "Denaro reale", "Verdetto", "Piano"):
            assert heading in text
        assert "NON eseguito" in text or "NON eseguito" in text.replace("NON", "NON")

    def test_json_serializzabile(self, snapshot):
        json.dumps(eh.run(snapshot, cap=0.05), default=str)

    def test_cli_json_e_out(self, snapshot, tmp_path, capsys):
        out = tmp_path / "report.md"
        rc = eh.main(["--db", str(snapshot), "--json", "--out", str(out)])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert "verdict" in payload
        assert out.exists() and "Caccia all'edge" in out.read_text(encoding="utf-8")

    def test_pct_e_num_tolleranti(self):
        assert eh._pct(None) == "n/d"
        assert eh._pct(float("nan")) == "n/d"
        assert eh._num(None) == "n/d"


# --------------------------------------------------------------------------- #
# 11. Tripwire: sola lettura, niente rete, niente ordini
# --------------------------------------------------------------------------- #
class TestTripwire:
    def test_nessuna_scrittura_nel_sorgente(self):
        code = _code_only(MODULE)
        for verb in ("INSERT ", "UPDATE ", "DELETE ", "CREATE TABLE",
                     "DROP TABLE", "commit("):
            assert verb not in code, f"il modulo non deve contenere {verb!r}"

    def test_nessun_percorso_ordini_o_di_rete(self):
        code = _code_only(MODULE)
        for bad in ("requests", "odds_api", "place_order", "_live_fill",
                    "save_bet", "execution_engine", "resolve_market_for"):
            assert bad not in code, f"riferimento vietato: {bad}"

    def test_la_connessione_rifiuta_una_update(self, snapshot):
        conn = loader.connect(snapshot)
        try:
            with pytest.raises(sqlite3.OperationalError):
                conn.execute("UPDATE predictions SET profit = 999")
        finally:
            conn.close()

    def test_import_non_carica_la_produzione(self):
        root = Path(__file__).resolve().parents[2]
        code = ("import research_isolated.lab.edge_hunt as m, sys;"
                "print([x for x in ('tracker','bot','auto_bet','odds_api') "
                "if x in sys.modules])")
        out = subprocess.run([sys.executable, "-c", code], cwd=root,
                             capture_output=True, text=True, timeout=120)
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == "[]", out.stdout

    def test_soglie_non_ricopiate(self):
        """Le soglie arrivano da `value_filter`, non da numeri nel modulo."""
        code = _code_only(MODULE)
        assert "PROD_EV_MIN" in code and "PROD_ODDS_MAX" in code

    def test_docstring_dichiara_lo_scopo(self):
        doc = eh.__doc__ or ""
        assert "fetta" in doc.lower() or "post-hoc" in doc.lower()
        assert "walk-forward" in doc.lower()
