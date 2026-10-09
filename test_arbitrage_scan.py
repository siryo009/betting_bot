"""Test di arbitrage_scan.py — TUTTI OFFLINE.

Nessuna rete, nessuna credenziale, nessun ordine: i due provider sono finti e
iniettati. Il modulo e' un MISURATORE (come `line_intersection.py`): i tripwire
verificano che non contenga percorsi d'ordine e che l'import resti leggero.
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

import arbitrage_scan as a

SRC = Path(a.__file__).read_text(encoding="utf-8")


def _code_only(src: str) -> str:
    """Sorgente senza docstring: il tripwire giudica il CODICE, non la prosa.

    Le docstring di questo progetto NOMINANO di proposito i moduli vietati
    (per spiegare che non vengono usati): un controllo sul testo grezzo
    boccia la documentazione invece del comportamento.
    """
    tree = ast.parse(src)
    holders = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    for node in ast.walk(tree):
        if isinstance(node, holders):
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                body.pop(0)
    return ast.unparse(tree)

# SX: prob * 1e20; size in unita' base (6 decimali, 1 USDC = 1e6).
SCALE = 10 ** 20


def _sx_book(odds: float, size: float = 100.0) -> dict:
    """Book SX nella forma dei runner di `get_market_book`."""
    prob = 1.0 / odds
    return {"runners": [{
        "selectionId": 1,
        "availableToBack": [{"percentageOdds": int(prob * SCALE),
                             "size": int(size * 10 ** 6)}],
    }]}


def _sx_row(mid: str, t1: str, t2: str, o1: str, ts: str = "2026-10-10T18:00:00Z"):
    return {"market_id": mid, "team_one_name": t1, "team_two_name": t2,
            "outcome_one_name": o1, "open_date": ts, "event_id": "ev-" + mid,
            "league_label": "Test League"}


class FakeSx:
    """Provider SX finto: catalogue + book, nessuna rete."""

    def __init__(self, rows, books):
        self.rows = rows
        self.books = books
        self.calls = []

    def list_market_catalogue(self, event_type_ids=("5",), market_type="1X2",
                              max_results=400, market_type_ids=None):
        self.calls.append({"sports": tuple(event_type_ids),
                           "types": tuple(market_type_ids or ())})
        return list(self.rows)

    def get_market_book(self, market_id):
        if market_id not in self.books:
            raise RuntimeError(f"book {market_id} assente")
        return self.books[market_id]


def _sm_row(mid: str, home: str, away: str, sids=(11, 12, 13),
            ts: str = "2026-10-10T18:00:00Z"):
    return {"market_id": mid, "market_name": "Match Odds",
            "event_name": f"{home} vs {away}", "open_date": ts,
            "runners": [{"selection_id": sids[0], "name": home},
                        {"selection_id": sids[1], "name": "Draw"},
                        {"selection_id": sids[2], "name": away}]}


def _sm_book(sids=(11, 12, 13), odds=(2.0, 3.5, 4.0), qty: float = 100.0) -> dict:
    """Book Smarkets: prezzo in bps (10000/odds), quantita' in 1e-4."""
    out = []
    for sid, od in zip(sids, odds):
        bps = int(round(10000.0 / od))
        out.append({"selectionId": sid,
                    "quotes": {str(sid): {"buy": {"price": bps,
                                                  "quantity": int(qty * 10000)}}}})
    return {"runners": out}


class FakeSm:
    def __init__(self, rows, books):
        self.rows = rows
        self.books = books

    def list_market_catalogue(self, event_type_ids=("football_match",),
                              market_type="match_odds", max_results=20):
        return list(self.rows)

    def get_market_book(self, market_id):
        if market_id not in self.books:
            raise RuntimeError(f"book {market_id} assente")
        return self.books[market_id]


# ---------------------------------------------------------------------------
# Helper di parsing
# ---------------------------------------------------------------------------

class TestParsing:
    def test_parse_ts_varianti(self):
        assert a._parse_ts("2026-10-10T18:00:00Z") is not None
        assert a._parse_ts("2026-10-10T18:00:00+00:00") is not None
        # naive -> assunta UTC
        assert a._parse_ts("2026-10-10T18:00:00") is not None
        assert a._parse_ts("") is None
        assert a._parse_ts(None) is None
        assert a._parse_ts("non-una-data") is None

    def test_sx_best_level(self):
        odds, size = a._sx_best_level([{"percentageOdds": int(0.5 * SCALE),
                                        "size": 250 * 10 ** 6}])
        assert odds == pytest.approx(2.0, abs=1e-6)
        assert size == pytest.approx(250.0)
        # libro vuoto / prezzo assurdo -> nessuna conclusione
        assert a._sx_best_level([]) == (None, 0.0)
        assert a._sx_best_level(None) == (None, 0.0)
        assert a._sx_best_level([{"percentageOdds": 0, "size": 100}]) == (None, 0.0)

    def test_sm_entry_price_qty_forme(self):
        assert a._sm_entry_price_qty({"price": 5000, "quantity": 100000}) == (5000, 100000.0)
        assert a._sm_entry_price_qty([{"price": 5000, "quantity": 20000}]) == (5000, 20000.0)
        assert a._sm_entry_price_qty([[5000, 30000]]) == (5000, 30000.0)
        assert a._sm_entry_price_qty(None) == (None, 0.0)
        assert a._sm_entry_price_qty({"quantity": 5}) == (None, 0.0)

    def test_sm_best(self):
        quotes = {"11": {"buy": {"price": 5000, "quantity": 100000}}}
        odds, size = a._sm_best(quotes, 11)
        assert odds == pytest.approx(2.0)
        assert size == pytest.approx(10.0)
        assert a._sm_best(quotes, 99) == (None, 0.0)
        assert a._sm_best("non-dict", 11) == (None, 0.0)

    def test_outcome_of(self):
        assert a._outcome_of("Alpha FC", "Alpha FC", "Beta FC") == "1"
        assert a._outcome_of("Beta FC", "Alpha FC", "Beta FC") == "2"
        assert a._outcome_of("Draw", "Alpha FC", "Beta FC") == "X"
        assert a._outcome_of("", "Alpha FC", "Beta FC") is None
        # un nome che non e' nessuno dei due ne' il pareggio: mai un indovinello
        assert a._outcome_of("Gamma FC", "Alpha FC", "Beta FC") is None


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

class TestSxEvents:
    def _rows(self):
        return [
            _sx_row("mh", "Alpha FC", "Beta FC", "Alpha FC"),
            _sx_row("md", "Alpha FC", "Beta FC", "Draw"),
            _sx_row("ma", "Alpha FC", "Beta FC", "Beta FC"),
        ]

    def test_tre_binari_diventano_un_evento(self):
        books = {"mh": _sx_book(3.2, 50), "md": _sx_book(3.0, 40),
                 "ma": _sx_book(3.4, 30)}
        evs = a.sx_events(FakeSx(self._rows(), books))
        assert len(evs) == 1
        ev = evs[0]
        assert ev["home"] == "Alpha FC" and ev["away"] == "Beta FC"
        assert set(ev["odds"]) == {"1", "X", "2"}
        # Tolleranza = granularita' della ladder SX (0.125% di probabilita'):
        # un prezzo "3.0" non e' un gradino esatto e viene riportato come
        # 3.0075, non per errore ma per quantizzazione (la decodifica tronca).
        assert ev["odds"]["1"]["odds"] == pytest.approx(3.2, abs=0.01)
        assert ev["odds"]["1"]["depth"] == pytest.approx(50.0)
        assert ev["odds"]["X"]["odds"] == pytest.approx(3.0, abs=0.01)
        assert ev["odds"]["2"]["odds"] == pytest.approx(3.4, abs=0.01)
        assert ev["venue"] == "sx"

    def test_book_assente_non_elimina_l_evento(self):
        books = {"mh": _sx_book(3.2)}          # solo Home ha book
        evs = a.sx_events(FakeSx(self._rows(), books))
        assert len(evs) == 1
        assert set(evs[0]["odds"]) == {"1"}

    def test_discovery_rossa_ritorna_vuoto(self):
        class Boom:
            def list_market_catalogue(self, **k):
                raise RuntimeError("SX giu'")
        assert a.sx_events(Boom()) == []

    def test_riga_senza_squadre_scartata(self):
        rows = [{"market_id": "x", "team_one_name": "", "team_two_name": "B",
                 "outcome_one_name": "A", "open_date": None}]
        assert a.sx_events(FakeSx(rows, {})) == []


class TestSmarketsEvents:
    def test_tre_contratti(self):
        rows = [_sm_row("sm1", "Alpha FC", "Beta FC")]
        books = {"sm1": _sm_book(odds=(2.0, 3.5, 4.0))}
        evs = a.smarkets_events(FakeSm(rows, books))
        assert len(evs) == 1
        ev = evs[0]
        assert ev["venue"] == "smarkets"
        assert set(ev["odds"]) == {"1", "X", "2"}
        assert ev["odds"]["1"]["odds"] == pytest.approx(2.0)
        assert ev["odds"]["X"]["odds"] == pytest.approx(3.5)
        assert ev["odds"]["2"]["odds"] == pytest.approx(4.0)
        assert ev["odds"]["1"]["depth"] == pytest.approx(100.0)
        assert ev["odds"]["1"]["id"] == 11

    def test_meno_di_tre_runner_scartato(self):
        rows = [{"market_id": "m", "event_name": "A vs B",
                 "runners": [{"selection_id": 1, "name": "A"}]}]
        assert a.smarkets_events(FakeSm(rows, {})) == []

    def test_discovery_rossa_ritorna_vuoto(self):
        class Boom:
            def list_market_catalogue(self, **k):
                raise RuntimeError("Smarkets giu'")
        assert a.smarkets_events(Boom()) == []


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

class TestMatching:
    def _ev(self, venue, home, away, kickoff):
        return {"venue": venue, "home": home, "away": away,
                "kickoff": kickoff, "odds": {}}

    def test_accoppia_per_nomi_e_kickoff(self):
        sx = [self._ev("sx", "Osasuna", "Getafe", "2026-10-10T18:00:00+00:00")]
        sm = [self._ev("smarkets", "CA Osasuna", "Getafe", "2026-10-10T18:05:00+00:00")]
        assert len(a.match_events(sx, sm)) == 1

    def test_kickoff_troppo_distante_non_accoppia(self):
        sx = [self._ev("sx", "Osasuna", "Getafe", "2026-10-10T18:00:00+00:00")]
        sm = [self._ev("smarkets", "Osasuna", "Getafe", "2026-10-11T18:00:00+00:00")]
        assert a.match_events(sx, sm) == []

    def test_squadre_diverse_non_accoppiano(self):
        sx = [self._ev("sx", "Manchester United", "Getafe", "2026-10-10T18:00:00+00:00")]
        sm = [self._ev("smarkets", "Manchester City", "Getafe", "2026-10-10T18:00:00+00:00")]
        assert a.match_events(sx, sm) == []

    def test_un_solo_accoppiamento_per_evento(self):
        sx = [self._ev("sx", "Osasuna", "Getafe", "2026-10-10T18:00:00+00:00")]
        sm = [self._ev("smarkets", "Osasuna", "Getafe", "2026-10-10T18:01:00+00:00"),
              self._ev("smarkets", "Osasuna", "Getafe", "2026-10-10T18:02:00+00:00")]
        assert len(a.match_events(sx, sm)) == 1


# ---------------------------------------------------------------------------
# Detection (arbitraggio)
# ---------------------------------------------------------------------------

class TestFindArbs:
    def _pair(self, sx_odds, sm_odds, sx_depth=100.0, sm_depth=100.0):
        sx = {"venue": "sx", "name": "Alpha vs Beta", "home": "Alpha",
              "away": "Beta", "kickoff": None, "league": "L",
              "odds": {k: {"odds": v, "depth": sx_depth, "id": k}
                       for k, v in sx_odds.items()}}
        sm = {"venue": "smarkets", "name": "Alpha vs Beta", "home": "Alpha",
              "away": "Beta", "kickoff": None, "league": "L",
              "odds": {k: {"odds": v, "depth": sm_depth, "id": k}
                       for k, v in sm_odds.items()}}
        return sx, sm

    def test_arbitraggio_rilevato_con_piano_di_capitale(self):
        sx, sm = self._pair({"1": 3.2, "X": 3.0, "2": 3.4},
                            {"1": 3.0, "X": 3.6, "2": 3.0})
        arbs = a.find_arbs([(sx, sm)], margin=0.005, budget=30.0)
        assert len(arbs) == 1
        o = arbs[0]
        # il miglior prezzo per esito viene dalla venue corretta
        by_out = {lg["outcome"]: lg for lg in o["legs"]}
        assert by_out["1"]["venue"] == "sx" and by_out["1"]["odds"] == pytest.approx(3.2)
        assert by_out["X"]["venue"] == "smarkets" and by_out["X"]["odds"] == pytest.approx(3.6)
        assert by_out["2"]["venue"] == "sx" and by_out["2"]["odds"] == pytest.approx(3.4)
        assert o["inverse_sum"] < 1.0
        assert o["roi_pct"] > 0
        assert o["profit"] > 0
        assert o["per_venue_cash"]["sx"] > 0 and o["per_venue_cash"]["smarkets"] > 0
        assert o["executable_at_budget"] is True

    def test_nessun_arbitraggio_sopra_soglia(self):
        sx, sm = self._pair({"1": 1.9, "X": 3.3, "2": 3.3},
                            {"1": 1.9, "X": 3.3, "2": 3.3})
        assert a.find_arbs([(sx, sm)], margin=0.005, budget=30.0) == []

    def test_esito_mancante_su_entrambe_non_e_opportunita(self):
        # L'unione delle venue COPRE tutti gli esiti: va bene (si gioca 1 su
        # SX, X e 2 su Smarkets). Serve che MANCHI lo stesso esito su ENTRAMBE.
        sx, sm = self._pair({"1": 3.2, "X": 3.0}, {"1": 3.0, "X": 3.6})
        assert a.find_arbs([(sx, sm)], margin=0.005, budget=30.0) == []

    def test_gambe_divise_fra_venue_formano_un_arbitraggio(self):
        sx, sm = self._pair({"1": 3.2}, {"X": 3.6, "2": 3.4})
        arbs = a.find_arbs([(sx, sm)], margin=0.005, budget=30.0)
        assert len(arbs) == 1
        by_out = {lg["outcome"]: lg["venue"] for lg in arbs[0]["legs"]}
        assert by_out == {"1": "sx", "X": "smarkets", "2": "smarkets"}

    def test_profondita_limita_il_budget_eseguibile(self):
        sx, sm = self._pair({"1": 3.2, "X": 3.0, "2": 3.4},
                            {"1": 3.0, "X": 3.6, "2": 3.0}, sx_depth=5.0)
        arbs = a.find_arbs([(sx, sm)], margin=0.005, budget=30.0)
        assert len(arbs) == 1
        o = arbs[0]
        assert o["max_budget_by_depth"] is not None
        assert o["max_budget_by_depth"] < 30.0
        assert o["executable_at_budget"] is False

    def test_profondita_ignota_e_fail_closed(self):
        sx, sm = self._pair({"1": 3.2, "X": 3.0, "2": 3.4},
                            {"1": 3.0, "X": 3.6, "2": 3.0}, sx_depth=0.0)
        arbs = a.find_arbs([(sx, sm)], margin=0.005, budget=30.0)
        assert len(arbs) == 1
        assert arbs[0]["max_budget_by_depth"] == 0.0
        assert arbs[0]["executable_at_budget"] is False

    def test_stake_sotto_minimo_non_eseguibile(self):
        sx, sm = self._pair({"1": 3.2, "X": 3.0, "2": 3.4},
                            {"1": 3.0, "X": 3.6, "2": 3.0})
        arbs = a.find_arbs([(sx, sm)], margin=0.005, budget=3.0)
        assert len(arbs) == 1
        o = arbs[0]
        # con 3 USDC una gamba scende sotto 1 USDC (floor) -> non eseguibile
        assert any(not lg["min_stake_ok"] for lg in o["legs"])
        assert o["executable_at_budget"] is False

    def test_ordinati_per_roi_decrescente(self):
        p1 = self._pair({"1": 3.2, "X": 3.0, "2": 3.4},
                        {"1": 3.0, "X": 3.6, "2": 3.0})
        p2 = self._pair({"1": 3.3, "X": 3.05, "2": 3.5},
                        {"1": 3.0, "X": 3.7, "2": 3.0})
        arbs = a.find_arbs([p1, p2], margin=0.005, budget=30.0)
        assert len(arbs) == 2
        assert arbs[0]["roi_pct"] >= arbs[1]["roi_pct"]


# ---------------------------------------------------------------------------
# Scan end-to-end (provider finti) e report
# ---------------------------------------------------------------------------

def _full_fakes():
    sx_rows = [_sx_row("mh", "Alpha FC", "Beta FC", "Alpha FC"),
               _sx_row("md", "Alpha FC", "Beta FC", "Draw"),
               _sx_row("ma", "Alpha FC", "Beta FC", "Beta FC")]
    sx_books = {"mh": _sx_book(3.2), "md": _sx_book(3.0), "ma": _sx_book(3.4)}
    sm_rows = [_sm_row("sm1", "Alpha FC", "Beta FC")]
    sm_books = {"sm1": _sm_book(odds=(3.0, 3.6, 3.0))}
    return FakeSx(sx_rows, sx_books), FakeSm(sm_rows, sm_books)


class TestScan:
    def test_scan_end_to_end(self, monkeypatch):
        monkeypatch.setattr(a, "smarkets_configured", lambda: True)
        sx, sm = _full_fakes()
        res = a.scan(sx_provider=sx, sm_provider=sm, margin=0.005, budget=30.0)
        assert res["sx_events"] == 1
        assert res["smarkets_events"] == 1
        assert res["matched"] == 1
        assert res["matched_with_3_outcomes"] == 1
        assert len(res["opportunities"]) == 1
        assert res["verdict"]["opportunities"] == 1
        assert res["verdict"]["executable"] == 1

    def test_scan_senza_credenziali_smarkets(self, monkeypatch):
        monkeypatch.setattr(a, "smarkets_configured", lambda: False)
        sx, sm = _full_fakes()
        res = a.scan(sx_provider=sx, sm_provider=sm)
        assert res["smarkets_configured"] is False
        assert res["smarkets_events"] == 0
        assert res["opportunities"] == []
        assert "Smarkets" in res["verdict"]["note"]

    def test_scan_fail_safe_su_discovery_rotta(self, monkeypatch):
        monkeypatch.setattr(a, "smarkets_configured", lambda: True)
        class Boom:
            def list_market_catalogue(self, **k):
                raise RuntimeError("giu'")
        res = a.scan(sx_provider=Boom(), sm_provider=Boom())
        assert res["sx_events"] == 0 and res["smarkets_events"] == 0
        assert res["opportunities"] == []


class TestReport:
    def test_report_dichiara_i_numeri(self, monkeypatch):
        monkeypatch.setattr(a, "smarkets_configured", lambda: True)
        sx, sm = _full_fakes()
        res = a.scan(sx_provider=sx, sm_provider=sm, margin=0.005, budget=30.0)
        txt = a.format_report(res)
        assert "ARBITRAGGIO SX Bet" in txt
        assert "Alpha FC vs Beta FC" in txt
        assert "ROI +" in txt and "cassa per venue" in txt

    def test_report_senza_credenziali_lo_dichiara(self, monkeypatch):
        monkeypatch.setattr(a, "smarkets_configured", lambda: False)
        res = a.scan(sx_provider=FakeSx([], {}), sm_provider=FakeSm([], {}))
        assert "credenziali Smarkets ASSENTI" in a.format_report(res)


# ---------------------------------------------------------------------------
# Tripwire: misuratore, non esecutore
# ---------------------------------------------------------------------------

class TestTripwire:
    def test_nessun_percorso_d_ordine_nel_codice(self):
        code = _code_only(SRC)
        for forbidden in ("place_limit_order", "place_order", "save_bet",
                          "ExecutionEngine", "auto_bet", "tracker"):
            assert forbidden not in code, f"trovato percorso d'ordine: {forbidden}"

    def test_nessuna_scrittura_sul_ledger(self):
        code = _code_only(SRC)
        for forbidden in ("INSERT", "UPDATE", "DELETE", "sqlite3"):
            assert forbidden not in code, f"trovato accesso al ledger: {forbidden}"

    def test_nessuna_rete_al_livello_di_modulo(self):
        code = _code_only(SRC)
        assert "requests" not in code
        assert "httpx" not in code and "aiohttp" not in code

    def test_connessioni_provider_solo_pigre(self):
        """I provider si costruiscono DENTRO le funzioni, mai all'import."""
        code = _code_only(SRC)
        assert "build_sx_provider" in code and "build_smarkets_provider" in code
        # `import execution_engine` deve stare dentro una funzione (indentato).
        for line in code.splitlines():
            if "import execution_engine" in line:
                assert line.startswith(" "), \
                    "import execution_engine a livello di modulo"

    def test_import_leggero(self):
        """Importare arbitrage_scan NON carica la produzione."""
        out = subprocess.run(
            [sys.executable, "-c",
             "import sys, arbitrage_scan; "
             "bad=[m for m in ('execution_engine','tracker','auto_bet','bot') "
             "if m in sys.modules]; "
             "print('|'.join(bad))"],
            capture_output=True, text=True, cwd=str(Path(a.__file__).parent))
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == "", f"moduli caricati: {out.stdout}"
