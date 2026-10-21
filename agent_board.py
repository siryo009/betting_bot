"""agent_board.py — TABELLA DI COMANDO degli agenti (sola lettura).

Cosa risponde, in una schermata sola: **chi ha lavorato, con quali numeri, e
cosa lo ha fermato**. E' il ponte fra la gerarchia (Chief -> agenti) e chi
guarda: la webapp (`/agenti`), il comando Telegram `/agenti` e la CLI.

PERCHE' UN MODULO E NON UNA QUERY NEL FRONTEND. Il registro dei cicli ha gia'
tutto (`data/decision/chief_cycles.jsonl`, scritto da
`chief_shadow_wiring.run_chief_cycle_shadow`: un record per ciclo con
`market` / `strategy` / `analysis` / `brain` / `finance` / `sizing` /
`advisor` / `execution` / `exposure` / `resting`), ma:
  - la lettura deve essere ROTATION-AWARE (`telemetry_logs.read_lines`: il
    file vive anche compresso in `.gz`, lezione del 03/10) e filtrata per
    finestra;
  - gli stati di SICUREZZA e CAPITALE vivono in `auto_bet` (kill switch, stop
    giornaliero/settimanale, CB2, recinto, tetto per ordine) e devono essere
    letti dalla LORO fonte, mai ricopiati nel frontend;
  - con il browser che ricarica, il file non va riletto a ogni richiesta:
    mini-cache in-process (`AGENT_BOARD_TTL_S`).

COSA NON FA (per progetto): **non esegue, non decide, non scrive**. Nessun
ordine, nessuna chiamata a the-odds-api (zero crediti), nessuna scrittura sul
ledger ne' sul volume. Un errore di lettura e' un campo `error` dichiarato,
mai un'eccezione al chiamante: la tabella e' diagnostica, e una diagnostica
che rompe il processo che dovrebbe osservare non serve a nessuno.

ISOLAMENTO DEGLI AGENTI (direttiva 15/10/2026). Ogni agente lavora per conto
suo: non importa fratelli, non conosce il bankroll, non tocca il denaro. La
composizione passa SOLO dal Capo (`ChiefOrchestrator`), ed e' la ragione per
cui qui c'e' UNA riga per agente con `consumes`/`produces` espliciti: leggendo
la tabella si vede il contratto, non solo il numero.

CLI: venv/bin/python agent_board.py [--days N] [--json] [--refresh]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("agent_board")

#: Finestra di default per gli aggregati (giorni).
DEFAULT_DAYS = 7.0
#: TTL della mini-cache in-process (secondi). Il browser ricarica, il volume no.
DEFAULT_TTL_S = 15.0
#: Quante righe di registro leggere al massimo (le ultime). Il job gira ogni
#: 60s -> ~1440 righe/giorno: 20.000 copre abbondantemente la finestra.
DEFAULT_MAX_LINES = 20000

#: Ordine REALE della catena (chief_orchestrator._run), non alfabetico: e'
#: quello che rende leggibile "chi e' fermo e perche'".
PIPELINE = ("chief", "data", "strategy", "analysis", "brain",
            "finance", "sizing", "advisor", "execution")


@dataclass(frozen=True)
class AgentSpec:
    """Un agente della tabella: identita', contratto e chiave del registro."""

    id: str
    name: str
    role: str
    consumes: str
    produces: str
    section: str
    isolation: str
    #: Sezione che NON e' un dict (l'Advisor produce una lista).
    is_list: bool = False


AGENT_SPECS: tuple[AgentSpec, ...] = (
    AgentSpec(
        "chief", "Capo (Chief Orchestrator)",
        "Orchestratore unico: decide l'ordine delle fasi e ferma il ciclo",
        "safety + gate di mercato + agenti",
        "CycleReport (uno per ciclo)",
        "", "E' l'UNICO che compone gli agenti: nessun altro li chiama",
    ),
    AgentSpec(
        "data", "Dati & Mercato",
        "Feed sharp (SX) + segnali aperti dal ledger + intel live",
        "ledger `predictions`/`matches`, feed di mercato",
        "MarketData (gate, segnali, snapshot, intel)",
        "market", "Sola lettura: non scrive il ledger, non piazza ordini",
    ),
    AgentSpec(
        "strategy", "Strategia",
        "Filtra i segnali per tier giocabile",
        "segnali del Data Agent",
        "StrategyOutput (playable/rejected/unclassified)",
        "strategy", "Non conosce il bankroll: classifica, non dimensiona",
    ),
    AgentSpec(
        "analysis", "Analisi",
        "Steam velocity (dQ/dt) + juice/overround + freschezza",
        "segnali giocabili",
        "AnalysisOutput (steam, juice, stale)",
        "analysis", "Descrive il mercato; non decide e non ordina",
    ),
    AgentSpec(
        "brain", "Cervello",
        "EV dinamico (liquidita'/volatilita') + Portfolio Shield",
        "segnali arricchiti",
        "BrainOutput (validati/respinti/ridotti/bloccati)",
        "brain", "Puo' solo stringere (soglia EV >= base, shield <= 1)",
    ),
    AgentSpec(
        "finance", "Finanza",
        "Risk engine: verdetti approve/review/reject per segnale",
        "segnali validati",
        "FinanceOutput (piani + conteggi)",
        "finance", "Soglie lette dai moduli di produzione, mai ricopiate",
    ),
    AgentSpec(
        "sizing", "Sizing (Kelly)",
        "Size dei trade col motore Kelly frazionario",
        "trade validati (Cervello)",
        "SizingOutput (sized/executable/skipped)",
        "sizing", "Dimensiona il DATO: lo stake diventa ordine solo in corsia",
    ),
    AgentSpec(
        "advisor", "Advisor (braccio destro)",
        "Consiglia sui piani bloccati: micro-stake, market switch, contesto",
        "piani respinti/non eseguibili",
        "AdvisorResolution[] (una per blocco)",
        "advisor", "Può solo ESCALARE all'umano o proporre un piano sostitutivo",
        is_list=True,
    ),
    AgentSpec(
        "execution", "Esecuzione",
        "Instrada i comandi e legge gli ordini RESTING sul book",
        "piani approvati + recinto di esposizione",
        "ExecutionOutput (dispatched/executed/shadow/resting)",
        "execution", "Gateway shadow in questa fase: nessun ordine reale da qui",
    ),
)

_SPECS_BY_ID = {spec.id: spec for spec in AGENT_SPECS}

#: Precedenza delle AUTORITA' di blocco (stessa catena di `decision/guards`):
#: la si dichiara qui perche' la tabella le mostra nell'ordine in cui fermano.
BLOCK_PRECEDENCE = ("kill_switch", "daily_stop", "weekly_stop",
                    "settlement_pause", "market_gate")

_CACHE: dict[str, Any] = {"key": None, "at": None, "value": None}


# ---------------------------------------------------------------------------
# Configurazione (letta a RUNTIME: cambiare non richiede redeploy)
# ---------------------------------------------------------------------------
def _num_env(name: str, default: float, *, minimum: float = 0.0) -> float:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning("agent_board: %s=%r non numerico, uso %s", name, raw, default)
        return default
    if value < minimum:
        logger.warning("agent_board: %s=%r sotto il minimo %s, uso %s",
                       name, raw, minimum, default)
        return default
    return value


def window_days() -> float:
    """Finestra di default degli aggregati (`AGENT_BOARD_DAYS`, 7 giorni)."""
    return _num_env("AGENT_BOARD_DAYS", DEFAULT_DAYS, minimum=0.01)


def cache_ttl_s() -> float:
    """TTL della mini-cache (`AGENT_BOARD_TTL_S`). 0 = nessuna cache."""
    return _num_env("AGENT_BOARD_TTL_S", DEFAULT_TTL_S, minimum=0.0)


def cycle_log_path() -> Path:
    """Dove scrive i cicli il Capo (una sola definizione: chief_shadow_wiring)."""
    try:
        from chief_shadow_wiring import cycle_log_path as _clp
        return _clp()
    except Exception:
        try:
            from config import DATA_DIR
            base = Path(DATA_DIR)
        except Exception:
            base = Path(__file__).resolve().parent / "data"
        return base / "decision" / "chief_cycles.jsonl"


def reset_cache() -> None:
    """Azzera la mini-cache (test e diagnostica)."""
    _CACHE.update({"key": None, "at": None, "value": None})


# ---------------------------------------------------------------------------
# Lettura del registro dei cicli
# ---------------------------------------------------------------------------
def _parse_ts(value: Any) -> Optional[datetime]:
    """ISO -> datetime UTC aware; naive = UTC (il registro e' scritto cosi').

    Un timestamp illeggibile NON esclude il record: la finestra e' un filtro di
    comodo, non una regola di correttezza (scartare righe valide per una data
    rotta renderebbe la tabella vuota proprio in caso di guasto).
    """
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def read_cycles(days: Optional[float] = None, *,
                path: Optional[Path] = None,
                max_lines: int = DEFAULT_MAX_LINES) -> dict[str, Any]:
    """Cicli del Capo nella finestra: `{records, total, error, path}`.

    Rotation-aware (`telemetry_logs.read_lines` legge vivo + `.gz`, lezione del
    03/10) e fail-safe. `records` e' in ordine cronologico (il piu' recente per
    ultimo): e' quello che serve a "stato attuale" + "aggregati".
    """
    log_path = path or cycle_log_path()
    window = float(days if days is not None else window_days())
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(window, 0.0))
    out: dict[str, Any] = {"records": [], "total": 0, "window_days": window,
                           "path": str(log_path), "error": ""}
    try:
        from telemetry_logs import read_lines
        lines = read_lines(log_path)[-int(max_lines):]
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
        logger.warning("agent_board: registro cicli non leggibile (%s): %s",
                       log_path, exc)
        return out
    out["total"] = len(lines)
    records = []
    for line in lines:
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if not isinstance(rec, dict):
            continue
        ts = _parse_ts(rec.get("ts"))
        if ts is not None and ts < cutoff:
            continue
        if ts is not None:
            rec["_ts"] = ts.isoformat()
        records.append(rec)
    out["records"] = records
    return out


# ---------------------------------------------------------------------------
# Un agente: stato, metriche, contratto
# ---------------------------------------------------------------------------
def _section(rec: Optional[dict], spec: AgentSpec) -> Any:
    """La sezione del registro per questo agente (dict o lista)."""
    if not rec or not spec.section:
        return [] if spec.is_list else {}
    value = rec.get(spec.section)
    if spec.is_list:
        return value if isinstance(value, list) else []
    return value if isinstance(value, dict) else {}


def _num(value: Any) -> Optional[float]:
    """Numero normalizzato per il frontend (None se non numerico).

    Booleani esclusi di proposito: `True` non e' 1 in una tabella di indicatori
    (una spunta e' un'altra cosa da un conteggio) e l'ordine delle priorita'
    resta leggibile a colpo d'occhio.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _metric(key: str, label: str, value: Any, unit: str = "",
            tone: str = "neutral") -> dict[str, Any]:
    return {"key": key, "label": label, "value": value, "unit": unit,
            "tone": tone}


def _feed_label(gate: dict) -> tuple[str, str]:
    """Etichetta + tono del gate di mercato (nessun valore inventato)."""
    if not gate:
        return "sconosciuto", "neutral"
    allowed = bool(gate.get("allowed"))
    reason = str(gate.get("reason") or "").strip()
    if allowed:
        return "validato", "good"
    return (reason or "bloccato"), "bad"


def _duration_ms(rec: dict) -> Optional[float]:
    """Durata del ciclo in millisecondi (`None` se le due date non tornano)."""
    started = _parse_ts(rec.get("started_at"))
    finished = _parse_ts(rec.get("finished_at"))
    if not started or not finished:
        return None
    return round((finished - started).total_seconds() * 1000.0, 1)


def _metrics_for(spec: AgentSpec, section: Any, rec: dict) -> list[dict]:
    """Indicatori dell'agente, dalle SOLE chiavi che il contratto produce."""
    sec = section if isinstance(section, dict) else {}
    if spec.id == "chief":
        duration_ms = _duration_ms(rec)
        return [
            _metric("bankroll", "Bankroll", _num(rec.get("bankroll")), "USDC"),
            _metric("mode", "Modalita'", str(rec.get("mode") or "?"), ""),
            _metric("duration_ms", "Durata ciclo", duration_ms, "ms"),
            _metric("ok", "Esito", "ok" if rec.get("ok") else "bloccato", "",
                    "good" if rec.get("ok") else "bad"),
        ]
    if spec.id == "data":
        gate = sec.get("gate") or {}
        snapshot = sec.get("snapshot") or {}
        label, tone = _feed_label(gate)
        intel = sec.get("intel") or []
        return [
            _metric("signals", "Segnali aperti", _num(sec.get("signals")), ""),
            _metric("feed", "Feed di mercato", label, "", tone),
            _metric("accepted", "Quote accettate", _num(snapshot.get("accepted")), ""),
            _metric("rejected", "Quote respinte", _num(snapshot.get("rejected")), "",
                    "warn" if (_num(snapshot.get("rejected")) or 0) > 0 else "neutral"),
            _metric("intel", "Intel live", len(intel) if intel else 0, "match"),
        ]
    if spec.id == "strategy":
        return [
            _metric("total", "Segnali in ingresso", _num(sec.get("total")), ""),
            _metric("playable", "Giocabili", _num(sec.get("playable")), "",
                    "good" if (_num(sec.get("playable")) or 0) > 0 else "neutral"),
            _metric("rejected", "Respinti", _num(sec.get("rejected")), ""),
            _metric("unclassified", "Non classificati", _num(sec.get("unclassified")), "",
                    "warn" if (_num(sec.get("unclassified")) or 0) > 0 else "neutral"),
        ]
    if spec.id == "analysis":
        return [
            _metric("signals", "Segnali analizzati", _num(sec.get("signals")), ""),
            _metric("steam_moves", "Steam move", _num(sec.get("steam_moves")), "",
                    "good" if (_num(sec.get("steam_moves")) or 0) > 0 else "neutral"),
            _metric("juice_anomalies", "Anomalie juice",
                    _num(sec.get("juice_anomalies")), "",
                    "warn" if (_num(sec.get("juice_anomalies")) or 0) > 0 else "neutral"),
            _metric("stale", "Osservazioni stantie", _num(sec.get("stale")), "",
                    "warn" if (_num(sec.get("stale")) or 0) > 0 else "neutral"),
        ]
    if spec.id == "brain":
        return [
            _metric("trades", "Trade in uscita", _num(sec.get("trades")), ""),
            _metric("validated", "Validati", _num(sec.get("validated")), "",
                    "good" if (_num(sec.get("validated")) or 0) > 0 else "neutral"),
            _metric("rejected", "Respinti (EV)", _num(sec.get("rejected")), ""),
            _metric("scaled", "Ridotti (shield)", _num(sec.get("scaled")), "",
                    "warn" if (_num(sec.get("scaled")) or 0) > 0 else "neutral"),
            _metric("blocked", "Bloccati (correlazione)", _num(sec.get("blocked")), "",
                    "warn" if (_num(sec.get("blocked")) or 0) > 0 else "neutral"),
        ]
    if spec.id == "finance":
        return [
            _metric("plans", "Piani", _num(sec.get("plans")), ""),
            _metric("approved", "Approvati", _num(sec.get("approved")), "",
                    "good" if (_num(sec.get("approved")) or 0) > 0 else "neutral"),
            _metric("review", "In revisione", _num(sec.get("review")), ""),
            _metric("rejected", "Respinti", _num(sec.get("rejected")), ""),
        ]
    if spec.id == "sizing":
        return [
            _metric("trades", "Trade", _num(sec.get("trades")), ""),
            _metric("sized", "Dimensionati", _num(sec.get("sized")), ""),
            _metric("executable", "Eseguibili", _num(sec.get("executable")), "",
                    "good" if (_num(sec.get("executable")) or 0) > 0 else "neutral"),
            _metric("skipped", "Sotto ticket", _num(sec.get("skipped")), "",
                    "warn" if (_num(sec.get("skipped")) or 0) > 0 else "neutral"),
        ]
    if spec.id == "advisor":
        rows = section if isinstance(section, list) else []
        kinds: dict[str, int] = {}
        escalations = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            kind = str(row.get("override_kind") or "context")
            kinds[kind] = kinds.get(kind, 0) + 1
            if row.get("escalate_review"):
                escalations += 1
        return [
            _metric("advice", "Consigli", len(rows), ""),
            _metric("escalations", "Escalation umane", escalations, "",
                    "warn" if escalations else "neutral"),
            _metric("kinds", "Tipi", kinds, ""),
        ]
    if spec.id == "execution":
        return [
            _metric("dispatched", "Comandi instradati", _num(sec.get("dispatched")), ""),
            _metric("executed", "Eseguiti", _num(sec.get("executed")), "",
                    "good" if (_num(sec.get("executed")) or 0) > 0 else "neutral"),
            _metric("shadow", "Modalita'", "shadow" if sec.get("shadow") else "reale",
                    "", "neutral" if sec.get("shadow") else "warn"),
            _metric("resting_open", "Ordini RESTING", _num(sec.get("resting_open")), ""),
            _metric("resting_stake", "Capitale parcheggiato",
                    _num(sec.get("resting_stake")), "USDC"),
        ]
    return []


def _status_for(spec: AgentSpec, section: Any, rec: Optional[dict]) -> tuple[str, str]:
    """Stato + dettaglio leggibile per un agente.

    Stati: `ok` (ha prodotto), `skipped` (il ciclo si e' fermato PRIMA),
    `idle` (nessun input), `error`, `unknown` (nessun ciclo registrato). La
    distinzione `skipped` / `idle` e' il punto: "non e' stato chiamato perche'
    il ciclo era fermo" e "e' stato chiamato ma non c'era nulla" portano a due
    azioni diverse.
    """
    if rec is None:
        return "unknown", "nessun ciclo registrato (Chief shadow spento o registro vuoto)"
    produced = bool(section)
    if isinstance(section, dict) and section.get("error"):
        return "error", str(section.get("error"))
    blocked = str(rec.get("blocked_reason") or "")
    # Il CAPO non ha una "sezione" propria: la sua sezione E' il record. Senza
    # questo ramo un ciclo riuscito lo mostrava "in attesa" (misurato in
    # produzione il 21/10: `ok: true` nel ciclo e `chief: idle` in tabella),
    # cioe' la tabella diceva il contrario di quello che era successo.
    if spec.section == "":
        dur = _duration_ms(rec)
        detail = "ciclo eseguito"
        if isinstance(dur, float):
            detail += " in %.1fs" % (dur / 1000.0)
        if blocked:
            return "skipped", f"ciclo fermato: {blocked}"
        return ("ok" if rec.get("ok") else "skipped"), detail
    if produced:
        if spec.id == "advisor":
            return "ok", f"{len(section) if isinstance(section, list) else 0} consigli"
        return "ok", ""
    if spec.id == "advisor":
        if blocked:
            return "skipped", f"ciclo fermato: {blocked}"
        return "idle", "non interpellato: nessun piano bloccato"
    if blocked:
        return "skipped", f"ciclo fermato: {blocked}"
    return "idle", "nessun input in questo ciclo"


def _window_stats(spec: AgentSpec, records: list[dict]) -> dict[str, Any]:
    """Quanti cicli hanno prodotto un output per questo agente (finestra)."""
    produced = 0
    last_seen = ""
    if not spec.section:
        # Il CAPO "produce" in OGNI ciclo registrato (la sua sezione e' il
        # record): contarlo con `_section` lo avrebbe lasciato a 0 su 7 cicli
        # riusciti (misurato in produzione il 21/10).
        last = records[-1] if records else {}
        return {"cycles": len(records), "with_output": len(records),
                "last_seen": (str(last.get("_ts") or last.get("ts") or "")
                              or None)}
    for rec in records:
        section = _section(rec, spec)
        if section:
            produced += 1
            last_seen = str(rec.get("_ts") or rec.get("ts") or "")
    return {"cycles": len(records), "with_output": produced,
            "last_seen": last_seen or None}


def agent_cards(records: list[dict]) -> list[dict[str, Any]]:
    """Una card per agente, nell'ORDINE della catena."""
    last = records[-1] if records else None
    cards: list[dict[str, Any]] = []
    for spec in AGENT_SPECS:
        section = _section(last, spec)
        status, detail = _status_for(spec, section, last)
        cards.append({
            "id": spec.id,
            "name": spec.name,
            "role": spec.role,
            "consumes": spec.consumes,
            "produces": spec.produces,
            "isolation": spec.isolation,
            "status": status,
            "detail": detail,
            "last_seen": (last.get("_ts") or last.get("ts")) if last else None,
            "metrics": _metrics_for(spec, section, last or {}),
            "window": _window_stats(spec, records),
        })
    return cards


# ---------------------------------------------------------------------------
# Sicurezza, capitale, flusso (dalle LORO fonti, mai ricopiati)
# ---------------------------------------------------------------------------
def safety_snapshot() -> dict[str, Any]:
    """Autorita' di blocco in ordine di precedenza + catena dichiarata.

    Ogni voce e' letta dalla funzione che la governa (`auto_bet`): un valore
    ricopiato qui sarebbe un secondo posto da aggiornare, cioe' la classe di
    bug che questo progetto paga piu' cara.
    """
    out: dict[str, Any] = {"checks": [], "blocked_by": [],
                           "precedence": list(BLOCK_PRECEDENCE), "error": ""}
    checks: list[dict[str, Any]] = []

    def _add(key: str, label: str, active: bool, detail: str) -> None:
        checks.append({"id": key, "label": label,
                       "status": "blocked" if active else "clear",
                       "active": bool(active), "detail": detail})

    try:
        import auto_bet
    except Exception as exc:
        out["error"] = f"auto_bet non importabile: {type(exc).__name__}: {exc}"
        return out

    try:
        kill = auto_bet.kill_switch_status()
        effective = str(kill.get("effective") or "?")
        _add("kill_switch", "Kill switch", effective == "off",
             f"modalita' effettiva: {effective}"
             + ("" if kill.get("provider_ready") else " · provider NON pronto"))
    except Exception as exc:
        out["error"] = out["error"] or f"kill switch: {exc}"

    try:
        daily = auto_bet.daily_stop_status()
        _add("daily_stop", "Stop-loss giornaliero", bool(daily.get("stopped")),
             str(daily.get("reason") or f"soglia {daily.get('loss_pct')}%"))
    except Exception as exc:
        out["error"] = out["error"] or f"stop giornaliero: {exc}"

    try:
        weekly = auto_bet.weekly_stop_status()
        _add("weekly_stop", "Circuit breaker settimanale",
             bool(weekly.get("stopped")),
             str(weekly.get("reason") or "nessun drawdown oltre soglia"))
    except Exception as exc:
        out["error"] = out["error"] or f"stop settimanale: {exc}"

    try:
        from tracker import settlement_paused
        paused = bool(settlement_paused())
        _add("settlement_pause", "Pausa settlement", paused,
             "referto sospeso" if paused else "referto attivo")
    except Exception as exc:
        out["error"] = out["error"] or f"pausa settlement: {exc}"

    try:
        gate = auto_bet.market_gate_status() or {}
        allowed = bool(gate.get("allowed", True))
        detail = str(gate.get("detail") or gate.get("reason")
                     or ("validato" if allowed else "bloccato"))
        _add("market_gate", "Gate di mercato (feed)", not allowed, detail)
    except Exception as exc:
        out["error"] = out["error"] or f"gate di mercato: {exc}"

    out["checks"] = checks
    out["blocked_by"] = [c["id"] for c in checks if c["active"]]
    return out


def capital_snapshot() -> dict[str, Any]:
    """Capitale di lavoro: equity riconciliata, recinto, tetto per ordine.

    `equity` e' quella di `order_watch.current_equity()` = la STESSA base su cui
    il bot dimensiona (`auto_bet.sizing_equity`). Un audit che legge un'altra
    base misura un altro portafoglio (lezioni del 14/10 e del 16/10).
    """
    out: dict[str, Any] = {"available": False, "error": ""}
    try:
        from order_watch import current_equity
        equity = current_equity()
    except Exception as exc:
        out["error"] = f"equity non leggibile: {type(exc).__name__}: {exc}"
        return out
    if equity is None:
        out["error"] = "equity non leggibile (wallet/provider non disponibili)"
        return out
    out["available"] = True
    out["equity"] = round(float(equity), 4)
    try:
        import auto_bet
        status = auto_bet.open_exposure_status(float(equity))
        out.update({
            "open_stake": status.get("open_stake"),
            "open_orders": status.get("count"),
            "cap": status.get("cap"),
            "used_pct": status.get("pct"),
            "blocked": bool(status.get("blocked")),
            "hard_cap_per_order": auto_bet.ORDER_MAX_STAKE_USDC,
            "order_ceiling": auto_bet.order_ceiling(float(equity)),
            "cap_pct": auto_bet.OPEN_EXPOSURE_CAP_PCT,
        })
    except Exception as exc:
        out["error"] = f"recinto non leggibile: {type(exc).__name__}: {exc}"
    return out


def flow_snapshot() -> dict[str, Any]:
    """Flusso: decisioni della catena e residuo di refertazione."""
    out: dict[str, Any] = {}
    try:
        from tracker import decision_stats
        stats = decision_stats()
        out["decisions"] = {
            "n": stats.get("n"),
            "open": stats.get("open"),
            "by_verdict": stats.get("by_verdict") or {},
            "by_status": stats.get("by_status") or {},
            "with_order": stats.get("with_order"),
            "stake_total": stats.get("stake_total"),
        }
    except Exception as exc:
        out["decisions"] = {"error": f"{type(exc).__name__}: {exc}"}
    try:
        from tracker import settlement_residue
        residue = settlement_residue()
        open_rows = residue.get("open") or {}
        out["settlement"] = {
            "bets_open": (open_rows.get("bets") if isinstance(open_rows, dict)
                          else open_rows),
            "predictions_open": (open_rows.get("predictions")
                                 if isinstance(open_rows, dict) else None),
            "overdue_orphans": residue.get("overdue_orphans"),
            "estimated_credits": residue.get("estimated_credits"),
        }
    except Exception as exc:
        out["settlement"] = {"error": f"{type(exc).__name__}: {exc}"}
    return out


# ---------------------------------------------------------------------------
# Assemblaggio
# ---------------------------------------------------------------------------
def board(days: Optional[float] = None, *, path: Optional[Path] = None,
          refresh: bool = False) -> dict[str, Any]:
    """La tabella completa. Mai un'eccezione verso il chiamante."""
    window = float(days if days is not None else window_days())
    log_path = path or cycle_log_path()
    key = (round(window, 4), str(log_path))
    now = datetime.now(timezone.utc)
    ttl = cache_ttl_s()
    if not refresh and ttl > 0 and _CACHE.get("key") == key:
        at = _CACHE.get("at")
        if isinstance(at, datetime) and (now - at).total_seconds() <= ttl:
            cached = dict(_CACHE.get("value") or {})
            cached["cached"] = True
            return cached

    cycles = read_cycles(window, path=log_path)
    records = cycles.get("records") or []
    last = records[-1] if records else None
    payload: dict[str, Any] = {
        "generated_at": now.isoformat().replace("+00:00", "Z"),
        "window_days": window,
        "pipeline": list(PIPELINE),
        "composer": {
            "name": "Chief Orchestrator",
            "rule": "nessun agente importa un altro agente: i dati passano "
                    "solo dal Capo (direttiva 15/10/2026)",
            "mode": str((last or {}).get("mode") or "?"),
            "shadow": True,
        },
        "last_cycle": {
            "ts": (last.get("_ts") or last.get("ts")) if last else None,
            "ok": bool((last or {}).get("ok")),
            "blocked_reason": str((last or {}).get("blocked_reason") or ""),
            "bankroll": (last or {}).get("bankroll"),
            "mode": (last or {}).get("mode"),
        },
        "cycles": {
            "window": len(records),
            "lines_read": cycles.get("total"),
            "log": cycles.get("path"),
            "error": cycles.get("error") or "",
        },
        "agents": agent_cards(records),
        "safety": safety_snapshot(),
        "capital": capital_snapshot(),
        "flow": flow_snapshot(),
        "cached": False,
    }
    if ttl > 0:
        _CACHE.update({"key": key, "at": now, "value": payload})
    return payload


def format_report(bo: dict[str, Any]) -> str:
    """Versione testuale (Telegram / CLI), derivata dal payload."""
    last = bo.get("last_cycle") or {}
    lines = [
        "🏛️ TABELLA DI COMANDO",
        "Ultimo ciclo: %s · modo %s · bankroll %s"
        % (last.get("ts") or "—", last.get("mode") or "?",
           ("%.2f USDC" % last["bankroll"]) if isinstance(last.get("bankroll"), (int, float))
           else "—"),
    ]
    safety = bo.get("safety") or {}
    blocked = safety.get("blocked_by") or []
    checks = safety.get("checks") or []
    if blocked:
        active = [c["label"] for c in checks if c.get("active")]
        lines.append("🛑 Blocchi attivi: " + ", ".join(active))
    else:
        lines.append("🛡 Sicurezza: nessun blocco ("
                     + ", ".join(c["label"] for c in checks) + ")")
    cap = bo.get("capital") or {}
    if cap.get("available"):
        lines.append(
            "💼 Capitale: equity %.2f USDC · impegnato %.2f/%.2f (%.0f%%) · "
            "tetto ordine %.2f" % (
                float(cap.get("equity") or 0.0),
                float(cap.get("open_stake") or 0.0),
                float(cap.get("cap") or 0.0),
                100.0 * float(cap.get("used_pct") or 0.0),
                float(cap.get("order_ceiling") or 0.0)))
    for index, card in enumerate(bo.get("agents") or [], start=1):
        if card["id"] == "chief":
            detail = card.get("detail") or ""
            lines.append("%d. %-24s %s%s" % (index, card["name"], card["status"],
                                             (" — " + detail) if detail else ""))
            continue
        metrics = [m for m in (card.get("metrics") or [])
                   if m.get("key") != "kinds"]
        parts = []
        for m in metrics:
            value = m.get("value")
            if value is None or value == "":
                continue
            if isinstance(value, float):
                parts.append(f"{m['label']} {value:g}")
            else:
                parts.append(f"{m['label']} {value}")
        window = card.get("window") or {}
        tail = (" — " + card["detail"]) if card.get("detail") else ""
        lines.append("%d. %-24s %s%s%s" % (
            index, card["name"], card["status"], tail,
            (" · " + " · ".join(parts)) if parts else ""))
        if window.get("cycles"):
            lines[-1] += " [%s/%s cicli]" % (window.get("with_output"),
                                            window.get("cycles"))
    flow = bo.get("flow") or {}
    decisions = flow.get("decisions") or {}
    if decisions and not decisions.get("error"):
        lines.append("🧭 Decisioni: %s (aperte %s) · verdetti %s" % (
            decisions.get("n"), decisions.get("open"),
            decisions.get("by_verdict")))
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Tabella di comando degli agenti")
    parser.add_argument("--days", type=float, default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args(argv)

    bo = board(args.days, refresh=args.refresh)
    if args.json:
        print(json.dumps(bo, indent=2, ensure_ascii=False, default=str))
        return 0
    print(format_report(bo))
    if (bo.get("cycles") or {}).get("error"):
        print(f"\n⚠️ registro cicli: {(bo.get('cycles') or {}).get('error')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
