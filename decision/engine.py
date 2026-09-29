"""decision/engine.py — Il motore emette COMANDI, non esegue.

E' il punto d'ingresso della catena: prende un `Signal` e restituisce un
`CommandPlan` (record + comandi + eventuale blocco). Non tocca DB, provider,
Telegram o filesystem: **nessun side effect**, quindi si testa offline in
millisecondi e in shadow mode si puo' eseguire senza rischi.

Ordine delle operazioni (non negoziabile):

    1. FAIL FAST  — `guards.require_clear(kills, stage=BETTING)`
                    se un blocco e' attivo si esce SUBITO: nessun calcolo di
                    modello, rischio o stake, e il piano contiene solo l'audit
                    della decisione bloccata (+ notifica con anti-spam);
    2. MERCATO    — **refresh forzato del gateway** e gate (`feeds.py`):
                    la catena valuta solo su una quotatura fresca, conforme al
                    contratto e VALIDATA. Se il refresh fallisce, se lo snapshot
                    e' vecchio o se la serie di refresh conformi non e' ancora
                    completa, si esce bloccati (`FEED_*`) — e' fail-closed:
                    nessun dato di mercato = nessuna puntata;
    3. RISCHIO    — `pipeline.decide()` (gate + eventuale coda umana);
    4. COMANDI    — traduzione del record in effetti richiesti.

Perche' il gate di mercato sta DOPO il kill switch e PRIMA del rischio: le
autorita' umane (kill switch, stop-loss) devono rispondere per prime — un
problema tecnico di dati non puo' scavalcare un "no" dell'operatore — ma
nessun calcolo di rischio puo' avvenire su quotature che non abbiamo
verificato. Il blocco di mercato e' una regola della stessa catena
(`SafetyBlock`, precedenza 4), quindi finisce nell'audit come gli altri.

L'identita' del feed (request_id, trace_id, gateway_id, schema_version,
config_hash) viaggia nel piano (`CommandPlan.market`): ogni decisione resta
legata alla quotatura che l'ha motivata.

I comandi emessi dipendono dal verdetto:

| verdetto | comandi                                                        |
|---|---|
| bloccato | `persist_decision` + `notify_operators(blocked)` (1/giorno)     |
| reject   | `persist_decision` (nessuna notifica: sarebbe rumore)           |
| review   | `persist_decision` + `notify_operators(review_pending)`         |
| approve  | `persist_decision` (+ `place_order` SE eseguibile e modo live)  |

In modo `sim` NON si emette `place_order`: il ledger delle puntate simulate
resta di `auto_bet`. Il motore non inventa un percorso d'esecuzione parallelo.
"""

from __future__ import annotations

from typing import Any, Optional, Sequence

from . import guards, kill_switch as kill_switch_mod, pipeline
from .commands import (
    CommandPlan, notify_command, persist_decision_command, place_order_command,
    plan_for_record,
)
from .feeds import FeedSnapshot, MarketFeed, feed_enabled as feeds_enabled, verify_feed
from .limits import RiskLimits
from .middleware import Observability, TraceContext
from .models import (
    DecisionRecord, KillSwitchStatus, Mode, RiskDecision, Signal, as_float,
    risk_reject,
)
from .review_queue import ReviewQueue

#: Scope della notifica di blocco: stabile nel giorno -> 1 alert/giorno,
#: indipendentemente da quante volte il job valuta lo stesso blocco.
BLOCKED_NOTIFY_SCOPE = "safety-blocked"
#: Scope del blocco di mercato: separato da quello di sicurezza (un feed fermo e
#: un kill switch attivo sono due problemi diversi, entrambi da vedere).
MARKET_NOTIFY_SCOPE = "market-blocked"


def blocked_record(signal: Signal, block: guards.SafetyBlock, *,
                   kills: KillSwitchStatus, mode: Mode = "off",
                   provider: str = "") -> DecisionRecord:
    """Record di una decisione fermata dal fail-fast (verdetto `reject`).

    Il record esiste perche' anche un blocco e' una decisione: va scritta, col
    motivo machine-readable, altrimenti il feedback engine non sa distinguere
    "non ho giocato perche' il rischio era alto" da "non ho giocato perche'
    tutto era fermo".
    """
    risk: RiskDecision = risk_reject(block.reason, block.detail or block.label,
                                     checked=[f"guard:{block.name}"])
    return DecisionRecord(signal=signal, kill_switch=kills, risk=risk,
                          mode=mode, provider=provider)


def build_plan(signal: Signal, *, kills: Optional[KillSwitchStatus] = None,
               limits: Optional[RiskLimits] = None, bankroll: float = 0.0,
               mode: Optional[Mode] = None, review_queue: Optional[ReviewQueue] = None,
               provider: str = "", ml_confidence: Optional[float] = None,
               has_clv_positive: Optional[bool] = None,
               already_exposed: bool = False, home: str = "", away: str = "",
               observability: Optional[Observability] = None,
               ctx: Optional[TraceContext] = None,               feed: Optional[MarketFeed | FeedSnapshot] = None,
               feed_required: Optional[bool] = None) -> CommandPlan:
    """Valuta un segnale e restituisce il piano di comandi (nessun effetto).

    `feed` e' il gateway di mercato (`MarketFeed`) o uno snapshot gia' preso.
    Con un `MarketFeed` il refresh viene **forzato qui**, prima di qualunque
    calcolo di rischio. Il gate e' fail-closed: senza feed la catena si ferma.

    `feed_required=None` (default) significa "decidi dall'ambiente": con il
    feed attivo (`DECISION_FEED_ENABLED`, ON di default) il gate e' obbligatorio,
    con il feed spento la catena valuta senza dati di mercato — scelta esplicita,
    loggata e reversibile invece che implicita. Un feed PASSATO esplicitamente
    viene sempre verificato.
    """
    obs = observability or Observability()
    scope = ctx or obs.new_trace()
    limits = limits or RiskLimits.from_env()

    # 1. FAIL FAST: il kill switch risponde per primo (e lo stop-loss dopo).
    kills = kills or kill_switch_mod.status()
    try:
        with obs.span("guards", ctx=scope, stage="betting", mode=kills.mode) as guard_scope:
            guards.require_clear(kills, stage=guards.STAGE_BETTING)
            obs.event("guards.clear", ctx=guard_scope, stage="betting",
                      advisories=[b.name for b in guards.advisories(kills)])
    except guards.SafetyBlockError as blocked:
        obs.event("guards.blocked", ctx=scope, stage="betting",
                  outcome="blocked", reason=blocked.block.reason.value,
                  block=blocked.block.name, detail=blocked.block.detail,
                  hint=blocked.block.hint)
        record = blocked_record(signal, blocked.block, kills=kills,
                                mode=("off" if mode is None else mode),
                                provider=provider)
        commands = [
            persist_decision_command(record),
            notify_command(record, kind="blocked",
                           text=_blocked_text(signal, blocked.block),
                           scope=BLOCKED_NOTIFY_SCOPE),
        ]
        return plan_for_record(record, commands, blocked=blocked.block.as_json())

    # 2. MERCATO: refresh FORZATO del gateway, poi il gate (fail-closed).
    snapshot, market = _market_gate(feed, obs=obs, ctx=scope,
                                    request_id=scope.request_id,
                                    required=_feed_required(feed, feed_required))
    # Identita' del feed nel piano (None = gate non attivo: nessun dato di
    # mercato da tracciare, e il piano lo dice invece di lasciare un dict vuoto).
    market_identity = market.identity or None
    obs.event("feed.gate", ctx=scope, stage="market",
              outcome="allow" if market.allowed else "blocked",
              reason=market.reason.value, detail=market.detail, **market.identity)
    if not market.allowed:
        block = market.block or guards.SafetyBlock(
            reason=market.reason, stage="market", name="market_feed",
            label="gateway di mercato non affidabile", detail=market.detail)
        record = blocked_record(signal, block, kills=kills,
                                mode=("off" if mode is None else mode),
                                provider=provider)
        commands = [
            persist_decision_command(record),
            notify_command(record, kind="blocked",
                           text=_market_text(signal, market),
                           scope=f"{MARKET_NOTIFY_SCOPE}-{market.reason.value}"),
        ]
        return plan_for_record(record, commands, blocked=block.as_json(),
                               market=market_identity)

    # 3. RISCHIO (+ coda umana) — un solo span: `decide` e' il contratto.
    with obs.span("decide", ctx=scope, stage="risk", tier=signal.tier,
                  price=signal.price) as risk_span:
        record = pipeline.decide(
            signal, kills=kills, limits=limits, bankroll=bankroll, mode=mode,
            review_queue=review_queue, provider=provider,
            ml_confidence=ml_confidence, has_clv_positive=has_clv_positive,
            already_exposed=already_exposed)
    obs.event("decision", ctx=risk_span, stage="risk", outcome=record.risk.verdict,
              reason=record.risk.reason.value, mode=record.mode,
              stake=(as_float(record.stake.stake) if record.stake else None),
              executable=(record.stake.executable if record.stake else False),
              coverage=signal.data_quality.model_coverage,
              confidence=signal.confidence)

    # 4. COMANDI — un'unica fonte per la mappa record -> comandi.
    if record.risk.verdict == "review":
        commands = [persist_decision_command(record),
                    notify_command(record, kind="review_pending",
                                   text=_review_text(signal, record))]
    else:
        commands = plan_for_resolved(record, provider=provider,
                                     home=home, away=away).commands
    return plan_for_record(record, commands, market=market_identity)


def plan_for_resolved(record: DecisionRecord, *, provider: str = "",
                      home: str = "", away: str = "") -> CommandPlan:
    """Comandi per un record GIA' deciso (nessun ricalcolo, nessun side effect).

    E' il gemello di `build_plan` per il punto in cui un record arriva da fuori:
    l'approvazione umana di una revisione (`decision/review_telegram.py`), dove
    la decisione e' gia' stata presa e serve solo tradurla in comandi. La mappa
    record -> comandi resta scritta UNA volta sola, cosi' il percorso del
    callback non puo' divergere da quello del motore:

    | verdetto | comandi                                              |
    |---|---|
    | reject   | `persist_decision` (l'audit c'e' anche per un rifiuto)|
    | approve  | `persist_decision` + `place_order` SE eseguibile e live|

    Il modo `sim` non emette `place_order`: il ledger simulato resta di
    `auto_bet`. In shadow mode il comando viene emesso e *registrato* dal
    `ShadowGateway`, che e' esattamente il "sarebbe partito" che si vuole
    misurare.
    """
    commands = [persist_decision_command(record)]
    executable = bool(record.stake and record.stake.executable)
    if record.risk.verdict == "approve" and executable and record.mode == "live":
        commands.append(place_order_command(record, provider=provider,
                                            home=home, away=away))
    return plan_for_record(record, commands)


def _feed_required(feed: Optional[MarketFeed | FeedSnapshot],
                   feed_required: Optional[bool]) -> bool:
    """Il gate di mercato e' obbligatorio? Un feed passato lo e' sempre."""
    if feed_required is not None:
        return bool(feed_required)
    if feed is not None:
        return True
    return feeds_enabled()


def _market_gate(feed: Optional[MarketFeed | FeedSnapshot], *, obs: Observability,
                 ctx: TraceContext, request_id: str, required: bool = True
                 ) -> tuple[Optional[FeedSnapshot], Any]:
    """Refresh forzato + gate. Mai un'eccezione: l'esito e' il verdetto.

    Un oggetto di tipo inatteso e' trattato come feed ASSENTE (fail-closed):
    meglio una puntata non fatta che una presa su dati non verificati.
    """
    if isinstance(feed, MarketFeed):
        snapshot = feed.refresh(request_id=request_id, ctx=ctx)   # forzato
        return snapshot, feed.gate(snapshot, required=required)
    if isinstance(feed, FeedSnapshot):
        return feed, verify_feed(feed, required=required)
    if feed is not None:
        obs.event("feed.unknown", ctx=ctx, stage="market", outcome="error",
                  detail=f"tipo di feed inatteso: {type(feed).__name__}")
    return None, verify_feed(None, required=required)


def emit_many(signals: Sequence[Signal], *, kills: Optional[KillSwitchStatus] = None,
              limits: Optional[RiskLimits] = None, bankroll: float = 0.0,
              observability: Optional[Observability] = None,
              request_id: str = "",
              traces: Optional[dict[str, TraceContext]] = None,
              feed: Optional[MarketFeed | FeedSnapshot] = None,
              feed_required: Optional[bool] = None,
              **kwargs) -> list[CommandPlan]:
    """Piani per piu' segnali, con UNA istantanea di kill switch e limiti.

    Tutti gli span condividono il `request_id` (cio' che il chiamante vede come
    "un giro"), ma ognuno ha il proprio `trace_id`: e' cio' che permette di
    seguire una singola decisione dentro un job che ne valuta venti.

    Il dizionario `traces` (opzionale) riceve `{plan_id: TraceContext}`: serve
    a chi deve continuare la trace del piano (il dispatch dei comandi) invece
    di aprirne una nuova.
    """
    obs = observability or Observability()
    root = obs.new_trace(request_id=request_id)
    traces = traces if traces is not None else {}
    kills = kills or kill_switch_mod.status()
    limits = limits or RiskLimits.from_env()
    required = _feed_required(feed, feed_required)
    # UN refresh per GIRO (non uno per segnale): se `feed` e' un gateway si
    # forza qui, e ogni piano riceve lo stesso snapshot verificato.
    if isinstance(feed, MarketFeed):
        snapshot = feed.refresh(request_id=root.request_id, ctx=root)
        obs.event("feed.session", ctx=root, stage="market",
                  outcome="ok" if snapshot.ok else "error",
                  **snapshot.identity())
        feed = snapshot
    plans: list[CommandPlan] = []
    for signal in signals:
        # UNA trace per decisione (stesso request_id): dentro un giro che
        # valuta venti segnali, gli span di un segnale devono restare
        # riconoscibili (`plan_trace` + `emit_many(..., traces=...)`).
        plan_trace = obs.new_trace(request_id=root.request_id)
        plan = build_plan(signal, kills=kills, limits=limits, bankroll=bankroll,
                          observability=obs, ctx=plan_trace, feed=feed,
                          feed_required=required, **kwargs)
        traces[plan.plan_id] = plan_trace
        plans.append(plan)
    return plans


def _blocked_text(signal: Signal, block: guards.SafetyBlock) -> str:
    return (f"⛔ Puntate fermate dal fail-fast\n"
            f"  blocco   : {block.label} ({block.reason.value})\n"
            f"  dettaglio: {block.detail or '-'}\n"
            f"  ripresa  : {block.hint or '-'}\n"
            f"  segnale  : {signal.selection_label or signal.match_id} "
            f"@ {signal.price:.2f}")


def _market_text(signal: Signal, market: Any) -> str:
    """Notifica del blocco di mercato, con gli identificatori tracciati."""
    identity = market.identity or {}
    return (f"📡 Puntate ferme: gateway di mercato non affidabile\n"
            f"  motivo   : {market.reason.value} — {market.detail}\n"
            f"  gateway  : {identity.get('gateway_id') or '-'} "
            f"(sorgente {identity.get('source') or '-'})\n"
            f"  identita': request {identity.get('request_id') or '-'} | "
            f"trace {identity.get('trace_id') or '-'} | "
            f"schema {identity.get('schema_version') or '-'} | "
            f"config {identity.get('config_hash') or '-'}\n"
            f"  segnale  : {signal.selection_label or signal.match_id} "
            f"@ {signal.price:.2f}")


def _review_text(signal: Signal, record: DecisionRecord) -> str:
    stake = record.stake.stake if record.stake else 0.0
    return (f"🕓 Revisione umana richiesta\n"
            f"  segnale  : {signal.selection_label or signal.match_id} "
            f"({signal.league})\n"
            f"  quota    : {signal.price:.2f} | edge {signal.edge*100:+.1f}pp | "
            f"EV {signal.ev*100:+.1f}%\n"
            f"  motivo   : {record.risk.detail}\n"
            f"  stake se approvi: {stake:.2f} ({record.mode})")


__all__ = ["BLOCKED_NOTIFY_SCOPE", "MARKET_NOTIFY_SCOPE", "blocked_record",
           "build_plan", "emit_many", "plan_for_resolved"]
