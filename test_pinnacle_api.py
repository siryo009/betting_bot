"""test_pinnacle_api.py — probe del modello TOP-DOWN (Pinnacle = oracolo).

Tutti i test sono OFFLINE per default: payload finti costruiti a mano, cache in
`tmp_path`, **zero rete, zero crediti, zero ordini**. L'unico test che tocca
davvero the-odds-api e' marcato `integration` ed e' doppio-opt-in (serve
`PINNACLE_PROBE=1`): costa 1 credito e serve a MISURARE `x-requests-last`,
cioe' a rispondere con un numero alla domanda "quanto costa il confronto
continuo?" invece che con un'opinione.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import pytest

import pinnacle_oracle as po


# ---------------------------------------------------------------------------
# Payload finti (stessa forma di the-odds-api v4 /odds)
# ---------------------------------------------------------------------------

def _book(key, title, home, away, prices):
    outcomes = []
    for name, price in zip((home, "Draw", away), prices):
        if price is not None:
            outcomes.append({"name": name, "price": price})
    return {"key": key, "title": title, "last_update": "2026-09-25T10:00:00Z",
            "markets": [{"key": "h2h", "last_update": "2026-09-25T10:00:00Z",
                         "outcomes": outcomes}]}


def _match(home, away, books, kickoff="2026-09-26T19:00:00Z"):
    return {"id": f"id-{home}", "sport_key": "soccer_usa_mls",
            "commence_time": kickoff, "home_team": home, "away_team": away,
            "bookmakers": books}


def _payload():
    return [
        # oracolo completo + un book non-sharp (non deve mai essere usato)
        _match("Atlanta United", "Toronto FC", [
            _book("pinnacle", "Pinnacle", "Atlanta United", "Toronto FC",
                  (1.75, 3.60, 4.50)),
            _book("draftkings", "DraftKings", "Atlanta United", "Toronto FC",
                  (1.60, 3.90, 5.50)),
        ]),
        # nessun book sharp
        _match("Inter Miami", "New York City", [
            _book("draftkings", "DraftKings", "Inter Miami", "New York City",
                  (2.10, 3.40, 3.20)),
        ]),
        # sharp incompleto: un prezzo e' <= 1.0 -> resta senza oracolo
        _match("Columbus Crew", "Chicago Fire", [
            _book("pinnacle", "Pinnacle", "Columbus Crew", "Chicago Fire",
                  (1.95, 3.50, 1.0)),
        ]),
        # assenza di mercato h2h (solo totals)
        {"id": "id-only-totals", "sport_key": "soccer_usa_mls",
         "commence_time": "2026-09-26T19:00:00Z",
         "home_team": "Seattle Sounders", "away_team": "Portland Timbers",
         "bookmakers": [{"key": "pinnacle", "title": "Pinnacle",
                         "markets": [{"key": "totals", "outcomes": [
                             {"name": "Over", "price": 1.9,
                              "point": 2.5}]}]}]},
    ]


PINNACLE = {"1": 1.75, "X": 3.60, "2": 4.50}


# ---------------------------------------------------------------------------
# 1. ESTRAZIONE
# ---------------------------------------------------------------------------

class TestEstrazione:
    def test_estrae_il_1x2_completo_di_pinnacle(self):
        assert po.pinnacle_quotes(_payload(), "Atlanta United",
                                  "Toronto FC") == PINNACLE

    def test_ignora_i_book_non_sharp(self):
        # DraftKings e' presente: se venisse usato le quote sarebbero altre.
        got = po.pinnacle_quotes(_payload(), "Atlanta United", "Toronto FC")
        assert got is not None and got["1"] == 1.75

    def test_senza_pinnacle_nessun_oracolo(self):
        assert po.pinnacle_quotes(_payload(), "Inter Miami",
                                  "New York City") is None

    def test_fail_closed_con_due_esiti_su_tre(self):
        """Due esiti su tre NON sono un oracolo: il margine del terzo
        verrebbe attribuito agli altri due, in silenzio."""
        assert po.pinnacle_quotes(_payload(), "Columbus Crew",
                                  "Chicago Fire") is None

    def test_mercato_diverso_da_h2h_non_e_oracolo(self):
        assert po.pinnacle_quotes(_payload(), "Seattle Sounders",
                                  "Portland Timbers") is None

    def test_partita_inesistente(self):
        assert po.pinnacle_quotes(_payload(), "Non Esiste", "Nemmeno") is None

    def test_normalizzazione_di_maiuscole_e_spazi(self):
        assert po.pinnacle_quotes(_payload(), "  atlanta   united ",
                                  "TORONTO FC") == PINNACLE

    def test_riconoscimento_sharp_su_key_o_titolo(self):
        assert po.is_sharp("pinnacle")
        assert po.is_sharp("Pinnacle")
        assert po.is_sharp("PINNACLE")
        assert not po.is_sharp("draftkings")
        assert not po.is_sharp("")
        assert not po.is_sharp(None)

    def test_riconoscimento_sharp_sul_titolo_quando_la_key_e_ignota(self):
        bm = _book("unknown-book", "PINNACLE", "A", "B", (1.9, 3.3, 4.0))
        bm["key"] = None                       # solo il titolo e' affidabile
        assert po.is_sharp(bm.get("key") or bm.get("title")) is True

    def test_iter_ritorna_solo_le_partite_con_oracolo_completo(self):
        hits = po.iter_pinnacle_markets(_payload())
        assert len(hits) == 1
        match, quotes = hits[0]
        assert match["home_team"] == "Atlanta United"
        assert quotes == PINNACLE

    def test_payload_ostile_non_solleva(self):
        for weird in (None, [], [None], ["x"], [{"bookmakers": "nope"}],
                      [{"home_team": "A", "bookmakers": [None, 42]}],
                      [{"home_team": "A", "away_team": "B",
                        "bookmakers": [{"key": "pinnacle",
                                         "markets": [None, 7]}]}]):
            assert isinstance(po.iter_pinnacle_markets(weird), list)
        assert po.pinnacle_quotes(["x", None], "A", "B") is None
        assert po.pinnacle_quotes(None, "A", "B") is None


# ---------------------------------------------------------------------------
# 1b. CONSENSO MULTI-ORACOLO (26/09/2026)
# ---------------------------------------------------------------------------

def _sharp_match(books, home="Atlanta United", away="Toronto FC"):
    return _match(home, away, books)


def _fresh_cache(folder, sport, payload):
    """Cache con `ts` recente (serve a `load_oracle`, che scarta le stantie)."""
    import time as _time
    (folder / f"toa_{sport}.json").write_text(
        json.dumps({"ts": _time.time(), "remaining": 400,
                    "payload": payload}), encoding="utf-8")


class TestConsensoMultiOracolo:
    def test_estrae_tutte_le_fonti_sharp_e_ignora_le_soft(self):
        m = _sharp_match([
            _book("pinnacle", "Pinnacle", "Atlanta United", "Toronto FC",
                  (1.75, 3.60, 4.50)),
            _book("betfair_ex_eu", "Betfair Exchange", "Atlanta United",
                  "Toronto FC", (1.72, 3.65, 4.60)),
            _book("matchbook", "Matchbook", "Atlanta United", "Toronto FC",
                  (1.74, 3.62, 4.55)),
            _book("draftkings", "DraftKings", "Atlanta United", "Toronto FC",
                  (1.60, 3.90, 5.50)),
        ])
        books = po.oracle_quotes([m], "Atlanta United", "Toronto FC")
        assert set(books) == {"pinnacle", "betfair_ex_eu", "matchbook"}
        assert books["pinnacle"] == PINNACLE

    def test_la_base_e_media_tra_pinnacle_e_betfair(self):
        m = _sharp_match([
            _book("pinnacle", "Pinnacle", "Atlanta United", "Toronto FC",
                  (1.75, 3.60, 4.50)),
            _book("betfair_ex_eu", "Betfair Exchange", "Atlanta United",
                  "Toronto FC", (1.72, 3.65, 4.60)),
        ])
        c = po.consensus_probabilities(
            po.oracle_quotes([m], "Atlanta United", "Toronto FC"))
        assert c is not None and c["n_sources"] == 2
        assert c["sources"] == ["pinnacle", "betfair_ex_eu"]
        pin = po.true_probabilities(PINNACLE)
        bf = po.true_probabilities({"1": 1.72, "X": 3.65, "2": 4.60})
        for e in "1X2":
            assert c[e] == pytest.approx((pin[e] + bf[e]) / 2.0, abs=1e-6)
        assert abs(c["1"] + c["X"] + c["2"] - 1.0) < 1e-5

    def test_mediana_configurabile(self):
        m = _sharp_match([
            _book("pinnacle", "Pinnacle", "Atlanta United", "Toronto FC",
                  (1.75, 3.60, 4.50)),
            _book("betfair_ex_eu", "Betfair Exchange", "Atlanta United",
                  "Toronto FC", (1.72, 3.65, 4.60)),
        ])
        c = po.consensus_probabilities(
            po.oracle_quotes([m], "Atlanta United", "Toronto FC"),
            method="median")
        assert c["consensus_method"] == "median"
        pin = po.true_probabilities(PINNACLE)
        bf = po.true_probabilities({"1": 1.72, "X": 3.65, "2": 4.60})
        for e in "1X2":
            assert c[e] == pytest.approx((pin[e] + bf[e]) / 2.0, abs=1e-6)

    def test_validatore_conferma_quando_vicino(self):
        m = _sharp_match([
            _book("pinnacle", "Pinnacle", "Atlanta United", "Toronto FC",
                  (1.75, 3.60, 4.50)),
            _book("betfair_ex_eu", "Betfair Exchange", "Atlanta United",
                  "Toronto FC", (1.72, 3.65, 4.60)),
            _book("matchbook", "Matchbook", "Atlanta United", "Toronto FC",
                  (1.73, 3.63, 4.58)),
        ])
        c = po.consensus_probabilities(
            po.oracle_quotes([m], "Atlanta United", "Toronto FC"))
        assert c["n_sources"] == 3 and c["validated"] is True
        assert c["sources"] == ["pinnacle", "betfair_ex_eu", "matchbook"]

    def test_validatore_escluso_quando_diverge(self):
        m = _sharp_match([
            _book("pinnacle", "Pinnacle", "Atlanta United", "Toronto FC",
                  (1.75, 3.60, 4.50)),
            _book("matchbook", "Matchbook", "Atlanta United", "Toronto FC",
                  (2.50, 3.60, 4.50)),   # 1 fuori scala
        ])
        c = po.consensus_probabilities(
            po.oracle_quotes([m], "Atlanta United", "Toronto FC"))
        assert c["validated"] is False
        assert c["n_sources"] == 1 and c["sources"] == ["pinnacle"]
        assert c["agreement_pp"] is not None and c["agreement_pp"] > 5.0
        # il consenso ESCLUSO non e' stato usato: e' la Pinnacle pura
        pin = po.true_probabilities(PINNACLE)
        assert c["1"] == pytest.approx(pin["1"], abs=1e-6)

    def test_tolleranza_validatore_configurabile(self):
        m = _sharp_match([
            _book("pinnacle", "Pinnacle", "Atlanta United", "Toronto FC",
                  (1.75, 3.60, 4.50)),
            _book("matchbook", "Matchbook", "Atlanta United", "Toronto FC",
                  (2.50, 3.60, 4.50)),
        ])
        c = po.consensus_probabilities(
            po.oracle_quotes([m], "Atlanta United", "Toronto FC"),
            validator_tolerance=0.50)
        assert c["validated"] is True and c["n_sources"] == 2

    def test_fallback_solo_pinnacle_coincide_col_comportamento_storico(self):
        m = _sharp_match([
            _book("pinnacle", "Pinnacle", "Atlanta United", "Toronto FC",
                  (1.75, 3.60, 4.50)),
        ])
        c = po.consensus_probabilities(
            po.oracle_quotes([m], "Atlanta United", "Toronto FC"))
        pin = po.true_probabilities(PINNACLE)
        assert c["n_sources"] == 1 and c["fallback"] == "pinnacle_only"
        assert c["validated"] is None
        for e in "1X2":
            assert c[e] == pytest.approx(pin[e], abs=1e-6)

    def test_fallback_sul_validatore_senza_benchmark(self):
        m = _sharp_match([
            _book("matchbook", "Matchbook", "Atlanta United", "Toronto FC",
                  (1.74, 3.62, 4.55)),
        ])
        c = po.consensus_probabilities(
            po.oracle_quotes([m], "Atlanta United", "Toronto FC"))
        assert c["fallback"] == "validator_only" and c["sources"] == ["matchbook"]

    def test_fallback_sul_benchmark_senza_pinnacle(self):
        m = _sharp_match([
            _book("betfair_ex_eu", "Betfair Exchange", "Atlanta United",
                  "Toronto FC", (1.72, 3.65, 4.60)),
        ])
        c = po.consensus_probabilities(
            po.oracle_quotes([m], "Atlanta United", "Toronto FC"))
        assert c["fallback"] == "single_source:betfair_ex_eu"
        assert c["sources"] == ["betfair_ex_eu"]

    def test_nessuna_fonte_nessun_oracolo(self):
        assert po.consensus_probabilities({}) is None
        assert po.consensus_probabilities(None) is None
        soft = _sharp_match([
            _book("draftkings", "DraftKings", "Atlanta United", "Toronto FC",
                  (1.60, 3.90, 5.50)),
        ])
        assert po.oracle_quotes([soft], "Atlanta United", "Toronto FC") == {}
        assert po.consensus_probabilities({}) is None

    def test_consenso_disabilitato_usa_la_sola_pinnacle(self):
        m = _sharp_match([
            _book("pinnacle", "Pinnacle", "Atlanta United", "Toronto FC",
                  (1.75, 3.60, 4.50)),
            _book("betfair_ex_eu", "Betfair Exchange", "Atlanta United",
                  "Toronto FC", (1.72, 3.65, 4.60)),
        ])
        c = po.consensus_probabilities(
            po.oracle_quotes([m], "Atlanta United", "Toronto FC"),
            enabled=False)
        assert c["fallback"] == "consensus_disabled"
        assert c["sources"] == ["pinnacle"]
        pin = po.true_probabilities(PINNACLE)
        assert c["1"] == pytest.approx(pin["1"], abs=1e-6)

    def test_i_metadati_non_entrano_in_ev_o_true_odd(self):
        probs = po.consensus_probabilities({
            "pinnacle": PINNACLE,
            "betfair_ex_eu": {"1": 1.72, "X": 3.65, "2": 4.60},
        })
        fair = po.fair_odds(probs)
        assert set(fair) == {"1", "X", "2"}      # niente sources/n_sources
        rows = po.ev_gate(probs, {"1": 2.10}, ev_min=0.02)
        assert {r["esito"] for r in rows} == {"1"}

    def test_canonical_book(self):
        assert po.canonical_book({"key": "pinnacle", "title": "Pinnacle"}) \
            == "pinnacle"
        assert po.canonical_book({"key": "betfair_ex_uk",
                                  "title": "Betfair Exchange"}) \
            == "betfair_ex_eu"
        assert po.canonical_book({"key": None, "title": "Matchbook"}) \
            == "matchbook"
        assert po.canonical_book({"key": "draftkings",
                                  "title": "DraftKings"}) is None
        assert po.canonical_book(None) is None

    def test_load_oracle_usa_il_consenso(self, tmp_path):
        _fresh_cache(tmp_path, "soccer_usa_mls", [_sharp_match([
            _book("pinnacle", "Pinnacle", "Atlanta United", "Toronto FC",
                  (1.75, 3.60, 4.50)),
            _book("betfair_ex_eu", "Betfair Exchange", "Atlanta United",
                  "Toronto FC", (1.72, 3.65, 4.60)),
        ])])
        probs = po.load_oracle("Atlanta United", "Toronto FC",
                               cache_dir=tmp_path)
        assert probs["n_sources"] == 2
        assert probs["sources"] == ["pinnacle", "betfair_ex_eu"]
        # il consenso DIFFERISCE dalla sola Pinnacle (altrimenti non serve)
        pin = po.true_probabilities(PINNACLE)
        assert probs["1"] != pytest.approx(pin["1"], abs=1e-9)

    def test_load_oracle_fallback_solo_pinnacle(self, tmp_path):
        _fresh_cache(tmp_path, "soccer_usa_mls", [_sharp_match([
            _book("pinnacle", "Pinnacle", "Atlanta United", "Toronto FC",
                  (1.75, 3.60, 4.50)),
        ])])
        probs = po.load_oracle("Atlanta United", "Toronto FC",
                               cache_dir=tmp_path)
        assert probs["fallback"] == "pinnacle_only"
        pin = po.true_probabilities(PINNACLE)
        assert probs["1"] == pytest.approx(pin["1"], abs=1e-6)


# ---------------------------------------------------------------------------
# 2. TRUE PROBABILITY (de-vig)
# ---------------------------------------------------------------------------

class TestTrueProbability:
    def test_le_probabilita_fair_sommano_a_uno(self):
        probs = po.true_probabilities(PINNACLE)
        assert probs is not None
        assert abs(sum(v for k, v in probs.items() if k != "overround")
                   - 1.0) < 1e-9

    def test_l_overround_e_il_margine_grezzo_e_sparisce_dalle_fair(self):
        probs = po.true_probabilities(PINNACLE)
        raw = sum(1.0 / o for o in PINNACLE.values())
        assert probs["overround"] == pytest.approx(raw, abs=1e-4)
        assert raw > 1.0                       # il vig c'e'
        # togliendo il vig la somma torna 1: la probabilita' e' "vera"
        assert sum(v for k, v in probs.items() if k != "overround") == \
            pytest.approx(1.0, abs=1e-9)

    def test_power_corregge_il_favourite_longshot_bias(self):
        """E' la ragione per cui il default e' `power`.

        Il devig power non "alza" la probabilita' del favorito rispetto a
        quella implicita grezza (il vig va comunque tolto): alza la sua QUOTA
        rispetto al devig proporzionale, togliendo margine al longshot.
        """
        power = po.true_probabilities(PINNACLE, method="power")
        prop = po.true_probabilities(PINNACLE, method="multiplicative")
        assert power["1"] > prop["1"]          # il favorito sale
        assert power["2"] < prop["2"]          # il longshot scende
        assert power["1"] < 1.0 / PINNACLE["1"]  # il vig e' comunque tolto
        assert abs(power["1"] - 0.5483) < 5e-4   # valore misurato

    def test_default_e_power_coerente_con_il_progetto(self):
        from market_calib import market_implied
        assert po.DEVIG_METHOD == "power"
        assert po.true_probabilities(PINNACLE) == market_implied(PINNACLE)

    def test_metodo_shin_accettato(self):
        probs = po.true_probabilities(PINNACLE, method="shin")
        assert probs is not None
        assert abs(sum(v for k, v in probs.items() if k != "overround")
                   - 1.0) < 1e-6

    def test_metodo_multiplicativo_accettato(self):
        probs = po.true_probabilities(PINNACLE, method="multiplicative")
        assert abs(probs["1"] - (1 / 1.75) / (1 / 1.75 + 1 / 3.6 + 1 / 4.5)) \
            < 1e-9

    def test_meno_di_tre_esiti_non_e_un_oracolo(self):
        assert po.true_probabilities({"1": 1.75, "X": 3.6}) is None
        assert po.true_probabilities({}) is None
        assert po.true_probabilities(None) is None

    def test_true_odd_e_l_inverso_della_probabilita(self):
        probs = po.true_probabilities(PINNACLE)
        fair = po.fair_odds(probs)
        assert fair["1"] == pytest.approx(1.0 / probs["1"], abs=1e-9)
        assert set(fair) == {"1", "X", "2"}    # 'overround' NON e' un esito

    def test_fair_odds_ignora_valori_non_interpretabili(self):
        # input = PROBABILITA' (non quote): un valore > 1 o non numerico non
        # puo' diventare una "true odd" (invertirebbe il segno dell'EV).
        assert po.fair_odds({"1": "x", "2": 0.5, "overround": 1.07}) == \
            {"2": 2.0}
        assert po.fair_odds({"1": 1.2, "X": 0.0, "2": None}) == {}
        assert po.fair_odds({}) == {}
        assert po.fair_odds(None) == {}


# ---------------------------------------------------------------------------
# 3. TRIGGER (EV gate e "True Odd + margine" sono la stessa condizione)
# ---------------------------------------------------------------------------

EV_MIN = 0.02


class TestEvGate:
    def test_senza_prezzo_non_c_e_valore_da_misurare(self):
        probs = po.true_probabilities(PINNACLE)
        rows = po.ev_gate(probs, {}, ev_min=EV_MIN)
        assert rows == []

    def test_le_due_letture_della_direttiva_coincidono(self):
        """EV >= ev_min  <=>  quota >= true_odd x (1 + ev_min).

        E' l'invariante che impedisce due standard diversi nella stessa
        pipeline (e il bug classico: due soglie che si scollano).
        """
        probs = po.true_probabilities(PINNACLE)
        prices = {"1": 1.90, "X": 3.45, "2": 4.60}
        for row in po.ev_gate(probs, prices, ev_min=EV_MIN):
            required = (1.0 / row["prob"]) * (1.0 + EV_MIN)
            assert row["trigger"] is (row["ev"] >= EV_MIN)
            assert row["trigger"] is (row["price"] >= required - 1e-4)
            assert row["required_price"] == pytest.approx(required, abs=1e-3)

    def test_il_trigger_non_scatta_sotto_il_margine(self):
        probs = po.true_probabilities(PINNACLE)
        true_odd_1 = 1.0 / probs["1"]
        # prezzo sopra la true odd ma sotto la true odd + 2% -> niente segnale
        under = true_odd_1 * (1.0 + EV_MIN) - 0.01
        rows = po.ev_gate(probs, {"1": round(under, 3)}, ev_min=EV_MIN)
        assert rows[0]["ev"] < EV_MIN and rows[0]["trigger"] is False
        assert rows[0]["price"] < rows[0]["required_price"]

    def test_il_trigger_scatta_sopra_il_margine(self):
        probs = po.true_probabilities(PINNACLE)
        over = (1.0 / probs["1"]) * (1.0 + EV_MIN) + 0.05
        rows = po.ev_gate(probs, {"1": round(over, 3)}, ev_min=EV_MIN)
        assert rows[0]["trigger"] is True and rows[0]["ev"] > EV_MIN

    def test_ordinati_per_ev_decrescente(self):
        probs = po.true_probabilities(PINNACLE)
        rows = po.ev_gate(probs, {"1": 1.90, "X": 3.90, "2": 4.40},
                          ev_min=EV_MIN)
        assert [r["ev"] for r in rows] == sorted((r["ev"] for r in rows),
                                                 reverse=True)
        assert {r["esito"] for r in rows} == {"1", "X", "2"}

    def test_value_candidates_tiene_solo_i_trigger(self):
        probs = po.true_probabilities(PINNACLE)
        prices = {"1": (1.0 / probs["1"]) * 1.10, "X": 3.60, "2": 4.50}
        cands = po.value_candidates(probs, prices, ev_min=EV_MIN)
        assert [c["esito"] for c in cands] == ["1"]
        assert all(c["trigger"] for c in cands)

    def test_prezzo_non_valido_ignorato(self):
        probs = po.true_probabilities(PINNACLE)
        rows = po.ev_gate(probs, {"1": 0.95, "X": None, "2": "nope"},
                          ev_min=EV_MIN)
        assert rows == []

    def test_soglia_di_default_e_quella_di_produzione(self):
        from value_filter import EV_MIN as PROD_EV_MIN
        probs = po.true_probabilities(PINNACLE)
        default_rows = po.ev_gate(probs, {"1": 1.90})
        explicit = po.ev_gate(probs, {"1": 1.90}, ev_min=PROD_EV_MIN)
        assert [r["trigger"] for r in default_rows] == \
            [r["trigger"] for r in explicit]
        assert po.DEFAULT_EV_MIN == PROD_EV_MIN


# ---------------------------------------------------------------------------
# 4. PERCORSO A COSTO ZERO (cache gia' scaricate)
# ---------------------------------------------------------------------------

def _write_cache(folder: Path, sport: str, payload) -> Path:
    path = folder / f"toa_{sport}.json"
    path.write_text(json.dumps({"ts": 1, "remaining": 400,
                                "remaining_ts": 2, "payload": payload}),
                    encoding="utf-8")
    return path


class TestScanCache:
    def test_copertura_sulle_cache_finte(self, tmp_path):
        _write_cache(tmp_path, "soccer_usa_mls", _payload())
        res = po.scan_cache(tmp_path)
        assert res["totals"] == {"leagues": 1, "matches": 4,
                                 "with_pinnacle": 1, "with_consensus": 1,
                                 "with_multi": 0, "candidates": 0,
                                 "price_errors": 0}
        lg = res["leagues"][0]
        assert lg["sport"] == "soccer_usa_mls"
        assert lg["with_pinnacle"] == 1 and lg["matches"] == 4
        assert lg["with_consensus"] == 1 and lg["with_multi"] == 0
        assert lg["avg_overround"] > 1.0

    def test_le_cache_dei_punteggi_non_sono_quote(self, tmp_path):
        _write_cache(tmp_path, "soccer_usa_mls", _payload())
        (tmp_path / "toa_scores_soccer_usa_mls.json").write_text(
            json.dumps({"payload": [{"home_team": "A", "away_team": "B"}]}),
            encoding="utf-8")
        res = po.scan_cache(tmp_path)
        assert [lg["sport"] for lg in res["leagues"]] == ["soccer_usa_mls"]

    def test_cache_illeggibile_o_vuota_non_solleva(self, tmp_path):
        (tmp_path / "toa_rotta.json").write_text("{non-json", encoding="utf-8")
        _write_cache(tmp_path, "soccer_vuota", [])
        res = po.scan_cache(tmp_path)
        assert res["totals"]["leagues"] == 0
        assert res["totals"]["with_pinnacle"] == 0

    def test_dichiarazione_del_gate(self, tmp_path):
        _write_cache(tmp_path, "soccer_usa_mls", _payload())
        gate = po.scan_cache(tmp_path, ev_min=0.03)["gate"]
        assert gate["ev_min"] == 0.03
        assert gate["sharp_book"] == "pinnacle"
        assert gate["devig_method"] == po.DEVIG_METHOD
        # Il consenso multi-oracolo e' dichiarato (fonti e metodo): un report
        # filtrato e uno no non devono essere indistinguibili.
        assert gate["consensus_books"] == list(po.CONSENSUS_BOOKS)
        assert gate["consensus_method"] == po.CONSENSUS_METHOD

    def test_senza_price_lookup_nessun_candidato(self, tmp_path):
        """In fase 1 SX non e' collegato: si misura la COPERTURA, non il P/L."""
        _write_cache(tmp_path, "soccer_usa_mls", _payload())
        res = po.scan_cache(tmp_path)
        assert res["candidates"] == []
        assert res["totals"]["with_pinnacle"] == 1   # la copertura c'e'

    def test_con_price_lookup_il_candidato_compare(self, tmp_path):
        _write_cache(tmp_path, "soccer_usa_mls", _payload())
        res = po.scan_cache(tmp_path, price_lookup=lambda m, e: (
            2.05 if e == "1" else None))
        assert res["totals"]["candidates"] == 1
        cand = res["candidates"][0]
        assert cand["esito"] == "1" and cand["trigger"] is True
        assert cand["event"] == "Atlanta United vs Toronto FC"
        assert cand["sport"] == "soccer_usa_mls"

    def test_price_lookup_basso_non_produce_candidati(self, tmp_path):
        _write_cache(tmp_path, "soccer_usa_mls", _payload())
        res = po.scan_cache(tmp_path, price_lookup=lambda m, e: 1.40)
        assert res["candidates"] == []

    def test_errore_del_lookup_e_contato_non_inghiottito(self, tmp_path):
        """Un book illeggibile NON deve leggersi come "zero value".

        E' la lezione del probe BTTS (25/09): `_discover_type` inghiottiva
        l'eccezione e "lettura rotta" diventava indistinguibile da "0 mercati".
        Qui l'errore si CONTA e si dichiara, e la scansione prosegue.
        """
        _write_cache(tmp_path, "soccer_usa_mls", _payload())
        res = po.scan_cache(tmp_path,
                            price_lookup=lambda m, e: (_ for _ in ()).throw(
                                RuntimeError("book giu'")))
        assert res["totals"]["price_errors"] == 1
        assert res["totals"]["with_pinnacle"] == 1   # la copertura resta
        assert res["candidates"] == []               # ma dichiarata incompleta
        assert po.scan_cache(tmp_path)["totals"]["price_errors"] == 0

    def test_max_leagues_limita_il_lavoro(self, tmp_path):
        for i in range(3):
            _write_cache(tmp_path, f"soccer_lega{i}", _payload())
        assert po.scan_cache(tmp_path, max_leagues=2)["totals"]["leagues"] == 2

    def test_cartella_inesistente_non_solleva(self, tmp_path):
        res = po.scan_cache(tmp_path / "non-esiste")
        assert res["totals"]["leagues"] == 0


class TestFetchSenzaChiave:
    def test_senza_chiave_fallisce_senza_toccare_la_rete(self, monkeypatch):
        monkeypatch.delenv("ODDS_API_KEY", raising=False)
        res = po.fetch_pinnacle_payload("soccer_usa_mls")
        assert res["payload"] == [] and res["error"] == "ODDS_API_KEY assente"
        assert res["status"] is None

    def test_crediti_bloccati_non_chiamano_l_api(self, monkeypatch):
        monkeypatch.setenv("ODDS_API_KEY", "fake-probe-key")
        import odds_api
        monkeypatch.setattr(odds_api, "credits_hard_stopped", lambda: True)
        res = po.fetch_pinnacle_payload("soccer_usa_mls")
        assert res["status"] is None and "soglia" in res["error"]


class TestCli:
    def test_from_cache_esce_zero_e_stampa_la_copertura(self, tmp_path,
                                                         monkeypatch, capsys):
        _write_cache(tmp_path, "soccer_usa_mls", _payload())
        monkeypatch.setattr(po, "DATA_DIR", str(tmp_path))
        assert po.main(["--from-cache"]) == 0
        out = capsys.readouterr().out
        assert "ORACOLO PINNACLE" in out and "soccer_usa_mls" in out
        assert "pinnacle=1" in out

    def test_from_cache_json_ha_i_totali(self, tmp_path, monkeypatch, capsys):
        _write_cache(tmp_path, "soccer_usa_mls", _payload())
        monkeypatch.setattr(po, "DATA_DIR", str(tmp_path))
        assert po.main(["--from-cache", "--json"]) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["totals"]["with_pinnacle"] == 1


# ---------------------------------------------------------------------------
# 5. TRIPWIRE: nessun Poisson, nessuna scrittura, nessun ordine, no rete
# ---------------------------------------------------------------------------

SOURCE = Path(po.__file__).read_text(encoding="utf-8")


class TestTripwire:
    def test_nessun_ricorso_al_motore_statistico(self):
        """Il pivot e' esplicito: la decisione NON passa dal modello."""
        for banned in ("poisson_engine", "expected_goals", "prob_1x2",
                       "prob_btts", "score_matrix", "ah_outcome_probs",
                       "ou_outcome_probs"):
            assert banned not in SOURCE, f"pinnacle_oracle usa {banned}"

    def test_nessuna_scrittura_sul_ledger(self):
        for banned in ("save_prediction", "save_bet", "save_market_quotes",
                       "save_analysis", "sqlite3", "INSERT INTO",
                       "UPDATE ", "DELETE FROM", "connect("):
            assert banned not in SOURCE, f"pinnacle_oracle scrive: {banned}"

    def test_nessun_ordine_e_nessun_executor(self):
        for banned in ("_live_fill", "place_order", "resolve_market_for",
                       "execution_engine", "auto_bet"):
            assert banned not in SOURCE, f"pinnacle_oracle ordina: {banned}"

    def test_nessuna_rete_all_import(self):
        """`requests` si importa DENTRO la funzione live: importare il modulo
        non deve poter toccare la rete (i test girano offline)."""
        top_level = [ln for ln in SOURCE.splitlines()
                     if re.match(r"^(import|from)\s+requests", ln)]
        assert top_level == [], f"import di rete a livello modulo: {top_level}"

    def test_importare_il_modulo_non_carica_la_produzione(self):
        import subprocess
        import sys
        code = ("import sys, pinnacle_oracle;"
                "print(sorted(m for m in ('poisson_engine','tracker','bot',"
                "'auto_bet','decision') if m in sys.modules))")
        out = subprocess.run([sys.executable, "-c", code], cwd=str(Path(po.__file__).parent),
                             capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == "[]", out.stdout


# ---------------------------------------------------------------------------
# 6. PROBE LIVE (opt-in, 1 credito): misura il costo reale della chiamata
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestProbeLive:
    def _skip_unless_enabled(self):
        if os.getenv("PINNACLE_PROBE") != "1":
            pytest.skip("probe live non abilitato (PINNACLE_PROBE=1)")
        if not (os.getenv("ODDS_API_KEY") or "").strip():
            pytest.skip("ODDS_API_KEY assente")

    def test_estrae_pinnacle_e_misura_il_costo_della_chiamata(self):
        self._skip_unless_enabled()
        sport = os.getenv("PINNACLE_SPORT", "soccer_usa_mls")
        res = po.fetch_pinnacle_payload(sport)
        if res["error"] and "soglia" in str(res["error"]):
            pytest.skip(f"crediti bloccati: {res['error']}")
        assert res["status"] == 200, f"{res['status']} {res['error']}"
        assert res["last_cost"] is not None, \
            "header x-requests-last assente: costo non misurabile"
        # il filtro bookmakers=pinnacle NON deve costare piu' di una chiamata
        assert 1 <= res["last_cost"] <= 2, res["last_cost"]
        hits = po.iter_pinnacle_markets(res["payload"])
        print(f"\nPROBE {sport}: eventi={len(res['payload'])} "
              f"con 1X2 Pinnacle={len(hits)} "
              f"crediti rimasti={res['remaining']} costo={res['last_cost']}")
        for match, quotes in hits[:3]:
            probs = po.true_probabilities(quotes)
            print(f"  {match['home_team']} vs {match['away_team']} "
                  f"quote={quotes} overround={probs['overround']:.4f} "
                  f"p1={probs['1']:.4f}")
        # se la lega ha partite, l'oracolo deve estrarle davvero
        if res["payload"]:
            assert hits, "payload non vuoto ma nessun 1X2 Pinnacle completo"


# ---------------------------------------------------------------------------
# FORMA DEL MERCATO a DUE ESITI (30/09/2026)
# ---------------------------------------------------------------------------
# Il gate dell'oracolo era cablato sui TRE esiti 1X2 (`OUTCOMES`): sui mercati
# testa-a-testa SENZA pareggio (tennis SX sportId 6 type 52, eSports) non
# poteva MAI produrre una probabilita' fair, quindi ogni candidato moriva con
# `no_oracle`. La forma ora la DICHIARA il chiamante (`outcomes=`), con i tre
# esiti 1X2 come default: il calcio deve restare identico bit per bit.

def _tennis_match(home, away, books, kickoff="2026-10-01T02:00:00Z"):
    return {"id": f"t-{home}", "sport_key": "tennis_atp_china_open",
            "commence_time": kickoff, "home_team": home, "away_team": away,
            "bookmakers": books}


def _write_cache(folder: Path, sport_key: str, payload, ts=None):
    """Scrive una cache `toa_<sport>.json` nella forma letta da `load_oracle`."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"toa_{sport_key}.json"
    path.write_text(json.dumps({"ts": time.time() if ts is None else ts,
                                "payload": payload, "remaining": 100}),
                    encoding="utf-8")
    return path


class TestFormaDueEsiti:
    TWO = ("1", "2")

    def _payload_2vie(self):
        return [_tennis_match("Felix Auger-Aliassime", "Karen Khachanov", [
            _book("pinnacle", "Pinnacle", "Felix Auger-Aliassime",
                  "Karen Khachanov", (1.66, None, 2.34)),
        ])]

    def test_calcio_invariato_col_default_a_tre_esiti(self):
        """La generalizzazione NON deve cambiare il percorso del calcio."""
        match = _payload()[0]
        quotes = po.pinnacle_quotes(_payload(), "Atlanta United", "Toronto FC")
        assert quotes == {"1": 1.75, "X": 3.60, "2": 4.50}
        probs = po.true_probabilities(quotes)          # default: 3 esiti
        assert set(k for k in probs if k != "overround") == {"1", "X", "2"}
        assert probs["1"] == pytest.approx(0.5483404337)
        assert quotes == po.h2h_odds_of(match["bookmakers"][0],
                                       "Atlanta United", "Toronto FC")

    def test_default_a_tre_esiti_rifiuta_un_mercato_a_due(self):
        payload = self._payload_2vie()
        assert po.pinnacle_quotes(payload, "Felix Auger-Aliassime",
                                  "Karen Khachanov") is None
        assert po.oracle_quotes(payload, "Felix Auger-Aliassime",
                                "Karen Khachanov") == {}

    def test_due_esiti_estrae_e_de_viga(self):
        payload = self._payload_2vie()
        quotes = po.oracle_quotes(payload, "Felix Auger-Aliassime",
                                  "Karen Khachanov", outcomes=self.TWO)
        assert list(quotes) == ["pinnacle"]
        assert quotes["pinnacle"] == {"1": 1.66, "2": 2.34}
        probs = po.true_probabilities(quotes["pinnacle"], min_outcomes=2)
        assert set(k for k in probs if k != "overround") == {"1", "2"}
        # il de-vig di un mercato a due vie somma 1 come un 1X2
        assert probs["1"] + probs["2"] == pytest.approx(1.0)
        # togliere il vig ABBASSA la probabilita' implicita...
        assert probs["1"] < 1.0 / 1.66
        # ...e il metodo "power" (default di progetto) alza il FAVORITO
        # rispetto al proporzionale: e' la correzione del favourite-longshot
        # bias, la stessa che il calcio usa da sempre.
        prop = po.true_probabilities(quotes["pinnacle"], min_outcomes=2,
                                     method="multiplicative")
        assert probs["1"] > prop["1"]

    def test_consenso_a_due_vie(self):
        payload = self._payload_2vie()
        by_book = po.oracle_quotes(payload, "Felix Auger-Aliassime",
                                   "Karen Khachanov", outcomes=self.TWO)
        cons = po.consensus_probabilities(by_book, outcomes=self.TWO)
        assert cons["fallback"] == "pinnacle_only"
        assert cons["n_sources"] == 1
        assert cons["1"] + cons["2"] == pytest.approx(1.0, abs=1e-5)
        # senza `outcomes` la stessa partita non produce oracolo (3 esiti attesi)
        assert po.consensus_probabilities(by_book) is None

    def test_fail_closed_su_mercato_a_due_incompleto(self):
        partial = [_tennis_match("A Player", "B Player", [
            _book("pinnacle", "Pinnacle", "A Player", "B Player",
                  (1.50, None, None)),          # un solo esito su due
        ])]
        assert po.oracle_quotes(partial, "A Player", "B Player",
                                outcomes=self.TWO) == {}

    def test_forma_a_un_esito_rifiutata(self):
        payload = self._payload_2vie()
        by_book = po.oracle_quotes(payload, "Felix Auger-Aliassime",
                                   "Karen Khachanov", outcomes=self.TWO)
        assert po.consensus_probabilities(by_book, outcomes=("1",)) is None

    def test_gate_ev_a_due_vie(self):
        probs = po.true_probabilities({"1": 1.66, "2": 2.34}, min_outcomes=2)
        rows = po.ev_gate(probs, {"1": 1.85, "2": 2.20})
        by_esito = {r["esito"]: r for r in rows}
        # fair 1 = 0.5888 -> quota equa 1.698; a 1.85 l'EV e' positivo
        assert by_esito["1"]["trigger"] is True
        assert by_esito["1"]["ev"] > 0
        assert by_esito["2"]["trigger"] is False
        assert set(by_esito) == {"1", "2"}       # nessun esito inventato

    def test_load_oracle_due_vie_dalla_cache(self, tmp_path):
        _write_cache(tmp_path, "tennis_atp_china_open", self._payload_2vie())
        probs = po.load_oracle("Felix Auger-Aliassime", "Karen Khachanov",
                               cache_dir=tmp_path, outcomes=self.TWO)
        assert probs is not None
        assert probs["1"] + probs["2"] == pytest.approx(1.0, abs=1e-5)
        # col default (3 esiti) la stessa cache NON produce oracolo
        assert po.load_oracle("Felix Auger-Aliassime", "Karen Khachanov",
                              cache_dir=tmp_path) is None

    def test_aggancio_per_nomi_senza_mappa_torneo(self, tmp_path):
        """Il ponte sono i NOMI: la chiave sport puo' chiamarsi come vuole."""
        _write_cache(tmp_path, "tennis_wta_china_open", self._payload_2vie())
        assert po.load_oracle("Felix Auger-Aliassime", "Karen Khachanov",
                              cache_dir=tmp_path, outcomes=self.TWO) is not None
        # nomi diversi: nessun aggancio (mai fuzzy sui partecipanti)
        assert po.load_oracle("Jannik Sinner", "Carlos Alcaraz",
                              cache_dir=tmp_path, outcomes=self.TWO) is None

    def test_cache_stantia_resta_fail_closed_anche_a_due_vie(self, tmp_path):
        _write_cache(tmp_path, "tennis_atp_china_open", self._payload_2vie(),
                     ts=time.time() - 48 * 3600)
        assert po.load_oracle("Felix Auger-Aliassime", "Karen Khachanov",
                              cache_dir=tmp_path, outcomes=self.TWO) is None
