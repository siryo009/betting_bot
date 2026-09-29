"""test_esports_oracle.py — Oracolo eSports (OddsPapi): contratto e tripwire.

Tutti i test girano **OFFLINE**: il getter HTTP e' iniettato, quindi nessuna
rete, nessuna chiave, nessun credito e nessun ordine. La chiave reale serve
solo alla prova dal vivo (`--fixtures`/`--odds` a mano), non qui.

Le due cose che questi test difendono:
1. **Comportamento** — estrazione del Match Winner (2 vie) dal payload annidato
   di OddsPapi, de-vig a due esiti, orientamento dei nomi squadra iOS, gate EV
   DELEGATO (stessa definizione del percorso calcio).
2. **Struttura** — il modulo non contiene Poisson, non scrive sul ledger, non
   piazza ordini e non fa rete all'import: senza questi vincoli un "oracolo" e'
   un secondo motore di decisione travestito da lettura.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

import pytest

import esports_oracle as eo

ROOT = Path(__file__).resolve().parent
SOURCE = (ROOT / "esports_oracle.py").read_text(encoding="utf-8")


def _code_only(source: str) -> str:
    """Sorgente SENZA docstring: il tripwire non deve colpire la PROSA.

    La docstring del modulo NOMINA di proposito le cose che il modulo rifiuta
    di fare (\"non consulta Poisson\", \"non scrive sul ledger\"): scandire il
    testo intero renderebbe il tripwire rumoroso, e un tripwire rumoroso viene
    disattivato. Qui resta solo il codice eseguibile (`ast`).
    """
    doc_lines: set[int] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", [])
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            doc_lines.update(range(body[0].lineno,
                                  (body[0].end_lineno or body[0].lineno) + 1))
    return "\n".join(line for index, line in enumerate(source.splitlines(), start=1)
                     if index not in doc_lines)


CODE = _code_only(SOURCE)


# ---------------------------------------------------------------------------
# Helper: payload e trasporto finti
# ---------------------------------------------------------------------------

def _node(price, *, flat=False, active=True):
    if flat:
        return {"active": active, "price": price}
    return {"active": active, "players": {"0": {"active": active, "price": price}}}


def odds_payload(team1="Estral E-Sports", team2="9Z Globant", p1=1.19, p2=4.89,
                 *, flat=False, active=True, market_id=eo.WINNER_MARKET_ID,
                 book="pinnacle"):
    """Risposta di `/v4/odds` nella forma osservata sui payload REALI (29/09):
    il market id coincide col primo outcome id (185->185/186, 171->171/172).
    `legacy=True` costruisce la forma DOCUMENTATA (171/172) per il fallback.
    """
    second = "172" if market_id == "171" else "186"
    return {
        "fixtureId": "id1704591169167084",
        "participant1Name": team1,
        "participant2Name": team2,
        "sportId": 18,
        "bookmakerOdds": {
            book: {"markets": {market_id: {"outcomes": {
                market_id: _node(p1, flat=flat, active=active),
                second: _node(p2, flat=flat, active=active),
            }}}},
        },
    }


class FakeHttp:
    """Getter HTTP finto: risposta fissa, chiamate registrate."""

    def __init__(self, status=200, payload=None, error=None):
        self.status = status
        self.payload = payload
        self.error = error
        self.calls: list[tuple] = []

    def __call__(self, url, params, timeout):
        self.calls.append((url, dict(params), timeout))
        if self.error is not None:
            raise self.error
        return self.status, self.payload

    @property
    def query(self):
        return self.calls[-1][1] if self.calls else {}


def _fail_if_called(url, params, timeout):        # pragma: no cover - deve esplodere
    raise AssertionError("il getter NON deve essere chiamato (fail-closed)")


# ---------------------------------------------------------------------------
# 1. Titoli e identificatori
# ---------------------------------------------------------------------------

class TestTitoli:
    @pytest.mark.parametrize("label,expected", [
        ("LOL - CBLOL", "lol"),
        ("LOL EMEA Masters Summer", "lol"),
        ("League of Legends - LEC", "lol"),
        ("Dota 2 - Blast Slam", "dota2"),
        ("Valorant - VCT", "valorant"),
        ("CS2 - BLAST Premier", "cs2"),
        ("Counter-Strike - IEM", "cs2"),
        ("Rocket League - RLCS", "rocket_league"),
    ])
    def test_etichette_reali_di_sx(self, label, expected):
        assert eo.title_of_league(label) == expected

    @pytest.mark.parametrize("label", ["", None, "   ", "Serie A", "NBA",
                                       "Some Unknown League"])
    def test_etichetta_ignota_non_indovina(self, label):
        """Una lega non riconosciuta NON diventa 'quella piu' simile'."""
        assert eo.title_of_league(label) is None

    def test_sport_id_per_nome_e_numero(self):
        assert eo.sport_id_of("lol") == 18
        assert eo.sport_id_of("18") == 18
        assert eo.sport_id_of("17") == 17
        assert eo.sport_id_of("lol - cblol") == 18      # via etichetta

    def test_sport_id_rifiuta_cio_che_non_e_un_titolo_coperto(self):
        assert eo.sport_id_of("overwatch") is None      # OddsPapi ce l'ha, SX no
        assert eo.sport_id_of("10") is None             # 10 = calcio
        assert eo.sport_id_of("") is None

    def test_titles_of_dedup_e_ordine(self):
        assert eo.titles_of(["LOL - CBLOL", "LOL EMEA", "Valorant - VCT",
                             "Serie A"]) == ["lol", "valorant"]


# ---------------------------------------------------------------------------
# 2. Trasporto: chiave solo da env/iniettata, mai un'eccezione
# ---------------------------------------------------------------------------

class TestTrasporto:
    def test_senza_chiave_non_si_chiama_nulla(self, monkeypatch):
        """Fail-closed: senza chiave il getter non viene nemmeno sfiorato."""
        monkeypatch.delenv(eo.KEY_ENV, raising=False)
        assert eo.configured() is False
        res = eo._call("fixtures", {}, http_get=_fail_if_called)
        assert res["ok"] is False
        assert eo.KEY_ENV in res["error"]

    def test_configured_legge_la_chiave(self, monkeypatch):
        monkeypatch.setenv(eo.KEY_ENV, "fake-key-for-tests")
        assert eo.configured() is True
        assert eo.configured("altra") is True
        assert eo.configured("") is False

    def test_la_chiave_finisce_nei_parametri_non_nell_url(self, monkeypatch):
        monkeypatch.setenv(eo.KEY_ENV, "fake-key-for-tests")
        http = FakeHttp(payload=[{"fixtureId": "x"}])
        res = eo.fixtures("lol", http_get=http)
        assert res["ok"] is True
        url, params, _ = http.calls[-1]
        assert "fake-key" not in url
        assert params["apiKey"] == "fake-key-for-tests"

    def test_parametri_della_discovery(self, monkeypatch):
        monkeypatch.setenv(eo.KEY_ENV, "k" * 12)
        http = FakeHttp(payload=[])
        eo.fixtures("cs2", days_ahead=3, http_get=http,
                    now=__import__("datetime").datetime(
                        2026, 9, 29, 12, tzinfo=__import__("datetime").timezone.utc))
        params = http.query
        assert params["sportId"] == 17
        assert params["hasOdds"] == "true"
        assert params["from"] == "2026-09-29" and params["to"] == "2026-10-02"

    def test_la_finestra_non_supera_il_massimo(self, monkeypatch):
        monkeypatch.setenv(eo.KEY_ENV, "k" * 12)
        http = FakeHttp(payload=[])
        res = eo.fixtures("lol", days_ahead=99, http_get=http)
        assert res["ok"] is True
        delta = (__import__("datetime").datetime.strptime(http.query["to"], "%Y-%m-%d")
                 - __import__("datetime").datetime.strptime(http.query["from"], "%Y-%m-%d")).days
        assert delta == eo.MAX_DAYS_AHEAD

    def test_titolo_sconosciuto_non_chiama(self, monkeypatch):
        monkeypatch.setenv(eo.KEY_ENV, "k" * 12)
        res = eo.fixtures("serie a", http_get=_fail_if_called)
        assert res["ok"] is False and res["requests"] == 0
        assert "non riconosciuto" in res["error"]

    def test_errore_http_dichiarato_col_messaggio_del_provider(self, monkeypatch):
        monkeypatch.setenv(eo.KEY_ENV, "k" * 12)
        http = FakeHttp(status=429, payload={"message": "Request limit exceeded",
                                            "code": "REQUEST_LIMIT_EXCEEDED"})
        res = eo._call("fixtures", {}, http_get=http)
        assert res["ok"] is False
        assert res["status"] == 429
        assert res["code"] == "REQUEST_LIMIT_EXCEEDED"
        assert "limit" in res["error"]

    def test_trasporto_rotto_non_solleva(self, monkeypatch):
        monkeypatch.setenv(eo.KEY_ENV, "k" * 12)
        http = FakeHttp(error=TimeoutError("boom"))
        res = eo._call("odds", {}, http_get=http)
        assert res["ok"] is False and "TimeoutError" in res["error"]

    def test_account_non_consuma_quota_nel_contratto(self, monkeypatch):
        """`/account` e' sempre accessibile: il contratto lo dichiara."""
        monkeypatch.setenv(eo.KEY_ENV, "k" * 12)
        http = FakeHttp(payload={"request_limit": 1000, "request_count": 12})
        res = eo.account(http_get=http)
        assert res["ok"] is True and res["payload"]["request_count"] == 12

    def test_la_chiave_non_compare_mai_nel_sorgente(self):
        assert "apiKey" in SOURCE                      # il nome del parametro
        assert not any(tok in SOURCE for tok in ("sk_live", "AIza", "ghp_"))


# ---------------------------------------------------------------------------
# 3. Estrazione del Match Winner
# ---------------------------------------------------------------------------

class TestWinnerMarket:
    def test_estrae_la_forma_annidata_con_players(self):
        market = eo.winner_market(odds_payload())
        assert market is not None
        assert market["team1"] == "Estral E-Sports"
        assert market["odds"] == {"1": 1.19, "2": 4.89}
        assert market["bookmaker"] == "pinnacle"

    def test_estrae_anche_la_forma_piatta(self):
        """Se il provider appiattisce lo schema il parser non deve morire."""
        market = eo.winner_market(odds_payload(flat=True))
        assert market is not None and market["odds"]["2"] == 4.89

    def test_manca_un_lato_nessun_oracolo(self):
        """FAIL-CLOSED: con 1 esito il margine mancante verrebbe attribuito all'altro."""
        payload = odds_payload()
        del payload["bookmakerOdds"]["pinnacle"]["markets"][eo.WINNER_MARKET_ID][
            "outcomes"][eo.WINNER_OUTCOMES[1]]
        assert eo.winner_market(payload) is None

    def test_prezzo_sospeso_scartato(self):
        payload = odds_payload()
        payload["bookmakerOdds"]["pinnacle"]["markets"][eo.WINNER_MARKET_ID][
            "outcomes"][eo.WINNER_OUTCOMES[0]]["active"] = False
        assert eo.winner_market(payload) is None

    def test_prezzo_sotto_uno_scartato(self):
        assert eo.winner_market(odds_payload(p1=0.95)) is None

    def test_forma_documentata_171_trovata_fallback(self):
        """La forma DOCUMENTATA (171/172) non e' mai apparsa nei payload reali
        (misura 29/09: il campo usa 185/186) ma resta come fallback: se il
        provider cambia schema, l'estrazione non muore."""
        market = eo.winner_market(odds_payload(market_id=eo.WINNER_MARKET_ID_LEGACY))
        assert market is not None
        assert market["market_id"] == eo.WINNER_MARKET_ID_LEGACY
        assert market["odds"] == {"1": 1.19, "2": 4.89}

    def test_odds_non_invia_il_parametro_bookmakers(self, monkeypatch):
        """MISURA 29/09 (container): il parametro documentato
        `bookmakers=<slug>` FA SVUOTARE `bookmakerOdds` (payload ridotto ai
        soli metadati); senza il parametro il payload porta tutti i book.
        Il filtro avviene lato client in `winner_market`: la query NON deve
        piu' contenere `bookmakers` (tripwire sulla regressione del payload
        vuoto)."""
        monkeypatch.setenv(eo.KEY_ENV, "k" * 12)
        class Http:
            def __call__(self, url, params, timeout):
                self.query = dict(params)
                return 200, {"fixtureId": "x"}
        http = Http()
        res = eo.odds("id123", bookmaker="pinnacle", http_get=http)
        assert res["ok"] is True
        assert "bookmakers" not in http.query, http.query
        assert http.query.get("fixtureId") == "id123"

    def test_bookmaker_diverso_non_aggancia(self):
        assert eo.winner_market(odds_payload(book="bet365")) is None

    def test_payload_ostile_non_solleva(self):
        for bad in (None, [], "x", {"bookmakerOdds": "no"},
                    {"bookmakerOdds": {"pinnacle": {"markets": None}}}):
            assert eo.winner_market(bad) is None

    def test_market_id_diverso_non_aggancia(self):
        """Gli id dei mercati a linea cambiano: qui si legge SOLO il Match Winner."""
        assert eo.winner_market(odds_payload(market_id="999")) is None


# ---------------------------------------------------------------------------
# 4. De-vig a due esiti (delega a market_calib)
# ---------------------------------------------------------------------------

class TestTrueProbabilities:
    def test_somma_a_uno_e_porta_l_overround(self):
        probs = eo.true_probabilities({"1": 1.19, "2": 4.89})
        assert probs is not None
        assert probs["1"] + probs["2"] == pytest.approx(1.0, abs=1e-9)
        assert probs["overround"] > 1.0
        assert probs["1"] > probs["2"]               # il favorito resta favorito

    def test_un_solo_esito_non_e_un_mercato(self):
        assert eo.true_probabilities({"1": 1.19}) is None
        assert eo.true_probabilities({}) is None

    def test_usa_la_stessa_formula_del_calcio(self):
        """Parita' col percorso calcio: stesso de-vig, nessun secondo standard."""
        from market_calib import market_implied

        assert eo.true_probabilities({"1": 1.19, "2": 4.89}) == \
            market_implied({"1": 1.19, "2": 4.89}, method=eo.DEVIG_METHOD)


# ---------------------------------------------------------------------------
# 5. Oracolo orientato sui nomi
# ---------------------------------------------------------------------------

class TestOracle:
    def test_orientamento_coerente(self):
        probs = eo.oracle(odds_payload(), home="Estral E-Sports",
                          away="9Z Globant")
        assert probs is not None
        assert probs["1"] > probs["2"]               # "1" = squadra di casa

    def test_orientamento_invertito_scambia_le_probabilita(self):
        """Un incrocio non rilevato comprerebbe l'esito sbagliato 'con valore'."""
        direct = eo.oracle(odds_payload(), home="9Z Globant",
                           away="Estral E-Sports")
        assert direct is not None
        assert direct["1"] < direct["2"]             # ora "1" e' l'underdog

    def test_nomi_non_agganciabili_nessun_oracolo(self):
        assert eo.oracle(odds_payload(), home="Team Inventato",
                         away="Altro Team") is None

    def test_nomi_assenti_significa_nessun_controllo_di_orientamento(self):
        probs = eo.oracle(odds_payload())
        assert probs is not None and probs["1"] > probs["2"]

    def test_metadati_dichiarati_e_inerti(self):
        """I metadati non devono MAI entrare in un calcolo di EV o true-odd."""
        from pinnacle_oracle import _META_KEYS, fair_odds

        probs = eo.oracle(odds_payload())
        assert {"overround", "sources", "n_sources", "consensus_method",
                "validated", "fallback", "agreement_pp"} <= set(probs.keys())
        assert "_teams" not in {k for k in probs if k in _META_KEYS}
        fair = fair_odds(probs)
        assert set(fair) == {"1", "2"}               # solo gli esiti

    def test_le_squadre_sono_disponibili_ma_fuori_dai_calcoli(self):
        probs = eo.oracle(odds_payload())
        assert probs["_teams"] == {"1": "Estral E-Sports", "2": "9Z Globant"}
        from pinnacle_oracle import fair_odds
        assert "_teams" not in fair_odds(probs)


# ---------------------------------------------------------------------------
# 6. Gate EV: delegato, una sola definizione nel progetto
# ---------------------------------------------------------------------------

class TestGateEv:
    def test_parita_col_percorso_calcio(self):
        from pinnacle_oracle import ev_gate as calcio_gate

        probs = {"1": 0.84, "2": 0.16}
        prices = {"1": 1.25, "2": 5.5}
        assert eo.ev_gate(probs, prices) == calcio_gate(probs, prices)

    def test_le_due_letture_della_direttiva_coincidono(self):
        """EV >= ev_min  <=>  quota >= true_odd x (1 + ev_min)."""
        probs = {"1": 0.84, "2": 0.16}
        ev_min = 0.02
        for price in (1.15, 1.19, 1.21, 1.30, 7.0):
            row = eo.candidate_for(probs, price, "1", ev_min=ev_min)
            assert row is not None
            assert row["trigger"] == (row["ev"] >= ev_min)
            assert row["trigger"] == (price >= row["required_price"] - 1e-9)

    def test_solo_gli_esiti_con_valore(self):
        probs = {"1": 0.84, "2": 0.16}
        # Solo l'underdog e' pagato sopra la sua true odd + margine (6.375).
        rows = eo.value_candidates(probs, {"1": 1.10, "2": 7.0})
        assert [r["esito"] for r in rows] == ["2"]

    def test_un_esito_non_valutabile_non_produce_righe(self):
        assert eo.candidate_for({"1": 0.84, "2": 0.16}, 1.20, "") is None

    def test_soglia_di_produzione_non_copiata(self):
        import value_filter

        assert eo.min_ev() == float(value_filter.EV_MIN)

    def test_la_formula_dell_ev_non_e_riscritta_qui(self):
        """Una seconda formula sarebbe un secondo standard di valore."""
        assert "p * (price - 1.0)" not in CODE
        assert "def required_price" not in CODE
        assert "from pinnacle_oracle import ev_gate" in CODE


# ---------------------------------------------------------------------------
# 7. Percorso end-to-end (fixture -> odds -> oracolo)
# ---------------------------------------------------------------------------

class TestOracleForFixture:
    def test_end_to_end_con_trasporto_finto(self, monkeypatch):
        monkeypatch.setenv(eo.KEY_ENV, "k" * 12)
        http = FakeHttp(payload=odds_payload())
        res = eo.oracle_for_fixture(
            {"fixtureId": "id1", "participant1Name": "Estral E-Sports",
             "participant2Name": "9Z Globant"}, http_get=http)
        assert res["ok"] is True and res["requests"] == 1
        assert res["oracle"]["1"] > res["oracle"]["2"]

    def test_senza_quote_dichiara_il_motivo(self, monkeypatch):
        monkeypatch.setenv(eo.KEY_ENV, "k" * 12)
        http = FakeHttp(payload={})
        res = eo.oracle_for_fixture({"fixtureId": "id1"}, http_get=http)
        assert res["ok"] is False and res["oracle"] is None
        assert "Match Winner" in res["error"]

    def test_fixture_senza_id_non_chiama(self, monkeypatch):
        monkeypatch.setenv(eo.KEY_ENV, "k" * 12)
        res = eo.oracle_for_fixture({}, http_get=_fail_if_called)
        assert res["ok"] is False and res["requests"] == 0


# ---------------------------------------------------------------------------
# 8. Tripwire di struttura
# ---------------------------------------------------------------------------

class TestTripwireStruttura:
    def test_nessun_motore_statistico(self):
        """Un oracolo che calcola da se' non e' un oracolo."""
        lowered = CODE.lower()
        for token in ("poisson", "expected_goals", "prob_1x2", "prob_btts",
                      "ah_outcome_probs", "ou_outcome_probs", "numpy"):
            assert token not in lowered, f"riferimento vietato: {token}"

    def test_nessuna_scrittura_ne_ordine(self):
        for token in ("save_prediction", "save_bet", "save_market_quotes",
                      "sqlite3", "INSERT", "UPDATE", "place_limit_order",
                      "_live_fill", "resolve_market_for", "execution_engine",
                      "auto_bet", "tracker"):
            assert token not in CODE, f"riferimento vietato: {token}"

    def test_nessuna_rete_all_import(self):
        """`requests` importato pigro: importare il modulo non apre una socket."""
        out = subprocess.run(
            [sys.executable, "-c",
             "import sys, esports_oracle; "
             "print('requests' in sys.modules, "
             "'tracker' in sys.modules, 'decision' in sys.modules)"],
            capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == "False False False", out.stdout

    def test_cli_parsing_senza_chiave_e_fail_closed(self, monkeypatch, capsys):
        monkeypatch.delenv(eo.KEY_ENV, raising=False)
        assert eo.main(["--fixtures", "lol"]) == 1
        assert eo.KEY_ENV in capsys.readouterr().out

    def test_cli_titles_non_tocca_la_rete(self, monkeypatch, capsys):
        monkeypatch.delenv(eo.KEY_ENV, raising=False)
        assert eo.main(["--titles"]) == 0
        assert "lol" in capsys.readouterr().out

    def test_export_pubblico(self):
        for name in ("oracle", "oracle_for_fixture", "ev_gate",
                     "value_candidates", "title_of_league", "sport_id_of"):
            assert name in eo.__all__

    def test_account_e_dichiarato_non_metered(self):
        doc = (eo.account.__doc__ or "").lower()
        assert "non consuma" in doc


class TestIaC:
    """Le variabili d'ambiente lette dal modulo devono essere dichiarate in
    `.railway/railway.ts` con `preserve()`.

    ⚠️ `railway config apply` DISTRUGGE cio' che l'operatore ha impostato ma
    non trova nel file — stessa regola e stesso tripwire degli altri moduli
    (adaptive weighting, smart hedging, recinto di capitale, live intel).
    """

    ENV = ("ODDSPAPI_KEY", "ODDSPAPI_BASE", "ODDSPAPI_BOOK",
           "ESPORTS_DEVIG_METHOD")

    def test_env_dichiarate_nella_iac(self):
        src = Path(".railway/railway.ts").read_text(encoding="utf-8")
        for env in self.ENV:
            assert f"{env}: preserve()" in src, env

    def test_ogni_env_letta_e_dichiarata(self):
        """Nessuna variabile letta dal modulo puo' restare fuori dalla IaC."""
        src = (ROOT / "esports_oracle.py").read_text(encoding="utf-8")
        letti = set(re.findall(r'getenv\(\s*"(ODDSPAPI[A-Z0-9_]*|'
                               r'ESPORTS_[A-Z0-9_]*)"', src))
        assert letti, "nessuna env letta: il test non sta misurando nulla"
        dichiarati = set(self.ENV)
        assert letti <= dichiarati, f"env non dichiarate: {sorted(letti - dichiarati)}"
