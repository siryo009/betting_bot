"""Test della telemetria ombra (`market_shadow.py`) — TUTTI OFFLINE.

Contesto (01/10/2026): il proprietario ha chiesto la telemetria sui mercati SX
NON calcistici (Basketball `sportId` 1, American Football `8`) registrando i
dati **esclusivamente** su `market_quotes`. Il vincolo tassativo e' che NESSUNA
riga venga scritta su `predictions` (una previsione a stake 0 non e' un
esperimento: e' un dato falso che inquina ROI e calibrazione).

Qui i provider e i book sono finti e iniettati: zero rete, zero chiavi, zero
crediti, zero ordini.
"""

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

import market_shadow
from market_shadow import build_rows, discover, enabled, format_report, run, shadow_markets


class FakeProvider:
    """Provider SX finto: risponde per (sportIds, type) e registra le chiamate."""

    def __init__(self, markets_by=None, *, fail=False):
        self.markets_by = markets_by or {}
        self.fail = fail
        self.calls = []

    def _get(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        if self.fail:
            raise RuntimeError("rete giu'")
        key = (str((params or {}).get("sportIds")), str((params or {}).get("type")))
        return {"data": {"markets": list(self.markets_by.get(key, [])),
                         "nextKey": None}}


def _mkt(event, t1, t2, hash_, kickoff_ms, o1, o2, *, main=True, line=None,
         league="NBA"):
    m = {"sportXeventId": event, "teamOneName": t1, "teamTwoName": t2,
         "marketHash": hash_, "gameTime": kickoff_ms // 1000,
         "leagueLabel": league, "outcomeOneName": o1, "outcomeTwoName": o2,
         "mainLine": main}
    if line is not None:
        m["line"] = line
    return m


def _kickoff_ms(hours=3):
    return int((datetime.now(timezone.utc) + timedelta(hours=hours)).timestamp() * 1000)


def _book(price1=1.9, price2=1.95, depth=120.0):
    return {1: {"best": {"price": price1}, "depth": depth},
            2: {"best": {"price": price2}, "depth": depth}}


# ---------------------------------------------------------------------------
# Interruttore e configurazione
# ---------------------------------------------------------------------------

class TestConfigurazione:
    def test_default_spento(self, monkeypatch):
        """Nessuna attivita' implicita: la telemetria parte solo se accesa."""
        monkeypatch.delenv("SHADOW_MARKET_ENABLED", raising=False)
        assert enabled() is False

    @pytest.mark.parametrize("value", ["1", "true", "on", "yes"])
    def test_si_accende_con_flag(self, monkeypatch, value):
        monkeypatch.setenv("SHADOW_MARKET_ENABLED", value)
        assert enabled() is True

    def test_mercati_derivati_dal_contratto(self, monkeypatch):
        monkeypatch.delenv("SHADOW_TYPES", raising=False)
        assert shadow_markets() == ("OU_OT", "AH_OT", "ML_OT")

    def test_type_id_ignoto_non_diventa_un_mercato(self, monkeypatch):
        """Un tipo che il contratto non conosce si IGNORA (mai inventato)."""
        assert shadow_markets(("28", "9999")) == ("OU_OT",)


# ---------------------------------------------------------------------------
# Lettura della linea (dominio NON calcistico)
# ---------------------------------------------------------------------------

class TestLinea:
    def test_linea_dai_nomi(self):
        raw = {"outcomeOneName": "Over 220.5", "outcomeTwoName": "Under 220.5"}
        assert market_shadow._line_for(raw, has_lines=True) == 220.5

    def test_linea_dal_campo_oltre_il_limite_calcistico(self):
        """Il campo vale anche a 220.5: il limite di `multi_market` (12) e' calcistico."""
        assert market_shadow._line_for({"line": "220.5"}, has_lines=True) == 220.5

    def test_campo_fuori_plausibilita_ignorato(self):
        assert market_shadow._line_for({"line": "99999"}, has_lines=True) is None

    def test_nessuna_linea_sui_mercati_senza(self):
        assert market_shadow._line_for({"outcomeOneName": "Over 2.5"},
                                      has_lines=False) is None


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

class TestDiscovery:
    def _provider(self):
        ko = _kickoff_ms(3)
        far = _kickoff_ms(72)          # fuori finestra (24h)
        return FakeProvider({
            ("1", "28"): [_mkt("e1", "Lakers", "Celtics", "h1", ko,
                               "Over 220.5", "Under 220.5", line=220.5),
                          _mkt("e2", "Heat", "Bulls", "h2", far,
                               "Over 210.5", "Under 210.5", line=210.5)],
            ("8", "342"): [_mkt("e3", "Chiefs", "Bills", "h3", ko,
                                "Chiefs -3.5", "Bills +3.5", line=-3.5)],
            ("1", "226"): [_mkt("e4", "Suns", "Nets", "h4", ko,
                                "Suns", "Nets")],
            ("5", "2"): [_mkt("x", "Inter", "Milan", "hx", ko,
                              "Over 2.5", "Under 2.5", line=2.5)],
        })

    def test_legge_gli_sport_non_calcistici_e_ignora_il_calcio(self):
        prov = self._provider()
        records = discover(prov, sports=("1", "8"), types=("28", "342", "226"))
        markets = sorted(r["market_type"] for r in records)
        # e4 (226, dentro finestra) + e1 (28) + e3 (342); e2 fuori finestra.
        assert markets == ["AH_OT", "ML_OT", "OU_OT"]
        # Il calcio (sportIds=5) non e' mai interrogato.
        assert all(c[1].get("sportIds") != "5" for c in prov.calls)
        assert {c[1]["sportIds"] for c in prov.calls} == {"1", "8"}

    def test_linea_e_main_line_coerenti(self):
        records = discover(self._provider(), sports=("1", "8"))
        by_market = {r["market_type"]: r for r in records}
        assert by_market["OU_OT"]["line"] == 220.5
        assert by_market["AH_OT"]["line"] == -3.5
        # Il moneyline non ha linea: `main_line` resta None (il contratto la
        # VIETA sui mercati senza, e una riga respinta sarebbe un dato perso).
        assert by_market["ML_OT"]["line"] is None
        assert by_market["ML_OT"]["main_line"] is None

    def test_sport_id_registrato(self):
        records = discover(self._provider(), sports=("1",))
        assert {r["sport_id"] for r in records} == {"1"}

    def test_record_malformati_saltati(self):
        ko = _kickoff_ms(2)
        prov = FakeProvider({("1", "226"): [
            _mkt("e1", "", "Nets", "h1", ko, "A", "B"),      # squadra mancante
            _mkt("e2", "Suns", "Nets", "", ko, "Suns", "Nets"),  # hash mancante
            _mkt("e3", "Suns", "Nets", "h3", ko, "Suns", "Nets"),  # ok
        ]})
        records = discover(prov, sports=("1",), types=("226",))
        assert len(records) == 1 and records[0]["event_id"] == "e3"

    def test_read_fallita_non_solleva(self):
        prov = FakeProvider(fail=True)
        assert discover(prov, sports=("1",), types=("226",)) == []


# ---------------------------------------------------------------------------
# Contratto + righe
# ---------------------------------------------------------------------------

class TestRigheContratto:
    def _record(self, market_type, *, line, o1, o2, home="Lakers",
                away="Celtics", market_hash="h1"):
        return {"event_id": "e1", "sport_id": "1", "league_label": "NBA",
                "kickoff_ms": _kickoff_ms(2), "home": home, "away": away,
                "market_type": market_type, "line": line,
                "main_line": True if line is not None else None,
                "market_hash": market_hash, "outcome_one": o1,
                "outcome_two": o2}

    def test_righe_ou_ot_e_esito_a_ledger(self):
        rec = self._record("OU_OT", line=220.5, o1="Over 220.5",
                           o2="Under 220.5")
        rows, stats = build_rows([rec], {"h1": _book()})
        assert stats["built"] == 2 and not stats["rejected"]
        by_sel = {r["selection"]: r for r in rows}
        assert by_sel["over"]["ledger_esito"] == "Over 220.5"
        assert by_sel["under"]["odds"] == 1.95
        assert by_sel["over"]["main_line"] == 1 or by_sel["over"]["main_line"] is True

    def test_righe_moneyline_senza_linea(self):
        rec = self._record("ML_OT", line=None, o1="Lakers", o2="Celtics")
        rows, stats = build_rows([rec], {"h1": _book()})
        assert stats["built"] == 2
        by_sel = {r["selection"]: r for r in rows}
        assert by_sel["1"]["ledger_esito"] == "Home"
        assert by_sel["2"]["ledger_esito"] == "Away"

    def test_righe_ah_ot(self):
        rec = self._record("AH_OT", line=-3.5, o1="Lakers -3.5",
                           o2="Celtics +3.5")
        rows, _ = build_rows([rec], {"h1": _book()})
        by_sel = {r["selection"]: r for r in rows}
        assert by_sel["1"]["ledger_esito"] == "Home -3.5"
        assert by_sel["2"]["ledger_esito"] == "Away +3.5"

    def test_book_in_errore_contato(self):
        rec = self._record("OU_OT", line=220.5, o1="Over 220.5",
                           o2="Under 220.5")
        rows, stats = build_rows([rec], {"h1": {"error": "timeout"}})
        assert rows == [] and stats["no_book"] == 1

    def test_lato_non_riconosciuto(self):
        rec = self._record("AH_OT", line=-3.5, o1="Squadra Ignota",
                           o2="Altra Ignota")
        rows, stats = build_rows([rec], {"h1": _book()})
        assert rows == [] and stats["no_side"] == 1

    def test_batch_incoerente_scartato(self):
        """inv_sum fuori 0.98-1.08 = book sporco: le due righe restano fuori."""
        rec = self._record("OU_OT", line=220.5, o1="Over 220.5",
                           o2="Under 220.5")
        rows, stats = build_rows([rec], {"h1": _book(price1=1.01, price2=1.01)})
        assert rows == [] and stats["incoherent"] == 1


# ---------------------------------------------------------------------------
# Ciclo completo: SOLO market_quotes
# ---------------------------------------------------------------------------

class TestRunScriveSoloMarketQuotes:
    def _wire(self, monkeypatch):
        ko = _kickoff_ms(3)
        prov = FakeProvider({("1", "28"): [
            _mkt("e1", "Lakers", "Celtics", "h1", ko,
                 "Over 220.5", "Under 220.5", line=220.5)],
            ("1", "226"): [
            _mkt("e4", "Suns", "Nets", "h4", ko, "Suns", "Nets")]})
        import sx_signals
        monkeypatch.setattr(sx_signals, "_books_parallel",
                            lambda p, hashes: {h: _book() for h in hashes})
        return prov

    def test_salva_solo_su_market_quotes(self, monkeypatch):
        prov = self._wire(monkeypatch)
        captured = {}

        import tracker

        def _save(rows):
            captured["rows"] = list(rows)
            return {"saved": len(rows), "skipped": 0, "fixtures": 2,
                    "error": None}

        monkeypatch.setattr(tracker, "save_market_quotes", _save)

        def _boom(*a, **k):                      # pragma: no cover
            raise AssertionError("market_shadow ha scritto su predictions!")

        monkeypatch.setattr(tracker, "save_prediction", _boom)

        summary = run(prov)
        assert summary["saved"] == len(captured["rows"]) > 0
        assert summary["fixtures"] == 2
        assert {r["market_type"] for r in captured["rows"]} == {"OU_OT", "ML_OT"}
        assert summary["error"] is None

    def test_no_save_non_tocca_il_ledger(self, monkeypatch):
        prov = self._wire(monkeypatch)
        import tracker
        monkeypatch.setattr(tracker, "save_market_quotes",
                            lambda rows: (_ for _ in ()).throw(
                                AssertionError("nessuna scrittura con --no-save")))
        summary = run(prov, save=False)
        assert summary["saved"] == 0 and summary["quotes"] > 0

    def test_fail_safe_su_provider_rotto(self):
        summary = run(FakeProvider(fail=True))
        assert summary["records"] == 0 and summary["error"] is None

    def test_nessun_mercato_non_e_un_errore(self):
        summary = run(FakeProvider({}))
        assert summary["records"] == 0 and summary["error"] is None


# ---------------------------------------------------------------------------
# Report e CLI
# ---------------------------------------------------------------------------

class TestReport:
    def test_dichiara_il_vincolo(self):
        text = format_report({"sports": ["1"], "markets": ["OU_OT"],
                              "records": 1, "quotes": 2, "saved": 2,
                              "skipped": 0, "fixtures": 1,
                              "by_market": {"OU_OT": 1}, "error": None})
        assert "predictions" in text
        assert "market_quotes" in text

    def test_cli_json(self, monkeypatch, capsys):
        monkeypatch.setattr(market_shadow, "run",
                            lambda **k: {"sports": [], "markets": [],
                                         "records": 0, "quotes": 0,
                                         "saved": 0, "skipped": 0,
                                         "fixtures": 0, "error": None})
        assert market_shadow.main(["--json"]) == 0
        assert json.loads(capsys.readouterr().out)["records"] == 0


# ---------------------------------------------------------------------------
# Tripwire: il vincolo tassativo e' strutturale, non una promessa
# ---------------------------------------------------------------------------

class TestTripwire:
    @staticmethod
    def _code_only():
        """Sorgente senza docstring e commenti.

        Lezione del 29/09 (`test_money_decimal`): un tripwire lessicale che
        legge la PROSA colpisce la documentazione invece del codice. Qui i
        docstring NOMINANO cio' che il modulo non deve fare (e' il senso del
        vincolo): si scansiona quindi solo il codice vero.
        """
        import ast

        with open("market_shadow.py", encoding="utf-8") as fh:
            src = fh.read()
        spans = []
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                spans.append((node.lineno, node.end_lineno or node.lineno))
        keep = []
        for index, line in enumerate(src.splitlines(), start=1):
            if any(lo <= index <= hi for lo, hi in spans):
                continue
            stripped = line.strip()
            keep.append("" if stripped.startswith("#") else line)
        return "\n".join(keep)

    def test_nessuna_scrittura_sul_ledger_previsioni(self):
        src = self._code_only()
        for forbidden in ("save_prediction", "INSERT INTO predictions",
                          "predictions SET"):
            assert forbidden not in src, forbidden

    def test_nessun_ordine(self):
        src = self._code_only()
        for forbidden in ("place_limit_order", "resolve_market_for",
                          "_live_fill", "place_order"):
            assert forbidden not in src, forbidden

    def test_env_dichiarate_nella_iac(self):
        """Lezione 28/09: `config apply` distrugge cio' che non e' dichiarato.

        L'interruttore della telemetria e la copertura sono env d'operatore:
        se non stanno in `preserve()` un apply le cancella senza avviso.
        """
        with open(".railway/railway.ts", encoding="utf-8") as fh:
            iac = fh.read()
        for name in ("SHADOW_MARKET_ENABLED", "SHADOW_SPORTS", "SHADOW_TYPES",
                     "SHADOW_HOURS_AHEAD", "SHADOW_MAX_MARKETS",
                     "SHADOW_GATEWAY_ID"):
            assert f"{name}: preserve()" in iac, name

    def test_job_registrato_e_spento_di_default(self):
        """Il job esiste in `bot.py` ma a codice invariato non parte."""
        with open("bot.py", encoding="utf-8") as fh:
            src = fh.read()
        assert "async def market_shadow_job" in src
        assert "run_repeating(market_shadow_job" in src
        assert "if not market_shadow.enabled():" in src

    def test_import_leggero(self):
        """`import market_shadow` non carica la produzione (import pigri)."""
        code = ("import sys, market_shadow;"
                "bad=[m for m in ('tracker','auto_bet','bot','decision',"
                "'multi_market','sx_signals','execution_engine') "
                "if m in sys.modules];"
                "print(bad)")
        out = subprocess.run([sys.executable, "-c", code],
                             capture_output=True, text=True)
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == "[]"
