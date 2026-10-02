"""test_steam_move.py — Steam move sullo sharp (Pinnacle) e priorita' d'esecuzione.

Tutti i test sono OFFLINE: DB SQLite temporaneo, cache finte in `tmp_path`,
**zero rete, zero crediti, zero ordini**. Il tempo e' INIETTATO (snapshot con
`recorded_at` esplicito), quindi nessun test puo' scadere col calendario.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import tracker
import steam_move as sm


# ---------------------------------------------------------------------------
# Fixture: DB temporaneo + isolamento della configurazione
# ---------------------------------------------------------------------------

@pytest.fixture()
def temp_db(monkeypatch, tmp_path):
    db = tmp_path / "test.db"
    monkeypatch.setattr(tracker, "DB_PATH", db)
    tracker.init_db()
    return db


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Nessuna env di steam move presente: si misura il DEFAULT di codice."""
    for name in ("STEAM_MOVE_ENABLED", "STEAM_MOVE_PCT", "STEAM_MOVE_WINDOW_MIN",
                 "STEAM_MOVE_MIN_WINDOW_MIN", "STEAM_MOVE_BOOK",
                 "STEAM_MOVE_DEDUP_MIN"):
        monkeypatch.delenv(name, raising=False)


def _iso(minutes_ago: float) -> str:
    return (datetime.now() - timedelta(minutes=minutes_ago)).isoformat()


def _count(db_path, match_id=None) -> int:
    """Righe in `price_snapshots` (0 se la tabella non e' ancora nata).

    La tabella e' creata in modo idempotente alla PRIMA scrittura: un DB dove
    nessuno ha mai registrato uno snapshot e' legittimamente senza tabella.
    """
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    try:
        if match_id is None:
            return conn.execute("SELECT COUNT(*) FROM price_snapshots").fetchone()[0]
        return conn.execute("SELECT COUNT(*) FROM price_snapshots WHERE match_id=?",
                            (match_id,)).fetchone()[0]
    except sqlite3.OperationalError:            # tabella non ancora creata
        return 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. Configurazione (default, env, valori impossibili)
# ---------------------------------------------------------------------------

class TestConfigurazione:
    def test_default_di_direttiva(self):
        cfg = sm.config()
        assert cfg["enabled"] is True
        assert cfg["move_pct"] == pytest.approx(0.04)     # 4%
        assert cfg["window_min"] == pytest.approx(30.0)   # 15-30'
        assert cfg["min_window_min"] == pytest.approx(15.0)
        assert cfg["book"] == "pinnacle"
        assert cfg["dedup_min"] == pytest.approx(5.0)

    def test_env_sovrascrive_i_default(self, monkeypatch):
        monkeypatch.setenv("STEAM_MOVE_PCT", "0.07")
        monkeypatch.setenv("STEAM_MOVE_WINDOW_MIN", "20")
        monkeypatch.setenv("STEAM_MOVE_MIN_WINDOW_MIN", "0")
        monkeypatch.setenv("STEAM_MOVE_BOOK", "Betfair")
        assert sm.move_pct() == pytest.approx(0.07)
        assert sm.window_min() == pytest.approx(20.0)
        assert sm.min_window_min() == pytest.approx(0.0)
        assert sm.book_name() == "betfair"        # normalizzato

    def test_valore_non_numerico_ricade_sul_default(self, monkeypatch):
        monkeypatch.setenv("STEAM_MOVE_PCT", "quattro per cento")
        assert sm.move_pct() == pytest.approx(0.04)

    def test_valore_negativo_viene_rialzato_al_minimo(self, monkeypatch):
        monkeypatch.setenv("STEAM_MOVE_WINDOW_MIN", "-5")
        assert sm.window_min() == pytest.approx(1.0)

    def test_interruttore(self, monkeypatch):
        monkeypatch.setenv("STEAM_MOVE_ENABLED", "0")
        assert sm.enabled() is False
        monkeypatch.setenv("STEAM_MOVE_ENABLED", "yes")
        assert sm.enabled() is True


# ---------------------------------------------------------------------------
# 2. Forma del mercato sharp
# ---------------------------------------------------------------------------

class TestFormaMercato:
    def test_1x2_tre_esiti(self):
        assert sm.outcomes_for_market("1X2") == ("1", "X", "2")
        assert sm.outcomes_for_market("1x2") == ("1", "X", "2")

    def test_testa_a_testa_due_esiti(self):
        assert sm.outcomes_for_market("TENNIS") == ("1", "2")
        assert sm.outcomes_for_market("ML") == ("1", "2")

    def test_mercati_a_linea_non_coperti(self):
        """OU/AH hanno una LINEA: nessun marcatore invece di uno sbagliato."""
        assert sm.outcomes_for_market("OU") is None
        assert sm.outcomes_for_market("AH") is None
        assert sm.outcomes_for_market("") is None
        assert sm.outcomes_for_market(None) is None


# ---------------------------------------------------------------------------
# 3. Storico dei prezzi sharp + dedup
# ---------------------------------------------------------------------------

class TestSnapshot:
    def test_registra_con_la_fonte(self, temp_db):
        assert sm.record_sharp_snapshot("m1", "1", 2.10) is True
        assert sm.record_sharp_snapshot("m1", "X", 3.40) is True
        assert _count(temp_db) == 2
        import sqlite3
        conn = sqlite3.connect(str(temp_db))
        bks = {r[0] for r in conn.execute(
            "SELECT DISTINCT bookmaker FROM price_snapshots").fetchall()}
        conn.close()
        assert bks == {"pinnacle"}

    def test_prezzo_identico_ravvicinato_non_riscrive(self, temp_db):
        assert sm.record_sharp_snapshot("m1", "1", 2.10) is True
        assert sm.record_sharp_snapshot("m1", "1", 2.10) is False   # dedup
        assert _count(temp_db) == 1

    def test_prezzo_diverso_si_registra_sempre(self, temp_db):
        assert sm.record_sharp_snapshot("m1", "1", 2.10) is True
        assert sm.record_sharp_snapshot("m1", "1", 2.00) is True    # movimento
        assert _count(temp_db) == 2

    def test_dedup_scaduto_riscrive(self, temp_db, monkeypatch):
        monkeypatch.setenv("STEAM_MOVE_DEDUP_MIN", "0")
        assert sm.record_sharp_snapshot("m1", "1", 2.10) is True
        assert sm.record_sharp_snapshot("m1", "1", 2.10) is True
        assert _count(temp_db) == 2

    def test_prezzo_degenere_o_dati_mancanti(self, temp_db):
        assert sm.record_sharp_snapshot("m1", "1", 1.0) is False
        assert sm.record_sharp_snapshot("m1", "1", 0.5) is False
        assert sm.record_sharp_snapshot("m1", "1", None) is False
        assert sm.record_sharp_snapshot("", "1", 2.0) is False
        assert sm.record_sharp_snapshot("m1", "", 2.0) is False
        assert _count(temp_db) == 0

    def test_errore_di_lettura_non_propaga(self, temp_db, monkeypatch):
        def boom(*a, **kw):
            raise RuntimeError("db rotto")
        monkeypatch.setattr(sm, "_last_snapshot", boom)
        assert sm.record_sharp_snapshot("m1", "1", 2.0) is False


# ---------------------------------------------------------------------------
# 4. ΔQ/Δt
# ---------------------------------------------------------------------------

class TestDeltaQdt:
    def _seed(self, prices_at):
        for price, minutes_ago in prices_at:
            sm.record_sharp_snapshot("m1", "1", price, recorded_at=_iso(minutes_ago))

    def test_movimento_misurato(self, temp_db):
        self._seed([(2.00, 20), (1.90, 5)])
        d = sm.delta_q_dt("m1", "1")
        assert d is not None
        assert d["move_pct"] == pytest.approx(-5.0, abs=0.01)
        assert d["span_minutes"] == pytest.approx(15.0, abs=0.6)
        assert d["direction"] == "down"
        assert d["first_price"] == pytest.approx(2.00)
        assert d["last_price"] == pytest.approx(1.90)

    def test_rialzo_registrato_come_up(self, temp_db):
        self._seed([(2.00, 20), (2.20, 5)])
        d = sm.delta_q_dt("m1", "1")
        assert d["move_pct"] == pytest.approx(+10.0, abs=0.01)
        assert d["direction"] == "up"

    def test_un_solo_snapshot_non_e_un_movimento(self, temp_db):
        self._seed([(2.00, 5)])
        assert sm.delta_q_dt("m1", "1") is None

    def test_span_troppo_corto_non_e_un_movimento(self, temp_db):
        self._seed([(2.00, 20), (1.80, 19)])       # 1 minuto: rumore
        assert sm.delta_q_dt("m1", "1") is None

    def test_span_pavimento_disattivabile(self, temp_db):
        self._seed([(2.00, 20), (1.80, 19)])
        d = sm.delta_q_dt("m1", "1", min_window_minutes=0)
        assert d is not None and d["move_pct"] == pytest.approx(-10.0, abs=0.01)

    def test_snapshot_fuori_finestra_esclusi(self, temp_db):
        # il primo e' vecchio oltre la finestra: resta solo l'ultimo -> nessun
        # movimento (non si confronta con la storia remota)
        self._seed([(2.80, 90), (1.90, 5)])
        assert sm.delta_q_dt("m1", "1") is None

    def test_le_altre_fonti_non_inquinano(self, temp_db):
        """`bookmaker` separa lo storico: il prezzo SX non e' lo sharp."""
        from line_movement import record_snapshot
        record_snapshot("m1", "1", 5.00, bookmaker="", recorded_at=_iso(20))
        self._seed([(2.00, 20), (1.90, 5)])
        d = sm.delta_q_dt("m1", "1")
        assert d["first_price"] == pytest.approx(2.00)   # non 5.00
        assert d["move_pct"] == pytest.approx(-5.0, abs=0.01)

    def test_esito_diverso_non_si_mescola(self, temp_db):
        self._seed([(2.00, 20), (1.90, 5)])
        assert sm.delta_q_dt("m1", "X") is None


# ---------------------------------------------------------------------------
# 5. Verdetto Steam Move
# ---------------------------------------------------------------------------

class TestDetect:
    def _seed(self, prices_at, esito="1"):
        for price, minutes_ago in prices_at:
            sm.record_sharp_snapshot("m1", esito, price,
                                     recorded_at=_iso(minutes_ago))

    def test_crollo_oltre_la_soglia_e_steam(self, temp_db):
        self._seed([(2.00, 20), (1.90, 5)])          # -5% > 4%
        v = sm.detect("m1", "1")
        assert v["steam_move"] is True
        assert v["priority"] is True
        assert v["reason"] == "steam_down"
        assert v["move_pct"] == pytest.approx(-5.0, abs=0.01)
        assert v["threshold_pct"] == pytest.approx(4.0)

    def test_crollo_sotto_la_soglia_non_scatta(self, temp_db):
        self._seed([(2.00, 20), (1.96, 5)])          # -2%
        v = sm.detect("m1", "1")
        assert v["steam_move"] is False and v["priority"] is False
        assert v["reason"] == "no_drop"

    def test_rialzo_non_scatta(self, temp_db):
        """Solo il CROLLO accende la priorita': un rialzo e' informazione."""
        self._seed([(2.00, 20), (2.20, 5)])
        v = sm.detect("m1", "1")
        assert v["steam_move"] is False
        assert v["direction"] == "up"

    def test_senza_dati(self, temp_db):
        v = sm.detect("m1", "1")
        assert v["steam_move"] is False and v["reason"] == "no_data"
        assert v["match_id"] == "m1" and v["esito"] == "1"

    def test_soglia_configurabile(self, temp_db, monkeypatch):
        self._seed([(2.00, 20), (1.96, 5)])          # -2%
        monkeypatch.setenv("STEAM_MOVE_PCT", "0.01")
        v = sm.detect("m1", "1")
        assert v["steam_move"] is True

    def test_lettura_rotta_non_propaga(self, temp_db, monkeypatch):
        def boom(*a, **kw):
            raise RuntimeError("db rotto")
        monkeypatch.setattr(sm, "delta_q_dt", boom)
        v = sm.detect("m1", "1")
        assert v["steam_move"] is False


# ---------------------------------------------------------------------------
# 6. Ponte con l'oracolo: quote Pinnacle dalle cache (0 crediti)
# ---------------------------------------------------------------------------

def _write_cache(folder: Path, prices=(1.75, 3.60, 4.50),
                 home="Atlanta United", away="Toronto FC", ts=None,
                 sport="soccer_usa_mls"):
    folder.mkdir(parents=True, exist_ok=True)
    payload = [{
        "id": "id-1", "sport_key": sport, "commence_time": "2026-10-03T19:00:00Z",
        "home_team": home, "away_team": away,
        "bookmakers": [{"key": "pinnacle", "title": "Pinnacle",
                        "markets": [{"key": "h2h", "outcomes": [
                            {"name": home, "price": prices[0]},
                            {"name": "Draw", "price": prices[1]},
                            {"name": away, "price": prices[2]}]}]}],
    }]
    (folder / f"toa_{sport}.json").write_text(
        json.dumps({"ts": time.time() if ts is None else ts, "payload": payload}),
        encoding="utf-8")


class TestPonteOracolo:
    def test_registra_le_quote_sharp_e_valuta(self, temp_db, tmp_path):
        _write_cache(tmp_path)
        info = sm.observe("Atlanta United", "Toronto FC", "m1", "1",
                          ("1", "X", "2"), cache_dir=tmp_path)
        # primo giro: nessuno storico precedente -> nessun movimento
        assert info["steam_move"] is False
        assert _count(temp_db, "m1") == 3          # i tre esiti dello sharp
        # secondo giro con lo STESSO prezzo: dedup, nessuna nuova riga
        sm.observe("Atlanta United", "Toronto FC", "m1", "1", ("1", "X", "2"),
                   cache_dir=tmp_path)
        assert _count(temp_db, "m1") == 3

    def test_senza_cache_fresca_niente_invenzioni(self, temp_db, tmp_path):
        info = sm.observe("Bologna", "Torino", "m2", "1", ("1", "X", "2"),
                          cache_dir=tmp_path)
        assert info["steam_move"] is False
        assert info["reason"] == "no_sharp_cache"
        assert _count(temp_db, "m2") == 0

    def test_cache_stantia_ignorata(self, temp_db, tmp_path):
        old = time.time() - 48 * 3600
        _write_cache(tmp_path, ts=old)
        info = sm.observe("Atlanta United", "Toronto FC", "m1", "1",
                          ("1", "X", "2"), cache_dir=tmp_path)
        assert info["reason"] == "no_sharp_cache"
        assert _count(temp_db, "m1") == 0

    def test_lettura_ostile_non_propaga(self, temp_db, tmp_path):
        (tmp_path / "toa_x.json").write_text("non-json", encoding="utf-8")
        info = sm.observe("Atlanta United", "Toronto FC", "m1", "1",
                          ("1", "X", "2"), cache_dir=tmp_path)
        assert info["steam_move"] is False


# ---------------------------------------------------------------------------
# 7. Marcatore sui candidati e ordinamento della coda
# ---------------------------------------------------------------------------

class TestMarcatoreEPriorita:
    def test_annotate_marca_e_conta(self, temp_db, tmp_path, monkeypatch):
        # due snapshot che producono uno steam: li inietto direttamente
        sm.record_sharp_snapshot("m1", "1", 2.00, recorded_at=_iso(20))
        sm.record_sharp_snapshot("m1", "1", 1.90, recorded_at=_iso(5))
        cands = [{"match_id": "m1", "esito_key": "1", "mercato": "1X2",
                  "home": "A", "away": "B"},
                 {"match_id": "m2", "esito_key": "Over 2.5", "mercato": "OU",
                  "home": "C", "away": "D"}]
        n = sm.annotate(cands, cache_dir=tmp_path)
        assert n == 1
        assert cands[0]["steam_move"] is True
        assert cands[0]["steam_move_info"]["reason"] == "steam_down"
        assert cands[1]["steam_move"] is False
        assert cands[1]["steam_move_info"]["reason"] == "unsupported_market"

    def test_annotate_disabilitato_non_tocca_nulla(self, temp_db, monkeypatch):
        monkeypatch.setenv("STEAM_MOVE_ENABLED", "0")
        cands = [{"match_id": "m1", "esito_key": "1", "mercato": "1X2"}]
        assert sm.annotate(cands) == 0
        assert "steam_move" not in cands[0]

    def test_annotate_fail_safe_su_candidato_ostile(self, temp_db):
        cands = [{}, {"match_id": "m1"}]           # chiavi mancanti
        assert sm.annotate(cands) == 0             # nessuna eccezione
        assert all(c["steam_move"] is False for c in cands)

    def test_sort_mette_gli_steam_per_primi(self):
        cands = [{"match_id": "a", "steam_move": False, "best_ev": 0.9},
                 {"match_id": "b", "steam_move": True, "best_ev": 0.1},
                 {"match_id": "c", "steam_move": False, "best_ev": 0.5},
                 {"match_id": "d", "steam_move": True, "best_ev": 0.2}]
        out = sm.sort_for_execution(cands)
        assert [c["match_id"] for c in out] == ["b", "d", "a", "c"]

    def test_sort_e_stabile_dentro_i_gruppi(self):
        cands = [{"match_id": "a", "steam_move": False},
                 {"match_id": "b", "steam_move": False},
                 {"match_id": "c", "steam_move": True}]
        assert [c["match_id"] for c in sm.sort_for_execution(cands)] == \
            ["c", "a", "b"]

    def test_sort_su_lista_vuota(self):
        assert sm.sort_for_execution([]) == []
        assert sm.sort_for_execution(None) == []


# ---------------------------------------------------------------------------
# 8. Report e CLI
# ---------------------------------------------------------------------------

class TestReportCli:
    def test_report_steam(self):
        txt = sm.format_report({"match_id": "m1", "esito": "1",
                                "steam_move": True, "book": "pinnacle",
                                "first_price": 2.0, "last_price": 1.9,
                                "move_pct": -5.0, "span_minutes": 15})
        assert "STEAM MOVE" in txt and "-5.00%" in txt

    def test_report_nessuno_steam(self):
        txt = sm.format_report({"match_id": "m1", "esito": "1",
                                "steam_move": False, "reason": "no_drop",
                                "move_pct": -1.2, "span_minutes": 20})
        assert "nessuno" in txt and "no_drop" in txt

    def test_cli_esce_zero(self, temp_db, capsys):
        assert sm.main(["--match", "m1", "--esito", "1"]) == 0
        assert "steam_move" in capsys.readouterr().out

    def test_cli_json(self, temp_db, capsys):
        assert sm.main(["--match", "m1", "--esito", "1", "--json"]) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["config"]["book"] == "pinnacle"
        assert data["detection"]["match_id"] == "m1"


# ---------------------------------------------------------------------------
# 9. Tripwire
# ---------------------------------------------------------------------------

SOURCE = Path(sm.__file__).read_text(encoding="utf-8")

IAC = Path(".railway/railway.ts")


class TestTripwire:
    def test_nessun_ordine_e_nessun_executor(self):
        """Il modulo misura e marca: NON decide l'ordine e NON parla con SX."""
        for banned in ("_live_fill", "place_order", "resolve_market_for",
                       "execution_engine", "auto_bet", "place_limit_order",
                       "OrderResult"):
            assert banned not in SOURCE, f"steam_move tocca l'esecuzione: {banned}"

    def test_nessuna_scrittura_sul_ledger(self):
        for banned in ("save_prediction", "save_bet", "save_market_quotes",
                       "save_analysis"):
            assert banned not in SOURCE, f"steam_move scrive: {banned}"

    def test_nessuna_formula_devig_copiata(self):
        for banned in ("market_implied", "def devig", "shin_fair"):
            assert banned not in SOURCE, f"steam_move copia una formula: {banned}"

    def test_delega_lo_storico_a_line_movement(self):
        assert "from line_movement import" in SOURCE
        assert "INSERT INTO" not in SOURCE

    def test_nessuna_rete_all_import(self):
        top = [ln for ln in SOURCE.splitlines()
               if re.match(r"^(import|from)\s+(requests|httpx|aiohttp)", ln)]
        assert top == []

    def test_importare_il_modulo_non_carica_la_produzione(self):
        code = ("import sys, steam_move;"
                "print(sorted(m for m in ('tracker','auto_bet','bot','decision',"
                "'pinnacle_oracle','line_movement') if m in sys.modules))")
        out = subprocess.run([sys.executable, "-c", code],
                             cwd=str(Path(sm.__file__).parent),
                             capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == "[]", out.stdout

    def test_env_dichiarate_nella_iac(self):
        """`preserve()`: un `config apply` non deve distruggere gli override."""
        src = IAC.read_text(encoding="utf-8")
        for env in ("STEAM_MOVE_ENABLED", "STEAM_MOVE_PCT",
                    "STEAM_MOVE_WINDOW_MIN", "STEAM_MOVE_MIN_WINDOW_MIN",
                    "STEAM_MOVE_BOOK", "STEAM_MOVE_DEDUP_MIN"):
            assert f"{env}: preserve()" in src, f"{env} non e' nella IaC"

    def test_de_vig_dell_oracolo_dichiarato_nella_iac(self):
        src = IAC.read_text(encoding="utf-8")
        assert "PINNACLE_DEVIG_METHOD: preserve()" in src


# ---------------------------------------------------------------------------
# 10. Wiring nel giro ordini
# ---------------------------------------------------------------------------

class TestWiringAutoBet:
    def test_auto_bet_importa_il_modulo(self):
        import auto_bet
        assert auto_bet.steam_move is not None

    def test_il_giro_chiama_annotate_e_riordina(self):
        src = Path("auto_bet.py").read_text(encoding="utf-8")
        assert "steam_move.annotate(candidates)" in src
        assert "steam_move.sort_for_execution(candidates)" in src

    def test_il_wiring_e_fail_safe(self):
        """Un errore della telemetria non deve poter fermare il giro puntate."""
        src = Path("auto_bet.py").read_text(encoding="utf-8")
        marker = src.index("steam_move.annotate(candidates)")
        window = src[max(0, marker - 400):marker + 1600]
        assert "except Exception" in window
