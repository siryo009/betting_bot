"""Test `credit_diagnose` + telemetria delle chiamate in `odds_api`.

Tutti OFFLINE: nessuna rete (le chiamate sono REGISTRATE a mano), nessun
credito, nessun ordine. Il log di telemetria va in una cartella temporanea.
"""
import json
import time
from pathlib import Path

import pytest

import credit_diagnose as cd
import odds_api as oa


@pytest.fixture()
def log_path(tmp_path, monkeypatch):
    p = tmp_path / "credit_calls.jsonl"
    monkeypatch.setenv("CREDIT_CALLS_LOG", str(p))
    return p


def _seed(log_path: Path, rows):
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


# ------------------------------------------------------- crediti per mercato

def test_crediti_per_mercato():
    """the-odds-api fattura `markets x regions` (eu = 1 regione)."""
    assert oa.credits_for_markets("h2h") == 1
    assert oa.credits_for_markets("h2h,totals,spreads") == 3
    assert oa.credits_for_markets("") == 1        # fail-safe
    assert oa.credits_for_markets(None) == 1


def test_record_credit_call_scrive_i_campi(log_path):
    evt = oa.record_credit_call("oracle", "soccer_italy_serie_a",
                                "h2h,totals,spreads", 320,
                                endpoint="/odds")
    assert evt["source"] == "oracle" and evt["credits"] == 3
    assert evt["remaining"] == 320 and evt["status"] == 200
    row = json.loads(log_path.read_text().splitlines()[0])
    assert row["sport"] == "soccer_italy_serie_a"
    assert row["ts_epoch"] > 0


def test_record_credit_call_non_propaga_errori(tmp_path, monkeypatch):
    """Una scrittura impossibile non deve MAI fermare una chiamata."""
    monkeypatch.setenv("CREDIT_CALLS_LOG",
                       str(tmp_path / "nope" / "x.jsonl"))
    evt = oa.record_credit_call("rotation", "x", "h2h", 10)
    assert evt["source"] == "rotation"            # ritorna l'evento, niente raise


# -------------------------------------------------------------- attribuzione

def test_breakdown_per_sorgente(log_path):
    _seed(log_path, [
        {"source": "rotation", "markets": "h2h", "credits": 1,
         "ts_epoch": time.time()},
        {"source": "rotation", "markets": "h2h", "credits": 1,
         "ts_epoch": time.time()},
        {"source": "oracle", "markets": "h2h,totals,spreads", "credits": 3,
         "ts_epoch": time.time()},
        {"source": "settlement", "markets": "scores", "credits": 1,
         "status": 429, "ts_epoch": time.time()},
    ])
    b = cd.breakdown(days=1)
    assert b["total_calls"] == 4
    assert b["total_credits"] == 6.0
    assert b["by_source"]["oracle"]["credits"] == 3.0
    assert b["by_source"]["rotation"]["calls"] == 2
    assert b["by_source"]["settlement"]["errors"] == 1   # status >= 400


def test_breakdown_senza_telemetria(log_path):
    b = cd.breakdown(days=1)
    assert b["total_calls"] == 0 and b["by_source"] == {}


def test_breakdown_ignora_eventi_fuori_finestra(log_path):
    _seed(log_path, [
        {"source": "rotation", "credits": 1, "ts_epoch": time.time()},
        {"source": "rotation", "credits": 1,
         "ts_epoch": time.time() - 20 * 86400},
    ])
    assert cd.breakdown(days=2)["total_calls"] == 1


def test_breakdown_by_market(log_path):
    _seed(log_path, [
        {"source": "rotation", "markets": "h2h", "ts_epoch": time.time()},
        {"source": "oracle", "markets": "h2h,totals,spreads",
         "ts_epoch": time.time()},
    ])
    assert cd.breakdown(days=1)["by_market"]["h2h"] == 1


# --------------------------------------------------------------- inventario

def test_inventario_classifica_le_cache(tmp_path):
    for name, ts in (("toa_soccer_italy_serie_a.json", time.time()),
                     ("toao_soccer_spain_la_liga.json", time.time() - 60),
                     ("toa_scores_soccer_italy_serie_a.json", time.time())):
        (tmp_path / name).write_text(json.dumps({"ts": ts}))
    inv = cd.inventory(directory=tmp_path)
    assert inv["rotation"]["files"] == 1
    assert inv["oracle"]["files"] == 1
    assert inv["settlement"]["files"] == 1
    assert inv["oracle"]["avg_age_h"] is not None


def test_inventario_cartella_assente(tmp_path):
    inv = cd.inventory(directory=tmp_path / "nope")
    assert inv["rotation"]["files"] == 0
    assert "error" not in inv


# ------------------------------------------------------------------ diagnose

def test_diagnose_struttura(log_path):
    _seed(log_path, [{"source": "oracle", "credits": 3,
                      "ts_epoch": time.time()}])
    d = cd.diagnose(days=1)
    assert "budget" in d and "calls" in d and "inventory" in d
    assert d["telemetry_empty"] is False
    assert d["credits_per_day_by_source"]["oracle"] == 3.0


def test_diagnose_senza_telemetria(log_path):
    d = cd.diagnose(days=1)
    assert d["telemetry_empty"] is True


def test_format_report_mostra_le_sorgenti(log_path):
    _seed(log_path, [{"source": "oracle", "credits": 3,
                      "ts_epoch": time.time()}])
    txt = cd.format_report(cd.diagnose(days=1))
    assert "DIAGNOSI CREDITI" in txt and "oracle" in txt
    empty = {"calls": {"total_calls": 0}, "telemetry_empty": True,
             "budget": {}, "inventory": {}}
    assert "NESSUNA telemetria" in cd.format_report(empty)


# ------------------------------------------------------------------ tripwire

def test_modulo_non_fa_rete_in_testa():
    """Diagnostica PURA: nessun import di rete a livello di MODULO.

    `odds_api` e' importato DENTRO `diagnose` (pigro), cosi' il modulo si puo'
    leggere e testare senza toccare la rete.
    """
    head = Path(cd.__file__).read_text().split("def ")[0]
    for bad in ("import requests", "import httpx", "import odds_api"):
        assert bad not in head, f"import di rete in testa al modulo: {bad}"


def test_sorgente_non_scrive_nel_ledger():
    src = (Path(cd.__file__).parent / "credit_diagnose.py").read_text()
    for bad in ("INSERT", "UPDATE ", "DELETE", "save_prediction", "save_bet"):
        assert bad not in src, f"credit_diagnose non deve scrivere ({bad})"


def test_contatore_crediti_include_la_cache_oracolo(tmp_path, monkeypatch):
    """Tripwire (03/10/2026): `toa_*.json` NON cattura `toao_*.json`.

    Senza `_credit_cache_files` le chiamate dell'oracolo (3 crediti l'una)
    restavano invisibili a `get_remaining`/`credit_burn_rate`: la telemetria
    sottostimava il consumo proprio sulla sorgente piu' costosa.
    """
    monkeypatch.setattr(oa, "CACHE_DIR", tmp_path)
    (tmp_path / "toa_soccer_italy_serie_a.json").write_text(json.dumps(
        {"remaining": 100, "ts": time.time() - 3600,
         "remaining_ts": time.time() - 3600}))
    (tmp_path / "toao_soccer_spain_la_liga.json").write_text(json.dumps(
        {"remaining": 97, "ts": time.time(), "remaining_ts": time.time()}))
    names = [f.name for f in oa._credit_cache_files()]
    assert "toao_soccer_spain_la_liga.json" in names
    assert oa.get_remaining() == 97      # la lettura piu' recente e' l'oracolo


def test_env_dichiarate_in_iac():
    iac = (Path(__file__).parent / ".railway" / "railway.ts").read_text()
    for name in ("CREDIT_CALLS_LOG", "ORACLE_SKIP_LOG",
                 "TELEMETRY_ROTATE_ENABLED", "LOG_ROTATE_MAX_MB",
                 "LOG_ROTATE_AFTER_DAYS", "LOG_ROTATE_QUIET_MIN",
                 "LOG_ROTATE_KEEP"):
        assert name in iac, f"{name} non dichiarata in preserve()"
