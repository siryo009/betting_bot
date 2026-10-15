"""drawdown_rebalancer.py — M6 FASE 4: Dynamic Drawdown Rebalancing (15/10/2026).

Direttiva del proprietario: monitorare lo stato di salute e l'equity della cassa
e **scalare automaticamente al ribasso gli stake** quando il portafoglio entra in
drawdown, proteggendo il capitale residuo.

PERCHE' QUESTO NON E' UNA SECONDA FORMULA (regola di progetto: "ogni formula vive
in UN solo posto" — i tre bug gravi del progetto sono tutti logica copiata in due
posti):
- la MISURA del drawdown e' `auto_bet.weekly_drawdown` (picco rolling sulle
  ultime `WEEKLY_STOP_WINDOW_H` ore, campioni max 1/h, riconciliazione
  dell'equity inclusa): qui non si ricopia, si legge;
- la CURVA storica a una soglia (10% -> riduzione lineare fino al 50%) e'
  `adaptive_staking.drawdown_factor`, e resta al suo posto: il modulo la espone
  nel verdetto come `factor_legacy` per il confronto, non la riapplica;
- lo stop di giornata e quello settimanale restano dove sono (bloccano le
  puntate, non ridimensionano): questo modulo **ridimensiona**, non blocca.

COSA AGGIUNGE: una curva a **piu' soglie** (tier), perche' la protezione lineare
a soglia singola non distingue un -6% da un -25%: sopra il primo tier la
riduzione cresce a scaglioni fino al pavimento, e sotto il pavimento l'ordine
viene **bloccato** invece che ordinato a un importo che non protegge nulla.

DIREZIONE UNICA (come `copula_portfolio` e `risk_parity`): il moltiplicatore e'
in **[0, 1]** — puo' solo RIDURRE o AZZERARE. Mai un fattore > 1: alzare lo stake
dopo un drawdown sarebbe l'opposto di un controllo di rischio, e un modulo che
lo facesse sarebbe una decisione di strategia, non un'integrazione.

FAIL-OPEN dichiarato: senza una misura del drawdown (storico assente, equity non
leggibile, file corrotto) i candidati restano **invariati**. La stessa direzione
di `apply_correlation_cap` e della copula: un guasto di telemetria non e' un
rischio misurato e non deve fermare il bot. La direzione opposta (fail-closed)
vale invece per l'APPLICAZIONE di una riduzione gia' calcolata: quella non si
"dimentica".

Confini (tripwire nei test): nessuna scrittura, zero rete, zero crediti, **zero
ordini**, nessuna soglia di strategia nel sorgente, `import drawdown_rebalancer`
non carica `auto_bet`/`bot`/`tracker`/`odds_api`.

CLI:
    venv/bin/python drawdown_rebalancer.py [--bankroll N] [--json]
    venv/bin/python drawdown_rebalancer.py --curve     # solo la curva
"""
from __future__ import annotations

import json
import logging
import math
import os
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configurazione (env letta a RUNTIME: invertire non richiede un redeploy)
# ---------------------------------------------------------------------------
DEFAULT_MODE = "enforce"
DEFAULT_MIN_MULTIPLIER = 0.10       # pavimento della CURVA
DEFAULT_MIN_TICKET = 1.0            # sotto questo stake ridotto -> ORDINE BLOCCATO
DEFAULT_TTL_S = 300                 # memo della misura (il giro gira ogni 60s)

# CURVA A SOGLIE: `drawdown -> moltiplicatore`. Sotto il primo tier nessuna
# riduzione (1.0); fra due tier interpolazione LINEARE; oltre l'ultimo tier il
# pavimento. Le soglie NON sono inventate: sono quelle che il progetto usa gia'
# per fermare le puntate, cosi' la riduzione e' visibile PRIMA che un'autorita'
# superiore (stop giornaliero, breaker settimanale) chiuda il giro:
#  - 5%  = `DAILY_STOP_LOSS_PCT` (stop giornaliero);
#  - 12% = `WEEKLY_STOP_LOSS_PCT` (circuit breaker settimanale);
#  - 20% = zona in cui il progetto ha gia' osservato un arresto reale (CB2).
DEFAULT_TIERS: Tuple[Tuple[float, float], ...] = (
    (0.05, 0.85),
    (0.10, 0.60),
    (0.12, 0.50),
    (0.15, 0.40),
    (0.20, 0.25),
    (0.30, 0.10),
)

DEFAULT_TIER_CSV = ",".join("%s:%s" % (d, m) for d, m in DEFAULT_TIERS)

_MEMO: Dict[str, Any] = {"stamp": None, "data": None}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float, *, minimum: float = 0.0,
               maximum: Optional[float] = None) -> float:
    """Numero da env: valore impossibile -> default con warning (mai silenzioso)."""
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning("drawdown_rebalancer: %s non numerico (%r): uso %s",
                       name, raw, default)
        return default
    if not math.isfinite(value) or value < minimum or (
            maximum is not None and value > maximum):
        logger.warning("drawdown_rebalancer: %s fuori range (%s): uso %s",
                       name, raw, default)
        return default
    return value


def parse_tiers(raw: Any) -> Tuple[Tuple[float, float], ...]:
    """Curva da stringa `dd:mult,dd:mult,...`.

    Una voce malformata viene SCARTATA con warning (non si indovina un tier) e
    una curva vuota o non crescente ricade sul default: una protezione non si
    spegne con un env sbagliato.
    """
    if raw is None or str(raw).strip() == "":
        return DEFAULT_TIERS
    out: List[Tuple[float, float]] = []
    for chunk in str(raw).split(","):
        item = chunk.strip()
        if not item:
            continue
        parts = item.split(":")
        if len(parts) != 2:
            logger.warning("drawdown_rebalancer: tier malformato %r: scartato", item)
            continue
        try:
            dd, mult = float(parts[0]), float(parts[1])
        except (TypeError, ValueError):
            logger.warning("drawdown_rebalancer: tier non numerico %r: scartato",
                           item)
            continue
        if not (math.isfinite(dd) and math.isfinite(mult)) or dd <= 0 or \
                not (0.0 <= mult <= 1.0):
            logger.warning("drawdown_rebalancer: tier fuori dominio %r: scartato",
                           item)
            continue
        out.append((dd, mult))
    out.sort(key=lambda x: x[0])
    if not out:
        logger.warning("drawdown_rebalancer: curva vuota: uso il default")
        return DEFAULT_TIERS
    return tuple(out)


def tiers() -> Tuple[Tuple[float, float], ...]:
    """Curva efficace (env `DRAWDOWN_REBALANCE_TIERS`)."""
    return parse_tiers(os.getenv("DRAWDOWN_REBALANCE_TIERS"))


def config() -> Dict[str, Any]:
    """Configurazione efficace (una sola lettura per decisione)."""
    mode = (os.getenv("DRAWDOWN_REBALANCE_MODE") or DEFAULT_MODE).strip().lower()
    if mode not in ("enforce", "shadow", "off"):
        logger.warning("drawdown_rebalancer: modo sconosciuto %r: uso %s",
                       mode, DEFAULT_MODE)
        mode = DEFAULT_MODE
    enabled = _env_bool("DRAWDOWN_REBALANCE", True)
    return {
        "enabled": enabled,
        "mode": mode,
        "active": bool(enabled and mode != "off"),
        "min_multiplier": _env_float("DRAWDOWN_REBALANCE_MIN_MULTIPLIER",
                                     DEFAULT_MIN_MULTIPLIER, minimum=0.0,
                                     maximum=1.0),
        "min_ticket": _env_float("DRAWDOWN_REBALANCE_MIN_TICKET_USDC",
                                 DEFAULT_MIN_TICKET, minimum=0.0),
        "tiers": tiers(),
        "ttl_s": _env_float("DRAWDOWN_REBALANCE_TTL_S", DEFAULT_TTL_S,
                            minimum=0.0),
    }


def reset_cache() -> None:
    """Azzera la memo della misura (test e diagnostica)."""
    _MEMO["stamp"] = None
    _MEMO["data"] = None


# ---------------------------------------------------------------------------
# 1) Misura del drawdown (delega: nessuna seconda formula)
# ---------------------------------------------------------------------------
def drawdown_state(*, bankroll: Optional[float] = None,
                   now: Any = None,
                   use_cache: bool = True,
                   cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Drawdown rolling + il fattore della curva storica, MISURATI altrove.

    Ritorna `{ok, reason, drawdown_pct, peak, current, n_samples, factor_legacy,
    source}`. `factor_legacy` e' il fattore di `adaptive_staking.drawdown_factor`
    (curva a soglia singola, gia' in produzione): il modulo lo ESPONE per il
    confronto e per l'audit, non lo riapplica.

    Fail-safe: qualunque errore -> `ok=False` con motivo dichiarato (il
    chiamante lascia i candidati invariati).
    """
    conf = cfg or config()
    # La memo e' per-BANKROLL: il picco rolling dipende dal valore corrente
    # (partecipa al picco), quindi una misura presa per un bankroll diverso non
    # e' la stessa misura. `now` esplicito disattiva la memo (diagnostica e
    # test: il tempo non si indovina).
    key = None if now is not None else ("bk", round(float(bankroll or 0.0), 6))
    if use_cache and key is not None and _MEMO["stamp"] == key and \
            _MEMO["data"] is not None:
        return dict(_MEMO["data"])
    out: Dict[str, Any] = {"ok": False, "reason": None, "drawdown_pct": None,
                           "peak": None, "current": None, "n_samples": 0,
                           "factor_legacy": None, "source": None}
    try:
        import auto_bet
        dd = auto_bet.weekly_drawdown(bankroll, now=now)
        out.update({"peak": dd.get("peak"), "current": dd.get("current"),
                    "n_samples": int(dd.get("n_samples") or 0),
                    "drawdown_pct": dd.get("drawdown_pct"),
                    "source": "auto_bet.weekly_drawdown"})
        try:
            import adaptive_staking
            peak = dd.get("peak") or 0.0
            current = dd.get("current")
            if peak and current is not None:
                out["factor_legacy"] = float(
                    adaptive_staking.drawdown_factor(float(current),
                                                     float(peak)))
        except Exception as e:                                # pragma: no cover
            logger.debug("drawdown_rebalancer: fattore storico non leggibile "
                         "(%s)", e)
    except Exception as e:                                    # pragma: no cover
        logger.debug("drawdown_rebalancer: misura drawdown non riuscita (%s)", e)
        out["reason"] = "misura_non_disponibile"
        return out
    peak, current = out.get("peak"), out.get("current")
    if peak is None or current is None or float(peak) <= 0:
        out["reason"] = "storico_insufficiente"
        return out
    if int(out["n_samples"]) < 2:
        out["reason"] = "campione_insufficiente"
        return out
    out["ok"] = True
    out["reason"] = "ok"
    if use_cache and key is not None:
        _MEMO["stamp"] = key
        _MEMO["data"] = dict(out)
    return out


# ---------------------------------------------------------------------------
# 2) Curva a soglie
# ---------------------------------------------------------------------------
def multiplier_for(drawdown: Any, *, cfg: Optional[Dict[str, Any]] = None,
                   curve: Optional[Sequence[Tuple[float, float]]] = None,
                   ) -> Dict[str, Any]:
    """Moltiplicatore per un drawdown dato (interpolazione lineare fra i tier).

    Proprieta' garantite (e testate):
    - il risultato e' SEMPRE in `[pavimento, 1.0]` — mai amplificazione;
    - sotto il primo tier il fattore e' esattamente **1.0** (nessuna riduzione
      su un drawdown che il progetto non considera ancora un problema);
    - oltre l'ultimo tier vale il pavimento (`min_multiplier`), e se lo stake
      ridotto scende sotto `min_ticket` l'ordine viene **BLOCCATO** a valle:
      mai un importo diverso dalla riduzione per farlo passare.
    """
    conf = cfg or config()
    dd = None
    try:
        dd = float(drawdown)
    except (TypeError, ValueError):
        dd = None
    if dd is None or not math.isfinite(dd):
        return {"ok": False, "reason": "drawdown_illeggibile",
                "multiplier": 1.0, "drawdown": None, "tier": None}
    dd = max(0.0, dd)
    pts = list(curve if curve is not None else conf["tiers"])
    if not pts:
        return {"ok": False, "reason": "curva_vuota", "multiplier": 1.0,
                "drawdown": dd, "tier": None}
    # Il bordo della PRIMA soglia appartiene alla curva (`5% -> x0.85`), non
    # al tratto "nessuna riduzione": con `<=` il primo tier sarebbe
    # irraggiungibile e la tabella direbbe una cosa diversa da quella che
    # applica (la classe di difetto dell'etichetta del 13/09).
    if dd < pts[0][0]:
        return {"ok": True, "reason": "sotto_prima_soglia", "multiplier": 1.0,
                "drawdown": dd, "tier": None}
    if dd >= pts[-1][0]:
        mult = max(float(conf["min_multiplier"]), float(pts[-1][1]))
        return {"ok": True, "reason": "oltre_ultima_soglia",
                "multiplier": min(1.0, mult), "drawdown": dd,
                "tier": pts[-1][0]}
    for (d0, m0), (d1, m1) in zip(pts, pts[1:]):
        if d0 <= dd <= d1:
            span = (d1 - d0) or 1.0
            t = (dd - d0) / span
            mult = m0 + t * (m1 - m0)
            return {"ok": True, "reason": "interpolato",
                    "multiplier": max(0.0, min(1.0, mult)), "drawdown": dd,
                    "tier": d1}
    return {"ok": False, "reason": "curva_non_monotona", "multiplier": 1.0,
            "drawdown": dd, "tier": None}


def curve_table(*, cfg: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """La curva come tabella leggibile (per report e CLI)."""
    conf = cfg or config()
    return [{"drawdown_pct": round(d * 100.0, 2),
             "multiplier": float(m),
             "stake_su_10_usdc": round(10.0 * float(m), 2)}
            for d, m in conf["tiers"]]


# ---------------------------------------------------------------------------
# 3) Applicazione ai candidati (solo riduzione)
# ---------------------------------------------------------------------------
def _stake_of(cand: Dict[str, Any]) -> float:
    try:
        value = float(cand.get("stake") or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return value if math.isfinite(value) and value > 0 else 0.0


def apply(candidates: List[Dict[str, Any]], *, bankroll: Optional[float] = None,
          cfg: Optional[Dict[str, Any]] = None,
          state: Optional[Dict[str, Any]] = None,
          now: Any = None) -> Tuple[List[Dict], Dict]:
    """Scala al ribasso gli stake in drawdown — MAI li alza.

    Ritorna `(candidati, info)`. Con drawdown sotto la prima soglia il
    moltiplicatore e' 1.0 e i candidati tornano INVARIATI (nessun tocco, nessun
    campo aggiunto: un giro in salute deve essere indistinguibile da un giro
    senza il modulo). Fail-open su misura non disponibile.
    """
    conf = cfg or config()
    info: Dict[str, Any] = {"enabled": bool(conf["enabled"]),
                            "active": bool(conf["active"]), "mode": conf["mode"],
                            "kept": len(candidates or []), "scaled": 0,
                            "blocked": 0, "drawdown_pct": None,
                            "multiplier": 1.0, "factor_legacy": None,
                            "reason": None, "error": None,
                            "min_multiplier": conf["min_multiplier"],
                            "min_ticket": conf["min_ticket"],
                            "by_reason": {}, "blocked_detail": []}
    if not conf["active"]:
        return list(candidates or []), info
    try:
        st = state if state is not None else drawdown_state(
            bankroll=bankroll, now=now, cfg=conf)
        info["drawdown_pct"] = st.get("drawdown_pct")
        info["factor_legacy"] = st.get("factor_legacy")
        if not st.get("ok"):
            info["reason"] = st.get("reason") or "misura_non_disponibile"
            return list(candidates or []), info
        dd_frac = float(st["drawdown_pct"]) / 100.0
        verdict = multiplier_for(dd_frac, cfg=conf)
        mult = float(verdict.get("multiplier") or 1.0)
        info.update({"multiplier": mult, "reason": verdict.get("reason"),
                     "tier": verdict.get("tier")})
        if mult >= 1.0:
            return list(candidates or []), info
        for cand in candidates or []:
            stake = _stake_of(cand)
            if stake <= 0:
                continue
            new_stake = math.floor(stake * mult * 100.0) / 100.0
            decision: Dict[str, Any] = {
                "drawdown_pct": st.get("drawdown_pct"),
                "multiplier": mult, "stake_before": stake,
                "stake_after": new_stake, "peak": st.get("peak"),
                "current": st.get("current"), "reason": verdict.get("reason"),
                "decision": "scaled", "detail": ""}
            if new_stake < float(conf["min_ticket"]):
                decision.update({"decision": "blocked", "stake_after": 0.0,
                                 "detail": ("drawdown %.1f%%: stake ridotto a "
                                            "%.2f USDC, sotto il ticket %.2f "
                                            "— ordine bloccato (fail-closed)"
                                            % (float(st["drawdown_pct"]),
                                               new_stake,
                                               float(conf["min_ticket"])))})
                cand["stake"] = 0.0
                cand["drawdown_rebalance"] = decision
                info["blocked"] = int(info["blocked"]) + 1
                key = "blocked_drawdown"
                info["by_reason"][key] = int(info["by_reason"].get(key, 0)) + 1
                info["blocked_detail"].append({
                    "match_id": cand.get("match_id"),
                    "esito": cand.get("esito_key") or cand.get("esito"),
                    "reason": key, "stake_before": stake,
                    "drawdown_pct": st.get("drawdown_pct"),
                    "detail": decision["detail"]})
                continue
            cand["stake"] = new_stake
            cand["drawdown_rebalance"] = decision
            info["scaled"] = int(info["scaled"]) + 1
            key = "scaled_drawdown"
            info["by_reason"][key] = int(info["by_reason"].get(key, 0)) + 1
    except Exception as e:                                    # pragma: no cover
        logger.warning("drawdown_rebalancer: gate non applicato (%s): candidati "
                       "invariati", e)
        info["error"] = "errore_valutazione"
        return list(candidates or []), info
    info["kept"] = sum(1 for c in candidates or [] if _stake_of(c) > 0)
    return list(candidates or []), info


# ---------------------------------------------------------------------------
# 4) Report e CLI
# ---------------------------------------------------------------------------
def format_report(state: Optional[Dict[str, Any]] = None, *,
                  cfg: Optional[Dict[str, Any]] = None) -> str:
    """Report testuale (Telegram-friendly) dello stato e della curva."""
    conf = cfg or config()
    st = state if state is not None else drawdown_state(cfg=conf)
    lines = ["📉 DRAWDOWN REBALANCING — riduzione dinamica degli stake",
             "  guard %s | modo %s | pavimento x%.2f | ticket %.2f USDC | "
             "curva %d soglie"
             % ("ON" if conf["enabled"] else "OFF", conf["mode"],
                conf["min_multiplier"], conf["min_ticket"], len(conf["tiers"]))]
    if not st.get("ok"):
        lines.append("  ⚠️ misura non disponibile: %s"
                     % (st.get("reason") or "errore"))
    else:
        verdict = multiplier_for(float(st["drawdown_pct"]) / 100.0, cfg=conf)
        lines.append("  drawdown rolling %.2f%% (picco %.2f -> attuale %.2f, "
                     "%d campioni)" % (float(st["drawdown_pct"]),
                                       float(st.get("peak") or 0.0),
                                       float(st.get("current") or 0.0),
                                       int(st.get("n_samples") or 0)))
        lines.append("  moltiplicatore applicato %.2f (%s) | curva storica %.2f"
                     % (float(verdict.get("multiplier") or 1.0),
                        verdict.get("reason"),
                        float(st.get("factor_legacy") or 1.0)))
    lines.append("  curva:")
    for row in curve_table(cfg=conf):
        lines.append("  · dd %6.2f%% -> x%.2f (10 USDC -> %.2f)"
                     % (row["drawdown_pct"], row["multiplier"],
                        row["stake_su_10_usdc"]))
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in args
    conf = config()
    if "--curve" in args:
        # Solo la curva: NESSUNA misura (non si tocca lo storico per una
        # domanda che riguarda la configurazione).
        payload: Dict[str, Any] = {"tiers": curve_table(cfg=conf),
                                   "min_multiplier": conf["min_multiplier"],
                                   "min_ticket": conf["min_ticket"],
                                   "mode": conf["mode"],
                                   "enabled": conf["enabled"]}
        print(json.dumps(payload, indent=2, default=str) if as_json
              else format_report({"ok": False, "reason": "solo curva (--curve)"},
                                 cfg=conf))
        return 0
    bankroll = None
    for i, a in enumerate(args):
        if a == "--bankroll" and i + 1 < len(args):
            try:
                bankroll = float(args[i + 1])
            except (TypeError, ValueError):
                bankroll = None
    st = drawdown_state(bankroll=bankroll, use_cache="--no-cache" not in args)
    verdict = multiplier_for(float(st["drawdown_pct"]) / 100.0, cfg=conf) \
        if st.get("ok") else {"multiplier": 1.0, "reason": st.get("reason")}
    payload = dict(st, **{"curve": curve_table(cfg=conf),
                          "applied_multiplier": verdict.get("multiplier"),
                          "applied_reason": verdict.get("reason"),
                          "config": {"mode": conf["mode"],
                                     "enabled": conf["enabled"],
                                     "min_multiplier": conf["min_multiplier"],
                                     "min_ticket": conf["min_ticket"]}})
    print(json.dumps(payload, indent=2, default=str) if as_json
          else format_report(st, cfg=conf))
    return 0


if __name__ == "__main__":                                    # pragma: no cover
    raise SystemExit(main())
