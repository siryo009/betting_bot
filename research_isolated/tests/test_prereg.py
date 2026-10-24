"""Test del protocollo pre-registrato (tutti OFFLINE, nessuno snapshot).

Cosa difendono:

* l'impronta del protocollo cambia col contenuto e il test rifiuta un file
  modificato dopo la firma (un protocollo riscritto a posteriori non e' un
  protocollo);
* l'holdout si consuma una volta sola;
* lo split e' cronologico e disgiunto;
* il gate "favorito" non si applica a OU/AH (dove scarterebbe lati che il
  sistema gioca davvero);
* la regola di selezione e quella del verdetto sono deterministiche.
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from research_isolated.lab import prereg


# ---------------------------------------------------------------------------
# dati sintetici
# ---------------------------------------------------------------------------
def _row(i: int, *, market="1X2", profit=None, outcome=None, odds=1.8,
         ev=0.03, edge=0.02, prob=0.6, market_prob=0.58, day=None):
    day = day or f"2026-10-{1 + (i // 40) % 20:02d}"
    return {
        "id": i, "match_id": f"m{i}", "mercato": market, "esito": "1",
        "quota": odds, "prob": prob, "ev": ev, "market_prob": market_prob,
        "market_edge": edge, "status": "value", "profit": profit,
        "esito_finale": outcome, "league": "Test League",
        "created_at": f"{day}T10:00:00+00:00",
        "settled_at": f"{day}T12:00:00+00:00",
    }


def frame(n=200, **kw):
    rows = []
    for i in range(n):
        rows.append(_row(i, profit=(0.8 if i % 3 == 0 else -1.0),
                         outcome=("won" if i % 3 == 0 else "lost"), **kw))
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# protocollo
# ---------------------------------------------------------------------------
class TestImpronta:
    def test_hash_stabile(self):
        assert prereg.protocol_hash() == prereg.protocol_hash()
        assert len(prereg.protocol_hash()) == 64

    def test_hash_cambia_col_contenuto(self):
        a = dict(prereg.PROTOCOL)
        b = json.loads(json.dumps(prereg.PROTOCOL))
        b["selection_rule"] = b["selection_rule"] + " (modificata)"
        assert prereg.protocol_hash(a) != prereg.protocol_hash(b)

    def test_firma_e_verifica(self, tmp_path):
        doc = prereg.save_protocol(tmp_path)
        assert doc["hash"] == prereg.protocol_hash()
        assert prereg.verify_protocol(tmp_path)["hash"] == doc["hash"]

    def test_rifiuta_un_protocollo_riscritto(self, tmp_path):
        prereg.save_protocol(tmp_path)
        p = prereg.protocol_path(tmp_path)
        doc = json.loads(p.read_text())
        doc["protocol"]["selection_rule"] = "massimo ROI su TUTTO il campione"
        p.write_text(json.dumps(doc))
        with pytest.raises(RuntimeError, match="impronta"):
            prereg.verify_protocol(tmp_path)

    def test_rifiuta_se_il_codice_differisce_dal_disco(self, tmp_path):
        prereg.save_protocol(tmp_path)
        p = prereg.protocol_path(tmp_path)
        doc = json.loads(p.read_text())
        doc["protocol"] = dict(doc["protocol"])
        doc["protocol"]["version"] = "0.9"
        doc["hash"] = prereg.protocol_hash(doc["protocol"])
        p.write_text(json.dumps(doc))
        with pytest.raises(RuntimeError, match="differisce"):
            prereg.verify_protocol(tmp_path)

    def test_non_sovrascrive_una_firma_diversa(self, tmp_path):
        prereg.save_protocol(tmp_path)
        p = prereg.protocol_path(tmp_path)
        doc = json.loads(p.read_text())
        doc["hash"] = "0" * 64
        p.write_text(json.dumps(doc))
        with pytest.raises(RuntimeError, match="DIVERSA"):
            prereg.save_protocol(tmp_path)

    def test_protocollo_assente_non_si_verifica(self, tmp_path):
        with pytest.raises(RuntimeError, match="assente"):
            prereg.verify_protocol(tmp_path)


# ---------------------------------------------------------------------------
# universo e split
# ---------------------------------------------------------------------------
class TestUniverso:
    def test_scarta_le_aperte_e_i_campi_mancanti(self):
        df = pd.DataFrame([_row(1, profit=1.0, outcome="won"),
                           _row(2, profit=None, outcome=None),
                           _row(3, profit=0.0, outcome="push")])
        u = prereg.universe(df)
        assert len(u) == 2
        assert set(u["esito_finale"]) == {"won", "push"}

    def test_dedup_per_partita_ed_esito_tiene_la_prima(self):
        df = pd.DataFrame([_row(1, profit=-1.0, outcome="lost"),
                           _row(1, profit=-1.0, outcome="lost")])
        u = prereg.universe(df)
        assert len(u) == 1
        assert int(u.iloc[0]["id"]) == 1

    def test_non_filtra_sullo_status(self):
        """Lo `status` registra il tier dell'ERA: usarlo come filtro
        mescolerebbe strategie diverse (lezione del 22/09)."""
        df = pd.DataFrame([_row(1, profit=-1.0, outcome="lost")])
        df.loc[0, "status"] = "rejected"
        assert len(prereg.universe(df)) == 1

    def test_mercato_normalizzato(self):
        df = pd.DataFrame([_row(1, market="ah", profit=-1.0, outcome="lost")])
        assert prereg.universe(df).iloc[0]["market"] == "AH"


class TestSplit:
    def test_cronologico_e_disgiunto(self):
        u = prereg.universe(frame(100))
        sp = prereg.chronological_split(u)
        assert len(sp["train"]) == 50
        assert len(sp["validation"]) == 25
        assert len(sp["test"]) == 25
        ids = [set(g["id"]) for g in (sp["train"], sp["validation"], sp["test"])]
        assert not (ids[0] & ids[1]) and not (ids[1] & ids[2]) and not (ids[0] & ids[2])
        assert sp["train"]["created"].max() <= sp["validation"]["created"].min()
        assert sp["validation"]["created"].max() <= sp["test"]["created"].min()

    def test_bounds_coerenti(self):
        sp = prereg.chronological_split(prereg.universe(frame(80)))
        assert sp["bounds"]["test"][0] is not None


# ---------------------------------------------------------------------------
# gate
# ---------------------------------------------------------------------------
class TestGate:
    def test_fascia_quote(self):
        df = prereg.universe(pd.DataFrame([
            _row(1, odds=1.10, profit=-1.0, outcome="lost"),
            _row(2, odds=1.50, profit=-1.0, outcome="lost"),
            _row(3, odds=2.50, profit=-1.0, outcome="lost")]))
        g = prereg.apply_gate(df, {"odds_min": 1.20, "odds_max": 2.00,
                                   "ev_min": 0.0, "edge_min": 0.0,
                                   "favourite_min": 0.0, "markets": ["1X2", "OU"]})
        assert list(g["id"]) == [2]

    def test_favorito_solo_su_1x2(self):
        """Su OU/AH il gate favorito scarterebbe il lato Under/Away che il
        sistema gioca: e' un errore di riproduzione, non una regola."""
        df = prereg.universe(pd.DataFrame([
            _row(1, market="1X2", market_prob=0.40, profit=-1.0, outcome="lost"),
            _row(2, market="OU", market_prob=0.40, profit=-1.0, outcome="lost")]))
        g = prereg.apply_gate(df, {"odds_min": 1.0, "odds_max": 3.0,
                                   "ev_min": 0.0, "edge_min": 0.0,
                                   "favourite_min": 0.50,
                                   "markets": ["1X2", "OU", "AH"]})
        assert list(g["id"]) == [2]

    def test_soglie_ev_e_edge(self):
        df = prereg.universe(pd.DataFrame([
            _row(1, ev=0.01, edge=0.02, profit=-1.0, outcome="lost"),
            _row(2, ev=0.05, edge=0.005, profit=-1.0, outcome="lost"),
            _row(3, ev=0.05, edge=0.05, profit=-1.0, outcome="lost")]))
        g = prereg.apply_gate(df, {"odds_min": 1.0, "odds_max": 3.0,
                                   "ev_min": 0.02, "edge_min": 0.02,
                                   "favourite_min": 0.0, "markets": ["1X2"]})
        assert list(g["id"]) == [3]

    def test_solo_1x2_esclude_gli_altri(self):
        df = prereg.universe(pd.DataFrame([
            _row(1, market="1X2", profit=-1.0, outcome="lost"),
            _row(2, market="OU", profit=-1.0, outcome="lost")]))
        g = prereg.apply_gate(df, {"odds_min": 1.0, "odds_max": 3.0, "ev_min": 0.0,
                                   "edge_min": 0.0, "favourite_min": 0.0,
                                   "markets": ["1X2"]})
        assert list(g["market"]) == ["1X2"]

    def test_famiglia_dichiarata(self):
        gates = prereg.candidate_gates()
        assert len(gates) == prereg.PROTOCOL["candidates"]["count"]
        labels = {g["label"] for g in gates}
        assert len(labels) == len(gates)


# ---------------------------------------------------------------------------
# selezione e test
# ---------------------------------------------------------------------------
class TestSelezione:
    def test_seleziona_il_migliore_ammissibile(self):
        rows = []
        for i in range(400):
            # 3 righe su 4 a quota 1.50 vincenti, 1 su 4 a 1.95 perdente: il
            # candidato con odds_max 1.80 tiene solo le vincenti e ha n >= MIN_VAL,
            # quindi deve vincere la selezione.
            if i % 4:
                rows.append(_row(i, odds=1.50, profit=0.5, outcome="won"))
            else:
                rows.append(_row(i, odds=1.95, profit=-1.0, outcome="lost"))
        sp = prereg.chronological_split(prereg.universe(pd.DataFrame(rows)))
        sel = prereg.select(sp)
        assert sel["selected"] is not None
        assert sel["selected"]["odds_max"] == 1.80
        assert sel["selected"]["roi"] > 0

    def test_senza_candidati_ammissibili_lo_dichiara(self):
        u = prereg.universe(frame(20))
        sp = prereg.chronological_split(u)
        sel = prereg.select(sp)
        assert sel["selected"] is None
        assert "n >=" in sel["reason"]


class TestTestOneShot:
    def _prepared(self, n=240):
        u = prereg.universe(frame(n))
        sp = prereg.chronological_split(u)
        sel = prereg.select(sp)
        return sp, sel["selected"]

    def test_verdetto_inconcludente_su_campione_piccolo(self, tmp_path):
        sp, _ = self._prepared(60)
        cand = {"odds_min": 1.0, "odds_max": 3.0, "ev_min": 0.0, "edge_min": 0.0,
                "favourite_min": 0.0, "markets": ["1X2"], "label": "tutto"}
        res = prereg.run_test(sp, cand, outdir=tmp_path, write=True)
        assert res["verdict"] in ("INCONCLUSIVE", "FAIL", "PASS")
        assert (res["test"]["n"] < prereg.MIN_TEST) == (res["verdict"] == "INCONCLUSIVE")

    def test_un_secondo_test_rifiuta(self, tmp_path):
        sp, cand = self._prepared()
        prereg.run_test(sp, cand, outdir=tmp_path, write=True)
        with pytest.raises(RuntimeError, match="gia' consumato"):
            prereg.run_test(sp, cand, outdir=tmp_path, write=True)

    def test_senza_firma_nessun_consumo(self, tmp_path):
        sp, cand = self._prepared()
        prereg.run_test(sp, cand, outdir=tmp_path, write=False)
        assert prereg.holdout_used(tmp_path) is None

    def test_marker_contiene_protocollo_e_finestra(self, tmp_path):
        sp, cand = self._prepared()
        res = prereg.run_test(sp, cand, outdir=tmp_path, write=True)
        used = prereg.holdout_used(tmp_path)
        assert used["protocol_hash"] == res["protocol_hash"]
        assert used["verdict"] == res["verdict"]
        assert used["used_at"]

    def test_il_test_non_usa_la_validation(self):
        """Il verdetto dipende SOLO dal tratto di test: cambiare la validation
        non deve cambiare l'esito (nessun trapelare di soglie)."""
        u = prereg.universe(frame(240))
        sp = prereg.chronological_split(u)
        cand = {"odds_min": 1.0, "odds_max": 3.0, "ev_min": 0.0, "edge_min": 0.0,
                "favourite_min": 0.0, "markets": ["1X2"], "label": "tutto"}
        a = prereg.run_test(sp, cand, write=False)
        sp2 = dict(sp)
        sp2["validation"] = sp["validation"].iloc[0:0]
        b = prereg.run_test(sp2, cand, write=False)
        assert a["test"] == b["test"]


class TestBenchmarkDiMercato:
    """Il confronto col mercato e' DESCRITTIVO: non deve poter cambiare il
    verdetto (altrimenti sarebbe un criterio aggiunto dopo la firma)."""

    def _gate(self):
        return {"odds_min": 1.0, "odds_max": 3.0, "ev_min": 0.0, "edge_min": 0.0,
                "favourite_min": 0.0, "markets": ["1X2"], "label": "tutto"}

    def test_compra_solo_dove_il_prezzo_batte_il_mercato(self):
        rows = [
            # market_prob 0.50 a quota 2.10 -> EV di mercato +5%: passa
            _row(1, odds=2.10, profit=1.1, outcome="won", market_prob=0.50),
            # market_prob 0.50 a quota 1.90 -> EV di mercato -5%: fuori
            _row(2, odds=1.90, profit=-1.0, outcome="lost", market_prob=0.50),
        ]
        g = prereg.market_benchmark(prereg.universe(pd.DataFrame(rows)), self._gate())
        assert len(g) == 1 and int(g.iloc[0]["id"]) == 1

    def test_senza_market_prob_non_inventa(self):
        df = prereg.universe(frame(20)).drop(columns=["market_prob"])
        g = prereg.market_benchmark(df, self._gate())
        assert len(g) == 0

    def test_non_cambia_il_verdetto(self, tmp_path):
        """Svuotando il benchmark di mercato il verdetto non si muove: se si
        muovesse, il confronto sarebbe un criterio e non un'informazione."""
        u = prereg.universe(frame(240))
        sp = prereg.chronological_split(u)
        cand = {**self._gate(), "odds_max": 1.80}
        res = prereg.run_test(sp, cand, outdir=tmp_path, write=False)
        no_mkt = dict(sp)
        no_mkt["test"] = sp["test"].drop(columns=["market_prob"])
        res2 = prereg.run_test(no_mkt, cand, outdir=tmp_path, write=False)
        assert res2["benchmark_market"]["n"] == 0
        assert res2["verdict"] == res["verdict"]
        assert res2["test"] == res["test"]


class TestScore:
    def test_ci_contiene_la_media(self):
        df = prereg.universe(frame(90))
        s = prereg.score(df)
        assert s["ci95"][0] <= s["roi"] <= s["ci95"][1]

    def test_vuoto_non_esplode(self):
        s = prereg.score(pd.DataFrame())
        assert s["n"] == 0 and s["roi"] is None
