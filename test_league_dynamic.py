"""Test del gate di lega DINAMICO (`league_dynamic.py`).

Direttiva del proprietario (29/09/2026): il gate di lega e' una tabella STATICA
(decisa il 12/09, congelata dal 22/09) mentre il ledger cresce ogni giorno, e
fino al 22/09 la strategia per campionato era NON misurabile (75,9% delle righe
senza lega). Ora `predictions.league` esiste, quindi quella misura e' nel ledger
e non deve restare una query a mano: le due letture sbagliate del 22/09 e del
25/09 erano nate proprio da query manuali senza filtro d'era.

Coperti: stato per lega (insufficient/hold/demote/promote), filtri CONDIVISI di
era e fascia quota (con il conteggio DICHIARATO delle righe escluse), sola
lettura, autorevolezza del modulo (default OFF; quando acceso puo' solo
RESTRINGERE, mai promuovere), fail-open sulla lettura e fail-closed sul
campione, report/CLI, cache e tripwire (nessuna scrittura, nessuna rete,
nessun ordine, import leggero, env in IaC).

Tutti i test sono OFFLINE: ledger SQLite temporaneo, nessuna rete, nessun
provider, zero crediti. Le date sono sempre RELATIVE o dentro l'era corrente, mai
fisse nel futuro (lezione del 15/09: un test che scade col calendario fallisce
sempre nel momento peggiore).
"""

import json
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

import league_dynamic as ld
import tracker
import value_filter as vf

# Dentro l'era corrente (`LEAGUE_DYNAMIC_SINCE` = 2026-09-11) e in fascia
# quota (1.30-1.80): senza queste due proprieta' le righe verrebbero escluse
# dai filtri condivisi e i test misurerebbero il filtro, non lo stato.
ERA_DATE = "2026-09-25T10:00:00"
QUOTA = 1.65


@pytest.fixture()
def temp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "league_dynamic.db"
        monkeypatch.setattr(tracker, "DB_PATH", db_path)
        tracker.init_db()
        yield db_path


_SEED_COLS = ("match_id", "mercato", "esito", "quota", "prob", "ev",
              "status", "esito_finale", "profit", "created_at", "settled_at",
              "league")


def _row(mid, finale, profit, *, league="Bundesliga", quota=QUOTA,
         status="value", created=ERA_DATE, mercato="1X2", ev=0.05):
    return {"match_id": mid, "mercato": mercato, "esito": f"SEL-{mid}",
            "quota": quota, "prob": 0.62, "ev": ev, "status": status,
            "esito_finale": finale, "profit": profit, "created_at": created,
            "settled_at": created, "league": league}


def _seed(db_path, rows):
    """Inserisce righe CHIUSE direttamente nel ledger temporaneo."""
    conn = sqlite3.connect(db_path)
    conn.executemany(
        f"INSERT INTO predictions ({', '.join(_SEED_COLS)}) "
        f"VALUES ({', '.join('?' * len(_SEED_COLS))})",
        [tuple(r.get(c) for c in _SEED_COLS) for r in rows])
    conn.commit()
    conn.close()


def _mix(won=0, lost=0, push=0, *, league="Bundesliga", quota=QUOTA,
         status="value", prefix="m", created=ERA_DATE):
    """Campione con un ROI ESATTO: `won` vittorie a quota, `lost`, `push`."""
    rows = []
    outcomes = ([("won", round(quota - 1.0, 4))] * won
                + [("lost", -1.0)] * lost
                + [("push", 0.0)] * push)
    for i, (finale, profit) in enumerate(outcomes):
        rows.append(_row(f"{prefix}-{i}", finale, profit, league=league,
                         quota=quota, status=status, created=created))
    return rows


# ---------------------------------------------------------------------------
# Stato per lega
# ---------------------------------------------------------------------------

class TestStato:
    def test_soglia_campione_allineata_al_progetto(self):
        """Una sola idea di \"campione affidabile\": 30, come significance."""
        import significance
        assert ld.min_samples() == 30
        assert ld.min_samples() == significance.MIN_SAMPLES

    def test_campione_insufficiente_non_conclude_nulla(self, temp_db):
        """5 perdite secche = ROI -100%, ma il campione non prova niente."""
        _seed(temp_db, _mix(lost=5, league="Bundesliga", prefix="a"))
        row = ld.league_stats()["leagues"]["Bundesliga"]
        assert row["n"] == 5
        assert row["roi"] == -1.0
        assert row["status"] == "insufficient"
        assert row["enough_sample"] is False

    def test_hold_nel_mezzo(self, temp_db):
        """ROI +10%: positivo ma sotto la soglia di promozione -> hold."""
        _seed(temp_db, _mix(won=20, lost=10, league="Eredivisie", prefix="b"))
        row = ld.league_stats()["leagues"]["Eredivisie"]
        assert row["n"] == 30
        assert row["roi"] == pytest.approx(0.10, abs=1e-6)
        assert row["status"] == "hold"

    def test_demote_su_perdita_catastrofica(self, temp_db):
        """30 perdite secche: e' il caso che la restrizione deve fermare."""
        _seed(temp_db, _mix(lost=30, league="Bundesliga", prefix="c"))
        row = ld.league_stats()["leagues"]["Bundesliga"]
        assert row["n"] == 30
        assert row["status"] == "demote"
        assert row["roi"] <= ld.demote_roi()

    def test_promote_come_candidata(self, temp_db):
        """27 vittorie e 3 sconfitte a 1.65 = ROI +48.5% -> candidata."""
        _seed(temp_db, _mix(won=27, lost=3, league="Premier League", prefix="d"))
        row = ld.league_stats()["leagues"]["Premier League"]
        assert row["roi"] == pytest.approx(0.485, abs=1e-6)
        assert row["status"] == "promote"

    def test_soglie_da_env(self, temp_db, monkeypatch):
        """La soglia e' `roi <= demote_roi`: alzandola, la stessa lega cambia
        stato senza toccare il ledger (nessun redeploy)."""
        _seed(temp_db, _mix(won=20, lost=10, league="Bundesliga", prefix="e"))
        # ROI +10%: con la soglia di default (-20%) e' hold...
        assert ld.league_stats()["leagues"]["Bundesliga"]["status"] == "hold"
        # ...con la soglia a +15% la stessa lega finisce in demote.
        monkeypatch.setenv("LEAGUE_DYNAMIC_DEMOTE_ROI", "0.15")
        assert ld.league_stats()["leagues"]["Bundesliga"]["status"] == "demote"
        # e abbassandola a -50% torna hold.
        monkeypatch.setenv("LEAGUE_DYNAMIC_DEMOTE_ROI", "-0.50")
        assert ld.league_stats()["leagues"]["Bundesliga"]["status"] == "hold"

    def test_conteggi_vittorie_perse_push(self, temp_db):
        _seed(temp_db, _mix(won=10, lost=10, push=5, league="Serie B",
                            prefix="f"))
        row = ld.league_stats()["leagues"]["Serie B"]
        assert (row["won"], row["lost"], row["push"]) == (10, 10, 5)
        assert row["n"] == 25
        assert row["hit_rate"] == pytest.approx(0.4, abs=1e-6)


# ---------------------------------------------------------------------------
# Filtri CONDIVISI: era, fascia quota, stati giocabili
# ---------------------------------------------------------------------------

class TestFiltri:
    def test_fuori_era_escluso_e_dichiarato(self, temp_db):
        """Una riga nata PRIMA dell'era corrente non entra nella misura."""
        _seed(temp_db, _mix(lost=30, league="Bundesliga", prefix="old",
                            created="2026-09-01T10:00:00"))
        _seed(temp_db, _mix(lost=1, league="Bundesliga", prefix="new"))
        st = ld.league_stats()
        assert st["meta"]["rows_unfiltered"] == 31
        assert st["meta"]["rows_filtered_out"] == 30
        assert st["leagues"]["Bundesliga"]["n"] == 1

    def test_fuori_fascia_quota_escluso(self, temp_db):
        _seed(temp_db, _mix(lost=30, league="Bundesliga", quota=2.60,
                            prefix="wide"))
        _seed(temp_db, _mix(lost=1, league="Bundesliga", prefix="narrow"))
        st = ld.league_stats()
        assert st["leagues"]["Bundesliga"]["n"] == 1
        assert st["meta"]["rows_filtered_out"] == 30

    def test_solo_le_righe_giocabili_contano(self, temp_db):
        """Le righe `rejected` non sono giocate: non devono pesare sul ROI."""
        _seed(temp_db, _mix(lost=30, league="Bundesliga", status="rejected",
                            prefix="rej"))
        _seed(temp_db, _mix(won=30, league="Bundesliga", prefix="play"))
        st = ld.league_stats()
        row = st["leagues"]["Bundesliga"]
        assert row["n"] == 30            # solo le giocabili
        assert row["status"] == "promote"
        assert st["meta"]["rows_excluded"] == 30   # le rejected, dichiarate

    def test_righe_senza_lega_dichiarate_non_nascoste(self, temp_db):
        """Storicamente la maggioranza: sparirebbero in silenzio."""
        _seed(temp_db, [dict(_row("noleague-1", "lost", -1.0), league="")])
        _seed(temp_db, _mix(won=1, league="Bundesliga", prefix="ok"))
        st = ld.league_stats()
        assert st["senza_lega"]["n"] == 1
        assert st["senza_lega"]["roi"] == -1.0
        assert st["leagues"]["Bundesliga"]["n"] == 1

    def test_leghe_diverse_separate(self, temp_db):
        _seed(temp_db, _mix(lost=30, league="Bundesliga", prefix="bl"))
        _seed(temp_db, _mix(won=27, lost=3, league="Eredivisie", prefix="er"))
        st = ld.league_stats()
        assert st["leagues"]["Bundesliga"]["status"] == "demote"
        assert st["leagues"]["Eredivisie"]["status"] == "promote"


# ---------------------------------------------------------------------------
# AUTORITA': default OFF, solo restrizione, mai promozione
# ---------------------------------------------------------------------------

class TestAutorita:
    def _perdente(self, temp_db, league="Bundesliga", n=30, prefix="p"):
        _seed(temp_db, _mix(lost=n, league=league, prefix=prefix))

    def test_default_off_non_restrittinge_nulla(self, temp_db, monkeypatch):
        """Interruttore spento: anche una lega in perdita resta intatta."""
        monkeypatch.delenv("LEAGUE_DYNAMIC_ENABLED", raising=False)
        ld.reset_cache()
        self._perdente(temp_db)
        assert ld.enabled() is False
        assert ld.restricted("Bundesliga") is False
        assert ld.effective_allowed("Bundesliga") is True

    def test_acceso_restringe_la_lega_in_perdita(self, temp_db, monkeypatch):
        monkeypatch.setenv("LEAGUE_DYNAMIC_ENABLED", "1")
        ld.reset_cache()
        self._perdente(temp_db, league="Bundesliga", prefix="q")
        assert ld.restricted("Bundesliga") is True
        assert ld.effective_allowed("Bundesliga") is False

    def test_acceso_non_tocca_le_altre_leghe(self, temp_db, monkeypatch):
        monkeypatch.setenv("LEAGUE_DYNAMIC_ENABLED", "1")
        ld.reset_cache()
        self._perdente(temp_db, league="Bundesliga", prefix="r")
        _seed(temp_db, _mix(won=20, lost=10, league="Eredivisie", prefix="s"))
        assert ld.restricted("Eredivisie") is False
        assert ld.effective_allowed("Eredivisie") is True

    def test_mai_promozione_automatica(self, temp_db, monkeypatch):
        """Una lega BLoccata con ROI stellare resta bloccata: aprire una lega a
        denaro reale e' una decisione dell'operatore, non un effetto del
        campione (30 chiusure distinguono solo un edge del ~41%)."""
        monkeypatch.setenv("LEAGUE_DYNAMIC_ENABLED", "1")
        ld.reset_cache()
        _seed(temp_db, _mix(won=30, league="Serie A", prefix="t"))
        assert ld.restricted("Serie A") is False
        assert ld.effective_allowed("Serie A") is False   # gate STATICO
        assert ld.report()["promote_candidates"] == []    # non ammessa a monte

    def test_fail_closed_sul_campione(self, temp_db, monkeypatch):
        """Acceso + perdita catastrofica ma sotto il campione minimo: niente."""
        monkeypatch.setenv("LEAGUE_DYNAMIC_ENABLED", "1")
        ld.reset_cache()
        self._perdente(temp_db, n=5, prefix="u")
        assert ld.restricted("Bundesliga") is False

    def test_fail_open_su_lettura_rotta(self, temp_db, monkeypatch):
        """Un errore di telemetria NON deve cambiare il gate."""
        monkeypatch.setenv("LEAGUE_DYNAMIC_ENABLED", "1")
        ld.reset_cache()

        def _boom(*a, **kw):
            raise sqlite3.OperationalError("db locked")

        monkeypatch.setattr(tracker, "get_predictions", _boom)
        assert ld.restricted("Bundesliga") is False
        res = ld.report()
        assert res["filter"]["error"]
        assert res["leagues"] == {}

    def test_restringe_solo_leghe_ammesse(self, temp_db, monkeypatch):
        """`restricted` non ha senso su una lega gia' vietata, e il report non
        deve proporla come 'da restringere' (sarebbe rumore su rumore)."""
        monkeypatch.setenv("LEAGUE_DYNAMIC_ENABLED", "1")
        ld.reset_cache()
        _seed(temp_db, _mix(lost=30, league="Greek Super League", prefix="v"))
        _seed(temp_db, _mix(lost=30, league="Bundesliga", prefix="w"))
        res = ld.report()
        assert res["restricted"] == ["Bundesliga"]
        assert res["leagues"]["Greek Super League"]["allowed"] is False


# ---------------------------------------------------------------------------
# Report e CLI
# ---------------------------------------------------------------------------

class TestReport:
    def test_report_dichiara_il_filtro(self, temp_db):
        _seed(temp_db, _mix(lost=30, league="Bundesliga", prefix="x"))
        _seed(temp_db, _mix(lost=30, league="Bundesliga", quota=3.40,
                            prefix="y"))
        res = ld.report()
        f = res["filter"]
        assert f["since"] == ld.era_since()
        assert f["odds_min"] == vf.ODDS_MIN and f["odds_max"] == vf.ODDS_MAX
        assert f["rows_unfiltered"] == 60
        assert f["rows_filtered_out"] == 30
        assert f["rows_kept"] == 30
        assert f["rows_excluded"] == 0

    def test_format_report_mostra_era_e_conteggi(self, temp_db):
        _seed(temp_db, _mix(lost=30, league="Bundesliga", prefix="z"))
        text = ld.format_report()
        assert "Gate di lega dinamico" in text
        assert ld.era_since() in text
        assert "30 giocabili su 30" in text
        assert "Bundesliga" in text

    def test_format_report_dichiara_lo_stato_off(self, temp_db, monkeypatch):
        monkeypatch.delenv("LEAGUE_DYNAMIC_ENABLED", raising=False)
        ld.reset_cache()
        _seed(temp_db, _mix(lost=30, league="Bundesliga", prefix="zz"))
        text = ld.format_report()
        assert "OFF (sola telemetria)" in text
        assert "Nessuna restrizione applicata" in text

    def test_cli_json_e_all(self, temp_db, monkeypatch):
        _seed(temp_db, _mix(lost=5, league="Bundesliga", prefix="cli"))
        assert ld.main(["--json"]) == 0
        assert ld.main(["--all"]) == 0

    def test_cli_since_esclude_le_righe_vecchie(self, temp_db):
        _seed(temp_db, _mix(lost=30, league="Bundesliga", prefix="vec",
                            created="2026-09-20T10:00:00"))
        res = ld.report(since="2026-09-28")
        assert res["leagues"] == {}


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

class TestCache:
    def test_cache_evita_riletture_e_si_resetta(self, temp_db, monkeypatch):
        _seed(temp_db, _mix(lost=30, league="Bundesliga", prefix="k"))
        calls = {"n": 0}
        real = tracker.get_predictions

        def _counted(*a, **kw):
            calls["n"] += 1
            return real(*a, **kw)

        monkeypatch.setattr(tracker, "get_predictions", _counted)
        ld.reset_cache()
        ld.table()
        first = calls["n"]
        ld.table()
        assert calls["n"] == first          # servita dalla cache
        ld.reset_cache()
        ld.table()
        assert calls["n"] > first

    def test_ttl_zero_ricalcola_ogni_volta(self, temp_db, monkeypatch):
        """TTL 0 = cache disattivata (utile in diagnostica)."""
        _seed(temp_db, _mix(lost=30, league="Bundesliga", prefix="l"))
        monkeypatch.setenv("LEAGUE_DYNAMIC_TTL", "0")
        calls = {"n": 0}
        real = tracker.get_predictions

        def _counted(*a, **kw):
            calls["n"] += 1
            return real(*a, **kw)

        monkeypatch.setattr(tracker, "get_predictions", _counted)
        ld.reset_cache()
        ld.table()
        first = calls["n"]
        ld.table()
        assert calls["n"] > first          # nessuna cache: si rilegge


# ---------------------------------------------------------------------------
# Tripwire: sola lettura, niente rete, niente ordini, import leggero, IaC
# ---------------------------------------------------------------------------

SOURCE = Path("league_dynamic.py").read_text(encoding="utf-8")


def _code_only(source: str = SOURCE) -> str:
    """Sorgente SENZA docstring: il tripwire non deve colpire la PROSA.

    Le docstring del modulo NOMINANO di proposito cio' che il modulo rifiuta di
    fare (\"sola lettura\", \"nessun ordine\", \"`tracker` entra pigro\"): scandire
    il testo intero renderebbe il tripwire rumoroso — e un tripwire rumoroso
    viene disattivato. Qui restano solo le righe di codice eseguibile.
    """
    import ast
    doc_lines: set = set()
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
    return "\n".join(line for index, line in enumerate(source.splitlines(),
                                                      start=1)
                     if index not in doc_lines)


def _snapshot(db_path) -> list:
    """Fotografia delle righe del ledger (per provare che non si scrive)."""
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT match_id, esito_finale, profit, quota, status, league "
        "FROM predictions ORDER BY match_id").fetchall()
    conn.close()
    return rows


class TestTripwire:
    def test_nessuna_scrittura_sul_ledger(self):
        code = _code_only()
        for bad in ("INSERT", "UPDATE", "DELETE", "save_prediction",
                    "save_bet", "commit()"):
            assert bad not in code, f"league_dynamic scrive sul ledger: {bad}"

    def test_nessuna_rete_o_ordine(self):
        code = _code_only()
        for bad in ("requests", "httpx", "aiohttp", "odds_api", "sx_signals",
                    "place_order", "_live_fill", "execution_engine",
                    "resolve_market_for", "fetch_scores"):
            assert bad not in code, f"league_dynamic tocca rete/ordini: {bad}"

    def test_nessuna_formula_di_strategia_copiata(self):
        """Le soglie si LEGGONO da value_filter: mai una copia."""
        code = _code_only()
        for bad in ("ODDS_MIN =", "ODDS_MAX =", "EV_MIN =",
                    "MARKET_EDGE_MIN =", "PLAYABLE_TIERS ="):
            assert bad not in code, f"soglia di strategia duplicata: {bad}"

    def test_la_misura_non_modifica_il_ledger(self, temp_db, monkeypatch):
        """Il modulo MISURA: nessuna riga del ledger deve cambiare."""
        _seed(temp_db, _mix(lost=30, league="Bundesliga", prefix="ro"))
        _seed(temp_db, _mix(won=20, lost=10, league="Eredivisie", prefix="rw"))
        before = _snapshot(temp_db)
        monkeypatch.setenv("LEAGUE_DYNAMIC_ENABLED", "1")
        ld.reset_cache()
        ld.league_stats()
        ld.report()
        ld.restricted("Bundesliga")
        ld.effective_allowed("Bundesliga")
        assert _snapshot(temp_db) == before

    def test_import_leggero(self):
        """Importare il modulo non deve caricare la produzione."""
        script = (
            "import sys, league_dynamic;"
            "bad=[m for m in ('tracker','auto_bet','bot','odds_api',"
            "'multi_market') if m in sys.modules];"
            "print(','.join(bad))"
        )
        out = subprocess.run([sys.executable, "-c", script],
                             capture_output=True, text=True, timeout=120)
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == "", f"import pesante: {out.stdout}"

    def test_env_dichiarate_nella_iac(self):
        iac = Path(".railway/railway.ts").read_text(encoding="utf-8")
        for name in ("LEAGUE_DYNAMIC_ENABLED", "LEAGUE_DYNAMIC_SINCE",
                     "LEAGUE_DYNAMIC_MIN_SAMPLES", "LEAGUE_DYNAMIC_DEMOTE_ROI",
                     "LEAGUE_DYNAMIC_PROMOTE_ROI", "LEAGUE_DYNAMIC_TTL"):
            assert name in iac, f"{name} non dichiarata in preserve()"

    def test_ogni_env_letta_e_dichiarata(self):
        """Ogni env letta dal modulo deve stare in IaC: senza, un
        `config apply` distrugge cio' che l'operatore ha impostato."""
        import re
        names = set(re.findall(r'_flag\("([A-Z_]+)"|_num\("([A-Z_]+)"|'
                               r'_int\("([A-Z_]+)"|getenv\("([A-Z_]+)"',
                               _code_only()))
        flat = {n for group in names for n in group if n}
        assert flat, "nessuna env trovata: il tripwire non prova nulla"
        iac = Path(".railway/railway.ts").read_text(encoding="utf-8")
        for name in sorted(flat):
            assert name in iac, f"{name} letta dal modulo ma assente dalla IaC"
