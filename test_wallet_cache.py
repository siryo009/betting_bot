"""Test della CACHE e della COALESCENZA della lettura del wallet.

INCIDENTE DEL 17/10/2026 (riprodotto dal vivo, non ipotizzato). Nel processo
del bot sono comparsi **309 fallimenti di lettura wallet** con `429 Too Many
Requests` su `/user/balance-v3` in pochi minuti (misurato: 00:39 142 fallimenti,
00:40 27, 00:41 140) piu' 6 `RecursionError`. `_live_wallet_snapshot` non aveva
NE' cache NE' coalescenza e veniva chiamata da ~15 percorsi (ciclo 60s, un
candidato per volta, catena chief, layer torneo/rush, job, web_api): ~100
richieste HTTP al minuto sullo stesso saldo.

Il danno non era solo il rumore nei log. Con la lettura fallita il bankroll non
veniva determinato e il giro **ripiegava sulla CASSA SIMULATA**, cioe' su una
base di misura DIVERSA — la classe di bug che il 21/09 ha armato un falso
stop-loss (cassa 20,00 contro equity 33,55) e che il 14/10 ha dimensionato
ordini su un capitale gonfiato di +32%.

Le tre proprieta' difese qui (tutte nel punto UNICO di lettura):
  1. TTL di riuso (`LIVE_WALLET_TTL_S`, 20s): tutti i chiamanti dello stesso
     ciclo vedono lo STESSO capitale;
  2. coalescenza (`LIVE_WALLET_MIN_INTERVAL_S`, 6s): nemmeno il percorso
     d'ordine (`force=True`) puo' martellare l'exchange;
  3. lettura fallita -> si resta sull'ultima istantanea RECENTE
     (`LIVE_WALLET_STALE_MAX_S`, 120s) invece di cambiare base di misura;
     senza istantanea si torna `None` (fail-closed, mai la cassa simulata).

TUTTO OFFLINE: provider iniettato, `_now` esplicito (nessun `sleep`), ledger
temporaneo, zero rete, zero credenziali, zero ordini.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

import pytest

import auto_bet
import tracker


@pytest.fixture
def temp_db(monkeypatch):
    """Ledger SQLite temporaneo: `_open_live_snapshot` legge da qui."""
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        monkeypatch.setattr(tracker, "DB_PATH", db_path)
        tracker.init_db()
        yield db_path


@pytest.fixture(autouse=True)
def _clean_cache(monkeypatch):
    """La cache e' STATO DI MODULO: si azzera sempre (prima e dopo)."""
    monkeypatch.setenv("LIVE_WALLET_TTL_S", "20")
    monkeypatch.setenv("LIVE_WALLET_MIN_INTERVAL_S", "6")
    monkeypatch.setenv("LIVE_WALLET_STALE_MAX_S", "120")
    monkeypatch.setenv("LIVE_WALLET_WARN_TTL_S", "60")
    auto_bet.reset_wallet_cache()
    yield
    auto_bet.reset_wallet_cache()


class _Prov:
    """Provider SX finto: conta le letture e puo' fallire a comando."""

    name = "sxbet"

    def __init__(self, available: float = 30.0, exposure: float = 0.0):
        self.available = available
        self.exposure = exposure
        self.calls = 0
        self.error: Exception | None = None

    def get_balance(self) -> dict:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return {"availableBalance": self.available, "exposure": self.exposure}


def _engine(monkeypatch, provider):
    """Instrada `ExecutionEngine()` sul provider finto."""
    import execution_engine as ee
    engine = type("_Eng", (), {"provider": provider})()
    monkeypatch.setattr(ee, "ExecutionEngine", lambda *a, **k: engine)
    return ee


class TestCacheRiuso:
    """Proprieta' 1: TTL di riuso — un solo HTTP per ciclo."""

    def test_due_letture_nel_ttl_una_sola_http(self, monkeypatch, temp_db):
        prov = _Prov(30.0)
        _engine(monkeypatch, prov)
        a = auto_bet._live_wallet_snapshot(_now=1000.0)
        b = auto_bet._live_wallet_snapshot(_now=1010.0)
        assert prov.calls == 1
        assert a["equity"] == pytest.approx(30.0)
        assert b["equity"] == pytest.approx(30.0)
        assert a["cached"] is False and b["cached"] is True

    def test_oltre_il_ttl_si_rilegge(self, monkeypatch, temp_db):
        prov = _Prov(30.0)
        _engine(monkeypatch, prov)
        auto_bet._live_wallet_snapshot(_now=1000.0)
        auto_bet._live_wallet_snapshot(_now=1021.0)
        assert prov.calls == 2

    def test_tutti_i_chiamanti_del_ciclo_vedono_lo_stesso_capitale(
            self, monkeypatch, temp_db):
        """Prima il wallet poteva essere letto due volte nello stesso ciclo e
        due ordini venivano dimensionati su capitali DIVERSI."""
        prov = _Prov(30.0)
        _engine(monkeypatch, prov)
        primo = auto_bet._live_wallet_snapshot(_now=1000.0)
        prov.available = 45.0          # il saldo cambia fra le due chiamate
        secondo = auto_bet._live_wallet_snapshot(_now=1005.0)
        assert primo["equity"] == secondo["equity"] == pytest.approx(30.0)

    def test_dry_run_non_avvelena_la_cache(self, monkeypatch, temp_db):
        import execution_engine as ee
        _engine(monkeypatch, ee.DryRunProvider())
        assert auto_bet._live_wallet_snapshot(_now=1000.0) is None
        # un provider reale configurato dopo e' letto per davvero
        prov = _Prov(12.0)
        _engine(monkeypatch, prov)
        snap = auto_bet._live_wallet_snapshot(_now=1001.0)
        assert snap is not None and snap["equity"] == pytest.approx(12.0)
        assert prov.calls == 1

    def test_contatori_telemetria(self, monkeypatch, temp_db):
        prov = _Prov(30.0)
        _engine(monkeypatch, prov)
        auto_bet._live_wallet_snapshot(_now=1000.0)
        auto_bet._live_wallet_snapshot(_now=1005.0)
        st = auto_bet.wallet_cache_stats()
        assert st["http_calls"] == 1 and st["reused"] == 1

    def test_esposizione_e_equity_coerenti(self, monkeypatch, temp_db):
        """`equity = available + exposure` (l'esposizione dal ledger: 0 qui)."""
        prov = _Prov(27.83, 0.0)
        _engine(monkeypatch, prov)
        snap = auto_bet._live_wallet_snapshot(_now=1000.0)
        assert snap["available"] == pytest.approx(27.83)
        assert snap["exposure"] == pytest.approx(0.0)
        assert snap["equity"] == pytest.approx(27.83)


class TestCoalescenza:
    """Proprieta' 2: nemmeno `force=True` puo' martellare l'exchange."""

    def test_force_entro_l_intervallo_riusa(self, monkeypatch, temp_db):
        prov = _Prov(30.0)
        _engine(monkeypatch, prov)
        auto_bet._live_wallet_snapshot(_now=1000.0)
        snap = auto_bet._live_wallet_snapshot(force=True, _now=1004.0)
        assert prov.calls == 1
        assert snap["cached"] is True
        assert snap["equity"] == pytest.approx(30.0)

    def test_force_oltre_l_intervallo_rilegge(self, monkeypatch, temp_db):
        prov = _Prov(30.0)
        _engine(monkeypatch, prov)
        auto_bet._live_wallet_snapshot(_now=1000.0)
        auto_bet._live_wallet_snapshot(force=True, _now=1007.0)
        assert prov.calls == 2

    def test_force_salta_il_ttl_ma_non_la_coalescenza(self, monkeypatch,
                                                     temp_db):
        """A t=1010 (oltre i 6s, dentro i 20s): il percorso NON forzato riusa,
        quello degli ORDINI rilegge — e' il senso di `force`."""
        prov = _Prov(30.0)
        _engine(monkeypatch, prov)
        auto_bet._live_wallet_snapshot(_now=1000.0)
        auto_bet._live_wallet_snapshot(_now=1010.0)          # riuso (TTL)
        assert prov.calls == 1
        auto_bet._live_wallet_snapshot(force=True, _now=1010.0)
        assert prov.calls == 2

    def test_refresh_live_stakes_usa_force(self):
        """Tripwire (chiamante, non resolver): il percorso che DIMENSIONA un
        ordine reale chiede la lettura forzata. Se qualcuno togliesse il
        `force=True`, un ordine potrebbe essere tagliato su un'equity vecchia
        di 20s senza che nessun test se ne accorga."""
        import inspect
        src = inspect.getsource(auto_bet.refresh_live_stakes)
        assert "_live_wallet_snapshot(force=True)" in src


class TestLetturaFallita:
    """Proprieta' 3: MAI il ripiego su un'altra base di misura."""

    def test_istantanea_recente_usata_senza_cambiare_base(self, monkeypatch,
                                                          temp_db):
        prov = _Prov(30.0)
        _engine(monkeypatch, prov)
        auto_bet._live_wallet_snapshot(_now=1000.0)
        prov.error = RuntimeError("429 Too Many Requests")
        snap = auto_bet._live_wallet_snapshot(_now=1060.0)
        assert snap is not None
        assert snap["stale"] is True
        assert snap["equity"] == pytest.approx(30.0)
        # le chiavi del contratto ci sono tutte: il chiamante NON ripiega
        assert {"available", "exposure", "equity"} <= set(snap)

    def test_senza_istantanea_niente_ripiego(self, monkeypatch, temp_db):
        """Nessuna lettura riuscita e nessuna cache: None (fail-closed). Mai un
        valore preso dalla cassa simulata — e' la classe del falso stop 21/09."""
        prov = _Prov(30.0)
        prov.error = RuntimeError("429 Too Many Requests")
        _engine(monkeypatch, prov)
        assert auto_bet._live_wallet_snapshot(_now=1000.0) is None

    def test_istantanea_troppo_vecchia_non_usata(self, monkeypatch, temp_db):
        prov = _Prov(30.0)
        _engine(monkeypatch, prov)
        auto_bet._live_wallet_snapshot(_now=1000.0)
        prov.error = RuntimeError("rete giu")
        assert auto_bet._live_wallet_snapshot(_now=1121.0) is None

    def test_istantanea_al_limite_e_ancora_usabile(self, monkeypatch, temp_db):
        prov = _Prov(30.0)
        _engine(monkeypatch, prov)
        auto_bet._live_wallet_snapshot(_now=1000.0)
        prov.error = RuntimeError("rete giu")
        snap = auto_bet._live_wallet_snapshot(_now=1120.0)
        assert snap is not None and snap["stale"] is True

    def test_contatore_dei_fallimenti(self, monkeypatch, temp_db):
        prov = _Prov(30.0)
        prov.error = RuntimeError("429")
        _engine(monkeypatch, prov)
        auto_bet._live_wallet_snapshot(_now=1000.0)
        auto_bet._live_wallet_snapshot(_now=1001.0)
        assert auto_bet.wallet_cache_stats()["failed"] == 2

    def test_rantumazione_con_istantanea_conta_lo_stale_used(self, monkeypatch,
                                                            temp_db):
        prov = _Prov(30.0)
        _engine(monkeypatch, prov)
        auto_bet._live_wallet_snapshot(_now=1000.0)
        prov.error = RuntimeError("429")
        auto_bet._live_wallet_snapshot(_now=1060.0)
        assert auto_bet.wallet_cache_stats()["stale_used"] == 1


class TestAntiSpamLog:
    """Una raffica di 429 non deve diventare 100 righe identiche al minuto."""

    def test_un_avviso_per_finestra(self, monkeypatch, temp_db, caplog):
        prov = _Prov(30.0)
        prov.error = RuntimeError("429 Too Many Requests")
        _engine(monkeypatch, prov)
        with caplog.at_level(logging.WARNING, logger="auto_bet"):
            for t in (1000.0, 1002.0, 1004.0, 1006.0, 1008.0):
                auto_bet._live_wallet_snapshot(_now=t)
        avvisi = [r for r in caplog.records
                  if "lettura saldo wallet fallita" in r.getMessage()]
        assert len(avvisi) == 1

    def test_oltre_la_finestra_si_avvisa_di_nuovo(self, monkeypatch, temp_db,
                                                  caplog):
        prov = _Prov(30.0)
        prov.error = RuntimeError("429")
        _engine(monkeypatch, prov)
        with caplog.at_level(logging.WARNING, logger="auto_bet"):
            auto_bet._live_wallet_snapshot(_now=1000.0)
            auto_bet._live_wallet_snapshot(_now=1061.0)   # oltre i 60s
        avvisi = [r for r in caplog.records
                  if "lettura saldo wallet fallita" in r.getMessage()]
        assert len(avvisi) == 2

    def test_il_riuso_stantio_si_dichiara(self, monkeypatch, temp_db, caplog):
        """Loggare il riuso e' parte del fix: senza, un'equity vecchia
        sembrerebbe una lettura fresca."""
        prov = _Prov(30.0)
        _engine(monkeypatch, prov)
        auto_bet._live_wallet_snapshot(_now=1000.0)
        prov.error = RuntimeError("429")
        with caplog.at_level(logging.WARNING, logger="auto_bet"):
            auto_bet._live_wallet_snapshot(_now=1060.0)
        msg = " ".join(r.getMessage() for r in caplog.records)
        assert "istantanea di" in msg and "invece di cambiare base di misura" in msg


class TestConfigurazione:
    """Le soglie sono da env ma un valore impossibile non spegne la guardia."""

    def test_env_cambia_il_ttl(self, monkeypatch):
        monkeypatch.setenv("LIVE_WALLET_TTL_S", "5")
        assert auto_bet.wallet_cache_ttl_s() == pytest.approx(5.0)
        monkeypatch.setenv("LIVE_WALLET_MIN_INTERVAL_S", "2")
        assert auto_bet.wallet_min_interval_s() == pytest.approx(2.0)

    def test_valore_impossibile_ricade_sul_default(self, monkeypatch, caplog):
        monkeypatch.setenv("LIVE_WALLET_TTL_S", "non-un-numero")
        with caplog.at_level(logging.WARNING, logger="auto_bet"):
            val = auto_bet.wallet_cache_ttl_s()
        assert val == pytest.approx(20.0)
        assert any("non numerico" in r.getMessage() for r in caplog.records)

    def test_valore_negativo_rialzato_a_zero(self, monkeypatch):
        monkeypatch.setenv("LIVE_WALLET_TTL_S", "-5")
        assert auto_bet.wallet_cache_ttl_s() == pytest.approx(0.0)

    def test_env_vuota_usa_il_default(self, monkeypatch):
        monkeypatch.setenv("LIVE_WALLET_STALE_MAX_S", "  ")
        assert auto_bet.wallet_stale_max_s() == pytest.approx(120.0)

    def test_soglie_dichiarate_nella_iac(self):
        """Le env lette dal modulo devono essere `preserve()` in IaC: senza la
        dichiarazione un `railway config apply` distrugge cio' che l'operatore
        imposta e le soglie tornano ai default **in silenzio** (lezione del
        28/09 sulle env perse dal `config pull`)."""
        src = Path(".railway/railway.ts").read_text()
        for nome in ("LIVE_WALLET_TTL_S", "LIVE_WALLET_MIN_INTERVAL_S",
                     "LIVE_WALLET_STALE_MAX_S", "LIVE_WALLET_WARN_TTL_S"):
            assert f"{nome}: preserve()" in src, f"{nome} non dichiarata in IaC"

    def test_reset_azzera_snap_e_contatori(self, monkeypatch, temp_db):
        prov = _Prov(30.0)
        _engine(monkeypatch, prov)
        auto_bet._live_wallet_snapshot(_now=1000.0)
        auto_bet.reset_wallet_cache()
        st = auto_bet.wallet_cache_stats()
        assert st["snap"] is None and st["http_calls"] == 0
        # e la lettura successiva rilegge davvero
        auto_bet._live_wallet_snapshot(_now=1000.5)
        assert prov.calls == 2


class TestIsolamentoTest:
    """La cache e' stato di MODULO: senza reset un test leggerebbe la foto del
    precedente (stessa lezione della cache di prontezza OU, 26/09)."""

    def test_conftest_azzera_la_cache(self):
        """Tripwire sull'isolamento: senza `reset_wallet_cache()` nel fixture
        autouse, un test leggerebbe l'istantanea del PRECEDENTE (e un test che
        vuole misurare un errore di lettura troverebbe una cache piena)."""
        src = Path("conftest.py").read_text()
        assert "reset_wallet_cache()" in src
        # e le soglie sono fissate ai default di codice (un `.env` non deve
        # cambiare il comportamento dei test)
        assert 'monkeypatch.setenv("LIVE_WALLET_TTL_S", "20")' in src
        assert "LIVE_WALLET_MIN_INTERVAL_S" in src

    def test_la_cache_e_vuota_a_ogni_inizio_di_test(self):
        """All'ingresso di QUESTO test la cache e' vuota: e' il fixture autouse
        che l'ha azzerata (i test precedenti l'hanno scritta)."""
        assert auto_bet.wallet_cache_stats()["snap"] is None


class TestConfineConLaCassa:
    """Il confine che il fix deve garantire: un errore di RETE non diventa mai
    una perdita (ne' un capitale diverso)."""

    def test_nessun_ripiego_sulla_cassa_simulata(self, monkeypatch, temp_db):
        """`sizing_equity`/`run_today_bets` hanno il ripiego sulla cassa; il
        SNAPSHOT no: senza istantanea torna None e chi chiama decide."""
        prov = _Prov(30.0)
        prov.error = RuntimeError("429")
        _engine(monkeypatch, prov)
        snap = auto_bet._live_wallet_snapshot(_now=1000.0)
        assert snap is None
        # `sizing_equity` resta una funzione PURA sul valore passato: non
        # consulta il wallet ne' la cassa da sola.
        import inspect
        src = inspect.getsource(auto_bet.sizing_equity)
        assert "bankroll_stats" not in src and "_live_wallet_snapshot" not in src


class TestNessunMartellamento:
    """Il conto che chiude l'incidente: N chiamate logiche -> <= T/N HTTP."""

    def test_cinquanta_chiamate_nello_stesso_ciclo_una_sola_http(
            self, monkeypatch, temp_db):
        """50 chiamanti in ~10s (il ciclo e' 60s): UNA lettura HTTP."""
        prov = _Prov(30.0)
        _engine(monkeypatch, prov)
        base = 1000.0
        for i in range(50):
            auto_bet._live_wallet_snapshot(_now=base + i * 0.2)
        assert prov.calls == 1

    def test_trenta_forzate_in_20_secondi_al_massimo_4_http(self, monkeypatch,
                                                           temp_db):
        """Con la coalescenza a 6s, 20 secondi valgono al massimo 4 letture:
        era ~100/minuto prima del fix."""
        prov = _Prov(30.0)
        _engine(monkeypatch, prov)
        base = 1000.0
        for i in range(30):
            auto_bet._live_wallet_snapshot(force=True, _now=base + i * 0.7)
        assert prov.calls <= 4
