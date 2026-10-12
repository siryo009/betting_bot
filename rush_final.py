"""rush_final.py — RUSH FINALE del torneo (finestra 17-20/10/2026).

PERCHE' ESISTE (direttiva del proprietario, 13/10/2026)
------------------------------------------------------
La classifica del **Gridiron Gauntlet** conta i RITORNI LORDI ("wins increase
your standing, losses will have no impact") e si decide negli ultimi due giorni
pieni di football americano (**sab 17/10 NCAAF, dom 18/10 NFL**), con scadenza
**20/10 13:00Z**. Il conto fatto il 12/10 e' brutale: per i +193 punti del
taglio Top-30 servono ~13 vincite con stake 8.00, contro ~20 con stake 5.00 —
e con **~3 ordini contemporanei** dentro il recinto (cap 70% di ~30 USDC).

Da qui le DUE leve di questo modulo, e nessuna terza:

1. **ACCELERAZIONE** del sizing nella finestra finale (aggressione + cap
   dedicato, entrambi entro il recinto);
2. **RICIRCOLO RAPIDO** del capitale: il capitale *immobilizzato* e' il vero
   limite (esposizione aperta + ordini RESTING non ancora riempiti + ordini GTC
   parcheggiati in attesa di controparte). Nel rush: TTL degli ordini RESTING
   accorciato e ritiro degli ordini che non hanno trovato controparte, cosi'
   il budget torna disponibile per il match successivo invece di restare
   parcheggiato su un book che non lo riempie.

⚠️ COSA QUESTO MODULO **NON** FA (leggere prima di regolare le env)
------------------------------------------------------------------
- **Non allarga il recinto**: il tetto di capitale resta quello del giro
  (`auto_bet.OPEN_EXPOSURE_CAP_PCT`), e qui viene LETTO, non riscritto. Il
  budget del rush e' `min(cap del recinto, RUSH_BUDGET_USDC)`: l'env puo' solo
  RESTRINGERE.
- **Non tocca i guardrail**: kill-switch, stop giornaliero/settimanale, CB2,
  cap di correlazione (30%) e gate EV/oracolo restano esattamente dove sono —
  il layer vive DOPO di loro, come `tournament.py`, e un ordine puo' solo
  essere **alzato**, mai creato: se nessuna corsia lo ha proposto, per il rush
  non esiste.
- **Non aggira il fail-closed**: wallet/equity illeggibili -> il rush NON si
  applica (lo stake resta quello calcolato dai gate). Un'accelerazione di stake
  su un capitale che non so leggere e' esattamente la classe di incidente del
  09/10 (picco gonfiato) e del 21/09 (base sbagliata).
- **Non cambia la strategia**: fascia quota, edge, EV e leghe restano quelli
  congelati. L'aggressione e' un moltiplicatore di DIMENSIONE.

Confini (tripwire in `test_rush_final.py`): nessun ordine, nessuna soglia di
strategia ricopiata (recinto da `auto_bet`, budget/perimetro da `tournament`,
esposizione da `auto_bet._open_live_snapshot`), import leggero
(`import rush_final` non carica `bot`/`execution_engine`), fail-safe totale:
`report()`/`stake_for()`/`apply()` non sollevano mai.

CLI:
    venv/bin/python rush_final.py [--json]
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Sequence

logger = logging.getLogger("rush_final")

#: Finestra di default (la scadenza del torneo e' il 20/10 13:00Z: l'ultimo
#: giorno utile e' il 19/10 per gli ordini e il 20 fino alle 11:00Z per i
#: referti). Le date sono inclusive e lette a RUNTIME.
DEFAULT_START = "2026-10-17"
DEFAULT_END = "2026-10-20"

#: Etichetta usata sui pick per la telemetria (`rush_final: True` + info).
RUSH_FLAG = "rush_final"


# ---------------------------------------------------------------------------
# Configurazione (tutte a RUNTIME: si accende/spegne senza redeploy)
# ---------------------------------------------------------------------------

def _num_env(name: str, default: float, *, minimum: float = 0.0) -> float:
    """Env numerica con clamp verso il basso (valore impossibile -> default)."""
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return float(default)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning("rush_final: %s='%s' non numerico, uso %s",
                       name, raw, default)
        return float(default)
    if value < minimum:
        logger.warning("rush_final: %s=%s sotto il minimo %s, uso %s",
                       name, value, minimum, default)
        return float(default)
    return value


def _flag(name: str, default: str = "1") -> bool:
    """Interruttore (stessa convenzione di tutto il progetto)."""
    return str(os.getenv(name, default)).strip().lower() not in (
        "0", "false", "no", "off", "")


def enabled() -> bool:
    """Interruttore maestro (`RUSH_FINAL`, default ON).

    ⚠️ ON di default ma INERTE fuori finestra: la finestra e' il vero gate, e
    il 21/10 questo modulo si spegne da solo senza che nessuno tocchi nulla.
    """
    return _flag("RUSH_FINAL", "1")


def _stamp(raw: Any) -> Optional[datetime]:
    """Data/ora di una env o del ledger -> datetime UTC (None se illeggibile).

    Accetta `YYYY-MM-DD` (data intera, interpretata come mezzanotte UTC) e ISO
    completo. Un valore illeggibile NON e' un default silenzioso: il chiamante
    lo dichiara (fail-closed sulla finestra).
    """
    if isinstance(raw, datetime):
        ts = raw
    else:
        text = str(raw or "").strip().replace("Z", "+00:00")
        if not text:
            return None
        try:
            ts = datetime.fromisoformat(text)
        except (TypeError, ValueError):
            return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def window() -> Dict[str, Any]:
    """Finestra del rush: `{start, end, valid, raw}` (mai un'eccezione)."""
    raw_s = os.getenv("RUSH_START", DEFAULT_START)
    raw_e = os.getenv("RUSH_END", DEFAULT_END)
    start, end = _stamp(raw_s), _stamp(raw_e)
    return {
        "start": start.isoformat() if start else None,
        "end": end.isoformat() if end else None,
        "raw": {"start": raw_s, "end": raw_e},
        "valid": bool(start and end and end >= start),
    }


def _in_window(now: datetime) -> bool:
    w = window()
    start, end = _stamp(w["start"]), _stamp(w["end"])
    if start is None or end is None or not w["valid"]:
        return False
    return start <= now <= end


def precheck(*, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Gate a COSTO ZERO: interruttore + finestra, senza toccare il wallet.

    Perche' esiste: `report()` legge il wallet (`auto_bet._live_wallet_snapshot`
    fa una chiamata di rete) ed e' chiamato da percorsi caldi — `_apply_rush`
    gira ogni **60 s** e `resting_orders.config()` a ogni lettura dello stato.
    Fuori finestra (tutto l'anno tranne pochi giorni) non si deve pagare nulla
    per rispondere "il rush non e' attivo", quindi il gate va risolto PRIMA di
    leggere il capitale. Il verdetto pieno (recinto, budget) resta in
    `report()` e si chiede solo se `active` e' True.

    Ritorna `{active, reason}` con la stessa nomenclatura di `report()`.
    """
    now = now or datetime.now(timezone.utc)
    if not enabled():
        return {"active": False, "reason": "rush_off"}
    w = window()
    start, end = _stamp(w["start"]), _stamp(w["end"])
    if start is None or end is None or not w["valid"]:
        return {"active": False, "reason": "window_invalid"}
    if now < start:
        return {"active": False, "reason": "before_window"}
    if now > end:
        return {"active": False, "reason": "window_ended"}
    return {"active": True, "reason": "in_window"}


def budget_declared() -> float:
    """Budget massimo del rush (`RUSH_BUDGET_USDC`, 0 = solo il recinto)."""
    return _num_env("RUSH_BUDGET_USDC", 0.0, minimum=0.0)


def max_order_stake() -> float:
    """Cap per ordine in modalita' rush (`RUSH_MAX_STAKE_USDC`)."""
    return _num_env("RUSH_MAX_STAKE_USDC", 8.0, minimum=0.0)


def aggression() -> float:
    """Moltiplicatore di stake del rush (>= 1.0: non riduce mai)."""
    return max(1.0, _num_env("RUSH_AGGRESSION", 1.25, minimum=1.0))


def recycle_window_min() -> float:
    """Eta' (minuti) oltre la quale un'opportunita' e' 'bloccata' altrove."""
    return _num_env("RUSH_RECYCLE_WINDOW_MIN", 180.0, minimum=1.0)


def recycle_boost() -> float:
    """Boost massimo aggiuntivo quando il capitale e' bloccato (>= 1.0)."""
    return max(1.0, _num_env("RUSH_RECYCLE_BOOST", 1.15, minimum=1.0))


def resting_ttl_minutes() -> float:
    """TTL degli ordini RESTING durante il rush (`RUSH_RESTING_TTL_MIN`)."""
    return _num_env("RUSH_RESTING_TTL_MIN", 45.0, minimum=1.0)


def clear_resting_enabled() -> bool:
    """"Ritirare gli ordini resting rimasti senza controparte (`RUSH_CLEAR_RESTING`)."""
    return _flag("RUSH_CLEAR_RESTING", "1")


def clear_resting_after_min() -> float:
    """Dopo quanti minuti un ordine resting 'stantio' viene ritirato."""
    return _num_env("RUSH_CLEAR_RESTING_AFTER_MIN", 30.0, minimum=1.0)


def classes() -> Sequence[str]:
    """Classi (mercati) su cui applicare il rush. Vuoto = tutte le corsie."""
    raw = os.getenv("RUSH_CLASSES")
    if raw is None or not str(raw).strip():
        return ()
    return tuple(p.strip().upper() for p in str(raw).split(",") if p.strip())


def covers(market: Any) -> bool:
    """True se il mercato rientra nel perimetro del rush."""
    wanted = classes()
    if not wanted:
        return True
    return str(market or "").strip().upper() in wanted


# ---------------------------------------------------------------------------
# Letture del giro (recinto, wallet, torneo, resting) — mai un'eccezione
# ---------------------------------------------------------------------------

def _wallet() -> Dict[str, Any]:
    """Istantanea del wallet (equity/deliverable) via `auto_bet`, fail-safe."""
    out = {"equity": None, "available": None, "exposure": None, "error": None}
    try:
        import auto_bet
        snap = auto_bet._live_wallet_snapshot()               # noqa: SLF001
        if isinstance(snap, dict):
            for key in ("equity", "available", "exposure"):
                try:
                    out[key] = round(float(snap.get(key)), 4)
                except (TypeError, ValueError):
                    out[key] = None
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def _exposure() -> Dict[str, Any]:
    """Capitale immobilizzato: puntate live aperte + ordini RESTING aperti."""
    out = {"open": None, "orders": None, "cap_pct": None, "cap_usdc": None,
           "free": None, "utilization": None, "error": None}
    try:
        import auto_bet
        stake, count = auto_bet._open_live_snapshot()          # noqa: SLF001
        out["open"] = round(float(stake or 0.0), 4)
        out["orders"] = int(count or 0)
        out["cap_pct"] = float(getattr(auto_bet, "OPEN_EXPOSURE_CAP_PCT", 0.40))
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out
    equity = _wallet().get("equity")
    if equity is not None and out["cap_pct"] is not None:
        cap = round(equity * out["cap_pct"], 4)
        out["cap_usdc"] = cap
        out["free"] = round(max(cap - (out["open"] or 0.0), 0.0), 4)
        out["utilization"] = (round((out["open"] or 0.0) / cap, 4) if cap > 0
                              else None)
    return out


def _stale_stake(now: datetime) -> Dict[str, Any]:
    """Capitale impegnato su opportunita' 'ferme' da oltre la finestra.

    Perche' misura il RICIRCOLO: un'opportunita' che aspetta da ore e' capitale
    che non sta girando. Se ce n'e' molto, il tetto non e' il vincolo — lo e'
    la velocita' con cui il capitale torna disponibile, ed e' quello che il
    boost di ricircolo compensa.
    """
    out = {"stale": 0, "stale_stake": 0.0, "open_stake": 0.0, "open": 0,
           "error": None}
    try:
        import auto_bet
        stale_min = recycle_window_min()
        rows = auto_bet._open_live_rows()                       # noqa: SLF001
        for r in rows or []:
            stake = float(r.get("stake") or 0.0)
            out["open"] += 1
            out["open_stake"] = round(out["open_stake"] + stake, 4)
            created = _stamp(r.get("created_at"))
            if created is not None and (now - created) >= timedelta(minutes=stale_min):
                out["stale"] += 1
                out["stale_stake"] = round(out["stale_stake"] + stake, 4)
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def _resting() -> Dict[str, Any]:
    """Stato degli ordini RESTING (delega: una sola definizione)."""
    try:
        import resting_orders
        s = resting_orders.summary()
        return {"enabled": bool(s.get("enabled")), "open": int(s.get("open") or 0),
                "open_stake": float(s.get("open_stake") or 0.0),
                "ttl_min": float(s.get("ttl_min") or 0.0)}
    except Exception as exc:
        return {"enabled": False, "open": 0, "open_stake": 0.0, "ttl_min": 0.0,
                "error": f"{type(exc).__name__}: {exc}"}


def _tournament() -> Dict[str, Any]:
    """Stato del torneo (delega: budget, perimetro e cap non si ricopiano)."""
    try:
        import tournament
        return tournament.status()
    except Exception as exc:
        return {"active": False, "error": f"{type(exc).__name__}: {exc}"}


def recycle_pressure(stale: Optional[Dict[str, Any]] = None,
                     exposure: Optional[Dict[str, Any]] = None,
                     ref: Optional[float] = None) -> float:
    """Pressione di ricircolo in [0, 1]: quanto il capitale e' fermo.

    Due componenti, entrambe MISURATE (mai inventate):
      - capitale 'fermo' (impegnato da oltre `RUSH_RECYCLE_WINDOW_MIN` minuti)
        sul totale impegnato;
      - saturazione del recinto (esposizione / cap).
    Il massimo delle due e' la pressione: se il recinto e' pieno oppure il
    capitale e' fermo, il rush alza lo stake perche' il fattore limitante non
    e' la disponibilita' ma il TEMPO.
    """
    ex = exposure if isinstance(exposure, dict) else _exposure()
    st = stale if isinstance(stale, dict) else _stale_stake(
        ref or datetime.now(timezone.utc))
    parts = []
    open_stake = float(st.get("open_stake") or 0.0)
    if open_stake > 0:
        parts.append(min(float(st.get("stale_stake") or 0.0) / open_stake, 1.0))
    util = ex.get("utilization")
    if util is not None:
        parts.append(min(max(float(util), 0.0), 1.0))
    return round(max(parts), 4) if parts else 0.0


def budget_usdc(exposure: Optional[Dict[str, Any]] = None) -> float:
    """Budget di esposizione del rush: **mai oltre il recinto**.

    `min(cap del recinto, RUSH_BUDGET_USDC)` — se l'env non e' impostata vale
    il cap del recinto, che e' il vero limite del giro. Fail-closed: senza una
    equity leggibile (cap ignoto) il budget e' 0.
    """
    ex = exposure if isinstance(exposure, dict) else _exposure()
    cap = ex.get("cap_usdc")
    declared = budget_declared()
    if cap is None:
        return 0.0
    return round(min(float(cap), declared) if declared > 0 else float(cap), 4)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def report(*, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Istantanea completa del rush. Fail-safe: mai un'eccezione."""
    now = now or datetime.now(timezone.utc)
    w = window()
    start, end = _stamp(w["start"]), _stamp(w["end"])
    out: Dict[str, Any] = {
        "now": now.isoformat(), "window": w, "enabled": enabled(),
        "ok": False, "reason": None, "active": False,
        "days_left": None, "hours_left": None,
        "wallet": {}, "exposure": {}, "recycle": {}, "budget": {},
        "stake": {"max": max_order_stake(), "aggression": aggression()},
        "resting": {}, "tournament": {}, "classes": list(classes()), "note": "",
    }
    try:
        out["wallet"] = _wallet()
        out["exposure"] = _exposure()
        if start is not None:
            out["days_left"] = round((end - now).total_seconds() / 86400.0, 3)
            out["hours_left"] = round((end - now).total_seconds() / 3600.0, 2)
        out["tournament"] = _tournament()
        out["resting"] = _resting()
        stale = _stale_stake(now)
        out["recycle"] = {**stale, "window_min": recycle_window_min(),
                          "boost": recycle_boost(),
                          "pressure": recycle_pressure(stale, out["exposure"])}
        b = budget_usdc(out["exposure"])
        out["budget"] = {"declared": budget_declared(), "cap_usdc": b,
                         "open": out["exposure"].get("open"),
                         "free": out["exposure"].get("free"),
                         "left": (round(max(b - float(out["exposure"].get("open") or 0.0), 0.0), 4)
                                  if b else 0.0)}

        # --- Gate: perche' il rush e' attivo o no (motivo SEMPRE dichiarato)
        if not enabled():
            out["reason"] = "rush_off"
        elif start is None or end is None or not w["valid"]:
            out["reason"] = "window_invalid"
        elif now < start:
            out["reason"] = "before_window"
        elif now > end:
            out["reason"] = "window_ended"
        elif out["wallet"].get("error") or out["wallet"].get("equity") is None:
            # Fail-closed: nessuna accelerazione su un capitale illeggibile.
            out["reason"] = "wallet_unreadable"
        elif not b:
            out["reason"] = "budget_unknown"
        elif (out["exposure"].get("free") or 0.0) <= 0.0:
            out["reason"] = "enclosure_full"
        else:
            out["ok"] = True
            out["active"] = True
            out["reason"] = "active"
    except Exception as exc:                                  # pragma: no cover
        out["reason"] = "error"
        out["note"] = f"{type(exc).__name__}: {exc}"
        logger.warning("rush_final: report non completato (%s)", exc)
    out["note"] = out["note"] or _note(out)
    return out


def _note(rep: Dict[str, Any]) -> str:
    """La riga che spiega lo stato senza che nessuno debba interpretarlo."""
    r = rep.get("reason")
    if r == "active":
        return ("finestra del rush: sizing accelerato e ricircolo rapido, "
                "sempre entro il recinto")
    if r == "before_window":
        return "fuori finestra (inizia il %s)" % (rep["window"]["raw"]["start"],)
    if r == "window_ended":
        return "finestra conclusa"
    if r == "wallet_unreadable":
        return ("wallet illeggibile: il rush NON si applica (lo stake resta "
                "quello dei gate) — fail-closed")
    if r == "enclosure_full":
        return ("recinto pieno: nessuna accelerazione finche' il capitale non "
                "si libera")
    if r == "budget_unknown":
        return "budget non calcolabile (equity ignota): nessuna accelerazione"
    return ""


# ---------------------------------------------------------------------------
# Il punto di decisione: quanto vale lo stake di un pick gia' approvato
# ---------------------------------------------------------------------------

def stake_for(stake: float, *, pick: Optional[Dict[str, Any]] = None,
              rep: Optional[Dict[str, Any]] = None,
              bankroll: Optional[float] = None) -> Dict[str, Any]:
    """Stake effettivo in modalita' rush. **Non solleva mai.**

    Ritorna `{stake, applied, blocked, reason, base, boost, ceiling}`:
      - `applied=False` -> il chiamante usa lo stake dei gate (nessun effetto);
      - `blocked=True`  -> il chiamante NON deve piazzare (solo se il
                           risanamento scenderebbe sotto il minimo ordine).

    Ordine delle decisioni (nessuna scavalca la precedente):
      0. rush non attivo / mercato fuori perimetro -> stake invariato;
      1. stake base sotto il minimo ordine        -> `below_min_stake`;
      2. `stake x aggressione x (1 + peso del ricircolo)` e poi i tetti:
         `min(cap rush, cap del torneo, budget residuo, spazio del recinto)`;
      3. se il risultato cade sotto il minimo ordine -> `below_min_after_cap`.
    """
    out: Dict[str, Any] = {"stake": 0.0, "applied": False,
                           "blocked": False, "reason": "off", "base": None,
                           "boost": 1.0, "ceiling": None}
    try:
        # ⚠️ La conversione va DENTRO il try anche per costruire `out`: fuori,
        # uno stake ostile (`"abc"`) solleverebbe PRIMA della guardia, e la
        # promessa "non solleva mai" sarebbe falsa (trovato dai test del
        # 13/10/2026). Con un input illeggibile lo stake resta 0.0 e il
        # chiamante usa quello dei gate.
        base = float(stake or 0.0)
    except (TypeError, ValueError):
        return out
    out["stake"] = base
    out["base"] = base
    if base <= 0:
        return out
    try:
        # L'interruttore e' l'AUTORITA', anche a valle: un `rep` costruito
        # altrove (o letto prima dello spegnimento) non puo' applicare il rush
        # quando `RUSH_FINAL=0`. Difesa in profondita': il gate di `report()`
        # copre il percorso normale, questo copre il chiamante che inietta un
        # verdetto.
        if not enabled():
            out["reason"] = "rush_off"
            return out
        r = rep if isinstance(rep, dict) else report()
        if not r.get("ok"):
            out["reason"] = r.get("reason") or "off"
            return out
        if not covers((pick or {}).get("mercato")):
            out["reason"] = "class_not_covered"
            return out
        floor = _min_stake()
        if base + 1e-9 < floor:
            out["reason"] = "below_min_stake"
            return out
        pressure = float((r.get("recycle") or {}).get("pressure") or 0.0)
        boost = aggression() * (1.0 + (recycle_boost() - 1.0) * pressure)
        target = base * boost
        ceiling = _ceiling(r)
        out["ceiling"] = round(ceiling, 6) if ceiling is not None else None
        out["boost"] = round(boost, 4)
        if ceiling is None or ceiling <= 0:
            out["reason"] = "no_room"
            return out
        final = min(target, ceiling)
        if final + 1e-9 < floor:
            out["blocked"] = True
            out["reason"] = "below_min_after_cap"
            return out
        out["stake"] = round(final, 6)
        out["applied"] = final > base + 1e-9
        out["reason"] = "rush" if out["applied"] else "already_at_cap"
        out["bankroll"] = float(bankroll) if bankroll is not None else None
        return out
    except Exception as exc:                                  # pragma: no cover
        logger.warning("rush_final: stake_for fallita (%s)", exc)
        out["reason"] = "error"
        return out


def _min_stake() -> float:
    """Minimo ordine dell'exchange (delega: una sola definizione)."""
    try:
        import tournament
        return float(tournament.min_stake())
    except Exception:
        try:
            import auto_bet
            return float(getattr(auto_bet, "MIN_STAKE_EUR", 1.0) or 1.0)
        except Exception:
            return 1.0


def _ceiling(rep: Dict[str, Any]) -> Optional[float]:
    """Il tetto effettivo del rush: nessuno dei quattro puo' essere scavalato.

    `min(cap rush, cap torneo, budget residuo, spazio del recinto)`. Il cap del
    TORNEO entra anche se il rush e' acceso: sono due regole di rischio che si
    sommano, non che si sostituiscono (se il torneo e' piu' severo, vince lui).
    """
    caps = []
    caps.append(max_order_stake())
    try:
        import tournament
        caps.append(float(tournament.max_order_stake()))
    except Exception:
        pass
    left = (rep.get("budget") or {}).get("left")
    if left is not None:
        caps.append(float(left))
    free = (rep.get("exposure") or {}).get("free")
    if free is not None:
        caps.append(float(free))
    clean = [c for c in caps if c is not None and c > 0]
    return min(clean) if clean else None


def apply(candidates: Sequence[Dict[str, Any]], *,
          rep: Optional[Dict[str, Any]] = None,
          bankroll: Optional[float] = None) -> list:
    """Applica il rush al board: ritorna i pick (copie) con lo stake aggiornato.

    Si applica SOLO ai pick che esistono gia' (proposti da una corsia e passati
    dai gate): il rush non crea mai un'opportunita'. Fail-safe: un pick
    malformato esce invariato.
    """
    r = rep if isinstance(rep, dict) else None
    if r is None:
        # Gate a COSTO ZERO anche per un chiamante che non passa `rep`: e' la
        # stessa disciplina di `auto_bet._apply_rush` — fuori finestra non si
        # legge il wallet (una chiamata di rete) per scoprire che il rush non
        # c'e'. Dentro la finestra il verdetto pieno si chiede una volta sola.
        try:
            chk = precheck()
        except Exception:                                    # pragma: no cover
            chk = {"active": False, "reason": "off"}
        r = (report() if chk.get("active")
             else {"ok": False, "reason": chk.get("reason") or "off"})
    out: list = []
    applied = blocked = 0
    for cand in candidates or []:
        pick = dict(cand) if isinstance(cand, dict) else cand
        try:
            verdict = stake_for(pick.get("stake"), pick=pick, rep=r,
                                bankroll=bankroll)
            if verdict.get("applied"):
                pick["stake"] = verdict["stake"]
                pick[RUSH_FLAG] = True
                pick["rush_info"] = verdict
                applied += 1
                logger.info(
                    "rush_final: %s %s stake %.2f -> %.2f (x%.2f, pressione "
                    "ricircolo %.0f%%%s)",
                    pick.get("match_id"), pick.get("esito_key"),
                    float(verdict.get("base") or 0.0), float(verdict["stake"]),
                    float(verdict.get("boost") or 1.0),
                    100.0 * float(((r or {}).get("recycle") or {}).get("pressure") or 0.0),
                    f", tetto {verdict['ceiling']:.2f}" if verdict.get("ceiling")
                    else "")
            elif verdict.get("blocked"):
                # ⚠️ Il verdetto va scritto ANCHE qui: `auto_bet._apply_rush`
                # conta i bloccati da `rush_info` (trovato dai test del
                # 13/10: senza, il log del giro diceva sempre "0 bloccati"
                # mentre i pick venivano fermati dal tetto).
                pick["rush_info"] = verdict
                blocked += 1
                logger.info("rush_final: %s %s BLOCCATO (%s)",
                            pick.get("match_id"), pick.get("esito_key"),
                            verdict.get("reason"))
        except Exception as exc:                              # pragma: no cover
            logger.warning("rush_final: pick non elaborato (%s)", exc)
        out.append(pick)
    if applied or blocked:
        logger.info("rush_final: %s pick accelerati, %s bloccati dal tetto",
                    applied, blocked)
    return out


# ---------------------------------------------------------------------------
# Ricircolo: TTL degli ordini RESTING + ritiro degli ordini stantii
# ---------------------------------------------------------------------------

def override_resting_ttl(default: float, *,
                         now: Optional[datetime] = None) -> float:
    """TTL effettivo degli ordini RESTING (hook chiamato da `resting_orders`).

    Nel rush un ordine che non trova controparte in 45' viene ritirato: il
    capitale torna disponibile per il prossimo match invece di restare
    parcheggiato (il ricircolo e' la leva, non il volume di tentativi).
    Fuori finestra ritorna il `default` invariato, quindi `resting_orders`
    resta esattamente quello di prima.
    """
    try:
        chk = precheck(now=now)
        if not chk.get("active"):
            # Fuori finestra NON si legge il wallet: `config()` e' chiamata
            # anche da percorsi che non stanno ordinando nulla.
            return float(default)
        rep = report(now=now)
        if rep.get("ok"):
            return float(resting_ttl_minutes())
    except Exception:                                         # pragma: no cover
        pass
    return float(default)


def clear_stale_resting(prov: Any = None, *, now: Optional[datetime] = None,
                        hours: Optional[float] = None) -> Dict[str, Any]:
    """Ritira gli ordini RESTING che non hanno trovato controparte.

    Perche' e' sicuro: un ordine RESTING non e' una puntata — resta parcheggiato
    sul book e diventa una riga `bets` solo quando `reconcile` ha la PROVA del
    riempimento. Qui si CHIEDE la cancellazione (`prov.cancel_order`) e si
    marcano `cancel_attempted`/`cancel_ok`: l'esito lo decide comunque
    `reconcile` (un ordine riempito all'ultimo istante non viene mai perso).

    Fail-closed: se il rush non e' attivo, o se il registro/provider non sono
    leggibili, non si cancella NULLA e il motivo e' dichiarato.
    """
    out: Dict[str, Any] = {"checked": 0, "cancel_requested": 0, "cancel_ok": 0,
                           "skipped": {}, "reason": None}
    now = now or datetime.now(timezone.utc)
    try:
        chk = precheck(now=now)
        if not chk.get("active"):
            # Fuori finestra zero rete: il job gira ogni 5' e non deve leggere
            # il wallet per scoprire che il rush non c'e'.
            out["reason"] = chk.get("reason") or "off"
            return out
        rep = report(now=now)
        if not rep.get("ok"):
            out["reason"] = rep.get("reason") or "off"
            return out
        if not clear_resting_enabled():
            out["reason"] = "clear_disabled"
            return out
        import resting_orders
        rows = resting_orders.open_orders()
        out["checked"] = len(rows)
        if not rows:
            out["reason"] = "active_no_orders"
            return out
        if prov is None:
            import execution_engine as ee
            prov = ee.SxBetProvider()
        age_min = float(hours) * 60.0 if hours else clear_resting_after_min()
        changed = False
        state = resting_orders.load()
        for r in rows:
            placed = _stamp(r.get("placed_at") or r.get("created_at"))
            if placed is None or (now - placed) < timedelta(minutes=age_min):
                out["skipped"]["fresh"] = out["skipped"].get("fresh", 0) + 1
                continue
            out["cancel_requested"] += 1
            try:
                ok = bool(prov.cancel_order(str(r.get("market_id") or ""),
                                            str(r.get("order_id") or "")))
            except Exception as exc:
                ok = False
                logger.warning("rush_final: cancellazione %s fallita (%s)",
                               str(r.get("order_id"))[:12], exc)
            if ok:
                out["cancel_ok"] += 1
                logger.info("rush_final: ordine resting %s ritirato per "
                            "ricircolo (fermo da %.0f')",
                            str(r.get("order_id"))[:12], age_min)
            # Aggiorna la riga nel registro caricato (mai una nuova scrittura
            # di stato: l'esito lo decide `reconcile`).
            oid = str(r.get("order_id") or "")
            for row in state.get("orders") or []:
                if isinstance(row, dict) and str(row.get("order_id") or "") == oid:
                    row["cancel_attempted"] = True
                    row["cancel_ok"] = ok
                    row["cancel_at"] = now.isoformat()
                    row["cancel_reason"] = "rush_recycle"
                    changed = True
        if changed:
            resting_orders.save(state)
    except Exception as exc:
        logger.warning("rush_final: ritiro resting interrotto (%s)", exc)
        out["reason"] = out["reason"] or "error"
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


# ---------------------------------------------------------------------------
# Report leggibile + CLI
# ---------------------------------------------------------------------------

def status() -> Dict[str, Any]:
    """Alias esplicito di `report()` (nomenclatura usata dagli altri moduli)."""
    return report()


def format_report(rep: Optional[Dict[str, Any]] = None) -> str:
    """Riga Telegram-friendly dello stato del rush finale."""
    r = rep if isinstance(rep, dict) else report()
    head = "🚀 RUSH FINALE"
    if not r.get("ok"):
        return (f"{head}: NON attivo — {r.get('reason')}"
                + (f" ({r['note']})" if r.get("note") else ""))
    w = r.get("window") or {}
    ex = r.get("exposure") or {}
    rec = r.get("recycle") or {}
    b = r.get("budget") or {}
    st = r.get("stake") or {}
    wal = r.get("wallet") or {}
    lines = [
        f"{head}: ATTIVO fino a {str(w.get('end') or '')[:16]}",
        f"  • equity {float(wal.get('equity') or 0):.2f} USDC | esposizione "
        f"{float(ex.get('open') or 0):.2f}/{float(ex.get('cap_usdc') or 0):.2f} "
        f"(recinto {float(ex.get('cap_pct') or 0) * 100:.0f}%, libero "
        f"{float(ex.get('free') or 0):.2f})",
        f"  • budget rush {b.get('declared') or 'recinto'} | residuo "
        f"{float(b.get('left') or 0):.2f} USDC",
        f"  • stake: cap {float(st.get('max') or 0):.2f} USDC | aggressione "
        f"x{float(st.get('aggression') or 1):.2f} (sul capitale fresco dei "
        "gate)",
        f"  • ricircolo: pressione {float(rec.get('pressure') or 0) * 100:.0f}% "
        f"({rec.get('stale')}/{rec.get('open')} trade fermi da oltre "
        f"{float(rec.get('window_min') or 0):.0f}', boost fino a "
        f"x{float(rec.get('boost') or 1):.2f})",
        f"  • resting: {r.get('resting', {}).get('open', 0)} aperti "
        f"({float(r.get('resting', {}).get('open_stake') or 0):.2f} USDC), "
        f"TTL {float(r.get('resting', {}).get('ttl_min') or 0):.0f}'",
        "  ℹ️ guardrail intatti: kill-switch, stop giornaliero/settimanale, "
        "CB2, correlazione 30% e gate EV restano quelli del giro",
    ]
    if r.get("note"):
        lines.append(f"  ↳ {r['note']}")
    return "\n".join(lines)


def main(argv: Optional[list] = None) -> int:                    # pragma: no cover
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    r = report()
    print(json.dumps(r, indent=2, ensure_ascii=False, default=str)
          if args.json else format_report(r))
    return 0 if r.get("ok") else 1


if __name__ == "__main__":                                       # pragma: no cover
    raise SystemExit(main())
