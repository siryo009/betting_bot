"""decision/shadow.py — Shadow mode: la catena nuova accanto a quella che gira.

Scelta del proprietario (15/09/2026): la catena Command si collega ad `auto_bet`
in **shadow mode**. Significa che, a ogni giro, `auto_bet` valuta i segnali
aperti anche con la catena nuova, ne emette i comandi e li registra — ma
**l'esecuzione reale resta quella attuale**. Cosi' il confronto fra i due
percorsi e' misurato sui dati veri prima di sostituire qualcosa.

Tre garanzie, tutte verificate dai test:

1. **Nessun effetto reale**: i gateway sono lo `ShadowGateway` (registra) e
   basta — niente ordini, niente Telegram. Perche' di default non si scrive
   nemmeno sul ledger `decisions`: il job gira ogni 60s e lo stesso segnale
   verrebbe registrato mille volte al giorno. Il registro della shadow mode e'
   il suo JSONL, deduplicato per `dedup_key`.

   **Shadow Validation, opt-in** (`DECISION_SHADOW_PERSIST`, default OFF): con
   l'interruttore attivo la valutazione viene anche PERSISTITA sul ledger
   (stato `pending`) e subito convalidata dal gateway di storage
   (`ValidatingLedgerGateway`), con l'ordine registrato — mai eseguito — solo a
   convalida positiva (`require_persist=True`). La deduplicazione e' per
   **segnale** (`signal_id`), non per `record_id`: il `record_id` porta i
   secondi e cambia a ogni giro, quindi deduplicare su di esso non deduplicherebbe
   nulla. Limite noto e accettato: un segnale viene registrato alla PRIMA
   valutazione — se il prezzo si muove dopo, la riga non si aggiorna (una riga
   per opportunita', non un diario di ogni giro).
2. **Zero crediti** the-odds-api: del mercato si legge SOLO il feed primario
   (`decision/feeds.py`, SX pubblica: nessuna credenziale, nessun ordine). Con
   `DECISION_FEED_ENABLED=0` la chain valuta senza il gate di mercato (nessun
   accesso di rete: e' la modalita' dei test e delle diagnosi offline). Il gate
   di liquidita' dello Stake Engine continua a non scattare (`depth_usdc`
   arriva dal feed solo se interrogato). Tripwire: con il feed spento la rete
   NON viene toccata.
3. **Fail-safe totale**: qualunque errore torna come `{"error": ...}`, mai
   un'eccezione verso `auto_bet` (il giro puntate non si rompe per la
   telemetria).

Fail fast anche qui, e prima di tutto: se il kill switch o lo stop-loss sono
attivi, `run_shadow` esce **senza nemmeno interrogare il ledger** — non c'e'
nulla da confrontare quando le puntate sono ferme.

CLI: `venv/bin/python -m decision shadow [--json] [--days N]`.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from . import engine, guards, kill_switch as kill_switch_mod
from .commands import CommandKind
from .dispatcher import Dispatcher
from .feeds import (
    MarketFeed, FeedSnapshot, feed_enabled as feeds_enabled, feed_from_env,
)
from .gateways import ShadowGateway, shadow_log_path
from .limits import RiskLimits
from .middleware import Observability, TraceContext
from .models import Signal, as_float
from .review_queue import ReviewQueue

logger = logging.getLogger("decision.shadow")

SHADOW_ENABLED_ENV = "DECISION_SHADOW"
REVIEWS_ENABLED_ENV = "DECISION_REVIEWS"
SHADOW_PERSIST_ENV = "DECISION_SHADOW_PERSIST"
#: Percorso CLV laterale (17/09/2026): `evaluate_clv` -> `WriteCLVCommand` ->
#: `ClvGateway` IN PARALLELO alla catena, mai al posto di
#: `fixture_engine -> tracker.save_clv`. Il writer del gateway e' di default il
#: registro shadow: ZERO scritture sul ledger `clv_history` (l'unico writer
#: reale e' quello iniettato esplicitamente, es. dai test). La misura corre
#: comunque: esiti ok/skipped/rejected finiscono nel riepilogo e negli eventi,
#: cosi' il confronto con il percorso attuale e' misurabile prima di deciderlo.
CLV_ENV = "DECISION_CLV_SHADOW"


def clv_shadow_enabled(value: Optional[str] = None) -> bool:
    """Percorso CLV laterale attivo? Default SI': misura e registra, non scrive."""
    raw = os.getenv(CLV_ENV) if value is None else value
    if raw is None or not str(raw).strip():
        return True
    return str(raw).strip().lower() not in ("0", "false", "no", "off")


def shadow_enabled(value: Optional[str] = None) -> bool:
    """Shadow mode attiva di default (non esegue nulla: rischio nullo)."""
    raw = os.getenv(SHADOW_ENABLED_ENV) if value is None else value
    if raw is None or not str(raw).strip():
        return True
    return str(raw).strip().lower() not in ("0", "false", "no", "off")


def shadow_persist_enabled(value: Optional[str] = None) -> bool:
    """La shadow mode PERSISTE le valutazioni sul ledger? (opt-in: default NO).

    Default spento per non tradire il progetto del 15/09: la shadow mode non
    scrive sul ledger perche' il job gira ogni 60s. Chi accende l'interruttore
    accetta la deduplicazione per segnale (`signal_id`) come contropartita —
    vedi la docstring del modulo. L'interruttore e' esplicito in ENTRAMBE le
    direzioni: `1` accende, `0` spegne.
    """
    raw = os.getenv(SHADOW_PERSIST_ENV) if value is None else value
    if raw is None or not str(raw).strip():
        return False
    return str(raw).strip().lower() not in ("0", "false", "no", "off")


def reviews_enabled(value: Optional[str] = None) -> bool:
    """La coda delle revisioni umane si riempie? (default si).

    Senza, un verdetto `review` verrebbe registrato solo nel log: l'operatore
    non vedrebbe mai il bottone e la revisione resterebbe una nota a verbale.
    """
    raw = os.getenv(REVIEWS_ENABLED_ENV) if value is None else value
    if raw is None or not str(raw).strip():
        return True
    return str(raw).strip().lower() not in ("0", "false", "no", "off")


def run_shadow(*, signals: Optional[Sequence[Signal]] = None, bankroll: float = 0.0,
               mode: str = "sim", hours: float = 24.0, limit: Optional[int] = None,
               observability: Optional[Observability] = None,
               request_id: str = "", shadow_path: Optional[str | Path] = None,
               kills: Optional[Any] = None, limits: Optional[RiskLimits] = None,
               feed: Optional[Any] = None, feed_required: Optional[bool] = None,
               review_queue: Optional[Any] = None,
               reviews: Optional[bool] = None,
               persist: Optional[bool] = None,
               clv_enabled: Optional[bool] = None,
               clv_writer: Optional[Any] = None) -> dict:
    """Valuta i segnali aperti in shadow mode. NON esegue nulla, non solleva.

    Il **feed di mercato** (`decision/feeds.py`) e' la sorgente primaria dei
    dati di quotazione: se non viene iniettato e `DECISION_FEED_ENABLED` non e'
    a zero, la catena ne forza il refresh PRIMA di ogni valutazione di rischio
    e blocca il giro se il feed non e' fresco, conforme e validato.

    `persist` (default: ambiente, `DECISION_SHADOW_PERSIST`, OFF) accende la
    **Shadow Validation**: la valutazione viene scritta sul ledger con stato
    `pending` e poi convalidata, e l'ordine che ne deriva resta registrato (mai
    eseguito) solo a convalida positiva.

    **Percorso CLV laterale** (`clv_enabled`, default ambiente
    `DECISION_CLV_SHADOW`, ON): per ogni segnale valutato gira anche
    `decision.clv.evaluate_clv` (puro) e il comando `WriteCLVCommand` emesso
    viene instradato a un dispatcher dedicato con SOLO il `ClvGateway`, col
    writer agganciato al registro shadow (default) — nessuna scrittura su
    `clv_history`. Il riepilogo porta `clv` = {enabled, ok, skipped, rejected,
    dispatched, errors, avg_diff}: il confronto col percorso attuale
    (`fixture_engine -> tracker.save_clv`) resta una misura, non un'opinione.
    """
    obs = observability or Observability()
    ctx = obs.new_trace(request_id=request_id)
    out: dict[str, Any] = {"evaluated": 0, "plans": [], "by_verdict": {},
                           "by_command": {}, "shadow": True, "blocked": None,
                           "market": None, "market_blocked": None, "errors": [],
                           "reviews_queued": 0, "persisted": 0,
                           "persist_enabled": False, "persisted_duplicates": 0,
                           "order_blocked": 0,
                           "clv": {"enabled": False, "ok": 0, "skipped": 0,
                                   "rejected": 0, "dispatched": 0, "errors": 0,
                                   "avg_diff": None}}
    try:
        status = kills or kill_switch_mod.status()

        # FAIL FAST: nessun lavoro se le puntate sono ferme.
        block = guards.first(status, stage=guards.STAGE_BETTING)
        if block is not None:
            obs.event("shadow.blocked", ctx=ctx, outcome="blocked",
                      reason=block.reason.value, block=block.name,
                      detail=block.detail)
            out["blocked"] = block.as_json()
            return out
        advisories = [b.name for b in guards.advisories(status)]

        if signals is None:
            from .adapters import iter_signals
            signals = iter_signals(hours=hours, limits=limits)
        if limit is not None:
            signals = list(signals)[:limit]

        if not signals:
            # Nessun segnale aperto: non c'e' nulla da confrontare. Si esce
            # SENZA eventi — il job gira ogni 60s e 1440 giri/giorno di
            # "nessun segnale" sarebbero solo rumore sul volume.
            out["no_signals"] = True
            return out

        # FEED di mercato: refresh forzato prima del Risk Engine (un giro, non
        # uno per segnale). Se il feed e' disattivato si valuta senza il gate di
        # mercato — scelta esplicita e tracciata, mai silenziosa.
        market_feed = feed
        required = (market_feed is not None or feeds_enabled()) if feed_required is None \
            else bool(feed_required)
        if market_feed is None and required:
            market_feed = feed_from_env(observability=obs)
        if market_feed is None:
            obs.event("feed.disabled", ctx=ctx, stage="market",
                      detail="gate di mercato non attivo (DECISION_FEED_ENABLED=0): "
                             "la catena valuta senza quota verificata")
        elif getattr(market_feed, "has_sources", True) is False:
            obs.event("feed.nosources", ctx=ctx, stage="market", outcome="error",
                      detail="nessuna sorgente di mercato configurata (fail-closed)")

        # Coda delle revisioni umane: un verdetto `review` entra qui e diventa
        # un prompt Telegram con bottoni (`decision/review_telegram.py`). La
        # coda e' idempotente per segnale, quindi il giro ogni 60s non la
        # riempie di copie dello stesso segnale.
        if review_queue is None and (reviews if reviews is not None else reviews_enabled()):
            review_queue = ReviewQueue()

        # Shadow Validation (opt-in): con la persistenza attiva il gateway di
        # storage va registrato PRIMA dello ShadowGateway (il dispatcher sceglie
        # il primo che sa gestire il comando), cosi' il `persist_decision`
        # scrive davvero sul ledger mentre `place_order`/`notify_operators`
        # restano al registro shadow. `require_persist` impedisce che l'ordine
        # parta (rectius: venga registrato come partente) senza convalida.
        persist_enabled = shadow_persist_enabled() if persist is None else bool(persist)
        out["persist_enabled"] = persist_enabled
        gateways: list[Any] = [ShadowGateway(shadow_path)]
        already_persisted: Optional[Any] = None
        if persist_enabled:
            from .feedback import row_exists_for_signal
            from .gateways import ValidatingLedgerGateway
            gateways = [ValidatingLedgerGateway(), ShadowGateway(shadow_path)]
            already_persisted = row_exists_for_signal

        # Percorso CLV laterale (17/09): il comando va DIRETTAMENTE al gateway
        # CLV (audit-only) col writer shadow di default — MAI il writer reale
        # di `tracker.save_clv` da questo percorso (il CLV di produzione resta
        # di `fixture_engine`) — e viene REGISTRATO dallo stesso ShadowGateway
        # del giro (dedup per dedup_key: un campione per misura). Nessun
        # Dispatcher-finto: un comando, un gateway, esecuzione diretta.
        clv_on = clv_shadow_enabled() if clv_enabled is None else bool(clv_enabled)
        clv_gw = shadow_gw = None
        if clv_on:
            from .gateways import ClvGateway
            out["clv"]["enabled"] = True
            try:
                shadow_gw = next(g for g in gateways if isinstance(g, ShadowGateway))
            except StopIteration:
                shadow_gw = None

            def _shadow_clv_writer(payload: dict) -> dict:
                # Writer del percorso shadow: il campione finisce negli eventi
                # ( misura, non esecuzione). Mai `tracker.save_clv` qui.
                obs.event("clv.shadow_sample", stage="clv",
                          match_id=payload.get("match_id"),
                          outcome=payload.get("outcome"),
                          signal_odds=payload.get("signal_odds"),
                          closing_odds=payload.get("closing_odds"),
                          source=payload.get("source"))
                return {"saved": True, "shadow": True}

            clv_gw = ClvGateway(writer=clv_writer or _shadow_clv_writer)

        dispatcher = Dispatcher(gateways, observability=obs, require_persist=persist_enabled)
        obs.event("shadow.start", ctx=ctx, signals=len(signals), mode=mode,
                  bankroll=bankroll, path=str(shadow_path or shadow_log_path()),
                  advisories=advisories, persist=persist_enabled, clv=clv_on)

        clv_diffs: list[float] = []
        for signal in signals:
            # Un segnale -> una riga: senza questo controllo il job ogni 60s
            # riscriverebbe la stessa opportunita' mille volte al giorno.
            if already_persisted is not None and already_persisted(signal.signal_id):
                out["persisted_duplicates"] += 1
                out["plans"].append({
                    "record_id": "", "signal_id": signal.signal_id,
                    "match_id": signal.match_id, "outcome": signal.outcome,
                    "commands": [], "persisted": "duplicate", "would_order": False,
                })
                continue
            # Una trace per decisione (stesso request_id): guardie, rischio e
            # comandi di QUEL segnale restano leggibili insieme anche quando
            # il giro ne valuta molti.
            plan_trace = obs.new_trace(request_id=ctx.request_id)
            plan = engine.build_plan(signal, kills=status, limits=limits,
                                     bankroll=bankroll, mode=mode,   # type: ignore[arg-type]
                                     observability=obs, ctx=plan_trace,
                                     review_queue=review_queue,
                                     feed=market_feed, feed_required=required)
            report = dispatcher.dispatch(plan, ctx=plan_trace)
            if clv_gw is not None:
                # Closing del percorso CLV: la quota corrente del feed (lo
                # snapshot in-process del refresh forzato dalla catena). Senza
                # quote disponibili la valutazione e' `skipped`, onestamente.
                snapshot_ref = None
                if isinstance(market_feed, FeedSnapshot):
                    snapshot_ref = market_feed
                elif isinstance(market_feed, MarketFeed):
                    try:
                        snapshot_ref = market_feed.last_snapshot()
                    except Exception:
                        snapshot_ref = None
                diff = _run_clv_lateral(signal, clv_gw=clv_gw, shadow_gw=shadow_gw,
                                        out=out, obs=obs, ctx=plan_trace,
                                        snapshot=snapshot_ref)
                if diff is not None:
                    clv_diffs.append(diff)
            verdict = plan.record.risk.verdict
            if verdict == "review" and review_queue is not None:
                out["reviews_queued"] += 1
            if plan.market:
                out["market"] = plan.market
            if plan.blocked and plan.blocked.get("stage") == "market":
                out["market_blocked"] = plan.blocked.get("reason")
            out["by_verdict"][verdict] = out["by_verdict"].get(verdict, 0) + 1
            for command in plan.commands:
                key = command.kind.value
                out["by_command"][key] = out["by_command"].get(key, 0) + 1
            # Esito della persistenza/convalida di QUESTO piano (colonna
            # `status` del ledger): e' cio' che la Shadow Validation aggiunge
            # al registro, senza cambiare come vengono eseguiti i comandi.
            persist_results = report.of_kind(CommandKind.PERSIST_DECISION)
            decision_status = ""
            if persist_results:
                decision_status = str(persist_results[0].data.get("decision_status") or "")
                if persist_results[0].ok:
                    out["persisted"] += 1
            if report.aborted:
                out["order_blocked"] += 1
            out["plans"].append({
                "record_id": plan.record.record_id,
                "signal_id": signal.signal_id,
                "blocked": (plan.blocked or {}).get("reason"),
                "match_id": signal.match_id,
                "outcome": signal.outcome,
                "verdict": verdict,
                "reason": plan.record.risk.reason.value,
                "commands": plan.kinds(),
                "would_order": plan.places_order,
                "order_blocked": report.aborted,
                "decision_status": decision_status,
                "stake": (as_float(plan.record.stake.stake) if plan.record.stake else 0.0),
                "executed": report.executed,
                "duplicated": report.duplicated,
            })
            out["errors"].extend(report.errors)
            out["evaluated"] += 1

        if clv_diffs:
            out["clv"]["avg_diff"] = round(sum(clv_diffs) / len(clv_diffs), 4)
        obs.event("shadow.end", ctx=ctx, outcome="ok" if not out["errors"] else "error",
                  evaluated=out["evaluated"], verdicts=out["by_verdict"],
                  commands=out["by_command"], errors=len(out["errors"]),
                  reviews_queued=out["reviews_queued"],
                  market_blocked=out["market_blocked"],
                  persist_enabled=out["persist_enabled"],
                  persisted=out["persisted"],
                  persisted_duplicates=out["persisted_duplicates"],
                  order_blocked=out["order_blocked"],
                  clv=dict(out["clv"]), **(out["market"] or {}))
        return out
    except Exception as exc:                       # la shadow non rompe mai il job
        logger.warning("shadow: valutazione fallita (%s)", exc)
        out["errors"].append(str(exc))
        obs.event("shadow.error", ctx=ctx, outcome="error",
                  error=f"{type(exc).__name__}: {exc}")
        return out


# ---------------------------------------------------------------------------
# Percorso CLV laterale: valutazione -> comando -> gateway (audit-only)
# ---------------------------------------------------------------------------

def _clv_closing(snapshot: Any, signal: Signal) -> Optional[float]:
    """Closing line del percorso CLV: la quota corrente del feed per l'esito.

    RESTITUISCE solo una quota (puo' essere None), non eccezioni mai: e' un
    arricchimento della valutazione, non un prerequisito. La selezione e'
    deterministica: stesso `event_id`, mercato 1X2, stessa selezione (`quote_
    for` del feed). Con lo snapshot riusato dalla finestra (6oo secondi) la
    quota e' comunque dentro il limite di freschezza del gate.
    """
    if snapshot is None:
        return None
    try:
        quote = snapshot.quote_for(signal.match_id, "1X2", signal.outcome)
    except Exception:
        return None
    if quote is None:
        return None
    try:
        value = float(quote.odds)
    except (TypeError, ValueError):
        return None
    return value if value > 1.0 else None


def _run_clv_lateral(signal: Signal, *, clv_gw: Any, shadow_gw: Any, out: dict,
                     obs: Observability, ctx: TraceContext,
                     snapshot: Any = None) -> Optional[float]:
    """Valutazione CLV per UN segnale: puro -> comando -> gateway audit-only.

    Esecuzione DIRETTA sul `ClvGateway` (un comando, un gateway: il dispatcher
    instrada piani, qui non c'e' niente da instradare) e REGISTRAZIONE del
    comando sullo stesso `ShadowGateway` del giro, cosi' il registro JSONL
    mostra anche cio' che il percorso CLV avrebbe scritto. Contatore in
    `out["clv"]` (ok/skipped/rejected/dispatched/errors) e span con
    tracciabilita' (request/trace/span id). NON tocca mai `clv_history`: il
    writer del gateway, in questo percorso, e' quello shadow. Ritorna la
    differenza di quota se misurata (per la media del riepilogo).
    """
    from .clv import ClvInput, evaluate_clv
    try:
        evaluation = evaluate_clv(ClvInput(
            signal_id=signal.signal_id,
            market_id=signal.match_id,
            outcome=signal.outcome,
            signal_odds=float(signal.price),
            closing_odds=_clv_closing(snapshot, signal),
            source="decision-shadow",
        ))
    except Exception as exc:                       # mai rompere il giro shadow
        out["clv"]["rejected"] += 1
        out["clv"]["errors"] += 1
        obs.event("clv.lateral_error", ctx=ctx, stage="clv", outcome="error",
                  error=f"{type(exc).__name__}: {exc}")
        return None

    status = evaluation.status
    key = "ok" if status == "ok" else status
    if key in out["clv"]:
        out["clv"][key] += 1
    if not evaluation.emitted:
        obs.event("clv.lateral", ctx=ctx, stage="clv", outcome=status,
                  reason=evaluation.reason, detail=evaluation.detail,
                  signal_id=signal.signal_id, match_id=signal.match_id)
        return None

    with obs.span("clv.write", ctx=ctx, stage="clv", outcome="ok",
                  signal_id=signal.signal_id, match_id=signal.match_id,
                  source="decision-shadow") as span:
        result = clv_gw.execute(evaluation.dispatch, ctx=span, obs=obs)
        # Registrazione nel registro shadow (dedup per dedup_key): e' il
        # "sarebbe stato scritto" che si confronta a fine fase. Fail-safe:
        # un registro non scrivibile non invalida la misura.
        if shadow_gw is not None:
            try:
                shadow_gw.execute(evaluation.dispatch, ctx=span, obs=obs)
            except Exception as exc:
                logger.warning("clv: registrazione shadow fallita (%s)", exc)
    dispatched = bool(result.ok and result.status == "executed")
    if dispatched:
        out["clv"]["dispatched"] += 1
    else:
        out["clv"]["errors"] += 1
    obs.event("clv.lateral", ctx=span, stage="clv", outcome="ok" if dispatched else "error",
              reason=evaluation.reason, dispatched=dispatched,
              gateway=result.gateway, status=result.status,
              clv_diff=evaluation.clv_diff, flags=list(evaluation.flags),
              detail=result.detail)
    return evaluation.clv_diff


# ---------------------------------------------------------------------------
# Lettura del registro (per il confronto misurato)
# ---------------------------------------------------------------------------

def iter_shadow_commands(path: Optional[str | Path] = None, *,
                         limit: int = 500) -> list[dict]:
    """Comandi registrati, piu' recenti prima (righe corrotte ignorate)."""
    target = Path(path) if path else shadow_log_path()
    if not target.exists():
        return []
    entries: list[dict] = []
    try:
        with open(target, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                if isinstance(entry, dict):
                    entries.append(entry)
    except Exception as exc:
        logger.warning("shadow: registro non leggibile (%s)", exc)
        return []
    entries.reverse()
    return entries[:limit]


def shadow_summary(path: Optional[str | Path] = None, *, limit: int = 500) -> dict:
    """Riepilogo del registro: comandi per tipo, esiti, segnali distinti."""
    entries = iter_shadow_commands(path, limit=limit)
    by_kind: dict[str, int] = {}
    by_verdict: dict[str, int] = {}
    verdicts: dict[str, int] = {}
    signals: set[str] = set()
    would_order = 0
    for entry in entries:
        command = entry.get("command") or {}
        kind = str(command.get("kind") or "?")
        by_kind[kind] = by_kind.get(kind, 0) + 1
        payload = command.get("payload") or {}
        if kind == "place_order":
            would_order += 1
        for key in ("verdict", "reason"):
            value = payload.get(key)
            if value:
                by_verdict[value] = by_verdict.get(value, 0) + 1
        if command.get("signal_id"):
            signals.add(str(command["signal_id"]))
    return {
        "entries": len(entries),
        "by_kind": by_kind,
        "by_verdict": by_verdict,
        "distinct_signals": len(signals),
        "would_order": would_order,
        "first_ts": (entries[-1].get("ts") if entries else None),
        "last_ts": (entries[0].get("ts") if entries else None),
    }


def format_report(summary_or_path: Any = None) -> str:
    """Report Telegram-friendly del registro shadow."""
    data = summary_or_path if isinstance(summary_or_path, dict) else shadow_summary(summary_or_path)
    lines = ["👻 Shadow mode (nessuna esecuzione reale)",
             f"  comandi registrati: {data.get('entries', 0)} "
             f"(segnali distinti {data.get('distinct_signals', 0)})"]
    by_kind = data.get("by_kind") or {}
    if by_kind:
        lines.append("  per tipo: " + " | ".join(f"{k} {v}" for k, v in sorted(by_kind.items())))
    if data.get("would_order"):
        lines.append(f"  ordini che SAREBBERO partiti: {data['would_order']}")
    verdicts = data.get("by_verdict") or {}
    if verdicts:
        lines.append("  esiti: " + " | ".join(f"{k} {v}" for k, v in sorted(verdicts.items())))
    if data.get("last_ts"):
        lines.append(f"  ultimo comando: {data['last_ts']}")
    return "\n".join(lines)


__all__ = [
    "CLV_ENV", "REVIEWS_ENABLED_ENV", "SHADOW_ENABLED_ENV", "SHADOW_PERSIST_ENV",
    "clv_shadow_enabled", "format_report", "iter_shadow_commands",
    "reviews_enabled", "run_shadow", "shadow_enabled",
    "shadow_persist_enabled", "shadow_summary",
]

