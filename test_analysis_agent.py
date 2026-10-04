"""Test dell'Agente ANALISI (Steam Velocity & Juice Monitoring, 04/10/2026).

L'agente DESCRIVE il mercato (non decide nulla): aggiunge a ogni segnale il
GRADIENTE della quota sharp (ΔQ/Δt, %/min), il margine implicito (overround) e
la sua VARIAZIONE, e la FRESCHEZZA dell'osservazione.

Tutti i test girano OFFLINE: `steam_fn`, `oracle_fn` e `names_fn` sono
iniettati (nessuna cache, nessuna rete) e lo stato del juice vive nel file
temporaneo del conftest. Gli import di `steam_move`/`pinnacle_oracle` restano
pigri: e' cosi' che un feed alternativo puo' entrare senza toccare l'agente.
"""

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from agents.analysis_agent import AnalysisAgent
from agents.contracts import (DEFAULT_MAX_AGE_S, FUTURE_TOLERANCE_S,
                              AnalysisOutput, OracleSignal)
from decision.models import DataQuality, Signal


def _signal(match_id="sx-1", market="1X2", outcome="1", price=1.66,
            blended=0.60, depth=None, league="Premier League"):
    return Signal(
        match_id=match_id, outcome=outcome, market=market, price=price,
        league=league,
        kickoff=(datetime.now(timezone.utc) + timedelta(hours=3)).isoformat(),
        market_prob=0.55, model_prob=blended, blended_prob=blended,
        data_quality=DataQuality(depth_usdc=depth),
    )


def _names(home="Home FC", away="Away FC", league="Premier League"):
    return lambda match_id: {"match_id": match_id, "home": home, "away": away,
                             "league": league}


@pytest.fixture()
def juice_env(monkeypatch, tmp_path):
    """Stato del juice nella tmp del test (il conftest lo fa gia', qui esplicito)."""
    path = tmp_path / "juice.json"
    monkeypatch.setenv("ANALYSIS_JUICE_STATE", str(path))
    return path


# ---------------------------------------------------------------------------
# 1. STEAM VELOCITY
# ---------------------------------------------------------------------------

class TestSteamVelocity:
    def test_velocity_normalizzata(self, juice_env):
        steam = lambda h, a, m, e, o: {"steam_move": True, "move_pct": -3.0,
                                       "span_minutes": 10.0, "reason": "steam_down"}
        out = AnalysisAgent(steam_fn=steam, names_fn=_names()).process([_signal()])
        assert isinstance(out, AnalysisOutput)
        assert out.steam_moves == 1
        s = out.signals[0]
        assert s.steam_move is True
        assert s.move_pct == -3.0 and s.span_minutes == 10.0
        assert s.velocity_pct_min == -0.3          # ΔQ/Δt in %/min

    def test_span_mancante_non_inventa_una_velocita(self, juice_env):
        steam = lambda *a: {"steam_move": True, "move_pct": -2.0}
        out = AnalysisAgent(steam_fn=steam, names_fn=_names()).process([_signal()])
        assert out.signals[0].velocity_pct_min == 0.0

    def test_la_dipendenza_iniettata_vince_sul_default(self, juice_env):
        """Con `steam_fn` iniettato la forma del mercato e' del chiamante:
        il ramo steam non deve restare morto dietro a `outcomes_for_market`."""
        steam = lambda *a: {"steam_move": True, "move_pct": -1.0,
                            "span_minutes": 5.0, "reason": "ok"}
        out = AnalysisAgent(steam_fn=steam, names_fn=_names()).process([_signal()])
        assert out.signals[0].steam_reason == "ok"

    def test_errore_dello_steam_e_fail_safe_dichiarato(self, juice_env):
        def boom(*a):
            raise RuntimeError("cache rotta")
        out = AnalysisAgent(steam_fn=boom, names_fn=_names()).process([_signal()])
        assert out.signals[0].steam_move is False
        assert out.signals[0].steam_reason == "error:RuntimeError"

    def test_senza_nomi_nessuno_steam(self, juice_env):
        """Un match_id senza riga `matches` non ha nomi: niente steam."""
        out = AnalysisAgent(steam_fn=lambda *a: {"steam_move": True},
                            names_fn=lambda mid: None).process([_signal()])
        s = out.signals[0]
        assert s.steam_move is False
        assert s.steam_reason == "unsupported_market"


# ---------------------------------------------------------------------------
# 2. JUICE (overround + variazione)
# ---------------------------------------------------------------------------

class TestJuice:
    def test_prima_lettura(self, juice_env):
        oracle = lambda h, a: {"overround": 0.045, "sources": ["pinnacle"]}
        out = AnalysisAgent(oracle_fn=oracle, names_fn=_names(),
                            steam_fn=lambda *a: {}).process([_signal()])
        s = out.signals[0]
        assert s.juice == 0.045
        assert s.juice_reason == "prima_lettura"
        assert s.juice_anomaly is False
        assert s.sources == ["pinnacle"]

    def test_allargamento_dell_aggio_accende_l_anomalia(self, juice_env):
        """La soglia scatta sul DELTA: +2.0pp in un giro >= 1.5pp di default."""
        state = {"overround": 0.045}
        agent = AnalysisAgent(oracle_fn=lambda h, a: dict(state),
                              names_fn=_names(), steam_fn=lambda *a: {})
        agent.process([_signal()])                     # prima lettura (0.045)
        state["overround"] = 0.065                     # +2.0pp
        out = agent.process([_signal()])
        s = out.signals[0]
        assert s.juice_delta == 2.0
        assert s.juice_anomaly is True
        assert out.juice_anomalies == 1
        assert "allargamento" in s.juice_reason

    def test_variazione_sotto_soglia_non_allarma(self, juice_env):
        state = {"overround": 0.045}
        agent = AnalysisAgent(oracle_fn=lambda h, a: dict(state),
                              names_fn=_names(), steam_fn=lambda *a: {})
        agent.process([_signal()])
        state["overround"] = 0.050                    # +0.5pp
        out = agent.process([_signal()])
        assert out.signals[0].juice_anomaly is False
        assert out.signals[0].juice_reason == "stabile"

    def test_stato_persistito_su_disco(self, juice_env):
        oracle = lambda h, a: {"overround": 0.05}
        AnalysisAgent(oracle_fn=oracle, names_fn=_names(),
                      steam_fn=lambda *a: {}).process([_signal()])
        saved = json.loads(juice_env.read_text())
        assert saved["sx-1|1X2"] == 0.05

    def test_senza_sharp_il_juice_e_dichiarato(self, juice_env):
        out = AnalysisAgent(oracle_fn=lambda h, a: None, names_fn=_names(),
                            steam_fn=lambda *a: {}).process([_signal()])
        assert out.signals[0].juice is None
        assert out.signals[0].juice_reason == "no_sharp_cache"


# ---------------------------------------------------------------------------
# 3. FRESCHEZZA
# ---------------------------------------------------------------------------

class TestFreshness:
    def test_osservazione_vecchia_non_e_fresca(self, juice_env):
        # Limite negativo = nessuna eta' accettabile: l'osservazione "adesso"
        # (age 0.0) risulta comunque scaduta.
        out = AnalysisAgent(steam_fn=lambda *a: {}, oracle_fn=lambda h, a: None,
                            names_fn=_names(),
                            max_age_seconds=-1.0).process([_signal()])
        s = out.signals[0]
        assert s.fresh is False
        assert out.stale == 1
        assert s.age_s >= 0.0

    def test_osservazione_recente_e_fresca(self, juice_env):
        out = AnalysisAgent(steam_fn=lambda *a: {}, oracle_fn=lambda h, a: None,
                            names_fn=_names(),
                            max_age_seconds=600.0).process([_signal()])
        assert out.signals[0].fresh is True

    def test_default_di_codice(self, monkeypatch):
        monkeypatch.delenv("ANALYSIS_MAX_AGE_S", raising=False)
        assert DEFAULT_MAX_AGE_S == 180.0


# ---------------------------------------------------------------------------
# 4. ROBUSTEZZA DEL CICLO
# ---------------------------------------------------------------------------

class TestCiclo:
    def test_segnale_rotto_non_ferma_gli_altri(self, juice_env):
        class _Rotto:
            match_id = "sx-broken"
            @property
            def market(self):
                raise ValueError("segnale corrotto")
        good = _signal(match_id="sx-2")
        out = AnalysisAgent(steam_fn=lambda *a: {}, oracle_fn=lambda h, a: None,
                            names_fn=_names()).process([_Rotto(), good])
        assert [s.match_id for s in out.signals] == ["sx-2"]

    def test_senza_match_id_il_segnale_e_saltato(self, juice_env):
        class _Senza:
            signal_id = "x"
            match_id = ""
        out = AnalysisAgent(names_fn=lambda mid: None).process([_Senza()])
        assert out.signals == []

    def test_lista_vuota(self, juice_env):
        out = AnalysisAgent().process([])
        assert out.signals == [] and out.steam_moves == 0

    def test_output_serializzabile(self, juice_env):
        out = AnalysisAgent(steam_fn=lambda *a: {}, oracle_fn=lambda h, a: None,
                            names_fn=_names()).process([_signal()])
        data = out.as_json()
        assert data["signals"] == 1 and isinstance(data["detail"], list)


# ---------------------------------------------------------------------------
# 5. NOMI DAL LEDGER (riuso di data_agent, nessuna seconda query)
# ---------------------------------------------------------------------------

class TestNomiDalLedger:
    def test_nomi_letti_dalla_tabella_matches(self, monkeypatch, juice_env):
        import tracker
        with tempfile.TemporaryDirectory() as td:
            monkeypatch.setattr(tracker, "DB_PATH", Path(td) / "t.db")
            tracker.init_db()
            tracker.save_match("sx-9", "Serie A", "Inter", "Milan",
                               datetime.now(timezone.utc).isoformat())
            seen = {}
            agent = AnalysisAgent(
                steam_fn=lambda h, a, m, e, o: seen.update(home=h, away=a) or {},
                oracle_fn=lambda h, a: None)
            out = agent.process([_signal(match_id="sx-9")])
            assert out.signals[0].home == "Inter"
            assert out.signals[0].away == "Milan"
            assert out.signals[0].league == "Serie A"
            assert seen == {"home": "Inter", "away": "Milan"}


# ---------------------------------------------------------------------------
# 6. CONTRATTO OracleSignal (Pydantic)
# ---------------------------------------------------------------------------

class TestContrattoOracleSignal:
    def _base(self, **kw):
        data = dict(signal_id="s1", match_id="m1", esito="1", price=1.66,
                    observed_at=datetime.now(timezone.utc))
        data.update(kw)
        return data

    def test_freshness_derivata_dall_osservazione(self):
        s = OracleSignal(**self._base(max_age_s=600.0))
        assert s.fresh is True and s.age_s >= 0.0

    def test_price_non_giocabile_rifiutata(self):
        with pytest.raises(ValidationError):
            OracleSignal(**self._base(price=1.0))

    def test_esito_vuoto_rifiutato(self):
        with pytest.raises(ValidationError):
            OracleSignal(**self._base(esito="  "))

    def test_market_normalizzato_in_maiuscolo(self):
        assert OracleSignal(**self._base(market="1x2")).market == "1X2"

    def test_timestamp_naive_rifiutato(self):
        """Su un mercato un istante senza fuso e' ambiguo (lezione 17/09)."""
        with pytest.raises(ValidationError):
            OracleSignal(**self._base(observed_at=datetime.now()))

    def test_timestamp_nel_futuro_rifiutato(self):
        futuro = datetime.now(timezone.utc) + timedelta(
            seconds=FUTURE_TOLERANCE_S + 60)
        with pytest.raises(ValidationError):
            OracleSignal(**self._base(observed_at=futuro))

    def test_serializzabile(self):
        s = OracleSignal(**self._base(kickoff=datetime.now(timezone.utc)))
        data = s.as_json()
        assert data["signal_id"] == "s1" and "fresh" in data
