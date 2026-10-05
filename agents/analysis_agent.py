"""Agente ANALISI — Steam Velocity & Juice Monitoring (direttiva 04/10/2026).

Perche' esiste: il prezzo a T-60 e' una FOTOGRAFIA; quello che distingue un
ritardo di mercato da un mercato che si sta muovendo e' il suo GRADIENTE. Questo
agente arricchisce ogni segnale con due letture che il prezzo non porta:

1. **Steam Velocity** — ΔQ/Δt dello sharp (`move_pct / span_minutes`, %/min)
   negli ultimi minuti della finestra: intercetta il flusso di denaro
   professionale (syndicate money). Delega a `steam_move.observe`, che registra
   gli snapshot in `price_snapshots` e misura lo storico REALE (mai inventato).
2. **Juice (overround)** — il margine implicito dello sharp e la sua VARIAZIONE
   fra due letture consecutive: un allargamento improvviso dell'aggio segnala
   instabilita' o informazioni di spogliatoio, e il segnale viene marcato
   `juice_anomaly`. La soglia scatta sul DELTA, non sul livello.

L'uscita e' tipizzata (`contracts.OracleSignal`, Pydantic) con controllo di
**freshness** del timestamp: un'osservazione troppo vecchia resta un DATO
(`fresh=False`) ma non e' giocabile.

Confini: questo agente NON decide nulla (nessuna soglia di gioco, nessun
ordine) e NON ricopia formule: lo steam e l'overround vivono in `steam_move` e
`pinnacle_oracle`. Il costo e' ZERO crediti (cache della rotazione quote +
storico locale). Fail-safe totale: qualunque errore degrada il singolo campo e
si dichiara (`steam_reason`/`juice_reason`), mai un'eccezione al chiamante.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from decision.models import Signal

from .contracts import (AnalysisOutput, DEFAULT_MAX_AGE_S, OracleSignal)

logger = logging.getLogger(__name__)

__all__ = ["AnalysisAgent", "juice_spike_pp", "juice_state_path", "max_age_s"]

MAX_AGE_ENV = "ANALYSIS_MAX_AGE_S"
JUICE_STATE_ENV = "ANALYSIS_JUICE_STATE"
JUICE_SPIKE_ENV = "ANALYSIS_JUICE_SPIKE_PP"
#: Allargamento dell'aggio (in punti percentuali) che marca `juice_anomaly`.
#: 1.5pp e' la banda tipica del rumore di pubblicazione dello sharp.
DEFAULT_JUICE_SPIKE_PP = 1.5


def _num_env(name: str, default: float, *, minimum: float = 0.0) -> float:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return max(float(raw), minimum)
    except (TypeError, ValueError):
        logger.warning("analysis: %s='%s' non numerico, uso %.2f", name, raw, default)
        return default


def max_age_s() -> float:
    """Eta' massima di un'osservazione perche' sia considerata fresca."""
    return _num_env(MAX_AGE_ENV, DEFAULT_MAX_AGE_S, minimum=1.0)


def juice_spike_pp() -> float:
    """Soglia di allargamento dell'aggio che accende `juice_anomaly`."""
    return _num_env(JUICE_SPIKE_ENV, DEFAULT_JUICE_SPIKE_PP, minimum=0.0)


def juice_state_path() -> Path:
    """Stato (ultimo overround per evento) sul volume: letta a RUNTIME.

    Isolata nei test (env) come le altre telemetrie: senza isolamento un giro
    di test scriverebbe nel file di PRODUZIONE (lezione del 03/10).
    """
    raw = os.getenv(JUICE_STATE_ENV)
    if raw:
        return Path(raw)
    from config import DATA_DIR
    return Path(DATA_DIR) / "decision" / "juice_state.json"


def _load_juice_state(path: Path) -> dict[str, float]:
    try:
        data = json.loads(path.read_text())
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, float] = {}
    for key, value in data.items():
        try:
            out[str(key)] = float(value)
        except (TypeError, ValueError):
            continue
    return out


def _save_juice_state(path: Path, state: dict[str, float]) -> None:
    """Scrittura atomica e fail-safe: la telemetria non rompe mai il ciclo."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=str(path.parent), delete=False,
                                         suffix=".tmp") as handle:
            json.dump(state, handle)
            tmp = handle.name
        os.replace(tmp, path)
    except Exception as exc:
        logger.debug("analysis: stato juice non salvato (%s)", exc)


class AnalysisAgent:
    """Arricchisce i segnali (steam velocity + juice + freshness). Iniettabile."""

    name = "analysis"

    def __init__(self, *, steam_fn: Optional[Callable] = None,
                 oracle_fn: Optional[Callable] = None,
                 names_fn: Optional[Callable] = None,
                 max_age_seconds: Optional[float] = None) -> None:
        # Tutte iniettabili: i test girano OFFLINE (nessuna rete, nessuna cache)
        # e un domani un feed alternativo entra da qui senza toccare l'agente.
        self.steam_fn = steam_fn
        self.oracle_fn = oracle_fn
        self.names_fn = names_fn
        self.max_age_seconds = max_age_seconds

    # -- dipendenze (import PIGRO: `import agents` resta leggero) ----------
    def _steam(self, home: str, away: str, match_id: str, esito: str,
               outcomes: Sequence[str]) -> dict[str, Any]:
        if self.steam_fn is not None:
            return dict(self.steam_fn(home, away, match_id, esito, outcomes) or {})
        import steam_move
        return dict(steam_move.observe(home, away, match_id, esito,
                                       tuple(outcomes)) or {})

    def _oracle(self, home: str, away: str) -> Optional[dict[str, Any]]:
        if self.oracle_fn is not None:
            got = self.oracle_fn(home, away)
        else:
            import pinnacle_oracle as po
            got = po.load_oracle(home, away)
        return dict(got) if got else None

    def _outcomes(self, market: str) -> Optional[tuple]:
        if self.steam_fn is not None:
            # Con uno steam_fn iniettato la forma del mercato e' del test:
            # gli esiti arrivano comunque a valle, qui basta non bloccare.
            return None
        import steam_move
        return steam_move.outcomes_for_market(market)

    def _names(self, match_id: str) -> Optional[dict[str, Any]]:
        if self.names_fn is not None:
            return dict(self.names_fn(match_id) or {}) or None
        from .data_agent import _match_rows   # RIUSO: nessuna seconda query
        return _match_rows(None, {match_id}).get(match_id)

    # -- ciclo -------------------------------------------------------------
    def process(self, signals: Sequence[Signal]) -> AnalysisOutput:
        out = AnalysisOutput()
        age_limit = max_age_s() if self.max_age_seconds is None \
            else float(self.max_age_seconds)
        state = _load_juice_state(juice_state_path())
        dirty = False
        for signal in signals or []:
            try:
                built = self._one(signal, age_limit=age_limit, state=state)
            except Exception as exc:  # un segnale rotto non ferma gli altri
                logger.debug("analysis: segnale %s non arricchito (%s)",
                             getattr(signal, "signal_id", "?"), exc)
                continue
            if built is None:
                continue
            out.signals.append(built)
            if built.steam_move:
                out.steam_moves += 1
            if built.juice_anomaly:
                out.juice_anomalies += 1
            if not built.fresh:
                out.stale += 1
            if built.juice is not None:
                dirty = True
        if dirty:
            _save_juice_state(juice_state_path(), state)
        return out

    def _one(self, signal: Signal, *, age_limit: float,
             state: dict[str, float]) -> Optional[OracleSignal]:
        match_id = str(getattr(signal, "match_id", "") or "")
        if not match_id:
            return None
        market = str(getattr(signal, "market", "") or "1X2")
        rows = self._names(match_id)
        home = str((rows or {}).get("home") or "")
        away = str((rows or {}).get("away") or "")
        league = str((rows or {}).get("league") or getattr(signal, "league", "") or "")

        # --- STEAM VELOCITY ---
        outcomes = self._outcomes(market)
        steam_info: dict[str, Any] = {}
        velocity = 0.0
        # Con `steam_fn` iniettato la forma del mercato e' responsabilita' del
        # chiamante (i test partono da qui): la dipendenza iniettata deve
        # VINCERE sul default, altrimenti il gancio resta morto e sembra
        # "mercato non supportato".
        steam_ok = bool(home and away) and (self.steam_fn is not None
                                            or outcomes is not None)
        if steam_ok:
            try:
                steam_info = self._steam(home, away, match_id,
                                         str(getattr(signal, "outcome", "") or ""),
                                         outcomes)
            except Exception as exc:
                steam_info = {"steam_move": False,
                              "reason": f"error:{type(exc).__name__}"}
        else:
            steam_info = {"steam_move": False, "reason": "unsupported_market"}
        move_pct = steam_info.get("move_pct")
        span = steam_info.get("span_minutes")
        if move_pct is not None and span:
            try:
                velocity = round(float(move_pct) / max(float(span), 1e-9), 4)
            except (TypeError, ValueError):
                velocity = 0.0

        # --- JUICE (overround dello sharp) + variazione ---
        juice = None
        juice_delta = None
        juice_anomaly = False
        juice_reason = ""
        oracle = self._oracle(home, away) if (home and away) else None
        # --- DE-VIG (direttiva 04/10/2026) ---------------------------------
        # L'EV NON si calcola sulle quote grezze: l'oracolo consegna la
        # probabilita' fair (de-vig, `shin` di default) e la sua quota equa.
        # Qui si ESPONE la provenienza, cosi' il gate EV a valle e' ispezionabile
        # (`devig_method`, parametro `z` di Shin) senza ricalcolare nulla.
        devig_method = str((oracle or {}).get("devig_method") or "")
        shin_z = (oracle or {}).get("shin_z")
        fair_odds = None
        if oracle:
            _p = oracle.get(str(getattr(signal, "outcome", "") or ""))
            try:
                _p = float(_p)
                if 0.0 < _p <= 1.0:
                    fair_odds = round(1.0 / _p, 6)
            except (TypeError, ValueError):
                fair_odds = None
        if oracle and oracle.get("overround") is not None:
            try:
                juice = round(float(oracle["overround"]), 6)
            except (TypeError, ValueError):
                juice = None
        if juice is None:
            juice_reason = "no_sharp_cache"
        else:
            key = f"{match_id}|{market}"
            prev = state.get(key)
            if prev is not None:
                juice_delta = round((juice - float(prev)) * 100.0, 4)  # punti %
                if juice_delta >= juice_spike_pp():
                    juice_anomaly = True
                    juice_reason = (f"aggio in allargamento "
                                    f"(+{juice_delta:.2f}pp in un giro)")
                else:
                    juice_reason = "stabile"
            else:
                juice_reason = "prima_lettura"
            state[key] = juice

        observed = datetime.now(timezone.utc)
        sample = getattr(signal, "observed_at", None)
        if sample is not None:
            try:
                ts = sample if isinstance(sample, datetime) else \
                    datetime.fromisoformat(str(sample).replace("Z", "+00:00"))
                if ts.tzinfo is not None:
                    observed = ts.astimezone(timezone.utc)
            except Exception:
                observed = datetime.now(timezone.utc)

        depth = getattr(getattr(signal, "data_quality", None), "depth_usdc", None)
        return OracleSignal(
            signal_id=str(getattr(signal, "signal_id", "") or match_id),
            match_id=match_id,
            market=market,
            esito=str(getattr(signal, "outcome", "") or ""),
            price=float(getattr(signal, "price", 0.0) or 0.0),
            true_prob=getattr(signal, "blended_prob", None),
            ev=getattr(signal, "ev", None),
            edge=getattr(signal, "edge", None),
            league=league,
            home=home,
            away=away,
            kickoff=getattr(signal, "kickoff", None),
            steam_move=bool(steam_info.get("steam_move")),
            move_pct=float(move_pct) if move_pct is not None else None,
            span_minutes=float(span) if span else None,
            velocity_pct_min=velocity,
            steam_reason=str(steam_info.get("reason") or ""),
            juice=juice,
            juice_delta=juice_delta,
            juice_anomaly=juice_anomaly,
            juice_reason=juice_reason,
            observed_at=observed,
            max_age_s=age_limit,
            sources=list(oracle.get("sources") or []) if oracle else [],
            depth_usdc=float(depth) if depth is not None else None,
            devig_method=devig_method,
            shin_z=float(shin_z) if shin_z is not None else None,
            fair_odds=fair_odds,
        )
