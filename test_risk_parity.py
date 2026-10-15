"""Test di `risk_parity.py` (M6 Fase 3) — TUTTI OFFLINE.

Nessuna rete, nessun credito, nessun ordine: ledger SQLite temporaneo (schema
`bets` di produzione) e covarianze costruite a mano con riferimento analitico
(su una diagonale l'ERC ha soluzione nota: `w_i ∝ 1/sqrt(v_i)`).

Tripwire: il modulo e' di SOLA LETTURA (la connessione `mode=ro` deve RIFIUTARE
una UPDATE), non conosce il percorso ordini e non importa la produzione
all'import.
"""
from __future__ import annotations

import ast
import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

import risk_parity as rp

ROOT = Path(__file__).resolve().parent


def _code_only(path) -> str:
    """Solo il CODICE del modulo (le docstring nominano i confini: la prosa non e'
    un tripwire — stessa lezione del 30/09 sui tripwire che bocciavano i commenti)."""
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and \
                    isinstance(body[0].value, ast.Constant) and \
                    isinstance(body[0].value.value, str):
                node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------
def _temp_db(tmp_path, rows):
    """Ledger con la sola tabella `bets` (le colonne che il modulo legge)."""
    path = str(tmp_path / "rp.db")
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE bets (id INTEGER PRIMARY KEY, match_id TEXT, mercato TEXT,
                           esito TEXT, stake REAL, profit REAL,
                           esito_finale TEXT, settled_at TEXT, mode TEXT);
        """
    )
    for i, row in enumerate(rows):
        conn.execute(
            "INSERT INTO bets (match_id, mercato, esito, stake, profit, "
            "esito_finale, settled_at, mode) VALUES (?,?,?,?,?,?,?,?)",
            (row.get("match_id", "sx-%d" % i), row.get("mercato", "1X2"),
             row.get("esito", "1"), row.get("stake", 1.0),
             row.get("profit", 0.0), row.get("esito_finale", "won"),
             row.get("settled_at", _iso_days_ago(1)),
             row.get("mode", "live")))
    conn.commit()
    conn.close()
    return path


def _iso_days_ago(days, *, hour=12):
    d = datetime.now(timezone.utc) - timedelta(days=days)
    return d.replace(hour=hour, minute=0, second=0, microsecond=0).isoformat()


def _bucket_rows(match_id, mercato, returns, *, day_step=1, start_day=1):
    """Righe chiuse con rendimenti dati (stake 1 -> profit = rendimento)."""
    return [{"match_id": match_id, "mercato": mercato, "stake": 1.0,
             "profit": float(r), "settled_at": _iso_days_ago(start_day + i * day_step)}
            for i, r in enumerate(returns)]


def _cand(match_id="sx-1", mercato="1X2", stake=5.0, esito="1"):
    return {"match_id": match_id, "mercato": mercato, "esito_key": esito,
            "stake": stake, "quota": 1.7, "league": "Premier League"}


def _use_db(monkeypatch, path):
    """Punta il modulo su un DB GIA' creato (una sola costruzione: `_db_path()`
    viene chiamato piu' volte per istantanea, ricrearlo ogni volta fallirebbe)."""
    monkeypatch.setattr(rp, "_db_path", lambda: str(path))
    return str(path)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Nessun file reale: DB e memo del modulo isolati."""
    rp.reset_cache()
    monkeypatch.setattr(rp, "_db_path",
                        lambda: str(tmp_path / "db-mai-creato.db"))
    yield
    rp.reset_cache()


# ---------------------------------------------------------------------------
# Configurazione
# ---------------------------------------------------------------------------
class TestConfig:
    def test_default_di_codice(self, monkeypatch):
        for name in ("RISK_PARITY_GUARD", "RISK_PARITY_MODE",
                     "RISK_PARITY_MAX_SHARE_RATIO", "RISK_PARITY_MIN_MULTIPLIER",
                     "RISK_PARITY_MIN_SAMPLE", "RISK_PARITY_DAYS",
                     "RISK_PARITY_MIN_DAYS", "RISK_PARITY_GRID",
                     "RISK_PARITY_TTL_S"):
            monkeypatch.delenv(name, raising=False)
        conf = rp.config()
        assert conf["enabled"] is True and conf["active"] is True
        assert conf["mode"] == "enforce"
        assert conf["max_share_ratio"] == 1.25
        assert conf["min_multiplier"] == 0.25
        assert conf["min_sample"] == 8

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("RISK_PARITY_MAX_SHARE_RATIO", "1.5")
        monkeypatch.setenv("RISK_PARITY_MIN_MULTIPLIER", "0.40")
        monkeypatch.setenv("RISK_PARITY_MIN_SAMPLE", "4")
        conf = rp.config()
        assert conf["max_share_ratio"] == 1.5
        assert conf["min_multiplier"] == 0.40
        assert conf["min_sample"] == 4

    def test_valori_impossibili_ricadono_sul_default(self, monkeypatch):
        monkeypatch.setenv("RISK_PARITY_MAX_SHARE_RATIO", "abc")
        monkeypatch.setenv("RISK_PARITY_MIN_MULTIPLIER", "3")   # > 1
        monkeypatch.setenv("RISK_PARITY_MIN_SAMPLE", "-5")
        conf = rp.config()
        assert conf["max_share_ratio"] == 1.25
        assert conf["min_multiplier"] == 0.25
        assert conf["min_sample"] == 8

    def test_modo_sconosciuto_e_interruttore(self, monkeypatch):
        monkeypatch.setenv("RISK_PARITY_MODE", "boh")
        assert rp.config()["mode"] == "enforce"
        monkeypatch.setenv("RISK_PARITY_MODE", "off")
        assert rp.config()["active"] is False
        monkeypatch.setenv("RISK_PARITY_MODE", "enforce")
        monkeypatch.setenv("RISK_PARITY_GUARD", "0")
        assert rp.config()["active"] is False


# ---------------------------------------------------------------------------
# Bucket e varianza
# ---------------------------------------------------------------------------
class TestBucket:
    def test_lane_dal_prefisso(self):
        assert rp.lane_of("sx-tennis-0xabc") == "tennis"
        assert rp.lane_of("sx-esports-123") == "esports"
        assert rp.lane_of("sx-L20175865") == "sxbet"
        assert rp.lane_of("6185eb4f") == "other"
        assert rp.lane_of(None) == "other"

    def test_bucket_mai_indovinato(self):
        assert rp.bucket_of("1X2", "sx-1") == "1X2|sxbet"
        assert rp.bucket_of("TENNIS", "sx-tennis-1") == "TENNIS|tennis"
        assert rp.bucket_of(None, "") == "?|other"

    def test_unit_return(self):
        assert rp.unit_return(1.5, 1.0) == 1.5
        assert rp.unit_return(-1.0, 2.0) == -0.5
        assert rp.unit_return(None, 1.0) is None
        assert rp.unit_return(1.0, 0) is None
        assert rp.unit_return("abc", 1.0) is None

    def test_varianza_campionaria(self, tmp_path, monkeypatch):
        _use_db(monkeypatch, _temp_db(tmp_path, _bucket_rows(
            "sx-1", "1X2", [1.0, -1.0, 1.0, -1.0])))
        snap = rp.variance_snapshot(use_cache=False)
        stats = snap["stats"]["1X2|sxbet"]
        assert stats["n"] == 4
        # ddof=1 su [1,-1,1,-1]: varianza 4/3
        assert stats["variance"] == pytest.approx(4.0 / 3.0)

    def test_bucket_sotto_campione_non_stimato(self, tmp_path, monkeypatch):
        _use_db(monkeypatch, _temp_db(tmp_path, _bucket_rows(
            "sx-1", "1X2", [1.0, -1.0])))
        got = rp.variance_for("1X2|sxbet")
        assert got["ok"] is False and got["reason"] == "campione_insufficiente"
        assert got["n"] == 2

    def test_varianza_nulla_dichiarata(self, tmp_path, monkeypatch):
        _use_db(monkeypatch, _temp_db(tmp_path, _bucket_rows(
            "sx-1", "1X2", [1.0] * 10)))
        got = rp.variance_for("1X2|sxbet")
        assert got["ok"] is False and got["reason"] == "varianza_nulla"

    def test_bucket_sconosciuto(self, tmp_path, monkeypatch):
        _use_db(monkeypatch, _temp_db(tmp_path, _bucket_rows(
            "sx-1", "1X2", [1.0, -1.0] * 6)))
        assert rp.variance_for("OU|other")["reason"] == "bucket_sconosciuto"


# ---------------------------------------------------------------------------
# Covarianza
# ---------------------------------------------------------------------------
class TestCovarianza:
    def test_diagonale_e_coppie_giorno(self, tmp_path, monkeypatch):
        rows = (_bucket_rows("sx-1", "1X2", [1.0, -1.0, 1.0, -1.0, 1.0, -1.0,
                                             1.0, -1.0, 1.0, -1.0])
                + _bucket_rows("sx-2", "OU", [-1.0, 1.0, -1.0, 1.0, -1.0, 1.0,
                                              -1.0, 1.0, -1.0, 1.0]))
        _use_db(monkeypatch, _temp_db(tmp_path, rows))
        snap = rp.variance_snapshot(use_cache=False)
        assert set(snap["buckets"]) == {"1X2|sxbet", "OU|sxbet"}
        cov = np.asarray(snap["matrix"], dtype=float)
        assert cov.shape == (2, 2)
        pair = snap["pairs"]["1X2|sxbet~OU|sxbet"]
        assert pair["n_days"] >= 2 and pair["cov"] is not None

    def test_pochi_giorni_in_comune_non_stimato(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RISK_PARITY_MIN_DAYS", "20")
        rows = (_bucket_rows("sx-1", "1X2", [1.0, -1.0] * 5)
                + _bucket_rows("sx-2", "OU", [-1.0, 1.0] * 5))
        _use_db(monkeypatch, _temp_db(tmp_path, rows))
        snap = rp.variance_snapshot(use_cache=False)
        pair = snap["pairs"]["1X2|sxbet~OU|sxbet"]
        assert pair["cov"] is None
        assert pair["reason"] == "giorni_insufficienti"

    def test_ledger_assente_dichiara_errore(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rp, "_db_path",
                            lambda: str(tmp_path / "inesistente.db"))
        snap = rp.variance_snapshot(use_cache=False)
        assert snap["error"] == "lettura_fallita"

    def test_memo_si_azzera_col_reset(self, tmp_path, monkeypatch):
        _use_db(monkeypatch, _temp_db(tmp_path, _bucket_rows(
            "sx-1", "1X2", [1.0, -1.0] * 6)))
        first = rp.variance_snapshot()
        assert rp.variance_snapshot() == first          # dalla memo
        rp.reset_cache()
        assert rp._MEMO["data"] is None


# ---------------------------------------------------------------------------
# ERC / risk parity
# ---------------------------------------------------------------------------
class TestRiskParity:
    def test_contributi_su_covarianza_identita(self):
        rc = rp.risk_contributions([0.5, 0.5], np.eye(2))
        assert rc["ok"] is True
        assert rc["shares"] == pytest.approx([0.5, 0.5])
        assert sum(rc["shares"]) == pytest.approx(1.0)

    def test_contributi_degeneri_uniformi(self):
        rc = rp.risk_contributions([1.0, 2.0], np.zeros((2, 2)))
        assert rc["ok"] is True and rc["degenerate"] is True
        assert rc["shares"] == pytest.approx([0.5, 0.5])

    def test_dimensioni_incoerenti(self):
        assert rp.risk_contributions([1.0, 2.0], np.eye(3))["ok"] is False

    def test_erc_diagonale_proporzionale_a_inverso_dev_std(self):
        """Riferimento analitico: su covarianza diagonale w_i ∝ 1/sqrt(v_i)."""
        cov = np.diag([0.04, 0.01])
        got = rp.risk_parity_weights(cov)
        assert got["ok"] is True
        w = np.asarray(got["weights"], dtype=float)
        expected = np.asarray([1 / 0.2, 1 / 0.1])
        expected = expected / expected.sum()
        assert w == pytest.approx(expected, rel=1e-4)
        assert got["max_share_error"] < 1e-6

    def test_erc_identita_pesi_uniformi(self):
        got = rp.risk_parity_weights(np.eye(4))
        assert got["ok"] is True
        assert np.asarray(got["weights"]) == pytest.approx([0.25] * 4)

    def test_matrice_non_definita_positiva_fail_closed(self):
        cov = np.array([[1.0, 2.0], [2.0, 1.0]])     # autovalore negativo
        got = rp.risk_parity_weights(cov)
        assert got["ok"] is False
        assert got["reason"] == "matrice_non_definita_positiva"
        assert got["weights"] is None

    def test_valori_non_finiti(self):
        got = rp.risk_parity_weights(np.array([[1.0, np.nan], [np.nan, 1.0]]))
        assert got["ok"] is False


# ---------------------------------------------------------------------------
# Applicazione ai candidati
# ---------------------------------------------------------------------------
class TestApply:
    def _heavy_portfolio(self, stake=4.0):
        """Posizione aperta sullo STESSO bucket: cosi' il candidato e' la gamba
        che pesa (e' lui che il modulo puo' ridurre: le posizioni aperte sono
        capitale gia' impegnato, non un ordine da dimensionare)."""
        return [{"match_id": "sx-open", "mercato": "1X2", "stake": stake}]

    def _snapshot(self, variance, *, n=20):
        """Istantanea finta ma della STESSA forma di quella vera."""
        stats = {"1X2|sxbet": {"bucket": "1X2|sxbet", "n": n,
                               "variance": variance, "std": variance ** 0.5,
                               "mean": 0.0}}
        return {"error": None, "buckets": ["1X2|sxbet"], "keys": ["1X2|sxbet"],
                "matrix": [[variance]], "stats": stats, "pairs": {}, "days": 20}

    def test_interruttore_spento_non_tocca_nulla(self, monkeypatch):
        monkeypatch.setenv("RISK_PARITY_GUARD", "0")
        cands = [_cand(stake=5.0)]
        kept, info = rp.apply(cands, bankroll=50.0,
                              open_positions=self._heavy_portfolio(),
                              snapshot=self._snapshot(0.04))
        assert kept[0]["stake"] == 5.0 and info["active"] is False

    def test_bucket_senza_campione_lascia_invariato(self, monkeypatch):
        cands = [_cand(mercato="OU", stake=5.0)]
        kept, info = rp.apply(cands, bankroll=50.0, open_positions=[],
                              snapshot=self._snapshot(0.04))
        assert kept[0]["stake"] == 5.0
        assert info["skipped_no_sample"] == 1
        assert kept[0]["risk_parity"]["decision"] == "no_sample"

    def test_posizione_pesante_viene_ridotta(self, monkeypatch):
        cands = [_cand(stake=12.0)]
        kept, info = rp.apply(cands, bankroll=50.0,
                              open_positions=self._heavy_portfolio(),
                              snapshot=self._snapshot(0.04))
        after = kept[0]["stake"]
        assert 0 < after < 12.0
        assert info["scaled"] == 1
        dec = kept[0]["risk_parity"]
        assert dec["decision"] == "scaled"
        assert dec["multiplier"] <= 1.0            # MAI amplificazione
        # La posizione nuova non deve portare piu' del target (ratio x 1/n)
        assert dec["share_before"] > dec["target_share"]

    def test_mai_amplificato_su_griglia(self, monkeypatch):
        """Con 10 candidati identici nessuno supera lo stake di partenza."""
        cands = [_cand(match_id="sx-%d" % i, stake=3.0) for i in range(10)]
        kept, info = rp.apply(cands, bankroll=50.0, open_positions=[],
                              snapshot=self._snapshot(0.04))
        assert all(c["stake"] <= 3.0 for c in kept)
        assert info["scaled"] + info["clear"] + info["blocked"] == 10

    def test_multiplier_sotto_pavimento_blocca(self, monkeypatch):
        monkeypatch.setenv("RISK_PARITY_MIN_MULTIPLIER", "0.99")
        cands = [_cand(stake=12.0)]
        kept, info = rp.apply(cands, bankroll=50.0,
                              open_positions=self._heavy_portfolio(),
                              snapshot=self._snapshot(0.04))
        assert kept[0]["stake"] == 0.0
        assert info["blocked"] == 1
        assert info["blocked_detail"][0]["reason"] == "blocked_risk_share"

    def test_stake_zero_non_entra_nel_portafoglio(self):
        cands = [dict(_cand(stake=0.0))]
        kept, info = rp.apply(cands, bankroll=50.0, open_positions=[],
                              snapshot=self._snapshot(0.04))
        assert kept[0]["stake"] == 0.0 and info["clear"] == 0

    def test_share_del_candidato_sotto_target_non_tocca_nulla(self, monkeypatch):
        """Candidato piccolo su un portafoglio gia' concentrato: la posizione
        sovrappesata e' l'APERTA, e il modulo non ridimensiona cio' che e' gia'
        impegnato — interviene solo sul candidato (decisione dichiarata)."""
        cands = [_cand(stake=3.0)]
        kept, info = rp.apply(cands, bankroll=50.0,
                              open_positions=self._heavy_portfolio(20.0),
                              snapshot=self._snapshot(0.04))
        assert kept[0]["stake"] == 3.0
        assert kept[0]["risk_parity"]["decision"] == "clear"
        assert info["clear"] == 1

    def test_errore_lascia_candidati_invariati(self, monkeypatch):
        def _boom(*a, **k):
            raise RuntimeError("db rotto")
        monkeypatch.setattr(rp, "variance_snapshot", _boom)
        cands = [_cand(stake=5.0)]
        kept, info = rp.apply(cands, bankroll=50.0, open_positions=[])
        assert kept[0]["stake"] == 5.0
        assert info["error"] == "errore_valutazione"

    def test_lettura_fallita_non_ridimensiona(self, monkeypatch):
        cands = [_cand(stake=5.0)]
        kept, info = rp.apply(cands, bankroll=50.0, open_positions=[],
                              snapshot={"error": "lettura_fallita"})
        assert kept[0]["stake"] == 5.0
        assert info["error"] == "lettura_fallita"

    def test_fallback_su_bucket_ignoto_non_azzera_il_rischio(self, monkeypatch):
        """Un bucket senza campione condivide la MEDIANA (mai varianza 0)."""
        snap = self._snapshot(0.04)
        snap["stats"]["OU|sxbet"] = {"bucket": "OU|sxbet", "n": 20,
                                     "variance": 0.25, "std": 0.5, "mean": 0.0}
        snapshot = dict(snap, buckets=["1X2|sxbet", "OU|sxbet"],
                        keys=["1X2|sxbet", "OU|sxbet"],
                        matrix=[[0.04, 0.0], [0.0, 0.25]])
        cands = [_cand(match_id="sx-a", stake=5.0),
                 _cand(match_id="sx-z", mercato="TENNIS", stake=5.0)]
        kept, info = rp.apply(cands, bankroll=50.0, open_positions=[],
                              snapshot=snapshot)
        # TENNIS|tennis non e' nel campione: resta invariato e dichiarato
        assert kept[1]["stake"] == 5.0
        assert info["skipped_no_sample"] == 1
        assert info["kept"] >= 1


# ---------------------------------------------------------------------------
# Report e CLI
# ---------------------------------------------------------------------------
class TestReport:
    def test_report_dichiara_campione_e_soglie(self, tmp_path, monkeypatch):
        _use_db(monkeypatch, _temp_db(tmp_path, _bucket_rows(
            "sx-1", "1X2", [1.0, -1.0] * 6)))
        text = rp.format_report(rp.variance_snapshot(use_cache=False))
        assert "RISK PARITY" in text and "1X2|sxbet" in text
        assert "campione insufficiente" in text or "dev.std" in text

    def test_report_su_errore_non_solleva(self):
        text = rp.format_report({"error": "lettura_fallita"})
        assert "non disponibile" in text

    def test_cli_json(self, tmp_path, monkeypatch, capsys):
        _use_db(monkeypatch, _temp_db(tmp_path, _bucket_rows(
            "sx-1", "1X2", [1.0, -1.0] * 6)))
        assert rp.main(["--variance", "--json", "--no-cache"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert "stats" in payload and "buckets" in payload


# ---------------------------------------------------------------------------
# Tripwire
# ---------------------------------------------------------------------------
class TestTripwire:
    def _src(self) -> str:
        return _code_only(rp.__file__)

    def test_sorgente_senza_scritture(self):
        src = self._src()
        for token in ("INSERT", "UPDATE ", "DELETE FROM", "save_bet(",
                      "save_prediction(", "save_clv("):
            assert token not in src, token

    def test_sorgente_senza_rete_ne_ordini(self):
        src = self._src()
        for token in ("import requests", "httpx", "odds_api", "sx_signals",
                      "_live_fill", "place_limit_order", "resolve_market_for",
                      "execution_engine", "poisson_engine", "value_filter"):
            assert token not in src, token
        # Il percorso ordini non e' nemmeno NOMINATO nel codice
        assert "auto_bet" not in src

    def test_connessione_in_sola_lettura_rifiuta_una_scrittura(self, tmp_path,
                                                              monkeypatch):
        path = _temp_db(tmp_path, _bucket_rows("sx-1", "1X2", [1.0, -1.0] * 6))
        monkeypatch.setattr(rp, "_db_path", lambda: path)
        conn = rp._ro_conn()
        try:
            with pytest.raises(sqlite3.OperationalError):
                conn.execute("UPDATE bets SET stake = 99")
        finally:
            conn.close()

    def test_import_leggero(self):
        code = ("import sys, risk_parity;"
                "print(any(m in sys.modules for m in "
                "('tracker','auto_bet','bot','odds_api')))")
        out = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT),
                             capture_output=True, text=True, timeout=120)
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == "False"
