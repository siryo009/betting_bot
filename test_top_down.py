"""Test della FASE 2 del pivot top-down (25/09/2026) — tutti OFFLINE.

Tre blocchi:
1. `pinnacle_oracle.load_oracle`: la p_true per UNA partita si legge dalle
   cache che la rotazione quote scarica gia' (zero crediti, zero rete);
2. `auto_bet._top_down_eval` + wiring in `run_today_bets`: l'EV del
   candidato si calcola contro l'oracolo, NON contro le probabilita' del
   modello (bypass del Poisson nella decisione);
3. DRY-RUN: i candidati che superano ogni gate vengono LOGGATI e
   INTERCETTATI prima della chiamata POST a SX Bet — nessun ordine,
   nessuna riga sul ledger.
"""
import json
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import auto_bet
import pinnacle_oracle as po
import tracker


# ---------------------------------------------------------------------------
# Fixture condivise
# ---------------------------------------------------------------------------

@pytest.fixture()
def temp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        monkeypatch.setattr(tracker, "DB_PATH", db_path)
        tracker.init_db()
        yield db_path


@pytest.fixture()
def cache_dir(tmp_path):
    return tmp_path / "data"


def _write_cache(cache_dir, sport, payload, *, age_h=0.5):
    """Cache quote nel formato di produzione ({ts, payload, remaining...})."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    f = cache_dir / f"toa_{sport}.json"
    f.write_text(json.dumps({
        "ts": time.time() - age_h * 3600.0,
        "remaining_ts": time.time() - age_h * 3600.0,
        "remaining": 400,
        "payload": payload,
    }), encoding="utf-8")
    return f


def _match(home="Arsenal", away="Everton", p1=1.85, px=3.60, p2=4.50):
    """Riga the-odds-api con UN bookmaker Pinnacle completo."""
    return {
        "id": "evt1", "sport_key": "soccer_epl", "sport_title": "EPL",
        "home_team": home, "away_team": away,
        "commence_time": (datetime.now(timezone.utc)
                          + timedelta(hours=5)).isoformat().replace("+00:00", "Z"),
        "bookmakers": [{
            "key": "pinnacle", "title": "Pinnacle",
            "markets": [{"key": "h2h", "outcomes": [
                {"name": home, "price": p1},
                {"name": "Draw", "price": px},
                {"name": away, "price": p2},
            ]}],
        }],
    }


@pytest.fixture(autouse=True)
def _oracle_env(monkeypatch, tmp_path):
    """Cache e flag nella tmp: nessun test tocca data/ di produzione."""
    monkeypatch.setenv("AUTO_BET_DRY_RUN", "0")
    monkeypatch.setenv("TOP_DOWN_EV", "1")
    yield


# ---------------------------------------------------------------------------
# 1. load_oracle: la p_true dalle cache (0 crediti)
# ---------------------------------------------------------------------------

class TestLoadOracle:
    def test_legge_la_p_true_dalla_cache(self, cache_dir):
        _write_cache(cache_dir, "soccer_epl", [_match()])
        probs = po.load_oracle("Arsenal", "Everton", cache_dir=cache_dir)
        assert probs is not None
        # Esiti + metadati del consenso (26/09): con la sola Pinnacle in cache
        # il consenso ripiega su quella fonte.
        assert {"1", "X", "2", "overround"} <= set(probs)
        assert probs["sources"] == ["pinnacle"]
        assert probs["fallback"] == "pinnacle_only"
        # Fair: somma 1
        assert abs(probs["1"] + probs["X"] + probs["2"] - 1.0) < 1e-9
        # Favorite-longshot: power alza il favorito sopra il proporzionale
        assert probs["1"] > (1 / 1.85) / (1 / 1.85 + 1 / 3.60 + 1 / 4.50)

    def test_match_per_sottostringa_su_varianti_di_nome(self, cache_dir):
        _write_cache(cache_dir, "soccer_epl",
                     [_match(home="Tottenham Hotspur", away="Everton FC")])
        probs = po.load_oracle("Tottenham", "Everton", cache_dir=cache_dir)
        assert probs is not None and "1" in probs

    def test_senza_partita_nessun_oracolo(self, cache_dir):
        _write_cache(cache_dir, "soccer_epl", [_match()])
        assert po.load_oracle("Milan", "Inter", cache_dir=cache_dir) is None

    def test_senza_pinnacle_fail_closed(self, cache_dir):
        m = _match()
        m["bookmakers"][0]["key"] = "bet365"          # solo soft
        _write_cache(cache_dir, "soccer_epl", [m])
        assert po.load_oracle("Arsenal", "Everton", cache_dir=cache_dir) is None

    def test_due_esiti_su_tre_fail_closed(self, cache_dir):
        m = _match()
        del m["bookmakers"][0]["markets"][0]["outcomes"][2]   # manca "2"
        _write_cache(cache_dir, "soccer_epl", [m])
        assert po.load_oracle("Arsenal", "Everton", cache_dir=cache_dir) is None

    def test_cache_stantia_oltre_il_tetto_fail_closed(self, cache_dir, monkeypatch):
        _write_cache(cache_dir, "soccer_epl", [_match()], age_h=30.0)
        monkeypatch.setattr(po, "CACHE_MAX_AGE_H", 24.0)
        assert po.load_oracle("Arsenal", "Everton", cache_dir=cache_dir) is None

    def test_cache_fresca_dentro_il_tetto(self, cache_dir, monkeypatch):
        _write_cache(cache_dir, "soccer_epl", [_match()], age_h=2.0)
        monkeypatch.setattr(po, "CACHE_MAX_AGE_H", 24.0)
        assert po.load_oracle("Arsenal", "Everton", cache_dir=cache_dir) is not None

    def test_sport_key_restringe_la_lettura(self, cache_dir):
        _write_cache(cache_dir, "soccer_epl", [_match()])
        assert po.load_oracle("Arsenal", "Everton", "soccer_epl",
                              cache_dir=cache_dir) is not None
        assert po.load_oracle("Arsenal", "Everton", "soccer_serbia_superliga",
                              cache_dir=cache_dir) is None

    def test_due_partite_stessi_token_non_si_confondono(self, cache_dir):
        # 'Arsenal' e' sottostringa di 'Arsenal Tula': home in COMUNE ma le
        # squadre AWAY diverse devono tenere le partite separate.
        _write_cache(cache_dir, "soccer_epl", [
            _match(home="Arsenal Tula", away="Rostov"),
        ])
        assert po.load_oracle("Arsenal", "Everton", cache_dir=cache_dir) is None

    def test_zero_chiamate_http(self, cache_dir, monkeypatch):
        """Il percorso oracolo NON tocca la rete: le cache sono gia' qui."""
        import requests
        _write_cache(cache_dir, "soccer_epl", [_match()])
        monkeypatch.setattr(requests, "get",
                            lambda *a, **k: pytest.fail("chiamata HTTP!"))
        assert po.load_oracle("Arsenal", "Everton", cache_dir=cache_dir)


# ---------------------------------------------------------------------------
# 2. _top_down_eval + wiring: l'EV arriva dall'oracolo, non dal modello
# ---------------------------------------------------------------------------

def _isolate_oracle(monkeypatch, cache_dir):
    """Punta load_oracle alla cache dei test."""
    monkeypatch.setattr(auto_bet, "_TOP_DOWN_CACHE_DIR", cache_dir)


def _patch_load(monkeypatch, probs):
    """Stub diretto del loader (per i test del wiring senza cache)."""
    monkeypatch.setattr(auto_bet, "_top_down_load", staticmethod(
        lambda home, away: probs))


ALLOWED_LEAGUE = "Premier League"


def _seed_pick(mid="m1", home="Osasuna", away="Getafe", esito="1",
               quota=1.65, status="value"):
    """Seeding del ledger come in test_auto_bet_live: candidato giocabile
    (lega ammessa, favorito netto, quota in fascia)."""
    start = (datetime.now(timezone.utc)
             + timedelta(hours=3)).isoformat().replace("+00:00", "Z")
    tracker.save_match(mid, ALLOWED_LEAGUE, home, away, start)
    best_esito = home if esito == "1" else (away if esito == "2" else "Draw")
    tracker.save_analysis(mid, 1.7, 1.1, 0.52, 0.27, 0.21, 0.58, 0.08,
                          best_esito, quota, "Pinnacle", status,
                          market_prob=0.60, market_edge=0.07)
    tracker.save_prediction(mid, "1X2", best_esito, quota, 0.52, 0.08,
                            market_prob=0.60, market_edge=0.07, status=status)


class TestTopDownEval:
    def test_ev_calcolato_sull_oracolo(self, monkeypatch):
        # p_true 0.60 (Pinnacle), quota 1.65: EV = 0.60*0.65 - 0.40 = -0.01
        _patch_load(monkeypatch, {"1": 0.60, "X": 0.25, "2": 0.15,
                                  "overround": 0.04})
        v = auto_bet._top_down_eval({"match_id": "m1", "home": "A",
                                     "away": "B", "esito_key": "1",
                                     "quota": 1.65})
        assert v["ok"] is True
        assert v["p_true"] == 0.60
        assert v["ev"] == pytest.approx(-0.01, abs=1e-6)
        assert v["trigger"] is False
        # true_odd = 1/0.6 = 1.6667 -> richiesto = 1.6667 * 1.02
        assert v["required_price"] == pytest.approx(1.6667 * 1.02, abs=1e-3)

    def test_trigger_sopra_la_soglia(self, monkeypatch):
        # p_true 0.55, quota 1.90 (test del solo valutatore, fuori fascia):
        # EV = 0.55x0.9 - 0.45 = +0.045. Prezzo di trigger: 1.8182 x 1.02 = 1.8545.
        _patch_load(monkeypatch, {"1": 0.55, "X": 0.27, "2": 0.18,
                                  "overround": 0.045})
        v = auto_bet._top_down_eval({"match_id": "m2", "home": "A",
                                     "away": "B", "esito_key": "1",
                                     "quota": 1.90})
        assert v["trigger"] is True
        assert v["ev"] == pytest.approx(0.045, abs=1e-6)

    def test_bypass_del_modello_l_ev_non_usa_le_prob_poisson(self, monkeypatch):
        """Le probabilita' del modello nel pick NON entrano nell'EV."""
        _patch_load(monkeypatch, {"1": 0.60, "X": 0.25, "2": 0.15,
                                  "overround": 0.04})
        pick = {"match_id": "m3", "home": "A", "away": "B", "esito_key": "1",
                "quota": 1.65, "market_prob": 0.10, "best_ev": 0.42,
                "market_edge": 0.09}
        v = auto_bet._top_down_eval(pick)
        # Se l'EV usasse il modello (prob 0.10 -> EV negativo, o best_ev 0.42
        # -> trigger): nessuno dei due deve comparire.
        assert v["ev"] == pytest.approx(-0.01, abs=1e-6)
        assert v["p_true"] == 0.60

    def test_no_oracle_fail_closed(self, monkeypatch):
        _patch_load(monkeypatch, None)
        v = auto_bet._top_down_eval({"match_id": "m4", "home": "A",
                                     "away": "B", "esito_key": "1",
                                     "quota": 1.65})
        assert v["ok"] is False and v["reason"] == "no_oracle"

    def test_esito_non_coperto_dall_oracolo(self, monkeypatch):
        _patch_load(monkeypatch, {"1": 0.60, "X": 0.25, "2": 0.15,
                                  "overround": 0.04})
        v = auto_bet._top_down_eval({"match_id": "m5", "home": "A",
                                     "away": "B", "esito_key": "AH",
                                     "quota": 1.65})
        assert v["ok"] is False and v["reason"] == "no_oracle"

    def test_quota_non_valida(self, monkeypatch):
        v = auto_bet._top_down_eval({"match_id": "m6", "home": "A",
                                     "away": "B", "esito_key": "1",
                                     "quota": 1.0})
        assert v["ok"] is False and v["reason"] == "quota non valida"

    def test_soglia_unica_con_value_filter(self):
        """Una sola definizione di EV_MIN: il modulo usa quella di value_filter
        (e l'oracolo la stessa costante importata)."""
        from value_filter import EV_MIN
        assert po.DEFAULT_EV_MIN == EV_MIN

    def test_dettatura_marginale_coincide_col_gate_ev(self, monkeypatch):
        """EV >= ev_min  <=>  quota >= true_odd x (1 + margine): le due
        letture della direttiva coincidono su una griglia di casi."""
        from value_filter import EV_MIN
        _patch_load(monkeypatch, {"1": 0.55, "X": 0.27, "2": 0.18,
                                  "overround": 0.045})
        p_true = 0.55
        # EV >= ev_min  <=>  p_true x quota >= 1 + ev_min  <=>  quota >= (1+ev_min)/p_true
        true_odd = 1.0 / p_true
        required = true_odd * (1.0 + auto_bet.TOP_DOWN_MARGIN)
        quota_ev = (1.0 + EV_MIN) / p_true                 # EV == ev_min
        assert quota_ev == pytest.approx(required, rel=1e-9)
        for quota in (required - 0.01, required, required + 0.01):
            q = round(quota, 4)     # lo stesso valore passato al valutatore
            v = auto_bet._top_down_eval({"match_id": "g", "home": "A",
                                         "away": "B", "esito_key": "1",
                                         "quota": q})
            assert v["trigger"] == (q >= required - 1e-9)


class TestWiringRunTodayBets:
    @pytest.fixture(autouse=True)
    def _live_lane(self, monkeypatch):
        """Questi test misurano il gate sulla CORSIA LIVE: la forzano.

        L'ambiente locale puo' avere AUTO_BET_MODE=live senza provider
        configurato -> mode=sim, dove il gate top-down non gira: senza il
        forcing i verdetti dipenderebbero dal .env della macchina.
        """
        monkeypatch.setattr(auto_bet, "_execution_mode",
                            lambda allow_sim=True: "live")

    def test_candidato_con_oracolo_arriva_al_dry_run(self, monkeypatch, temp_db):
        # quota 1.75 in fascia favoriti (<= 1.80); p_true 0.60 -> true odd
        # 1.6667, richiesto 1.70: EV = 0.60x0.75 - 0.40 = +5% -> trigger.
        _seed_pick(quota=1.75)
        _patch_load(monkeypatch, {"1": 0.60, "X": 0.25, "2": 0.15,
                                  "overround": 0.04})
        monkeypatch.setattr(auto_bet, "DRY_RUN", True)
        monkeypatch.setattr(auto_bet, "_live_fill",
                            lambda *a, **k: pytest.fail("POST a SX!"))
        placed = auto_bet.run_today_bets(stake_eur=5.0)
        assert placed == []          # dry-run: nessuna riga

    def test_candidato_con_ev_basso_non_arriva_all_esecuzione(self, monkeypatch,
                                                              temp_db):
        _seed_pick(quota=1.65)
        _patch_load(monkeypatch, {"1": 0.60, "X": 0.25, "2": 0.15,
                                  "overround": 0.04})
        monkeypatch.setattr(auto_bet, "DRY_RUN", False)
        monkeypatch.setattr(auto_bet, "_live_fill",
                            lambda *a, **k: pytest.fail("POST a SX!"))
        placed = auto_bet.run_today_bets(stake_eur=5.0)
        assert placed == []          # EV top-down -1%: no value, nessun ordine

    def test_senza_oracolo_nessun_ordine_fail_closed(self, monkeypatch, temp_db):
        _seed_pick()
        _patch_load(monkeypatch, None)
        monkeypatch.setattr(auto_bet, "_live_fill",
                            lambda *a, **k: pytest.fail("POST a SX!"))
        placed = auto_bet.run_today_bets(stake_eur=5.0)
        assert placed == []

    def test_top_down_disattivato_riprende_il_percorso_storico(self, monkeypatch,
                                                               temp_db):
        _seed_pick()
        monkeypatch.setenv("TOP_DOWN_EV", "0")
        monkeypatch.setattr(auto_bet, "TOP_DOWN_EV", False)
        monkeypatch.setattr(auto_bet, "_execution_mode", lambda a=True: "sim")
        called = []
        monkeypatch.setattr(tracker, "save_bet",
                            lambda **kw: called.append(kw) or None)
        placed = auto_bet.run_today_bets(stake_eur=5.0)
        assert len(placed) == 1 and called      # SIM storico, senza oracolo

    def test_log_dry_run_con_dettagli(self, monkeypatch, temp_db, caplog):
        _seed_pick(quota=1.75)
        _patch_load(monkeypatch, {"1": 0.60, "X": 0.25, "2": 0.15,
                                  "overround": 0.04})
        monkeypatch.setattr(auto_bet, "DRY_RUN", True)
        with caplog.at_level("WARNING", logger="auto_bet"):
            placed = auto_bet.run_today_bets(stake_eur=5.0)
        assert placed == []
        msgs = [r.getMessage() for r in caplog.records]
        assert any("DRY-RUN" in m and "INTERCETTATO" in m for m in msgs)
        # Il log porta il prezzo, lo stake e l'EV top-down: e' il report
        # richiesto dal proprietario (calcolare e loggare, non eseguire).
        detail = next(m for m in msgs if "INTERCETTATO" in m)
        assert "1.75" in detail and "5.00" in detail and "+5.00%" in detail

    def test_live_fill_bloccato_dal_flag_anche_chiamato_direttamente(self,
                                                                     monkeypatch,
                                                                     caplog):
        monkeypatch.setattr(auto_bet, "DRY_RUN", True)
        with caplog.at_level("WARNING", logger="auto_bet"):
            res = auto_bet._live_fill({"match_id": "x", "esito_key": "1",
                                       "home": "A", "away": "B"}, 1.0, 1.9)
        assert res is None
        assert any("DRY-RUN" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# 2-bis. CORSIA TOP-DOWN (25/09, direttiva "bypass del filtro quote"): la
# corsia LIVE non eredita la fascia favoriti ne' i tier: l'unico giudice del
# prezzo e' l'oracolo (EV >= EV_MIN, fail-closed senza oracolo). I gate NON
# di prezzo (lega, esito canonico, kickoff) restano.
# ---------------------------------------------------------------------------


def _seed_row(mid: str, home: str, away: str, esito: str, quota: float,
              ev: float = 0.10, prob: float = 0.55, status: str | None = None,
              lega: str = ALLOWED_LEAGUE, hours: float = 3.0) -> None:
    """Riga generica nel ledger: qualsivoglia status/quota (di default NESSUNO
    status: e' il caso delle righe `rejected` dalla fascia bottom-up)."""
    start = (datetime.now(timezone.utc)
             + timedelta(hours=hours)).isoformat().replace("+00:00", "Z")
    tracker.save_match(mid, lega, home, away, start)
    tracker.save_prediction(mid, "1X2", esito, quota, prob, ev,
                            market_prob=prob, market_edge=0.05,
                            status=status)


class TestCorsiaTopDown:
    def test_bypass_fascia_quote(self, monkeypatch, temp_db):
        """Quote FUORI fascia da AMBO I LATI (2.10 e 1.20) diventano candidati:
        il filtro di prezzo non e' piu' la fascia 1.30-1.80 ma l'oracolo."""
        # Il bypass NON e' piu' ON di default (26/09): la corsia si testa
        # accesa, esplicitamente.
        monkeypatch.setattr(auto_bet, "TOP_DOWN_BYPASS", True)
        _seed_row("b1", "Osasuna", "Getafe", "Osasuna", 2.10)
        _seed_row("b2", "Osasuna", "Getafe", "Draw", 4.50, ev=0.06)
        _seed_row("b3", "Osasuna", "Getafe", "Getafe", 1.20, ev=0.05)
        picks = auto_bet._top_down_picks()
        assert sorted(p["match_id"] for p in picks) == ["b1", "b2", "b3"]
        assert all(p["top_down_lane"] for p in picks)
        assert {p["esito_key"] for p in picks} == {"1", "X", "2"}

    def test_gate_lega_non_bypassato(self, monkeypatch, temp_db):
        """Il filtro di prezzo e' bypassato, il gate di lega NO: Serie A resta
        fuori (direttiva 12/09: leghe con ROI misurato negativo)."""
        monkeypatch.setattr(auto_bet, "TOP_DOWN_BYPASS", True)
        _seed_row("b1", "Osasuna", "Getafe", "Osasuna", 2.10)
        _seed_row("b2", "Osasuna", "Getafe", "Osasuna", 1.65, lega="Serie A")
        picks = auto_bet._top_down_picks()
        assert [p["match_id"] for p in picks] == ["b1"]

    def test_lea_ammessa_con_nome_grezzo_passa(self, monkeypatch, temp_db):
        """Lezione 24/09: una lega ammessa scritta come la scrive la fonte
        ("Major League Soccer") non deve essere rifiutata dal gate."""
        monkeypatch.setattr(auto_bet, "TOP_DOWN_BYPASS", True)
        _seed_row("b1", "Inter", "Siviglia", "Inter", 1.95,
                  lega="Major League Soccer")
        picks = auto_bet._top_down_picks()
        assert [p["match_id"] for p in picks] == ["b1"]

    def test_dedup_un_pick_per_evento(self, monkeypatch, temp_db):
        """Tre esiti dello stesso evento (stesso match_id): un solo pick
        (miglior EV, ordine deterministico della query). Un evento diverso
        resta."""
        monkeypatch.setattr(auto_bet, "TOP_DOWN_BYPASS", True)
        _seed_row("ev", "Osasuna", "Getafe", "Osasuna", 2.10, ev=0.10)
        _seed_row("ev", "Osasuna", "Getafe", "Draw", 4.50, ev=0.05)
        _seed_row("altro", "Lille", "Lione", "Draw", 3.40, ev=0.04)
        picks = auto_bet._top_down_picks()
        assert len(picks) == 2
        assert [p["match_id"] for p in picks].count("ev") == 1
        ev_pick = next(p for p in picks if p["match_id"] == "ev")
        assert ev_pick["esito_key"] == "1"      # EV 10% > 5%: vince il primo

    def test_esito_non_canonico_scartato(self, monkeypatch, temp_db):
        monkeypatch.setattr(auto_bet, "TOP_DOWN_BYPASS", True)
        _seed_row("b1", "Osasuna", "Getafe", "Marskipansa", 1.65)
        assert auto_bet._top_down_picks() == []

    def test_default_spento_senza_env(self):
        """Il bypass non e' MAI attivo per default: serve TOP_DOWN_BYPASS=1.

        Decisione del 26/09: con `AUTO_BET_DRY_RUN` a 0 (default) un bypass ON
        significherebbe ordini REALI che scavalcano il gate di prezzo
        1.30-1.80 senza una scelta esplicita — cioe' l'opposto del congelamento
        di strategia del 22/09.
        """
        import os
        import subprocess
        import sys
        from pathlib import Path
        env = {k: v for k, v in os.environ.items() if k != "TOP_DOWN_BYPASS"}
        out = subprocess.run(
            [sys.executable, "-c",
             "import auto_bet; print(auto_bet.TOP_DOWN_BYPASS)"],
            cwd=str(Path(auto_bet.__file__).parent), env=env,
            capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == "False", out.stdout

    def test_env_accende_il_bypass(self, monkeypatch, temp_db):
        """L'interruttore funziona: con TOP_DOWN_BYPASS acceso la corsia
        pesca anche le quote fuori fascia (e' il percorso voluto)."""
        monkeypatch.setattr(auto_bet, "TOP_DOWN_BYPASS", True)
        _seed_row("b1", "Osasuna", "Getafe", "Osasuna", 2.10)
        assert [p["match_id"] for p in auto_bet._top_down_picks()] == ["b1"]

    def test_fail_closed_su_kickoff_assente(self, monkeypatch, temp_db):
        _seed_row("b1", "Osasuna", "Getafe", "Osasuna", 1.65, hours=3)
        conn = tracker._get_conn()
        conn.execute("UPDATE matches SET commence_time = NULL WHERE id = 'b1'")
        conn.commit()
        conn.close()
        assert auto_bet._top_down_picks() == []

    def test_corsia_spenta_senza_top_down(self, monkeypatch, temp_db):
        _seed_row("b1", "Osasuna", "Getafe", "Osasuna", 2.10)
        monkeypatch.setattr(auto_bet, "TOP_DOWN_EV", False)
        assert auto_bet._top_down_picks() == []
        monkeypatch.setattr(auto_bet, "TOP_DOWN_EV", True)
        monkeypatch.setattr(auto_bet, "TOP_DOWN_BYPASS", False)
        assert auto_bet._top_down_picks() == []

    def test_fuori_dalla_finestra_24h_esclusa(self, monkeypatch, temp_db):
        _seed_row("b1", "Osasuna", "Getafe", "Osasuna", 2.10, hours=30.0)
        assert auto_bet._top_down_picks() == []


class TestWiringCorsiaLive:
    def test_solo_in_live_e_solo_con_gate_attivo(self, monkeypatch, temp_db):
        """In SIM (o con TOP_DOWN_BYPASS=0) il board resta quello storico."""
        _seed_row("b1", "Osasuna", "Getafe", "Osasuna", 2.10)
        monkeypatch.setattr(auto_bet, "TOP_DOWN_BYPASS", True)
        called = []
        monkeypatch.setattr(tracker, "save_bet",
                            lambda **kw: called.append(kw) or None)
        # SIM: la riga a 2.10 NON entra (corsia top-down spenta).
        monkeypatch.setattr(auto_bet, "_execution_mode", lambda a=True: "sim")
        assert auto_bet.run_today_bets(stake_eur=5.0) == []
        assert called == []

    def test_live_ordina_la_riga_rejected_fuori_fascia(self, monkeypatch,
                                                       temp_db):
        """End-to-end: riga `rejected` a 2.10 (legale ammessa) + oracolo con
        p_true 0.60 -> EV +5% -> ordine LIVE. La stessa config bottom-up NON
        l'avrebbe mai candidata (quota > ODDS_MAX)."""
        _seed_row("b1", "Osasuna", "Getafe", "Osasuna", 2.10, status="rejected")
        _patch_load(monkeypatch, {"1": 0.60, "X": 0.25, "2": 0.15,
                                  "overround": 0.04})
        monkeypatch.setattr(auto_bet, "TOP_DOWN_BYPASS", True)
        monkeypatch.setattr(auto_bet, "_execution_mode", lambda a=True: "live")
        # Wallet 150: il cap 1% (1.50 USDC) sta sopra il minimo ordine, cosi'
        # il test misura la CORSIA e non il cap severo (scenari suoi).
        monkeypatch.setattr(auto_bet, "_live_wallet_snapshot",
                            lambda: {"available": 150.0, "exposure": 0.0,
                                     "equity": 150.0})
        monkeypatch.setattr(auto_bet, "T60_EXECUTION_ONLY", False)
        sent = []
        monkeypatch.setattr(auto_bet, "_live_fill",
                            lambda pick, stake, floor: sent.append(
                                (pick["match_id"], pick["esito_key"], floor))
                            or {"ok": True, "market_id": "0xm",
                                "selection_id": 1, "bet_id": "0xb",
                                "status": "FULLY_FILLED", "price": floor,
                                "stake": stake})
        placed = auto_bet.run_today_bets(stake_eur=5.0)
        assert len(placed) == 1 and placed[0]["mode"] == "live"
        assert sent and sent[0][0] == "b1" and sent[0][1] == "1"
        assert tracker.get_bets()[0]["price"] == 2.10

    def test_no_oracle_blocca_anche_la_nuova_corsia(self, monkeypatch, temp_db):
        """Fail-closed: senza oracolo NESSUN ordine, nemmeno dal board bypass."""
        _seed_row("b1", "Osasuna", "Getafe", "Osasuna", 2.10)
        _patch_load(monkeypatch, None)
        monkeypatch.setattr(auto_bet, "TOP_DOWN_BYPASS", True)
        monkeypatch.setattr(auto_bet, "_execution_mode", lambda a=True: "live")
        monkeypatch.setattr(auto_bet, "_live_wallet_snapshot",
                            lambda: {"available": 40.0, "exposure": 0.0,
                                     "equity": 40.0})
        monkeypatch.setattr(auto_bet, "T60_EXECUTION_ONLY", False)
        monkeypatch.setattr(auto_bet, "_live_fill",
                            lambda *a, **k: pytest.fail("POST a SX!"))
        assert auto_bet.run_today_bets(stake_eur=5.0) == []


class TestProbationExtra:
    def test_probation_alza_la_soglia_ev(self, monkeypatch, temp_db):
        """Liga MX (probation): EV +3% con extra 2pp NON basta (servirebbe
        >= 4%); la stessa EV su una lega core passa."""
        pick = {"match_id": "p1", "home": "A", "away": "B",
                "esito_key": "1", "quota": 2.00}
        # p_true 0.515, quota 2.00 -> EV = 0.515 - 0.485 = +3% (strettamente
        # fra le due soglie: 2% core < 3% < 4% probation).
        _patch_load(monkeypatch, {"1": 0.515, "X": 0.26, "2": 0.225,
                                  "overround": 0.04})
        v = auto_bet._top_down_eval(pick, league="Liga MX")
        assert v["ok"] and v["trigger"] is False        # EV 3% < 2%+2%
        assert v["ev_min"] == pytest.approx(0.04)
        v_core = auto_bet._top_down_eval(pick, league="Premier League")
        assert v_core["ok"] and v_core["trigger"] is True
        assert v_core["ev_min"] == pytest.approx(0.02)

    def test_senza_leaga_nessun_extra(self, monkeypatch):
        pick = {"match_id": "p1", "home": "A", "away": "B",
                "esito_key": "1", "quota": 2.00}
        _patch_load(monkeypatch, {"1": 0.52, "X": 0.25, "2": 0.23,
                                  "overround": 0.04})
        v = auto_bet._top_down_eval(pick)
        assert v["ev_min"] == pytest.approx(0.02) and v["trigger"] is True

    def test_extra_configurabile(self, monkeypatch):
        pick = {"match_id": "p1", "home": "A", "away": "B",
                "esito_key": "1", "quota": 2.00}
        _patch_load(monkeypatch, {"1": 0.515, "X": 0.26, "2": 0.225,
                                  "overround": 0.04})
        monkeypatch.setattr(auto_bet, "TOP_DOWN_PROBATION_EXTRA", 0.10)
        v = auto_bet._top_down_eval(pick, league="Liga MX")
        assert v["ev_min"] == pytest.approx(0.12) and v["trigger"] is False


# ---------------------------------------------------------------------------
# 2b. Consenso multi-oracolo nel gate EV (26/09/2026)
# ---------------------------------------------------------------------------

class TestConsensoNelGateEv:
    """Il gate EV legge la p_true di CONSENSO, non il prezzo secco di una
    sola fonte. La struttura della cache e' quella di produzione."""

    def _match_multi(self, betfair=True):
        def bk(key, title, p):
            return {"key": key, "title": title,
                    "markets": [{"key": "h2h", "outcomes": [
                        {"name": "Arsenal", "price": p[0]},
                        {"name": "Draw", "price": p[1]},
                        {"name": "Everton", "price": p[2]}]}]}
        books = [bk("pinnacle", "Pinnacle", (1.85, 3.60, 4.50))]
        if betfair:
            books.append(bk("betfair_ex_eu", "Betfair Exchange",
                            (1.80, 3.70, 4.60)))
        return {"id": "evt1", "sport_key": "soccer_epl",
                "home_team": "Arsenal", "away_team": "Everton",
                "commence_time": (datetime.now(timezone.utc)
                                  + timedelta(hours=5))
                                 .isoformat().replace("+00:00", "Z"),
                "bookmakers": books}

    def test_ev_sul_consenso_non_sulla_sola_pinnacle(self, monkeypatch,
                                                     cache_dir):
        _isolate_oracle(monkeypatch, cache_dir)
        _write_cache(cache_dir, "soccer_epl", [self._match_multi()])
        pick = {"match_id": "m1", "home": "Arsenal", "away": "Everton",
                "esito_key": "1", "quota": 1.90}
        v = auto_bet._top_down_eval(pick)
        assert v["ok"] is True
        c = po.load_oracle("Arsenal", "Everton", cache_dir=cache_dir)
        assert v["p_true"] == pytest.approx(c["1"], abs=1e-6)
        assert v["oracle_sources"] == ["pinnacle", "betfair_ex_eu"]
        # e DIFFERISCE dalla Pinnacle secca (altrimenti non serve il consenso)
        pin = po.true_probabilities({"1": 1.85, "X": 3.60, "2": 4.50})
        assert v["p_true"] != pytest.approx(pin["1"], abs=1e-4)

    def test_fallback_pinnacle_only_se_senza_betfair(self, monkeypatch,
                                                     cache_dir):
        _isolate_oracle(monkeypatch, cache_dir)
        _write_cache(cache_dir, "soccer_epl",
                     [self._match_multi(betfair=False)])
        v = auto_bet._top_down_eval({"match_id": "m1", "home": "Arsenal",
                                     "away": "Everton", "esito_key": "1",
                                     "quota": 1.90})
        assert v["ok"] is True
        assert v["oracle_fallback"] == "pinnacle_only"
        pin = po.true_probabilities({"1": 1.85, "X": 3.60, "2": 4.50})
        assert v["p_true"] == pytest.approx(pin["1"], abs=1e-6)


# ---------------------------------------------------------------------------
# 3. Tripwire: la pipeline resta ecolgicamente coerente
# ---------------------------------------------------------------------------

class TestTripwire:
    def test_oracolo_non_importa_la_produzione(self):
        import subprocess
        code = ("import sys, pinnacle_oracle;"
                "print(sorted(m for m in ('poisson_engine','tracker','bot',"
                "'auto_bet','decision') if m in sys.modules))")
        out = subprocess.run([sys.executable, "-c", code],
                             cwd=str(Path(po.__file__).parent),
                             capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == "[]", out.stdout

    def test_flag_documentati_nella_iac(self):
        src = Path(".railway/railway.ts").read_text(encoding="utf-8")
        for env in ("TOP_DOWN_EV", "TOP_DOWN_MARGIN", "AUTO_BET_DRY_RUN",
                    "PINNACLE_CONSENSUS", "PINNACLE_CONSENSUS_METHOD",
                    "PINNACLE_VALIDATOR_TOLERANCE"):
            assert env in src, f"{env} non dichiarata in .railway/railway.ts"

    def test_pinnacle_oracle_senza_riferimenti_al_percorso_ordini(self):
        """La direzione dell'integrazione e' auto_bet -> oracolo: il modulo
        dell'oracolo NON puo' conoscere il percorso ordini (tripwire Fase 1)."""
        src = Path(po.__file__).read_text(encoding="utf-8")
        for banned in ("auto_bet", "execution_engine", "_live_fill"):
            assert banned not in src
