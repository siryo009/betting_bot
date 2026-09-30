"""Test della catena di decisione (`decision/`) — tutti OFFLINE.

Nessun DB, nessuna rete, nessun provider: i motori sono puri e l'orchestratore
riceve l'istantanea del kill switch iniettata. L'unico confronto con la
produzione e' il test di PARITA': il set dei `reject` deve coincidere
esattamente con i rifiuti di `value_filter.is_sane`.
"""

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

import value_filter as vf
from decision import (
    DataQuality, DecisionRecord, KillSwitchStatus, ReasonCode, ReviewQueue,
    RiskLimits, Signal, decide, decide_many, kill_switch, risk_engine,
    stake_engine, summary, utcnow,
)

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

KICKOFF_HOURS = 6.0


def make_signal(*, price=1.60, market_prob=0.58, blended_prob=0.65,
                model_prob=None, league="Premier League", outcome="1",
                confidence=0.90, tier="value", match_id="sx-L1",
                data_quality=None, **kwargs) -> Signal:
    """Segnale che PASSA tutti i gate con i default (1.60 / 58% / 65%).

    `model_prob` (previsione grezza del modello) e' obbligatoria nel contratto:
    qui default = blend + 1pp, cioe' modello leggermente piu' estremo del blend.
    `data_quality` default = modello NON cieco (copertura 0.8, calibrato): un
    modello senza rating finirebbe in review per qualita' dei dati, non per
    confidenza, e i test dei gate non stanno misurando quello.
    """
    quality = data_quality or DataQuality(model_coverage=0.8, calibrated=True,
                                          depth_usdc=900.0)
    return Signal(
        match_id=match_id,
        league=league,
        outcome=outcome,
        selection_label=f"Casa ({outcome})",
        kickoff=utcnow() + timedelta(hours=KICKOFF_HOURS),
        price=price,
        price_source="sx",
        market_prob=market_prob,
        model_prob=blended_prob + 0.01 if model_prob is None else model_prob,
        blended_prob=blended_prob,
        tier=tier,
        confidence=confidence,
        data_quality=quality,
        **kwargs,
    )


def live_kills(**kwargs) -> KillSwitchStatus:
    base = {"mode": "live", "provider_ready": True}
    base.update(kwargs)
    return KillSwitchStatus(**base)


@pytest.fixture
def limits() -> RiskLimits:
    return RiskLimits.from_env()


# ---------------------------------------------------------------------------
# 1. Contratti
# ---------------------------------------------------------------------------

class TestContratti:
    def test_signal_non_contiene_lo_stake(self):
        """Regola 1: il Signal descrive un'opportunita', non un importo."""
        assert "stake" not in Signal.model_fields
        assert "bankroll" not in Signal.model_fields

    def test_signal_id_stabile_e_derivato(self):
        a = make_signal()
        b = make_signal(price=1.75)
        assert a.signal_id == b.signal_id          # stesso match/mercato/esito
        assert a.signal_id == Signal(match_id="sx-L1", outcome="1",
                                     kickoff=a.kickoff, price=1.6,
                                     market_prob=0.58, model_prob=0.66,
                                     blended_prob=0.65).signal_id
        assert len(a.signal_id) == 12

    def test_edge_ed_ev_calcolati_se_assenti(self):
        signal = make_signal(price=1.50, market_prob=0.60, blended_prob=0.66)
        assert signal.edge == pytest.approx(0.06)
        assert signal.ev == pytest.approx(0.66 * 1.50 - 1.0)

    def test_warning_automatico_su_modello_cieco(self):
        signal = make_signal(data_quality=DataQuality(model_coverage=0.0))
        assert "ratings_assenti" in signal.warnings
        assert signal.data_quality.is_blind is True

    def test_confidenza_fuori_range_rifiutata(self):
        with pytest.raises(Exception):
            make_signal(confidence=1.4)

    def test_precedenza_dichiarata(self):
        assert decision_precedence() == ["manual", "daily_stop",
                                         "weekly_stop", "settlement_pause"]


def decision_precedence():
    from decision.models import KILL_SWITCH_PRECEDENCE
    return list(KILL_SWITCH_PRECEDENCE)


# ---------------------------------------------------------------------------
# 2. Kill switch (autorita' superiore)
# ---------------------------------------------------------------------------

class TestKillSwitch:
    def test_modalita_off_blocca(self):
        assert live_kills(mode="off").first_block() is ReasonCode.KILL_SWITCH_OFF
        assert live_kills(mode="off").betting_allowed is False

    def test_stop_loss_giornaliero_blocca(self):
        kills = live_kills(daily_stop_active=True, daily_stop_detail="perdita 6.0%")
        assert kills.first_block() is ReasonCode.DAILY_STOP_LOSS

    def test_off_batte_lo_stop_loss(self):
        """Precedenza: il motivo riportato e' quello da rimuovere per primo."""
        kills = live_kills(mode="off", daily_stop_active=True, settlement_paused=True)
        assert kills.betting_blocks() == ["manual", "daily_stop"]
        assert kills.first_block() is ReasonCode.KILL_SWITCH_OFF
        assert kills.advisories() == ["settlement_pause"]

    def test_pausa_settlement_non_blocca_la_bet(self):
        kills = live_kills(settlement_paused=True)
        assert kills.betting_allowed is True
        assert kills.first_block() is None
        assert kills.advisories() == ["settlement_pause"]

    def test_status_legge_le_sonde(self):
        probes = {
            "kill_switch": lambda: {"effective": "live", "env_mode": "live",
                                    "provider_ready": True},
            "daily_stop": lambda: {"active": False},
            "settlement_paused": lambda: True,
        }
        kills = kill_switch.status(probes=probes)
        assert kills.mode == "live" and kills.provider_ready is True
        assert kills.settlement_paused is True
        assert kills.betting_allowed is True

    def test_sonde_illeggibili_direzioni_opposte(self):
        """Modalita' illeggibile = fail-closed; stop-loss illeggibile = fail-open."""

        def boom():
            raise RuntimeError("file corrotto")

        kills = kill_switch.status(probes={"kill_switch": boom, "daily_stop": boom,
                                           "settlement_paused": boom})
        assert kills.mode == "off" and kills.betting_allowed is False
        assert kills.daily_stop_active is False

    def test_sonda_reale_dello_stop_loss_giornaliero(self, tmp_path, monkeypatch):
        """Regressione 21/09/2026: la sonda REALE espone la chiave `stopped`.

        Il 21/09 in produzione `decision status` riportava "stadio betting:
        libero" mentre `auto_bet` bloccava ogni giro con lo stop-loss attivo:
        la catena leggeva `active`, `auto_bet.daily_stop_status()` scrive
        `stopped`. Questo test usa la sonda VERA (nessun probe iniettato) con
        un file di stop nella tmp: senza il fix `daily_stop_active` resta False.
        """
        import auto_bet

        now = datetime.now(timezone.utc)
        (tmp_path / "daily_stop.json").write_text(json.dumps({
            "day": now.strftime("%Y-%m-%d"),
            "start_bankroll": 33.5535,
            "stopped_at": now.isoformat(),
            "stopped_until": (now + timedelta(hours=20)).isoformat(),
            "reason": "equity wallet -40.4% dall'inizio giornata (valore 20.00)",
        }), encoding="utf-8")
        monkeypatch.setattr(auto_bet, "DAILY_STOP_FILE",
                            tmp_path / "daily_stop.json")
        monkeypatch.setattr(auto_bet, "KILL_SWITCH_FILE",
                            tmp_path / "auto_bet_mode.json")
        monkeypatch.setenv("AUTO_BET_MODE", "live")

        kills = kill_switch.status()
        assert kills.daily_stop_active is True
        assert "40.4" in kills.daily_stop_detail
        assert kills.first_block() is ReasonCode.DAILY_STOP_LOSS
        assert kills.betting_allowed is False

    def test_stop_loss_scaduto_non_blocca(self, tmp_path, monkeypatch):
        """Un file di stop con `stopped_until` nel passato non blocca nulla."""
        import auto_bet

        now = datetime.now(timezone.utc)
        (tmp_path / "daily_stop.json").write_text(json.dumps({
            "stopped_until": (now - timedelta(hours=1)).isoformat(),
            "reason": "vecchio",
        }), encoding="utf-8")
        monkeypatch.setattr(auto_bet, "DAILY_STOP_FILE",
                            tmp_path / "daily_stop.json")
        monkeypatch.setattr(auto_bet, "KILL_SWITCH_FILE",
                            tmp_path / "auto_bet_mode.json")
        monkeypatch.setenv("AUTO_BET_MODE", "live")

        assert kill_switch.status().daily_stop_active is False

    def test_modalita_sconosciuta_diventa_off(self):
        kills = kill_switch.status(probes={"kill_switch": lambda: {"effective": "boh"},
                                           "daily_stop": lambda: {},
                                           "settlement_paused": lambda: False})
        assert kills.mode == "off"

    def test_riepilogo_leggibile(self):
        text = kill_switch.blocking_summary(live_kills(mode="off", settlement_paused=True))
        assert "kill_switch_off" in text and "settlement in pausa" in text


# ---------------------------------------------------------------------------
# 3. Risk Engine
# ---------------------------------------------------------------------------

class TestRiskEngine:
    def test_approve_su_segnale_in_regola(self, limits):
        decision = risk_engine.evaluate(make_signal(), kills=live_kills(), limits=limits)
        assert decision.verdict == "approve"
        assert decision.allows_stake is True
        assert decision.reason is ReasonCode.OK
        assert decision.checked[0] == "kill_switch"      # ordine dei controlli

    def test_review_su_confidenza_bassa(self, limits):
        decision = risk_engine.evaluate(make_signal(confidence=0.30),
                                        kills=live_kills(), limits=limits)
        assert decision.verdict == "review"
        assert decision.reason is ReasonCode.CONFIDENCE_LOW
        assert decision.allows_stake is False

    def test_review_su_modello_cieco(self, limits):
        """Qualita' dei dati: senza ratings il modello usa il profilo neutro
        di lega -> serve un umano (in produzione oggi sarebbe un ordine)."""
        blind = DataQuality(model_coverage=0.0, calibrated=True, depth_usdc=900.0)
        decision = risk_engine.evaluate(make_signal(confidence=0.95, data_quality=blind),
                                        kills=live_kills(), limits=limits)
        assert decision.verdict == "review"
        assert decision.reason is ReasonCode.DATA_QUALITY_LOW
        assert "cieco" in decision.detail

    def test_copertura_parziale_ma_sufficiente_passa(self, limits):
        partial = DataQuality(model_coverage=0.6, calibrated=True)
        decision = risk_engine.evaluate(make_signal(data_quality=partial),
                                        kills=live_kills(), limits=limits)
        assert decision.verdict == "approve"

    def test_review_disattivabile(self, limits):
        off = limits.model_copy(update={"review_enabled": False})
        decision = risk_engine.evaluate(make_signal(confidence=0.30),
                                        kills=live_kills(), limits=off)
        assert decision.verdict == "approve"

    @pytest.mark.parametrize("kwargs,expected", [
        ({"league": "Serie A"}, ReasonCode.LEAGUE_NOT_ALLOWED),
        ({"price": 1.20}, ReasonCode.ODDS_TOO_LOW),
        ({"price": 2.10}, ReasonCode.ODDS_TOO_HIGH),
        ({"market_prob": 0.45, "blended_prob": 0.55}, ReasonCode.NOT_FAVOURITE),
        ({"price": 1.50, "market_prob": 0.58, "blended_prob": 0.585}, ReasonCode.EV_TOO_LOW),
        ({"price": 1.79, "market_prob": 0.55, "blended_prob": 0.90}, ReasonCode.EV_ANOMALOUS),
        # EV sopra soglia (+3.2%) ma edge sotto il minimo di lega (1.5pp < 2pp):
        # l'EV deve restare PASSATO, altrimenti il rifiuto sarebbe attribuito
        # al gate sbagliato.
        ({"price": 1.60, "market_prob": 0.63, "blended_prob": 0.645}, ReasonCode.EDGE_TOO_LOW),
    ])
    def test_reject_col_motivo_giusto(self, limits, kwargs, expected):
        decision = risk_engine.evaluate(make_signal(**kwargs), kills=live_kills(),
                                        limits=limits)
        assert decision.verdict == "reject"
        assert decision.reason is expected
        assert decision.reasons == [expected]

    def test_reject_per_mercato_incoerente(self, limits):
        signal = make_signal(data_quality=DataQuality(market_coherent=False, inv_sum=1.31,
                                                     model_coverage=0.8))
        decision = risk_engine.evaluate(signal, kills=live_kills(), limits=limits)
        assert decision.reason is ReasonCode.MARKET_INCOHERENT
        assert "1.310" in decision.detail

    def test_reject_per_esito_gia_in_portafoglio(self, limits):
        decision = risk_engine.evaluate(make_signal(), kills=live_kills(),
                                        limits=limits, already_exposed=True)
        assert decision.reason is ReasonCode.ALREADY_EXPOSED

    def test_kill_switch_prima_di_tutto(self, limits):
        """Con la modalita' off il motivo NON e' il gate del segnale."""
        decision = risk_engine.evaluate(make_signal(league="Serie A"),
                                        kills=live_kills(mode="off"), limits=limits)
        assert decision.reason is ReasonCode.KILL_SWITCH_OFF
        assert decision.checked == ["kill_switch"]

    def test_parita_con_is_sane(self, limits):
        """Il set dei reject deve coincidere ESATTAMENTE con i rifiuti di is_sane."""
        mismatches = []
        for league in ("Premier League", "Serie A", ""):
            for price in (1.20, 1.50, 1.90):
                for market_prob in (0.45, 0.55):
                    for blended in (market_prob, market_prob + 0.01, market_prob + 0.03):
                        signal = make_signal(price=price, market_prob=market_prob,
                                             blended_prob=blended, league=league)
                        verdict = risk_engine.evaluate(signal, kills=live_kills(),
                                                       limits=limits).verdict
                        ok, why = vf.is_sane(prob=blended, odds=price, ev=signal.ev,
                                             market_prob=market_prob, league=league)
                        if (verdict == "reject") != (not ok):
                            mismatches.append((league, price, market_prob, blended,
                                               verdict, ok, why))
        assert mismatches == [], f"divergenza dai gate di produzione: {mismatches[:3]}"

    def test_tighten_puo_solo_stringere(self, limits):
        assert risk_engine.tighten(limits, "value", 0.005) == {"max_stake_pct": 0.005}
        assert risk_engine.tighten(limits, "value", 0.30) == {}      # piu' largo: ignorato
        assert risk_engine.tighten(limits, "value", None) == {}


# ---------------------------------------------------------------------------
# 4. Stake Engine
# ---------------------------------------------------------------------------

class TestStakeEngine:
    def _approved(self, limits, **kwargs):
        signal = make_signal(**kwargs)
        risk = risk_engine.evaluate(signal, kills=live_kills(), limits=limits)
        assert risk.verdict == "approve"
        return signal, risk

    def test_nessuno_stake_senza_approvazione(self, limits):
        signal = make_signal()
        risk = risk_engine.evaluate(signal, kills=live_kills(mode="off"), limits=limits)
        stake = stake_engine.size(signal, risk, bankroll=1000.0, limits=limits)
        assert stake.executable is False
        assert stake.stake == 0.0
        assert stake.reason is ReasonCode.KILL_SWITCH_OFF

    def test_kelly_scalato_una_volta_sola(self, limits):
        """Blinda il bug del 13/09: la frazione non si applica due volte.

        La frazione si fissa con un limite esplicito (i default di
        `adaptive_staking` sono letti da env all'IMPORT: cambiarli con
        monkeypatch dopo non avrebbe effetto).
        """
        fixed = limits.model_copy(update={"kelly_min": 0.25, "kelly_max": 0.25})
        signal, _ = self._approved(fixed)

        stake_value, fraction = stake_engine.kelly_stake(signal, bankroll=1000.0,
                                                         limits=fixed)
        # La quota del segnale e' `Decimal` (Money): il Kelly e' una formula
        # statistica e vive in float, quindi qui si converte all'estremo — la
        # stessa conversione che fa il motore.
        full = vf.kelly_fraction(signal.blended_prob, float(signal.price), fraction=1.0)
        assert fraction == pytest.approx(0.25)
        assert stake_value == pytest.approx(1000.0 * full * 0.25)
        assert stake_value != pytest.approx(1000.0 * full * 0.25 * 0.25)

    def test_cap_del_tier_applicato(self, limits):
        signal, risk = self._approved(limits, tier="strong_value", league="Premier League")
        stake = stake_engine.size(signal, risk, bankroll=1000.0, limits=limits)
        assert stake.executable is True
        assert stake.stake <= 1000.0 * limits.cap_strong
        assert stake.cap_source in ("tier", "league")

    def test_cap_di_lega_piu_stretto_vince(self, limits):
        """Lega fuori strategia: cap di lega 0.5% (fallback) < cap del tier."""
        signal, risk = self._approved(limits, league="")
        stake = stake_engine.size(signal, risk, bankroll=1000.0, limits=limits)
        assert stake.cap_source == "league"
        assert stake.stake <= 1000.0 * limits.league_max_stake_pct("")

    def test_cap_chiesto_dal_risk_engine(self, limits):
        signal, risk = self._approved(limits)
        risk.tightened["max_stake_pct"] = 0.002
        stake = stake_engine.size(signal, risk, bankroll=1000.0, limits=limits)
        assert stake.cap_source == "risk"
        assert stake.stake <= 2.0

    def test_cap_severo_salta_la_puntata(self, monkeypatch, limits):
        """Wallet piccolo + cap 1% < minimo ordine -> fail-closed (0 ordini)."""
        monkeypatch.delenv("STAKE_CAP_HARD", raising=False)
        strict = RiskLimits.from_env()
        assert strict.stake_cap_hard is True
        signal, risk = self._approved(strict)
        stake = stake_engine.size(signal, risk, bankroll=38.0, limits=strict, mode="live")
        assert stake.executable is False
        assert stake.reason is ReasonCode.STAKE_BELOW_FLOOR
        assert "CAP SEVERO" in stake.detail

    def test_cap_severo_disattivato_accetta_il_floor(self, monkeypatch, limits):
        monkeypatch.setenv("STAKE_CAP_HARD", "0")
        soft = RiskLimits.from_env()
        signal, risk = self._approved(soft)
        stake = stake_engine.size(signal, risk, bankroll=38.0, limits=soft, mode="live")
        assert stake.executable is True
        assert stake.stake == pytest.approx(soft.exchange_floor)

    def test_liquidita_relativa_allo_stake(self, limits):
        signal, risk = self._approved(
            limits, data_quality=DataQuality(model_coverage=0.8, depth_usdc=4.0))
        stake = stake_engine.size(signal, risk, bankroll=1000.0, limits=limits)
        assert stake.executable is False
        assert stake.reason is ReasonCode.LIQUIDITY_LOW

    def test_bankroll_insufficiente(self, limits):
        signal, risk = self._approved(limits)
        stake = stake_engine.size(signal, risk, bankroll=0.0, limits=limits)
        assert stake.executable is False


# ---------------------------------------------------------------------------
# 5. Orchestrazione
# ---------------------------------------------------------------------------

class TestPipeline:
    def test_percorso_approve(self, limits):
        record = decide(make_signal(), kills=live_kills(), limits=limits, bankroll=1000.0)
        assert isinstance(record, DecisionRecord)
        assert record.risk.verdict == "approve"
        assert record.stake is not None and record.stake.executable is True
        assert record.mode == "live"

    def test_kill_switch_off_non_calcola_stake(self, limits, monkeypatch):
        def boom(*args, **kwargs):
            raise AssertionError("lo Stake Engine non deve essere chiamato")

        monkeypatch.setattr(stake_engine, "size", boom)
        record = decide(make_signal(), kills=live_kills(mode="off"), limits=limits,
                        bankroll=1000.0)
        assert record.risk.reason is ReasonCode.KILL_SWITCH_OFF
        assert record.stake is None

    def test_review_finisce_in_coda_senza_stake(self, limits, tmp_path):
        queue = ReviewQueue(tmp_path / "reviews.json")
        record = decide(make_signal(confidence=0.20), kills=live_kills(), limits=limits,
                        bankroll=1000.0, review_queue=queue)
        assert record.risk.verdict == "review"
        assert record.stake is None
        assert queue.summary()["pending"] == 1

    def test_reject_non_entra_in_coda(self, limits, tmp_path):
        queue = ReviewQueue(tmp_path / "reviews.json")
        record = decide(make_signal(league="Serie A"), kills=live_kills(), limits=limits,
                        bankroll=1000.0, review_queue=queue)
        assert record.risk.verdict == "reject"
        assert queue.summary()["total"] == 0

    def test_pausa_settlement_approve_con_avviso(self, limits):
        record = decide(make_signal(), kills=live_kills(settlement_paused=True),
                        limits=limits, bankroll=1000.0)
        assert record.risk.verdict == "approve"
        assert record.risk.advisories == [ReasonCode.SETTLEMENT_PAUSED]
        assert record.kill_switch.settlement_paused is True

    def test_record_as_row_lega_tutto(self, limits):
        record = decide(make_signal(), kills=live_kills(), limits=limits, bankroll=1000.0)
        row = record.as_row()
        for key in ("record_id", "signal_id", "match_id", "price", "market_prob",
                    "blended_prob", "edge", "ev", "tier", "verdict", "reason",
                    "stake", "stake_executable", "mode"):
            assert key in row, key
        assert row["verdict"] == "approve" and row["reason"] == "ok"

    def test_stesso_record_per_due_chiamate(self, limits):
        signal = make_signal()
        a = decide(signal, kills=live_kills(), limits=limits, bankroll=1000.0)
        b = decide(signal, kills=live_kills(), limits=limits, bankroll=1000.0)
        assert a.record_id == b.record_id          # idempotenza del record

    def test_decide_many_usa_una_sola_istantanea(self, limits):
        records = decide_many([make_signal(), make_signal(match_id="sx-L2")],
                              kills=live_kills(), limits=limits, bankroll=1000.0)
        assert len(records) == 2
        report = summary(records)
        assert report["by_verdict"]["approve"] == 2
        assert report["executable"] == 2
        assert report["stake_total"] > 0

    def test_import_decision_non_carica_la_produzione(self):
        """`decision` deve restare leggero: niente auto_bet/bot all'import."""
        code = ("import decision, sys;"
                "print(any(m in sys.modules for m in ('auto_bet', 'bot', 'tracker')))")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        assert out.stdout.strip() == "False", out.stdout + out.stderr
