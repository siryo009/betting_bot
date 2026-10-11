"""tournament.py — layer di staking per un torneo SX Bet "return-based".

PERCHE' ESISTE (direttiva del proprietario, 11/10/2026)
-------------------------------------------------------
SX Bet sponsorizza tornei a periodo (es. **Gridiron Gauntlet**, 08/10 06:00 ->
20/10 06:00: 58 partecipanti, **$7.777 divisi fra i top 30**) con una regola
precisa, verificata sul sito ufficiale:

    "wins increase your standing, losses will have no impact"
    "RETURN betting tournament"

Cioe': il punteggio e' la somma dei RITORNI LORDI delle puntate vincenti; una
puntata persa **non toglie punti**. E' una metrica di classificazione, NON di
profitto.

⚠️ COSA QUESTO MODULO *NON* E' — da leggere prima di usarlo
----------------------------------------------------------
Un torneo return-based **non e' un edge**: matematicamente, se le perdite non
contano, la giocata ottimale *per la classifica* e' la massima varianza
possibile (anche a EV negativo volontario). Il progetto e' costruito al
contrario — solo +EV, oracolo sharp fail-closed, edge minimo, cap severo — e
questa module NON cambia quella disciplina:

    il torneo puo' solo ALZARE leggermente lo stake di un segnale che i gate
    hanno GIA' approvato. Nessun gate viene bypassato, nessun EV negativo
    diventa giocabile, nessuna soglia di strategia viene toccata.

In cambio di un po' piu' di varianza, il capitale esposto al torneo ha un tetto
esplicito e una soglia di sicurezza sull'equity reale.

LE TRE PROTEZIONI (tutte fail-closed)
-------------------------------------
1. **BUDGET**: l'esposizione APERTA totale (bet `mode='live'` non ancora
   saldate) non puo' superare `TOURNAMENT_BUDGET_USDC` (default **10.0**). Il
   vincolo e' sull'esposizione *corrente*, non giornaliera: chiuso un match, il
   capitale si libera da solo (stessa logica del recinto 40%).
2. **SOGLIA DI SICUREZZA**: sotto `TOURNAMENT_SAFETY_FLOOR_USDC` (default
   **20.0**) di equity il torneo si mette in pausa: si continua a giocare col
   percorso standard, non con l'incremento del torneo.
3. **CAP PER ORDINE**: `TOURNAMENT_MAX_STAKE_USDC` (default **2.50**) — tetto
   assoluto per singola puntata in modalita' torneo (con ~30 USDC di equity e'
   ~8%: sopra il cap 12% del Kelly ma sotto qualunque soglia di rovina).

I guardrail di portafoglio (recinto 40%, correlazione 30%, CB2, stop-loss
giornaliero/settimanale) restano quelli del giro: questo modulo sta **sopra**
il percorso di sizing e **sotto** i cap di portafoglio.

Env (tutte dichiarate `preserve()` in `.railway/railway.ts`):
    TOURNAMENT_MODE            "0" (spento; "1" attiva)
    TOURNAMENT_BUDGET_USDC     "10.0"
    TOURNAMENT_SAFETY_FLOOR_USDC "20.0"
    TOURNAMENT_MAX_STAKE_USDC  "2.50"
    TOURNAMENT_AGGRESSION      "1.5"  (moltiplicatore, >= 1.0)
    TOURNAMENT_CLASSES         ""     (CSV di `mercato`; vuoto = tutte le corsie)

Fail-safe: `stake_for` non solleva MAI. Un errore di lettura non alza lo stake
(si torna a quello standard), una regola violata lo azzera (`blocked`).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger("tournament")

#: Nome del torneo in corso (solo per i report: nessuna logica vi dipende).
LABEL = os.getenv("TOURNAMENT_LABEL", "Gridiron Gauntlet")


# ---------------------------------------------------------------------------
# Configurazione (letture a RUNTIME: una soglia si cambia senza redeploy)
# ---------------------------------------------------------------------------

def _num_env(name: str, default: float, *, minimum: float = 0.0) -> float:
    """Env numerica con clamp verso il basso (valore impossibile -> default)."""
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return float(default)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning("tournament: %s='%s' non numerico, uso %s",
                       name, raw, default)
        return float(default)
    if value < minimum:
        logger.warning("tournament: %s=%s sotto il minimo %s, uso %s",
                       name, value, minimum, default)
        return float(default)
    return value


def active() -> bool:
    """Il torneo e' in corso? (`TOURNAMENT_MODE`; default OFF)."""
    return str(os.getenv("TOURNAMENT_MODE", "0")).strip().lower() \
        not in ("0", "false", "no", "off", "")


def budget() -> float:
    return _num_env("TOURNAMENT_BUDGET_USDC", 10.0, minimum=0.0)


def safety_floor() -> float:
    return _num_env("TOURNAMENT_SAFETY_FLOOR_USDC", 20.0, minimum=0.0)


def max_order_stake() -> float:
    return _num_env("TOURNAMENT_MAX_STAKE_USDC", 2.50, minimum=0.0)


def aggression() -> float:
    """Moltiplicatore di stake (>= 1.0: il torneo non RIDUCE mai lo stake)."""
    return max(1.0, _num_env("TOURNAMENT_AGGRESSION", 1.5, minimum=1.0))


def classes() -> Sequence[str]:
    """Classi di rischio su cui applicare l'incremento.

    Vuoto = TUTTE le corsie (decisione del proprietario: l'incremento non e'
    riservato al football USA). Un valore `mercato` (case-insensitive) limita.
    """
    raw = os.getenv("TOURNAMENT_CLASSES")
    if raw is None or not str(raw).strip():
        return ()
    return tuple(p.strip().upper() for p in str(raw).split(",") if p.strip())


def covers(market: Any) -> bool:
    """True se la classe di rischio rientra nel perimetro del torneo."""
    wanted = classes()
    if not wanted:
        return True                      # vuoto = tutte le corsie
    return str(market or "").strip().upper() in wanted


def min_stake() -> float:
    """Minimo ordine dell'exchange (1 USDC): preso da `auto_bet`, mai copiato."""
    try:
        import auto_bet
        return float(getattr(auto_bet, "MIN_STAKE_EUR", 1.0) or 1.0)
    except Exception:
        return 1.0


# ---------------------------------------------------------------------------
# Esposizione aperta (UNICA fonte: il ledger delle puntate)
# ---------------------------------------------------------------------------

def deployed() -> float:
    """Capitale attualmente impegnato su SX (bet live ancora aperte).

    Delega ad `auto_bet._open_live_snapshot` — l'UNICO punto del progetto che
    sa cos'e' il "capitale immobilizzato" (dal 10/10 include anche gli ordini
    RESTING aperti). Reimplementarlo qui creerebbe una seconda definizione di
    esposizione, e le due divergerebbero al primo cambio.
    Fail-open: su errore si ritorna 0.0 — un problema di telemetria non deve
    azzerare il budget del torneo (i cap di portafoglio restano attivi).
    """
    try:
        import auto_bet
        stake, _count = auto_bet._open_live_snapshot()
        return round(max(float(stake or 0.0), 0.0), 4)
    except Exception as exc:
        logger.debug("tournament: esposizione non leggibile (%s)", exc)
        return 0.0


def budget_left() -> float:
    """Budget torneo ancora disponibile (>= 0)."""
    return round(max(budget() - deployed(), 0.0), 4)


# ---------------------------------------------------------------------------
# Il punto di decisione: quanto vale lo stake di un pick già approvato
# ---------------------------------------------------------------------------

def stake_for(stake: float, *, pick: Optional[Dict[str, Any]] = None,
              capital: Optional[float] = None) -> Dict[str, Any]:
    """Stake effettivo in modalita' torneo. **Non solleva mai.**

    Ritorna `{stake, applied, blocked, reason, detail}`:
      - `applied=False`  -> il chiamante usa il PROPRIO stake (percorso standard);
      - `blocked=True`   -> il chiamante NON deve piazzare l'ordine
                            (`stake` = 0.0 e `reason` dice perche').

    Ordine dei controlli (nessuno scavalca il precedente):
      0. torneo spento / classe fuori perimetro  -> `off` (stake invariato);
      1. equity sotto la soglia di sicurezza     -> `safety_floor` (bloccato);
      2. budget torneo esaurito                  -> `budget_exhausted` (bloccato);
      3. altrimenti `min(stake x aggressione, cap per ordine, budget residuo)`
         e, se il risultato cade sotto il minimo ordine,  -> `below_min` (bloccato).
    """
    try:
        base = float(stake or 0.0)
    except (TypeError, ValueError):
        base = 0.0
    out: Dict[str, Any] = {"stake": max(base, 0.0), "applied": False,
                           "blocked": False, "reason": "off", "detail": ""}
    if base <= 0:
        return out
    try:
        if not active():
            out["reason"] = "mode_off"
            return out
        if not covers((pick or {}).get("mercato")):
            out["reason"] = "class_not_covered"
            return out
        # 1. Soglia di sicurezza: l'incremento del torneo non si applica quando
        #    il capitale e' gia' sceso sotto il pavimento dichiarato.
        try:
            cap_value = float(capital) if capital is not None else None
        except (TypeError, ValueError):
            cap_value = None
        floor = safety_floor()
        if cap_value is not None and cap_value < floor:
            out.update(blocked=True, stake=0.0, reason="safety_floor",
                       detail=f"equity {cap_value:.2f} < soglia {floor:.2f} USDC")
            return out
        # 2. Budget del torneo (esposizione aperta totale).
        left = budget_left()
        if left < min_stake():
            out.update(blocked=True, stake=0.0, reason="budget_exhausted",
                       detail=f"residuo {left:.2f} < minimo ordine "
                              f"{min_stake():.2f} USDC "
                              f"(budget {budget():.2f}, in gioco {deployed():.2f})")
            return out
        # 3. Incremento + cap.
        mult = aggression()
        raised = base * mult
        capped = min(raised, max_order_stake(), left)
        if capped < min_stake():
            out.update(blocked=True, stake=0.0, reason="below_min",
                       detail=f"stake torneo {capped:.2f} < minimo ordine "
                              f"{min_stake():.2f} USDC")
            return out
        out.update(stake=round(capped, 2), applied=True,
                   reason="tournament",
                   detail=f"x{mult:.2f} (base {base:.2f}) cap "
                          f"{max_order_stake():.2f} residuo {left:.2f}")
        return out
    except Exception as exc:                                     # fail-safe
        logger.warning("tournament: stake_for non applicato (%s)", exc)
        out["reason"] = "error"
        out["detail"] = str(exc)
        out["applied"] = False
        return out


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def status() -> Dict[str, Any]:
    """Istantanea per log/Telegram/CLI. Fail-safe."""
    info: Dict[str, Any] = {
        "label": LABEL, "active": False, "budget": budget(),
        "deployed": 0.0, "left": 0.0, "safety_floor": safety_floor(),
        "max_order_stake": max_order_stake(), "aggression": aggression(),
        "classes": list(classes()), "min_stake": min_stake(), "error": None,
    }
    try:
        info["active"] = active()
        info["deployed"] = deployed()
        info["left"] = budget_left()
    except Exception as exc:                                     # pragma: no cover
        info["error"] = str(exc)
    return info


def format_report(block: Optional[Dict[str, Any]] = None) -> str:
    """Riga di stato del torneo (Telegram/CLI)."""
    s = block if isinstance(block, dict) else status()
    if not s.get("active"):
        return (f"🏆 Torneo {s.get('label')}: SPENTO "
                f"(TOURNAMENT_MODE=0)")
    per = ", ".join(s.get("classes") or []) or "tutte le corsie"
    lines = [
        f"🏆 Torneo {s.get('label')}: ATTIVO",
        f"  • budget esposizione {s.get('deployed', 0):.2f}/"
        f"{s.get('budget', 0):.2f} USDC (residuo {s.get('left', 0):.2f})",
        f"  • aggressivita' x{float(s.get('aggression') or 1):.2f} | cap per "
        f"ordine {float(s.get('max_order_stake') or 0):.2f} USDC",
        f"  • soglia di sicurezza: equity >= "
        f"{float(s.get('safety_floor') or 0):.2f} USDC",
        f"  • perimetro: {per}",
        "  ℹ️ return-based: le perdite non tolgono punti, ma il tetto di "
        "capitale e i guardrail di portafoglio restano quelli del giro",
    ]
    if s.get("error"):
        lines.append(f"  ⚠️ {s['error']}")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:               # pragma: no cover
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    s = status()
    print(json.dumps(s, indent=2, ensure_ascii=False) if args.json
          else format_report(s))
    return 0


if __name__ == "__main__":                                       # pragma: no cover
    raise SystemExit(main())
