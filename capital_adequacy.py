"""Adeguatezza del capitale: il minimo ordine e' un rischio accettabile?

MOTIVO (27/10/2026). Il ledger live dice, dopo 101 puntate chiuse, -21,19 USDC
(-9,2% di ROI) su un'equity scesa da 47,16 (deposito del 09/09) a 16,24 USDC.
La causa misurata non era il modello ma la TAGLIA: un exchange ha un minimo
ordine (1 USDC su SX Bet) e con 16,24 USDC di capitale quel floor vale il
**6,2% del patrimonio per puntata**. Il Kelly vero su un edge del 2,5% a quota
1,90 chiede lo **0,56%**: il minimo dell'exchange e' **~11 volte** il rischio
voluto. Nessuna formula di staking puo' funzionare quando il vincolo minimo
del mercato supera il rischio che la strategia giustifica.

Questo modulo risponde a UNA domanda, con le formule di produzione (mai
ricopiate): **quanto capitale serve perche' il floor coincida col rischio
voluto?** E, di conseguenza, quanto rischia DAVVERO ogni puntata oggi.

    rischio_per_puntata = floor / equity
    rischio_voluto      = kelly_pieno(ev, quota) x k_dinamico
    capitale_minimo     = floor / rischio_voluto

Nota: `kelly_pieno` e `k` arrivano da `value_filter.kelly_fraction` e
`decision.stake_engine.dynamic_kelly_fraction` — le stesse fonti che
dimensionano gli ordini reali, cosi' questa diagnostica non puo' divergere dal
percorso del denaro.

Sola LETTURA, zero crediti, zero ordini, nessuna scrittura: si usa dalla
dashboard, da `/api/agents`, dai log e dalla CLI.

    venv/bin/python capital_adequacy.py [--json] [--equity N]
"""

from __future__ import annotations

import json
import logging
import os
import statistics
import sys
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Quota "rappresentativa" quando il ledger non ha puntate da cui stimare la
# mediana: e' il centro della fascia in cui il progetto gioca (1.30-2.00).
DEFAULT_ODDS = 1.90

# Soglie di lettura. NON sono decisioni di strategia: dicono solo come
# etichettare il numero (`adequate` / `thin` / `overbet`).
THIN_RATIO = 2.0      # capitale fra 1x e 2x il minimo -> "sottile"


def floor_stake() -> float:
    """Minimo ordine dell'exchange: definizione UNICA in `auto_bet`.

    Se `auto_bet` non e' importabile si ripiega su 1.0 (il floor SX Bet
    documentato), MA il campo `floor_source` lo dichiara: un numero copiato
    non deve sembrare una lettura.
    """
    try:
        import auto_bet
        return float(auto_bet.MIN_STAKE_EUR), "auto_bet"
    except Exception:
        return 1.0, "default"


def _ev_levels() -> List[float]:
    """I tre EV su cui si misura: la soglia minima, e due casi buoni."""
    try:
        import value_filter as vf
        base = float(vf.EV_MIN)
    except Exception:
        base = 0.025
    return [base, 0.05, 0.10]


def typical_odds(days: float = 30.0) -> Dict[str, Any]:
    """Mediana delle quote delle puntate live recenti (fallback dichiarato)."""
    try:
        from tracker import _get_conn  # import pigro: sola lettura
        conn = _get_conn()
        rows = conn.execute(
            "SELECT price FROM bets WHERE mode = 'live' AND price > 1.0 "
            "AND COALESCE(created_at, '') >= datetime('now', ?) "
            "ORDER BY price",
            (f"-{int(days)} days",),
        ).fetchall()
        vals = [float(r[0]) for r in rows if r and r[0]]
        if vals:
            return {"odds": round(float(statistics.median(vals)), 4),
                    "source": "ledger", "n": len(vals)}
    except Exception as exc:
        logger.debug("capital_adequacy: quote dal ledger non leggibili (%s)", exc)
    return {"odds": DEFAULT_ODDS, "source": "default", "n": 0}


def required_bankroll(*, ev: float, odds: float, k: Optional[float] = None,
                      floor: Optional[float] = None) -> Dict[str, Any]:
    """Capitale per cui `floor` coincide col rischio voluto (fail-closed).

    `p = (1 + ev) / odds` e' l'algebra dell'EV (la stessa di
    `auto_bet.true_probability`): una probabilita' coerente col prezzo, non
    una stima indipendente.
    """
    f = float(floor if floor is not None else floor_stake()[0])
    try:
        ev = float(ev)
        odds = float(odds)
    except (TypeError, ValueError):
        return {"ok": False, "reason": "invalid_inputs"}
    if odds <= 1.0 or f <= 0:
        return {"ok": False, "reason": "invalid_inputs"}
    try:
        import value_filter as vf
        prob = (1.0 + ev) / odds
        kelly_full = float(vf.kelly_fraction(prob, odds, fraction=1.0))
    except Exception as exc:
        return {"ok": False, "reason": f"engine_unavailable:{type(exc).__name__}"}
    if k is None:
        try:
            from decision.stake_engine import dynamic_kelly_fraction
            k = float(dynamic_kelly_fraction(ev=ev)["kelly_fraction"])
        except Exception:
            k = 0.20      # centro della banda 0.15-0.25 (documentato)
    risk = kelly_full * float(k)
    if risk <= 0:
        # Nessun edge: non esiste un capitale "adeguato" per una scommessa
        # senza valore. Si dichiara, non si inventa un numero.
        return {"ok": False, "reason": "no_edge", "ev": ev, "odds": odds,
                "kelly_full": kelly_full, "k": float(k)}
    return {"ok": True, "ev": round(ev, 4), "odds": round(odds, 4),
            "prob": round(prob, 6), "kelly_full": round(kelly_full, 6),
            "k": round(float(k), 4),
            "risk_pct": round(risk, 6),
            "required_bankroll": round(f / risk, 2),
            "floor": f}


def evaluate(equity: Optional[float] = None, *,
             odds: Optional[float] = None) -> Dict[str, Any]:
    """Istantanea completa: rischio reale oggi e capitale minimo per EV.

    Mai un'eccezione (la diagnostica non deve poter rompere una dashboard).
    """
    out: Dict[str, Any] = {"ok": False, "error": ""}
    try:
        floor, floor_src = floor_stake()
        out["floor"] = floor
        out["floor_source"] = floor_src
        if equity is None:
            try:
                from order_watch import current_equity
                equity = current_equity()
            except Exception as exc:
                out["error"] = f"equity non leggibile: {type(exc).__name__}"
        if equity is None:
            out["error"] = out["error"] or "equity non disponibile"
            return out
        eq = float(equity)
        out["equity"] = round(eq, 4)
        if eq <= 0:
            out["error"] = "equity non positiva"
            return out
        # Rischio REALE di una puntata al floor (la domanda operativa).
        out["risk_pct_now"] = round(floor / eq, 6)
        out["risk_pct_now_label"] = f"{floor / eq * 100:.2f}%"

        if odds is None:
            info = typical_odds()
            odds = info["odds"]
            out["odds_source"] = info["source"]
            out["odds_n"] = info["n"]
        else:
            out["odds_source"] = "input"
        out["odds"] = round(float(odds), 4)

        rows = []
        for ev in _ev_levels():
            r = required_bankroll(ev=ev, odds=float(odds), k=None, floor=floor)
            if r.get("ok"):
                r["adequate"] = eq >= r["required_bankroll"]
                rows.append(r)
            elif r.get("reason") == "no_edge":
                # Un EV sotto la soglia minima puo' non avere Kelly positivo
                # a certe quote: e' un'informazione, non un errore.
                rows.append({"ok": False, "reason": "no_edge",
                             "ev": round(float(ev), 4),
                             "odds": round(float(odds), 4)})
        out["levels"] = rows
        base = next((r for r in rows if r.get("ok")), None)
        out["ok"] = base is not None
        if base is None:
            out["error"] = "nessun livello con edge positivo"
            return out
        out["required_bankroll"] = base["required_bankroll"]
        out["risk_pct_wanted"] = base["risk_pct"]
        out["deficit"] = round(max(0.0, base["required_bankroll"] - eq), 2)
        ratio = eq / base["required_bankroll"] if base["required_bankroll"] else 0.0
        out["ratio"] = round(ratio, 3)
        if ratio >= 1.0:
            out["status"] = "adequate"
        elif ratio >= 1.0 / THIN_RATIO:
            out["status"] = "thin"
        else:
            out["status"] = "overbet"
        return out
    except Exception as exc:            # pragma: no cover - difesa estrema
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out


def format_report(res: Optional[Dict[str, Any]] = None) -> str:
    """Riga pronta per log/Telegram: dice sempre il PERCHE'."""
    r = res if res is not None else evaluate()
    if not r.get("ok"):
        return f"💼 Adeguatezza capitale: non misurabile ({r.get('error') or 'n/d'})"
    icon = {"adequate": "✅", "thin": "⚠️", "overbet": "🔴"}.get(
        r.get("status", ""), "❔")
    lines = [
        f"{icon} CAPITALE: equity {r['equity']:.2f} USDC · floor "
        f"{r['floor']:.2f} = {r['risk_pct_now_label']} per puntata",
        f"   rischio voluto (Kelly frazionato, EV {r['levels'][0]['ev'] * 100:.1f}%)"
        f" = {r['risk_pct_wanted'] * 100:.2f}% → servono "
        f"{r['required_bankroll']:.2f} USDC",
        f"   stato: {r.get('status')}"
        + (f" · mancano {r['deficit']:.2f} USDC" if r.get("deficit") else ""),
    ]
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:      # pragma: no cover
    argv = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in argv
    equity = None
    if "--equity" in argv:
        try:
            equity = float(argv[argv.index("--equity") + 1])
        except (IndexError, ValueError):
            print("uso: capital_adequacy.py [--json] [--equity N]")
            return 2
    res = evaluate(equity)
    if as_json:
        print(json.dumps(res, ensure_ascii=False, indent=1))
    else:
        print(format_report(res))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":                              # pragma: no cover
    raise SystemExit(main())
