"""decision/guards.py — Fail Fast sui blocchi di sicurezza.

Gerarchia dichiarata (decisione del proprietario, 14/09/2026; fail-fast
esplicito il 15/09/2026):

    1. KILL SWITCH MANUALE      (`/autobet off`, `auto_bet_mode.json`)
    2. STOP-LOSS GIORNALIERO    (`daily_stop.json`, -5% in 24h)
    3. PAUSA SETTLEMENT         (`settlement_paused.json`)

**Precedenza assoluta**: la catena si valuta sempre in quest'ordine e si ferma
al PRIMO blocco attivo — nessun calcolo di modello, di rischio o di stake
avviene dopo un blocco. E' questo il significato di *fail fast*: non "verifico
tutto e poi decido", ma "appena un'autorita' dice no, esco".

Gli **stadi** (`stage`) rendono onesta la semantica decisa il 15/09:

| stadio       | cosa governa                     | blocchi applicabili |
|--------------|----------------------------------|---------------------|
| `betting`    | la puntata (stake, ordine)       | kill switch, stop-loss |
| `settlement` | referto + feedback engine        | pausa settlement |

La pausa settlement **non blocca la puntata** (scelta del proprietario: ferma
il referto e quindi il feedback, non il rischio gia' dimensionato); resta
percio' nella catena con precedenza 3 ma su un altro stadio, e per lo stadio
`betting` viene riportata come **avviso** (`advisories`), non come blocco.

Fail-fast, due direzioni opposte e volute (ereditate da `kill_switch.py`):

- modalita' illeggibile -> `off` (**fail-CLOSED**: senza certezza non si punta);
- stop-loss illeggibile -> non attivo (**fail-OPEN**: un file corrotto non
  deve fermare il portafoglio per 24h).

Uso:

    from decision.guards import STAGE_BETTING, SafetyBlockError, require_clear

    try:
        require_clear(kills, stage=STAGE_BETTING)     # raise = fail fast
    except SafetyBlockError as block:
        return blocked_plan(block.block)              # nessun altro calcolo
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from . import kill_switch as kill_switch_mod
from .models import KillSwitchStatus, ReasonCode

STAGE_BETTING = "betting"
STAGE_SETTLEMENT = "settlement"
STAGES = (STAGE_BETTING, STAGE_SETTLEMENT)


class SafetyRule(BaseModel):
    """Una voce della catena: cosa blocca, in quali stadi, con che precedenza."""

    name: str
    reason: ReasonCode
    stages: tuple[str, ...]
    label: str
    precedence: int
    #: Cosa rimuovere per ripartire (la precedenza serve a questo).
    hint: str = ""


#: LA catena: unica fonte dell'ordine. Nessun modulo la ricostruisce a mano.
SAFETY_CHAIN: tuple[SafetyRule, ...] = (
    SafetyRule(
        name="manual", reason=ReasonCode.KILL_SWITCH_OFF, stages=(STAGE_BETTING,),
        label="kill switch manuale (modalita' off)", precedence=1,
        hint="riarma con `/autobet live` (o `/autobet sim`)",
    ),
    SafetyRule(
        name="daily_stop", reason=ReasonCode.DAILY_STOP_LOSS, stages=(STAGE_BETTING,),
        label="stop-loss giornaliero", precedence=2,
        hint="scade da solo (DAILY_STOP_HOURS) o si azzera con clear_daily_stop()",
    ),
    SafetyRule(
        name="weekly_stop", reason=ReasonCode.WEEKLY_STOP_LOSS, stages=(STAGE_BETTING,),
        label="circuit breaker settimanale", precedence=3,
        hint="si riarma da solo quando il picco esce dalla finestra (o clear_weekly_stop())",
    ),
    SafetyRule(
        name="settlement_pause", reason=ReasonCode.SETTLEMENT_PAUSED,
        stages=(STAGE_SETTLEMENT,), label="pausa settlement", precedence=4,
        hint="riattiva con `/settlement on`",
    ),
)

RULES_BY_NAME = {rule.name: rule for rule in SAFETY_CHAIN}


class SafetyBlock(BaseModel):
    """Un blocco attivo, con motivo machine-readable e contesto."""

    reason: ReasonCode
    stage: str
    name: str = ""
    label: str = ""
    detail: str = ""
    precedence: int = 0
    hint: str = ""

    def as_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class SafetyBlockError(Exception):
    """Sollevata da `require_clear`: la catena si ferma QUI (fail fast).

    Porta con se' il `SafetyBlock`, cosi' il chiamante non deve ricostruire il
    motivo (che e' l'errore tipico quando si gestiscono blocchi a mano).
    """

    def __init__(self, block: SafetyBlock) -> None:
        super().__init__(f"bloccato da {block.name or block.reason.value}: "
                         f"{block.detail or block.label}")
        self.block = block


def _is_active(name: str, kills: KillSwitchStatus) -> bool:
    """True se il blocco `name` e' attivo nell'istantanea `kills`."""
    if name == "manual":
        return kills.mode == "off"
    if name == "daily_stop":
        return bool(kills.daily_stop_active)
    if name == "weekly_stop":
        return bool(kills.weekly_stop_active)
    if name == "settlement_pause":
        return bool(kills.settlement_paused)
    return False


def _detail(rule: SafetyRule, kills: KillSwitchStatus) -> str:
    if rule.name == "manual":
        if kills.override:
            return f"kill switch manuale attivo (override '{kills.override}')"
        return "modalita' 'off' (kill-switch o fail-closed)"
    if rule.name == "daily_stop":
        return ("stop-loss giornaliero attivo"
                + (f": {kills.daily_stop_detail}" if kills.daily_stop_detail else ""))
    if rule.name == "weekly_stop":
        return ("circuit breaker settimanale attivo"
                + (f": {kills.weekly_stop_detail}" if kills.weekly_stop_detail else ""))
    if rule.name == "settlement_pause":
        return "settlement in pausa: referto e feedback engine fermi"
    return rule.label


def blocks(kills: KillSwitchStatus, stage: str = STAGE_BETTING) -> list[SafetyBlock]:
    """Blocchi attivi per lo stadio, IN ORDINE DI PRECEDENZA."""
    out: list[SafetyBlock] = []
    for rule in SAFETY_CHAIN:
        if stage not in rule.stages or not _is_active(rule.name, kills):
            continue
        out.append(SafetyBlock(reason=rule.reason, stage=stage, name=rule.name,
                               label=rule.label, detail=_detail(rule, kills),
                               precedence=rule.precedence, hint=rule.hint))
    return out


def advisories(kills: KillSwitchStatus, stage: str = STAGE_BETTING) -> list[SafetyBlock]:
    """Blocchi attivi su ALTRI stadi: bloccano qualcosa, ma non questo.

    Servono a non nascondere una condizione: con la pausa settlement attiva la
    puntata puo' partire, ma l'operatore deve sapere che il referto e' fermo.
    """
    out: list[SafetyBlock] = []
    for rule in SAFETY_CHAIN:
        if stage in rule.stages or not _is_active(rule.name, kills):
            continue
        out.append(SafetyBlock(reason=rule.reason, stage=stage, name=rule.name,
                               label=rule.label, detail=_detail(rule, kills),
                               precedence=rule.precedence, hint=rule.hint))
    return out


def first(kills: KillSwitchStatus, stage: str = STAGE_BETTING) -> Optional[SafetyBlock]:
    """Primo blocco attivo per lo stadio (None = libero)."""
    found = blocks(kills, stage=stage)
    return found[0] if found else None


def require_clear(kills: KillSwitchStatus, stage: str = STAGE_BETTING) -> None:
    """FAIL FAST: solleva `SafetyBlockError` al primo blocco, altrimenti None.

    E' il cancello che il motore chiama PRIMA di qualunque altra cosa: se
    solleva, non esiste nessun percorso che arrivi a calcolare uno stake.
    """
    block = first(kills, stage=stage)
    if block is not None:
        raise SafetyBlockError(block)


def snapshot(kills: Optional[KillSwitchStatus] = None, *,
             probes: Optional[dict] = None) -> dict[str, Any]:
    """Istantanea completa per log/report: blocchi, avvisi e catena dichiarata."""
    status = kills or kill_switch_mod.status(probes=probes)
    return {
        "mode": status.mode,
        "env_mode": status.env_mode,
        "override": status.override,
        "provider_ready": status.provider_ready,
        "chain": [rule.name for rule in SAFETY_CHAIN],
        "stages": {stage: [b.as_json() for b in blocks(status, stage=stage)]
                   for stage in STAGES},
        "advisories": {stage: [b.as_json() for b in advisories(status, stage=stage)]
                       for stage in STAGES},
        "betting_allowed": first(status, stage=STAGE_BETTING) is None,
        "settlement_allowed": first(status, stage=STAGE_SETTLEMENT) is None,
    }


def describe(kills: KillSwitchStatus, stage: str = STAGE_BETTING) -> str:
    """Riga per log/Telegram: primo blocco + avvisi attivi."""
    block = first(kills, stage=stage)
    base = f"bloccato da {block.name} ({block.reason.value})" if block else "nessun blocco"
    note = advisories(kills, stage=stage)
    if note:
        base += " | avvisi: " + ", ".join(f"{b.name} ({b.stage})" for b in note)
    return base


__all__ = [
    "RULES_BY_NAME", "SAFETY_CHAIN", "STAGES", "STAGE_BETTING", "STAGE_SETTLEMENT",
    "SafetyBlock", "SafetyBlockError", "SafetyRule", "advisories", "blocks",
    "describe", "first", "require_clear", "snapshot",
]
