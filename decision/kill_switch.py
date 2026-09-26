"""decision/kill_switch.py — Autorita' superiore: chi blocca le puntate.

Precedenza (deciso dal proprietario il 14/09/2026):

    1. **kill switch manuale** (`/autobet off`, file `auto_bet_mode.json`)
    2. **stop-loss giornaliero** (`daily_stop.json`, -5% in 24h)
    3. **circuit breaker settimanale** (`weekly_stop.json`, -12% rolling 7g)
    4. **pausa settlement** (opzionale, non blocca la bet: blocca il REFERTO)

La precedenza serve a una cosa concreta: dire **cosa rimuovere per ripartire**.
Se sono attivi sia il kill switch sia lo stop-loss, il motivo riportato e' il
kill switch (si riarma con `/autobet live`); lo stop-loss resta visibile come
avviso e scade da solo.

La pausa settlement e' un asse diverso: le puntate possono continuare (il
budget/edge non cambia), ma la riga non si chiude e il **feedback engine si
ferma** (nessun esito -> nessun ritraining, drift non misurabile). Per questo
viene riportata come `advisory` e non come blocco, con precedenza minore.

Fail-safe, direzioni opposte e volute:
  - modalita' illeggibile -> `off` (FAIL-CLOSED: senza certezza non si punta);
  - stop-loss illeggibile -> non attivo (FAIL-OPEN: un file corrotto non deve
    bloccare per 24h, come documentato per `daily_stop.json`).
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from .models import KillSwitchStatus

Probe = Callable[[], Any]


def _default_probes() -> dict[str, Probe]:
    """Sonde reali (import pigro: `decision` resta importabile senza DB)."""
    def kill_switch() -> dict:
        import auto_bet
        return auto_bet.kill_switch_status()

    def daily_stop() -> dict:
        import auto_bet
        return auto_bet.daily_stop_status()

    def weekly_stop() -> dict:
        import auto_bet
        return auto_bet.weekly_stop_status()

    def settlement_paused() -> bool:
        import tracker
        return bool(tracker.settlement_paused())

    return {
        "kill_switch": kill_switch,
        "daily_stop": daily_stop,
        "weekly_stop": weekly_stop,
        "settlement_paused": settlement_paused,
    }


def status(probes: Optional[dict[str, Probe]] = None) -> KillSwitchStatus:
    """Istantanea dei blocchi attivi (sonde iniettabili -> test senza file/DB)."""
    active = dict(_default_probes())
    if probes:
        active.update(probes)

    out = KillSwitchStatus()

    try:
        info = active["kill_switch"]() or {}
        out.mode = str(info.get("effective") or info.get("mode") or "off").lower()
        out.env_mode = str(info.get("env_mode") or "")
        override = info.get("override")
        out.override = None if override in (None, "") else str(override)
        out.provider_ready = bool(info.get("provider_ready"))
    except Exception:
        # fail-closed: non sappiamo se e' consentito puntare -> non puntiamo
        out.mode = "off"

    try:
        stop = active["daily_stop"]() or {}
        # `auto_bet.daily_stop_status()` espone la chiave **`stopped`**: leggere
        # `active` (nome usato solo dalle sonde iniettate nei test) rendeva la
        # catena CIECA allo stop-loss — visto in produzione il 21/09/2026, con
        # `decision status` che dichiarava "stadio betting: libero" mentre
        # `auto_bet` bloccava ogni giro. Si accettano ENTRAMBE le chiavi: la
        # sonda reale usa `stopped`, i probe dei test possono usare `active`.
        out.daily_stop_active = bool(stop.get("stopped", stop.get("active")))
        out.daily_stop_detail = str(stop.get("detail") or stop.get("reason") or "")
    except Exception:
        # fail-open: un file illeggibile non blocca il portafoglio per 24h
        out.daily_stop_active = False

    try:
        weekly = active["weekly_stop"]() or {}
        # `auto_bet.weekly_stop_status()` espone `stopped`: si accettano
        # entrambe le chiavi (i probe dei test possono usare `active`).
        out.weekly_stop_active = bool(weekly.get("stopped", weekly.get("active")))
        if out.weekly_stop_active:
            out.weekly_stop_detail = str(
                weekly.get("reason")
                or f"drawdown rolling {weekly.get('drawdown_pct')}%")
    except Exception:
        # fail-open come il daily: nessuna certezza sul file non deve
        # bloccare il portafoglio
        out.weekly_stop_active = False

    try:
        out.settlement_paused = bool(active["settlement_paused"]())
    except Exception:
        out.settlement_paused = False

    if out.mode not in ("off", "sim", "live"):
        out.mode = "off"
    return out


def blocking_summary(kills: KillSwitchStatus) -> str:
    """Riga per log/Telegram: motivo primo + avvisi (in ordine di precedenza)."""
    block = kills.first_block()
    if block is None:
        base = "nessun blocco"
    else:
        base = f"bloccato da {block.value}"
    advisories = [KILL_SWITCH_ADVISORY_LABELS[name]
                  for name in kills.advisories() if name in KILL_SWITCH_ADVISORY_LABELS]
    if advisories:
        base += " | avvisi: " + ", ".join(advisories)
    return base


KILL_SWITCH_ADVISORY_LABELS = {
    "settlement_pause": "settlement in pausa (referto fermo)",
}


__all__ = ["KILL_SWITCH_ADVISORY_LABELS", "blocking_summary", "status"]
