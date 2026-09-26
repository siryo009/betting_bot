"""Test della gerarchia Fail Fast (`decision/guards.py`).

OFFLINE e senza file: le istantanee dei blocchi si costruiscono con
`KillSwitchStatus` (dati) e le sonde si iniettano. Verificano:

1. l'ORDINE della catena (kill switch > stop-loss > pausa settlement) e la
   precedenza assoluta;
2. gli STADI: la pausa settlement non blocca la puntata ma il referto;
3. il fail-fast vero: `require_clear` solleva `SafetyBlockError` col blocco
   dentro, cosi' il chiamante non ricostruisce il motivo;
4. le direzioni fail-safe opposte: modalita' illeggibile -> non si punta
   (fail-closed); stop-loss illeggibile -> non si blocca (fail-open).
"""

import pytest

from decision import kill_switch as kill_switch_mod
from decision.guards import (
    RULES_BY_NAME, SAFETY_CHAIN, STAGE_BETTING, STAGE_SETTLEMENT, SafetyBlockError,
    advisories, blocks, describe, first, require_clear, snapshot,
)
from decision.models import KillSwitchStatus, ReasonCode


def live(**kwargs):
    base = {"mode": "live", "env_mode": "live", "provider_ready": True}
    base.update(kwargs)
    return KillSwitchStatus(**base)


# ---------------------------------------------------------------------------
# 1. Catena e precedenza
# ---------------------------------------------------------------------------

class TestCatena:
    def test_ordine_dichiarato(self):
        assert [rule.name for rule in SAFETY_CHAIN] == [
            "manual", "daily_stop", "weekly_stop", "settlement_pause"]
        assert [rule.precedence for rule in SAFETY_CHAIN] == [1, 2, 3, 4]

    def test_motivi_machine_readable(self):
        assert RULES_BY_NAME["manual"].reason == ReasonCode.KILL_SWITCH_OFF
        assert RULES_BY_NAME["daily_stop"].reason == ReasonCode.DAILY_STOP_LOSS
        assert RULES_BY_NAME["settlement_pause"].reason == ReasonCode.SETTLEMENT_PAUSED

    def test_ogni_blocco_ha_una_ripresa(self):
        for rule in SAFETY_CHAIN:
            assert rule.hint, rule.name

    def test_nessun_blocco_quando_tutto_libero(self):
        assert blocks(live()) == []
        assert first(live()) is None
        require_clear(live())                      # non solleva

    def test_precedenza_assoluta_al_kill_switch(self):
        kills = KillSwitchStatus(mode="off", env_mode="live",
                                 daily_stop_active=True, settlement_paused=True,
                                 daily_stop_detail="perdita 6.0%")
        found = blocks(kills, stage=STAGE_BETTING)
        assert [b.name for b in found] == ["manual", "daily_stop"]
        assert first(kills).name == "manual"
        assert first(kills).precedence == 1
        assert first(kills).label.startswith("kill switch manuale")
        assert "kill-switch" in first(kills).detail

    def test_stop_loss_da_solo(self):
        kills = live(daily_stop_active=True, daily_stop_detail="perdita 5.5%")
        block = first(kills)
        assert block.name == "daily_stop"
        assert "5.5%" in block.detail
        assert block.reason == ReasonCode.DAILY_STOP_LOSS


# ---------------------------------------------------------------------------
# 2. Stadi
# ---------------------------------------------------------------------------

class TestStadi:
    def test_pausa_settlement_non_blocca_la_puntata(self):
        kills = live(settlement_paused=True)
        assert blocks(kills, stage=STAGE_BETTING) == []
        assert first(kills, stage=STAGE_BETTING) is None
        # ... ma blocca il referto/feedback
        referral = blocks(kills, stage=STAGE_SETTLEMENT)
        assert [b.name for b in referral] == ["settlement_pause"]

    def test_pausa_settlement_e_un_avviso_per_lo_stadio_betting(self):
        note = advisories(live(settlement_paused=True), stage=STAGE_BETTING)
        assert [b.name for b in note] == ["settlement_pause"]
        assert note[0].stage == STAGE_BETTING      # riportato NEL contesto chiesto
        assert note[0].reason == ReasonCode.SETTLEMENT_PAUSED

    def test_kill_switch_non_blocca_il_referto(self):
        """Il kill switch ferma le puntate, non il settlement gia' dovuto."""
        kills = KillSwitchStatus(mode="off", env_mode="live", settlement_paused=False)
        assert blocks(kills, stage=STAGE_SETTLEMENT) == []
        assert advisories(kills, stage=STAGE_SETTLEMENT)[0].name == "manual"

    def test_blocchi_e_avvisi_non_si_sovrappongono(self):
        kills = KillSwitchStatus(mode="off", env_mode="live", settlement_paused=True)
        hard = {b.name for b in blocks(kills, stage=STAGE_BETTING)}
        soft = {b.name for b in advisories(kills, stage=STAGE_BETTING)}
        assert hard.isdisjoint(soft)


# ---------------------------------------------------------------------------
# 3. Fail fast
# ---------------------------------------------------------------------------

class TestFailFast:
    def test_require_clear_solleva_col_blocco_dentro(self):
        with pytest.raises(SafetyBlockError) as excinfo:
            require_clear(KillSwitchStatus(mode="off"), stage=STAGE_BETTING)
        block = excinfo.value.block
        assert block.name == "manual"
        assert block.reason == ReasonCode.KILL_SWITCH_OFF
        assert "bloccato da manual" in str(excinfo.value)
        assert "kill-switch" in str(excinfo.value)

    def test_require_clear_sullo_stadio_settlement(self):
        with pytest.raises(SafetyBlockError) as excinfo:
            require_clear(live(settlement_paused=True), stage=STAGE_SETTLEMENT)
        assert excinfo.value.block.name == "settlement_pause"
        # lo stesso stato NON blocca la puntata
        require_clear(live(settlement_paused=True), stage=STAGE_BETTING)

    def test_describe_mostra_blocco_e_avvisi(self):
        kills = KillSwitchStatus(mode="off", env_mode="live", settlement_paused=True)
        text = describe(kills, stage=STAGE_BETTING)
        assert "manual" in text and "avvisi" in text and "settlement_pause" in text
        assert describe(live()).startswith("nessun blocco")


# ---------------------------------------------------------------------------
# 4. Sonde e direzioni fail-safe
# ---------------------------------------------------------------------------

class TestSonde:
    def test_istantanea_completa(self):
        data = snapshot(live(settlement_paused=True))
        assert data["chain"] == ["manual", "daily_stop", "weekly_stop",
                                 "settlement_pause"]
        assert data["betting_allowed"] is True
        assert data["settlement_allowed"] is False
        assert data["stages"][STAGE_SETTLEMENT][0]["name"] == "settlement_pause"

    def test_modalita_illeggibile_fail_closed(self):
        """Senza certezza non si punta: la sonda che esplode -> modalita' off."""
        def boom():
            raise RuntimeError("file corrotto")

        status = kill_switch_mod.status(probes={"kill_switch": boom})
        assert status.mode == "off"
        assert first(status).name == "manual"

    def test_stop_loss_illeggibile_fail_open(self):
        """Un file corrotto non deve fermare il portafoglio per 24h."""
        def boom():
            raise RuntimeError("json rotto")

        status = kill_switch_mod.status(probes={"kill_switch": lambda: {"effective": "live"},
                                                "daily_stop": boom})
        assert status.mode == "live"
        assert first(status) is None

    def test_pausa_illeggibile_non_blocca(self):
        def boom():
            raise RuntimeError("db chiuso")

        status = kill_switch_mod.status(probes={"kill_switch": lambda: {"effective": "live"},
                                                "settlement_paused": boom})
        assert status.settlement_paused is False

    def test_modalita_sconosciuta_diventa_off(self):
        status = kill_switch_mod.status(
            probes={"kill_switch": lambda: {"effective": "turbo"}})
        assert status.mode == "off" and first(status) is not None

    def test_provider_non_pronto_non_e_un_blocco(self):
        """La prontezza del provider e' informativa: non e' un blocco di sicurezza."""
        status = kill_switch_mod.status(
            probes={"kill_switch": lambda: {"effective": "sim", "provider_ready": False}})
        assert status.mode == "sim" and first(status) is None
        assert status.provider_ready is False
